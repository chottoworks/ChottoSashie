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

    async def submit(_self, workflow):
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


def _text_still_image(
    client: TestClient, project_id: str, name: str, character_ids: list[str]
) -> str:
    """指示文で描き起こす静止画。合成には背景が要るので、背景も付ける。"""
    return client.post(
        f"/api/v1/projects/{project_id}/still-images",
        json={
            "name": name,
            "background_asset_id": _background_with_image(client, project_id),
            "character_ids": character_ids,
        },
    ).json()["id"]


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
    still_image = client.post(
        f"/api/v1/projects/{project_id}/still-images",
        json={
            "name": "登校",
            "background_asset_id": background_id,
            "character_ids": [character_id],
        },
    ).json()
    # 作り方を選ばずに作った静止画は、指示文で合成する。
    assert still_image["composition_method"] == "text"
    still_image_id = still_image["id"]

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
    # ComfyUI へ渡した指示文を、画像と一緒に記録して画面で参照できるようにする。
    assert still_image["source_prompt"] == workflow["20"]["inputs"]["prompt"]

    image_file = client.get(f"/api/v1/still-images/{still_image_id}/image")
    assert image_file.status_code == 200
    assert image_file.content == b"KEYFRAME-BYTES"


def test_compose_without_a_figure_is_rejected(client: TestClient, fake_comfy: dict) -> None:
    """立ち絵が1枚も無い人物は合成できない。理由は押す前に画面へ出す。"""
    project_id = _project(client)
    character_id = client.post(
        f"/api/v1/projects/{project_id}/characters", json={"name": "立ち絵なし"}
    ).json()["id"]
    still_image_id = _text_still_image(client, project_id, "未完成", [character_id])

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
    still_image_id = _text_still_image(client, project_id, "一枚目", [character_id])
    second_still_image = _text_still_image(client, project_id, "二枚目", [character_id])

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
    still_image_id = _text_still_image(client, project_id, "対決", [character_id])

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
    still_image_id = _text_still_image(client, project_id, "丁寧", [character_id])

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
    still_image_id = _text_still_image(client, project_id, "冬の朝", [character_id])

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

    composed = client.post(
        f"/api/v1/still-images/{still_image_id}/compose",
        json={
            "base_url": "http://127.0.0.1:8190",
            "seed": 11,
            "character_specs": {
                first: {"pose": "椅子に座って右手をあげる", "expression": "笑顔"},
            },
        },
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


def _two_person_still_image(client: TestClient) -> tuple[str, str, str]:
    project_id = _project(client)
    first = _character_with_image(client, project_id, name="花子")
    second = _character_with_image(client, project_id, name="しおり")
    still_image_id = _text_still_image(client, project_id, "通り", [first, second])
    return still_image_id, first, second


def _saved_specs(client: TestClient, still_image_id: str) -> dict[str, dict]:
    listed = client.get(f"/api/v1/still-images/{still_image_id}/characters").json()
    return {item["character_id"]: item["spec"] for item in listed}


def test_compose_saves_the_specs_on_screen_and_uses_them(
    client: TestClient, fake_comfy: dict
) -> None:
    """合成するときに画面の指定を保存し、その内容で合成する。画面と合成が食い違わない。"""
    still_image_id, first, second = _two_person_still_image(client)
    client.post(
        f"/api/v1/still-images/{still_image_id}/compose",
        json={
            "base_url": "http://127.0.0.1:8190",
            "seed": 1,
            "character_specs": {
                first: {"direction": "右向き"},
                second: {"direction": "左向き"},
            },
        },
    )

    # 画面で書き換えた内容を、次の合成で送る。前に保存した内容ではなく、これが使われる。
    composed = client.post(
        f"/api/v1/still-images/{still_image_id}/compose",
        json={
            "base_url": "http://127.0.0.1:8190",
            "seed": 2,
            "character_specs": {
                first: {"direction": "右手前向き", "placement": "左側の道"},
                second: {"direction": "左手前向き", "placement": "右側の道"},
            },
        },
    )
    assert composed.status_code == 200

    prompt = fake_comfy["workflow"]["20"]["inputs"]["prompt"]
    assert "the first person in image 1: 体の向きは右手前向き。" in prompt
    assert "the second person in image 2: 体の向きは左手前向き。" in prompt
    assert "右向き。" not in prompt

    saved = _saved_specs(client, still_image_id)
    assert saved[first]["direction"] == "右手前向き"
    assert saved[first]["placement"] == "左側の道"
    assert saved[second]["direction"] == "左手前向き"


def test_compose_without_specs_keeps_the_saved_ones(client: TestClient, fake_comfy: dict) -> None:
    """指定を送らなかった人物は、保存済みの指定のまま合成する。"""
    still_image_id, first, second = _two_person_still_image(client)
    client.post(
        f"/api/v1/still-images/{still_image_id}/compose",
        json={
            "base_url": "http://127.0.0.1:8190",
            "character_specs": {first: {"expression": "笑顔"}, second: {"expression": "真顔"}},
        },
    )

    composed = client.post(
        f"/api/v1/still-images/{still_image_id}/compose",
        json={"base_url": "http://127.0.0.1:8190", "character_specs": {first: {}}},
    )
    assert composed.status_code == 200

    saved = _saved_specs(client, still_image_id)
    assert saved[first]["expression"] == ""
    assert saved[second]["expression"] == "真顔"
    assert "the second person in image 2: 表情は真顔。" in fake_comfy["workflow"]["20"]["inputs"]["prompt"]


def test_compose_rejects_a_person_not_in_the_still_image(
    client: TestClient, fake_comfy: dict
) -> None:
    """静止画に出ていない人物の指定は受け付けず、何も保存しない。"""
    still_image_id, first, _second = _two_person_still_image(client)

    response = client.post(
        f"/api/v1/still-images/{still_image_id}/compose",
        json={
            "base_url": "http://127.0.0.1:8190",
            "character_specs": {first: {"pose": "走る"}, "not-in-this-image": {"pose": "座る"}},
        },
    )

    assert response.status_code == 404
    assert _saved_specs(client, still_image_id)[first]["pose"] == ""
    assert fake_comfy["workflow"] is None


def test_rejected_compose_does_not_save_the_specs(client: TestClient, fake_comfy: dict) -> None:
    """合成を受け付けられないときは、指定も保存しない（合成しなければ保存しない）。"""
    project_id = _project(client)
    character_id = client.post(
        f"/api/v1/projects/{project_id}/characters", json={"name": "立ち絵なし"}
    ).json()["id"]
    still_image_id = _text_still_image(client, project_id, "未完成", [character_id])

    response = client.post(
        f"/api/v1/still-images/{still_image_id}/compose",
        json={
            "base_url": "http://127.0.0.1:8190",
            "character_specs": {character_id: {"pose": "走る"}},
        },
    )

    assert response.status_code == 409
    assert _saved_specs(client, still_image_id)[character_id]["pose"] == ""


def test_compose_without_a_comfyui_address_does_not_save_the_specs(
    client: TestClient, fake_comfy: dict
) -> None:
    """ComfyUIの接続先が決まっておらず合成できないときも、指定は保存しない。"""
    still_image_id, first, _second = _two_person_still_image(client)

    response = client.post(
        f"/api/v1/still-images/{still_image_id}/compose",
        json={"character_specs": {first: {"pose": "走る"}}},
    )

    assert response.status_code == 409
    assert _saved_specs(client, still_image_id)[first]["pose"] == ""


def test_choosing_a_figure_keeps_the_saved_spec(client: TestClient, fake_comfy: dict) -> None:
    """立ち絵の選び直しだけを送っても、保存済みの指定は消えない。"""
    project_id = _project(client)
    character_id = _character_with_image(client, project_id)
    coat = _add_figure(client, character_id, label="コート")
    still_image_id = _text_still_image(client, project_id, "冬の朝", [character_id])
    client.post(
        f"/api/v1/still-images/{still_image_id}/compose",
        json={
            "base_url": "http://127.0.0.1:8190",
            "character_specs": {character_id: {"pose": "手を振る"}},
        },
    )

    chosen = client.put(
        f"/api/v1/still-images/{still_image_id}/characters/{character_id}",
        json={"figure_id": coat["id"]},
    )

    assert chosen.status_code == 200
    assert chosen.json()["figure_id"] == coat["id"]
    assert chosen.json()["spec"]["pose"] == "手を振る"


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
