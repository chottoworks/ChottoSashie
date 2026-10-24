from pathlib import Path

import pytest

from chotto_sashie_api.database import Database
from chotto_sashie_shared.jobs import InvalidTransitionError, JobRepository
from chotto_sashie_shared.jobs import JobCreate, JobState, JobTransition


@pytest.fixture
def repository(tmp_path: Path) -> JobRepository:
    database = Database(tmp_path / "test.sqlite3")
    database.initialize()
    return JobRepository(database)


def test_job_follows_normal_lifecycle(repository: JobRepository) -> None:
    job = repository.create(JobCreate(kind="generation.turbo", payload={"seed": 42}))
    assert job.state is JobState.QUEUED

    job = repository.transition(job.id, JobTransition(state="preparing", progress=0.1))
    job = repository.transition(job.id, JobTransition(state="running", progress=0.2))
    job = repository.transition(
        job.id,
        JobTransition(
            state="validating",
            progress=0.9,
            result={"images": [{"filename": "test.png"}]},
        ),
    )
    job = repository.transition(job.id, JobTransition(state="completed"))
    assert job.state is JobState.COMPLETED
    assert job.progress == 1
    assert job.result == {"images": [{"filename": "test.png"}]}


def test_invalid_transition_is_rejected(repository: JobRepository) -> None:
    job = repository.create(JobCreate(kind="training.character"))
    with pytest.raises(InvalidTransitionError):
        repository.transition(job.id, JobTransition(state="completed"))


def test_recover_marks_active_jobs_interrupted(repository: JobRepository) -> None:
    job = repository.create(JobCreate(kind="generation.turbo"))
    repository.transition(job.id, JobTransition(state="preparing"))
    repository.transition(job.id, JobTransition(state="running"))

    assert repository.recover_interrupted() == 1
    assert repository.get(job.id).state is JobState.INTERRUPTED


def test_claim_next_is_ordered(repository: JobRepository) -> None:
    first = repository.create(JobCreate(kind="first"))
    repository.create(JobCreate(kind="second"))
    claimed = repository.claim_next()
    assert claimed is not None
    assert claimed.id == first.id
    assert claimed.state is JobState.PREPARING
