"""SQLite接続の共通土台。

接続時の設定（WAL、外部キー、busy_timeout）と
短いトランザクション運用は同じ。その共通部分をここへ集約する。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class SqliteDatabase:
    """スキーマを受け取り、共通の接続設定で開くSQLiteラッパー。"""

    def __init__(self, path: Path, schema: str):
        self.path = path
        self._schema = schema

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(self._schema)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
