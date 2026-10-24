from pathlib import Path

import pytest

from chotto_sashie_api.database import Database
from chotto_sashie_shared.profiles import (
    CharacterNotFoundError,
    ProfileRepository,
    ProjectNotFoundError,
)
from chotto_sashie_shared.character_schemas import (
    CharacterCreate,
    CharacterUpdate,
    ProjectCreate,
    ProjectUpdate,
)
from pydantic import ValidationError


def make_repository(tmp_path: Path) -> ProfileRepository:
    database = Database(tmp_path / "profiles.sqlite3")
    database.initialize()
    return ProfileRepository(database)


def test_project_and_character_profile_round_trip(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    project = repository.create_project(
        ProjectCreate(
            name="星降る図書館",
            description="長編ファンタジー",
            illustration_style="淡い水彩の小説挿絵",
        )
    )
    character = repository.create_character(
        project.id,
        CharacterCreate(
            name="ミナ",
            gender_age="女性・10代後半",
            face="卵型の顔、そばかす",
            hair_style="肩までのゆるい波髪",
            hair_color="黒",
            eyes="少しつり目",
            eye_color="琥珀色",
            immutable_features=["左目の下の小さなほくろ", "銀色の星形髪留め", "銀色の星形髪留め"],
            art_style="繊細なライトノベル挿絵",
        ),
    )

    restored = repository.get_character(character.id)
    projects = repository.list_projects()
    characters = repository.list_characters(project.id)

    assert restored.hair_color == "黒"
    assert restored.eye_color == "琥珀色"
    assert restored.immutable_features == ["左目の下の小さなほくろ", "銀色の星形髪留め"]
    assert projects[0].illustration_style == "淡い水彩の小説挿絵"
    assert projects[0].character_count == 1
    assert characters == [restored]


def test_character_update_preserves_unset_fields(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    project = repository.create_project(ProjectCreate(name="更新テスト"))
    character = repository.create_character(
        project.id,
        CharacterCreate(name="ユウ", hair_color="青", eye_color="灰色"),
    )

    updated = repository.update_character(
        character.id,
        CharacterUpdate(hair_color="濃紺", immutable_features=["右耳のイヤーカフ"]),
    )

    assert updated.hair_color == "濃紺"
    assert updated.eye_color == "灰色"
    assert updated.immutable_features == ["右耳のイヤーカフ"]


def test_unknown_project_and_character_raise(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)

    with pytest.raises(ProjectNotFoundError):
        repository.list_characters("missing")
    with pytest.raises(CharacterNotFoundError):
        repository.get_character("missing")


def test_project_style_update_preserves_other_fields(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    project = repository.create_project(
        ProjectCreate(name="画風テスト", description="説明", illustration_style="線画")
    )

    updated = repository.update_project(
        project.id,
        ProjectUpdate(illustration_style="淡い水彩、低彩度"),
    )

    assert updated.name == "画風テスト"
    assert updated.description == "説明"
    assert updated.illustration_style == "淡い水彩、低彩度"


def test_character_tables_are_wired_to_projects(tmp_path: Path) -> None:
    """人物候補と立ち絵が、作品を消したときに人物と一緒に消える形になっている。"""
    database = Database(tmp_path / "schema.sqlite3")
    database.initialize()

    with database.connect() as connection:
        foreign_keys = connection.execute("PRAGMA foreign_key_list(characters)").fetchall()
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    assert foreign_keys[0]["table"] == "projects"
    assert foreign_keys[0]["on_delete"] == "CASCADE"
    assert {"character_candidates", "character_figures"} <= tables


def test_whitespace_only_names_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ProjectCreate(name="   ")
    with pytest.raises(ValidationError):
        CharacterCreate(name="\t")
