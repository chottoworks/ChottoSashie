"""不足モデルの取得先（共有フォルダ／既存モデルフォルダ）の選択を確かめる。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chotto_sashie_shared.download import safe_destination
from chotto_sashie_shared.jobs import JobRepository
from chotto_sashie_shared.shared_env import shared_models_root
from chotto_sashie_api import main
from chotto_sashie_api.database import Database


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    database = Database(tmp_path / "video-api.sqlite3")
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(main, "jobs", JobRepository(database))
    monkeypatch.setattr(main, "MANAGED_ROOT", tmp_path / "managed")
    with TestClient(main.app) as test_client:
        yield test_client


def _manifest(tmp_path: Path) -> Path:
    """種別フォルダの別名を確かめたいので、拡散モデル1件だけのマニフェストにする。"""
    manifest = {
        "format_version": 1,
        "components": [
            {
                "id": "tiny",
                "display_name_ja": "テスト用モデル",
                "download_url": "https://example.invalid/tiny.safetensors",
                "destination": "models/diffusion_models/tiny.safetensors",
                "size_bytes": 4,
                "sha256": "cc" * 32,
                "license": "Apache-2.0",
                "source": "https://example.invalid",
            }
        ],
    }
    path = tmp_path / "tiny-models.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _fake_download(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    written: list[Path] = []

    def fake_download(root, spec, *, relative_path=None, **_kwargs):
        destination = safe_destination(root, relative_path or spec.destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"x" * spec.size_bytes)
        written.append(destination)
        return destination

    monkeypatch.setattr(main, "download_with_resume", fake_download)
    return written


def _external_root(tmp_path: Path, *, folder: str = "unet") -> Path:
    root = tmp_path / "AI" / "Models"
    (root / folder).mkdir(parents=True)
    return root


def _configure(client: TestClient, root: Path, *, download_root: str | None = None) -> dict:
    payload: dict = {
        "mode": "managed_external_models",
        "external_model_roots": [str(root)],
    }
    if download_root is not None:
        payload["model_download_root"] = download_root
    return client.put("/api/v1/setup/config", json=payload)


def test_default_destination_is_the_shared_folder(client: TestClient) -> None:
    plan = client.get("/api/v1/setup/models/plan").json()
    assert plan["download_target"] == "shared"
    assert plan["download_root"] == str(shared_models_root())


def test_existing_folder_can_be_chosen_as_the_destination(
    client: TestClient, tmp_path: Path
) -> None:
    root = _external_root(tmp_path)
    saved = _configure(client, root, download_root=str(root))
    assert saved.status_code == 200
    assert saved.json()["model_download_root"] == str(root)

    plan = client.get("/api/v1/setup/models/plan").json()
    assert plan["download_target"] == "external"
    assert plan["download_root"] == str(root)


def test_download_writes_into_the_existing_folder(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """既存フォルダを選んだら、そのフォルダの並びに合わせて書き足す。"""
    monkeypatch.setattr(main, "MODELS_MANIFEST_PATH", _manifest(tmp_path))
    root = _external_root(tmp_path)
    assert _configure(client, root, download_root=str(root)).status_code == 200
    written = _fake_download(monkeypatch)

    started = client.post("/api/v1/setup/models/download")
    assert started.status_code == 200
    job = client.get(f"/api/v1/jobs/{started.json()['id']}").json()
    assert job["state"] == "completed"

    # 既存フォルダが unet を使っているので、diffusion_models を新設せず合わせる。
    assert written == [root / "unet" / "tiny.safetensors"]


def test_download_defaults_to_the_shared_folder(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "MODELS_MANIFEST_PATH", _manifest(tmp_path))
    root = _external_root(tmp_path)
    # 取得先を選ばなければ、既存フォルダは読み取りのままで書き込まない。
    assert _configure(client, root).status_code == 200
    written = _fake_download(monkeypatch)

    started = client.post("/api/v1/setup/models/download")
    job = client.get(f"/api/v1/jobs/{started.json()['id']}").json()
    assert job["state"] == "completed"

    assert written == [shared_models_root() / "diffusion_models" / "tiny.safetensors"]
    assert list((root / "unet").iterdir()) == []


def test_destination_must_be_one_of_the_referenced_folders(
    client: TestClient, tmp_path: Path
) -> None:
    """参照フォルダとして挙げていない場所へは書き込ませない。"""
    root = _external_root(tmp_path)
    other = tmp_path / "somewhere-else"
    other.mkdir()

    response = _configure(client, root, download_root=str(other))
    assert response.status_code == 422
    assert "参照する既存モデルフォルダ" in response.json()["detail"]


def test_destination_requires_the_external_models_mode(client: TestClient, tmp_path: Path) -> None:
    root = _external_root(tmp_path)
    response = client.put(
        "/api/v1/setup/config",
        json={"mode": "managed_dedicated", "model_download_root": str(root)},
    )
    assert response.status_code == 422


def test_missing_destination_folder_is_rejected(client: TestClient, tmp_path: Path) -> None:
    root = _external_root(tmp_path)
    response = _configure(client, root, download_root=str(tmp_path / "missing"))
    assert response.status_code == 422


def test_models_already_in_the_shared_folder_are_not_downloaded_again(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """共有フォルダにあるものは、既存フォルダへ取得先を変えても取り直さない。"""
    monkeypatch.setattr(main, "MODELS_MANIFEST_PATH", _manifest(tmp_path))
    shared_copy = shared_models_root() / "diffusion_models" / "tiny.safetensors"
    shared_copy.parent.mkdir(parents=True, exist_ok=True)
    shared_copy.write_bytes(b"x" * 4)
    root = _external_root(tmp_path)
    assert _configure(client, root, download_root=str(root)).status_code == 200
    written = _fake_download(monkeypatch)

    started = client.post("/api/v1/setup/models/download")
    job = client.get(f"/api/v1/jobs/{started.json()['id']}").json()
    assert job["state"] == "completed"
    assert written == []
