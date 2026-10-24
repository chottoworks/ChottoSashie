"""このAPIが起動した子プロセスだけを追跡し、終了時に確実に停止する（共通の土台）。

`RuntimeManager` と同じ所有権の原則に従う。ここで扱うのはAPIが自分で起動した
学習・生成の子プロセスだけで、利用者が別途起動した外部プロセスには触れない。

追跡しないと、API終了後も学習プロセスだけが残ってGPUとチェックポイント保存先を
掴み続ける。データベース上は中断扱いなのに実体が動いている状態になり、
チェックポイントからの再開が衝突する。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


logger = logging.getLogger(__name__)

# Windowsでコンソールウィンドウを出さずに子プロセスを起動する。
_CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# 進み具合の表示は改行ではなく復帰で上書きされる。どちらでも区切る。
_SEGMENT_SEPARATOR = re.compile("[\r\n]")


async def _relay_stderr(
    stream: asyncio.StreamReader, sink: object, on_text: Callable[[str], None]
) -> None:
    """標準エラー出力を記録へ書きながら、1区切りずつ渡す。

    進み具合の表示は改行ではなく復帰で上書きされるため、行単位で待つと
    処理が終わるまで何も届かない。どちらの区切りでも切り出す。
    """
    pending = ""
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        try:
            sink.write(chunk)  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            logger.warning("子プロセスの記録に書けませんでした", exc_info=True)
        pending += chunk.decode("utf-8", errors="replace")
        segments = _SEGMENT_SEPARATOR.split(pending)
        pending = segments.pop()
        for segment in segments:
            _notify(on_text, segment)
    _notify(on_text, pending)


def _notify(on_text: Callable[[str], None], segment: str) -> None:
    text = segment.strip()
    if not text:
        return
    try:
        on_text(text)
    except Exception:  # noqa: BLE001 - 進み具合の表示で処理そのものを止めない
        logger.warning("進み具合の受け取りに失敗しました", exc_info=True)


@dataclass(frozen=True)
class OwnedProcess:
    """APIが所有する実行中の子プロセス一件。"""

    pid: int
    label: str
    job_id: str | None


class OwnedProcessRegistry:
    """所有する子プロセスの起動・待機・一括停止をまとめる。

    起動も停止も単一のイベントループ上で行うため、追加の排他は設けない。
    """

    def __init__(self) -> None:
        self._processes: dict[int, tuple[asyncio.subprocess.Process, OwnedProcess]] = {}

    def running(self) -> list[OwnedProcess]:
        return [record for _, record in self._processes.values()]

    async def run(
        self,
        command: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        stdout: object,
        stderr: object,
        label: str,
        job_id: str | None = None,
        on_started: Callable[[OwnedProcess], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
    ) -> int:
        """子プロセスを起動し、追跡しながら終了コードを返す。

        `on_stderr` を渡すと、標準エラー出力を1区切りずつ読みながら渡す。
        学習のような長い処理は進み具合をここにしか出さないため、記録へ書きつつ
        同じ内容を見て、待っている利用者へ現在地を伝えられるようにする。
        """
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            env=environment,
            stdout=stdout,
            stderr=asyncio.subprocess.PIPE if on_stderr is not None else stderr,
            creationflags=_CREATION_FLAGS,
        )
        record = OwnedProcess(pid=process.pid, label=label, job_id=job_id)
        self._processes[process.pid] = (process, record)
        if on_started is not None:
            # 記録に失敗しても、起動済みの処理そのものは続行させる。
            try:
                on_started(record)
            except Exception:  # noqa: BLE001 - 記録は補助情報のため停止理由にしない
                logger.warning("子プロセスの記録に失敗しました: pid=%s", record.pid, exc_info=True)
        try:
            if on_stderr is not None and process.stderr is not None:
                await _relay_stderr(process.stderr, stderr, on_stderr)
            return await process.wait()
        finally:
            self._processes.pop(process.pid, None)

    async def terminate_job(self, job_id: str, *, timeout_seconds: float = 15.0) -> list[OwnedProcess]:
        """指定したジョブの子プロセスだけを停止する（利用者による中止）。

        止まらなければ強制終了する。該当が無ければ何もしない（すでに終わっている）。
        """
        targets = [
            (process, record)
            for process, record in self._processes.values()
            if record.job_id == job_id and process.returncode is None
        ]
        stopped: list[OwnedProcess] = []
        for process, record in targets:
            logger.info("中止のため子プロセスを停止します: %s pid=%s", record.label, record.pid)
            try:
                process.terminate()
            except ProcessLookupError:
                continue
            stopped.append(record)
        for process, record in targets:
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
            except (TimeoutError, asyncio.TimeoutError):
                logger.warning(
                    "子プロセスが時間内に終了しないため強制終了します: %s pid=%s",
                    record.label,
                    record.pid,
                )
                try:
                    process.kill()
                    await process.wait()
                except ProcessLookupError:
                    pass
        return stopped

    async def terminate_all(self, *, timeout_seconds: float = 15.0) -> list[OwnedProcess]:
        """所有する子プロセスをすべて停止し、停止したものを返す。"""
        snapshot = list(self._processes.values())
        stopped: list[OwnedProcess] = []
        for process, record in snapshot:
            if process.returncode is not None:
                continue
            logger.info("所有する子プロセスを停止します: %s pid=%s", record.label, record.pid)
            try:
                process.terminate()
            except ProcessLookupError:
                continue
            stopped.append(record)

        for process, record in snapshot:
            if process.returncode is not None:
                continue
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
            except (TimeoutError, asyncio.TimeoutError):
                logger.warning(
                    "子プロセスが時間内に終了しないため強制終了します: %s pid=%s",
                    record.label,
                    record.pid,
                )
                try:
                    process.kill()
                    await process.wait()
                except ProcessLookupError:
                    pass
        return stopped


# APIプロセス全体で共有する、所有する子プロセスの登録簿。
owned_processes = OwnedProcessRegistry()
