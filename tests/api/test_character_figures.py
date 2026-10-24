"""立ち絵（人物の実体）の作成・登録・選び直し・削除を確かめる。

人物モデル（LoRA）を作らず、基準画像から作った全身1枚を人物の実体として扱う。
合成はここから選んだ1枚を使うので、**実体の持ち主が立ち絵の記録であること**
（人物を消せば消え、静止画からは参照されるだけ）もここで見る。
"""

import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chotto_sashie_shared.jobs import JobRepository
from chotto_sashie_api import main
from chotto_sashie_api.character_figures import figures_dir
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
def fake_comfy(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    submitted: dict[str, list] = {
        "workflows": [],
        "uploads": [],
        "models": ["qwen_image_edit_2511_fp8mixed.safetensors"],
    }

    async def object_info(_self):
        return {"UNETLoader": {"input": {"required": {"unet_name": [submitted["models"]]}}}}

    async def submit(_self, workflow, prompt_id):
        submitted["workflows"].append(workflow)
        return prompt_id

    async def upload_image(
        _self, data, filename, content_type="image/png", *, subfolder="", overwrite=True
    ):
        submitted["uploads"].append(filename)
        return {"name": filename, "subfolder": "", "type": "input"}

    async def history(_self, prompt_id):
        return {
            "status": {"completed": True, "status_str": "success"},
            "outputs": {
                "17": {
                    "images": [
                        {"filename": f"{prompt_id}.png", "subfolder": "ChottoSashie", "type": "output"}
                    ]
                }
            },
        }

    async def view_image(_self, _image):
        return _TINY_PNG, "image/png"

    async def free_vram(_self):
        return True

    monkeypatch.setattr(main.ComfyClient, "object_info", object_info)
    monkeypatch.setattr(main.ComfyClient, "submit", submit)
    monkeypatch.setattr(main.ComfyClient, "upload_image", upload_image)
    monkeypatch.setattr(main.ComfyClient, "history", history)
    monkeypatch.setattr(main.ComfyClient, "view_image", view_image)
    monkeypatch.setattr(main.ComfyClient, "free_vram", free_vram)
    return submitted


def _finish_job(client: TestClient, job_id: str) -> dict:
    for _ in range(200):
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["state"] in {"completed", "failed", "cancelled", "interrupted"}:
            return job
    raise AssertionError(f"ジョブが終わりませんでした: {job_id}")


def _character(client: TestClient) -> tuple[str, str]:
    project_id = client.post("/api/v1/projects", json={"name": "作品"}).json()["id"]
    character_id = client.post(
        f"/api/v1/projects/{project_id}/characters", json={"name": "ミナ"}
    ).json()["id"]
    return project_id, character_id


def _with_base_image(client: TestClient, character_id: str) -> None:
    """人物候補を作って基準画像を決める（立ち絵はここから作る）。"""
    started = client.post(
        f"/api/v1/characters/{character_id}/candidates/generate",
        json={"count": 8, "seed_start": 1},
    )
    _finish_job(client, started.json()["job"]["id"])
    candidates = client.get(f"/api/v1/characters/{character_id}/candidates").json()
    client.post(f"/api/v1/character-candidates/{candidates[0]['id']}/select-base")


def _generate_figures(
    client: TestClient, character_id: str, count: int = 3, seed: int = 500
) -> list[dict]:
    """立ち絵は1回に1枚。何枚か要る試験は、その数だけ頼む。"""
    for index in range(count):
        response = client.post(
            f"/api/v1/characters/{character_id}/figures/generate",
            json={"seed": seed + index},
        )
        assert response.status_code == 200, response.text
        assert _finish_job(client, response.json()["job"]["id"])["state"] == "completed"
    return client.get(f"/api/v1/characters/{character_id}/figures").json()


def test_figures_need_a_base_image(client: TestClient, fake_comfy: dict[str, list]) -> None:
    """基準画像が決まっていなければ、立ち絵は作れない（何から作るか決まらない）。"""
    _project_id, character_id = _character(client)
    response = client.post(f"/api/v1/characters/{character_id}/figures/generate", json={})
    assert response.status_code == 409


def test_generate_figures_from_the_base_image(
    client: TestClient, fake_comfy: dict[str, list]
) -> None:
    _project_id, character_id = _character(client)
    _with_base_image(client, character_id)
    before = len(fake_comfy["workflows"])

    figures = _generate_figures(client, character_id, count=1, seed=500)

    # 1回に作るのは1枚だけ。
    assert len(figures) == 1
    assert figures[0]["state"] == "completed"
    assert figures[0]["seed"] == 500
    assert figures[0]["origin"] == "base"
    assert figures[0]["edit"] is None
    # 出来た1枚はアプリの持ち物として置かれ、そのまま取り出せる。
    assert client.get(figures[0]["url"]).content == _TINY_PNG

    workflows = fake_comfy["workflows"][before:]
    assert len(workflows) == 1
    # 角度LoRAが学習している語順そのままで正面全身へ組み直す。
    assert workflows[0]["10"]["inputs"]["prompt"].startswith("<sks> front view")
    # 4ステップでは組み直しの指示が弱いので、参照画像の全身と同じ20ステップで作る。
    assert workflows[0]["15"]["inputs"]["steps"] == 20
    assert workflows[0]["4"]["inputs"]["model"] == ["2", 0]
    assert "/figures/" in workflows[0]["17"]["inputs"]["filename_prefix"]


def test_first_figure_becomes_the_default(
    client: TestClient, fake_comfy: dict[str, list]
) -> None:
    """1枚目が既定になる。合成のたびに選ばせないため。"""
    _project_id, character_id = _character(client)
    _with_base_image(client, character_id)
    figures = _generate_figures(client, character_id, count=3)

    defaults = [figure for figure in figures if figure["is_default"]]
    assert len(defaults) == 1
    assert defaults[0]["seed"] == 500


def test_switch_the_default_figure(client: TestClient, fake_comfy: dict[str, list]) -> None:
    _project_id, character_id = _character(client)
    _with_base_image(client, character_id)
    figures = _generate_figures(client, character_id, count=3)
    target = next(figure for figure in figures if not figure["is_default"])

    updated = client.patch(
        f"/api/v1/character-figures/{target['id']}",
        json={"label": "制服", "is_default": True},
    )
    assert updated.status_code == 200
    assert updated.json()["label"] == "制服"

    after = client.get(f"/api/v1/characters/{character_id}/figures").json()
    assert [figure["id"] for figure in after if figure["is_default"]] == [target["id"]]


def test_upload_an_existing_image_as_a_figure(client: TestClient) -> None:
    """手持ちの全身画像も立ち絵として使える（生成しなくても合成へ進める）。"""
    _project_id, character_id = _character(client)
    response = client.post(
        f"/api/v1/characters/{character_id}/figures/upload",
        files={"file": ("figure.png", _TINY_PNG, "image/png")},
    )
    assert response.status_code == 200, response.text
    figure = response.json()
    assert figure["origin"] == "upload"
    assert figure["state"] == "completed"
    assert figure["is_default"] is True
    assert client.get(figure["url"]).content == _TINY_PNG


def test_rejected_upload_leaves_no_record(client: TestClient) -> None:
    """受け取れなかった画像の記録は残さない（消せない空の行を作らない）。"""
    _project_id, character_id = _character(client)
    response = client.post(
        f"/api/v1/characters/{character_id}/figures/upload",
        files={"file": ("figure.txt", b"not an image", "text/plain")},
    )
    assert response.status_code == 422
    assert client.get(f"/api/v1/characters/{character_id}/figures").json() == []


def test_deleting_a_figure_removes_its_file_and_moves_the_default(
    client: TestClient, fake_comfy: dict[str, list]
) -> None:
    """実体の持ち主は立ち絵の記録。消せばファイルも消え、既定は残りへ移る。"""
    _project_id, character_id = _character(client)
    _with_base_image(client, character_id)
    figures = _generate_figures(client, character_id, count=3)
    default = next(figure for figure in figures if figure["is_default"])
    image_path = figures_dir(main.settings.data_dir) / f"{default['id']}.png"
    assert image_path.is_file()

    assert client.delete(f"/api/v1/character-figures/{default['id']}").status_code == 204
    assert not image_path.exists()

    after = client.get(f"/api/v1/characters/{character_id}/figures").json()
    assert len(after) == 2
    assert sum(1 for figure in after if figure["is_default"]) == 1


def test_character_list_counts_completed_figures(
    client: TestClient, fake_comfy: dict[str, list]
) -> None:
    """一覧で「立ち絵がまだ無い人物」を見分けられる。"""
    project_id, character_id = _character(client)
    assert client.get(f"/api/v1/projects/{project_id}/characters").json()[0]["figure_count"] == 0

    _with_base_image(client, character_id)
    _generate_figures(client, character_id, count=3)

    listed = client.get(f"/api/v1/projects/{project_id}/characters").json()
    assert listed[0]["figure_count"] == 3


def test_deleting_the_character_removes_the_figure_files(
    client: TestClient, fake_comfy: dict[str, list]
) -> None:
    _project_id, character_id = _character(client)
    _with_base_image(client, character_id)
    figures = _generate_figures(client, character_id, count=3)
    paths = [figures_dir(main.settings.data_dir) / f"{figure['id']}.png" for figure in figures]
    assert all(path.is_file() for path in paths)

    preview = client.get(f"/api/v1/characters/{character_id}/deletion-preview").json()
    assert preview["figure_count"] == 3

    assert client.delete(f"/api/v1/characters/{character_id}").status_code == 204
    assert not any(path.exists() for path in paths)


def test_default_does_not_depend_on_which_finishes_first(tmp_path: Path) -> None:
    """出来上がる順で既定が入れ替わらない（生成側の都合を選択に持ち込まない）。"""
    from chotto_sashie_api.character_figures import CharacterFigureRepository

    database = Database(tmp_path / "figures.sqlite3")
    database.initialize()
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO projects(id, name, created_at, updated_at) VALUES ('p1', '作品', '2026-09-18', '2026-09-18')"
        )
        connection.execute(
            """
            INSERT INTO characters(id, project_id, name, created_at, updated_at)
            VALUES ('c1', 'p1', 'ミナ', '2026-09-18', '2026-09-18')
            """
        )

    repository = CharacterFigureRepository(database)
    figures = [
        repository.create("c1", origin="base", job_id="j1")
        for _ in range(3)
    ]
    # 3枚目→1枚目の順に仕上がっても、既定は先に頼んだ1枚目のまま。
    for figure in reversed(figures):
        repository.complete(figure.id, tmp_path / f"{figure.id}.png")

    assert repository.default_figure("c1").id == figures[0].id

    # 利用者が選び直したら、その1枚が既定になる。
    repository.set_default(figures[2].id)
    assert repository.default_figure("c1").id == figures[2].id

    # 選ばれていた1枚を消すと、残りの先頭へ戻る（既定が空にならない）。
    repository.delete(figures[2].id)
    assert repository.default_figure("c1").id == figures[0].id


