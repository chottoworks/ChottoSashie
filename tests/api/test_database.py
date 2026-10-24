from pathlib import Path

from chotto_sashie_api.database import Database


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
    assert version == "1"


def test_initialize_is_idempotent(tmp_path: Path) -> None:
    database = Database(tmp_path / "video-api.sqlite3")
    database.initialize()
    database.initialize()  # 二回目も例外を出さずに成功する

    with database.connect() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    assert row["count"] == 1
