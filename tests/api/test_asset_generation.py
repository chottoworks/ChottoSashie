import pytest
from fastapi.testclient import TestClient

from chotto_sashie_shared.jobs import JobCreate, JobRepository, JobTransition
from chotto_sashie_api import main
from chotto_sashie_api.database import Database


@pytest.fixture
def client(tmp_path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    database = Database(tmp_path / "video-api.sqlite3")
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(main, "jobs", JobRepository(database))
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture
def fake_comfy(monkeypatch: pytest.MonkeyPatch) -> dict:
    captured: dict = {"workflow": None, "bytes": b"GENERATED-ASSET"}

    async def submit(_self, workflow):
        captured["workflow"] = workflow
        return "asset-gen-1"

    async def history(_self, prompt_id):
        return {
            "status": {"completed": True, "status_str": "success"},
            "outputs": {"9": {"images": [{"filename": f"{prompt_id}.png", "subfolder": "", "type": "output"}]}},
        }

    async def view_image(_self, image):
        return captured["bytes"], "image/png"

    monkeypatch.setattr(main.ComfyClient, "submit", submit)
    monkeypatch.setattr(main.ComfyClient, "history", history)
    monkeypatch.setattr(main.ComfyClient, "view_image", view_image)
    return captured


@pytest.fixture
def project_id(client: TestClient) -> str:
    return client.post("/api/v1/projects", json={"name": "生成テスト"}).json()["id"]


@pytest.mark.parametrize("path_prefix", ["background-assets", "prop-assets"])
def test_generate_sets_asset_image(client, fake_comfy, project_id, path_prefix) -> None:
    asset_id = client.post(
        f"/api/v1/projects/{project_id}/{path_prefix}", json={"name": "夕暮れの教室"}
    ).json()["id"]

    generated = client.post(
        f"/api/v1/{path_prefix}/{asset_id}/generate",
        json={"base_url": "http://127.0.0.1:8190", "prompt": "empty classroom at dusk", "seed": 55},
    )
    assert generated.status_code == 200
    job_id = generated.json()["job_id"]

    job = client.get(f"/api/v1/jobs/{job_id}").json()
    assert job["state"] == "completed"

    # プロンプト・seed・サイズがワークフローへ反映されている。
    workflow = fake_comfy["workflow"]
    assert workflow["4"]["inputs"]["text"] == "empty classroom at dusk"
    assert workflow["7"]["inputs"]["seed"] == 55

    asset = client.get(f"/api/v1/{path_prefix}/{asset_id}").json()
    assert asset["has_image"] is True
    assert asset["source_prompt"] == "empty classroom at dusk"

    served = client.get(f"/api/v1/{path_prefix}/{asset_id}/image")
    assert served.status_code == 200
    assert served.content == b"GENERATED-ASSET"


def test_generate_missing_asset_returns_404(client, fake_comfy, project_id) -> None:
    response = client.post(
        "/api/v1/background-assets/nope/generate",
        json={"base_url": "http://127.0.0.1:8190", "prompt": "x"},
    )
    assert response.status_code == 404


def test_generate_is_queued_when_gpu_busy(client, fake_comfy, project_id) -> None:
    """GPUが埋まっていても断らず、順番待ちへ入れる。"""
    asset_id = client.post(
        f"/api/v1/projects/{project_id}/background-assets", json={"name": "廊下"}
    ).json()["id"]

    busy = main.jobs.create(JobCreate(kind="composition.still_image", payload={}))
    main.jobs.transition(busy.id, JobTransition(state="preparing"))

    response = client.post(
        f"/api/v1/background-assets/{asset_id}/generate",
        json={"base_url": "http://127.0.0.1:8190", "prompt": "hallway"},
    )
    assert response.status_code == 200
    queued = main.jobs.get(response.json()["job_id"])
    assert queued.kind == "generation.asset"


def test_regenerate_overwrites_image(client, fake_comfy, project_id) -> None:
    asset_id = client.post(
        f"/api/v1/projects/{project_id}/background-assets", json={"name": "書架"}
    ).json()["id"]

    fake_comfy["bytes"] = b"FIRST"
    client.post(
        f"/api/v1/background-assets/{asset_id}/generate",
        json={"base_url": "http://127.0.0.1:8190", "prompt": "bookshelf v1"},
    )
    assert client.get(f"/api/v1/background-assets/{asset_id}/image").content == b"FIRST"

    fake_comfy["bytes"] = b"SECOND"
    client.post(
        f"/api/v1/background-assets/{asset_id}/generate",
        json={"base_url": "http://127.0.0.1:8190", "prompt": "bookshelf v2"},
    )
    assert client.get(f"/api/v1/background-assets/{asset_id}/image").content == b"SECOND"

    asset = client.get(f"/api/v1/background-assets/{asset_id}").json()
    assert asset["source_prompt"] == "bookshelf v2"


@pytest.mark.parametrize("path_prefix", ["background-assets", "prop-assets"])
def test_generate_keeps_the_prompt_as_typed(client, fake_comfy, project_id, path_prefix) -> None:
    """ComfyUI へは作品の画風を足して渡し、生成プロンプト欄へ戻す文は入れたままの形で残す。"""
    client.patch(f"/api/v1/projects/{project_id}", json={"illustration_style": "淡い水彩"})
    asset_id = client.post(
        f"/api/v1/projects/{project_id}/{path_prefix}", json={"name": "古い置き時計"}
    ).json()["id"]

    client.post(
        f"/api/v1/{path_prefix}/{asset_id}/generate",
        json={"base_url": "http://127.0.0.1:8190", "prompt": "an old mantel clock"},
    )

    asset = client.get(f"/api/v1/{path_prefix}/{asset_id}").json()
    assert asset["source_prompt"] == "an old mantel clock 作品全体の画風: 淡い水彩"
    assert asset["input_prompt"] == "an old mantel clock"


def test_uploading_an_image_drops_the_prompt(client, fake_comfy, project_id) -> None:
    """アップロードした画像はプロンプトから作ったものではないので、前の文を欄へ戻さない。"""
    asset_id = client.post(
        f"/api/v1/projects/{project_id}/prop-assets", json={"name": "傘"}
    ).json()["id"]
    client.post(
        f"/api/v1/prop-assets/{asset_id}/generate",
        json={"base_url": "http://127.0.0.1:8190", "prompt": "a red umbrella"},
    )

    uploaded = client.post(
        f"/api/v1/prop-assets/{asset_id}/image",
        files={"file": ("umbrella.png", _png(), "image/png")},
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["source_prompt"] == ""
    assert uploaded.json()["input_prompt"] == ""


def _png() -> bytes:
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (8, 8), "white").save(buffer, format="PNG")
    return buffer.getvalue()
