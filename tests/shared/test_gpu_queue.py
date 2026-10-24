"""GPUを使う処理の順番待ちを確かめる。

断らずに受け取り、前の処理が終わってから次を始めることが要。
"""

import asyncio
from pathlib import Path

from chotto_sashie_shared.gpu_queue import GpuQueue, mark_queue_lost
from chotto_sashie_shared.jobs import JOBS_TABLE_DDL, JobCreate, JobRepository, JobTransition
from chotto_sashie_shared.sqlite import SqliteDatabase

_KINDS = ("generation.asset", "composition.scene")


def _repository(tmp_path: Path) -> JobRepository:
    database = SqliteDatabase(tmp_path / "jobs.sqlite3", JOBS_TABLE_DDL)
    database.initialize()
    return JobRepository(database)


def test_jobs_run_one_at_a_time_in_order(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    queue = GpuQueue(_KINDS)
    order: list[str] = []
    overlapping = False

    async def scenario() -> None:
        nonlocal overlapping
        running = 0

        def make_runner(name: str):
            async def run() -> None:
                nonlocal running, overlapping
                running += 1
                if running > 1:
                    overlapping = True
                # 実行中に次が始まらないことを確かめるため、一度手放す。
                await asyncio.sleep(0)
                order.append(name)
                running -= 1

            return run

        for name in ("A", "B", "C"):
            await queue.submit(
                repository,
                JobCreate(kind="generation.asset", payload={"name": name}),
                lambda _job_id, name=name: make_runner(name),
            )
        for _ in range(50):
            if len(order) == 3:
                break
            await asyncio.sleep(0.01)
        await queue.stop()

    asyncio.run(scenario())

    assert order == ["A", "B", "C"]
    assert overlapping is False


def test_submit_does_not_reject_while_busy(tmp_path: Path) -> None:
    """実行中でも断らない。順番待ちとしてジョブが残る。"""
    repository = _repository(tmp_path)
    queue = GpuQueue(_KINDS)

    async def scenario() -> str:
        blocked = asyncio.Event()

        async def first() -> None:
            await blocked.wait()

        await queue.submit(
            repository, JobCreate(kind="composition.scene", payload={}), lambda _id: first
        )
        second = await queue.submit(
            repository,
            JobCreate(kind="generation.asset", payload={}),
            lambda _id: (lambda: asyncio.sleep(0)),
        )
        await asyncio.sleep(0.02)
        state = repository.get(second.id).state.value
        blocked.set()
        await queue.stop()
        return state

    # 先の処理が終わるまで、後の要求は queued のまま待つ。
    assert asyncio.run(scenario()) == "queued"


def test_waiting_ahead_counts_the_running_job(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    queue = GpuQueue(_KINDS)
    first = repository.create(JobCreate(kind="composition.scene", payload={}))
    repository.transition(first.id, JobTransition(state="preparing"))
    second = repository.create(JobCreate(kind="generation.asset", payload={}))

    assert queue.waiting_ahead(repository, first.id) == 0
    assert queue.waiting_ahead(repository, second.id) == 1


def test_leftover_queue_is_cleared_on_restart(tmp_path: Path) -> None:
    """待ち行列はプロセス内にしか無い。再起動時に畳んで表示を残さない。"""
    repository = _repository(tmp_path)
    waiting = repository.create(JobCreate(kind="generation.asset", payload={}))
    running = repository.create(JobCreate(kind="composition.scene", payload={}))
    repository.transition(running.id, JobTransition(state="preparing"))
    repository.transition(running.id, JobTransition(state="running"))

    assert mark_queue_lost(repository, _KINDS) == 1
    assert repository.get(waiting.id).state.value == "cancelled"
    # 実行中のものは別の仕組み（recover_interrupted）が扱う。
    assert repository.get(running.id).state.value == "running"
