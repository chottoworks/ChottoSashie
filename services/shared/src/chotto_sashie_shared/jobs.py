"""複数アプリで共有する非同期ジョブの状態機械。

呼び出し側のDBラッパーは ``connect()`` が ``sqlite3.Row`` 行ファクトリ済みの
コネクションをコンテキストマネージャとして返し、かつ ``jobs`` テーブル
(id, kind, state, progress, payload_json, message, external_id, error_code,
result_json, created_at, updated_at) を持ってさえいれば、どのアプリのDB実装
とも組み合わせられる（構造的部分型のため直接の継承・インポートは不要）。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field


class JobState(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    RUNNING = "running"
    VALIDATING = "validating"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class JobCreate(BaseModel):
    kind: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_.-]+$")
    payload: dict[str, Any] = Field(default_factory=dict)


class JobTransition(BaseModel):
    state: JobState
    progress: float | None = Field(default=None, ge=0, le=1)
    message: str | None = Field(default=None, max_length=1000)
    external_id: str | None = Field(default=None, max_length=200)
    error_code: str | None = Field(default=None, max_length=100)
    result: dict[str, Any] | None = None


class JobRead(BaseModel):
    id: str
    kind: str
    state: JobState
    progress: float
    payload: dict[str, Any]
    message: str | None
    external_id: str | None
    error_code: str | None
    result: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


ALLOWED_TRANSITIONS: dict[JobState, set[JobState]] = {
    JobState.QUEUED: {JobState.PREPARING, JobState.CANCELLED, JobState.FAILED},
    JobState.PREPARING: {
        JobState.RUNNING,
        JobState.CANCELLED,
        JobState.FAILED,
        JobState.INTERRUPTED,
    },
    JobState.RUNNING: {
        JobState.RUNNING,
        JobState.VALIDATING,
        JobState.COMPLETED,
        JobState.CANCELLED,
        JobState.FAILED,
        JobState.INTERRUPTED,
    },
    JobState.VALIDATING: {
        # 確認中も進み具合を書き足せるようにする（実行中と同じ扱い）。
        JobState.VALIDATING,
        JobState.COMPLETED,
        JobState.CANCELLED,
        JobState.FAILED,
        JobState.INTERRUPTED,
    },
    JobState.INTERRUPTED: {JobState.QUEUED, JobState.PREPARING, JobState.CANCELLED},
    JobState.COMPLETED: set(),
    JobState.FAILED: {JobState.QUEUED},
    JobState.CANCELLED: {JobState.QUEUED},
}


ACTIVE_JOB_STATES: tuple[str, ...] = (
    JobState.QUEUED.value,
    JobState.PREPARING.value,
    JobState.RUNNING.value,
    JobState.VALIDATING.value,
)


JOBS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'queued', 'preparing', 'running', 'validating',
        'completed', 'failed', 'cancelled', 'interrupted'
    )),
    progress REAL NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 1),
    payload_json TEXT NOT NULL,
    message TEXT,
    external_id TEXT,
    error_code TEXT,
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_state_created
ON jobs(state, created_at);
"""


class JobNotFoundError(KeyError):
    pass


class InvalidTransitionError(ValueError):
    pass


class SupportsConnect(Protocol):
    def connect(self) -> AbstractContextManager[sqlite3.Connection]: ...


