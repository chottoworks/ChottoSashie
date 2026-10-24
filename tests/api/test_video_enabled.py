"""「動画も作るか」の切り替えが、画面の項目・モデル取得・要件確認へ効くかを確かめる。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chotto_sashie_shared.jobs import JobRepository
from chotto_sashie_shared.requirements import check_requirements, load_requirements
from chotto_sashie_api import main
from chotto_sashie_api.database import Database


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    database = Database(tmp_path / "video-api.sqlite3")
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(main, "jobs", JobRepository(database))
    with TestClient(main.app) as test_client:
        yield test_client


def _save_existing_comfyui(client: TestClient) -> None:
    response = client.put(
        "/api/v1/setup/config",
        json={"mode": "existing_comfyui", "base_url": "http://127.0.0.1:8190"},
    )
    assert response.status_code == 200


def test_video_is_enabled_by_default(client: TestClient) -> None:
    assert client.get("/api/v1/setup/config").json()["video_enabled"] is True


def test_toggle_is_kept_before_the_connection_is_saved(client: TestClient) -> None:
    """接続方式より先に選べる。まだ「設定済み」にはしない。"""
    response = client.put("/api/v1/setup/video-enabled", json={"enabled": False})
    assert response.status_code == 200
    assert response.json()["video_enabled"] is False
    assert response.json()["configured"] is False
    assert client.get("/api/v1/setup/config").json()["video_enabled"] is False


def test_toggle_is_kept(client: TestClient) -> None:
    _save_existing_comfyui(client)

    response = client.put("/api/v1/setup/video-enabled", json={"enabled": False})
    assert response.status_code == 200
    assert response.json()["video_enabled"] is False
    assert client.get("/api/v1/setup/config").json()["video_enabled"] is False


def test_saving_the_connection_does_not_reset_the_choice(client: TestClient) -> None:
    """接続方式だけを保存し直しても、「挿絵だけ」の選択は戻らない。"""
    _save_existing_comfyui(client)
    client.put("/api/v1/setup/video-enabled", json={"enabled": False})

    response = client.put(
        "/api/v1/setup/config",
        json={"mode": "existing_comfyui", "base_url": "http://127.0.0.1:8191"},
    )
    assert response.status_code == 200
    assert response.json()["video_enabled"] is False


def test_the_choice_can_be_saved_together_with_the_connection(client: TestClient) -> None:
    response = client.put(
        "/api/v1/setup/config",
        json={
            "mode": "existing_comfyui",
            "base_url": "http://127.0.0.1:8190",
            "video_enabled": False,
        },
    )
    assert response.status_code == 200
    assert response.json()["video_enabled"] is False


def test_model_plan_drops_video_models_when_disabled(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "MANAGED_ROOT", tmp_path / "managed")

    with_video = client.get("/api/v1/setup/models/plan").json()
    video_ids = {"wan22_i2v_high_noise", "wan21_vae", "minimax_h3_fl2va"}
    assert video_ids <= {component["id"] for component in with_video["components"]}

    _save_existing_comfyui(client)
    client.put("/api/v1/setup/video-enabled", json={"enabled": False})

    without_video = client.get("/api/v1/setup/models/plan").json()
    remaining = {component["id"] for component in without_video["components"]}
    assert not (video_ids & remaining)
    # 静止画（合成）と人物モデルに使うものは残る。
    assert "qwen_image_edit_2511_fp8mixed" in remaining
    assert "qwen-3-4b-text-encoder" in remaining
    # 動画用（数十GB）が外れる分、必要容量も減る。
    assert without_video["required_download_bytes"] < with_video["required_download_bytes"]


def test_download_only_covers_the_remaining_models(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """取得ジョブにも動画用モデルを渡さない（計画とちぐはぐにならないこと）。"""
    monkeypatch.setattr(main, "MANAGED_ROOT", tmp_path / "managed")
    _save_existing_comfyui(client)
    client.put("/api/v1/setup/video-enabled", json={"enabled": False})

    captured: list[list] = []
    monkeypatch.setattr(main, "run_model_download", lambda job_id, specs: captured.append(specs))

    response = client.post("/api/v1/setup/models/download", json={"groups": ["minimax_h3"]})
    assert response.status_code == 200
    assert captured, "取得ジョブが組み立てられていない"
    assert all(not spec.video_only for spec in captured[0])


def test_requirements_skip_video_capabilities_when_disabled() -> None:
    manifest = load_requirements(Path("model-manifests/video-requirements.json"))
    # ノードもモデルも何も無いComfyUIとして扱う（不足の並びだけを見る）。
    report = check_requirements({}, manifest, include_video=False)
    ids = {capability.id for capability in report.capabilities}
    assert "i2v" not in ids
    assert "i2v_minimax_h3" not in ids
    assert "composition" in ids
    assert "character_figure" in ids
