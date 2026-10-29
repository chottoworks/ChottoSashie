"""置いて合成する: 人物ごとに姿を作り、背景の上へ置いた下書きをなじませる。"""

import io
import json
import time
from pathlib import Path, PurePosixPath

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from chotto_sashie_shared.jobs import JobCreate, JobRepository, JobTransition
from chotto_sashie_api import main
from chotto_sashie_api.database import Database
from chotto_sashie_api.placed_composition import (
    PlacedFigure,
    PlacedSources,
    blend_mask,
    build_blend_prompt,
    build_blend_workflow,
    build_pose_prompt,
    build_pose_workflow,
    compose_draft,
    cut_out,
    finish_blend,
)
from chotto_sashie_api.schemas import StillImageCharacterLayout, StillImageCharacterSpec

_BASE_URL = "http://127.0.0.1:8190"


def _png(size: tuple[int, int], color, mode: str = "RGB") -> bytes:  # noqa: ANN001
    output = io.BytesIO()
    Image.new(mode, size, color).save(output, format="PNG")
    return output.getvalue()


def _removal_template() -> dict:
    return json.loads(Path("workflows/composition/background_removal.json").read_text(encoding="utf-8"))


# --- 指示文 ---


def test_pose_prompt_is_empty_without_a_spec() -> None:
    """指定が空なら編集はせず、立ち絵を切り抜くだけにする。"""
    assert build_pose_prompt(StillImageCharacterSpec()) == ""


def test_pose_prompt_carries_the_spec_and_keeps_the_figure_rules() -> None:
    prompt = build_pose_prompt(
        StillImageCharacterSpec(direction="右向き", placement="左側に小さく"), "淡い水彩"
    )

    assert prompt.startswith("体の向きは右向き。")
    # 配置は姿ではなく位置で決めるので、姿を作る文には入れない。
    assert "左側に小さく" not in prompt
    # 次に背景を抜けるよう、素背景・全身・1人だけのまま作らせる。
    assert "Plain light-gray background" in prompt
    assert "Show the whole body" in prompt
    assert "Show the person from image 1 only once." in prompt
    assert prompt.endswith("作品全体の画風: 淡い水彩")


def test_blend_prompt_keeps_what_was_placed() -> None:
    """なじませる文は、置いた位置・大きさ・向きを保たせ、状況説明は持たない。"""
    prompt = build_blend_prompt(2)

    assert prompt.startswith("Edit image 1.")
    assert "Each person keeps standing on the same spot, at the same size" in prompt
    # 描き直すのは人物のまわりだけなので、影は言わないと描かれない。
    assert "Each person casts a soft shadow on the ground at their feet" in prompt
    assert "Show each of them only once." in prompt
    assert "do not remove, move, redraw or replace anything" in prompt
    assert "The person keeps" in build_blend_prompt(1)


# --- ワークフロー ---


def test_pose_workflow_without_an_edit_cuts_out_the_figure() -> None:
    workflow = build_pose_workflow(
        _removal_template(),
        edit_workflow=None,
        figure_image="figure.png",
        removal_prefix="ChottoSashie/still_images/poses/job-mask",
    )

    assert workflow["101"]["inputs"]["image"] == "figure.png"
    assert workflow["103"]["inputs"]["image"] == ["101", 0]
    assert workflow["102"]["inputs"]["bg_removal_name"] == "birefnet.safetensors"
    assert workflow["105"]["inputs"]["filename_prefix"] == "ChottoSashie/still_images/poses/job-mask"


def test_pose_workflow_cuts_out_the_edited_image() -> None:
    """編集と背景除去は1件のプロンプトにつなぎ、編集した画像から抜く。"""
    edit = {"31": {"class_type": "VAEDecode", "inputs": {}}, "40": {"class_type": "SaveImage", "inputs": {}}}

    workflow = build_pose_workflow(
        _removal_template(),
        edit_workflow=edit,
        figure_image="figure.png",
        removal_prefix="mask",
    )

    assert "101" not in workflow
    assert workflow["103"]["inputs"]["image"] == ["31", 0]
    assert {"31", "40", "102", "103", "104", "105"} <= set(workflow)


# --- 切り抜きと下書き ---


