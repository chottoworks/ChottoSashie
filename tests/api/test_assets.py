import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chotto_sashie_shared.jobs import JobRepository
from chotto_sashie_api import main
from chotto_sashie_api.database import Database

# 1x1の透明PNG。
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    database = Database(tmp_path / "video-api.sqlite3")
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(main, "jobs", JobRepository(database))
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture
def project_id(client: TestClient) -> str:
    created = client.post("/api/v1/projects", json={"name": "資産テスト用"})
    return created.json()["id"]


@pytest.mark.parametrize("path_prefix", ["background-assets", "prop-assets"])
class TestAssetCrud:
    def test_create_list_get_update(self, client: TestClient, project_id: str, path_prefix: str) -> None:
        created = client.post(
            f"/api/v1/projects/{project_id}/{path_prefix}",
            json={"name": "教室", "category": "屋内", "description": "放課後の教室"},
        )
        assert created.status_code == 201
        asset = created.json()
        assert asset["project_id"] == project_id
        assert asset["has_image"] is False

        listed = client.get(f"/api/v1/projects/{project_id}/{path_prefix}")
        assert listed.status_code == 200
        assert len(listed.json()) == 1

        fetched = client.get(f"/api/v1/{path_prefix}/{asset['id']}")
        assert fetched.status_code == 200
        assert fetched.json()["name"] == "教室"

        updated = client.patch(f"/api/v1/{path_prefix}/{asset['id']}", json={"name": "図書室"})
        assert updated.status_code == 200
        assert updated.json()["name"] == "図書室"

    def test_create_under_missing_project_returns_404(self, client: TestClient, path_prefix: str) -> None:
        response = client.post(
            f"/api/v1/projects/does-not-exist/{path_prefix}", json={"name": "教室"}
        )
        assert response.status_code == 404

    def test_get_update_delete_missing_asset_returns_404(
        self, client: TestClient, project_id: str, path_prefix: str
    ) -> None:
        assert client.get(f"/api/v1/{path_prefix}/does-not-exist").status_code == 404
        assert (
            client.patch(f"/api/v1/{path_prefix}/does-not-exist", json={"name": "x"}).status_code
            == 404
        )
        assert client.delete(f"/api/v1/{path_prefix}/does-not-exist").status_code == 404

    def test_delete_removes_asset(self, client: TestClient, project_id: str, path_prefix: str) -> None:
        created = client.post(f"/api/v1/projects/{project_id}/{path_prefix}", json={"name": "倉庫"})
        asset_id = created.json()["id"]

        deleted = client.delete(f"/api/v1/{path_prefix}/{asset_id}")
        assert deleted.status_code == 204
        assert client.get(f"/api/v1/{path_prefix}/{asset_id}").status_code == 404

    def test_image_upload_and_serve_round_trip(
        self, client: TestClient, project_id: str, path_prefix: str
    ) -> None:
        created = client.post(f"/api/v1/projects/{project_id}/{path_prefix}", json={"name": "机"})
        asset_id = created.json()["id"]

        uploaded = client.post(
            f"/api/v1/{path_prefix}/{asset_id}/image",
            files={"file": ("reference.png", _TINY_PNG, "image/png")},
        )
        assert uploaded.status_code == 200
        assert uploaded.json()["has_image"] is True

        served = client.get(f"/api/v1/{path_prefix}/{asset_id}/image")
        assert served.status_code == 200
        assert served.headers["content-type"] == "image/png"
        assert served.content == _TINY_PNG

    def test_image_upload_rejects_unsupported_content_type(
        self, client: TestClient, project_id: str, path_prefix: str
    ) -> None:
        created = client.post(f"/api/v1/projects/{project_id}/{path_prefix}", json={"name": "机"})
        asset_id = created.json()["id"]

        response = client.post(
            f"/api/v1/{path_prefix}/{asset_id}/image",
            files={"file": ("notes.txt", b"hello", "text/plain")},
        )
        assert response.status_code == 422

    def test_image_upload_rejects_oversized_file(
        self, client: TestClient, project_id: str, path_prefix: str
    ) -> None:
        created = client.post(f"/api/v1/projects/{project_id}/{path_prefix}", json={"name": "机"})
        asset_id = created.json()["id"]

        oversized = b"0" * (15 * 1024 * 1024 + 1)
        response = client.post(
            f"/api/v1/{path_prefix}/{asset_id}/image",
            files={"file": ("big.png", oversized, "image/png")},
        )
        assert response.status_code == 422

    def test_get_image_before_upload_returns_404(
        self, client: TestClient, project_id: str, path_prefix: str
    ) -> None:
        created = client.post(f"/api/v1/projects/{project_id}/{path_prefix}", json={"name": "机"})
        asset_id = created.json()["id"]

        response = client.get(f"/api/v1/{path_prefix}/{asset_id}/image")
        assert response.status_code == 404
