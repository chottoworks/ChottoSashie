import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chotto_sashie_shared.download import safe_destination
from chotto_sashie_shared.jobs import JobCreate, JobRepository
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


def _manifest_with_optional(tmp_path: Path) -> Path:
    """必須1件＋任意1件のマニフェスト。取得範囲の切り分けを確かめるため。"""
    manifest = {
        "format_version": 1,
        "components": [
            {
                "id": "required-one",
                "display_name_ja": "必須モデル",
                "download_url": "https://example.invalid/required.safetensors",
                "destination": "models/vae/required.safetensors",
                "size_bytes": 3,
                "sha256": "aa" * 32,
                "license": "Apache-2.0",
                "source": "https://example.invalid",
            },
            {
                "id": "extra-one",
                "display_name_ja": "任意モデル",
                "download_url": "https://example.invalid/extra.safetensors",
                "destination": "models/vae/extra.safetensors",
                "size_bytes": 5,
                "sha256": "bb" * 32,
                "license": "Apache-2.0",
                "source": "https://example.invalid",
                "optional": True,
                "group": "extras",
            },
        ],
    }
    path = tmp_path / "optional-models.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _tiny_manifest(tmp_path: Path, *, size: int = 3) -> Path:
    manifest = {
        "format_version": 1,
        "components": [
            {
                "id": "tiny",
                "display_name_ja": "テスト用モデル",
                "download_url": "https://example.invalid/tiny.safetensors",
                "destination": "models/vae/tiny.safetensors",
                "size_bytes": size,
                "sha256": "aa" * 32,
                "license": "Apache-2.0",
                "source": "https://example.invalid",
            }
        ],
    }
    path = tmp_path / "tiny-models.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_real_manifest_plan_reports_all_missing(client: TestClient) -> None:
    body = client.get("/api/v1/setup/models/plan").json()
    # Wan/Qwen系10件 + 合成で人物を切り抜く BiRefNet 1件 + Z-Image系4件（背景・小物と
    # 人物候補・立ち絵）+ 任意のMiniMax H3 5件と、その指示を英語へ直す Gemma 1件。
    assert len(body["components"]) == 21
    assert all(component["state"] == "missing" for component in body["components"])
    assert body["required_download_bytes"] > 0

    # 任意のモデルは印が付き、既定の「必要な容量」には数えない。
    optional = [c for c in body["components"] if c["optional"]]
    assert {c["group"] for c in optional} == {"minimax_h3"}
    assert len(optional) == 6
    assert body["required_download_bytes"] == sum(
        c["download_bytes"] for c in body["components"] if not c["optional"]
    )


def test_download_fetches_missing(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _tiny_manifest(tmp_path)
    monkeypatch.setattr(main, "MODELS_MANIFEST_PATH", manifest)

    calls: list = []

    def fake_download(root, spec, *, relative_path=None, **_kwargs):
        calls.append(spec.id)
        destination = safe_destination(root, relative_path or spec.destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"x" * spec.size_bytes)
        return destination

    monkeypatch.setattr(main, "download_with_resume", fake_download)

    started = client.post("/api/v1/setup/models/download")
    assert started.status_code == 200
    job_id = started.json()["id"]

    job = client.get(f"/api/v1/jobs/{job_id}").json()
    assert job["state"] == "completed"
    assert calls == ["tiny"]
    # 取得先は共有フォルダ。データ領域とは分け、保存先の移動に巻き込まない。
    assert (tmp_path / "shared-env" / "models" / "vae" / "tiny.safetensors").is_file()


def test_download_skips_already_present(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _tiny_manifest(tmp_path, size=5)
    monkeypatch.setattr(main, "MODELS_MANIFEST_PATH", manifest)

    # 共有フォルダに正しいサイズのファイルがあれば、取得はスキップされる。
    existing = shared_models_root() / "vae" / "tiny.safetensors"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_bytes(b"x" * 5)

    calls: list = []

    def fake_download(root, spec, **_kwargs):
        calls.append(spec.id)
        return existing

    monkeypatch.setattr(main, "download_with_resume", fake_download)

    started = client.post("/api/v1/setup/models/download")
    job = client.get(f"/api/v1/jobs/{started.json()['id']}").json()
    assert job["state"] == "completed"
    assert calls == []


def test_duplicate_download_is_rejected(client: TestClient) -> None:
    main.jobs.create(JobCreate(kind="setup.models.download", payload={}))
    response = client.post("/api/v1/setup/models/download")
    assert response.status_code == 409


def test_optional_models_are_only_fetched_when_asked(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "MODELS_MANIFEST_PATH", _manifest_with_optional(tmp_path))

    calls: list = []

    def fake_download(root, spec, *, relative_path=None, **_kwargs):
        calls.append(spec.id)
        destination = safe_destination(root, relative_path or spec.destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"x" * spec.size_bytes)
        return destination

    monkeypatch.setattr(main, "download_with_resume", fake_download)

    # 指定しなければ必須だけ。
    assert client.post("/api/v1/setup/models/download").status_code == 200
    assert calls == ["required-one"]

    # まとまりを指定したときだけ、任意のモデルも取りに行く。
    calls.clear()
    assert client.post("/api/v1/setup/models/download", json={"groups": ["extras"]}).status_code == 200
    assert calls == ["extra-one"]


def test_plan_marks_optional_models(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "MODELS_MANIFEST_PATH", _manifest_with_optional(tmp_path))

    body = client.get("/api/v1/setup/models/plan").json()
    optional = {c["id"]: c for c in body["components"] if c["optional"]}
    assert set(optional) == {"extra-one"}
    assert optional["extra-one"]["group"] == "extras"
    # 任意のぶんは「必要なダウンロード」に含めない。
    assert body["required_download_bytes"] == 3