def test_cut_out_keeps_only_the_person_and_trims_to_them() -> None:
    mask = Image.new("L", (40, 60), 0)
    mask.paste(255, (10, 20, 30, 50))
    mask_bytes = io.BytesIO()
    mask.save(mask_bytes, format="PNG")

    pose = Image.open(io.BytesIO(cut_out(_png((40, 60), (200, 10, 10)), mask_bytes.getvalue())))

    assert pose.mode == "RGBA"
    assert pose.size == (20, 30)


def test_draft_places_people_where_they_were_put(tmp_path: Path) -> None:
    """位置は割合で決まり、足もとの低い（手前の）人物が上に重なる。"""
    background = tmp_path / "background.png"
    background.write_bytes(_png((400, 100), (0, 0, 255)))
    far = tmp_path / "far.png"
    far.write_bytes(_png((10, 20), (255, 0, 0, 255), "RGBA"))
    near = tmp_path / "near.png"
    near.write_bytes(_png((10, 20), (0, 255, 0, 255), "RGBA"))
    sources = PlacedSources(
        background_path=background,
        figures=[
            PlacedFigure("手前", near, StillImageCharacterLayout(x=0.5, y=0.9, height=0.5)),
            PlacedFigure("奥", far, StillImageCharacterLayout(x=0.5, y=0.8, height=0.5)),
        ],
    )

    draft = Image.open(io.BytesIO(compose_draft(sources, 200, 100))).convert("RGB")

    # 背景は縦横比を保って広げ、はみ出した分を中央で切る（青のまま）。
    assert draft.size == (200, 100)
    assert draft.getpixel((5, 5)) == (0, 0, 255)
    # 手前（足もと y=0.9）の緑が、奥（y=0.8）の赤の上に重なる。
    assert draft.getpixel((100, 70)) == (0, 255, 0)
    # 奥の人物は手前より上にはみ出した分だけ見える。
    assert draft.getpixel((100, 35)) == (255, 0, 0)


def _one_person(tmp_path: Path) -> PlacedSources:
    background = tmp_path / "background.png"
    background.write_bytes(_png((400, 200), (0, 0, 255)))
    person = tmp_path / "person.png"
    person.write_bytes(_png((10, 40), (255, 0, 0, 255), "RGBA"))
    return PlacedSources(
        background_path=background,
        figures=[PlacedFigure("人物", person, StillImageCharacterLayout(x=0.5, y=0.8, height=0.5))],
    )


def test_blend_mask_covers_the_person_and_their_feet_only(tmp_path: Path) -> None:
    mask = Image.open(io.BytesIO(blend_mask(_one_person(tmp_path), 400, 200))).convert("L")

    # 人物（中央、y=40〜160）と足もと（y=160付近）は描き直し、離れた背景は描き直さない。
    assert mask.getpixel((200, 100)) == 255
    assert mask.getpixel((200, 160)) > 200
    assert mask.getpixel((20, 20)) == 0
    assert mask.getpixel((380, 190)) == 0


def test_blend_workflow_paints_only_inside_the_mask() -> None:
    composition = {
        "5": {"class_type": "CFGNorm", "inputs": {}},
        "7": {"class_type": "VAELoader", "inputs": {}},
        "11": {"class_type": "LoadImage", "inputs": {"image": "draft.png"}},
        "28": {"class_type": "EmptySD3LatentImage", "inputs": {}},
        "30": {"class_type": "KSampler", "inputs": {"model": ["5", 0], "latent_image": ["28", 0]}},
    }

    workflow = build_blend_workflow(composition, "mask.png")

    # 空の下地ではなく、下書きを潜在へ写して範囲の中だけを描く。
    assert "28" not in workflow
    assert workflow["51"]["inputs"]["pixels"] == ["11", 0]
    assert workflow["52"]["inputs"] == {"samples": ["51", 0], "mask": ["50", 0]}
    assert workflow["50"]["inputs"]["image"] == "mask.png"
    assert workflow["53"]["inputs"]["model"] == ["5", 0]
    assert workflow["30"]["inputs"]["model"] == ["53", 0]
    assert workflow["30"]["inputs"]["latent_image"] == ["52", 0]
    # 元のワークフローは書き換えない。
    assert composition["30"]["inputs"]["latent_image"] == ["28", 0]


