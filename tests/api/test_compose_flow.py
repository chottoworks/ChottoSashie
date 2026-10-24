import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chotto_sashie_shared.jobs import JobRepository
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
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture
def fake_comfy(monkeypatch: pytest.MonkeyPatch) -> dict:
    captured: dict = {"uploads": [], "workflow": None}

    async def upload_image(_self, data, filename, content_type="image/png", *, subfolder="", overwrite=True):
        captured["uploads"].append({"filename": filename, "content_type": content_type, "size": len(data)})
        return {"name": filename, "subfolder": "", "type": "input"}

    async def submit(_self, workflow, _client_id):
        captured["workflow"] = workflow
        return "compose-1"

    async def history(_self, prompt_id):
        return {
            "status": {"completed": True, "status_str": "success"},
            "outputs": {
                "40": {"images": [{"filename": f"{prompt_id}.png", "subfolder": "still_images", "type": "output"}]}
            },
        }

    async def view_image(_self, image):
        return b"KEYFRAME-BYTES", "image/png"

    monkeypatch.setattr(main.ComfyClient, "upload_image", upload_image)
    monkeypatch.setattr(main.ComfyClient, "submit", submit)
    monkeypatch.setattr(main.ComfyClient, "history", history)
    monkeypatch.setattr(main.ComfyClient, "view_image", view_image)
    return captured


def _project(client: TestClient) -> str:
    return client.post("/api/v1/projects", json={"name": "合成テスト"}).json()["id"]


def _character_with_image(client: TestClient, project_id: str, name: str = "ミナ") -> str:
    """立ち絵を1枚持つ人物。合成が使うのはこの立ち絵。"""
    character_id = client.post(
        f"/api/v1/projects/{project_id}/characters", json={"name": name}
    ).json()["id"]
    _add_figure(client, character_id)
    return character_id


def _add_figure(client: TestClient, character_id: str, label: str = "") -> dict:
    figure = client.post(
        f"/api/v1/characters/{character_id}/figures/upload",
        files={"file": ("figure.png", _TINY_PNG, "image/png")},
    ).json()
    if label:
        figure = client.patch(
            f"/api/v1/character-figures/{figure['id']}", json={"label": label}
        ).json()
    return figure


def _background_with_image(client: TestClient, project_id: str) -> str:
    background_id = client.post(
        f"/api/v1/projects/{project_id}/background-assets", json={"name": "教室"}
    ).json()["id"]
    client.post(
        f"/api/v1/background-assets/{background_id}/image",
        files={"file": ("room.png", _TINY_PNG, "image/png")},
    )
    return background_id


def test_compose_produces_still_image(client: TestClient, fake_comfy: dict) -> None:
    project_id = _project(client)
    character_id = _character_with_image(client, project_id)
    background_id = _background_with_image(client, project_id)
    still_image_id = client.post(
        f"/api/v1/projects/{project_id}/still-images",
        json={
            "name": "登校",
            "background_asset_id": background_id,
            "character_ids": [character_id],
        },
    ).json()["id"]

    composed = client.post(
        f"/api/v1/still-images/{still_image_id}/compose",
        json={"base_url": "http://127.0.0.1:8190", "prompt": "窓辺で手を振る", "seed": 77},
    )
    assert composed.status_code == 200
    job_id = composed.json()["job_id"]

    job = client.get(f"/api/v1/jobs/{job_id}").json()
    assert job["state"] == "completed"

    # 人物→背景の順で2枚アップロードされ、3枚目スロットは枝刈りされている。
    assert len(fake_comfy["uploads"]) == 2
    workflow = fake_comfy["workflow"]
    assert "13" not in workflow
    assert workflow["30"]["inputs"]["seed"] == 77
    # 参照は `... in image N` の形で、順番どおりに書かれる。
    assert "the person in image 1 is in the background in image 2." in workflow["20"]["inputs"]["prompt"]

    still_image = client.get(f"/api/v1/still-images/{still_image_id}").json()
    assert still_image["has_image"] is True
    assert still_image["composition_job_id"] == job_id

    image_file = client.get(f"/api/v1/still-images/{still_image_id}/image")
    assert image_file.status_code == 200
    assert image_file.content == b"KEYFRAME-BYTES"


