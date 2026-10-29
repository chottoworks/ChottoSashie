"""人物候補から基準画像を決めるところまでを通しで確かめる。

立ち絵の作成は `test_character_figures.py`。
"""

import base64
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from chotto_sashie_shared.jobs import JobCreate, JobRepository, JobTransition
from chotto_sashie_api import main
from chotto_sashie_api.database import Database

_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    database = Database(tmp_path / "video-api.sqlite3")
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(main, "jobs", JobRepository(database))
    monkeypatch.setattr(main.settings, "data_dir", tmp_path / "app-data")
    # 接続先の設定を済ませた状態にする（生成は偽ComfyUIが受ける）。
    with TestClient(main.app) as test_client:
        test_client.put(
            "/api/v1/setup/config",
            json={"mode": "existing_comfyui", "base_url": "http://127.0.0.1:8190"},
        )
        yield test_client


@pytest.fixture
def fake_comfy(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """投入されたワークフローを記録し、生成済みとして応答する偽ComfyUI。

    `models` を書き換えると、そのファイルだけが置かれた環境として振る舞う
    （向きの精度を切り替えたときの不足検出に使う）。
    """
    submitted: dict[str, list] = {
        "workflows": [],
        "prompt_ids": [],
        # モデル解放を頼まれた時点の投入枚数。回の切れ目で頼めているかを見る。
        "freed_after": [],
        "models": [
            "z_image_bf16.safetensors",
            "qwen_image_edit_2511_fp8mixed.safetensors",
            "qwen_image_edit_2511_bf16.safetensors",
        ],
    }

    async def object_info(_self):
        return {"UNETLoader": {"input": {"required": {"unet_name": [submitted["models"]]}}}}

    monkeypatch.setattr(main.ComfyClient, "object_info", object_info)

    async def submit(_self, workflow):
        prompt_id = uuid4().hex
        submitted["workflows"].append(workflow)
        submitted["prompt_ids"].append(prompt_id)
        return prompt_id

    async def history(_self, prompt_id):
        return {
            "status": {"completed": True, "status_str": "success"},
            "outputs": {
                "9": {
                    "images": [
                        {"filename": f"{prompt_id}.png", "subfolder": "ChottoSashie", "type": "output"}
                    ]
                }
            },
        }

    async def view_image(_self, _image):
        return _TINY_PNG, "image/png"

    async def free_vram(_self):
        submitted["freed_after"].append(len(submitted["workflows"]))
        return True

    monkeypatch.setattr(main.ComfyClient, "submit", submit)
    monkeypatch.setattr(main.ComfyClient, "free_vram", free_vram)
    monkeypatch.setattr(main.ComfyClient, "history", history)
    monkeypatch.setattr(main.ComfyClient, "view_image", view_image)
    return submitted


def _character(client: TestClient) -> tuple[str, str]:
    project_id = client.post("/api/v1/projects", json={"name": "動画作品"}).json()["id"]
    character_id = client.post(
        f"/api/v1/projects/{project_id}/characters", json={"name": "ミナ"}
    ).json()["id"]
    return project_id, character_id


def test_profile_uses_shared_fields(client: TestClient) -> None:
    """挿絵ツールと同じプロフィール項目を編集できる。"""
    _project_id, character_id = _character(client)
    updated = client.patch(
        f"/api/v1/characters/{character_id}/profile",
        json={"hair_color": "濃紺", "immutable_features": ["銀色の星形髪留め"]},
    )
    assert updated.status_code == 200
    assert updated.json()["hair_color"] == "濃紺"

    profile = client.get(f"/api/v1/characters/{character_id}/profile").json()
    assert profile["immutable_features"] == ["銀色の星形髪留め"]
    # 合成側の見え方（名前と参照画像）も同じ人物として保たれる。
    assert client.get(f"/api/v1/characters/{character_id}").json()["name"] == "ミナ"


def _finish_job(client: TestClient, job_id: str) -> dict:
    """順番待ちの処理が終わるまで待つ。

    生成はジョブの順番待ちへ入るため、要求した直後には実体がまだ無い。
    ジョブを見に行くたびに待ち行列が進むので、終わりまで確かめてから続ける。
    """
    for _ in range(200):
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["state"] in {"completed", "failed", "cancelled", "interrupted"}:
            return job
    raise AssertionError(f"ジョブが終わりませんでした: {job_id}")


def test_candidate_batch_and_base_selection(
    client: TestClient, fake_comfy: dict[str, list]
) -> None:
    _project_id, character_id = _character(client)
    response = client.post(
        f"/api/v1/characters/{character_id}/candidates/generate",
        json={"count": 8, "seed_start": 100},
    )
    assert response.status_code == 200
    # 実体は順番が来てから作られる。終わるまで待ってから中身を確かめる。
    assert response.json()["candidates"] == []
    assert _finish_job(client, response.json()["job"]["id"])["state"] == "completed"
    assert len(fake_comfy["workflows"]) == 8
    # 出力先は動画ツールの名前空間へ分ける。
    prefix = fake_comfy["workflows"][0]["9"]["inputs"]["filename_prefix"]
    assert prefix.startswith("ChottoSashie/characters/")

    candidates = client.get(f"/api/v1/characters/{character_id}/candidates").json()
    assert [candidate["seed"] for candidate in candidates][:2] == [100, 101]

    selected = client.post(f"/api/v1/character-candidates/{candidates[0]['id']}/select-base")
    assert selected.status_code == 200
    assert selected.json()["is_base"] is True

    # 基準画像は立ち絵を作る起点。合成に使う画像はここでは決まらない。
    listed = client.get(f"/api/v1/projects/{_project_id}/characters").json()
    assert listed[0]["figure_count"] == 0


def test_candidate_image_is_stored_locally(
    client: TestClient, fake_comfy: dict[str, list]
) -> None:
    """一度取得した画像はアプリ管理領域へ控え、以後ComfyUI無しでも表示できる。"""
    _project_id, character_id = _character(client)
    started = client.post(
        f"/api/v1/characters/{character_id}/candidates/generate",
        json={"count": 8, "seed_start": 1},
    )
    assert _finish_job(client, started.json()["job"]["id"])["state"] == "completed"
    candidate = client.get(f"/api/v1/characters/{character_id}/candidates").json()[0]

    first = client.get(f"/api/v1/character-candidates/{candidate['id']}/image")
    assert first.status_code == 200
    assert first.content == _TINY_PNG

    async def unreachable(_self, _image):
        raise AssertionError("控えがあるのにComfyUIへ問い合わせている")

    main.ComfyClient.view_image = unreachable
    again = client.get(f"/api/v1/character-candidates/{candidate['id']}/image")
    assert again.status_code == 200
    assert again.content == _TINY_PNG
