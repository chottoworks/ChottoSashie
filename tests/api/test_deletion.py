"""削除の内訳提示と、シーンが持つ参照の掃除。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chotto_sashie_shared.jobs import JobRepository
from chotto_sashie_api import main
from chotto_sashie_api.database import Database
from chotto_sashie_api.exporting import project_export_path


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def client(tmp_path: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    database = Database(tmp_path / "video-api.sqlite3")
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(main, "jobs", JobRepository(database))
    monkeypatch.setattr(main.settings, "data_dir", data_dir)
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture
def project_id(client: TestClient) -> str:
    return client.post("/api/v1/projects", json={"name": "削除テスト"}).json()["id"]


def _character(client: TestClient, project_id: str, name: str = "ミナ") -> str:
    return client.post(
        f"/api/v1/projects/{project_id}/characters", json={"name": name}
    ).json()["id"]


def _prop(client: TestClient, project_id: str, name: str = "古い手紙") -> str:
    return client.post(
        f"/api/v1/projects/{project_id}/prop-assets", json={"name": name}
    ).json()["id"]


def _still_image(client: TestClient, project_id: str, **payload: object) -> dict:
    return client.post(f"/api/v1/projects/{project_id}/still-images", json=payload).json()


def test_deleting_character_detaches_it_from_still_images(client: TestClient, project_id: str) -> None:
    character_id = _character(client, project_id)
    other_id = _character(client, project_id, "レン")
    still_image = _still_image(
        client,
        project_id,
        name="放課後",
        character_ids=[character_id, other_id],
    )

    preview = client.get(f"/api/v1/characters/{character_id}/deletion-preview")
    assert preview.status_code == 200
    assert preview.json()["usage"]["still_image_count"] == 1
    assert preview.json()["usage"]["still_image_names"] == ["放課後"]

    assert client.delete(f"/api/v1/characters/{character_id}").status_code == 204

    remaining = client.get(f"/api/v1/still-images/{still_image['id']}").json()
    assert remaining["character_ids"] == [other_id]
    assert client.delete(f"/api/v1/characters/{character_id}").status_code == 404


def test_deleting_prop_leaves_still_images_alone(client: TestClient, project_id: str) -> None:
    """小物は静止画に載らない（立ち絵の変更と背景のバリエーションの入力にしか使わない）ので、外れる静止画も無い。"""
    prop_id = _prop(client, project_id)
    still_image = _still_image(client, project_id, name="教室")

    preview = client.get(f"/api/v1/prop-assets/{prop_id}/deletion-preview").json()
    assert preview["usage"]["still_image_count"] == 0

    assert client.delete(f"/api/v1/prop-assets/{prop_id}").status_code == 204
    assert client.get(f"/api/v1/still-images/{still_image['id']}").status_code == 200


def test_still_image_deletion_preview_counts_shots(client: TestClient, project_id: str) -> None:
    still_image = _still_image(client, project_id, name="夕暮れ")
    client.post(f"/api/v1/still-images/{still_image['id']}/shots", json={"motion_prompt": "振り返る"})
    client.post(f"/api/v1/still-images/{still_image['id']}/shots", json={"motion_prompt": "歩き出す"})

    preview = client.get(f"/api/v1/still-images/{still_image['id']}/deletion-preview").json()
    assert preview["shot_count"] == 2
    assert preview["video_count"] == 0
    assert preview["has_image"] is False

    assert client.delete(f"/api/v1/still-images/{still_image['id']}").status_code == 204
    assert client.get(f"/api/v1/still-images/{still_image['id']}").status_code == 404
    assert client.get(f"/api/v1/still-images/{still_image['id']}/deletion-preview").status_code == 404


def test_shot_deletion_preview_and_delete(client: TestClient, project_id: str) -> None:
    still_image = _still_image(client, project_id, name="夜道")
    shot = client.post(
        f"/api/v1/still-images/{still_image['id']}/shots", json={"motion_prompt": "立ち止まる"}
    ).json()

    preview = client.get(f"/api/v1/shots/{shot['id']}/deletion-preview").json()
    assert preview["ordinal"] == 1
    assert preview["has_video"] is False

    assert client.delete(f"/api/v1/shots/{shot['id']}").status_code == 204
    assert client.get(f"/api/v1/shots/{shot['id']}").status_code == 404


def test_project_deletion_preview_and_delete(
    client: TestClient, project_id: str, data_dir: Path
) -> None:
    character_id = _character(client, project_id)
    _prop(client, project_id)
    still_image = _still_image(client, project_id, name="始まり", character_ids=[character_id])
    client.post(f"/api/v1/still-images/{still_image['id']}/shots", json={"motion_prompt": "歩く"})
    # 生成物の実体があっても、アプリ管理領域の内側なら片付ける。
    image_file = data_dir / "keyframes" / f"{still_image['id']}.png"
    image_file.parent.mkdir(parents=True, exist_ok=True)
    image_file.write_bytes(b"png")
    main.StillImageRepository(main.database).set_image(still_image["id"], image_file)

    preview = client.get(f"/api/v1/projects/{project_id}/deletion-preview").json()
    assert preview["character_count"] == 1
    assert preview["prop_count"] == 1
    assert preview["still_image_count"] == 1
    assert preview["shot_count"] == 1

    assert client.delete(f"/api/v1/projects/{project_id}").status_code == 204
    assert client.get(f"/api/v1/projects/{project_id}").status_code == 404
    assert client.get(f"/api/v1/projects/{project_id}/characters").status_code == 404
    assert not image_file.exists()
    assert client.delete(f"/api/v1/projects/{project_id}").status_code == 404


def test_project_deletion_removes_its_exported_video(
    client: TestClient, project_id: str, data_dir: Path
) -> None:
    """書き出した1本も作品の持ち物。ほかの作品の書き出しは残す。"""
    other_id = client.post("/api/v1/projects", json={"name": "別の作品"}).json()["id"]
    exported = project_export_path(data_dir, project_id)
    other = project_export_path(data_dir, other_id)
    exported.parent.mkdir(parents=True, exist_ok=True)
    exported.write_bytes(b"mp4")
    other.write_bytes(b"mp4")

    assert client.delete(f"/api/v1/projects/{project_id}").status_code == 204

    assert not exported.exists()
    assert other.exists()


def test_project_deletion_keeps_files_outside_the_managed_folder(
    client: TestClient, project_id: str, tmp_path: Path, data_dir: Path
) -> None:
    still_image = _still_image(client, project_id, name="外部参照")
    outside = tmp_path / "existing-comfyui" / "output" / "keep.png"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"keep")
    main.StillImageRepository(main.database).set_image(still_image["id"], outside)

    assert client.delete(f"/api/v1/projects/{project_id}").status_code == 204
    assert outside.exists()
