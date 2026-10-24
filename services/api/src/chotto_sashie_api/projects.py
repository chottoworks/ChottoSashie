from __future__ import annotations

from datetime import UTC, datetime
from sqlite3 import Row
from uuid import uuid4

from .database import Database
from .schemas import ProjectCreate, ProjectRead, ProjectUpdate


class ProjectNotFoundError(KeyError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _to_project(row: Row) -> ProjectRead:
    return ProjectRead(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        illustration_style=row["illustration_style"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class ProjectRepository:
    def __init__(self, database: Database):
        self.database = database

    def create(self, request: ProjectCreate) -> ProjectRead:
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
        return self.get(project_id)

    def update(self, project_id: str, request: ProjectUpdate) -> ProjectRead:
        """作品名・説明・画風を更新する。画風は次に作る候補・参照画像から反映される。"""
        current = self.get(project_id)
        updates = request.model_dump(exclude_unset=True)
        updates = {key: value for key, value in updates.items() if value is not None}
        if not updates:
            return current
        assignments = ", ".join(f"{name} = ?" for name in updates)
        with self.database.connect() as connection:
            cursor = connection.execute(
                f"UPDATE projects SET {assignments}, updated_at = ? WHERE id = ?",  # noqa: S608
                (*updates.values(), _now(), project_id),
            )
            if cursor.rowcount != 1:
                raise ProjectNotFoundError(project_id)
        return self.get(project_id)

    def get(self, project_id: str) -> ProjectRead:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        if row is None:
            raise ProjectNotFoundError(project_id)
        return _to_project(row)

    def list(self) -> list[ProjectRead]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM projects ORDER BY updated_at DESC, created_at DESC"
            ).fetchall()
        return [_to_project(row) for row in rows]
