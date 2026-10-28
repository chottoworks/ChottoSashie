from pathlib import Path

import pytest

from chotto_sashie_api.database import SCHEMA_VERSION, Database

# 版4で足した画像・動画の版の列。版3までのDBの形へ戻すときに外す。
_IMAGE_REVISION_COLUMNS = (
    ("characters", "portrait_revision"),
    ("background_assets", "image_revision"),
    ("prop_assets", "image_revision"),
    ("still_images", "image_revision"),
    ("shots", "frame_revision"),
    ("shots", "video_revision"),
)


def _drop_image_revisions(connection) -> None:  # noqa: ANN001
    for table, column in _IMAGE_REVISION_COLUMNS:
        connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")


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
    # 版1の形へ戻す（代表写真の範囲の列も、欄へ入れた文の列も、画像の版の列もまだ無い）。
    with database.connect() as connection:
        connection.execute("ALTER TABLE characters DROP COLUMN portrait_crop")
        _drop_image_revisions(connection)
        connection.execute("ALTER TABLE background_assets DROP COLUMN input_prompt")
        connection.execute("ALTER TABLE prop_assets DROP COLUMN input_prompt")
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
            "SELECT name, portrait_crop, portrait_revision FROM characters WHERE id = 'c'"
        ).fetchone()
    assert version == str(SCHEMA_VERSION)
    assert character["name"] == "人物"
    assert character["portrait_crop"] == ""
    assert character["portrait_revision"] == 0


def test_initialize_starts_the_image_versions_of_a_version_3_database(tmp_path: Path) -> None:
    """0.2.1 で作ったDB（版3）には画像の版の列を足し、どの画像も版0から始める。"""
    database = Database(tmp_path / "video-api.sqlite3")
    database.initialize()
    with database.connect() as connection:
        _drop_image_revisions(connection)
        connection.execute("UPDATE schema_meta SET value = '3' WHERE key = 'schema_version'")
        connection.execute(
            "INSERT INTO projects(id, name, created_at, updated_at) VALUES ('p', '作品', 'now', 'now')"
        )
        connection.execute(
            """
            INSERT INTO prop_assets(id, project_id, name, image_path, created_at, updated_at)
            VALUES ('a', 'p', '小物', 'a.png', 'now', 'now')
            """
        )

    database.initialize()

    with database.connect() as connection:
        version = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()["value"]
        for table, column in _IMAGE_REVISION_COLUMNS:
            names = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
            assert column in names, (table, column)
        asset = connection.execute(
            "SELECT image_path, image_revision FROM prop_assets WHERE id = 'a'"
        ).fetchone()
    assert version == str(SCHEMA_VERSION)
    assert asset["image_path"] == "a.png"
    assert asset["image_revision"] == 0


def test_initialize_fills_the_typed_prompt_of_version_2_assets(tmp_path: Path) -> None:
    """版2のDBの背景・小物は、ComfyUI へ渡した指示文から、欄へ入れた文を取り出して埋める。"""
    database = Database(tmp_path / "video-api.sqlite3")
    database.initialize()
    variation_prompt = (
        "Change only the time of day, the light and the weather of the place in image 1. "
        "Keep the same place, the same layout, the same camera position and the same "
        "perspective as image 1. There are no people anywhere in the scene. "
        "作品全体の画風: 淡い水彩"
    )
    with database.connect() as connection:
        _drop_image_revisions(connection)
        connection.execute("ALTER TABLE background_assets DROP COLUMN input_prompt")
        connection.execute("ALTER TABLE prop_assets DROP COLUMN input_prompt")
        connection.execute("UPDATE schema_meta SET value = '2' WHERE key = 'schema_version'")
        connection.execute(
            "INSERT INTO projects(id, name, created_at, updated_at) VALUES ('p', '作品', 'now', 'now')"
        )
        for table, asset_id, source_prompt in [
            ("prop_assets", "styled", "an old mantel clock 作品全体の画風: 淡い水彩"),
            ("prop_assets", "plain", "a red umbrella"),
            ("prop_assets", "uploaded", ""),
            ("background_assets", "variation", variation_prompt),
        ]:
            connection.execute(
                f"""
                INSERT INTO {table}(id, project_id, name, source_prompt, created_at, updated_at)
                VALUES (?, 'p', '素材', ?, 'now', 'now')
                """,
                (asset_id, source_prompt),
            )

    database.initialize()

    with database.connect() as connection:
        typed = {
            row["id"]: row["input_prompt"]
            for table in ("prop_assets", "background_assets")
            for row in connection.execute(f"SELECT id, input_prompt FROM {table}").fetchall()
        }
    assert typed == {
        "styled": "an old mantel clock",
        "plain": "a red umbrella",
        "uploaded": "",
        "variation": "",
    }


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
