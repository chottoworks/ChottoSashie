from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chotto_sashie_shared.jobs import JobCreate, JobRepository
from chotto_sashie_api import main
from chotto_sashie_shared.shared_env import shared_comfyui_root
from chotto_sashie_api.comfyui_provision import (
    comfyui_update_available,
    installed_comfyui_version,
    is_provisioned,
    managed_comfyui_paths,
)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from chotto_sashie_api.database import Database

    database = Database(tmp_path / "video-api.sqlite3")
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(main, "jobs", JobRepository(database))
    with TestClient(main.app) as test_client:
        yield test_client


def _fake_provisioned(comfyui_root: Path, version: str = "") -> None:
    comfy_dir, python = managed_comfyui_paths(comfyui_root)
    comfy_dir.mkdir(parents=True, exist_ok=True)
    (comfy_dir / "main.py").write_text("", encoding="utf-8")
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("", encoding="utf-8")
    if version:
        (comfy_dir / "comfyui_version.py").write_text(
            f'__version__ = "{version}"\n', encoding="utf-8"
        )


def test_is_provisioned_detects_layout(tmp_path: Path) -> None:
    root = tmp_path / "comfyui"
    assert is_provisioned(root) is False
    _fake_provisioned(root)
    assert is_provisioned(root) is True


def test_status_reports_not_provisioned(client: TestClient) -> None:
    body = client.get("/api/v1/setup/comfyui/status").json()
    assert body["provisioned"] is False
    assert body["target_version"] == "v0.42.1"


def test_status_reports_provisioned(client: TestClient, tmp_path: Path) -> None:
    _fake_provisioned(shared_comfyui_root())
    body = client.get("/api/v1/setup/comfyui/status").json()
    assert body["provisioned"] is True
    assert body["comfyui_path"] is not None


