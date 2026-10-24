"""初回セットアップの完了フラグと再セットアップの振る舞いを確かめる。"""

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


def test_new_install_is_not_completed(client: TestClient) -> None:
    """初回起動はウィザードを出す状態。"""
    body = client.get("/api/v1/setup/config").json()
    assert body["configured"] is False
    assert body["setup_completed"] is False


def test_complete_requires_saved_config(client: TestClient) -> None:
    assert client.post("/api/v1/setup/complete").status_code == 409


def test_complete_then_reset(client: TestClient) -> None:
    client.put(
        "/api/v1/setup/config",
        json={"mode": "existing_comfyui", "base_url": "http://127.0.0.1:8190"},
    )
    # 保存しただけでは未完了（ウィザードの途中）。
    assert client.get("/api/v1/setup/config").json()["setup_completed"] is False

    completed = client.post("/api/v1/setup/complete")
    assert completed.status_code == 200
    assert completed.json()["setup_completed"] is True
    # 次回起動以降も完了状態が続く。
    assert client.get("/api/v1/setup/config").json()["setup_completed"] is True

    # 再セットアップ：設定値は残したままウィザードへ戻る。
    reset = client.post("/api/v1/setup/reset")
    assert reset.status_code == 200
    body = reset.json()
    assert body["setup_completed"] is False
    assert body["configured"] is True
    assert body["resolved_base_url"] == "http://127.0.0.1:8190"


def test_setup_config_put_passes_preflight_from_desktop_webview(client: TestClient) -> None:
    """本番WebViewの送信元から設定保存(PUT)のプリフライトが通る。"""
    response = client.options(
        "/api/v1/setup/config",
        headers={
            "Origin": "http://tauri.localhost",
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert response.status_code == 200
    assert "PUT" in response.headers["access-control-allow-methods"]


def test_clear_config_returns_to_first_run_state(client: TestClient) -> None:
    """「設定を初期化して最初から」は保存済みレコードを消し、未設定状態へ戻す。"""
    client.put(
        "/api/v1/setup/config",
        json={"mode": "existing_comfyui", "base_url": "http://127.0.0.1:8190"},
    )
    client.post("/api/v1/setup/complete")

    cleared = client.post("/api/v1/setup/config/clear")
    assert cleared.status_code == 200
    body = cleared.json()
    assert body["configured"] is False
    assert body["setup_completed"] is False
    assert body["base_url"] == ""
    assert client.get("/api/v1/setup/config").json()["configured"] is False


def test_saving_config_again_keeps_completion(client: TestClient) -> None:
    """完了後に設定を調整しても、完了状態は維持される。"""
    client.put(
        "/api/v1/setup/config",
        json={"mode": "existing_comfyui", "base_url": "http://127.0.0.1:8190"},
    )
    client.post("/api/v1/setup/complete")
    client.put(
        "/api/v1/setup/config",
        json={"mode": "existing_comfyui", "base_url": "http://127.0.0.1:8191"},
    )
    body = client.get("/api/v1/setup/config").json()
    assert body["setup_completed"] is True
    assert body["resolved_base_url"] == "http://127.0.0.1:8191"
