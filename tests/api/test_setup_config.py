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


def _external_dir(tmp_path: Path) -> str:
    models = tmp_path / "models"
    models.mkdir()
    return str(models)


def test_default_config_is_unconfigured(client: TestClient) -> None:
    body = client.get("/api/v1/setup/config").json()
    assert body["configured"] is False
    assert body["mode"] == "existing_comfyui"


def test_set_existing_comfyui(client: TestClient) -> None:
    response = client.put(
        "/api/v1/setup/config",
        json={"mode": "existing_comfyui", "base_url": "http://127.0.0.1:8190"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is True
    assert body["resolved_base_url"] == "http://127.0.0.1:8190"

    # 再取得しても保持されている。
    assert client.get("/api/v1/setup/config").json()["resolved_base_url"] == "http://127.0.0.1:8190"


def test_existing_mode_requires_base_url(client: TestClient) -> None:
    response = client.put("/api/v1/setup/config", json={"mode": "existing_comfyui"})
    assert response.status_code == 422


def test_set_managed_external_models(client: TestClient, tmp_path: Path) -> None:
    external = _external_dir(tmp_path)
    response = client.put(
        "/api/v1/setup/config",
        json={
            "mode": "managed_external_models",
            "port": 8199,
            "external_model_roots": [external],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["resolved_base_url"] == "http://127.0.0.1:8199"
    assert body["external_model_roots"] == [external]


def test_set_managed_dedicated(client: TestClient) -> None:
    # 専用モードはComfyUIをアプリが用意するため、パス入力は不要。
    response = client.put("/api/v1/setup/config", json={"mode": "managed_dedicated", "port": 8200})
    assert response.status_code == 200
    assert response.json()["resolved_base_url"] == "http://127.0.0.1:8200"


def test_managed_external_requires_roots(client: TestClient) -> None:
    response = client.put(
        "/api/v1/setup/config",
        json={"mode": "managed_external_models", "external_model_roots": []},
    )
    assert response.status_code == 422


def test_status_probes_existing_comfyui(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    client.put(
        "/api/v1/setup/config",
        json={"mode": "existing_comfyui", "base_url": "http://127.0.0.1:8190"},
    )

    async def object_info(_self):
        return {}

    monkeypatch.setattr(main.ComfyClient, "object_info", object_info)

    body = client.get("/api/v1/setup/status").json()
    assert body["configured"] is True
    assert body["reachable"] is True


def test_requirements_uses_configured_base_url(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    client.put(
        "/api/v1/setup/config",
        json={"mode": "existing_comfyui", "base_url": "http://127.0.0.1:8190"},
    )

    async def object_info(_self):
        return {"UNETLoader": {"input": {"required": {"unet_name": [[], {}]}}}}

    monkeypatch.setattr(main.ComfyClient, "object_info", object_info)

    # base_url を渡さなくても設定済みの接続先が使われる。
    response = client.get("/api/v1/setup/requirements")
    assert response.status_code == 200


def test_generation_without_base_url_and_no_config_is_409(client: TestClient) -> None:
    project_id = client.post("/api/v1/projects", json={"name": "未設定"}).json()["id"]
    asset_id = client.post(
        f"/api/v1/projects/{project_id}/background-assets", json={"name": "空"}
    ).json()["id"]

    response = client.post(
        f"/api/v1/background-assets/{asset_id}/generate", json={"prompt": "sky"}
    )
    assert response.status_code == 409


def test_generation_uses_configured_base_url(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    client.put(
        "/api/v1/setup/config",
        json={"mode": "existing_comfyui", "base_url": "http://127.0.0.1:8190"},
    )

    async def submit(_self, workflow, _client_id):
        return "gen-1"

    async def history(_self, prompt_id):
        return {
            "status": {"completed": True, "status_str": "success"},
            "outputs": {"9": {"images": [{"filename": "a.png", "subfolder": "", "type": "output"}]}},
        }

    async def view_image(_self, image):
        return b"IMG", "image/png"

    monkeypatch.setattr(main.ComfyClient, "submit", submit)
    monkeypatch.setattr(main.ComfyClient, "history", history)
    monkeypatch.setattr(main.ComfyClient, "view_image", view_image)

    project_id = client.post("/api/v1/projects", json={"name": "設定済み"}).json()["id"]
    asset_id = client.post(
        f"/api/v1/projects/{project_id}/background-assets", json={"name": "空"}
    ).json()["id"]

    # base_url を渡さずに生成できる。
    generated = client.post(
        f"/api/v1/background-assets/{asset_id}/generate", json={"prompt": "sky"}
    )
    assert generated.status_code == 200
    job = client.get(f"/api/v1/jobs/{generated.json()['job_id']}").json()
    assert job["state"] == "completed"