def test_finish_blend_keeps_the_background_pixels(tmp_path: Path) -> None:
    sources = _one_person(tmp_path)
    draft = compose_draft(sources, 400, 200)
    mask = blend_mask(sources, 400, 200)
    # 描き直した1枚は全体が別の色になって返ってくる（大きさも違うことがある）。
    generated = _png((200, 100), (0, 255, 0))

    finished = Image.open(io.BytesIO(finish_blend(generated, draft, mask))).convert("RGB")

    assert finished.size == (400, 200)
    # 範囲の外は下書き（元の背景）の画素のまま、範囲の中は描き直したもの。
    assert finished.getpixel((20, 20)) == (0, 0, 255)
    assert finished.getpixel((200, 100)) == (0, 255, 0)


# --- API ---


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    database = Database(tmp_path / "video-api.sqlite3")
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(main, "jobs", JobRepository(database))
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture
def fake_comfy(monkeypatch: pytest.MonkeyPatch) -> dict:
    """送られたワークフローの保存ノードの名前で、出来上がった画像を返すComfyUI。"""
    captured: dict = {"uploads": [], "workflows": []}

    async def upload_image(_self, data, filename, content_type="image/png", *, subfolder="", overwrite=True):
        captured["uploads"].append({"filename": filename, "data": data})
        return {"name": filename, "subfolder": "", "type": "input"}

    async def submit(_self, workflow, _client_id):
        captured["workflows"].append(workflow)
        return f"prompt-{len(captured['workflows'])}"

    async def history(_self, prompt_id):
        workflow = captured["workflows"][int(prompt_id.removeprefix("prompt-")) - 1]
        outputs = {
            node_id: {
                "images": [
                    {
                        "filename": f"{PurePosixPath(node['inputs']['filename_prefix']).name}_00001_.png",
                        "subfolder": "out",
                        "type": "output",
                    }
                ]
            }
            for node_id, node in workflow.items()
            if node.get("class_type") == "SaveImage"
        }
        return {"status": {"completed": True, "status_str": "success"}, "outputs": outputs}

    async def view_image(_self, image):
        if "-mask" in image["filename"]:
            mask = Image.new("L", (16, 32), 0)
            mask.paste(255, (4, 4, 12, 30))
            output = io.BytesIO()
            mask.save(output, format="PNG")
            return output.getvalue(), "image/png"
        return _png((16, 32), (180, 120, 60)), "image/png"

    monkeypatch.setattr(main.ComfyClient, "upload_image", upload_image)
    monkeypatch.setattr(main.ComfyClient, "submit", submit)
    monkeypatch.setattr(main.ComfyClient, "history", history)
    monkeypatch.setattr(main.ComfyClient, "view_image", view_image)
    return captured


def _figure(client: TestClient, character_id: str) -> dict:
    return client.post(
        f"/api/v1/characters/{character_id}/figures/upload",
        files={"file": ("figure.png", _png((16, 32), (90, 90, 90)), "image/png")},
    ).json()


def _placed_still_image(client: TestClient, *, background: bool = True) -> tuple[str, str]:
    project_id = client.post("/api/v1/projects", json={"name": "置く"}).json()["id"]
    character_id = client.post(
        f"/api/v1/projects/{project_id}/characters", json={"name": "ミナ"}
    ).json()["id"]
    _figure(client, character_id)
    body: dict = {"name": "通り", "character_ids": [character_id], "composition_method": "placed"}
    if background:
        background_id = client.post(
            f"/api/v1/projects/{project_id}/background-assets", json={"name": "通り"}
        ).json()["id"]
        client.post(
            f"/api/v1/background-assets/{background_id}/image",
            files={"file": ("street.png", _png((64, 36), (20, 30, 90)), "image/png")},
        )
        body["background_asset_id"] = background_id
    still_image = client.post(f"/api/v1/projects/{project_id}/still-images", json=body).json()
    assert still_image["composition_method"] == "placed"
    return still_image["id"], character_id


def _character(client: TestClient, still_image_id: str) -> dict:
    return client.get(f"/api/v1/still-images/{still_image_id}/characters").json()[0]


def _finished(client: TestClient, job_id: str) -> dict:
    """ジョブが終わるまで待つ。画像の加工は別スレッドで行うので、応答の時点では終わっていない。"""
    for _ in range(200):
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["state"] in ("completed", "failed", "cancelled"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"ジョブが終わりませんでした: {job}")


