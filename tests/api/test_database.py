from pathlib import Path

import pytest

from chotto_sashie_api.database import SCHEMA_VERSION, Database


def test_initialize_creates_expected_tables(tmp_path: Path) -> None:
    database = Database(tmp_path / "video-api.sqlite3")
    database.initialize()

    with database.connect() as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        version = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()["value"]

    assert {
        "jobs",
        "projects",
        "characters",
        "background_assets",
        "prop_assets",
        "still_images",
        "shots",
        # 顔を決めるための人物候補。
        "character_candidates",
        # 静止画ごとの人物（どの立ち絵を使うか・姿勢の指定）。
        "still_image_characters",
        # 立ち絵（人物の実体。合成はここから選んだ1枚を使う）。
        "character_figures",
    }.issubset(tables)
    assert version == str(SCHEMA_VERSION)


def test_initialize_is_idempotent(tmp_path: Path) -> None:
    database = Database(tmp_path / "video-api.sqlite3")
    database.initialize()
    database.initialize()  # 二回目も例外を出さずに成功する

    with database.connect() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    assert row["count"] == 1


def test_initialize_brings_a_version_1_database_up_to_date(tmp_path: Path) -> None:
    """0.1.0 で作ったDB（版1）も、起動したときに今の形へ追いつき、中身は残る。"""
    database = Database(tmp_path / "video-api.sqlite3")
    database.initialize()
    # 版1の形へ戻す（代表写真の範囲の列はまだ無い）。
    with database.connect() as connection:
        connection.execute("ALTER TABLE characters DROP COLUMN portrait_crop")
        connection.execute("UPDATE schema_meta SET value = '1' WHERE key = 'schema_version'")
        connection.execute(
            "INSERT INTO projects(id, name, created_at, updated_at) VALUES ('p', '作品', 'now', 'now')"
        )
        connection.execute(
            """
            INSERT INTO characters(id, project_id, name, created_at, updated_at)
            VALUES ('c', 'p', '人物', 'now', 'now')
            """
        )

    database.initialize()

    with database.connect() as connection:
        version = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()["value"]
        character = connection.execute(
            "SELECT name, portrait_crop FROM characters WHERE id = 'c'"
        ).fetchone()
    assert version == str(SCHEMA_VERSION)
    assert character["name"] == "人物"
    assert character["portrait_crop"] == ""


def test_initialize_refuses_a_database_from_a_newer_app(tmp_path: Path) -> None:
    """新しい版のアプリで作ったDBは、知らない形のまま書き換えない。"""
    database = Database(tmp_path / "video-api.sqlite3")
    database.initialize()
    with database.connect() as connection:
        connection.execute(
            "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION + 1),),
        )

    with pytest.raises(RuntimeError):
        database.initialize()
