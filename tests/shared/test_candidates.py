from pathlib import Path

import pytest

from chotto_sashie_shared.candidates import (
    CandidateRepository,
    CandidateSelectionError,
    build_character_candidate_prompts,
)
from chotto_sashie_api.database import Database
from chotto_sashie_shared.profiles import ProfileRepository
from chotto_sashie_shared.character_schemas import CharacterCreate, ProjectCreate


def make_character(tmp_path: Path):
    database = Database(tmp_path / "candidates.sqlite3")
    database.initialize()
    profiles = ProfileRepository(database)
    project = profiles.create_project(ProjectCreate(name="候補テスト"))
    character = profiles.create_character(
        project.id,
        CharacterCreate(
            name="ミナ",
            face="面長で頬骨が高く、太い直線眉、細い鼻、薄い口",
            hair_style="肩までの波髪",
            hair_color="濃紺",
            eyes="少しつり目",
            eye_color="琥珀色",
            immutable_features=["左目の下の小さなほくろ", "銀色の星形髪留め"],
            art_style="この人物だけ少し硬質な印象",
        ),
    )
    return database, character


def test_candidate_prompts_use_structured_profile(tmp_path: Path) -> None:
    _database, character = make_character(tmp_path)

    prompts = build_character_candidate_prompts(
        character,
        8,
        project_style="淡い水彩、低彩度の小説挿絵",
    )

    assert len(prompts) == 8
    assert len(set(prompts)) == 8
    assert all("front-facing bust portrait" in prompt for prompt in prompts)
    # 顔立ちが指定済みなので、造作を上書きしない描写差分を使う。
    assert "DEFINING FACE" in prompts[0]
    assert "面長で頬骨が高く、太い直線眉、細い鼻、薄い口" in prompts[0]
    assert all("keep the defined face exactly" in prompt for prompt in prompts)
    assert "explore a distinct facial geometry" not in prompts[0]
    # 顔立ちは冒頭付近に置き、反映を強める。
    assert prompts[0].index("DEFINING FACE") < prompts[0].index("illustration style shared")
    assert "illustration style shared by every character" in prompts[0]
    assert "淡い水彩、低彩度の小説挿絵" in prompts[0]
    assert "character-specific nuance" in prompts[0]
    assert "hair color: 濃紺" in prompts[0]
    assert "eye color: 琥珀色" in prompts[0]
    assert "左目の下の小さなほくろ" in prompts[0]
    assert "one person only" in prompts[0]


def test_candidate_prompts_explore_geometry_when_face_unspecified(tmp_path: Path) -> None:
    database = Database(tmp_path / "no-face.sqlite3")
    database.initialize()
    profiles = ProfileRepository(database)
    project = profiles.create_project(ProjectCreate(name="顔未指定"))
    character = profiles.create_character(
        project.id,
        CharacterCreate(name="名無し", hair_color="黒"),
    )

    prompts = build_character_candidate_prompts(character, 8)

    assert len(set(prompts)) == 8
    # 顔立ち未指定なので、候補ごとに造作そのものを探索する。
    assert all("explore a distinct facial geometry" in prompt for prompt in prompts)
    assert "DEFINING FACE" not in prompts[0]
    assert "keep the defined face exactly" not in prompts[0]


def test_candidate_completion_and_base_selection(tmp_path: Path) -> None:
    database, character = make_character(tmp_path)
    repository = CandidateRepository(database)
    first = repository.create(
        character_id=character.id,
        batch_job_id="batch-1",
        ordinal=1,
        prompt="portrait one",
        negative_prompt="text",
        seed=100,
        width=512,
        height=688,
        base_url="http://127.0.0.1:8190",
    )
    second = repository.create(
        character_id=character.id,
        batch_job_id="batch-1",
        ordinal=2,
        prompt="portrait two",
        negative_prompt="text",
        seed=101,
        width=512,
        height=688,
        base_url="http://127.0.0.1:8190",
    )

    with pytest.raises(CandidateSelectionError):
        repository.select_base(first.id)

    repository.mark_running(first.id, "prompt-1")
    repository.complete(
        first.id,
        {"filename": "01.png", "subfolder": "ChottoSashie", "type": "output"},
    )
    repository.mark_running(second.id, "prompt-2")
    repository.complete(
        second.id,
        {"filename": "02.png", "subfolder": "ChottoSashie", "type": "output"},
    )
    repository.select_base(first.id)
    selected = repository.select_base(second.id)
    restored = repository.list_for_character(character.id)

    assert selected.is_base is True
    assert restored[0].is_base is False
    assert restored[1].is_base is True
    assert restored[1].url is not None
