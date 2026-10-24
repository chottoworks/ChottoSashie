"""人物候補の「作った回（バッチ）」の一覧（共通の土台）。

候補は生成のたびに新しいバッチが増える。画面は既定で最新バッチだけを出すため、
作り直すと前の回が見えなくなる。記録自体は残っているので、ここで一覧を作って
前の回へ戻れるようにする。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .local_images import CANDIDATE_IMAGES, delete_local_image
from .sqlite import SqliteDatabase as Database

# 種別ごとの記録テーブル。削除で使う。
_BATCH_TABLES: dict[str, str] = {
    CANDIDATE_IMAGES: "character_candidates",
}


@dataclass(frozen=True)
class BatchSummary:
    """1回ぶんの生成の要約。"""

    batch_job_id: str
    created_at: str
    image_count: int
    completed_count: int
    is_latest: bool
    # 基準画像がこの回から選ばれているか。
    has_base: bool = False


def list_candidate_batches(database: Database, character_id: str) -> list[BatchSummary]:
    """人物候補の生成回を新しい順に返す。"""
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT
                batch_job_id,
                MIN(created_at) AS created_at,
                COUNT(*) AS image_count,
                SUM(CASE WHEN state = 'completed' THEN 1 ELSE 0 END) AS completed_count,
                MAX(is_base) AS has_base
            FROM character_candidates
            WHERE character_id = ?
            GROUP BY batch_job_id
            ORDER BY created_at DESC
            """,
            (character_id,),
        ).fetchall()
    return [
        BatchSummary(
            batch_job_id=row["batch_job_id"],
            created_at=row["created_at"],
            image_count=row["image_count"],
            completed_count=row["completed_count"] or 0,
            is_latest=index == 0,
            has_base=bool(row["has_base"]),
        )
        for index, row in enumerate(rows)
    ]


class BatchInUseError(RuntimeError):
    """生成中の回。消すと監視が行き先を失うため、終わるまで待ってもらう。"""


def delete_image_batches(
    database: Database,
    data_dir: Path,
    kind: str,
    character_id: str,
    *,
    batch_job_ids: list[str] | None = None,
    keep_base: bool = False,
) -> int:
    """生成の回を、記録と画像の控えごと消す。消した枚数を返す。

    `batch_job_ids` を省くと、その人物のその種別の回をすべて消す（作り直しの
    「上書き」で使う）。`keep_base` を付けると、選んでいる基準画像だけは残す——
    立ち絵の起点として使い続けるため。生成中の回が混ざっていたら何も消さずに
    `BatchInUseError`。候補を消すと、その回を基準に作った立ち絵の基準リンクは外れる
    （`character_figures.base_candidate_id` は ON DELETE SET NULL）。
    """
    table = _BATCH_TABLES[kind]
    where = "character_id = ?"
    parameters: list[str] = [character_id]
    if batch_job_ids is not None:
        if not batch_job_ids:
            return 0
        placeholders = ", ".join("?" for _ in batch_job_ids)
        where += f" AND batch_job_id IN ({placeholders})"
        parameters.extend(batch_job_ids)
    if keep_base:
        where += " AND is_base = 0"
    return _delete_records(database, data_dir, kind, table, where, parameters)


def delete_candidate_batches(
    database: Database,
    data_dir: Path,
    character_id: str,
    *,
    batch_job_ids: list[str] | None = None,
    keep_base: bool = False,
) -> int:
    return delete_image_batches(
        database,
        data_dir,
        CANDIDATE_IMAGES,
        character_id,
        batch_job_ids=batch_job_ids,
        keep_base=keep_base,
    )


def delete_unused_candidates(database: Database, data_dir: Path, character_id: str) -> int:
    """基準画像に選ばなかった人物候補を、画像の控えごと消す。消した枚数を返す。

    基準を決めた時点で、ほかの候補は使い道が無くなる（立ち絵は基準画像から作り、
    代表写真は選んだ時点で写しを持つ）。生成中の回があるときは、何も消さずに
    `BatchInUseError`（出来上がるものまで「選ばなかった」ことにしないため）。
    """
    with database.connect() as connection:
        running = connection.execute(
            """
            SELECT 1 FROM character_candidates
            WHERE character_id = ? AND state IN ('queued', 'running') LIMIT 1
            """,
            (character_id,),
        ).fetchone()
    if running is not None:
        raise BatchInUseError("生成中の人物候補があるため、終わってから削除してください")
    return delete_image_batches(
        database, data_dir, CANDIDATE_IMAGES, character_id, keep_base=True
    )


def _delete_records(
    database: Database,
    data_dir: Path,
    kind: str,
    table: str,
    where: str,
    parameters: list[str],
) -> int:
    with database.connect() as connection:
        rows = connection.execute(
            f"SELECT id, state FROM {table} WHERE {where}",  # noqa: S608 - 表名は定数表から
            parameters,
        ).fetchall()
        if any(row["state"] in ("queued", "running") for row in rows):
            raise BatchInUseError("生成中の回は削除できません")
        if not rows:
            return 0
        connection.execute(
            f"DELETE FROM {table} WHERE {where}",  # noqa: S608 - 表名は定数表から
            parameters,
        )
    for row in rows:
        delete_local_image(data_dir, kind, row["id"])
    return len(rows)