def _make_pose(client: TestClient, still_image_id: str, character_id: str, spec: dict) -> dict:
    response = client.post(
        f"/api/v1/still-images/{still_image_id}/characters/{character_id}/pose",
        json={"base_url": _BASE_URL, "spec": spec, "seed": 4},
    )
    assert response.status_code == 200, response.text
    job = _finished(client, response.json()["job_id"])
    assert job["state"] == "completed", job
    return _character(client, still_image_id)


def test_placed_still_image_needs_a_pose_before_composing(client: TestClient, fake_comfy: dict) -> None:
    still_image_id, _character_id = _placed_still_image(client)

    still_image = client.get(f"/api/v1/still-images/{still_image_id}").json()
    assert still_image["can_compose"] is False
    assert "姿がまだありません" in still_image["compose_blocked_reason"]
    # まだ置いていない人物は、既定の並びに出す。
    assert _character(client, still_image_id)["layout"] == {"x": 0.5, "y": 0.9, "height": 0.42}


def test_placed_still_image_needs_a_background(client: TestClient, fake_comfy: dict) -> None:
    still_image_id, _character_id = _placed_still_image(client, background=False)

    still_image = client.get(f"/api/v1/still-images/{still_image_id}").json()
    assert still_image["can_compose"] is False
    assert "背景" in still_image["compose_blocked_reason"]


def test_pose_without_a_spec_only_cuts_out_the_figure(client: TestClient, fake_comfy: dict) -> None:
    still_image_id, character_id = _placed_still_image(client)

    character = _make_pose(client, still_image_id, character_id, {})

    workflow = fake_comfy["workflows"][-1]
    assert {node["class_type"] for node in workflow.values()} == {
        "LoadImage", "LoadBackgroundRemovalModel", "RemoveBackground", "MaskToImage", "SaveImage",
    }
    assert character["pose_url"].endswith("?v=1")
    assert character["pose_outdated"] is False
    # 編集しなかったので、渡した指示文は無い（記録が無いのとは分ける）。
    assert character["pose_prompt"] == ""
    pose = client.get(character["pose_url"])
    assert pose.status_code == 200
    # マスクの人物の範囲だけを残した透過PNG。
    assert Image.open(io.BytesIO(pose.content)).size == (8, 26)
    assert client.get(f"/api/v1/still-images/{still_image_id}").json()["can_compose"] is True


def test_pose_with_a_spec_edits_then_cuts_out_and_saves_the_spec(
    client: TestClient, fake_comfy: dict
) -> None:
    still_image_id, character_id = _placed_still_image(client)
    # 指示文で描き起こしたときの配置の文は、姿を作っても消さない。
    client.patch(f"/api/v1/still-images/{still_image_id}", json={"composition_method": "text"})
    client.post(
        f"/api/v1/still-images/{still_image_id}/compose",
        json={"base_url": _BASE_URL, "character_specs": {character_id: {"placement": "右寄り"}}},
    )
    client.patch(f"/api/v1/still-images/{still_image_id}", json={"composition_method": "placed"})

    character = _make_pose(client, still_image_id, character_id, {"direction": "右向き", "placement": "無視"})

    workflow = fake_comfy["workflows"][-1]
    assert workflow["103"]["inputs"]["image"] == ["31", 0]
    assert workflow["20"]["inputs"]["prompt"].startswith("体の向きは右向き。")
    # 姿は丁寧な設定で作る。
    assert workflow["30"]["inputs"]["steps"] > 4
    assert character["spec"]["direction"] == "右向き"
    assert character["spec"]["placement"] == "右寄り"
    assert character["pose_spec"]["direction"] == "右向き"
    assert character["pose_prompt"] == workflow["20"]["inputs"]["prompt"]


