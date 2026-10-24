"""ComfyUIジョブの完了待ちを1本化した監視ヘルパ。

各アプリの生成（素材・合成・動画・候補など）は「履歴をポーリングし、完了したら
成果物を取り出して保存し、ジョブを完了へ進める」という同じ流れを持つ。差分は
成果物の扱い（`on_success`）と表示文だけなので、ループ本体はここへ集約する。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .comfy_client import ComfyClient, ComfyConnectionError, output_files
from .comfy_scratch import ComfyScratch
from .jobs import InvalidTransitionError, JobNotFoundError, JobRepository, JobTransition


@dataclass(frozen=True)
class MonitorMessages:
    """監視中に利用者へ見せる文言と失敗コード。"""

    running: str
    saving: str
    completed: str
    failed: str
    failed_code: str
    timeout: str
    timeout_code: str
    monitor_failed_code: str


# 成果物（ComfyUIの出力ファイル記述子）を受け取り、保存などを行って
# ジョブのresultへ載せる辞書を返す。
OnSuccess = Callable[[list[dict[str, str]]], Awaitable[dict[str, Any] | None]]


@dataclass
class MonitorSettings:
    poll_seconds: float = 0.5
    max_polls: int = 3600
    # ジョブ失敗として扱う追加の例外（保存先が消えた等、アプリ固有のもの）。
    extra_errors: tuple[type[BaseException], ...] = field(default_factory=tuple)


async def monitor_comfy_prompt(
    *,
    client: ComfyClient,
    jobs: JobRepository,
    job_id: str,
    prompt_id: str,
    on_success: OnSuccess,
    messages: MonitorMessages,
    settings: MonitorSettings | None = None,
    scratch: ComfyScratch | None = None,
) -> None:
    """1件のComfyUIプロンプトを監視し、成否に応じてジョブを終端まで進める。

    `scratch` を渡すと、終わったプロンプトの出力ファイルを（取り出したあとで）
    ComfyUI側から片付ける。成功でも失敗でも、そのファイルを読み直すことは無い。
    """
    options = settings or MonitorSettings()
    failure_errors = (
        ComfyConnectionError,
        InvalidTransitionError,
        JobNotFoundError,
        OSError,
        *options.extra_errors,
    )
    try:
        jobs.transition(
            job_id,
            JobTransition(state="running", progress=0.2, message=messages.running),
        )
        for _ in range(options.max_polls):
            # 利用者が中止したら、ここで静かに降りる（状態はすでに cancelled）。
            if jobs.is_cancelled(job_id):
                return
            entry = await client.history(prompt_id)
            if entry is not None:
                status = entry.get("status", {})
                if isinstance(status, dict) and status.get("completed"):
                    files = output_files(entry)
                    try:
                        await _finish(jobs, job_id, status, files, on_success, messages)
                    finally:
                        if scratch is not None:
                            scratch.discard_outputs(files)
                    return
            await asyncio.sleep(options.poll_seconds)

        jobs.transition(
            job_id,
            JobTransition(
                state="failed",
                message=messages.timeout,
                error_code=messages.timeout_code,
            ),
        )
    except failure_errors as error:
        try:
            jobs.transition(
                job_id,
                JobTransition(
                    state="failed",
                    message=str(error),
                    error_code=messages.monitor_failed_code,
                ),
            )
        except (InvalidTransitionError, JobNotFoundError):
            return


async def _finish(
    jobs: JobRepository,
    job_id: str,
    status: dict[str, Any],
    files: list[dict[str, str]],
    on_success: OnSuccess,
    messages: MonitorMessages,
) -> None:
    """終わったプロンプトの成否に合わせて、成果物を取り込みジョブを終端へ進める。"""
    if status.get("status_str") == "success" and files:
        result = await on_success(files)
        jobs.transition(
            job_id,
            JobTransition(
                state="validating",
                progress=0.95,
                message=messages.saving,
                result=result,
            ),
        )
        jobs.transition(
            job_id,
            JobTransition(state="completed", message=messages.completed),
        )
        return
    jobs.transition(
        job_id,
        JobTransition(
            state="failed",
            message=messages.failed,
            error_code=messages.failed_code,
        ),
    )
