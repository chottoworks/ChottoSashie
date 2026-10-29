"""ComfyUIの途中経過（どのノードを実行中か・何ステップ目か）を受け取る。

完了したかどうかは `/history` で分かるが、途中経過は WebSocket（`/ws?clientId=…`）
でしか流れてこない。しかも ComfyUI は、そのプロンプトを依頼したときの
`client_id` でつないでいる相手にしか送らない。そこで ComfyUI ごとに1本だけ
つなぎっぱなしにし、このアプリからの依頼はすべてその `client_id` で送る
（`ComfyClient.submit`）。届いた通知はプロンプトごとに覚えておき、監視側は
`/history` を待つ合間にそれを読んで、ジョブの進み具合と表示文へ写す。

途中経過は「見せるための情報」なので、つなげない・途中で切れたときでも生成は
止めない。その場合は従来どおり、完了するまで段階の文だけを出す。

進み具合の割合は、ワークフローのノードを種類ごとの重みで足し合わせて出す。
ComfyUIは全体の割合を持たない（ノードごとの段数だけを送る）ため、時間の
大半を占めるサンプラーを重く、読み込み・VAEデコードをその次に置く。
サンプラーの中は、届いたステップ数でなめらかに進める。
"""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import InvalidHandshake, InvalidURI, WebSocketException

from .jobs import JobRepository, JobTransition

# 覚えておくプロンプトの数。終わったものから古い順に捨てる。
_KEPT_PROMPTS = 64
_CONNECT_TIMEOUT_SECONDS = 5.0
# プレビュー画像（バイナリ）も同じ線で届く。大きめの1枚で切られないようにする。
_MAX_MESSAGE_BYTES = 16 * 1024 * 1024

_SAMPLER_CLASSES = frozenset(
    {"KSampler", "KSamplerAdvanced", "SamplerCustom", "SamplerCustomAdvanced"}
)
_SAMPLER_WEIGHT = 20.0
_LOADER_WEIGHT = 2.0
_DECODE_WEIGHT = 3.0
_OTHER_WEIGHT = 1.0


def _is_loader(class_type: str) -> bool:
    # UNETLoader / CLIPLoader / VAELoader / LoraLoaderModelOnly / CheckpointLoaderSimple など。
    # 画像を読むだけの LoadImage は含めない。
    return "Loader" in class_type


def _is_decode(class_type: str) -> bool:
    return class_type.startswith("VAEDecode") and class_type != "VAEDecodeAudio"


def _is_text_encode(class_type: str) -> bool:
    return "TextEncode" in class_type


def _is_output(class_type: str) -> bool:
    return class_type.startswith("Save") or class_type == "CreateVideo"


def _weight(class_type: str) -> float:
    if class_type in _SAMPLER_CLASSES:
        return _SAMPLER_WEIGHT
    if _is_decode(class_type):
        return _DECODE_WEIGHT
    if _is_loader(class_type):
        return _LOADER_WEIGHT
    return _OTHER_WEIGHT


@dataclass(frozen=True)
class PromptProgress:
    """1件のプロンプトの途中経過。`fraction` は 0〜1、`phase` は今の段階の短い説明。"""

    fraction: float
    phase: str | None

    def describe(self, message: str) -> str:
        """ジョブの表示文へ、今の段階を添える。"""
        return f"{message}（{self.phase}）" if self.phase else message


@dataclass
class _PromptState:
    classes: dict[str, str] = field(default_factory=dict)
    done: set[str] = field(default_factory=set)
    current: str | None = None
    value: float = 0.0
    maximum: float = 0.0
    started: bool = False
    finished: bool = False
    best: float = 0.0

    def class_of(self, node: str | None) -> str | None:
        if node is None:
            return None
        if node in self.classes:
            return self.classes[node]
        # 展開されたノード（"12.0.3" のような子）は、元のノードとして数える。
        return self.classes.get(node.split(".", 1)[0])

    def finish_current(self) -> None:
        if self.current is not None:
            self.done.add(self.current)
        self.current = None
        self.value = 0.0
        self.maximum = 0.0

    def snapshot(self) -> PromptProgress | None:
        if not self.classes or not self.started:
            return None
        total = sum(_weight(class_type) for class_type in self.classes.values())
        done_nodes = {node for node in self.done if node in self.classes}
        completed = sum(_weight(self.classes[node]) for node in done_nodes)
        current_class = self.class_of(self.current)
        phase: str | None = None
        if current_class is not None and self.current not in done_nodes:
            if self.maximum > 0:
                completed += _weight(current_class) * min(1.0, self.value / self.maximum)
            phase = self._phase(current_class, done_nodes)
        if self.finished:
            fraction = 1.0
        else:
            fraction = min(1.0, completed / total) if total else 0.0
        # 同じノードが2度数えられる・ステップが巻き戻るなどで、表示が後戻りしないようにする。
        self.best = max(self.best, fraction)
        return PromptProgress(fraction=self.best, phase=phase)

    def _phase(self, class_type: str, done_nodes: set[str]) -> str | None:
        if class_type in _SAMPLER_CLASSES:
            samplers = [node for node, kind in self.classes.items() if kind in _SAMPLER_CLASSES]
            steps = (
                f"{int(self.value)}/{int(self.maximum)} ステップ" if self.maximum > 0 else None
            )
            if len(samplers) > 1:
                order = sum(1 for node in samplers if node in done_nodes) + 1
                label = f"サンプリング {order}/{len(samplers)}"
                return f"{label}：{steps}" if steps else label
            return f"サンプリング {steps}" if steps else "サンプリングしています"
        if _is_loader(class_type):
            return "モデルを読み込んでいます"
        if _is_text_encode(class_type):
            return "プロンプトをエンコードしています"
        if _is_decode(class_type):
            return "VAEデコードしています"
        if _is_output(class_type):
            return "書き出しています"
        return None


