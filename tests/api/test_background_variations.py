"""背景のバリエーション（小物を置く／時間帯を変える）とグループ化。

時間帯は**背景1枚だけを渡す編集でしか変えられない**（合成の状況説明では変えられない、
という実測がある）。作り方を分けているのはそのためで、ここではその分け方が守られて
いることと、派生が同じグループへ入ることを確かめる。
"""

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
    monkeypatch.setattr(main.settings, "data_dir", tmp_path / "app-data")
    with TestClient(main.app) as test_client:
        test_client.put(
            "/api/v1/setup/config",
            json={"mode": "existing_comfyui", "base_url": "http://127.0.0.1:8190"},
        )
        yield test_client


@pytest.fixture
def fake_comfy(monkeypatch: pytest.MonkeyPatch) -> dict:
    captured: dict = {"workflows": [], "uploads": []}

    async def object_info(_self):
        return {
            "UNETLoader": {
                "input": {"required": {"unet_name": [["qwen_image_edit_2511_fp8mixed.safetensors"]]}}
            }
        }

    async def upload_image(
        _self, data, filename, content_type="image/png", *, subfolder="", overwrite=True
    ):
        captured["uploads"].append(filename)
        return {"name": filename, "subfolder": "", "type": "input"}

    async def submit(_self, workflow, _client_id):
        captured["workflows"].append(workflow)
        return f"prompt-{len(captured['workflows'])}"

    async def history(_self, prompt_id):
        return {
            "status": {"completed": True, "status_str": "success"},
            "outputs": {"40": {"images": [{"filename": f"{prompt_id}.png", "subfolder": "", "type": "output"}]}},
        }

    async def view_image(_self, _image):
        return _TINY_PNG, "image/png"

    monkeypatch.setattr(main.ComfyClient, "object_info", object_info)
    monkeypatch.setattr(main.ComfyClient, "upload_image", upload_image)
    monkeypatch.setattr(main.ComfyClient, "submit", submit)
    monkeypatch.setattr(main.ComfyClient, "history", history)
    monkeypatch.setattr(main.ComfyClient, "view_image", view_image)
    return captured


def _finish_job(client: TestClient, job_id: str) -> dict:
    for _ in range(200):
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["state"] in {"completed", "failed", "cancelled", "interrupted"}:
            return job
    raise AssertionError(f"ジョブが終わりませんでした: {job_id}")


def _project(client: TestClient) -> str:
    return client.post("/api/v1/projects", json={"name": "作品"}).json()["id"]


def _background(client: TestClient, project_id: str, name: str = "教室") -> dict:
    asset = client.post(
        f"/api/v1/projects/{project_id}/background-assets", json={"name": name}
    ).json()
    client.post(
        f"/api/v1/background-assets/{asset['id']}/image",
        files={"file": ("bg.png", _TINY_PNG, "image/png")},
    )
    return asset


def _prop(client: TestClient, project_id: str, name: str = "本") -> str:
    asset = client.post(
        f"/api/v1/projects/{project_id}/prop-assets", json={"name": name}
    ).json()
    client.post(
        f"/api/v1/prop-assets/{asset['id']}/image",
        files={"file": ("prop.png", _TINY_PNG, "image/png")},
    )
    return asset["id"]


def test_time_variation_refuses_props(client: TestClient) -> None:
    """時間帯を変える回に小物を混ぜさせない（混ぜると時間帯が変わらない）。"""
    project_id = _project(client)
    background = _background(client, project_id)
    prop_id = _prop(client, project_id)

    response = client.post(
        f"/api/v1/background-assets/{background['id']}/variations",
        json={"kind": "time", "instruction": "夕方にする", "prop_asset_ids": [prop_id]},
    )
    assert response.status_code == 422


