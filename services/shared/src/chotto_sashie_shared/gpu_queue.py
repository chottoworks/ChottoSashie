"""GPUを使う処理の順番待ち（複数アプリ共有）。

単一利用者・単一GPUなので重い処理は常に一件ずつしか動かせない。実行中に来た
新しい要求を断ると、利用者から見れば「あとで自分でやり直す」しかなく、
待ち時間の管理を人に押しつけることになる。ここでは断らずに順番待ちへ入れ、
前の処理が終わったら次を自動で始める。

順番はジョブの作成順（`queued` のまま待つ）で、実行はこのプロセス内の worker が
一件ずつ取り出して行う。API再起動をまたぐと待ち行列の実行予定は失われるため、
起動時に `queued` のまま残ったジョブは中断として扱う（`JobRepository` 側）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence

from .jobs import JobCreate, JobRead, JobRepository, JobTransition

# 実行本体。ジョブIDを受け取り、そのジョブを終わらせるまで面倒を見る。
JobRunner = Callable[[], Awaitable[None]]


class GpuQueue:
    """GPUを使うジョブを作成し、一件ずつ順に実行する唯一の入口。

    `submit` はジョブを作って待ち行列へ積み、すぐ返る。呼び出し側は
    ジョブIDを使って進み具合を見る（待っている間は `queued` のまま）。
    """

    def __init__(self, job_kinds: Sequence[str]) -> None:
        self._job_kinds = tuple(job_kinds)
        self._pending: asyncio.Queue[tuple[str, JobRunner]] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._worker: asyncio.Task | None = None
        self._running_job_id: str | None = None

    @property
    def job_kinds(self) -> tuple[str, ...]:
        return self._job_kinds

    def _ensure_queue(self) -> asyncio.Queue[tuple[str, JobRunner]]:
        """いま動いているイベントループに紐づく待ち行列を返す。

        待ち行列とタスクはループに紐づく。モジュール読み込み時に作ると、別の
        ループで動かしたとき（試験など）に使えないため、使う時点で用意する。
        """
        loop = asyncio.get_running_loop()
        if self._pending is None or self._loop is not loop:
            self._pending = asyncio.Queue()
            self._loop = loop
            self._worker = None
        return self._pending

    def start(self) -> None:
        """待ち行列の実行役を起こす（アプリ起動時に一度だけ呼ぶ）。"""
        self._ensure_queue()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        """実行役を止める（アプリ終了時）。実行中の処理は所有プロセス側が止める。"""
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.cancel()
            try:
                await worker
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 終了処理は握りつぶす
                pass

    async def submit(
        self, repository: JobRepository, request: JobCreate, runner_factory: Callable[[str], JobRunner]
    ) -> JobRead:
        """ジョブを作って順番待ちへ積む。作成と登録は不可分に行う。

        `runner_factory` はジョブIDを受け取り、実行本体を返す。ジョブを作ってから
        実行本体を組み立てるため、順番が確定してからでないと作れない情報も渡せる。
        """
        pending = self._ensure_queue()
        job = repository.create(request)
        pending.put_nowait((job.id, runner_factory(job.id)))
        self.start()
        return job

    def waiting_ahead(self, repository: JobRepository, job_id: str) -> int:
        """そのジョブの前に何件待っているか。実行中の1件も数に入れる。"""
        ahead = 0
        for job in repository.list_active(self._job_kinds):
            if job.id == job_id:
                return ahead
            ahead += 1
        return ahead

    async def _run_forever(self) -> None:
        pending = self._ensure_queue()
        while True:
            job_id, runner = await pending.get()
            self._running_job_id = job_id
            try:
                await runner()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - 一件の失敗で待ち行列を止めない
                print(f"[gpu-queue] ジョブ {job_id} が失敗しました: {error}")
            finally:
                self._running_job_id = None
                pending.task_done()


def mark_queue_lost(repository: JobRepository, kinds: Sequence[str]) -> int:
    """再起動で実行予定が失われた順番待ちを中断として片付ける。

    待ち行列はプロセス内にしか無いため、`queued` のまま残ったジョブは誰も
    実行しない。放置すると「順番待ち」の表示が消えないので、起動時に畳む。
    """
    lost = 0
    for job in repository.list_active(kinds):
        if job.state.value != "queued":
            continue
        try:
            # queued から移れるのは preparing / cancelled / failed。
            # 誰も実行しないまま残った順番待ちは「取り消された」が実態に近い。
            repository.transition(
                job.id,
                JobTransition(
                    state="cancelled",
                    message="アプリ終了により順番待ちが取り消されました",
                ),
            )
            lost += 1
        except Exception:  # noqa: BLE001 - 片付けは best-effort
            continue
    return lost
