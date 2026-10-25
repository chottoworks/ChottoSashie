"""一覧に出す代表写真の範囲（トリミング）を確かめる。

既定では画像の全体を出し、利用者が範囲を決めたときだけその範囲を切り出す。
保存してある画像そのものは切らないので、いつでも全体へ戻せる。
"""

from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from chotto_sashie_shared.jobs import JobRepository
from chotto_sashie_api import main
from chotto_sashie_api.database import Database

_RED = (255, 0, 0)
_BLUE = (0, 0, 255)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    database = Database(tmp_path / "video-api.sqlite3")
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(main, "jobs", JobRepository(database))
    monkeypatch.setattr(main.settings, "data_dir", tmp_path / "app-data")
    with TestClient(main.app) as test_client:
        yield test_client


def _character(client: TestClient) -> str:
    project_id = client.post("/api/v1/projects", json={"name": "作品"}).json()["id"]
    return client.post(
        f"/api/v1/projects/{project_id}/characters", json={"name": "人物"}
    ).json()["id"]


def _tall_png() -> bytes:
    """縦長（40×80）で、上半分が赤・下半分が青の画像。"""
    image = Image.new("RGB", (40, 80), _BLUE)
    image.paste(_RED, (0, 0, 40, 40))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _upload_portrait(client: TestClient, character_id: str, data: bytes, media_type: str) -> None:
    response = client.post(
        f"/api/v1/characters/{character_id}/portrait",
        files={"file": ("face", data, media_type)},
    )
    assert response.status_code == 200, response.text


def _image(client: TestClient, path: str) -> Image.Image:
    response = client.get(path)
    assert response.status_code == 200, response.text
    return Image.open(BytesIO(response.content)).convert("RGB")


def _crop(client: TestClient, character_id: str, crop: dict | None):
    return client.put(f"/api/v1/characters/{character_id}/portrait/crop", json={"crop": crop})


_BOTTOM_HALF = {"x": 0, "y": 0.5, "width": 1, "height": 0.5}


def test_portrait_is_shown_whole_until_a_range_is_chosen(client: TestClient) -> None:
    character_id = _character(client)
    _upload_portrait(client, character_id, _tall_png(), "image/png")

    assert _image(client, f"/api/v1/characters/{character_id}/portrait").size == (40, 80)
    assert client.get(f"/api/v1/characters/{character_id}").json()["portrait_crop"] is None


def test_chosen_range_is_cut_out_and_the_source_stays_whole(client: TestClient) -> None:
    character_id = _character(client)
    _upload_portrait(client, character_id, _tall_png(), "image/png")
    before = client.get(f"/api/v1/characters/{character_id}").json()["portrait_version"]

    response = _crop(client, character_id, _BOTTOM_HALF)

    assert response.status_code == 200, response.text
    assert response.json()["portrait_crop"] == _BOTTOM_HALF
    # 一覧の画像URLが変わり、前の見た目を出し続けない。
    assert response.json()["portrait_version"] != before
    cropped = _image(client, f"/api/v1/characters/{character_id}/portrait")
    assert cropped.size == (40, 40)
    assert cropped.getpixel((20, 20)) == _BLUE
    # 範囲を決める画面には、切る前の全体を出す。
    assert _image(client, f"/api/v1/characters/{character_id}/portrait/source").size == (40, 80)


def test_clearing_the_range_shows_the_whole_image_again(client: TestClient) -> None:
    character_id = _character(client)
    _upload_portrait(client, character_id, _tall_png(), "image/png")
    _crop(client, character_id, _BOTTOM_HALF)

    response = _crop(client, character_id, None)

    assert response.status_code == 200, response.text
    assert response.json()["portrait_crop"] is None
    assert _image(client, f"/api/v1/characters/{character_id}/portrait").size == (40, 80)


def test_changing_the_portrait_drops_the_range_made_for_the_old_one(client: TestClient) -> None:
    character_id = _character(client)
    _upload_portrait(client, character_id, _tall_png(), "image/png")
    _crop(client, character_id, _BOTTOM_HALF)

    _upload_portrait(client, character_id, _tall_png(), "image/png")

    assert client.get(f"/api/v1/characters/{character_id}").json()["portrait_crop"] is None
    assert _image(client, f"/api/v1/characters/{character_id}/portrait").size == (40, 80)


def test_range_on_the_default_figure_pins_that_figure(client: TestClient) -> None:
    """代表写真を決めずに範囲を決めたら、いま出ている立ち絵を代表写真として固定する。"""
    character_id = _character(client)
    figure = client.post(
        f"/api/v1/characters/{character_id}/figures/upload",
        files={"file": ("figure.png", _tall_png(), "image/png")},
    ).json()

    response = _crop(client, character_id, _BOTTOM_HALF)

    assert response.status_code == 200, response.text
    assert response.json()["has_portrait_image"] is True
    assert response.json()["portrait_source"] == f"figure-{figure['id']}"
    assert _image(client, f"/api/v1/characters/{character_id}/portrait").getpixel((20, 20)) == _BLUE


def test_range_needs_an_image(client: TestClient) -> None:
    character_id = _character(client)

    assert _crop(client, character_id, _BOTTOM_HALF).status_code == 404


@pytest.mark.parametrize(
    "crop",
    [
        {"x": 0.5, "y": 0, "width": 0.6, "height": 1},
        {"x": 0, "y": 0, "width": 0, "height": 1},
        {"x": -0.1, "y": 0, "width": 0.5, "height": 1},
    ],
)
def test_range_outside_the_image_is_refused(client: TestClient, crop: dict) -> None:
    character_id = _character(client)
    _upload_portrait(client, character_id, _tall_png(), "image/png")

    assert _crop(client, character_id, crop).status_code == 422


def test_range_follows_the_orientation_recorded_in_a_photo(client: TestClient) -> None:
    """縦に撮った写真（向きがEXIFに記録されている）でも、画面で見えている向きで切る。"""
    # 保存されている画素は横長（左が赤・右が青）。向き6（右へ90度回して見る）なので、
    # 画面では縦長に見え、上半分が赤になる。
    stored = Image.new("RGB", (80, 40), _BLUE)
    stored.paste(_RED, (0, 0, 40, 40))
    exif = Image.Exif()
    exif[0x0112] = 6
    output = BytesIO()
    stored.save(output, format="JPEG", exif=exif, quality=95)
    character_id = _character(client)
    _upload_portrait(client, character_id, output.getvalue(), "image/jpeg")

    _crop(client, character_id, {"x": 0, "y": 0, "width": 1, "height": 0.5})

    cropped = _image(client, f"/api/v1/characters/{character_id}/portrait")
    assert cropped.size == (40, 40)
    red, _green, blue = cropped.getpixel((20, 20))
    assert red > 200 and blue < 60
