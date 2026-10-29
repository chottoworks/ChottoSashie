from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chotto_sashie_shared.jobs import JobRepository
from chotto_sashie_api import main
from chotto_sashie_api import still_images
from chotto_sashie_api.composition import MAX_CHARACTERS, MAX_REFERENCE_IMAGES
from chotto_sashie_api.database import Database


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    database = Database(tmp_path / "video-api.sqlite3")
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(main, "jobs", JobRepository(database))
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture
def project_id(client: TestClient) -> str:
    return client.post("/api/v1/projects", json={"name": "シーンテスト"}).json()["id"]


def _make_background(client: TestClient, project_id: str, name: str = "教室") -> str:
    return client.post(
        f"/api/v1/projects/{project_id}/background-assets", json={"name": name}
    ).json()["id"]


def _make_character(client: TestClient, project_id: str, name: str = "ミナ") -> str:
    return client.post(
        f"/api/v1/projects/{project_id}/characters", json={"name": name}
    ).json()["id"]


def test_still_image_crud_with_references(client: TestClient, project_id: str) -> None:
    background_id = _make_background(client, project_id)
    character_id = _make_character(client, project_id)

    created = client.post(
        f"/api/v1/projects/{project_id}/still-images",
        json={
            "name": "登校",
            "background_asset_id": background_id,
            "character_ids": [character_id],
        },
    )
    assert created.status_code == 201
    still_image = created.json()
    assert still_image["ordinal"] == 1
    assert still_image["character_ids"] == [character_id]
    assert still_image["has_image"] is False

    second = client.post(f"/api/v1/projects/{project_id}/still-images", json={"name": "放課後"})
    assert second.json()["ordinal"] == 2

    listed = client.get(f"/api/v1/projects/{project_id}/still-images").json()
    assert [item["ordinal"] for item in listed] == [1, 2]

    updated = client.patch(f"/api/v1/still-images/{still_image['id']}", json={"name": "朝の登校"})
    assert updated.json()["name"] == "朝の登校"


def test_still_image_rejects_asset_from_other_project(client: TestClient, project_id: str) -> None:
    other_project = client.post("/api/v1/projects", json={"name": "別作品"}).json()["id"]
    foreign_background = _make_background(client, other_project, name="よその背景")

    response = client.post(
        f"/api/v1/projects/{project_id}/still-images",
        json={"name": "混線", "background_asset_id": foreign_background},
    )
    assert response.status_code == 422


def test_still_image_rejects_missing_asset(client: TestClient, project_id: str) -> None:
    response = client.post(
        f"/api/v1/projects/{project_id}/still-images",
        json={"name": "亡霊", "character_ids": ["does-not-exist"]},
    )
    assert response.status_code == 422


def test_get_missing_still_image_returns_404(client: TestClient) -> None:
    assert client.get("/api/v1/still-images/nope").status_code == 404


def test_still_image_rejects_more_characters_than_composition_allows(
    client: TestClient, project_id: str
) -> None:
    """人数の上限超えは、追加した時点で断る（合成まで待たせない）。"""
    background_id = _make_background(client, project_id)
    characters = [
        _make_character(client, project_id, f"人物{index}")
        for index in range(MAX_CHARACTERS + 1)
    ]

    response = client.post(
        f"/api/v1/projects/{project_id}/still-images",
        json={
            "name": "詰め込みすぎ",
            "background_asset_id": background_id,
            "character_ids": characters,
        },
    )
    assert response.status_code == 422
    assert f"{MAX_CHARACTERS}人まで" in response.json()["detail"]

    # 上限ちょうどなら通る。
    ok = client.post(
        f"/api/v1/projects/{project_id}/still-images",
        json={
            "name": "ちょうど",
            "background_asset_id": background_id,
            "character_ids": characters[:MAX_CHARACTERS],
        },
    )
    assert ok.status_code == 201

    # あとから増やす場合も同じところで断る。
    over = client.patch(
        f"/api/v1/still-images/{ok.json()['id']}",
        json={"character_ids": characters},
    )
    assert over.status_code == 422

    # 減らす向きの変更は通る（超えた組み合わせから戻れる道を残す）。
    reduced = client.patch(
        f"/api/v1/still-images/{ok.json()['id']}",
        json={"character_ids": characters[:1]},
    )
    assert reduced.status_code == 200
    assert reduced.json()["character_ids"] == characters[:1]


