"""ComfyUIの途中経過（WebSocketの通知）を、進み具合と表示文へ写す。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from websockets.asyncio.server import ServerConnection, serve

from chotto_sashie_shared.comfy_monitor import (
    RUNNING_END,
    RUNNING_START,
    MonitorMessages,
    MonitorSettings,
    monitor_comfy_prompt,
)
from chotto_sashie_shared.comfy_progress import (
    ComfyProgressWatcher,
    PromptProgress,
    close_progress_watchers,
    progress_watcher,
)
from chotto_sashie_shared.jobs import JobCreate, JobRepository, JobTransition
from chotto_sashie_api.database import Database

# Wan 2.2 の I2V と同じ形（読み込み → 高ノイズ → 低ノイズ → VAEデコード → 書き出し）。
_WORKFLOW = {
    "1": {"class_type": "UNETLoader", "inputs": {}},
    "2": {"class_type": "CLIPTextEncode", "inputs": {}},
    "11": {"class_type": "KSamplerAdvanced", "inputs": {}},
    "12": {"class_type": "KSamplerAdvanced", "inputs": {}},
    "13": {"class_type": "VAEDecode", "inputs": {}},
    "14": {"class_type": "SaveAnimatedWEBP", "inputs": {}},
    "_chotto_sashie_bindings": {},
}


def _event(kind: str, **data: object) -> dict[str, object]:
    return {"type": kind, "data": {"prompt_id": "p1", **data}}


def _watcher() -> ComfyProgressWatcher:
    watcher = ComfyProgressWatcher("http://127.0.0.1:8188")
    watcher.track("p1", _WORKFLOW)
    return watcher


def test_nothing_is_reported_before_the_prompt_starts() -> None:
    assert _watcher().progress("p1") is None
    assert _watcher().progress("unknown") is None


def test_sampler_steps_are_shown_with_which_sampler() -> None:
    watcher = _watcher()
    watcher.handle(_event("execution_start"))
    watcher.handle(_event("executing", node="1"))
    loading = watcher.progress("p1")
    assert loading == PromptProgress(fraction=0.0, phase="モデルを読み込んでいます")

    watcher.handle(_event("executing", node="2"))
    watcher.handle(_event("executing", node="11"))
    watcher.handle(_event("progress", node="11", value=1, max=4))
    first = watcher.progress("p1")
    assert first is not None
    assert first.phase == "サンプリング 1/2：1/4 ステップ"
    assert first.fraction > loading.fraction

    watcher.handle(_event("progress", node="11", value=4, max=4))
    watcher.handle(_event("executing", node="12"))
    watcher.handle(_event("progress", node="12", value=2, max=4))
    second = watcher.progress("p1")
    assert second is not None
    assert second.phase == "サンプリング 2/2：2/4 ステップ"
    assert second.fraction > first.fraction

    watcher.handle(_event("executing", node="13"))
    decoding = watcher.progress("p1")
    assert decoding is not None
    assert decoding.phase == "VAEデコードしています"
    assert decoding.fraction > second.fraction

    watcher.handle(_event("executing", node="14"))
    assert watcher.progress("p1").phase == "書き出しています"  # type: ignore[union-attr]
    watcher.handle(_event("executing", node=None))
    assert watcher.progress("p1") == PromptProgress(fraction=1.0, phase=None)


def test_cached_nodes_count_as_done() -> None:
    watcher = _watcher()
    watcher.handle(_event("execution_start"))
    watcher.handle(_event("execution_cached", nodes=["1", "2"]))
    watcher.handle(_event("executing", node="11"))
    progress = watcher.progress("p1")
    assert progress is not None
    assert progress.fraction > 0
    assert progress.phase == "サンプリング 1/2"


def test_progress_never_goes_backwards() -> None:
    watcher = _watcher()
    watcher.handle(_event("execution_start"))
    watcher.handle(_event("executing", node="11"))
    watcher.handle(_event("progress", node="11", value=3, max=4))
    ahead = watcher.progress("p1")
    watcher.handle(_event("progress", node="11", value=1, max=4))
    assert watcher.progress("p1").fraction == ahead.fraction  # type: ignore[union-attr]


def test_single_sampler_shows_steps_only() -> None:
    watcher = ComfyProgressWatcher("http://127.0.0.1:8188")
    watcher.track("p1", {"7": {"class_type": "KSampler"}, "8": {"class_type": "SaveImage"}})
    watcher.handle(_event("executing", node="7"))
    watcher.handle(_event("progress", node="7", value=3, max=8))
    progress = watcher.progress("p1")
    assert progress is not None
    assert progress.phase == "サンプリング 3/8 ステップ"
    assert progress.describe("素材を生成しています") == (
        "素材を生成しています（サンプリング 3/8 ステップ）"
    )


def test_events_for_other_prompts_are_kept_apart() -> None:
    watcher = _watcher()
    watcher.handle({"type": "executing", "data": {"prompt_id": "other", "node": "11"}})
    assert watcher.progress("p1") is None


def test_watcher_listens_with_its_own_client_id() -> None:
    """依頼に使う client_id でつなぎ、届いた通知をプロンプトごとに覚える。"""
    received: list[str] = []

    async def handler(connection: ServerConnection) -> None:
        received.append(connection.request.path)
        await connection.send(json.dumps(_event("execution_start")))
        await connection.send(b"\x00\x00\x00\x01preview")
        await connection.send(json.dumps(_event("executing", node="11")))
        await connection.send(json.dumps(_event("progress", node="11", value=2, max=4)))
        await connection.wait_closed()

    async def scenario() -> PromptProgress | None:
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            watcher = progress_watcher(f"http://127.0.0.1:{port}")
            assert await watcher.ensure_listening() is True
            watcher.track("p1", _WORKFLOW)
            for _ in range(100):
                progress = watcher.progress("p1")
                if progress is not None and progress.phase and "2/4" in progress.phase:
                    break
                await asyncio.sleep(0.01)
            assert received == [f"/ws?clientId={watcher.client_id}"]
            await close_progress_watchers()
            return progress

    progress = asyncio.run(scenario())
    assert progress is not None
    assert progress.phase == "サンプリング 1/2：2/4 ステップ"


def test_watcher_gives_up_quietly_when_comfyui_is_not_there() -> None:
    async def scenario() -> bool:
        watcher = ComfyProgressWatcher("http://127.0.0.1:9")
        return await watcher.ensure_listening()

    assert asyncio.run(scenario()) is False


class _ProgressingClient:
    """3回目の確認で終わるComfyUI。それまではサンプリングの途中経過を返す。"""

    def __init__(self) -> None:
        self.polls = 0

    def prompt_progress(self, prompt_id: str) -> PromptProgress | None:  # noqa: ARG002
        return PromptProgress(fraction=0.5, phase="サンプリング 2/4 ステップ")

    async def history(self, prompt_id: str) -> dict[str, object] | None:  # noqa: ARG002
        self.polls += 1
        if self.polls < 3:
            return None
        return {
            "status": {"completed": True, "status_str": "success"},
            "outputs": {"9": {"images": [{"filename": "a.png", "subfolder": "", "type": "output"}]}},
        }


def test_monitor_writes_comfyui_progress_into_the_job(tmp_path: Path) -> None:
    database = Database(tmp_path / "jobs.sqlite3")
    database.initialize()
    jobs = JobRepository(database)
    job = jobs.create(JobCreate(kind="generation.turbo"))
    jobs.transition(job.id, JobTransition(state="preparing"))
    seen: list[tuple[float, str | None]] = []

    async def on_success(_files: list[dict[str, str]]) -> dict[str, object]:
        return {}

    async def scenario() -> None:
        client = _ProgressingClient()
        original = jobs.transition

        def recording(job_id: str, update: JobTransition):  # type: ignore[no-untyped-def]
            result = original(job_id, update)
            seen.append((result.progress, result.message))
            return result

        jobs.transition = recording  # type: ignore[method-assign]
        await monitor_comfy_prompt(
            client=client,  # type: ignore[arg-type]
            jobs=jobs,
            job_id=job.id,
            prompt_id="p1",
            on_success=on_success,
            messages=MonitorMessages(
                running="素材を生成しています",
                saving="保存しています",
                completed="できました",
                failed="失敗しました",
                failed_code="F",
                timeout="時間切れ",
                timeout_code="T",
                monitor_failed_code="M",
            ),
            settings=MonitorSettings(poll_seconds=0.001, max_polls=10),
        )

    asyncio.run(scenario())
    middle = RUNNING_START + (RUNNING_END - RUNNING_START) * 0.5
    assert (pytest.approx(middle), "素材を生成しています（サンプリング 2/4 ステップ）") in seen
    # 同じ内容は書き直さない。
    assert sum(1 for _, message in seen if message and "2/4" in message) == 1
    assert jobs.get(job.id).state.value == "completed"
