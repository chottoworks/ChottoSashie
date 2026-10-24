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


def test_create_list_get_project(client: TestClient) -> None:
    created = client.post("/api/v1/projects", json={"name": "第一部", "description": "序章"})
    assert created.status_code == 201
    project = created.json()
    assert project["name"] == "第一部"

    listed = client.get("/api/v1/projects")
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    fetched = client.get(f"/api/v1/projects/{project['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == project["id"]


def test_get_missing_project_returns_404(client: TestClient) -> None:
    response = client.get("/api/v1/projects/does-not-exist")
    assert response.status_code == 404


def test_project_style_can_be_saved_and_read_back(client: TestClient) -> None:
    """作品全体の画風は設定でき、候補・参照画像の生成へ渡る値として保存される。"""
    project = client.post("/api/v1/projects", json={"name": "画風テスト"}).json()
    assert project["illustration_style"] == ""

    updated = client.patch(
        f"/api/v1/projects/{project['id']}",
        json={"illustration_style": "繊細な線、淡い水彩"},
    )
    assert updated.status_code == 200
    assert updated.json()["illustration_style"] == "繊細な線、淡い水彩"
    assert client.get(f"/api/v1/projects/{project['id']}").json()["illustration_style"] == "繊細な線、淡い水彩"
    assert client.patch("/api/v1/projects/missing", json={"name": "x"}).status_code == 404
