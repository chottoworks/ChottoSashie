"""使わなくなった人物候補を、画像ごと残さず消せることを確かめる。

基準画像を決めると、ほかの候補は使い道が無くなる。作り直しの既定は上書きで、
設定で「履歴として残す」に切り替えられる。
"""

import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chotto_sashie_shared.jobs import JobRepository
from chotto_sashie_shared.local_images import CANDIDATE_IMAGES, local_image_path
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
            json={"mode": "existing_comfyui", "base_url": "http://127.0.0.1:8188"},
        )
        yield test_client


@pytest.fixture
def fake_comfy(monkeypatch: pytest.MonkeyPatch) -> None:
    async def submit(_self, _workflow, prompt_id):
        return prompt_id

    async def history(_self, prompt_id):
        return {
            "status": {"completed": True, "status_str": "success"},
            "outputs": {"9": {"images": [{"filename": f"{prompt_id}.png", "subfolder": "", "type": "output"}]}},
        }

    async def view_image(_self, _image):
        return _TINY_PNG, "image/png"

    async def free_vram(_self):
        return True

    monkeypatch.setattr(main.ComfyClient, "submit", submit)
    monkeypatch.setattr(main.ComfyClient, "history", history)
    monkeypatch.setattr(main.ComfyClient, "view_image", view_image)
    monkeypatch.setattr(main.ComfyClient, "free_vram", free_vram)


def _finish_job(client: TestClient, job_id: str) -> dict:
    for _ in range(200):
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["state"] in {"completed", "failed", "cancelled", "interrupted"}:
            return job
    raise AssertionError(f"ジョブが終わりませんでした: {job_id}")


def _character(client: TestClient) -> str:
    project_id = client.post("/api/v1/projects", json={"name": "作品"}).json()["id"]
    return client.post(
        f"/api/v1/projects/{project_id}/characters", json={"name": "ミナ"}
    ).json()["id"]


def _generate(client: TestClient, character_id: str, **body) -> None:
    started = client.post(
        f"/api/v1/characters/{character_id}/candidates/generate",
        json={"count": 8, "seed_start": 1, **body},
    )
    assert started.status_code == 200, started.text
    assert _finish_job(client, started.json()["job"]["id"])["state"] == "completed"


def _all_candidates(client: TestClient, character_id: str) -> list[dict]:
    return client.get(
        f"/api/v1/characters/{character_id}/candidates", params={"latest_only": False}
    ).json()


def _local_copy(candidate_id: str) -> Path:
    return local_image_path(main.settings.data_dir, CANDIDATE_IMAGES, candidate_id)


def test_regenerating_overwrites_by_default_but_keeps_the_base(
    client: TestClient, fake_comfy: None
) -> None:
    character_id = _character(client)
    _generate(client, character_id)
    first = _all_candidates(client, character_id)
    base = first[0]
    client.post(f"/api/v1/character-candidates/{base['id']}/select-base")

    _generate(client, character_id)

    remaining = _all_candidates(client, character_id)
    first_ids = {candidate["id"] for candidate in first}
    # 前の回は基準画像だけが残る（立ち絵の起点として使い続けるため）。
    assert [c["id"] for c in remaining if c["id"] in first_ids] == [base["id"]]
    assert len(remaining) == 9
    for candidate in first:
        assert _local_copy(candidate["id"]).is_file() is (candidate["id"] == base["id"])


def test_keeping_history_can_be_chosen_in_the_settings(
    client: TestClient, fake_comfy: None
) -> None:
    character_id = _character(client)
    client.put("/api/v1/preferences", json={"candidate_history": "keep"})
    _generate(client, character_id)

    _generate(client, character_id)

    assert len(_all_candidates(client, character_id)) == 16
    assert len(client.get(f"/api/v1/characters/{character_id}/candidate-batches").json()) == 2


def test_each_regeneration_can_override_the_setting(
    client: TestClient, fake_comfy: None
) -> None:
    character_id = _character(client)
    _generate(client, character_id)

    _generate(client, character_id, keep_history=True)

    assert len(_all_candidates(client, character_id)) == 16


def test_unused_candidates_are_deleted_with_their_images(
    client: TestClient, fake_comfy: None
) -> None:
    """基準画像を決めたら、選ばなかった候補を画像ごとまとめて消せる。"""
    character_id = _character(client)
    client.put("/api/v1/preferences", json={"candidate_history": "keep"})
    _generate(client, character_id)
    _generate(client, character_id)
    candidates = _all_candidates(client, character_id)
    base = candidates[3]
    client.post(f"/api/v1/character-candidates/{base['id']}/select-base")

    response = client.delete(f"/api/v1/characters/{character_id}/candidates/unused")

    assert response.status_code == 200, response.text
    assert response.json()["deleted_count"] == 15
    assert [c["id"] for c in _all_candidates(client, character_id)] == [base["id"]]
    assert _local_copy(base["id"]).is_file()
    for candidate in candidates:
        if candidate["id"] != base["id"]:
            assert not _local_copy(candidate["id"]).exists()


def test_unused_candidates_wait_for_a_running_batch(client: TestClient) -> None:
    """生成中の回があるときは、出来上がるものまで「選ばなかった」ことにしない。"""
    character_id = _character(client)
    with main.database.connect() as connection:
        connection.execute(
            """
            INSERT INTO character_candidates(
                id, character_id, batch_job_id, ordinal, state, prompt, negative_prompt,
                seed, width, height, base_url, created_at, updated_at
            ) VALUES ('running-1', ?, 'job-1', 1, 'running', '', '', 1, 512, 688, '', 'now', 'now')
            """,
            (character_id,),
        )

    response = client.delete(f"/api/v1/characters/{character_id}/candidates/unused")

    assert response.status_code == 409


def test_returning_the_portrait_to_default_removes_its_copy(client: TestClient) -> None:
    """代表写真を既定へ戻したら、使わなくなった写しは消す。"""
    character_id = _character(client)
    uploaded = client.post(
        f"/api/v1/characters/{character_id}/portrait",
        files={"file": ("face.png", _TINY_PNG, "image/png")},
    )
    assert uploaded.status_code == 200, uploaded.text
    portrait = main._CHARACTER_PORTRAIT_ROOT / f"{character_id}.png"
    assert portrait.is_file()

    reset = client.post(
        f"/api/v1/characters/{character_id}/portrait/select", json={"option_id": "default"}
    )

    assert reset.status_code == 200, reset.text
    assert reset.json()["has_portrait_image"] is False
    assert not portrait.exists()
