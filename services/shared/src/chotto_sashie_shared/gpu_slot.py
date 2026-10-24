"""GPUを占有する処理が同時に走らないようにする調停（複数アプリ共有版）。

単一利用者・単一GPUを前提に、重い処理は常に一件だけ実行する。判定はSQLiteの
ジョブ状態を根拠とするため、API再起動をまたいでも矛盾しない。将来ジョブ実行を
別プロセスへ分離する場合は、この判定をそちらの取得条件へ移設する。

対象ジョブ種別とラベルはアプリごとに異なるため（挿絵アプリと動画アプリで別の
job kind 一覧を持つ）、`GpuSlot` はそれらを構築時の引数として受け取る。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence

from .jobs import JobCreate, JobRead, JobRepository


class GpuBusyError(RuntimeError):
    """GPUを使う別の処理が実行中で、新しい処理を開始できない状態を表す。"""

    def __init__(self, active: JobRead, describe: Callable[[str], str]) -> None:
        self.active = active
        super().__init__(
            f"GPUを使用する別の処理が実行中です（{describe(active.kind)}）。"
            "完了または中止してからやり直してください"
        )


class GpuSlot:
    """GPUを使うジョブの作成を直列化する、プロセス内の唯一の入口。

    保存先は呼び出しごとに受け取る。ここでリポジトリを保持すると、取り込み時に
    決まった保存先へ固定され、差し替えた保存先を使う試験が実データへ書き込む。
    """

    def __init__(
        self,
        job_kinds: Sequence[str],
        describe: Callable[[str], str] | None = None,
    ) -> None:
        self._lock = asyncio.Lock()
        self._job_kinds = tuple(job_kinds)
        self._describe = describe or (lambda kind: kind)

    async def claim(self, repository: JobRepository, request: JobCreate) -> JobRead:
        """実行中ジョブの確認と新規ジョブ作成を不可分に行う。

        作成直後のジョブは `queued` として実行中扱いになるため、ロックを解放した
        時点で後続の要求からも見える。これにより二重起動の隙間がなくなる。
        """
        async with self._lock:
            active = repository.find_active(self._job_kinds)
            if active is not None:
                raise GpuBusyError(active, self._describe)
            return repository.create(request)