def _prop_with_image(client: TestClient, project_id: str, name: str = "鞄") -> str:
    prop_id = client.post(
        f"/api/v1/projects/{project_id}/prop-assets", json={"name": name}
    ).json()["id"]
    client.post(
        f"/api/v1/prop-assets/{prop_id}/image",
        files={"file": ("prop.png", _TINY_PNG, "image/png")},
    )
    return prop_id


def _uploaded_figure(client: TestClient, character_id: str) -> dict:
    return client.post(
        f"/api/v1/characters/{character_id}/figures/upload",
        files={"file": ("figure.png", _TINY_PNG, "image/png")},
    ).json()


def _edited_png(monkeypatch: pytest.MonkeyPatch) -> bytes:
    """変更の結果として返す画像。元の立ち絵と見分けられるよう中身を変える。"""
    edited = _TINY_PNG + b"edited"

    async def view_image(_self, _image):
        return edited, "image/png"

    monkeypatch.setattr(main.ComfyClient, "view_image", view_image)
    return edited


def _start_edit(client: TestClient, figure_id: str, **body) -> dict:
    response = client.post(
        f"/api/v1/character-figures/{figure_id}/edits",
        json={"instruction": "制服をパーカーに変える", **body},
    )
    assert response.status_code == 200, response.text
    assert _finish_job(client, response.json()["job"]["id"])["state"] == "completed"
    figures = client.get(f"/api/v1/characters/{_character_of(client, figure_id)}/figures").json()
    return next(item for item in figures if item["id"] == figure_id)


