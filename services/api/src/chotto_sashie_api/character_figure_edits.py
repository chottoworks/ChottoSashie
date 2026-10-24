"""立ち絵の変更: いまある立ち絵の衣装・小物を変えた1枚を作り、保存するまで預かる。

変更の結果はすぐには立ち絵へ反映しない。出来上がりを見てから採用させ、採用すると
**元の立ち絵を置き換える**。見ないうちに置き換えると、思った衣装にならなかったときに
元の1枚が戻らない。元も残したいときは、変更する前に立ち絵を複製しておく。

預かっている間の結果は、立ち絵とは別の表に持つ。立ち絵の一覧・既定の判定・静止画の
選択は立ち絵の表だけを見ているので、保存前の結果がそこへ紛れ込まない。1枚の立ち絵に
預かる結果は1つだけで、変更し直すと前の結果は捨てる。
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from sqlite3 import Row
from typing import TYPE_CHECKING
from uuid import uuid4

from .character_figures import ORIGIN_VARIATION, CharacterFigureRecord, figures_dir

if TYPE_CHECKING:  # DBの型は実行時に取り込まない（循環参照）。
    from .database import Database

CHARACTER_FIGURE_EDITS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS character_figure_edits (
    id TEXT PRIMARY KEY,
    -- 変更する立ち絵。立ち絵を消せば、預かっている結果も一緒に消える。
    figure_id TEXT NOT NULL UNIQUE REFERENCES character_figures(id) ON DELETE CASCADE,
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    -- 利用者が書いた指示と、合わせた小物（保存したら立ち絵の記録へ移す）。
    instruction TEXT NOT NULL DEFAULT '',
    prop_asset_ids_json TEXT NOT NULL DEFAULT '[]',
    -- 実際にComfyUIへ送った文。
    prompt TEXT NOT NULL DEFAULT '',
    seed INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'completed', 'failed')),
    job_id TEXT NOT NULL DEFAULT '',
    base_url TEXT NOT NULL DEFAULT '',
    prompt_id TEXT NOT NULL DEFAULT '',
    image_path TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_character_figure_edits_character
ON character_figure_edits(character_id);
"""

_ACTIVE_STATES = ("queued", "running")


def edit_image_path(data_dir: Path, edit_id: str) -> Path:
    """預かっている結果の置き場所。立ち絵と同じフォルダに、名前で分けて置く。"""
    safe_id = edit_id.replace("/", "_").replace("\\", "_")
    return figures_dir(data_dir) / f"edit-{safe_id}.png"


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class FigureEditRecord:
    id: str
    figure_id: str
    character_id: str
    instruction: str
    prop_asset_ids: list[str]
    prompt: str
    seed: int
    state: str
    job_id: str
    base_url: str
    prompt_id: str
    image_path: str
    error_message: str
    created_at: str
    updated_at: str

    @property
    def active(self) -> bool:
        return self.state in _ACTIVE_STATES


class FigureEditNotFoundError(KeyError):
    pass


class FigureEditStateError(ValueError):
    """いまの状態ではできない操作（変更中に捨てる、出来ていない結果を保存する等）。"""


