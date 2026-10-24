import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chotto_sashie_shared.jobs import JobCreate, JobRepository, JobTransition
from chotto_sashie_api import main, video_frames
from chotto_sashie_api.database import Database
from chotto_sashie_api.still_images import StillImageRepository

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
def fake_comfy(monkeypatch: pytest.MonkeyPatch) -> dict:
    captured: dict = {"uploads": [], "workflow": None, "video": b"WEBM-VIDEO"}

    async def upload_image(_self, data, filename, content_type="image/png", *, subfolder="", overwrite=True):
        captured["uploads"].append(filename)
        return {"name": filename, "subfolder": "", "type": "input"}

    async def submit(_self, workflow, _client_id):
        captured["workflow"] = workflow
        return "shot-1"

    async def history(_self, prompt_id):
        # SaveAnimatedWEBP は "images" キーへ1件のアニメーションファイルを出す。
        return {
            "status": {"completed": True, "status_str": "success"},
            "outputs": {"14": {"images": [{"filename": f"{prompt_id}.webp", "subfolder": "shots", "type": "output"}]}},
        }

    async def view_image(_self, image):
        return captured["video"], "image/webp"

    monkeypatch.setattr(main.ComfyClient, "upload_image", upload_image)
    monkeypatch.setattr(main.ComfyClient, "submit", submit)
    monkeypatch.setattr(main.ComfyClient, "history", history)
    monkeypatch.setattr(main.ComfyClient, "view_image", view_image)
    return captured


def _still_image_with_image(client: TestClient) -> str:
    project_id = client.post("/api/v1/projects", json={"name": "動画テスト"}).json()["id"]
    still_image_id = client.post(f"/api/v1/projects/{project_id}/still-images", json={"name": "登校"}).json()["id"]
    image_dir = main._STILL_IMAGE_ROOT
    image_dir.mkdir(parents=True, exist_ok=True)
    image_file = image_dir / f"{still_image_id}.png"
    image_file.write_bytes(_TINY_PNG)
    StillImageRepository(main.database).set_image(still_image_id, image_file)
    return still_image_id


def test_generate_video_from_still_image(client: TestClient, fake_comfy: dict) -> None:
    still_image_id = _still_image_with_image(client)
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots", json={"motion_prompt": "髪が風に揺れる"}
    ).json()["id"]

    generated = client.post(
        f"/api/v1/shots/{shot_id}/generate-video",
        json={"base_url": "http://127.0.0.1:8190", "motion_prompt": "髪が風に揺れる", "seed": 99, "length": 81},
    )
    assert generated.status_code == 200
    job_id = generated.json()["job_id"]

    job = client.get(f"/api/v1/jobs/{job_id}").json()
    assert job["state"] == "completed"

    # 起点フレームがアップロードされ、seedが両エキスパートのKSamplerへ入る。
    assert len(fake_comfy["uploads"]) == 1
    workflow = fake_comfy["workflow"]
    assert workflow["7"]["inputs"]["image"] == fake_comfy["uploads"][0]
    assert workflow["11"]["inputs"]["noise_seed"] == 99
    assert workflow["12"]["inputs"]["noise_seed"] == 99
    assert workflow["10"]["inputs"]["length"] == 81
    assert workflow["8"]["inputs"]["text"] == "髪が風に揺れる"
    # Lightning LoRAが両エキスパートへ適用され、4ステップに短縮されている。
    assert workflow["15"]["inputs"]["lora_name"] == "wan2.2_i2v_lightx2v_4steps_lora_v1_high_noise.safetensors"
    assert workflow["16"]["inputs"]["lora_name"] == "wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors"
    assert workflow["11"]["inputs"]["steps"] == 4
    assert workflow["12"]["inputs"]["steps"] == 4

    shot = client.get(f"/api/v1/shots/{shot_id}").json()
    assert shot["has_video"] is True
    assert shot["has_start_frame"] is True
    assert shot["video_job_id"] == job_id

    video = client.get(f"/api/v1/shots/{shot_id}/video")
    assert video.status_code == 200
    assert video.content == b"WEBM-VIDEO"


def test_generate_video_without_still_image_returns_409(client: TestClient, fake_comfy: dict) -> None:
    project_id = client.post("/api/v1/projects", json={"name": "キーフレームなし"}).json()["id"]
    still_image_id = client.post(f"/api/v1/projects/{project_id}/still-images", json={"name": "未合成"}).json()["id"]
    shot_id = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]

    response = client.post(
        f"/api/v1/shots/{shot_id}/generate-video",
        json={"base_url": "http://127.0.0.1:8190"},
    )
    # 起点の画像が無いのは順番待ちより前の問題なので、その場で断る。
    assert response.status_code == 409


def test_generate_video_is_queued_when_gpu_busy(client: TestClient, fake_comfy: dict) -> None:
    still_image_id = _still_image_with_image(client)
    shot_id = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]

    busy = main.jobs.create(JobCreate(kind="composition.still_image", payload={}))
    main.jobs.transition(busy.id, JobTransition(state="preparing"))

    response = client.post(
        f"/api/v1/shots/{shot_id}/generate-video",
        json={"base_url": "http://127.0.0.1:8190"},
    )
    # 断らず順番待ちへ入れる（前の処理が終わったら実行される）。
    assert response.status_code == 200
    assert main.jobs.get(response.json()["job_id"]).kind == "generation.video.shot"


