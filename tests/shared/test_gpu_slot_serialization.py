import asyncio
from pathlib import Path

import pytest

from chotto_sashie_shared.gpu_slot import GpuBusyError, GpuSlot
from chotto_sashie_shared.jobs import JobCreate, JobRepository, JobTransition
from chotto_sashie_api.database import Database

# GPUを占有する種別と、その表示名。ここで確かめたいのは仕組みそのものなので、
# アプリの一覧に依存させず試験用の並びを渡す。
GPU_JOB_KINDS: tuple[str, ...] = (
    "training.character",
    "generation.character",
    "generation.turbo",
    "character.candidates",
)
_LABELS = {
    "training.character": "人物モデルの作成",
    "generation.character": "高品質生成",
    "generation.turbo": "下書き生成",
    "character.candidates": "人物候補の生成",
}


def GpuSlotForTest() -> GpuSlot:
    return GpuSlot(GPU_JOB_KINDS, lambda kind: _LABELS.get(kind, kind))


@pytest.fixture
def repository(tmp_path: Path) -> JobRepository:
    database = Database(tmp_path / "gpu-slots.sqlite3")
    database.initialize()
    return JobRepository(database)


def test_second_gpu_job_is_rejected_while_first_is_active(repository: JobRepository) -> None:
    slot = GpuSlotForTest()
    first = asyncio.run(slot.claim(repository, JobCreate(kind="training.character")))
    repository.transition(first.id, JobTransition(state="preparing", progress=0.03))

    with pytest.raises(GpuBusyError) as raised:
        asyncio.run(slot.claim(repository, JobCreate(kind="generation.character")))
    assert raised.value.active.id == first.id
    assert "人物モデルの作成" in str(raised.value)


def test_gpu_slot_is_released_once_the_previous_job_finishes(repository: JobRepository) -> None:
    slot = GpuSlotForTest()
    first = asyncio.run(slot.claim(repository, JobCreate(kind="generation.turbo")))
    for state in ("preparing", "running", "validating", "completed"):
        repository.transition(first.id, JobTransition(state=state))

    second = asyncio.run(slot.claim(repository, JobCreate(kind="character.candidates")))
    assert second.id != first.id


def test_interrupted_job_does_not_hold_the_gpu(repository: JobRepository) -> None:
    """再起動復旧で中断へ移したジョブが、以後の生成を塞がないことを確かめる。"""
    slot = GpuSlotForTest()
    first = asyncio.run(slot.claim(repository, JobCreate(kind="training.character")))
    repository.transition(first.id, JobTransition(state="preparing"))
    repository.recover_interrupted()

    assert asyncio.run(slot.claim(repository, JobCreate(kind="generation.character"))) is not None


def test_non_gpu_job_does_not_hold_the_gpu(repository: JobRepository) -> None:
    """学習データの前処理はGPUを占有しないため、生成を妨げない。"""
    slot = GpuSlotForTest()
    preparing = repository.create(JobCreate(kind="training.character.prepare"))
    repository.transition(preparing.id, JobTransition(state="preparing"))
    repository.transition(preparing.id, JobTransition(state="running", progress=0.5))

    assert asyncio.run(slot.claim(repository, JobCreate(kind="training.character"))) is not None


def test_concurrent_claims_yield_exactly_one_winner(repository: JobRepository) -> None:
    """同時に届いた要求が二重にGPUを掴まないことを確かめる。"""
    slot = GpuSlotForTest()

    async def claim_all() -> list[object]:
        return await asyncio.gather(
            *(slot.claim(repository, JobCreate(kind=kind)) for kind in GPU_JOB_KINDS),
            return_exceptions=True,
        )

    results = asyncio.run(claim_all())
    accepted = [result for result in results if not isinstance(result, Exception)]
    rejected = [result for result in results if isinstance(result, GpuBusyError)]
    assert len(accepted) == 1
    assert len(rejected) == len(GPU_JOB_KINDS) - 1
