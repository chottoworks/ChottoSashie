"""静止画1枚の中での、人物ごとの指定と「どの立ち絵を使うか」。

ここでは**立ち絵を選ぶだけ**で、姿勢・表情・向きは合成のときに文で指定する。

`figure_id` は「この静止画で使う立ち絵」への参照であって、実体の持ち主ではない。
選んだ立ち絵が消えたときは参照だけが外れ（`ON DELETE SET NULL`）、既定の立ち絵へ
戻る。合成済みの静止画の画像には触れない。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from sqlite3 import Row
from typing import TYPE_CHECKING

from .schemas import StillImageCharacterSpec

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

    @property
    def spec(self) -> StillImageCharacterSpec:
        try:
            return StillImageCharacterSpec(**json.loads(self.spec_json))
        except (ValueError, TypeError):
            return StillImageCharacterSpec()


def _to_record(row: Row) -> StillImageCharacterRecord:
    return StillImageCharacterRecord(
        still_image_id=row["still_image_id"],
        character_id=row["character_id"],
        figure_id=row["figure_id"],
        spec_json=row["spec_json"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
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
