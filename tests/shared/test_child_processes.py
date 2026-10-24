import asyncio
import sys
from pathlib import Path

from chotto_sashie_shared.child_processes import OwnedProcess, OwnedProcessRegistry
import pytest

pytestmark = pytest.mark.slow


def _sleep_command(seconds: float) -> list[str]:
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def test_finished_process_is_removed_from_the_registry(tmp_path: Path) -> None:
    registry = OwnedProcessRegistry()

    async def scenario() -> int:
        with (tmp_path / "out.log").open("wb") as stream:
            return await registry.run(
                [sys.executable, "-c", "print('done')"],
                cwd=tmp_path,
                environment={},
                stdout=stream,
                stderr=stream,
                label="試験用",
                job_id="job-1",
            )

    assert asyncio.run(scenario()) == 0
    assert registry.running() == []


def test_running_process_is_reported_then_terminated(tmp_path: Path) -> None:
    """終了処理が、所有する実行中の子プロセスを実際に停止することを確かめる。"""
    registry = OwnedProcessRegistry()
    observed: list[OwnedProcess] = []

    async def scenario() -> int:
        with (tmp_path / "out.log").open("wb") as stream:
            child = asyncio.create_task(
                registry.run(
                    _sleep_command(60),
                    cwd=tmp_path,
                    environment={},
                    stdout=stream,
                    stderr=stream,
                    label="長時間の試験用処理",
                    job_id="job-2",
                    on_started=observed.append,
                )
            )
            while not registry.running():
                await asyncio.sleep(0.01)
            stopped = await registry.terminate_all(timeout_seconds=10)
            assert [record.label for record in stopped] == ["長時間の試験用処理"]
            return await child

    return_code = asyncio.run(scenario())
    assert return_code != 0
    assert registry.running() == []
    assert [record.job_id for record in observed] == ["job-2"]


def test_registry_survives_a_failing_start_callback(tmp_path: Path) -> None:
    """記録側の失敗が、起動済みの処理そのものを止めないことを確かめる。"""
    registry = OwnedProcessRegistry()

    def explode(_record: OwnedProcess) -> None:
        raise RuntimeError("記録に失敗しました")

    async def scenario() -> int:
        with (tmp_path / "out.log").open("wb") as stream:
            return await registry.run(
                [sys.executable, "-c", "print('ok')"],
                cwd=tmp_path,
                environment={},
                stdout=stream,
                stderr=stream,
                label="試験用",
                on_started=explode,
            )

    assert asyncio.run(scenario()) == 0
    assert registry.running() == []
