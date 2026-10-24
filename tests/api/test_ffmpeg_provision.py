"""書き出しに使う処理エンジン（ffmpeg）を、アプリが用意する。

押さえたいのは4点。
1. 手元にあるものは使う（PATHを通していない導入先も探す）
2. 無いときは「用意できる」と量を添えて返し、画面が押せるボタンを出せる
3. 用意する処理は、配布物から実行ファイルだけを取り出して共有フォルダへ置く
4. 使う場所を指定しているときは、勝手に別のものを用意しない
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chotto_sashie_shared.jobs import JobRepository
from chotto_sashie_api import main
from chotto_sashie_api.database import Database
from chotto_sashie_shared import ffmpeg_provision


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    database = Database(tmp_path / "ffmpeg-api.sqlite3")
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(main, "jobs", JobRepository(database))
    # 共有フォルダを試験用に閉じ込める（本物の置き場所へ書かない）。
    monkeypatch.setenv("CHOTTO_SASHIE_SHARED_DIR", str(tmp_path / "shared"))
    # 実機のPATHや導入先に結果を左右されないようにする。
    monkeypatch.setattr(ffmpeg_provision.shutil, "which", lambda _name: None)
    monkeypatch.setattr(ffmpeg_provision, "common_install_locations", lambda: [])
    monkeypatch.setattr(main.settings, "ffmpeg_path", "")
    with TestClient(main.app) as test_client:
        yield test_client


def _status(client: TestClient) -> dict:
    response = client.get("/api/v1/export/status")
    assert response.status_code == 200, response.text
    return response.json()


def _fake_archive(names: list[str]) -> bytes:
    """配布物と同じ形（版名のフォルダが1階層挟まる）の書庫を作る。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name in names:
            bundle.writestr(name, b"binary-content")
    return buffer.getvalue()


# --- 1. 手元にあるものを使う ---


