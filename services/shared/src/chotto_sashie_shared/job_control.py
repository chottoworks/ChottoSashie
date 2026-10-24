"""実行中ジョブの中止（共通の土台）。

「中止」は記録を書き換えるだけでは足りない。GPUを掴んでいる生成や学習が動いたままだと、
次の操作が「GPUを使用する別の処理が実行中です」で始められない。ここでは記録の更新と、
実際に動いているもの（ComfyUIのプロンプト・所有する子プロセス）の停止をまとめて行う。

止められるかどうかは相手次第なので、停止依頼はどれもbest-effort。記録上の中止は必ず成立させる。
"""

from __future__ import annotations

from .child_processes import OwnedProcessRegistry
from .comfy_client import ComfyClient
from .jobs import JobRead, JobRepository

# GPUを使わない、止めても実害のない後片付け系。中止の対象にしない。
CANCEL_MESSAGE = "利用者が中止しました"


async def cancel_running_job(
    jobs: JobRepository,
    job_id: str,
    *,
    comfy_base_url: str | None = None,
    processes: OwnedProcessRegistry | None = None,
) -> JobRead:
    """ジョブを中止済みにし、動いているものを止める。

    終了済みのジョブには `InvalidTransitionError`、存在しなければ `JobNotFoundError`。
    監視ループは次の確認で中止に気づき、静かに降りる。
    """
    job = jobs.request_cancel(job_id, CANCEL_MESSAGE)
    if processes is not None:
        await processes.terminate_job(job_id)
    if comfy_base_url:
        await ComfyClient(comfy_base_url).interrupt()
    return job