def _character_of(client: TestClient, figure_id: str) -> str:
    from chotto_sashie_api.character_figures import CharacterFigureRepository

    return CharacterFigureRepository(main.database).get(figure_id).character_id


def test_edit_needs_something_to_change(client: TestClient) -> None:
    """指示も小物も無ければ、同じ絵を作り直すだけになる。作る前に断る。"""
    _project_id, character_id = _character(client)
    figure = _uploaded_figure(client, character_id)

    response = client.post(f"/api/v1/character-figures/{figure['id']}/edits", json={})
    assert response.status_code == 422


def test_edit_result_waits_until_saved(
    client: TestClient, fake_comfy: dict[str, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    """変更の結果は、保存するまで立ち絵へ反映しない（見てから決めるため）。"""
    project_id, character_id = _character(client)
    figure = _uploaded_figure(client, character_id)
    prop_id = _prop_with_image(client, project_id)
    edited = _edited_png(monkeypatch)

    after = _start_edit(client, figure["id"], prop_asset_ids=[prop_id])

    edit = after["edit"]
    assert edit["state"] == "completed"
    assert edit["instruction"] == "制服をパーカーに変える"
    assert edit["prop_asset_ids"] == [prop_id]
    assert client.get(edit["url"]).content == edited
    # 立ち絵そのものはまだ元のまま。一覧にも増えない。
    assert client.get(figure["url"]).content == _TINY_PNG
    assert len(client.get(f"/api/v1/characters/{character_id}/figures").json()) == 1


def test_saving_an_edit_replaces_the_figure_by_default(
    client: TestClient, fake_comfy: dict[str, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    """既定の保存は置き換え。IDと既定の印はそのままなので、静止画の選択も生きる。"""
    _project_id, character_id = _character(client)
    figure = _uploaded_figure(client, character_id)
    client.patch(f"/api/v1/character-figures/{figure['id']}", json={"label": "制服"})
    old_path = Path(_figure_record(figure["id"]).image_path)
    edited = _edited_png(monkeypatch)
    edit = _start_edit(client, figure["id"])["edit"]
    edit_path = figures_dir(main.settings.data_dir) / f"edit-{edit['id']}.png"
    assert edit_path.is_file()

    saved = client.post(f"/api/v1/character-figure-edits/{edit['id']}/save", json={})
    assert saved.status_code == 200, saved.text

    figures = client.get(f"/api/v1/characters/{character_id}/figures").json()
    assert len(figures) == 1
    replaced = figures[0]
    assert replaced["id"] == figure["id"]
    assert replaced["label"] == "制服"
    assert replaced["is_default"] is True
    assert replaced["origin"] == "variation"
    assert replaced["instruction"] == "制服をパーカーに変える"
    assert replaced["edit"] is None
    assert replaced["updated_at"] != figure["updated_at"]
    assert client.get(replaced["url"]).content == edited
    # 元の画像と、預かっていた結果のファイルは残さない。
    assert not old_path.exists()
    assert not edit_path.exists()


def test_duplicating_a_figure_adds_an_independent_copy(client: TestClient) -> None:
    """複製すると、同じ立ち絵がもう1枚増える。元を残したまま変えたいときは複製してから変更する。"""
    _project_id, character_id = _character(client)
    figure = _uploaded_figure(client, character_id)
    client.patch(f"/api/v1/character-figures/{figure['id']}", json={"label": "制服"})

    response = client.post(f"/api/v1/character-figures/{figure['id']}/duplicate")

    assert response.status_code == 201, response.text
    copy = response.json()
    assert copy["id"] != figure["id"]
    assert copy["label"] == "制服（複製）"
    assert copy["origin"] == figure["origin"]
    assert copy["is_default"] is False
    assert client.get(copy["url"]).content == client.get(figure["url"]).content
    figures = client.get(f"/api/v1/characters/{character_id}/figures").json()
    assert {item["id"] for item in figures} == {figure["id"], copy["id"]}
    # 画像は別のファイル。片方を消しても、もう片方は残る。
    assert client.delete(f"/api/v1/character-figures/{figure['id']}").status_code == 204
    assert client.get(copy["url"]).status_code == 200


def _figure_ids(client: TestClient, character_id: str) -> list[str]:
    return [item["id"] for item in client.get(f"/api/v1/characters/{character_id}/figures").json()]


def test_figures_are_listed_in_the_order_they_were_added(client: TestClient) -> None:
    """新しい立ち絵は右（末尾）へ足していく。"""
    _project_id, character_id = _character(client)
    first = _uploaded_figure(client, character_id)
    second = _uploaded_figure(client, character_id)
    third = _uploaded_figure(client, character_id)

    assert _figure_ids(client, character_id) == [first["id"], second["id"], third["id"]]


def test_a_duplicate_is_placed_right_after_its_source(client: TestClient) -> None:
    """複製は元のすぐ右に置く。末尾に置くと、何枚もあるときにどれの複製か分からなくなる。"""
    _project_id, character_id = _character(client)
    first = _uploaded_figure(client, character_id)
    second = _uploaded_figure(client, character_id)
    third = _uploaded_figure(client, character_id)

    copy = client.post(f"/api/v1/character-figures/{first['id']}/duplicate").json()

    assert _figure_ids(client, character_id) == [first["id"], copy["id"], second["id"], third["id"]]
    # 複製しても既定は動かない（先頭は元のまま）。
    defaults = [
        item["id"]
        for item in client.get(f"/api/v1/characters/{character_id}/figures").json()
        if item["is_default"]
    ]
    assert defaults == [first["id"]]


def test_a_duplicate_goes_right_of_its_source_before_any_reordering(client: TestClient) -> None:
    """並び位置を振る前の記録（位置がみな同じ）でも、複製は元のすぐ右に入る。"""
    _project_id, character_id = _character(client)
    first = _uploaded_figure(client, character_id)
    second = _uploaded_figure(client, character_id)
    with main.database.connect() as connection:
        connection.execute(
            "UPDATE character_figures SET ordinal = 1 WHERE character_id = ?", (character_id,)
        )

    copy = client.post(f"/api/v1/character-figures/{first['id']}/duplicate").json()

    assert _figure_ids(client, character_id) == [first["id"], copy["id"], second["id"]]


def test_reorder_figures(client: TestClient) -> None:
    """並べ替えた順で返り、その後に足した立ち絵は末尾へ入る。"""
    _project_id, character_id = _character(client)
    first = _uploaded_figure(client, character_id)
    second = _uploaded_figure(client, character_id)
    third = _uploaded_figure(client, character_id)
    order = [third["id"], first["id"], second["id"]]

    response = client.put(
        f"/api/v1/characters/{character_id}/figures/order", json={"figure_ids": order}
    )

    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()] == order
    assert _figure_ids(client, character_id) == order
    fourth = _uploaded_figure(client, character_id)
    assert _figure_ids(client, character_id) == [*order, fourth["id"]]


def test_the_first_figure_becomes_the_default_unless_one_was_chosen(client: TestClient) -> None:
    """誰も既定を選んでいなければ先頭の1枚が既定。選んだ既定は並べ替えても動かない。"""
    _project_id, character_id = _character(client)
    first = _uploaded_figure(client, character_id)
    second = _uploaded_figure(client, character_id)
    url = f"/api/v1/characters/{character_id}/figures/order"

    moved = client.put(url, json={"figure_ids": [second["id"], first["id"]]}).json()
    assert [item["id"] for item in moved if item["is_default"]] == [second["id"]]

    client.patch(f"/api/v1/character-figures/{first['id']}", json={"is_default": True})
    moved = client.put(url, json={"figure_ids": [second["id"], first["id"]]}).json()
    assert [item["id"] for item in moved if item["is_default"]] == [first["id"]]


def test_reorder_is_refused_when_the_figures_changed(client: TestClient) -> None:
    """画面が古い一覧を見ていたら断り、並びは変えない。"""
    _project_id, character_id = _character(client)
    first = _uploaded_figure(client, character_id)
    second = _uploaded_figure(client, character_id)
    url = f"/api/v1/characters/{character_id}/figures/order"

    for figure_ids in ([second["id"]], [second["id"], first["id"], "missing"], [first["id"]] * 2):
        response = client.put(url, json={"figure_ids": figure_ids})
        assert response.status_code == 409
    assert _figure_ids(client, character_id) == [first["id"], second["id"]]


def test_a_figure_still_being_made_cannot_be_duplicated(client: TestClient) -> None:
    _project_id, character_id = _character(client)
    from chotto_sashie_api.character_figures import ORIGIN_BASE, CharacterFigureRepository

    running = CharacterFigureRepository(main.database).create(
        character_id, origin=ORIGIN_BASE, job_id="job-1"
    )

    response = client.post(f"/api/v1/character-figures/{running.id}/duplicate")

    assert response.status_code == 409


def test_discarding_an_edit_keeps_the_figure(
    client: TestClient, fake_comfy: dict[str, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    _project_id, character_id = _character(client)
    figure = _uploaded_figure(client, character_id)
    _edited_png(monkeypatch)
    edit = _start_edit(client, figure["id"])["edit"]
    edit_path = figures_dir(main.settings.data_dir) / f"edit-{edit['id']}.png"

    assert client.delete(f"/api/v1/character-figure-edits/{edit['id']}").status_code == 204

    figures = client.get(f"/api/v1/characters/{character_id}/figures").json()
    assert figures[0]["edit"] is None
    assert client.get(figures[0]["url"]).content == _TINY_PNG
    assert not edit_path.exists()


def test_editing_again_drops_the_unsaved_result(
    client: TestClient, fake_comfy: dict[str, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    """1枚の立ち絵に預かる結果は1つ。変更し直すと前の結果は捨てる。"""
    _project_id, character_id = _character(client)
    figure = _uploaded_figure(client, character_id)
    _edited_png(monkeypatch)
    first = _start_edit(client, figure["id"])["edit"]
    first_path = figures_dir(main.settings.data_dir) / f"edit-{first['id']}.png"

    second = _start_edit(client, figure["id"], instruction="コートを着せる")["edit"]

    assert second["id"] != first["id"]
    assert second["instruction"] == "コートを着せる"
    assert not first_path.exists()
    assert client.get(f"/api/v1/character-figure-edits/{first['id']}/image").status_code == 404


def test_figure_being_edited_cannot_be_deleted_or_edited_twice(
    client: TestClient, fake_comfy: dict[str, list]
) -> None:
    """変更中の立ち絵は消せず、二重にも頼めない（出来た画像の行き先が無くなるため）。"""
    from chotto_sashie_api.character_figure_edits import FigureEditRepository

    _project_id, character_id = _character(client)
    figure = _uploaded_figure(client, character_id)
    FigureEditRepository(main.database).start(
        _figure_record(figure["id"]),
        instruction="コートを着せる",
        prop_asset_ids=[],
        prompt="",
        seed=1,
        base_url="",
    )

    assert client.delete(f"/api/v1/character-figures/{figure['id']}").status_code == 409
    again = client.post(
        f"/api/v1/character-figures/{figure['id']}/edits", json={"instruction": "帽子を足す"}
    )
    assert again.status_code == 409

    # 再起動をまたいだ変更中は失敗へ倒し、捨てて頼み直せるようにする。
    assert FigureEditRepository(main.database).recover_interrupted("中断") == 1
    edit = client.get(f"/api/v1/characters/{character_id}/figures").json()[0]["edit"]
    assert edit["state"] == "failed"
    assert client.delete(f"/api/v1/character-figure-edits/{edit['id']}").status_code == 204


def test_deleting_a_figure_removes_its_unsaved_edit(
    client: TestClient, fake_comfy: dict[str, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    _project_id, character_id = _character(client)
    figure = _uploaded_figure(client, character_id)
    _edited_png(monkeypatch)
    edit = _start_edit(client, figure["id"])["edit"]
    edit_path = figures_dir(main.settings.data_dir) / f"edit-{edit['id']}.png"

    assert client.delete(f"/api/v1/character-figures/{figure['id']}").status_code == 204
    assert not edit_path.exists()


def test_deleting_the_character_removes_unsaved_edits(
    client: TestClient, fake_comfy: dict[str, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    _project_id, character_id = _character(client)
    figure = _uploaded_figure(client, character_id)
    _edited_png(monkeypatch)
    edit = _start_edit(client, figure["id"])["edit"]
    edit_path = figures_dir(main.settings.data_dir) / f"edit-{edit['id']}.png"

    assert client.delete(f"/api/v1/characters/{character_id}").status_code == 204
    assert not edit_path.exists()


def test_replacing_the_default_figure_changes_the_portrait_version(
    client: TestClient, fake_comfy: dict[str, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    """一覧の写真は既定の立ち絵が出ている。置き換えたら版が変わり、古い画像を出さない。"""
    project_id, character_id = _character(client)
    figure = _uploaded_figure(client, character_id)
    before = client.get(f"/api/v1/projects/{project_id}/characters").json()[0]["portrait_version"]
    _edited_png(monkeypatch)
    edit = _start_edit(client, figure["id"])["edit"]
    client.post(f"/api/v1/character-figure-edits/{edit['id']}/save", json={})

    after = client.get(f"/api/v1/projects/{project_id}/characters").json()[0]["portrait_version"]
    assert after != before


def test_edit_prompt_keeps_the_figure_conditions(
    client: TestClient, fake_comfy: dict[str, list]
) -> None:
    """出来上がりが立ち絵の条件（全身・素背景・1人）を満たすよう指示する。"""
    project_id, character_id = _character(client)
    figure = _uploaded_figure(client, character_id)
    prop_id = _prop_with_image(client, project_id)
    before = len(fake_comfy["workflows"])

    started = client.post(
        f"/api/v1/character-figures/{figure['id']}/edits",
        json={"instruction": "コートを着せる", "prop_asset_ids": [prop_id]},
    )
    _finish_job(client, started.json()["job"]["id"])

    workflow = fake_comfy["workflows"][before]
    prompt = workflow["20"]["inputs"]["prompt"]
    assert "コートを着せる." in prompt
    assert "The person in image 1 is wearing or holding the object in image 2." in prompt
    assert "Keep that object exactly as in its own image." in prompt
    assert "Never crop at the waist or thighs." in prompt
    assert "Plain light-gray background" in prompt
    assert "a single person and nobody else" in prompt
    # 衣装だけを変え、顔・体格は元の立ち絵のまま。
    assert "Change only their clothing and the items they carry." in prompt
    # 高速化LoRAは外す（4ステップでは指示も立ち絵の条件も守られない）。
    assert "3" not in workflow
    assert workflow["30"]["inputs"]["steps"] > 4


def test_edit_rejects_a_prop_without_an_image(
    client: TestClient, fake_comfy: dict[str, list]
) -> None:
    project_id, character_id = _character(client)
    figure = _uploaded_figure(client, character_id)
    prop_id = client.post(
        f"/api/v1/projects/{project_id}/prop-assets", json={"name": "画像なし"}
    ).json()["id"]

    response = client.post(
        f"/api/v1/character-figures/{figure['id']}/edits",
        json={"prop_asset_ids": [prop_id]},
    )
    assert response.status_code == 409
    assert "画像が登録されていません" in response.json()["detail"]
    # 断ったときは、預かりの記録も残さない。
    assert client.get(f"/api/v1/characters/{character_id}/figures").json()[0]["edit"] is None


def _figure_record(figure_id: str):
    from chotto_sashie_api.character_figures import CharacterFigureRepository

    return CharacterFigureRepository(main.database).get(figure_id)
