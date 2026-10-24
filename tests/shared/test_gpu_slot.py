import asyncio
from pathlib import Path

import pytest

from chotto_sashie_shared.gpu_slot import GpuBusyError, GpuSlot
from chotto_sashie_shared.jobs import JobCreate, JobRepository, JobTransition
from chotto_sashie_api.database import Database


@pytest.fixture
def repository(tmp_path: Path) -> JobRepository:
    database = Database(tmp_path / "gpu-slot.sqlite3")
    database.initialize()
    return JobRepository(database)


def test_second_job_of_a_bound_kind_is_rejected_while_first_is_active(
    repository: JobRepository,
) -> None:
    slot = GpuSlot(["generation.video.shot"], describe=lambda kind: f"動画: {kind}")
    first = asyncio.run(slot.claim(repository, JobCreate(kind="generation.video.shot")))
    repository.transition(first.id, JobTransition(state="preparing"))

    with pytest.raises(GpuBusyError) as raised:
        asyncio.run(slot.claim(repository, JobCreate(kind="generation.video.shot")))
    assert "動画: generation.video.shot" in str(raised.value)


def test_kinds_outside_the_bound_list_do_not_hold_the_slot(
    repository: JobRepository,
) -> None:
    slot = GpuSlot(["generation.video.shot"])
    repository.create(JobCreate(kind="composition.scene"))

    assert asyncio.run(slot.claim(repository, JobCreate(kind="generation.video.shot"))) is not None
