import base64
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from chotto_sashie_shared.jobs import JobRepository
from chotto_sashie_api import main, shots, still_images
from chotto_sashie_api.database import Database
from chotto_sashie_api.still_images import StillImageRepository

_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _animated_webp(color_first=(10, 20, 30), color_last=(200, 100, 50)) -> bytes:
    """2フレームのアニメーションwebp。最終フレームはcolor_last一色。"""
    frames = [
        Image.new("RGB", (8, 8), color_first),
        Image.new("RGB", (8, 8), color_last),
    ]
    buffer = io.BytesIO()
    frames[0].save(
        buffer,
        format="WEBP",
        save_all=True,
        append_images=frames[1:],
        duration=100,
        lossless=True,
    )
    return buffer.getvalue()


_LAST_COLOR = (200, 100, 50)
_ANIMATED_WEBP = _animated_webp(color_last=_LAST_COLOR)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    database = Database(tmp_path / "video-api.sqlite3")
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(main, "jobs", JobRepository(database))
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture
def fake_comfy(monkeypatch: pytest.MonkeyPatch) -> dict:
    captured: dict = {"uploads": []}

    async def upload_image(_self, data, filename, content_type="image/png", *, subfolder="", overwrite=True):
        captured["uploads"].append({"filename": filename, "data": data})
        return {"name": filename, "subfolder": "", "type": "input"}

    async def submit(_self, workflow, _client_id):
        return "shot-x"

    async def history(_self, prompt_id):
        return {
            "status": {"completed": True, "status_str": "success"},
            "outputs": {"14": {"images": [{"filename": "v.webp", "subfolder": "shots", "type": "output"}]}},
        }

    async def view_image(_self, image):
        return _ANIMATED_WEBP, "image/webp"

    monkeypatch.setattr(main.ComfyClient, "upload_image", upload_image)
    monkeypatch.setattr(main.ComfyClient, "submit", submit)
    monkeypatch.setattr(main.ComfyClient, "history", history)
    monkeypatch.setattr(main.ComfyClient, "view_image", view_image)
    return captured


def _still_image_with_image(client: TestClient) -> str:
    project_id = client.post("/api/v1/projects", json={"name": "連続性テスト"}).json()["id"]
    still_image_id = client.post(f"/api/v1/projects/{project_id}/still-images", json={"name": "S"}).json()["id"]
    image_dir = main._STILL_IMAGE_ROOT
    image_dir.mkdir(parents=True, exist_ok=True)
    image_file = image_dir / f"{still_image_id}.png"
    image_file.write_bytes(_TINY_PNG)
    StillImageRepository(main.database).set_image(still_image_id, image_file, source_prompt="")
    return still_image_id


def _generate(client: TestClient, shot_id: str, **body) -> None:
    payload = {"base_url": "http://127.0.0.1:8190", **body}
    resp = client.post(f"/api/v1/shots/{shot_id}/generate-video", json=payload)
    assert resp.status_code == 200


def test_second_shot_starts_from_first_shot_last_frame(client: TestClient, fake_comfy: dict) -> None:
    still_image_id = _still_image_with_image(client)
    shot1 = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]
    shot2 = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]

    _generate(client, shot1)
    # カット1が最終フレームを持つ。
    assert client.get(f"/api/v1/shots/{shot1}").json()["has_last_frame"] is True

    fake_comfy["uploads"].clear()
    _generate(client, shot2)

    # カット2の起点として、カット1の最終フレーム(color_last一色のPNG)がアップロードされる。
    uploaded = fake_comfy["uploads"][0]["data"]
    with Image.open(io.BytesIO(uploaded)) as image:
        assert image.convert("RGB").getpixel((0, 0)) == _LAST_COLOR
    assert client.get(f"/api/v1/shots/{shot2}").json()["has_start_frame"] is True


def test_start_frame_version_follows_the_picture_it_points_to(
    client: TestClient, fake_comfy: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """起点の絵は他の記録が同じ場所へ書き直す。時刻が同じでも、書き直せば起点の版が変わる。"""
    for module in (shots, still_images):
        monkeypatch.setattr(module, "_now", lambda: "2026-01-01T00:00:00+00:00")
    still_image_id = _still_image_with_image(client)
    shot1 = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]
    shot2 = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]

    def version(shot_id: str) -> str:
        return client.get(f"/api/v1/shots/{shot_id}").json()["start_frame_version"]

    # カット1の起点は静止画。合成し直すと、同じ場所へ書いても版が変わる。
    from_still = version(shot1)
    StillImageRepository(main.database).set_image(
        still_image_id, main._STILL_IMAGE_ROOT / f"{still_image_id}.png", source_prompt=""
    )
    assert version(shot1) != from_still

    # カット2の起点はカット1の最終フレーム。カット1を作り直すと版が変わる。
    _generate(client, shot1)
    from_previous = version(shot2)
    _generate(client, shot1)
    assert version(shot2) != from_previous



def test_regenerating_a_video_moves_its_version_even_within_the_same_instant(
    client: TestClient, fake_comfy: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """動画は同じ場所へ上書きする。時刻が同じでも、作り直すたびに動画URLの版が変わる。"""
    monkeypatch.setattr(shots, "_now", lambda: "2026-01-01T00:00:00+00:00")
    still_image_id = _still_image_with_image(client)
    shot_id = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]
    versions = [client.get(f"/api/v1/shots/{shot_id}").json()["video_version"]]
    for _ in range(2):
        _generate(client, shot_id)
        versions.append(client.get(f"/api/v1/shots/{shot_id}").json()["video_version"])

    assert len(set(versions)) == len(versions)


def test_continue_disabled_falls_back_to_still_image(client: TestClient, fake_comfy: dict) -> None:
    still_image_id = _still_image_with_image(client)
    shot1 = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]
    shot2 = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]

    _generate(client, shot1)
    fake_comfy["uploads"].clear()
    _generate(client, shot2, continue_from_previous=False)

    # 連続性オフなら、カット1の最終フレームではなくシーンのキーフレームが起点になる。
    uploaded = fake_comfy["uploads"][0]["data"]
    assert uploaded == _TINY_PNG


def test_first_shot_uses_still_image(client: TestClient, fake_comfy: dict) -> None:
    still_image_id = _still_image_with_image(client)
    shot1 = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]

    _generate(client, shot1)
    uploaded = fake_comfy["uploads"][0]["data"]
    assert uploaded == _TINY_PNG
