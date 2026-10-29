from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from sqlite3 import Connection, Row
from uuid import uuid4

from .database import Database
from .composition import MAX_CHARACTERS, over_limit_message
from .projects import ProjectRepository
from .schemas import StillImageCreate, StillImageRead, StillImageUpdate


class StillImageNotFoundError(KeyError):
    pass


class StillImageReferenceError(ValueError):
    """静止画の組み合わせが成り立たない。

    参照する素材（人物・背景）が同じ作品に無いか、人物が上限を超えているとき。
    """


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _with_readiness(database: Database, still_image: StillImageRead) -> StillImageRead:
    """「合成できるか」と、できない理由を添える（UIはこれを表示するだけにする）。"""
    from .readiness import still_image_compose_block_reason

    reason = still_image_compose_block_reason(database, still_image)
    return still_image.model_copy(update={"can_compose": not reason, "compose_blocked_reason": reason})


def _with_shot_counts(database: Database, still_image: StillImageRead) -> StillImageRead:
    """シーンとしての進み具合を添える。シーンの一覧は、カットを開かずにこれだけで並べる。"""
    from .shots import count_scene_shots

    shot_count, video_count = count_scene_shots(database, still_image.id)
    return still_image.model_copy(update={"shot_count": shot_count, "shot_video_count": video_count})


def _complete(database: Database, row: Row) -> StillImageRead:
    return _with_shot_counts(database, _with_readiness(database, _to_still_image(row)))


