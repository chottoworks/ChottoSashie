"""静止画1枚の中での、人物ごとの指定と「どの立ち絵を使うか」。

置いて合成する静止画では、ここが**この静止画用の人物の姿**（ポーズ・向き・表情を付けて
切り抜いた1枚）と、背景の上の位置も持つ。姿はこの記録の持ち物で、立ち絵の一覧には
足さない（静止画ごとに作り直すもので、ほかの静止画では使わない）。

`figure_id` は「この静止画で使う立ち絵」への参照であって、実体の持ち主ではない。
選んだ立ち絵が消えたときは参照だけが外れ（`ON DELETE SET NULL`）、既定の立ち絵へ
戻る。合成済みの静止画の画像には触れない。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from sqlite3 import Row
from typing import TYPE_CHECKING

from .schemas import StillImageCharacterLayout, StillImageCharacterSpec

if TYPE_CHECKING:  # スキーマ定義をここに置くので、DBの型は実行時に取り込まない。
    from .database import Database

STILL_IMAGE_CHARACTERS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS still_image_characters (
    still_image_id TEXT NOT NULL REFERENCES still_images(id) ON DELETE CASCADE,
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    -- この静止画で使う立ち絵。選んでいなければ NULL で、既定の立ち絵を使う。
    -- 立ち絵が消えても静止画は残す（参照が外れるだけ）。
    figure_id TEXT REFERENCES character_figures(id) ON DELETE SET NULL,
    -- 姿勢・表情・向き・配置・覚え書き。合成の指示文へこの人物の分として入る。
    spec_json TEXT NOT NULL DEFAULT '{}',
    -- 置いて合成するときの姿（切り抜いた透過PNG）。この記録の持ち物。
    pose_image_path TEXT NOT NULL DEFAULT '',
    -- 姿を書くたびに1つ増やす（画像の版）。
    pose_revision INTEGER NOT NULL DEFAULT 0,
    -- 姿をどの立ち絵とどの指定から作ったか。今の立ち絵・指定と違えば作り直しが要る。
    -- 立ち絵は消えることがあるので、参照ではなく覚え書きとして持つ。
    pose_figure_id TEXT NOT NULL DEFAULT '',
    pose_spec_json TEXT NOT NULL DEFAULT '',
    -- 姿を作るときに ComfyUI へ渡した編集の指示文。指定が空で編集しなかったときは空、
    -- 記録が無ければ NULL。
    pose_prompt TEXT,
    -- 最後に姿を作り始めたジョブ。終わるまで合成させない。ジョブの記録が消えていれば
    -- 終わったものとして扱う（覚え書きなので外部キーにはしない）。
    pose_job_id TEXT NOT NULL DEFAULT '',
    -- 背景の上の位置（足もとの位置と身長を背景に対する割合で）。空なら既定の並び。
    layout_json TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(still_image_id, character_id)
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class StillImageCharacterRecord:
    still_image_id: str
    character_id: str
    figure_id: str | None
    spec_json: str
    created_at: str
    updated_at: str
    pose_image_path: str = ""
    pose_revision: int = 0
    pose_figure_id: str = ""
    pose_spec_json: str = ""
    pose_prompt: str | None = None
    pose_job_id: str = ""
    layout_json: str = ""

    @property
    def spec(self) -> StillImageCharacterSpec:
        return _spec(self.spec_json) or StillImageCharacterSpec()

    @property
    def pose_spec(self) -> StillImageCharacterSpec | None:
        """姿を作ったときの指定。姿が無ければ None。"""
        return _spec(self.pose_spec_json) if self.pose_image_path else None

    @property
    def layout(self) -> StillImageCharacterLayout | None:
        """保存した位置。まだ置いていなければ None（呼び出し側で既定の並びを使う）。"""
        if not self.layout_json:
            return None
        try:
            return StillImageCharacterLayout(**json.loads(self.layout_json))
        except (ValueError, TypeError):
            return None


def _spec(spec_json: str) -> StillImageCharacterSpec | None:
    if not spec_json:
        return None
    try:
        return StillImageCharacterSpec(**json.loads(spec_json))
    except (ValueError, TypeError):
        return None


def _to_record(row: Row) -> StillImageCharacterRecord:
    return StillImageCharacterRecord(
        still_image_id=row["still_image_id"],
        character_id=row["character_id"],
        figure_id=row["figure_id"],
        spec_json=row["spec_json"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        pose_image_path=row["pose_image_path"],
        pose_revision=row["pose_revision"],
        pose_figure_id=row["pose_figure_id"],
        pose_spec_json=row["pose_spec_json"],
        pose_prompt=row["pose_prompt"],
        pose_job_id=row["pose_job_id"],
        layout_json=row["layout_json"],
    )


class StillImageCharacterRepository:
    def __init__(self, database: Database):
        self.database = database

    def get(self, still_image_id: str, character_id: str) -> StillImageCharacterRecord | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM still_image_characters
                WHERE still_image_id = ? AND character_id = ?
                """,
                (still_image_id, character_id),
            ).fetchone()
        return _to_record(row) if row else None

    def upsert(
        self,
        still_image_id: str,
        character_id: str,
        spec: StillImageCharacterSpec,
        figure_id: str | None,
    ) -> StillImageCharacterRecord:
        """指定と立ち絵の選択を保存する。`figure_id` の None は「既定の立ち絵を使う」。"""
        spec_json = json.dumps(spec.model_dump(), ensure_ascii=False)
        timestamp = _now()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO still_image_characters(
                    still_image_id, character_id, figure_id, spec_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(still_image_id, character_id) DO UPDATE SET
                    figure_id = excluded.figure_id,
                    spec_json = excluded.spec_json,
                    updated_at = excluded.updated_at
                """,
                (still_image_id, character_id, figure_id, spec_json, timestamp, timestamp),
            )
        return self.get(still_image_id, character_id)  # type: ignore[return-value]

    def set_pose_job(self, still_image_id: str, character_id: str, job_id: str) -> None:
        """姿を作るジョブを結び付ける。行は指定の保存（upsert）で先に作ってある。"""
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE still_image_characters SET pose_job_id = ?, updated_at = ?
                WHERE still_image_id = ? AND character_id = ?
                """,
                (job_id, _now(), still_image_id, character_id),
            )

    def set_pose(
        self,
        still_image_id: str,
        character_id: str,
        *,
        image_path: Path,
        figure_id: str,
        spec: StillImageCharacterSpec,
        prompt: str,
    ) -> bool:
        """出来た姿と、そのとき ComfyUI へ渡した編集の指示文を書く。

        行が消えていれば（静止画や人物を消した）何もしないで False。
        """
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE still_image_characters
                SET pose_image_path = ?, pose_revision = pose_revision + 1,
                    pose_figure_id = ?, pose_spec_json = ?, pose_prompt = ?, updated_at = ?
                WHERE still_image_id = ? AND character_id = ?
                """,
                (
                    str(image_path),
                    figure_id,
                    json.dumps(spec.model_dump(), ensure_ascii=False),
                    prompt,
                    _now(),
                    still_image_id,
                    character_id,
                ),
            )
        return cursor.rowcount == 1

    def set_layout(
        self, still_image_id: str, character_id: str, layout: StillImageCharacterLayout
    ) -> None:
        timestamp = _now()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO still_image_characters(
                    still_image_id, character_id, layout_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(still_image_id, character_id) DO UPDATE SET
                    layout_json = excluded.layout_json,
                    updated_at = excluded.updated_at
                """,
                (
                    still_image_id,
                    character_id,
                    json.dumps(layout.model_dump()),
                    timestamp,
                    timestamp,
                ),
            )
