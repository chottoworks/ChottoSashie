"""登場人物の最小CRUD。

人物の実体は立ち絵（`character_figures`）で、ここが持つのは名前・同一性キャプションと、
一覧に出す代表写真だけ。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from sqlite3 import Row
from uuid import uuid4

from .database import Database
from .projects import ProjectRepository
from .schemas import CharacterCreate, CharacterRead, CharacterUpdate


class CharacterNotFoundError(KeyError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _to_character(row: Row) -> CharacterRead:
    columns = set(row.keys())
    # 代表写真（一覧に出す顔写真）。立ち絵とは別に持つ。
    has_portrait = (
        bool(row["portrait_image_path"]) if "portrait_image_path" in columns else False
    )
    # 合成に使える立ち絵が何枚あるか（0枚なら静止画を作れない）。
    figure_count = int(row["figure_count"]) if "figure_count" in columns else 0
    portrait_source = row["portrait_source"] if "portrait_source" in columns else ""
    # 代表写真を決めていなければ既定の立ち絵が出る。既定の付け替えや立ち絵の置き換えでも
    # 人物の更新時刻は変わらないので、出ている立ち絵の版も版に含める。
    default_figure_version = (
        row["default_figure_version"] or "" if "default_figure_version" in columns else ""
    )
    return CharacterRead(
        id=row["id"],
        project_id=row["project_id"],
        name=row["name"],
        has_portrait_image=has_portrait,
        portrait_source=portrait_source,
        figure_count=figure_count,
        portrait_version=f"{row['updated_at']}|{default_figure_version}",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class CharacterRepository:
    def __init__(self, database: Database):
        self.database = database

    def create(self, project_id: str, request: CharacterCreate) -> CharacterRead:
        ProjectRepository(self.database).get(project_id)
        character_id = str(uuid4())
        timestamp = _now()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO characters(id, project_id, name, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    character_id,
                    project_id,
                    request.name,
                    timestamp,
                    timestamp,
                ),
            )
        return self.get(character_id)

    # 出来上がっている立ち絵の枚数。一覧で「まだ作っていない人物」を見分けるために読む。
    _FIGURE_COUNT_COLUMN = """
        (SELECT COUNT(*) FROM character_figures f
          WHERE f.character_id = c.id AND f.state = 'completed' AND f.image_path <> '')
        AS figure_count,
        (SELECT f.id || ':' || f.updated_at FROM character_figures f
          WHERE f.character_id = c.id AND f.state = 'completed' AND f.image_path <> ''
          ORDER BY f.is_default DESC, f.ordinal ASC, f.created_at ASC, f.rowid ASC
          LIMIT 1)
        AS default_figure_version
    """

    def get(self, character_id: str) -> CharacterRead:
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT c.*, {self._FIGURE_COUNT_COLUMN} FROM characters c WHERE c.id = ?",  # noqa: S608
                (character_id,),
            ).fetchone()
        if row is None:
            raise CharacterNotFoundError(character_id)
        return _to_character(row)

    def list(self, project_id: str) -> list[CharacterRead]:
        ProjectRepository(self.database).get(project_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT c.*, {self._FIGURE_COUNT_COLUMN}
                FROM characters c
                WHERE c.project_id = ?
                ORDER BY c.updated_at DESC, c.created_at DESC
                """,  # noqa: S608
                (project_id,),
            ).fetchall()
        return [_to_character(row) for row in rows]

    def update(self, character_id: str, request: CharacterUpdate) -> CharacterRead:
        current = self.get(character_id)
        updates = request.model_dump(exclude_unset=True)
        if not updates:
            return current
        allowed = {"name"}
        if not set(updates).issubset(allowed):
            raise ValueError("更新対象に許可されていない項目があります")
        timestamp = _now()
        assignments = ", ".join(f"{name} = ?" for name in updates)
        with self.database.connect() as connection:
            cursor = connection.execute(
                f"UPDATE characters SET {assignments}, updated_at = ? WHERE id = ?",
                (*updates.values(), timestamp, character_id),
            )
            if cursor.rowcount != 1:
                raise CharacterNotFoundError(character_id)
        return self.get(character_id)

    def portrait_image_path(self, character_id: str) -> Path | None:
        """一覧に出す代表写真。未設定なら None（呼び出し側が既定の立ち絵へ落とす）。"""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT portrait_image_path FROM characters WHERE id = ?", (character_id,)
            ).fetchone()
        if row is None:
            raise CharacterNotFoundError(character_id)
        return Path(row["portrait_image_path"]) if row["portrait_image_path"] else None

    def set_portrait_image(
        self, character_id: str, image_path: Path | None, *, source: str = ""
    ) -> CharacterRead:
        """代表写真を差し替える。None を渡すと既定（立ち絵）へ戻す。

        `source` はどの候補を選んだかの覚え書きで、一覧に「選択中」を出すために使う。
        """
        self.get(character_id)
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE characters
                SET portrait_image_path = ?, portrait_source = ?, updated_at = ?
                WHERE id = ?
                """,
                (str(image_path) if image_path else "", source, _now(), character_id),
            )
        return self.get(character_id)
