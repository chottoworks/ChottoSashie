"""動画ツールでも既存モデルフォルダを走査できることを確かめる（挿絵ツールからの移植）。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chotto_sashie_shared.jobs import JobRepository
from chotto_sashie_api import main
from chotto_sashie_api.database import Database


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    database = Database(tmp_path / "video-api.sqlite3")
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(main, "jobs", JobRepository(database))
    with TestClient(main.app) as test_client:
        yield test_client


def test_scan_reports_models_by_category(client: TestClient, tmp_path: Path) -> None:
    root = tmp_path / "models"
    (root / "diffusion_models").mkdir(parents=True)
    (root / "vae").mkdir(parents=True)
    (root / "diffusion_models" / "wan.safetensors").write_bytes(b"x" * 10)
    (root / "vae" / "wan_vae.safetensors").write_bytes(b"y" * 5)
    (root / "notes.txt").write_text("ignored", encoding="utf-8")

    response = client.post("/api/v1/models/scan", json={"roots": [str(root)]})
    assert response.status_code == 200
    body = response.json()
    names = {item["name"]: item["category"] for item in body["files"]}
    assert names == {"wan.safetensors": "diffusion_models", "wan_vae.safetensors": "vae"}
    assert body["truncated"] is False


def test_scan_reports_inaccessible_root(client: TestClient, tmp_path: Path) -> None:
    response = client.post("/api/v1/models/scan", json={"roots": [str(tmp_path / "missing")]})
    assert response.status_code == 200
    assert response.json()["inaccessible_paths"] != []


def test_scan_detects_duplicates(client: TestClient, tmp_path: Path) -> None:
    first = tmp_path / "a" / "loras"
    second = tmp_path / "b" / "loras"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    (first / "same.safetensors").write_bytes(b"z" * 7)
    (second / "same.safetensors").write_bytes(b"z" * 7)

    response = client.post(
        "/api/v1/models/scan", json={"roots": [str(tmp_path / "a"), str(tmp_path / "b")]}
    )
    assert len(response.json()["duplicate_groups"]) == 1