def test_provision_downloads_and_extracts(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_download(root, spec, **_kwargs):
        archive = root / spec.destination
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(b"7z")
        return archive

    def fake_extract(archive, destination):
        # 展開結果として正しいレイアウトを作る。
        _fake_provisioned(destination)

    monkeypatch.setattr(main, "download_with_resume", fake_download)
    monkeypatch.setattr(main, "extract_archive", fake_extract)

    started = client.post("/api/v1/setup/comfyui/provision")
    assert started.status_code == 200
    job = client.get(f"/api/v1/jobs/{started.json()['id']}").json()
    assert job["state"] == "completed"
    assert client.get("/api/v1/setup/comfyui/status").json()["provisioned"] is True


def _deep_archive(tmp_path: Path) -> tuple[Path, str]:
    """260文字を超える階層を1件だけ持つ .7z を作る（同梱torchのライセンス類に相当）。"""
    import py7zr

    member = "/".join(["third_party_folder_with_long_name"] * 7) + "/colorama/LICENSE.txt"
    archive = tmp_path / "deep.7z"
    with py7zr.SevenZipFile(archive, "w") as handle:
        handle.writestr("ライセンス本文", member)
    return archive, member


def _assert_extracted(destination: Path, member: str) -> None:
    from chotto_sashie_shared.comfyui_provision import long_path

    assert len(str(destination / member)) > 260
    # 存在確認も同じ長さ制限を受けるため、拡張パス表記で確かめる。
    extracted = long_path(destination / member.replace("/", "\\"))
    assert extracted.is_file()
    assert extracted.read_text(encoding="utf-8") == "ライセンス本文"


def test_extract_archive_handles_deep_paths(tmp_path: Path) -> None:
    """既定の展開経路で、260文字を超える深い階層まで展開できる。"""
    from chotto_sashie_shared.comfyui_provision import extract_archive

    archive, member = _deep_archive(tmp_path)
    destination = tmp_path / "comfyui"
    extract_archive(archive, destination)
    # 失敗後のやり直しに備え、展開済みの上へ再展開しても成功すること。
    extract_archive(archive, destination)

    _assert_extracted(destination, member)


def test_extract_archive_falls_back_to_py7zr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """bsdtarが無い環境でも、py7zrで展開できる（BCJ2以外の書庫）。"""
    from chotto_sashie_shared import comfyui_provision

    monkeypatch.setattr(comfyui_provision, "bundled_bsdtar", lambda: None)
    archive, member = _deep_archive(tmp_path)
    destination = tmp_path / "comfyui-fallback"
    comfyui_provision.extract_archive(archive, destination)

    _assert_extracted(destination, member)


def test_extract_archive_reports_bsdtar_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """展開コマンドが失敗したら、理由を日本語のエラーとして返す。"""
    from chotto_sashie_shared import comfyui_provision

    archive, _member = _deep_archive(tmp_path)
    if comfyui_provision.bundled_bsdtar() is None:
        pytest.skip("bsdtarが無い環境")

    broken = tmp_path / "broken.7z"
    broken.write_bytes(b"not an archive")
    with pytest.raises(comfyui_provision.ComfyUIProvisionError, match="展開に失敗しました"):
        comfyui_provision.extract_archive(broken, tmp_path / "comfyui-broken")


def test_provision_duplicate_is_rejected(client: TestClient) -> None:
    main.jobs.create(JobCreate(kind="setup.comfyui.provision", payload={}))
    response = client.post("/api/v1/setup/comfyui/provision")
    assert response.status_code == 409


def test_installed_version_is_read_from_the_extracted_comfyui(tmp_path: Path) -> None:
    root = tmp_path / "comfyui"
    comfy_dir, _python = managed_comfyui_paths(root)
    assert installed_comfyui_version(comfy_dir) == ""
    _fake_provisioned(root, version="0.33.1")
    assert installed_comfyui_version(comfy_dir) == "0.33.1"


def test_update_available_compares_numbers_not_text() -> None:
    # 文字列比較だと "0.9" > "0.33" になってしまうため、数値として比べる。
    assert comfyui_update_available("0.9.0", "v0.33.1") is True
    assert comfyui_update_available("0.33.1", "v0.33.1") is False
    assert comfyui_update_available("0.34.0", "v0.33.1") is False
    # どちらかが読めないときは「古い」と決めつけない。
    assert comfyui_update_available("", "v0.33.1") is False


def test_status_offers_an_update_when_the_installed_comfyui_is_old(
    client: TestClient, tmp_path: Path
) -> None:
    _fake_provisioned(shared_comfyui_root(), version="0.28.0")
    body = client.get("/api/v1/setup/comfyui/status").json()
    assert body["provisioned"] is True
    assert body["installed_version"] == "0.28.0"
    # 新しいモデルのノードが無い状態なので、入れ替えを提案できるようにする。
    assert body["update_available"] is True


def test_status_does_not_offer_an_update_when_current(client: TestClient, tmp_path: Path) -> None:
    _fake_provisioned(shared_comfyui_root(), version="0.42.1")
    body = client.get("/api/v1/setup/comfyui/status").json()
    assert body["update_available"] is False


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        # v0.39.0 から、有料APIノードを止めるオプションが分かれた。
        ("0.42.1", ["--disable-partner-nodes", "--offline"]),
        ("0.39.0", ["--disable-partner-nodes", "--offline"]),
        # それより前の版は新しいオプションを知らず、渡すと起動が止まる。
        ("0.38.0", ["--disable-api-nodes"]),
        # 版が読めない（流用した既存インストールなど）ときは、どちらも受け付ける方。
        ("", ["--disable-api-nodes"]),
    ],
)
def test_offline_flags_follow_the_comfyui_version(
    tmp_path: Path, version: str, expected: list[str]
) -> None:
    from chotto_sashie_shared.comfyui_provision import offline_flags

    _fake_provisioned(tmp_path / "comfyui", version=version)
    comfy_dir, _python = managed_comfyui_paths(tmp_path / "comfyui")

    assert offline_flags(comfy_dir) == expected
