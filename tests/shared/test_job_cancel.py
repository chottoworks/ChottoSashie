"""実行中ジョブの中止（記録の更新と、監視ループが静かに降りること）。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from chotto_sashie_shared.comfy_monitor import (
    MonitorMessages,
    MonitorSettings,
    monitor_comfy_prompt,
)
from chotto_sashie_shared.jobs import (
    InvalidTransitionError,
    JobCreate,
    JobRepository,
    JobTransition,
)
from chotto_sashie_shared.job_control import cancel_running_job

from chotto_sashie_api.database import Database

_MESSAGES = MonitorMessages(
    running="生成しています",
    saving="保存しています",
    completed="完了しました",
    failed="失敗しました",
    failed_code="FAILED",
    timeout="時間切れです",
    timeout_code="TIMEOUT",
    monitor_failed_code="MONITOR_FAILED",
)


class _NeverFinishingClient:
    """完了しないComfyUI。中止しない限り監視ループは回り続ける。"""

    def __init__(self) -> None:
        self.interrupted = False

    async def history(self, prompt_id: str) -> None:  # noqa: ARG002
        return None

    def prompt_progress(self, prompt_id: str) -> None:  # noqa: ARG002
        return None

    async def interrupt(self) -> bool:
        self.interrupted = True
        return True


def _repository(tmp_path: Path) -> JobRepository:
    database = Database(tmp_path / "jobs.sqlite3")
    database.initialize()
    return JobRepository(database)


def test_request_cancel_marks_the_job_cancelled(tmp_path: Path) -> None:
    jobs = _repository(tmp_path)
    job = jobs.create(JobCreate(kind="generation.turbo"))
    jobs.transition(job.id, JobTransition(state="preparing"))
    jobs.transition(job.id, JobTransition(state="running"))

    cancelled = jobs.request_cancel(job.id)

    assert cancelled.state.value == "cancelled"
    assert cancelled.message == "利用者が中止しました"
    assert jobs.is_cancelled(job.id) is True


def test_finished_jobs_cannot_be_cancelled(tmp_path: Path) -> None:
    jobs = _repository(tmp_path)
    job = jobs.create(JobCreate(kind="generation.turbo"))
    jobs.transition(job.id, JobTransition(state="preparing"))
    jobs.transition(job.id, JobTransition(state="running"))
    jobs.transition(job.id, JobTransition(state="completed"))

    with pytest.raises(InvalidTransitionError):
        jobs.request_cancel(job.id)


def test_missing_job_counts_as_cancelled_for_monitors(tmp_path: Path) -> None:
    jobs = _repository(tmp_path)

    assert jobs.is_cancelled("missing") is True


def test_monitor_stops_quietly_after_cancel(tmp_path: Path) -> None:
    jobs = _repository(tmp_path)
    job = jobs.create(JobCreate(kind="generation.turbo"))
    jobs.transition(job.id, JobTransition(state="preparing"))
    client = _NeverFinishingClient()

    async def scenario() -> None:
        monitor = asyncio.create_task(
            monitor_comfy_prompt(
                client=client,  # type: ignore[arg-type]
                jobs=jobs,
                job_id=job.id,
                prompt_id="prompt-1",
                on_success=lambda files: asyncio.sleep(0, result={}),  # noqa: ARG005
                messages=_MESSAGES,
                settings=MonitorSettings(poll_seconds=0.01, max_polls=500),
            )
        )
        await asyncio.sleep(0.05)
        await cancel_running_job(jobs, job.id, comfy_base_url=None)
        await asyncio.wait_for(monitor, timeout=2)

    asyncio.run(scenario())

    finished = jobs.get(job.id)
    assert finished.state.value == "cancelled"
    assert finished.message == "利用者が中止しました"


def test_cancel_asks_comfyui_to_interrupt(tmp_path: Path, monkeypatch) -> None:
    jobs = _repository(tmp_path)
    job = jobs.create(JobCreate(kind="generation.turbo"))
    jobs.transition(job.id, JobTransition(state="preparing"))
    client = _NeverFinishingClient()
    monkeypatch.setattr(
        "chotto_sashie_shared.job_control.ComfyClient", lambda base_url: client  # noqa: ARG005
    )

    asyncio.run(cancel_running_job(jobs, job.id, comfy_base_url="http://127.0.0.1:8190"))

    assert client.interrupted is True
    assert jobs.get(job.id).state.value == "cancelled"
