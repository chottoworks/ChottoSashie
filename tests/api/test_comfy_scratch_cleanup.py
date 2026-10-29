"""生成のあとに、ComfyUI側へ残る写しを片付けることを確かめる。

ComfyUIは生成物をまず自分の出力フォルダへ保存し、アプリはそれを自分の保存先へ写す。
送り込んだ画像も入力フォルダに残る。専用ComfyUIではこの2つはアプリの持ち物なので、
写し終えたら消す（没にした画像まで溜まり続け、作品を消しても消えないため）。
既存ComfyUIのフォルダは利用者のものなので、そのまま残す。
"""

import base64
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from chotto_sashie_shared.jobs import JobRepository
from chotto_sashie_shared.runtime import comfy_io_directories
from chotto_sashie_api import main
from chotto_sashie_api.database import Database

_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

_EXISTING_URL = "http://127.0.0.1:8190"


@pytest.fixture
def folders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """専用ComfyUIの入力・出力フォルダ（このアプリの管理領域の中）。"""
    monkeypatch.setattr(main, "MANAGED_ROOT", tmp_path / "managed")
    input_dir, output_dir = comfy_io_directories(tmp_path / "managed")
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    return input_dir, output_dir


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    database = Database(tmp_path / "video-api.sqlite3")
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(main, "jobs", JobRepository(database))
    monkeypatch.setattr(main.settings, "data_dir", tmp_path / "app-data")
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture
def fake_comfy(monkeypatch: pytest.MonkeyPatch, folders: tuple[Path, Path]) -> dict:
    """送られた画像と生成物を、本物と同じく入力・出力フォルダへ実際に置く偽ComfyUI。"""
    input_dir, output_dir = folders
    captured: dict = {"workflows": [], "uploads": []}

    async def object_info(_self):
        return {
            "UNETLoader": {
                "input": {
                    "required": {
                        "unet_name": [
                            [
                                "qwen_image_edit_2511_fp8mixed.safetensors",
                                "qwen_image_edit_2511_bf16.safetensors",
                            ]
                        ]
                    }
                }
            }
        }

    async def upload_image(
        _self, data, filename, content_type="image/png", *, subfolder="", overwrite=True
    ):
        (input_dir / filename).write_bytes(data)
        captured["uploads"].append(filename)
        return {"name": filename, "subfolder": "", "type": "input"}

    async def submit(_self, workflow):
        prompt_id = uuid4().hex
        captured["workflows"].append(workflow)
        produced = output_dir / "ChottoSashie" / "made" / f"{prompt_id}.png"
        produced.parent.mkdir(parents=True, exist_ok=True)
        produced.write_bytes(_TINY_PNG)
        return prompt_id

    async def history(_self, prompt_id):
        return {
            "status": {"completed": True, "status_str": "success"},
            "outputs": {
                "9": {
                    "images": [
                        {
                            "filename": f"{prompt_id}.png",
                            "subfolder": "ChottoSashie/made",
                            "type": "output",
                        }
                    ]
                }
            },
        }

    async def view_image(_self, _image):
        return _TINY_PNG, "image/png"

    async def free_vram(_self):
        return True

    monkeypatch.setattr(main.ComfyClient, "object_info", object_info)
    monkeypatch.setattr(main.ComfyClient, "upload_image", upload_image)
    monkeypatch.setattr(main.ComfyClient, "submit", submit)
    monkeypatch.setattr(main.ComfyClient, "history", history)
    monkeypatch.setattr(main.ComfyClient, "view_image", view_image)
    monkeypatch.setattr(main.ComfyClient, "free_vram", free_vram)
    return captured


def _use_managed(client: TestClient) -> None:
    response = client.put("/api/v1/setup/config", json={"mode": "managed_dedicated"})
    assert response.status_code == 200, response.text


def _use_existing(client: TestClient) -> None:
    response = client.put(
        "/api/v1/setup/config", json={"mode": "existing_comfyui", "base_url": _EXISTING_URL}
    )
    assert response.status_code == 200, response.text