# アプリが落ちた・落とされたあとの後片付けで使う文言。親ジョブと、1枚ごとの記録の
# どちらにも同じ文言を残し、画面のどこを見ても同じ理由が読めるようにする。
INTERRUPTED_MESSAGE = "アプリ終了またはWindows再起動により中断されました"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _to_job(row: sqlite3.Row) -> JobRead:
    return JobRead(
        id=row["id"],
        kind=row["kind"],
        state=row["state"],
        progress=row["progress"],
        payload=json.loads(row["payload_json"]),
        message=row["message"],
        external_id=row["external_id"],
        error_code=row["error_code"],
        result=json.loads(row["result_json"]) if row["result_json"] else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class JobRepository:
    def __init__(self, database: SupportsConnect):
        self.database = database

    def create(self, request: JobCreate) -> JobRead:
        job_id = str(uuid4())
        timestamp = _now()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs(
                    id, kind, state, progress, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    job_id,
                    request.kind,
                    JobState.QUEUED.value,
                    json.dumps(request.payload, ensure_ascii=False),
                    timestamp,
                    timestamp,
                ),
            )
        return self.get(job_id)

    def get(self, job_id: str) -> JobRead:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFoundError(job_id)
        return _to_job(row)

    def list(self, limit: int = 100) -> list[JobRead]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_to_job(row) for row in rows]

    def find_active(self, kinds: Sequence[str]) -> JobRead | None:
        """指定した種別のうち、まだ終了していない最古のジョブを一件返す。"""
        if not kinds:
            return None
        kind_slots = ",".join("?" for _ in kinds)
        state_slots = ",".join("?" for _ in ACTIVE_JOB_STATES)
        with self.database.connect() as connection:
            row = connection.execute(
                f"""
                SELECT * FROM jobs
                WHERE kind IN ({kind_slots}) AND state IN ({state_slots})
                ORDER BY created_at LIMIT 1
                """,
                (*kinds, *ACTIVE_JOB_STATES),
            ).fetchone()
        return _to_job(row) if row is not None else None

    def list_active(self, kinds: Sequence[str]) -> list[JobRead]:
        """指定した種別のうち、まだ終了していないジョブを古い順に返す。

        順番待ちの何番目かを数えるのに使う（実行中の一件も含む）。
        """
        if not kinds:
            return []
        kind_slots = ",".join("?" for _ in kinds)
        state_slots = ",".join("?" for _ in ACTIVE_JOB_STATES)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM jobs
                WHERE kind IN ({kind_slots}) AND state IN ({state_slots})
                ORDER BY created_at
                """,
                (*kinds, *ACTIVE_JOB_STATES),
            ).fetchall()
        return [_to_job(row) for row in rows]

    def claim_next(self) -> JobRead | None:
        """最古の待機ジョブを一件だけ準備中へ移す。"""
        timestamp = _now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id FROM jobs WHERE state = 'queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            cursor = connection.execute(
                """
                UPDATE jobs SET state = 'preparing', updated_at = ?
                WHERE id = ? AND state = 'queued'
                """,
                (timestamp, row["id"]),
            )
            if cursor.rowcount != 1:
                return None
            claimed_id = row["id"]
        return self.get(claimed_id)

    def transition(self, job_id: str, update: JobTransition) -> JobRead:
        current = self.get(job_id)
        if update.state not in ALLOWED_TRANSITIONS[current.state]:
            raise InvalidTransitionError(
                f"{current.state.value} から {update.state.value} へは遷移できません"
            )

        progress = update.progress if update.progress is not None else current.progress
        result = update.result if update.result is not None else current.result
        if update.state is JobState.COMPLETED:
            progress = 1
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET state = ?, progress = ?, message = ?, external_id = ?,
                    error_code = ?, result_json = ?, updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (
                    update.state.value,
                    progress,
                    update.message,
                    update.external_id or current.external_id,
                    update.error_code,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    _now(),
                    job_id,
                    current.state.value,
                ),
            )
            if cursor.rowcount != 1:
                raise InvalidTransitionError("別の処理が先にジョブ状態を更新しました")
        return self.get(job_id)

    def request_cancel(self, job_id: str, message: str = "利用者が中止しました") -> JobRead:
        """実行中のジョブを中止済みにする。

        実際の停止（ComfyUIへの中止依頼・子プロセスの終了）は呼び出し側が行う。
        監視ループは次の確認でこの状態を見て、静かに降りる。
        すでに終わっているジョブには InvalidTransitionError。
        """
        return self.transition(job_id, JobTransition(state=JobState.CANCELLED, message=message))

    def is_cancelled(self, job_id: str) -> bool:
        """監視ループが「降りるべきか」を判断するための確認。"""
        try:
            return self.get(job_id).state is JobState.CANCELLED
        except JobNotFoundError:
            # 記録ごと消えている（作品の削除など）。監視を続ける理由はない。
            return True

    def recover_interrupted(self) -> int:
        """前回終了時に処理中だったジョブを中断状態へ移す。

        1枚ごとの記録（候補・参照画像・状態画像）は別の表にあり、ここでは倒せない。
        呼び出し側で `recover_interrupted` を持つ各リポジトリも一緒に呼ぶこと。
        """
        timestamp = _now()
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET state = 'interrupted',
                    message = ?,
                    updated_at = ?
                WHERE state IN ('preparing', 'running', 'validating')
                """,
                (INTERRUPTED_MESSAGE, timestamp),
            )
            return cursor.rowcount
