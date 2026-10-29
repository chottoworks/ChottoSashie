"""どの記録からも辿れなくなったファイルを片付けることを確かめる。

記録を消してからファイルを消すまでの間に落ちると、行き先の無いファイルが保存先に残る。
利用者からは見えず、消す手段も無いので、起動のたびに片付ける。
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chotto_sashie_shared.jobs import JobRepository
from chotto_sashie_shared.local_images import CANDIDATE_IMAGES, local_image_path
from chotto_sashie_api import main
from chotto_sashie_api.database import Database
from chotto_sashie_api.exporting import project_export_path
from chotto_sashie_api.orphans import remove_unreferenced_files
from chotto_sashie_api.still_images import StillImageRepository
from chotto_sashie_api.storage import shot_video_dir, still_image_dir


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def client(tmp_path: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    database = Database(tmp_path / "video-api.sqlite3")
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(main, "jobs", JobRepository(database))
    monkeypatch.setattr(main.settings, "data_dir", data_dir)
    with TestClient(main.app) as test_client:
        yield test_client


def _write(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


def _still_image_with_file(client: TestClient, data_dir: Path) -> tuple[str, str, Path]:
    project_id = client.post("/api/v1/projects", json={"name": "作品"}).json()["id"]
    still_image_id = client.post(
        f"/api/v1/projects/{project_id}/still-images", json={"name": "放課後"}
    ).json()["id"]
    image = _write(still_image_dir(data_dir) / f"{still_image_id}.png")
    StillImageRepository(main.database).set_image(still_image_id, image, source_prompt="")
    return project_id, still_image_id, image


def test_files_no_record_points_to_are_removed(client: TestClient, data_dir: Path) -> None:
    project_id, _still_image_id, kept = _still_image_with_file(client, data_dir)
    exported = _write(project_export_path(data_dir, project_id))
    orphan_image = _write(still_image_dir(data_dir) / "gone.png")
    orphan_video = _write(shot_video_dir(data_dir) / "gone.webp")
    orphan_export = _write(project_export_path(data_dir, "deleted-project"))
    orphan_candidate = _write(local_image_path(data_dir, CANDIDATE_IMAGES, "deleted-candidate"))
    leftover_temp = _write(still_image_dir(data_dir) / ".half-written.png.tmp")

    removed = remove_unreferenced_files(main.database, data_dir)

    assert removed == 5
    assert kept.is_file()
    assert exported.is_file()
    for path in (orphan_image, orphan_video, orphan_export, orphan_candidate, leftover_temp):
        assert not path.exists()


def test_records_are_matched_by_file_name(client: TestClient, data_dir: Path, tmp_path: Path) -> None:
    """記録の中の場所が古い保存先を指していても、同じ名前のファイルは消さない。"""
    _project_id, still_image_id, image = _still_image_with_file(client, data_dir)
    StillImageRepository(main.database).set_image(
        still_image_id,
        tmp_path / "old-location" / "keyframes" / image.name,
        source_prompt="",
    )

    assert remove_unreferenced_files(main.database, data_dir) == 0
    assert image.is_file()


def test_only_the_apps_own_folders_are_looked_at(client: TestClient, data_dir: Path) -> None:
    """記録ごとのファイルを置くフォルダの外と、その中のフォルダには触れない。"""
    database_file = data_dir / "database" / "keep.txt"
    managed = data_dir / "managed" / "runtime" / "comfy" / "user" / "keep.json"
    nested = still_image_dir(data_dir) / "folder" / "keep.png"
    for path in (database_file, managed, nested):
        _write(path)

    remove_unreferenced_files(main.database, data_dir)

    assert database_file.is_file()
    assert managed.is_file()
    assert nested.is_file()


def test_startup_removes_what_was_left_behind(
    tmp_path: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """起動のたびに片付ける（削除の途中で落ちた分を残さない）。"""
    database = Database(tmp_path / "video-api.sqlite3")
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(main, "jobs", JobRepository(database))
    monkeypatch.setattr(main.settings, "data_dir", data_dir)
    orphan = _write(still_image_dir(data_dir) / "left-behind.png")

    with TestClient(main.app):
        pass

    assert not orphan.exists()


def test_a_result_whose_record_was_deleted_is_not_kept(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """生成の途中で静止画を消したら、出来上がった1枚は行き先が無いので残さない。"""
    import asyncio

    from chotto_sashie_shared.jobs import JobCreate, JobTransition

    project_id = client.post("/api/v1/projects", json={"name": "作品"}).json()["id"]
    still_image_id = client.post(
        f"/api/v1/projects/{project_id}/still-images", json={"name": "消える静止画"}
    ).json()["id"]
    job = main.jobs.create(JobCreate(kind="composition.still_image", payload={}))
    main.jobs.transition(job.id, JobTransition(state="preparing", progress=0.1))
    assert client.delete(f"/api/v1/still-images/{still_image_id}").status_code == 204

    async def history(_self, _prompt_id):
        return {
            "status": {"completed": True, "status_str": "success"},
            "outputs": {"40": {"images": [{"filename": "a.png", "subfolder": "", "type": "output"}]}},
        }

    async def view_image(_self, _image):
        return b"PNG", "image/png"

    monkeypatch.setattr(main.ComfyClient, "history", history)
    monkeypatch.setattr(main.ComfyClient, "view_image", view_image)

    asyncio.run(
        main.monitor_still_image_composition(
            job.id,
            still_image_id,
            "prompt-1",
            "http://127.0.0.1:8188",
            poll_seconds=0,
            source_prompt="",
        )
    )

    assert main.jobs.get(job.id).state == "failed"
    assert not (main._STILL_IMAGE_ROOT / f"{still_image_id}.png").exists()