def _to_still_image(row: Row) -> StillImageRead:
    return StillImageRead(
        id=row["id"],
        project_id=row["project_id"],
        ordinal=row["ordinal"],
        name=row["name"],
        background_asset_id=row["background_asset_id"],
        character_ids=json.loads(row["character_ids_json"]),
        composition_job_id=row["composition_job_id"],
        has_image=bool(row["image_path"]),
        image_version=str(row["image_revision"]),
        composition_method=row["composition_method"],
        source_prompt=row["source_prompt"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _ensure_belongs(
    connection: Connection,
    table: str,
    asset_id: str,
    project_id: str,
    label: str,
) -> None:
    row = connection.execute(
        f"SELECT project_id FROM {table} WHERE id = ?", (asset_id,)
    ).fetchone()
    if row is None:
        # 内部の識別子は出さない。利用者にできることは「選び直す」だけなので、それを伝える。
        raise StillImageReferenceError(f"選ばれている{label}が見つかりません。選び直してください")
    if row["project_id"] != project_id:
        raise StillImageReferenceError(f"選ばれている{label}は別の作品のものです。選び直してください")


class StillImageRepository:
    def __init__(self, database: Database):
        self.database = database

    def _validate_references(
        self,
        connection: Connection,
        project_id: str,
        *,
        background_asset_id: str | None,
        character_ids: list[str],
        previous_count: int,
    ) -> None:
        # 人数は合成のときにも見るが、そこで初めて分かっても直す手立てが無い
        # （作り直すしかない）。組み合わせを決めるこの時点で断る。
        #
        # 断るのは「今より増やして上限を超える」ときだけにする。上限を下げた版へ
        # 更新すると、それより前に作った静止画が上限を超えていることがある。名前を
        # 直すことも、人物を減らして上限内へ戻すこともできなくなると、その静止画は
        # 捨てるしかなくなる。
        if len(character_ids) > MAX_CHARACTERS and len(character_ids) > previous_count:
            raise StillImageReferenceError(over_limit_message(len(character_ids)))
        if background_asset_id is not None:
            _ensure_belongs(connection, "background_assets", background_asset_id, project_id, "背景")
        for character_id in character_ids:
            _ensure_belongs(connection, "characters", character_id, project_id, "登場人物")

    def create(self, project_id: str, request: StillImageCreate) -> StillImageRead:
        ProjectRepository(self.database).get(project_id)
        still_image_id = str(uuid4())
        timestamp = _now()
        with self.database.connect() as connection:
            self._validate_references(
                connection,
                project_id,
                background_asset_id=request.background_asset_id,
                character_ids=request.character_ids,
                previous_count=0,
            )
            row = connection.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 AS next FROM still_images WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            ordinal = row["next"]
            connection.execute(
                """
                INSERT INTO still_images(
                    id, project_id, ordinal, name, background_asset_id,
                    character_ids_json, composition_method, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    still_image_id,
                    project_id,
                    ordinal,
                    request.name,
                    request.background_asset_id,
                    json.dumps(request.character_ids, ensure_ascii=False),
                    str(request.composition_method),
                    timestamp,
                    timestamp,
                ),
            )
        return self.get(still_image_id)

    def get(self, still_image_id: str) -> StillImageRead:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM still_images WHERE id = ?", (still_image_id,)
            ).fetchone()
        if row is None:
            raise StillImageNotFoundError(still_image_id)
        return _complete(self.database, row)

    def list(self, project_id: str) -> list[StillImageRead]:
        ProjectRepository(self.database).get(project_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM still_images WHERE project_id = ? ORDER BY ordinal",
                (project_id,),
            ).fetchall()
        return [_complete(self.database, row) for row in rows]

    def update(self, still_image_id: str, request: StillImageUpdate) -> StillImageRead:
        current = self.get(still_image_id)
        updates = request.model_dump(exclude_unset=True)
        if not updates:
            return current
        timestamp = _now()
        with self.database.connect() as connection:
            self._validate_references(
                connection,
                current.project_id,
                background_asset_id=updates.get("background_asset_id", current.background_asset_id),
                character_ids=updates.get("character_ids", current.character_ids),
                previous_count=len(current.character_ids),
            )
            columns: dict[str, object] = {}
            if "name" in updates:
                columns["name"] = updates["name"]
            if "background_asset_id" in updates:
                columns["background_asset_id"] = updates["background_asset_id"]
            if "character_ids" in updates:
                columns["character_ids_json"] = json.dumps(updates["character_ids"], ensure_ascii=False)
            if updates.get("composition_method") is not None:
                columns["composition_method"] = str(updates["composition_method"])
            assignments = ", ".join(f"{name} = ?" for name in columns)
            cursor = connection.execute(
                f"UPDATE still_images SET {assignments}, updated_at = ? WHERE id = ?",
                (*columns.values(), timestamp, still_image_id),
            )
            if cursor.rowcount != 1:
                raise StillImageNotFoundError(still_image_id)
        return self.get(still_image_id)

    def delete(self, still_image_id: str) -> Path | None:
        """削除した静止画の画像パスを返す（あれば呼び出し側で削除）。"""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT image_path FROM still_images WHERE id = ?", (still_image_id,)
            ).fetchone()
            if row is None:
                raise StillImageNotFoundError(still_image_id)
            image_file = row["image_path"]
            connection.execute("DELETE FROM still_images WHERE id = ?", (still_image_id,))
        return Path(image_file) if image_file else None

    def image_path(self, still_image_id: str) -> Path | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT image_path FROM still_images WHERE id = ?", (still_image_id,)
            ).fetchone()
        if row is None:
            raise StillImageNotFoundError(still_image_id)
        return Path(row["image_path"]) if row["image_path"] else None

    def set_composition_job(self, still_image_id: str, job_id: str) -> StillImageRead:
        with self.database.connect() as connection:
            cursor = connection.execute(
                "UPDATE still_images SET composition_job_id = ?, updated_at = ? WHERE id = ?",
                (job_id, _now(), still_image_id),
            )
            if cursor.rowcount != 1:
                raise StillImageNotFoundError(still_image_id)
        return self.get(still_image_id)

    def set_image(
        self, still_image_id: str, image_path: Path, *, source_prompt: str
    ) -> StillImageRead:
        """合成した画像と、そのとき ComfyUI へ渡した指示文を一緒に書く。"""
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE still_images
                SET image_path = ?, image_revision = image_revision + 1, source_prompt = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (str(image_path), source_prompt, _now(), still_image_id),
            )
            if cursor.rowcount != 1:
                raise StillImageNotFoundError(still_image_id)
        return self.get(still_image_id)
