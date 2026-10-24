from __future__ import annotations

import json
from datetime import UTC, datetime
from sqlite3 import Row
from uuid import uuid4

from .sqlite import SqliteDatabase as Database
from .character_schemas import (
    CharacterCreate,
    CharacterRead,
    CharacterUpdate,
    ProjectCreate,
    ProjectRead,
    ProjectUpdate,
)


class ProjectNotFoundError(KeyError):
    pass


class CharacterNotFoundError(KeyError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _to_project(row: Row) -> ProjectRead:
    return ProjectRead(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        illustration_style=row["illustration_style"],
        character_count=row["character_count"] if "character_count" in row.keys() else 0,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _to_character(row: Row) -> CharacterRead:
    return CharacterRead(
        id=row["id"],
        project_id=row["project_id"],
        name=row["name"],
        gender_age=row["gender_age"],
        face=row["face"],
        hair_style=row["hair_style"],
        hair_color=row["hair_color"],
        eyes=row["eyes"],
        eye_color=row["eye_color"],
        body_type=row["body_type"],
        base_outfit=row["base_outfit"],
        accessories=row["accessories"],
        personality=row["personality"],
        immutable_features=json.loads(row["immutable_features_json"]),
        art_style=row["art_style"],
        notes=row["notes"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class ProfileRepository:
    """プロジェクトと登場人物プロフィールを短いトランザクションで保存する。"""

    def __init__(self, database: Database):
        self.database = database

    def create_project(self, request: ProjectCreate) -> ProjectRead:
        project_id = str(uuid4())
        timestamp = _now()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO projects(
                    id, name, description, illustration_style, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    request.name,
                    request.description,
                    request.illustration_style,
                    timestamp,
                    timestamp,
                ),
            )
        return self.get_project(project_id)

    def update_project(self, project_id: str, request: ProjectUpdate) -> ProjectRead:
        current = self.get_project(project_id)
        updates = request.model_dump(exclude_unset=True)
        if not updates:
            return current
        if not set(updates).issubset({"name", "description", "illustration_style"}):
            raise ValueError("更新対象に許可されていない項目があります")
        timestamp = _now()
        assignments = ", ".join(f"{name} = ?" for name in updates)
        with self.database.connect() as connection:
            cursor = connection.execute(
                f"UPDATE projects SET {assignments}, updated_at = ? WHERE id = ?",
                (*updates.values(), timestamp, project_id),
            )
        if cursor.rowcount != 1:
            raise ProjectNotFoundError(project_id)
        return self.get_project(project_id)

    def get_project(self, project_id: str) -> ProjectRead:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT p.*, COUNT(c.id) AS character_count
                FROM projects p
                LEFT JOIN characters c ON c.project_id = p.id
                WHERE p.id = ?
                GROUP BY p.id
                """,
                (project_id,),
            ).fetchone()
        if row is None:
            raise ProjectNotFoundError(project_id)
        return _to_project(row)

    def list_projects(self) -> list[ProjectRead]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT p.*, COUNT(c.id) AS character_count
                FROM projects p
                LEFT JOIN characters c ON c.project_id = p.id
                GROUP BY p.id
                ORDER BY p.updated_at DESC, p.created_at DESC
                """
            ).fetchall()
        return [_to_project(row) for row in rows]

    def create_character(self, project_id: str, request: CharacterCreate) -> CharacterRead:
        self.get_project(project_id)
        character_id = str(uuid4())
        timestamp = _now()
        values = request.model_dump()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO characters(
                    id, project_id, name, gender_age, face, hair_style, hair_color,
                    eyes, eye_color, body_type, base_outfit, accessories, personality,
                    immutable_features_json, art_style, notes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    character_id,
                    project_id,
                    values["name"],
                    values["gender_age"],
                    values["face"],
                    values["hair_style"],
                    values["hair_color"],
                    values["eyes"],
                    values["eye_color"],
                    values["body_type"],
                    values["base_outfit"],
                    values["accessories"],
                    values["personality"],
                    json.dumps(values["immutable_features"], ensure_ascii=False),
                    values["art_style"],
                    values["notes"],
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?",
                (timestamp, project_id),
            )
        return self.get_character(character_id)

    def get_character(self, character_id: str) -> CharacterRead:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM characters WHERE id = ?", (character_id,)
            ).fetchone()
        if row is None:
            raise CharacterNotFoundError(character_id)
        return _to_character(row)

    def list_characters(self, project_id: str) -> list[CharacterRead]:
        self.get_project(project_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM characters
                WHERE project_id = ?
                ORDER BY updated_at DESC, created_at DESC
                """,
                (project_id,),
            ).fetchall()
        return [_to_character(row) for row in rows]

    def update_character(self, character_id: str, request: CharacterUpdate) -> CharacterRead:
        current = self.get_character(character_id)
        updates = request.model_dump(exclude_unset=True)
        if not updates:
            return current
        if "immutable_features" in updates:
            updates["immutable_features_json"] = json.dumps(
                updates.pop("immutable_features"), ensure_ascii=False
            )
        allowed = {
            "name",
            "gender_age",
            "face",
            "hair_style",
            "hair_color",
            "eyes",
            "eye_color",
            "body_type",
            "base_outfit",
            "accessories",
            "personality",
            "immutable_features_json",
            "art_style",
            "notes",
        }
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
            connection.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?",
                (timestamp, current.project_id),
            )
        return self.get_character(character_id)