class ComfyProgressWatcher:
    """1つのComfyUIへつなぎっぱなしにし、このアプリの依頼の途中経過を覚えておく。"""

    def __init__(self, base_url: str) -> None:
        self.client_id = uuid4().hex
        scheme, _, rest = base_url.rstrip("/").partition("://")
        ws_scheme = "wss" if scheme == "https" else "ws"
        self._url = f"{ws_scheme}://{rest}/ws?clientId={self.client_id}"
        self._prompts: OrderedDict[str, _PromptState] = OrderedDict()
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def ensure_listening(self) -> bool:
        """つながっていなければつなぐ。つながっていれば True。

        依頼より先につないでおかないと、始まってすぐの通知を取りこぼす。
        """
        async with self._lock:
            if self._task is not None and not self._task.done():
                return True
            try:
                connection = await connect(
                    self._url,
                    open_timeout=_CONNECT_TIMEOUT_SECONDS,
                    max_size=_MAX_MESSAGE_BYTES,
                )
            except (OSError, TimeoutError, InvalidURI, InvalidHandshake):
                return False
            self._task = asyncio.create_task(self._listen(connection))
            return True

    def track(self, prompt_id: str, workflow: dict[str, Any]) -> None:
        """依頼したワークフローのノード構成を覚える（割合の計算に使う）。"""
        state = self._state(prompt_id)
        state.classes = {
            str(node_id): node["class_type"]
            for node_id, node in workflow.items()
            if isinstance(node, dict) and isinstance(node.get("class_type"), str)
        }

    def progress(self, prompt_id: str) -> PromptProgress | None:
        """届いている途中経過。まだ始まっていない・何も届いていないときは None。"""
        state = self._prompts.get(prompt_id)
        return state.snapshot() if state is not None else None

    async def close(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 終了処理は握りつぶす
                pass

    def handle(self, message: dict[str, Any]) -> None:
        """ComfyUIから届いた1件の通知を、プロンプトごとの状態へ反映する。"""
        kind = message.get("type")
        data = message.get("data")
        if not isinstance(data, dict):
            return
        prompt_id = data.get("prompt_id")
        if not isinstance(prompt_id, str):
            return
        state = self._state(prompt_id)
        if kind == "execution_start":
            state.started = True
        elif kind == "execution_cached":
            state.started = True
            nodes = data.get("nodes")
            if isinstance(nodes, list):
                state.done.update(str(node) for node in nodes)
        elif kind == "executing":
            state.started = True
            node = data.get("node")
            if node is None:
                state.finish_current()
                state.finished = True
            elif str(node) != state.current:
                state.finish_current()
                state.current = str(node)
        elif kind == "progress":
            state.started = True
            value, maximum = data.get("value"), data.get("max")
            if isinstance(value, (int, float)) and isinstance(maximum, (int, float)):
                node = data.get("node")
                if isinstance(node, str) and state.current is None:
                    state.current = node
                state.value, state.maximum = float(value), float(maximum)
        elif kind == "executed":
            node = data.get("node")
            if isinstance(node, str):
                state.done.add(node)
        elif kind in {"execution_success", "execution_error", "execution_interrupted"}:
            state.finish_current()
            state.finished = kind == "execution_success"

    def _state(self, prompt_id: str) -> _PromptState:
        state = self._prompts.get(prompt_id)
        if state is None:
            state = self._prompts[prompt_id] = _PromptState()
            while len(self._prompts) > _KEPT_PROMPTS:
                self._prompts.popitem(last=False)
        return state

    async def _listen(self, connection: ClientConnection) -> None:
        try:
            async for raw in connection:
                # バイナリはプレビュー画像。進み具合には使わない。
                if not isinstance(raw, str):
                    continue
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(message, dict):
                    self.handle(message)
        except (OSError, WebSocketException):
            # ComfyUIが止まった・再起動した。次の依頼のときにつなぎ直す。
            return
        finally:
            await connection.close()


# ComfyUIのURLごとに1本。つなぎ先とタスクはイベントループに紐づくため、
# ループが変わったら（試験などで）作り直す。
_watchers: dict[str, tuple[asyncio.AbstractEventLoop, ComfyProgressWatcher]] = {}


def progress_watcher(base_url: str) -> ComfyProgressWatcher:
    key = base_url.rstrip("/")
    loop = asyncio.get_running_loop()
    entry = _watchers.get(key)
    if entry is None or entry[0] is not loop:
        entry = _watchers[key] = (loop, ComfyProgressWatcher(key))
    return entry[1]


async def close_progress_watchers() -> None:
    """アプリ終了時に、つなぎっぱなしの線をすべて閉じる。"""
    loop = asyncio.get_running_loop()
    for key, (owner, watcher) in list(_watchers.items()):
        if owner is loop:
            await watcher.close()
            del _watchers[key]


class JobProgressWriter:
    """実行中のジョブへ、変わったときだけ進み具合と表示文を書く。

    途中経過は0.5秒ごとに読むので、同じ内容を毎回書き込まないようにする。
    """

    def __init__(self, jobs: JobRepository, job_id: str) -> None:
        self._jobs = jobs
        self._job_id = job_id
        self._last: tuple[float, str] | None = None

    def write(self, progress: float, message: str) -> None:
        current = (round(progress, 3), message)
        if current == self._last:
            return
        self._jobs.transition(
            self._job_id, JobTransition(state="running", progress=progress, message=message)
        )
        self._last = current
