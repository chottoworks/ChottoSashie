"""アプリが落ちたあとの後片付け。

親ジョブだけを中断へ倒しても、1枚ごとの記録は別の表にある。倒し忘れると
選別画面に終わらない「生成中」が残るため、そこまで含めて確かめる。
"""

from pathlib import Path

from chotto_sashie_api.character_figures import CharacterFigureRepository
from chotto_sashie_api.database import Database
from chotto_sashie_shared.candidates import CandidateRepository
from chotto_sashie_shared.character_schemas import CharacterCreate, ProjectCreate
from chotto_sashie_shared.jobs import INTERRUPTED_MESSAGE, JobCreate, JobRepository, JobState, JobTransition
from chotto_sashie_shared.profiles import ProfileRepository


def _character(tmp_path: Path):
    database = Database(tmp_path / "recovery.sqlite3")
    database.initialize()
    profiles = ProfileRepository(database)
    project = profiles.create_project(ProjectCreate(name="復旧テスト"))
    character = profiles.create_character(project.id, CharacterCreate(name="ミナ"))
    return database, character


def test_interrupted_batch_leaves_no_running_images(tmp_path: Path) -> None:
    database, character = _character(tmp_path)
    jobs = JobRepository(database)
    job = jobs.create(JobCreate(kind="character.figures"))
    jobs.transition(job.id, JobTransition(state="preparing"))
    jobs.transition(job.id, JobTransition(state="running"))

    candidates = CandidateRepository(database)
    base = candidates.create(
        character_id=character.id,
        batch_job_id=job.id,
        ordinal=1,
        prompt="基準",
        negative_prompt="",
        seed=1,
        width=512,
        height=688,
        base_url="http://127.0.0.1:8190",
    )
    candidates.mark_running(base.id, "prompt-candidate")

    figures = CharacterFigureRepository(database)
    figure = figures.create(character.id, origin="base", job_id=job.id)
    figures.mark_running(figure.id, "prompt-figure")

    # ここでアプリが落ちたことにして、次の起動と同じ後片付けを行う。
    assert jobs.recover_interrupted() == 1
    assert candidates.recover_interrupted(INTERRUPTED_MESSAGE) == 1
    assert figures.recover_interrupted(INTERRUPTED_MESSAGE) == 1

    assert jobs.get(job.id).state is JobState.INTERRUPTED
    assert candidates.get(base.id).state == "failed"
    # 立ち絵も同じ扱い（画面に終わらない「生成中」を残さない）。
    assert figures.get(figure.id).state == "failed"
    assert figures.get(figure.id).error_message == INTERRUPTED_MESSAGE