def test_an_existing_install_off_the_path_is_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PATHを通さない導入方法が多い。PATHだけを見ると「あるのに見つからない」が起きる。"""
    installed = tmp_path / "somewhere" / "bin" / ffmpeg_provision._executable_name("ffmpeg")
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b"x")
    monkeypatch.setenv("CHOTTO_SASHIE_SHARED_DIR", str(tmp_path / "shared"))
    monkeypatch.setattr(ffmpeg_provision.shutil, "which", lambda _name: None)
    monkeypatch.setattr(ffmpeg_provision, "common_install_locations", lambda: [installed])

    assert ffmpeg_provision.locate_ffmpeg() == installed


def test_the_app_copy_is_preferred_over_the_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """アプリが用意したものは検証済みで、結合に使う符号化器が確実に入っている。"""
    shared = tmp_path / "shared"
    monkeypatch.setenv("CHOTTO_SASHIE_SHARED_DIR", str(shared))
    managed = ffmpeg_provision.managed_ffmpeg_path()
    managed.parent.mkdir(parents=True)
    managed.write_bytes(b"x")
    monkeypatch.setattr(ffmpeg_provision.shutil, "which", lambda _name: "C:\\elsewhere\\ffmpeg.exe")

    assert ffmpeg_provision.locate_ffmpeg() == managed


# --- 2. 無いときは、用意できることと量を返す ---


@pytest.fixture
def offline_comfy(monkeypatch: pytest.MonkeyPatch) -> None:
    """ノードが1つも無いComfyUIを装う（道具の判定だけを見たいので中身は空でよい）。"""
    async def object_info(_self):
        return {}

    monkeypatch.setattr(main.ComfyClient, "object_info", object_info)


def _export_capability(client: TestClient) -> dict:
    response = client.get(
        "/api/v1/setup/requirements", params={"base_url": "http://127.0.0.1:8190"}
    )
    assert response.status_code == 200, response.text
    report = response.json()
    found = [cap for cap in report["capabilities"] if cap["id"] == "export"]
    assert found, [cap["id"] for cap in report["capabilities"]]
    return found[0]


def test_the_requirements_check_lists_the_missing_tool(
    client: TestClient, offline_comfy: None
) -> None:
    """動画まで進む設定なら、書き出しに要るものも必要環境の確認に出す。"""
    client.put("/api/v1/setup/video-enabled", json={"enabled": True})

    capability = _export_capability(client)

    assert capability["ready"] is False
    tool = capability["tools"][0]
    assert tool["id"] == "ffmpeg"
    assert tool["present"] is False
    assert tool["can_install"] is True
    # 押す前に量が分かるようにする。
    assert tool["install_size_bytes"] > 0
    # 何を入れるのかを隠さない（外部の配布物なので、名前と取得元と利用条件を出す）。
    assert "ffmpeg" in tool["display_name"]
    assert tool["source"]
    assert tool["license"]


def test_the_export_capability_does_not_block_finishing_setup(
    client: TestClient, offline_comfy: None
) -> None:
    """つなぐ機能は任意。無くてもカット1本ずつは保存できるので、完了を止めない。"""
    client.put("/api/v1/setup/video-enabled", json={"enabled": True})

    response = client.get(
        "/api/v1/setup/requirements", params={"base_url": "http://127.0.0.1:8190"}
    )
    report = response.json()
    export = next(c for c in report["capabilities"] if c["id"] == "export")
    assert export["optional"] is True
    assert export["ready"] is False
    # 任意なので、この不足だけで全体の判定は下げない。
    blocking = [c["id"] for c in report["capabilities"] if not c["optional"] and not c["ready"]]
    assert "export" not in blocking


def test_the_requirements_check_turns_ok_once_it_is_there(
    client: TestClient, offline_comfy: None
) -> None:
    client.put("/api/v1/setup/video-enabled", json={"enabled": True})
    managed = ffmpeg_provision.managed_ffmpeg_path()
    managed.parent.mkdir(parents=True, exist_ok=True)
    managed.write_bytes(b"x")

    capability = _export_capability(client)

    assert capability["ready"] is True
    assert capability["tools"][0]["present"] is True
    assert capability["tools"][0]["detail"] == str(managed)


def test_illustration_only_setups_never_see_it(client: TestClient, offline_comfy: None) -> None:
    """挿絵だけを作る人に、使わない機能の不足を見せない。"""
    client.put("/api/v1/setup/video-enabled", json={"enabled": False})

    response = client.get(
        "/api/v1/setup/requirements", params={"base_url": "http://127.0.0.1:8190"}
    )
    ids = [cap["id"] for cap in response.json()["capabilities"]]
    assert "export" not in ids


def test_a_configured_path_is_not_offered_for_install(
    client: TestClient, offline_comfy: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """場所を指定しているときは、その指定を直すのが先（黙って別のものを用意しない）。"""
    client.put("/api/v1/setup/video-enabled", json={"enabled": True})
    monkeypatch.setattr(main.settings, "ffmpeg_path", r"C:\typo\ffmpeg.exe")

    tool = _export_capability(client)["tools"][0]

    assert tool["present"] is False
    assert tool["can_install"] is False
    assert r"C:\typo\ffmpeg.exe" in tool["hint"]


def test_export_screen_points_at_the_requirements_check(client: TestClient) -> None:
    """書き出し画面は行き先を示すだけ。用意は「ノードとモデルの確認」に集める。

    外部の配布物を入れるので、名前（ffmpeg）は隠さない。
    """
    status = _status(client)

    assert status["ffmpeg_available"] is False
    assert "ノードとモデルの確認" in status["hint"]
    assert "ffmpeg" in status["hint"]


def test_export_screen_is_quiet_once_it_is_ready(client: TestClient) -> None:
    managed = ffmpeg_provision.managed_ffmpeg_path()
    managed.parent.mkdir(parents=True, exist_ok=True)
    managed.write_bytes(b"x")

    status = _status(client)

    assert status["ffmpeg_available"] is True
    assert status["hint"] == ""


# --- 3. 用意する処理 ---


def test_provision_extracts_only_the_executables(tmp_path: Path) -> None:
    """配布物には再生用の実行ファイルや文書も入っている。要るものだけ取り出す。"""
    archive = tmp_path / "ffmpeg.zip"
    archive.write_bytes(
        _fake_archive(
            [
                "ffmpeg-n9.0.1-win64-gpl/bin/ffmpeg.exe",
                "ffmpeg-n9.0.1-win64-gpl/bin/ffprobe.exe",
                "ffmpeg-n9.0.1-win64-gpl/bin/ffplay.exe",
                "ffmpeg-n9.0.1-win64-gpl/LICENSE.txt",
                "ffmpeg-n9.0.1-win64-gpl/doc/ffmpeg.html",
            ]
        )
    )
    tools = tmp_path / "tools" / "ffmpeg"

    executable = ffmpeg_provision.extract_ffmpeg(archive, tools)

    assert executable.is_file()
    assert (tools / "bin" / ffmpeg_provision._executable_name("ffprobe")).is_file()
    # 利用条件は実行ファイルと一緒に残す。
    assert (tools / "LICENSE.txt").is_file()
    # 再生用は使わないので置かない（静的ビルドでは1つで100MB近くある）。
    assert not (tools / "bin" / "ffplay.exe").is_file()
    assert not (tools / "doc").exists()


def test_provision_refuses_an_archive_without_ffmpeg(tmp_path: Path) -> None:
    """中身が違う書庫を、黙って「用意できた」ことにしない。"""
    archive = tmp_path / "wrong.zip"
    archive.write_bytes(_fake_archive(["something/bin/other.exe"]))

    with pytest.raises(ffmpeg_provision.FfmpegProvisionError):
        ffmpeg_provision.extract_ffmpeg(archive, tmp_path / "tools")


def test_provision_never_writes_outside_the_tools_folder(tmp_path: Path) -> None:
    """書庫の中の道をそのまま使わないので、外へ抜ける細工は効かない。"""
    archive = tmp_path / "evil.zip"
    archive.write_bytes(
        _fake_archive(["../../escaped/bin/ffmpeg.exe", "pkg/bin/ffmpeg.exe"])
    )
    tools = tmp_path / "tools" / "ffmpeg"

    ffmpeg_provision.extract_ffmpeg(archive, tools)

    assert (tools / "bin" / ffmpeg_provision._executable_name("ffmpeg")).is_file()
    assert not (tmp_path / "escaped").exists()


def test_provision_job_reports_progress_and_finishes(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """取得は差し替えて、ジョブの流れ（取得→取り出し→確認）だけを確かめる。"""
    archive_bytes = _fake_archive(
        ["ffmpeg-x/bin/ffmpeg.exe", "ffmpeg-x/bin/ffprobe.exe", "ffmpeg-x/LICENSE.txt"]
    )

    def fake_download(root, spec, *, progress=None, should_cancel=None, **_kwargs):
        destination = Path(root) / spec.destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(archive_bytes)
        if progress:
            progress(spec.size_bytes // 2, spec.size_bytes)
            progress(spec.size_bytes, spec.size_bytes)
        return destination

    monkeypatch.setattr(main, "download_with_resume", fake_download)
    monkeypatch.setattr(main, "ensure_free_space", lambda *_args, **_kwargs: None)

    response = client.post("/api/v1/setup/tools/ffmpeg/provision")
    assert response.status_code == 200, response.text
    job = client.get(f"/api/v1/jobs/{response.json()['id']}").json()

    assert job["state"] == "completed", job.get("message")
    assert _status(client)["ffmpeg_available"] is True
    # 展開が済めば書庫は残さない（数百MBを置いたままにしない）。
    assert list((ffmpeg_provision.ffmpeg_tools_root() / "download").glob("*.zip")) == []


def test_provision_is_refused_when_already_usable(client: TestClient) -> None:
    managed = ffmpeg_provision.managed_ffmpeg_path()
    managed.parent.mkdir(parents=True, exist_ok=True)
    managed.write_bytes(b"x")

    assert client.post("/api/v1/setup/tools/ffmpeg/provision").status_code == 409


# --- 4. 場所を指定しているときは、勝手に別のものを用意しない ---


def test_a_configured_path_is_left_alone(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main.settings, "ffmpeg_path", "C:\\typo\\ffmpeg.exe")

    status = _status(client)
    assert status["ffmpeg_available"] is False
    # 指定を直すほうへ導く。
    assert "C:\\typo\\ffmpeg.exe" in status["hint"]
    assert client.post("/api/v1/setup/tools/ffmpeg/provision").status_code == 409
