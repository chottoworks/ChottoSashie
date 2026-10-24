"""アンインストールで「アプリデータも削除」を選んだときの片付けを確かめる。"""

import json
from pathlib import Path

from chotto_sashie_shared.data_removal import APP_ENTRIES, main, remove_app_data


def _app_data(root: Path) -> Path:
    """このアプリの保存先らしい中身を作る。"""
    (root / "database").mkdir(parents=True)
    (root / "database" / "app.sqlite3").write_bytes(b"db")
    for name in ("assets/background_assets", "keyframes", "managed/runtime/comfy/output"):
        folder = root / name
        folder.mkdir(parents=True)
        (folder / "x.png").write_bytes(b"x")
    return root


def test_everything_the_app_made_is_removed(tmp_path: Path) -> None:
    data_dir = _app_data(tmp_path / "ChottoSashie")

    result = remove_app_data(data_dir)

    assert result.found is True
    assert result.failed == []
    assert result.removed_folder is True
    assert not data_dir.exists()


def test_files_the_user_put_there_are_kept(tmp_path: Path) -> None:
    """あとから利用者が置いたものには触れず、そのときはフォルダも残す。"""
    data_dir = _app_data(tmp_path / "works")
    mine = data_dir / "メモ.txt"
    mine.write_text("残す", encoding="utf-8")

    result = remove_app_data(data_dir)

    assert mine.is_file()
    assert result.kept == ["メモ.txt"]
    assert result.removed_folder is False
    assert not any((data_dir / name).exists() for name in APP_ENTRIES)


def test_a_folder_without_the_apps_database_is_left_alone(tmp_path: Path) -> None:
    """このアプリの保存先と確かめられないフォルダは、指し間違いを疑って何もしない。"""
    folder = tmp_path / "Documents"
    (folder / "assets").mkdir(parents=True)
    (folder / "assets" / "photo.png").write_bytes(b"x")

    result = remove_app_data(folder)

    assert result.found is False
    assert (folder / "assets" / "photo.png").is_file()


def test_command_line_reports_the_result_as_json(tmp_path: Path, capsys) -> None:
    data_dir = _app_data(tmp_path / "ChottoSashie")

    assert main(["--data-dir", str(data_dir)]) == 0

    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert result["found"] is True
    assert result["removed_folder"] is True