def test_time_variation_sends_only_the_background(
    client: TestClient, fake_comfy: dict
) -> None:
    project_id = _project(client)
    background = _background(client, project_id)

    response = client.post(
        f"/api/v1/background-assets/{background['id']}/variations",
        json={"kind": "time", "instruction": "夕方にする", "variant_label": "夕方"},
    )
    assert response.status_code == 200, response.text
    assert _finish_job(client, response.json()["job_id"])["state"] == "completed"

    # 渡した画像は元の背景1枚だけ。
    assert len(fake_comfy["uploads"]) == 1
    prompt = fake_comfy["workflows"][0]["20"]["inputs"]["prompt"]
    assert "Change only the time of day" in prompt
    assert "夕方にする." in prompt
    assert "Keep the same place, the same layout" in prompt
    # 文を入れると人が湧く実測があるので、毎回打ち消す。
    assert "There are no people anywhere in the scene." in prompt

    # 生成プロンプト欄から作ったものではないので、欄に出す文は持たない。
    variation = client.get(
        f"/api/v1/background-assets/{response.json()['asset']['id']}"
    ).json()
    assert variation["source_prompt"] == prompt
    assert variation["input_prompt"] == ""


def test_prop_variation_edits_the_background_instead_of_redrawing_it(
    client: TestClient, fake_comfy: dict
) -> None:
    """小物を置く回は image 1 の編集だと言い切り、小物の画像は見た目の参考に留める。

    「小物を image 1 の場所へ置け」とだけ書くと、小物を主役にした別の場面が描かれ、
    元の背景が残らなかった（実機、同じ種で確認）。
    """
    project_id = _project(client)
    background = _background(client, project_id)
    prop_id = _prop(client, project_id)

    response = client.post(
        f"/api/v1/background-assets/{background['id']}/variations",
        json={"kind": "prop", "prop_asset_ids": [prop_id], "instruction": "机の上に置く"},
    )
    assert response.status_code == 200, response.text
    assert _finish_job(client, response.json()["job_id"])["state"] == "completed"

    # 元の背景 → 小物 の順で渡す（指示文が image 1 を元の背景として指す）。
    assert len(fake_comfy["uploads"]) == 2
    prompt = fake_comfy["workflows"][0]["20"]["inputs"]["prompt"]
    assert prompt.startswith("Edit image 1. Add the object in image 2 into the scene of image 1")
    assert "Image 2 is only a reference for what the object looks like" in prompt
    assert "do not remove, move, redraw or replace anything" in prompt
    assert "Show the object only once." in prompt
    assert "机の上に置く." in prompt
    assert "Place the object" not in prompt


def test_prop_variation_with_two_props_names_both_references(
    client: TestClient, fake_comfy: dict
) -> None:
    project_id = _project(client)
    background = _background(client, project_id)
    first = _prop(client, project_id, "本")
    second = _prop(client, project_id, "鞄")

    response = client.post(
        f"/api/v1/background-assets/{background['id']}/variations",
        json={"kind": "prop", "prop_asset_ids": [first, second]},
    )
    assert response.status_code == 200, response.text
    assert _finish_job(client, response.json()["job_id"])["state"] == "completed"

    prompt = fake_comfy["workflows"][0]["20"]["inputs"]["prompt"]
    assert "Add the object in image 2 and the object in image 3 into the scene" in prompt
    assert "Images 2 and 3 are only references for what the objects look like" in prompt
    assert "Show each object only once." in prompt


def test_prop_variation_joins_the_same_group(client: TestClient, fake_comfy: dict) -> None:
    """派生した背景は、元背景と同じグループに入る。"""
    project_id = _project(client)
    background = _background(client, project_id)
    prop_id = _prop(client, project_id)

    response = client.post(
        f"/api/v1/background-assets/{background['id']}/variations",
        json={"kind": "prop", "prop_asset_ids": [prop_id], "variant_label": "机に本"},
    )
    assert response.status_code == 200, response.text
    variation = response.json()["asset"]
    assert variation["base_asset_id"] == background["id"]
    assert variation["variant_label"] == "机に本"
    assert _finish_job(client, response.json()["job_id"])["state"] == "completed"

    listed = client.get(f"/api/v1/projects/{project_id}/background-assets").json()
    assert {item["id"] for item in listed} == {background["id"], variation["id"]}
    # 出来上がった背景は、そのまま静止画で選べる。
    assert client.get(f"/api/v1/background-assets/{variation['id']}").json()["has_image"] is True