def test_compose_without_a_figure_is_rejected(client: TestClient, fake_comfy: dict) -> None:
    """立ち絵が1枚も無い人物は合成できない。理由は押す前に画面へ出す。"""
    project_id = _project(client)
    character_id = client.post(
        f"/api/v1/projects/{project_id}/characters", json={"name": "立ち絵なし"}
    ).json()["id"]
    still_image_id = client.post(
        f"/api/v1/projects/{project_id}/still-images",
        json={"name": "未完成", "character_ids": [character_id]},
    ).json()["id"]

    still_image = client.get(f"/api/v1/still-images/{still_image_id}").json()
    assert still_image["can_compose"] is False
    assert "立ち絵" in still_image["compose_blocked_reason"]

    response = client.post(
        f"/api/v1/still-images/{still_image_id}/compose",
        json={"base_url": "http://127.0.0.1:8190"},
    )
    assert response.status_code == 409


def test_compose_is_queued_when_gpu_busy(client: TestClient, fake_comfy: dict) -> None:
    project_id = _project(client)
    character_id = _character_with_image(client, project_id)
    still_image_id = client.post(
        f"/api/v1/projects/{project_id}/still-images",
        json={"name": "一枚目", "character_ids": [character_id]},
    ).json()["id"]
    second_still_image = client.post(
        f"/api/v1/projects/{project_id}/still-images",
        json={"name": "二枚目", "character_ids": [character_id]},
    ).json()["id"]

    # 実行中のGPUジョブを1件作っておく（合成ジョブが占有中の状態を模す）。
    busy = main.jobs.create(_busy_job())
    main.jobs.transition(busy.id, _to_preparing())

    response = client.post(
        f"/api/v1/still-images/{second_still_image}/compose",
        json={"base_url": "http://127.0.0.1:8190"},
    )
    # 断らず順番待ちへ入れる（前の処理が終わったら実行される）。
    assert response.status_code == 200
    queued = main.jobs.get(response.json()["job_id"])
    assert queued.kind == "composition.still_image"
    assert still_image_id  # 参照だけ


def _busy_job():
    from chotto_sashie_shared.jobs import JobCreate

    return JobCreate(kind="composition.still_image", payload={})


def _to_preparing():
    from chotto_sashie_shared.jobs import JobTransition

    return JobTransition(state="preparing")


def test_compose_prompt_carries_the_project_style(client: TestClient, fake_comfy: dict) -> None:
    """作品の画風が合成プロンプトへ届く（写実指定がイラスト調へ寄らないように）。"""
    project_id = client.post(
        "/api/v1/projects",
        json={"name": "写実", "illustration_style": "昭和の映画風実写写真。フォトリアリスティック。"},
    ).json()["id"]
    character_id = _character_with_image(client, project_id)
    still_image_id = client.post(
        f"/api/v1/projects/{project_id}/still-images",
        json={"name": "対決", "character_ids": [character_id]},
    ).json()["id"]

    composed = client.post(
        f"/api/v1/still-images/{still_image_id}/compose",
        json={"base_url": "http://127.0.0.1:8190", "seed": 5},
    )
    assert composed.status_code == 200

    prompt = fake_comfy["workflow"]["20"]["inputs"]["prompt"]
    assert "作品全体の画風: 昭和の映画風実写写真。フォトリアリスティック。" in prompt
    assert "illustration that merges" not in prompt


def test_compose_accepts_the_careful_quality(client: TestClient, fake_comfy: dict) -> None:
    """画面から「丁寧に合成」を選べる（高速化LoRA無し・ステップ増）。"""
    project_id = _project(client)
    character_id = _character_with_image(client, project_id)
    still_image_id = client.post(
        f"/api/v1/projects/{project_id}/still-images",
        json={"name": "丁寧", "character_ids": [character_id]},
    ).json()["id"]

    composed = client.post(
        f"/api/v1/still-images/{still_image_id}/compose",
        json={"base_url": "http://127.0.0.1:8190", "seed": 3, "quality": "careful"},
    )
    assert composed.status_code == 200

    workflow = fake_comfy["workflow"]
    assert "3" not in workflow
    assert workflow["30"]["inputs"]["steps"] > 4


