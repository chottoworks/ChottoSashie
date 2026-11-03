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


def _component(component_id: str, size: int, **extra: object) -> dict:
    return {
        "id": component_id,
        "display_name_ja": component_id,
        "download_url": f"https://example.invalid/{component_id}.safetensors",
        "destination": f"models/vae/{component_id}.safetensors",
        "size_bytes": size,
        "sha256": "aa" * 32,
        "license": "Apache-2.0",
        "source": "https://example.invalid",
        **extra,
    }


def _manifest_with_choices(tmp_path: Path) -> Path:
    """本体1件と、動画の作り方の選択肢ごとに1件ずつのマニフェスト。取得範囲の切り分けを確かめるため。"""
    manifest = {
        "format_version": 1,
        "groups": [
            {"id": "wan22", "display_name_ja": "Wan", "setting": {"key": "video_engine", "value": "wan22"}},
            {
                "id": "minimax_h3",
                "display_name_ja": "H3",
                "setting": {"key": "video_engine", "value": "minimax_h3"},
            },
        ],
        "components": [
            _component("core-one", 3),
            _component("wan-one", 5, group="wan22"),
            _component("h3-one", 7, group="minimax_h3"),
        ],
    }
    path = tmp_path / "choice-models.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _record_downloads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def fake_download(root, spec, *, relative_path=None, **_kwargs):
        calls.append(spec.id)
        destination = safe_destination(root, relative_path or spec.destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"x" * spec.size_bytes)
        return destination

    monkeypatch.setattr(main, "download_with_resume", fake_download)
    return calls


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
    # Wan/Qwen系10件 + 合成で人物を切り抜く BiRefNet 1件 + Z-Image系6件（背景・小物の
    # Turbo、人物候補のベースとその int8 版、共通のテキストエンコーダーとVAE、立ち絵の
    # 角度LoRA）+ MiniMax H3 5件と、その指示を英語へ直す Gemma 1件。
    assert len(body["components"]) == 23
    assert all(component["state"] == "missing" for component in body["components"])
    assert body["required_download_bytes"] > 0

    # 構成を判定できない端末では設定の既定（Wan 2.2・人物候補は bf16）に従い、
    # ほかの選択肢のモデルは任意の印を付けて「必要な容量」に数えない。
    optional = [c for c in body["components"] if c["optional"]]
    assert {c["group"] for c in optional} == {"minimax_h3", "candidate_int8"}
    assert len(optional) == 7
    assert body["required_download_bytes"] == sum(
        c["download_bytes"] for c in body["components"] if not c["optional"]
    )
    assert body["all_download_bytes"] == sum(c["download_bytes"] for c in body["components"])
    defaults = {group["id"]: group["default"] for group in body["groups"]}
    assert defaults == {
        "wan22": True,
        "minimax_h3": False,
        "candidate_bf16": True,
        "candidate_int8": False,
    }


def test_real_manifest_plan_follows_recommendation(client: TestClient, set_hardware) -> None:
    """VRAMの多い端末では H3 と bf16、少ない端末では Wan 2.2 と int8 だけを既定で取得する。"""
    set_hardware(vram_gb=32)
    body = client.get("/api/v1/setup/models/plan").json()
    defaults = {group["id"] for group in body["groups"] if group["default"]}
    assert defaults == {"minimax_h3", "candidate_bf16"}
    optional = {c["group"] for c in body["components"] if c["optional"]}
    assert optional == {"wan22", "candidate_int8"}

    set_hardware(vram_gb=12)
    body = client.get("/api/v1/setup/models/plan").json()
    defaults = {group["id"] for group in body["groups"] if group["default"]}
    assert defaults == {"wan22", "candidate_int8"}


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


def test_default_download_follows_recommendation(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, set_hardware
) -> None:
    monkeypatch.setattr(main, "MODELS_MANIFEST_PATH", _manifest_with_choices(tmp_path))
    calls = _record_downloads(monkeypatch)
    set_hardware(vram_gb=32)

    # 指定しなければ、本体と、おすすめ（H3）の選択肢だけ。
    assert client.post("/api/v1/setup/models/download").status_code == 200
    assert calls == ["core-one", "h3-one"]

    # ほかの選択肢は、まとまりを指定したときだけ取りに行く。
    calls.clear()
    assert client.post("/api/v1/setup/models/download", json={"groups": ["wan22"]}).status_code == 200
    assert calls == ["wan-one"]


def test_undetected_hardware_downloads_setting_defaults(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """構成を判定できなければ、設定の既定（Wan 2.2）の選択肢を取得する。"""
    monkeypatch.setattr(main, "MODELS_MANIFEST_PATH", _manifest_with_choices(tmp_path))
    calls = _record_downloads(monkeypatch)

    assert client.post("/api/v1/setup/models/download").status_code == 200
    assert calls == ["core-one", "wan-one"]


def test_download_all_ignores_recommendation(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, set_hardware
) -> None:
    monkeypatch.setattr(main, "MODELS_MANIFEST_PATH", _manifest_with_choices(tmp_path))
    calls = _record_downloads(monkeypatch)
    set_hardware(vram_gb=32)

    assert client.post("/api/v1/setup/models/download", json={"all": True}).status_code == 200
    assert calls == ["core-one", "wan-one", "h3-one"]


def test_chosen_setting_overrides_recommendation(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, set_hardware
) -> None:
    """利用者が選んだ設定は、おすすめより優先して取得と確認の対象を決める。"""
    monkeypatch.setattr(main, "MODELS_MANIFEST_PATH", _manifest_with_choices(tmp_path))
    set_hardware(vram_gb=32)
    assert client.put("/api/v1/preferences", json={"video_engine": "wan22"}).status_code == 200

    body = client.get("/api/v1/setup/models/plan").json()
    optional = {c["id"] for c in body["components"] if c["optional"]}
    assert optional == {"h3-one"}
    # 任意のぶんは「必要なダウンロード」に含めない。
    assert body["required_download_bytes"] == 3 + 5
    assert body["all_download_bytes"] == 3 + 5 + 7
