"""保存先を変えるときの、選べるやり方と実際の移動を確かめる。

フォルダを移すだけでは足りない。生成物の置き場所は絶対パスでDBに入っているため、
書き換えないと「記録はあるのに開けない」状態になる。ここが壊れると作品が失われる
ように見えるので、経路ごとに見張る。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from chotto_sashie_shared.data_location import (
    MODE_ADOPT,
    MODE_FRESH,
    MODE_MIGRATE,
    DataLocationError,
    ProgressReporter,
    apply_change,
    inspect_directory,
    plan_move,
    rewrite_database_paths,
)


def _make_data_dir(root: Path, *, image_root: Path | None = None) -> Path:
    """作品が入っている保存先を作る（DBと生成物）。"""
    where = image_root or root
    (root / "database").mkdir(parents=True)
    (root / "outputs" / "candidates").mkdir(parents=True)
    (root / "outputs" / "candidates" / "a.png").write_bytes(b"png")
    connection = sqlite3.connect(root / "database" / "app.sqlite3")
    connection.execute("CREATE TABLE still_images (id TEXT, image_path TEXT)")
    connection.execute("CREATE TABLE jobs (id TEXT, payload_json TEXT)")
    connection.execute(
        "INSERT INTO still_images VALUES ('s1', ?)",
        (str(where / "outputs" / "candidates" / "a.png"),),
    )
    connection.execute(
        "INSERT INTO jobs VALUES ('j1', ?)",
        (json.dumps({"output": str(where / "outputs" / "candidates")}),),
    )
    connection.commit()
    connection.close()
    return root


def test_moving_to_an_empty_folder_carries_the_data_and_fixes_the_paths(tmp_path: Path) -> None:
    """移したあとも、DBが指す先が実物と合っていること。"""
    source = _make_data_dir(tmp_path / "old")
    target = tmp_path / "new"

    result = apply_change(source, target, MODE_MIGRATE)

    assert result["mode"] == MODE_MIGRATE
    assert not source.exists()
    assert (target / "outputs" / "candidates" / "a.png").is_file()

    connection = sqlite3.connect(target / "database" / "app.sqlite3")
    stored = connection.execute("SELECT image_path FROM still_images").fetchone()[0]
    payload = connection.execute("SELECT payload_json FROM jobs").fetchone()[0]
    connection.close()

    # 記録が指す先が、実際に読める場所であること（ここが本題）。
    assert Path(stored).is_file()
    assert str(target) in stored
    assert str(tmp_path / "old") not in stored
    # JSON列の中の絶対パスも直っていること（区切りがエスケープされている）。
    assert str(target) in json.loads(payload)["output"]


def test_starting_fresh_leaves_the_current_data_alone(tmp_path: Path) -> None:
    """「新しく始める」は、今のデータに触らない。"""
    source = _make_data_dir(tmp_path / "old")
    target = tmp_path / "new"

    result = apply_change(source, target, MODE_FRESH)

    assert result["mode"] == MODE_FRESH
    assert (source / "database" / "app.sqlite3").is_file()
    assert target.is_dir()
    assert inspect_directory(target).is_empty


def test_a_folder_that_already_has_works_can_only_be_used_as_is(tmp_path: Path) -> None:
    """すでに作品がある場所は、上書きも移動もさせない。そのまま使う道だけ残す。"""
    source = _make_data_dir(tmp_path / "old")
    target = _make_data_dir(tmp_path / "other")

    plan = plan_move(source, target)

    assert plan.available_modes == [MODE_ADOPT]
    assert MODE_MIGRATE in plan.blocked
    with pytest.raises(DataLocationError, match="すでに作品があります"):
        apply_change(source, target, MODE_MIGRATE)
    # そのまま使う分には、どちらのデータも触らない。
    apply_change(source, target, MODE_ADOPT)
    assert (source / "database" / "app.sqlite3").is_file()
    assert (target / "database" / "app.sqlite3").is_file()


def test_a_folder_with_other_files_gets_its_own_folder_inside(tmp_path: Path) -> None:
    """ほかのファイルが入ったフォルダを選ぶと、その中に ChottoSashie を作って使う。

    普段使いのフォルダへ直にばらまかず、まとめておく（混ざると片付けられない）。
    """
    source = _make_data_dir(tmp_path / "old")
    chosen = tmp_path / "busy"
    chosen.mkdir()
    (chosen / "memo.txt").write_text("別の用事", encoding="utf-8")

    plan = plan_move(source, chosen)

    assert plan.requested == str(chosen)
    assert plan.target == str(chosen / "ChottoSashie")
    assert set(plan.available_modes) == {MODE_MIGRATE, MODE_FRESH}

    apply_change(source, chosen, MODE_MIGRATE)

    assert (chosen / "ChottoSashie" / "database" / "app.sqlite3").is_file()
    assert (chosen / "memo.txt").read_text(encoding="utf-8") == "別の用事"
    assert not (chosen / "database").exists()


def test_an_earlier_chottosashie_folder_with_works_is_offered_as_is(tmp_path: Path) -> None:
    """選んだフォルダの中に、前に作った ChottoSashie（作品入り）があれば、それを使える。"""
    source = _make_data_dir(tmp_path / "old")
    chosen = tmp_path / "busy"
    chosen.mkdir()
    (chosen / "memo.txt").write_text("別の用事", encoding="utf-8")
    _make_data_dir(chosen / "ChottoSashie")

    plan = plan_move(source, chosen)

    assert plan.target == str(chosen / "ChottoSashie")
    assert plan.available_modes == [MODE_ADOPT]


def test_a_chottosashie_folder_with_other_files_is_refused(tmp_path: Path) -> None:
    """中に作る ChottoSashie にまで別のファイルがあれば、混ぜずに断る。"""
    source = _make_data_dir(tmp_path / "old")
    chosen = tmp_path / "busy"
    (chosen / "ChottoSashie").mkdir(parents=True)
    (chosen / "ChottoSashie" / "memo.txt").write_text("別の用事", encoding="utf-8")

    plan = plan_move(source, chosen)

    assert plan.available_modes == []
    assert "ほかのファイル" in plan.blocked[MODE_MIGRATE]


def test_choosing_the_folder_that_holds_the_current_one_is_the_same_place(tmp_path: Path) -> None:
    """いまの保存先（…/ChottoSashie）の親を選ぶと、行き先はいまの保存先そのものになる。"""
    source = _make_data_dir(tmp_path / "ChottoSashie")

    plan = plan_move(source, tmp_path)

    assert plan.available_modes == []
    assert "同じ場所" in plan.blocked[MODE_MIGRATE]


def test_the_current_folder_itself_cannot_be_chosen(tmp_path: Path) -> None:
    """今の保存先の中は選ばせない（自分の中へ自分を移すことになる）。"""
    source = _make_data_dir(tmp_path / "old")

    plan = plan_move(source, source / "inner")

    assert plan.available_modes == []
    assert "今の保存先の中" in plan.blocked[MODE_MIGRATE]


def test_rewrite_only_touches_the_old_location(tmp_path: Path) -> None:
    """関係のない絶対パス（共有フォルダなど）は書き換えない。"""
    root = _make_data_dir(tmp_path / "old")
    database = root / "database" / "app.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO still_images VALUES ('s2', ?)", (str(tmp_path / "shared" / "models" / "x"),)
    )
    connection.commit()
    connection.close()

    rewrite_database_paths(database, tmp_path / "old", tmp_path / "new")

    connection = sqlite3.connect(database)
    rows = dict(connection.execute("SELECT id, image_path FROM still_images").fetchall())
    connection.close()
    assert str(tmp_path / "new") in rows["s1"]
    # 共有フォルダの場所は、保存先を移しても変わらない。
    assert rows["s2"] == str(tmp_path / "shared" / "models" / "x")


def test_moving_across_drives_copies_first_and_only_then_deletes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """別ドライブへの移動は、複製が揃ったことを確かめてから元を消す。"""
    from chotto_sashie_shared import data_location

    source = _make_data_dir(tmp_path / "old")
    target = tmp_path / "new"
    # 同じ一時フォルダのままでは付け替えになってしまうので、別ドライブに見せる。
    monkeypatch.setattr(
        data_location, "_drive_of", lambda path: "c:" if "old" in str(path) else "d:"
    )

    apply_change(source, target, MODE_MIGRATE)

    assert not source.exists()
    assert (target / "outputs" / "candidates" / "a.png").read_bytes() == b"png"


def test_a_short_copy_across_drives_leaves_the_original_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """複製が元と合わなければ、元は消さない。

    ここで消してしまうと、途中で失敗したときに何も残らない。
    """
    from chotto_sashie_shared import data_location

    source = _make_data_dir(tmp_path / "old")
    target = tmp_path / "new"
    monkeypatch.setattr(
        data_location, "_drive_of", lambda path: "c:" if "old" in str(path) else "d:"
    )

    def short_copy(src, dst, _progress=None):
        # 1ファイルだけ写して、途中で終わったことにする。
        Path(dst).mkdir(parents=True, exist_ok=True)
        (Path(dst) / "half.png").write_bytes(b"x")

    monkeypatch.setattr(data_location, "copy_tree", short_copy)

    with pytest.raises(DataLocationError, match="一致しません"):
        apply_change(source, target, MODE_MIGRATE)

    assert (source / "database" / "app.sqlite3").is_file()
    assert (source / "outputs" / "candidates" / "a.png").is_file()


def test_a_cross_drive_move_reports_how_far_it_has_come(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """複製の間、写した量が全体量に向かって増えていくこと。

    ここが出ないと、数十GBの移動中に画面が黙ったままになり、止まったのか進んで
    いるのか分からない。
    """
    from chotto_sashie_shared import data_location

    source = _make_data_dir(tmp_path / "old")
    (source / "outputs" / "candidates" / "big.png").write_bytes(b"y" * 9_000_000)
    target = tmp_path / "new"
    monkeypatch.setattr(
        data_location, "_drive_of", lambda path: "c:" if "old" in str(path) else "d:"
    )

    lines: list[str] = []

    class _Collector:
        def write(self, text: str) -> int:
            lines.append(text)
            return len(text)

        def flush(self) -> None:
            pass

    before = inspect_directory(source)
    apply_change(source, target, MODE_MIGRATE, ProgressReporter(_Collector(), interval_seconds=0))

    events = [json.loads(line)["progress"] for line in lines if line.strip()]
    phases = [event["phase"] for event in events]
    assert phases[0] == "scan"
    assert phases[-1] == "done"
    for phase in ("copy", "verify", "cleanup", "rewrite"):
        assert phase in phases

    copying = [event for event in events if event["phase"] == "copy"]
    # 全体量は下見で数えた分。写した量はそこへ向かって増え、最後は追いつく。
    assert copying[0]["total_bytes"] == before.total_bytes
    assert copying[0]["total_files"] == before.file_count
    assert [event["copied_bytes"] for event in copying] == sorted(
        event["copied_bytes"] for event in copying
    )
    assert copying[-1]["copied_bytes"] == before.total_bytes
    assert copying[-1]["copied_files"] == before.file_count
    # 大きな1ファイルの途中でも進む（写し終わりの2回だけにならない）。
    assert len({event["copied_bytes"] for event in copying}) > 2


def test_a_same_drive_move_still_says_what_it_is_doing(tmp_path: Path) -> None:
    """付け替えは一瞬でも、段階は伝える（複製の全体量は出ない）。"""
    source = _make_data_dir(tmp_path / "old")
    target = tmp_path / "new"

    lines: list[str] = []

    class _Collector:
        def write(self, text: str) -> int:
            lines.append(text)
            return len(text)

        def flush(self) -> None:
            pass

    apply_change(source, target, MODE_MIGRATE, ProgressReporter(_Collector()))

    phases = [json.loads(line)["progress"]["phase"] for line in lines if line.strip()]
    assert phases == ["scan", "relink", "rewrite", "done"]
