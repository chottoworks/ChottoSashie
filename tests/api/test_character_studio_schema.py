"""人物まわりの土台（テーブルと列）が入っているかを確かめる。"""

import sqlite3
from pathlib import Path

from chotto_sashie_shared.character_schemas import CharacterCreate, ProjectCreate
from chotto_sashie_shared.character_studio import CHARACTER_PROFILE_COLUMNS
from chotto_sashie_shared.profiles import ProfileRepository
from chotto_sashie_api.characters import CharacterRepository
from chotto_sashie_api.database import Database


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}


def test_new_install_has_character_tables(tmp_path: Path) -> None:
    database = Database(tmp_path / "video.sqlite3")
    database.initialize()
    with database.connect() as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {"character_candidates", "character_figures"} <= tables
        # 学習まわりの記録は残さない（人物の実体は立ち絵）。
        assert not tables & {
            "reference_images",
            "character_training_datasets",
            "character_model_reviews",
            "character_state_images",
        }
        assert {name for name, _ in CHARACTER_PROFILE_COLUMNS} <= _columns(connection, "characters")
        assert "illustration_style" in _columns(connection, "projects")


def test_shared_repositories_work_on_video_database(tmp_path: Path) -> None:
    """挿絵ツールと同じ共有リポジトリで、人物を作成・取得できる。"""
    database = Database(tmp_path / "video.sqlite3")
    database.initialize()
    profiles = ProfileRepository(database)
    project = profiles.create_project(ProjectCreate(name="動画作品", illustration_style="淡い水彩"))
    character = profiles.create_character(
        project.id,
        CharacterCreate(name="ミナ", hair_color="濃紺", immutable_features=["銀色の星形髪留め"]),
    )

    assert profiles.get_character(character.id).hair_color == "濃紺"
    assert profiles.get_character(character.id).immutable_features == ["銀色の星形髪留め"]
    # このアプリ固有の見え方（立ち絵の枚数）も同じ人物として扱える。
    listed = CharacterRepository(database).list(project.id)
    assert [item.name for item in listed] == ["ミナ"]
    assert listed[0].figure_count == 0