def test_compose_places_the_poses_and_blends_one_image(client: TestClient, fake_comfy: dict) -> None:
    still_image_id, character_id = _placed_still_image(client)
    _make_pose(client, still_image_id, character_id, {})

    response = client.post(
        f"/api/v1/still-images/{still_image_id}/compose",
        json={
            "base_url": _BASE_URL,
            "seed": 9,
            "layouts": {character_id: {"x": 0.3, "y": 0.8, "height": 0.5}},
        },
    )

    assert response.status_code == 200, response.text
    assert _finished(client, response.json()["job_id"])["state"] == "completed"
    workflow = fake_comfy["workflows"][-1]
    # 渡すのは下書き1枚だけで、描き直すのは範囲（マスク）の中だけ。
    assert "12" not in workflow and "13" not in workflow
    assert workflow["20"]["inputs"]["prompt"].startswith("Edit image 1.")
    assert workflow["30"]["inputs"]["latent_image"] == ["52", 0]
    draft, mask = fake_comfy["uploads"][-2:]
    assert draft["filename"] == f"{still_image_id}-draft.png"
    assert mask["filename"] == f"{still_image_id}-mask.png"
    assert workflow["50"]["inputs"]["image"] == mask["filename"]
    assert Image.open(io.BytesIO(draft["data"])).size == (1280, 720)
    # 出来上がりは、範囲の外が下書きの画素のまま（描き直した小さな1枚を広げて重ねる）。
    still = Image.open(io.BytesIO(client.get(f"/api/v1/still-images/{still_image_id}/image").content))
    assert still.size == (1280, 720)
    assert still.convert("RGB").getpixel((5, 5)) == Image.open(io.BytesIO(draft["data"])).convert("RGB").getpixel((5, 5))
    # 置いた位置は合成と一緒に保存する。
    assert _character(client, still_image_id)["layout"] == {"x": 0.3, "y": 0.8, "height": 0.5}
    composed = client.get(f"/api/v1/still-images/{still_image_id}").json()
    assert composed["has_image"] is True
    assert composed["source_prompt"] == workflow["20"]["inputs"]["prompt"]


def test_rejected_compose_does_not_save_the_layout(client: TestClient, fake_comfy: dict) -> None:
    still_image_id, character_id = _placed_still_image(client)

    response = client.post(
        f"/api/v1/still-images/{still_image_id}/compose",
        json={"base_url": _BASE_URL, "layouts": {character_id: {"x": 0.3, "y": 0.8, "height": 0.5}}},
    )

    assert response.status_code == 409
    assert _character(client, still_image_id)["layout"]["x"] == 0.5


def test_choosing_another_figure_asks_for_a_new_pose(client: TestClient, fake_comfy: dict) -> None:
    still_image_id, character_id = _placed_still_image(client)
    _make_pose(client, still_image_id, character_id, {})
    coat = _figure(client, character_id)

    client.put(
        f"/api/v1/still-images/{still_image_id}/characters/{character_id}",
        json={"figure_id": coat["id"]},
    )

    assert _character(client, still_image_id)["pose_outdated"] is True
    still_image = client.get(f"/api/v1/still-images/{still_image_id}").json()
    assert still_image["can_compose"] is False
    assert "作り直して" in still_image["compose_blocked_reason"]


def test_pose_is_not_started_twice(client: TestClient, fake_comfy: dict) -> None:
    """姿を作っている間は、同じ人物の姿づくりも合成も受け付けない。"""
    still_image_id, character_id = _placed_still_image(client)
    _make_pose(client, still_image_id, character_id, {})
    working = main.jobs.create(JobCreate(kind="composition.still_image_pose", payload={}))
    main.jobs.transition(working.id, JobTransition(state="preparing"))
    main.StillImageCharacterRepository(main.database).set_pose_job(
        still_image_id, character_id, working.id
    )

    response = client.post(
        f"/api/v1/still-images/{still_image_id}/characters/{character_id}/pose",
        json={"base_url": _BASE_URL, "spec": {}},
    )

    assert response.status_code == 409
    still_image = client.get(f"/api/v1/still-images/{still_image_id}").json()
    assert "作っています" in still_image["compose_blocked_reason"]


def test_deleting_the_still_image_removes_the_poses(client: TestClient, fake_comfy: dict) -> None:
    still_image_id, character_id = _placed_still_image(client)
    _make_pose(client, still_image_id, character_id, {})
    record = main.StillImageCharacterRepository(main.database).get(still_image_id, character_id)
    pose_path = Path(record.pose_image_path)
    assert pose_path.is_file()

    assert client.delete(f"/api/v1/still-images/{still_image_id}").status_code == 204

    assert not pose_path.exists()