def test_compose_uses_the_chosen_figure(client: TestClient, fake_comfy: dict) -> None:
    """静止画ごとに、どの立ち絵を使うかを選べる。"""
    project_id = _project(client)
    character_id = _character_with_image(client, project_id)
    coat = _add_figure(client, character_id, label="コート")
    still_image_id = client.post(
        f"/api/v1/projects/{project_id}/still-images",
        json={"name": "冬の朝", "character_ids": [character_id]},
    ).json()["id"]

    # 何も選んでいないうちは既定の立ち絵（先に作った1枚）。
    listed = client.get(f"/api/v1/still-images/{still_image_id}/characters").json()
    assert len(listed) == 1
    assert listed[0]["figure_is_default"] is True
    assert listed[0]["figure_id"] != coat["id"]

    chosen = client.put(
        f"/api/v1/still-images/{still_image_id}/characters/{character_id}",
        json={"figure_id": coat["id"], "spec": {}},
    )
    assert chosen.status_code == 200
    assert chosen.json()["figure_id"] == coat["id"]
    assert chosen.json()["figure_is_default"] is False
    assert chosen.json()["figure_label"] == "コート"

    # 空に戻すと既定へ戻る（選び直しを取り消せる）。
    cleared = client.put(
        f"/api/v1/still-images/{still_image_id}/characters/{character_id}",
        json={"figure_id": None, "spec": {}},
    )
    assert cleared.json()["figure_is_default"] is True


def test_compose_prompt_carries_the_pose_only_for_who_has_one(
    client: TestClient, fake_comfy: dict
) -> None:
    """姿勢を書いた人だけ変え、書いていない人は立ち絵のままにする。"""
    project_id = _project(client)
    first = _character_with_image(client, project_id, name="花子")
    second = _character_with_image(client, project_id, name="しおり")
    background_id = _background_with_image(client, project_id)
    still_image_id = client.post(
        f"/api/v1/projects/{project_id}/still-images",
        json={
            "name": "教室",
            "background_asset_id": background_id,
            "character_ids": [first, second],
        },
    ).json()["id"]

    client.put(
        f"/api/v1/still-images/{still_image_id}/characters/{first}",
        json={"spec": {"pose": "椅子に座って右手をあげる", "expression": "笑顔"}},
    )

    composed = client.post(
        f"/api/v1/still-images/{still_image_id}/compose",
        json={"base_url": "http://127.0.0.1:8190", "seed": 11},
    )
    assert composed.status_code == 200

    prompt = fake_comfy["workflow"]["20"]["inputs"]["prompt"]
    # 指定は日本語のまま、その人物の image 番号へ結び付けて入る。
    assert "the first person in image 1: ポーズは椅子に座って右手をあげる。表情は笑顔。" in prompt
    # 衣装は立ち絵のまま保ち、変えるのは姿勢・向き・表情だけ。
    assert "outfit and accessories exactly as in their own image." in prompt
    assert (
        "Change the pose, gesture, body orientation and facial expression of "
        "the first person in image 1" in prompt
    )
    # 書いていない人へは、変える指示を出さない。
    assert (
        "the second person in image 2 keeps the pose, gesture, body orientation and "
        "facial expression from their own image." in prompt
    )


def test_default_figure_is_stable_when_created_at_the_same_moment(client: TestClient) -> None:
    """続けて登録して作った時刻が同じ値になっても、先に登録した1枚が既定になる。"""
    project_id = _project(client)
    character_id = _character_with_image(client, project_id)
    second = _add_figure(client, character_id, label="二枚目")
    with main.database.connect() as connection:
        connection.execute(
            "UPDATE character_figures SET created_at = '2026-10-20T00:00:00+00:00' "
            "WHERE character_id = ?",
            (character_id,),
        )

    figures = client.get(f"/api/v1/characters/{character_id}/figures").json()
    default = [figure for figure in figures if figure["is_default"]]

    assert len(default) == 1
    assert default[0]["id"] != second["id"]
