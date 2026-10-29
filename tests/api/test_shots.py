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


@pytest.fixture
def still_image_id(client: TestClient) -> str:
    project_id = client.post("/api/v1/projects", json={"name": "カットテスト"}).json()["id"]
    return client.post(f"/api/v1/projects/{project_id}/still-images", json={"name": "登校"}).json()["id"]


def test_shot_crud(client: TestClient, still_image_id: str) -> None:
    first = client.post(
        f"/api/v1/still-images/{still_image_id}/shots",
        json={"motion_prompt": "ゆっくり瞬きする", "duration_seconds": 4},
    )
    assert first.status_code == 201
    shot = first.json()
    assert shot["ordinal"] == 1
    assert shot["has_video"] is False
    assert shot["has_start_frame"] is False

    second = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={"motion_prompt": "風で髪が揺れる"})
    assert second.json()["ordinal"] == 2

    listed = client.get(f"/api/v1/still-images/{still_image_id}/shots").json()
    assert [item["ordinal"] for item in listed] == [1, 2]

    updated = client.patch(f"/api/v1/shots/{shot['id']}", json={"motion_prompt": "はっきり瞬きする"})
    assert updated.json()["motion_prompt"] == "はっきり瞬きする"

    deleted = client.delete(f"/api/v1/shots/{shot['id']}")
    assert deleted.status_code == 204
    assert client.get(f"/api/v1/shots/{shot['id']}").status_code == 404


def test_still_image_lists_scene_progress(client: TestClient, still_image_id: str, tmp_path: Path) -> None:
    """シーンの一覧は、カットを開かずにカットの数と動画のあるカットの数を出せる。"""
    from chotto_sashie_api.shots import ShotRepository

    first = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()
    client.post(f"/api/v1/still-images/{still_image_id}/shots", json={})
    project_id = client.get(f"/api/v1/still-images/{still_image_id}").json()["project_id"]

    listed = client.get(f"/api/v1/projects/{project_id}/still-images").json()
    assert (listed[0]["shot_count"], listed[0]["shot_video_count"]) == (2, 0)

    video = tmp_path / "cut.webp"
    video.write_bytes(b"video")
    ShotRepository(main.database).set_output_video(first["id"], video)
    still_image = client.get(f"/api/v1/still-images/{still_image_id}").json()
    assert (still_image["shot_count"], still_image["shot_video_count"]) == (2, 1)

    # 道だけ残って実体が無い動画は、カットの has_video と同じく数えない。
    video.unlink()
    still_image = client.get(f"/api/v1/still-images/{still_image_id}").json()
    assert still_image["shot_video_count"] == 0


def test_create_shot_under_missing_still_image_returns_404(client: TestClient) -> None:
    response = client.post("/api/v1/still-images/nope/shots", json={"motion_prompt": "x"})
    assert response.status_code == 404


def test_shot_length_must_be_four_k_plus_one(client: TestClient, still_image_id: str) -> None:
    shot_id = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]
    response = client.post(
        f"/api/v1/shots/{shot_id}/generate-video",
        json={"base_url": "http://127.0.0.1:8190", "length": 80},
    )
    assert response.status_code == 422