def test_existing_over_limit_still_image_can_still_be_fixed(
    client: TestClient, project_id: str
) -> None:
    """上限を下げる前に作った、上限超えの静止画も、直せる道を残す。

    増やす向きだけを断る。名前を変えることも、人物を減らして上限内へ戻すことも
    できないと、その静止画は捨てるしかなくなる。
    """
    background_id = _make_background(client, project_id)
    characters = [
        _make_character(client, project_id, f"人物{index}")
        for index in range(MAX_CHARACTERS + 1)
    ]

    # 上限が緩かったころに作られた静止画を再現する。
    original_limit = still_images.MAX_CHARACTERS
    still_images.MAX_CHARACTERS = MAX_CHARACTERS + 1
    try:
        over = client.post(
            f"/api/v1/projects/{project_id}/still-images",
            json={
                "name": "上限超え",
                "background_asset_id": background_id,
                "character_ids": characters,
            },
        )
    finally:
        still_images.MAX_CHARACTERS = original_limit
    assert over.status_code == 201
    still_image_id = over.json()["id"]

    # 合成はできない。
    assert client.get(f"/api/v1/still-images/{still_image_id}").json()["can_compose"] is False

    # 名前だけの変更は通る。
    renamed = client.patch(f"/api/v1/still-images/{still_image_id}", json={"name": "直す前"})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "直す前"

    # 減らして上限内へ戻せる。
    fixed = client.patch(
        f"/api/v1/still-images/{still_image_id}",
        json={"character_ids": characters[:MAX_CHARACTERS]},
    )
    assert fixed.status_code == 200
    assert fixed.json()["character_ids"] == characters[:MAX_CHARACTERS]


def _still_image_ids(client: TestClient, project_id: str) -> list[str]:
    return [item["id"] for item in client.get(f"/api/v1/projects/{project_id}/still-images").json()]


def test_still_images_can_be_reordered(client: TestClient, project_id: str) -> None:
    """渡した順に並び、番号は抜けを詰めて 1 から振り直す。あとから足した1枚は末尾に付く。"""
    url = f"/api/v1/projects/{project_id}/still-images"
    first, removed, second, third = (
        client.post(url, json={"name": name}).json()["id"] for name in ("朝", "昼", "夕方", "夜")
    )
    client.delete(f"/api/v1/still-images/{removed}")
    order = [third, first, second]

    response = client.put(f"{url}/order", json={"still_image_ids": order})

    assert response.status_code == 200, response.text
    assert [(item["id"], item["ordinal"]) for item in response.json()] == [
        (third, 1),
        (first, 2),
        (second, 3),
    ]
    assert _still_image_ids(client, project_id) == order
    fourth = client.post(url, json={"name": "深夜"}).json()
    assert fourth["ordinal"] == 4
    assert _still_image_ids(client, project_id) == [*order, fourth["id"]]


def test_still_image_reorder_is_refused_when_the_list_changed(
    client: TestClient, project_id: str
) -> None:
    """画面が古い一覧を見ていたら断り、並びは変えない。別の作品の静止画も受け付けない。"""
    url = f"/api/v1/projects/{project_id}/still-images"
    first, second = (client.post(url, json={"name": name}).json()["id"] for name in ("朝", "夜"))
    other_project = client.post("/api/v1/projects", json={"name": "別の作品"}).json()["id"]
    other = client.post(
        f"/api/v1/projects/{other_project}/still-images", json={"name": "よそ"}
    ).json()["id"]

    for still_image_ids in ([second], [second, first, other], [first] * 2):
        response = client.put(f"{url}/order", json={"still_image_ids": still_image_ids})
        assert response.status_code == 409
    assert _still_image_ids(client, project_id) == [first, second]


def test_reordering_still_images_of_a_missing_project_returns_404(client: TestClient) -> None:
    response = client.put("/api/v1/projects/missing/still-images/order", json={"still_image_ids": []})
    assert response.status_code == 404