@pytest.fixture
def fake_comfy_h3(monkeypatch: pytest.MonkeyPatch) -> dict:
    """MiniMax H3 は SaveVideo が音声付きmp4を1件出す。"""
    captured: dict = {"uploads": [], "workflow": None, "video": b"MP4-VIDEO"}

    async def upload_image(_self, data, filename, content_type="image/png", *, subfolder="", overwrite=True):
        captured["uploads"].append(filename)
        return {"name": filename, "subfolder": "", "type": "input"}

    async def submit(_self, workflow, _client_id):
        captured["workflow"] = workflow
        return "shot-h3"

    async def history(_self, prompt_id):
        return {
            "status": {"completed": True, "status_str": "success"},
            "outputs": {"16": {"images": [{"filename": f"{prompt_id}.mp4", "subfolder": "shots", "type": "output"}]}},
        }

    async def view_image(_self, image):
        return captured["video"], "video/mp4"

    monkeypatch.setattr(main.ComfyClient, "upload_image", upload_image)
    monkeypatch.setattr(main.ComfyClient, "submit", submit)
    monkeypatch.setattr(main.ComfyClient, "history", history)
    monkeypatch.setattr(main.ComfyClient, "view_image", view_image)
    return captured


def test_generate_video_with_minimax_h3(
    client: TestClient, fake_comfy_h3: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 最終フレーム抽出はffmpeg頼みなので、この試験では使わない（動画保存は続く）。
    monkeypatch.setattr(video_frames, "find_ffmpeg", lambda: None)
    still_image_id = _still_image_with_image(client)
    shot_id = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]

    generated = client.post(
        f"/api/v1/shots/{shot_id}/generate-video",
        json={
            "base_url": "http://127.0.0.1:8190",
            "engine": "minimax_h3",
            "motion_prompt": "ゆっくり振り返り、風が吹く。足音と衣擦れの音。",
            "seed": 7,
        },
    )
    assert generated.status_code == 200
    assert client.get(f"/api/v1/jobs/{generated.json()['job_id']}").json()["state"] == "completed"

    workflow = fake_comfy_h3["workflow"]
    # H3のテンプレートへ束縛され、Wanのノード構成は混ざらない。
    assert workflow["7"]["class_type"] == "MiniMaxH3ImageToVideo"
    # 動きの指示は、映像の記述だと分かる足場の後ろに置く（公式テンプレートの書き方）。
    prompt = workflow["7"]["inputs"]["prompt"]
    # H3は欄に分けた形で、常に時間付き。動きの指示は Second の行に入る。
    assert prompt.startswith("integrated_multimodal_description:")
    assert "ゆっくり" in prompt.splitlines()[3]
    assert workflow["6"]["inputs"]["image"] == fake_comfy_h3["uploads"][0]
    assert workflow["8"]["inputs"]["noise_seed"] == 7
    # 解像度・尺・fpsは指定しなければH3の既定（17k+5フレーム・24fps）になる。
    assert (workflow["7"]["inputs"]["width"], workflow["7"]["inputs"]["height"]) == (1344, 768)
    assert workflow["7"]["inputs"]["length"] == 124
    assert workflow["15"]["inputs"]["fps"] == 24.0
    # 高速化LoRAを前提に8ステップ。
    assert workflow["10"]["inputs"]["steps"] == 8
    assert workflow["2"]["inputs"]["lora_name"].startswith("minimax_h3_fl2v_turbo_8step")

    shot = client.get(f"/api/v1/shots/{shot_id}").json()
    assert shot["has_video"] is True
    assert shot["video_format"] == "mp4"

    video = client.get(f"/api/v1/shots/{shot_id}/video")
    assert video.status_code == 200
    assert video.headers["content-type"] == "video/mp4"
    assert video.content == b"MP4-VIDEO"


def test_minimax_h3_rejects_frame_counts_off_its_grid(client: TestClient) -> None:
    still_image_id = _still_image_with_image(client)
    shot_id = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]

    # H3は 17k+5。Wanの 81 フレームは通さない（ComfyUIまで行かせない）。
    response = client.post(
        f"/api/v1/shots/{shot_id}/generate-video",
        json={"base_url": "http://127.0.0.1:8190", "engine": "minimax_h3", "length": 81},
    )
    assert response.status_code == 422


def test_regenerating_with_another_engine_replaces_the_video(
    client: TestClient, fake_comfy_h3: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """入れ物が変わっても、1カットに残る動画は1本だけにする。

    偽ComfyUIは同じ中身を返すが、保存する拡張子は選んだエンジンで決まるため、
    作り直しで入れ物が変わる状況をここで再現できる。
    """
    monkeypatch.setattr(video_frames, "find_ffmpeg", lambda: None)
    still_image_id = _still_image_with_image(client)
    shot_id = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]

    client.post(
        f"/api/v1/shots/{shot_id}/generate-video",
        json={"base_url": "http://127.0.0.1:8190", "engine": "minimax_h3"},
    )
    assert client.get(f"/api/v1/shots/{shot_id}").json()["video_format"] == "mp4"

    client.post(
        f"/api/v1/shots/{shot_id}/generate-video",
        json={"base_url": "http://127.0.0.1:8190", "engine": "wan22"},
    )
    assert client.get(f"/api/v1/shots/{shot_id}").json()["video_format"] == "webp"
    assert sorted(p.name for p in main._SHOT_VIDEO_ROOT.glob(f"{shot_id}.*")) == [f"{shot_id}.webp"]
