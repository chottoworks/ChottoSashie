"""共有フォルダのComfyUI（準備・更新）の振る舞いを確かめる。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chotto_sashie_shared.comfyui_provision import managed_comfyui_paths
from chotto_sashie_shared.jobs import JobRepository
from chotto_sashie_shared.shared_env import shared_comfyui_root
from chotto_sashie_api import main
from chotto_sashie_api.database import Database


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    database = Database(tmp_path / "video-api.sqlite3")
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(main, "jobs", JobRepository(database))
    monkeypatch.setenv("CHOTTO_SASHIE_DATA_DIR", str(tmp_path / "data"))
    with TestClient(main.app) as test_client:
        yield test_client


def _fake_install(comfyui_root: Path) -> None:
    comfy_dir, python = managed_comfyui_paths(comfyui_root)
    comfy_dir.mkdir(parents=True, exist_ok=True)
    (comfy_dir / "main.py").write_text("", encoding="utf-8")
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("", encoding="utf-8")


def _fake_provision(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """取得と展開を偽装し、どこへ展開したかを記録する。"""
    extracted: list[Path] = []

    def fake_download(root, spec, **_kwargs):
        archive = root / spec.destination
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(b"7z")
        return archive

    def fake_extract(_archive, destination):
        extracted.append(Path(destination))
        _fake_install(Path(destination))

    monkeypatch.setattr(main, "download_with_resume", fake_download)
    monkeypatch.setattr(main, "extract_archive", fake_extract)
    return extracted


def test_provision_installs_into_the_shared_folder(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    extracted = _fake_provision(monkeypatch)

    started = client.post("/api/v1/setup/comfyui/provision")
    assert started.status_code == 200
    job = client.get(f"/api/v1/jobs/{started.json()['id']}").json()
    assert job["state"] == "completed"

    assert extracted == [shared_comfyui_root()]
    status = client.get("/api/v1/setup/comfyui/status").json()
    assert status["provisioned"] is True
    assert status["source"] == "shared"
    assert status["install_root"] == str(shared_comfyui_root())