def test_variation_of_a_variation_keeps_the_first_background_as_the_group(
    client: TestClient, fake_comfy: dict
) -> None:
    """派生の派生でも、グループの元は最初の背景のまま（群れが割れない）。"""
    project_id = _project(client)
    background = _background(client, project_id)
    prop_id = _prop(client, project_id)

    first = client.post(
        f"/api/v1/background-assets/{background['id']}/variations",
        json={"kind": "prop", "prop_asset_ids": [prop_id], "variant_label": "机に本"},
    ).json()
    _finish_job(client, first["job_id"])

    second = client.post(
        f"/api/v1/background-assets/{first['asset']['id']}/variations",
        json={"kind": "time", "instruction": "夕方にする", "variant_label": "夕方・机に本"},
    ).json()
    assert second["asset"]["base_asset_id"] == background["id"]


def test_deleting_the_base_keeps_the_variations(client: TestClient, fake_comfy: dict) -> None:
    """元の背景を消しても、派生した背景は残る（それぞれが元になる）。"""
    project_id = _project(client)
    background = _background(client, project_id)
    prop_id = _prop(client, project_id)
    variation = client.post(
        f"/api/v1/background-assets/{background['id']}/variations",
        json={"kind": "prop", "prop_asset_ids": [prop_id]},
    ).json()["asset"]

    assert client.delete(f"/api/v1/background-assets/{background['id']}").status_code == 204

    remaining = client.get(f"/api/v1/background-assets/{variation['id']}").json()
    assert remaining["base_asset_id"] is None


def test_variation_is_named_after_the_group_base(client: TestClient, fake_comfy: dict) -> None:
    """名前は元の背景の名前に違いを添える。派生の派生でも括弧を重ねない。"""
    project_id = _project(client)
    background = _background(client, project_id)
    prop_id = _prop(client, project_id)

    first = client.post(
        f"/api/v1/background-assets/{background['id']}/variations",
        json={"kind": "prop", "prop_asset_ids": [prop_id], "variant_label": "机に本"},
    ).json()
    _finish_job(client, first["job_id"])
    second = client.post(
        f"/api/v1/background-assets/{first['asset']['id']}/variations",
        json={"kind": "time", "instruction": "夕方にする", "variant_label": "夕方"},
    ).json()

    assert first["asset"]["name"] == "教室（机に本）"
    assert second["asset"]["name"] == "教室（夕方）"


def test_renaming_the_base_renames_the_variations_that_follow_it(
    client: TestClient, fake_comfy: dict
) -> None:
    """元の名前を変えたら、元の名前のままのバリエーションも付け直す。個別に変えた名前は残す。"""
    project_id = _project(client)
    background = _background(client, project_id)
    prop_id = _prop(client, project_id)
    following = client.post(
        f"/api/v1/background-assets/{background['id']}/variations",
        json={"kind": "prop", "prop_asset_ids": [prop_id], "variant_label": "机に本"},
    ).json()["asset"]
    renamed = client.post(
        f"/api/v1/background-assets/{background['id']}/variations",
        json={"kind": "time", "instruction": "夕方にする", "variant_label": "夕方"},
    ).json()["asset"]
    client.patch(f"/api/v1/background-assets/{renamed['id']}", json={"name": "放課後の教室"})

    response = client.patch(f"/api/v1/background-assets/{background['id']}", json={"name": "音楽室"})
    assert response.status_code == 200, response.text

    assert client.get(f"/api/v1/background-assets/{following['id']}").json()["name"] == "音楽室（机に本）"
    assert client.get(f"/api/v1/background-assets/{renamed['id']}").json()["name"] == "放課後の教室"