def _to_edit(row: Row) -> FigureEditRecord:
    try:
        prop_ids = json.loads(row["prop_asset_ids_json"])
    except (ValueError, TypeError):
        prop_ids = []
    return FigureEditRecord(
        id=row["id"],
        figure_id=row["figure_id"],
        character_id=row["character_id"],
        instruction=row["instruction"],
        prop_asset_ids=prop_ids if isinstance(prop_ids, list) else [],
        prompt=row["prompt"],
        seed=row["seed"],
        state=row["state"],
        job_id=row["job_id"],
        base_url=row["base_url"],
        prompt_id=row["prompt_id"],
        image_path=row["image_path"],
        error_message=row["error_message"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class FigureEditRepository:
    def __init__(self, database: Database):
        self.database = database

    def start(
        self,
        figure: CharacterFigureRecord,
        *,
        instruction: str,
        prop_asset_ids: list[str],
        prompt: str,
        seed: int,
        base_url: str,
    ) -> tuple[FigureEditRecord, Path | None]:
        """この立ち絵の変更を始める。前に預かっていた結果は捨て、その画像の場所を返す。

        変更中のものがあれば断る（同じ立ち絵へ2つの結果を同時に作らない）。
        """
        edit_id = str(uuid4())
        timestamp = _now()
        with self.database.connect() as connection:
            previous = connection.execute(
                "SELECT state, image_path FROM character_figure_edits WHERE figure_id = ?",
                (figure.id,),
            ).fetchone()
            if previous is not None and previous["state"] in _ACTIVE_STATES:
                raise FigureEditStateError("この立ち絵は変更中です。終わるか中止してから変更し直してください")
            connection.execute("DELETE FROM character_figure_edits WHERE figure_id = ?", (figure.id,))
            connection.execute(
                """
                INSERT INTO character_figure_edits(
                    id, figure_id, character_id, instruction, prop_asset_ids_json, prompt,
                    seed, state, base_url, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (
                    edit_id,
                    figure.id,
                    figure.character_id,
                    instruction,
                    json.dumps(prop_asset_ids, ensure_ascii=False),
                    prompt,
                    seed,
                    base_url,
                    timestamp,
                    timestamp,
                ),
            )
        previous_path = (
            Path(previous["image_path"]) if previous is not None and previous["image_path"] else None
        )
        return self.get(edit_id), previous_path

    def get(self, edit_id: str) -> FigureEditRecord:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM character_figure_edits WHERE id = ?", (edit_id,)
            ).fetchone()
        if row is None:
            raise FigureEditNotFoundError(edit_id)
        return _to_edit(row)

    def for_figure(self, figure_id: str) -> FigureEditRecord | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM character_figure_edits WHERE figure_id = ?", (figure_id,)
            ).fetchone()
        return _to_edit(row) if row is not None else None

    def by_figure_for_character(self, character_id: str) -> dict[str, FigureEditRecord]:
        """人物の立ち絵それぞれに預かっている結果（立ち絵IDで引く）。"""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM character_figure_edits WHERE character_id = ?", (character_id,)
            ).fetchall()
        return {row["figure_id"]: _to_edit(row) for row in rows}

    def set_job(self, edit_id: str, job_id: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE character_figure_edits SET job_id = ?, updated_at = ? WHERE id = ?",
                (job_id, _now(), edit_id),
            )

    def mark_running(self, edit_id: str, prompt_id: str) -> FigureEditRecord:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE character_figure_edits
                SET state = 'running', prompt_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (prompt_id, _now(), edit_id),
            )
        return self.get(edit_id)

    def complete(self, edit_id: str, image_path: Path) -> FigureEditRecord:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE character_figure_edits
                SET state = 'completed', image_path = ?, error_message = '', updated_at = ?
                WHERE id = ?
                """,
                (str(image_path), _now(), edit_id),
            )
        return self.get(edit_id)

    def fail(self, edit_id: str, message: str) -> None:
        """終わっていない結果だけを失敗にする（出来上がったものは上書きしない）。"""
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE character_figure_edits
                SET state = 'failed', error_message = ?, updated_at = ?
                WHERE id = ? AND state IN ('queued', 'running')
                """,
                (message[:1000], _now(), edit_id),
            )

    def recover_interrupted(self, message: str) -> int:
        """前回終了時に変更中だった結果を失敗へ倒す（終わらない「変更中」を残さない）。"""
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE character_figure_edits
                SET state = 'failed', error_message = ?, updated_at = ?
                WHERE state IN ('queued', 'running')
                """,
                (message[:1000], _now()),
            )
        return cursor.rowcount

    def discard(self, edit_id: str) -> Path | None:
        """預かっている結果を捨て、画像の場所を返す（ファイルは呼び出し側が片付ける）。"""
        edit = self.get(edit_id)
        if edit.active:
            raise FigureEditStateError("変更中の結果は捨てられません。先に中止してください")
        with self.database.connect() as connection:
            connection.execute("DELETE FROM character_figure_edits WHERE id = ?", (edit_id,))
        return Path(edit.image_path) if edit.image_path else None

    def save(self, edit_id: str, *, data_dir: Path) -> str:
        """出来上がった結果を採用し、元の立ち絵を置き換える。立ち絵のIDを返す。

        IDと見出し・既定の印はそのまま、画像と作り方の記録だけを入れ替える。元の立ち絵も
        残したいときは、変更する前に複製しておく（変更は置き換えだけにして、迷わせない）。

        画像は先に新しい名前で書いてから記録を付け替え、古い画像は最後に消す。途中で
        落ちても、記録が指している画像が欠けることはない（残ったファイルは起動時に片付く）。
        """
        edit = self.get(edit_id)
        if edit.state != "completed" or not edit.image_path or not Path(edit.image_path).is_file():
            raise FigureEditStateError("まだ出来上がっていない変更は保存できません")

        destination = figures_dir(data_dir) / f"{edit.figure_id}-{uuid4().hex[:8]}.png"
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        try:
            shutil.copyfile(edit.image_path, temporary)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

        timestamp = _now()
        try:
            with self.database.connect() as connection:
                figure = connection.execute(
                    "SELECT * FROM character_figures WHERE id = ?", (edit.figure_id,)
                ).fetchone()
                if figure is None:
                    raise FigureEditNotFoundError(edit.figure_id)
                replaced_path = Path(figure["image_path"]) if figure["image_path"] else None
                connection.execute(
                    """
                    UPDATE character_figures
                    SET origin = ?, prop_asset_ids_json = ?, instruction = ?, prompt = ?,
                        seed = ?, image_path = ?, error_message = '', updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        ORIGIN_VARIATION,
                        json.dumps(edit.prop_asset_ids, ensure_ascii=False),
                        edit.instruction,
                        edit.prompt,
                        edit.seed,
                        str(destination),
                        timestamp,
                        edit.figure_id,
                    ),
                )
                connection.execute("DELETE FROM character_figure_edits WHERE id = ?", (edit_id,))
        except Exception:
            destination.unlink(missing_ok=True)
            raise

        Path(edit.image_path).unlink(missing_ok=True)
        if replaced_path is not None and replaced_path != destination:
            replaced_path.unlink(missing_ok=True)
        return edit.figure_id