def _files(folder: Path) -> list[Path]:
    return [path for path in folder.rglob("*") if path.is_file()]


def _finish_job(client: TestClient, job_id: str) -> dict:
    for _ in range(200):
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["state"] in {"completed", "failed", "cancelled", "interrupted"}:
            return job
    raise AssertionError(f"ジョブが終わりませんでした: {job_id}")


def _background_variation(client: TestClient) -> dict:
    project_id = client.post("/api/v1/projects", json={"name": "作品"}).json()["id"]
    background = client.post(
        f"/api/v1/projects/{project_id}/background-assets", json={"name": "教室"}
    ).json()
    client.post(
        f"/api/v1/background-assets/{background['id']}/image",
        files={"file": ("bg.png", _TINY_PNG, "image/png")},
    )
    response = client.post(
        f"/api/v1/background-assets/{background['id']}/variations",
        json={"kind": "time", "instruction": "夕方にする", "variant_label": "夕方"},
    )
    assert response.status_code == 200, response.text
    assert _finish_job(client, response.json()["job_id"])["state"] == "completed"
    return response.json()["asset"]


def test_managed_comfyui_copies_are_removed_after_saving(
    client: TestClient, fake_comfy: dict, folders: tuple[Path, Path]
) -> None:
    input_dir, output_dir = folders
    _use_managed(client)

    variation = _background_variation(client)

    # 出来た背景はアプリの保存先に残り、ComfyUI側の写しは残らない。
    saved = client.get(f"/api/v1/background-assets/{variation['id']}").json()
    assert saved["has_image"] is True
    assert fake_comfy["uploads"]
    assert _files(input_dir) == []
    assert _files(output_dir) == []
    # 種類ごとの入れ物も、空になれば畳む。
    assert list(output_dir.iterdir()) == []


def test_existing_comfyui_folders_are_left_alone(
    client: TestClient, fake_comfy: dict, folders: tuple[Path, Path]
) -> None:
    """既存ComfyUIへ書き込まれたものは利用者のフォルダにあるので、アプリは消さない。"""
    input_dir, output_dir = folders
    _use_existing(client)

    _background_variation(client)

    assert len(_files(input_dir)) == 1
    assert len(_files(output_dir)) == 1


def test_figures_are_made_from_the_saved_base_image(
    client: TestClient, fake_comfy: dict, folders: tuple[Path, Path]
) -> None:
    """人物候補はその場で控え、立ち絵の起点はその控えから送る。

    ComfyUIの出力フォルダを指して作ると、そこを片付けた時点で、基準画像があるのに
    立ち絵が作れなくなる。
    """
    input_dir, output_dir = folders
    _use_managed(client)
    project_id = client.post("/api/v1/projects", json={"name": "作品"}).json()["id"]
    character_id = client.post(
        f"/api/v1/projects/{project_id}/characters", json={"name": "ミナ"}
    ).json()["id"]

    started = client.post(
        f"/api/v1/characters/{character_id}/candidates/generate",
        json={"count": 8, "seed_start": 1},
    )
    assert _finish_job(client, started.json()["job"]["id"])["state"] == "completed"
    # 候補は出来た時点で控えてあり、ComfyUI側には残っていない。
    assert _files(output_dir) == []
    candidates = client.get(f"/api/v1/characters/{character_id}/candidates").json()
    client.post(f"/api/v1/character-candidates/{candidates[0]['id']}/select-base")

    response = client.post(
        f"/api/v1/characters/{character_id}/figures/generate",
        json={"seed": 10},
    )
    assert response.status_code == 200, response.text
    assert _finish_job(client, response.json()["job"]["id"])["state"] == "completed"

    # 起点は送り込んだ控え（出力フォルダの注釈付きの道ではない）。
    sent = fake_comfy["uploads"][-1]
    assert sent.endswith("-base.png")
    assert fake_comfy["workflows"][-1]["8"]["inputs"]["image"] == sent
    figures = client.get(f"/api/v1/characters/{character_id}/figures").json()
    assert [figure["state"] for figure in figures] == ["completed"]
    assert _files(input_dir) == []
    assert _files(output_dir) == []
