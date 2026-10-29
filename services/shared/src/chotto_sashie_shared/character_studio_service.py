"""人物候補の生成投入（共通の土台）。

「どんなプロンプトで、どのテンプレートへ、どのseedで投入し、記録をどう作るか」
という手順そのものを共有する。ジョブの確保・進捗遷移・監視の開始・HTTPの扱いは
アプリ側に残す（GPU枠や監視の作りがアプリごとに違うため）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from .candidates import (
    CandidateNotFoundError,
    CandidateRepository,
    build_character_candidate_prompts,
)
from .character_schemas import CandidateGenerateRequest, CharacterRead
from .comfy_client import ComfyClient, ComfyConnectionError, output_images
from .comfy_progress import JobProgressWriter, PromptProgress
from .comfy_scratch import ComfyScratch
from .comfy_workflows import bind_turbo_workflow
from .jobs import InvalidTransitionError, JobNotFoundError, JobRepository, JobTransition
from .local_images import CANDIDATE_IMAGES, write_local_image
from .sqlite import SqliteDatabase


async def submit_candidate_batch(
    *,
    database: SqliteDatabase,
    job_id: str,
    character: CharacterRead,
    project_style: str,
    request: CandidateGenerateRequest,
    seed_start: int,
    template: dict[str, Any],
    output_root: str,
) -> tuple[list[str], str | None]:
    """人物候補を1枚ずつ記録しながらComfyUIへ投入する。

    戻り値は (候補ID一覧, 最初のprompt_id)。失敗時は例外をそのまま送出するので、
    呼び出し側でジョブの失敗遷移と記録の後始末を行う。
    """
    repository = CandidateRepository(database)
    client = ComfyClient(str(request.base_url))
    prompts = build_character_candidate_prompts(
        character, request.count, project_style=project_style
    )
    candidate_ids: list[str] = []
    first_prompt_id: str | None = None

    for index, prompt in enumerate(prompts):
        seed = seed_start + index
        candidate = repository.create(
            character_id=character.id,
            batch_job_id=job_id,
            ordinal=index + 1,
            prompt=prompt,
            negative_prompt=request.negative_prompt,
            seed=seed,
            width=request.width,
            height=request.height,
            base_url=str(request.base_url),
        )
        candidate_ids.append(candidate.id)
        workflow = bind_turbo_workflow(
            template,
            prompt=prompt,
            negative_prompt=request.negative_prompt,
            seed=seed,
            cfg=request.variation_strength,
            width=request.width,
            height=request.height,
            diffusion_model=request.diffusion_model,
            text_encoder=request.text_encoder,
            vae=request.vae,
            filename_prefix=(
                f"{output_root}/characters/{character.id}/candidates/{job_id}/{index + 1:02d}"
            ),
        )
        prompt_id = await client.submit(workflow)
        repository.mark_running(candidate.id, prompt_id)
        first_prompt_id = first_prompt_id or prompt_id

    return candidate_ids, first_prompt_id


async def monitor_candidate_batch(
    job_id: str,
    candidate_ids: list[str],
    base_url: str,
    *,
    database: SqliteDatabase,
    jobs: JobRepository,
    data_dir: Path,
    scratch: ComfyScratch | None = None,
    poll_seconds: float = 0.5,
    max_polls: int = 1800,
) -> None:
    """人物候補を一括監視し、各画像と親ジョブの進捗を永続化する。

    出来た候補は、その場でアプリの保存先へ控える。立ち絵の起点にも代表写真にも
    この控えを使うので、ComfyUI側の出力は控えた時点で要らなくなる（`scratch`）。
    """
    repository = CandidateRepository(database)
    client = ComfyClient(base_url)
    pending = set(candidate_ids)
    total = len(candidate_ids)
    writer = JobProgressWriter(jobs, job_id)
    try:
        writer.write(0.1, f"人物候補を生成しています（0/{total}）")
        for _ in range(max_polls):
            # 利用者が中止したら、ここで静かに降りる（状態はすでに cancelled）。
            if jobs.is_cancelled(job_id):
                return
            # ComfyUIは1枚ずつ順に描く。いま描いている1枚の途中経過を、済んだ枚数に足す。
            current: PromptProgress | None = None
            for candidate_id in list(pending):
                candidate = repository.get(candidate_id)
                if candidate.state in {"completed", "failed"}:
                    pending.remove(candidate_id)
                    continue
                if not candidate.prompt_id:
                    continue
                entry = await client.history(candidate.prompt_id)
                status = entry.get("status", {}) if entry is not None else {}
                if not isinstance(status, dict) or not status.get("completed"):
                    current = current or client.prompt_progress(candidate.prompt_id)
                    continue
                images = output_images(entry)
                if status.get("status_str") == "success" and images:
                    repository.complete(candidate_id, images[0])
                    # 控えが出来ていなければComfyUI側を残す（表示のときに取り直せるように）。
                    keep = not await _store_candidate_image(
                        client, data_dir, candidate_id, images[0]
                    )
                else:
                    repository.fail(candidate_id, "ComfyUIで候補画像を生成できませんでした")
                    keep = False
                if scratch is not None and not keep:
                    scratch.discard_outputs(images)
                pending.remove(candidate_id)

            finished = total - len(pending)
            if pending:
                drawing = current.fraction if current is not None else 0.0
                message = f"人物候補を生成しています（{finished}/{total}）"
                writer.write(
                    min(0.9, 0.1 + 0.8 * (finished + drawing) / total),
                    current.describe(message) if current is not None else message,
                )
            if not pending:
                candidates = [repository.get(candidate_id) for candidate_id in candidate_ids]
                completed = sum(candidate.state == "completed" for candidate in candidates)
                failed = total - completed
                result = {
                    "candidate_ids": candidate_ids,
                    "completed_count": completed,
                    "failed_count": failed,
                }
                if completed == 0:
                    jobs.transition(
                        job_id,
                        JobTransition(
                            state="failed",
                            message="人物候補を1枚も生成できませんでした",
                            error_code="CANDIDATE_BATCH_FAILED",
                            result=result,
                        ),
                    )
                    return
                jobs.transition(
                    job_id,
                    JobTransition(
                        state="validating",
                        progress=0.95,
                        message="候補画像を一覧へ登録しています",
                        result=result,
                    ),
                )
                jobs.transition(
                    job_id,
                    JobTransition(
                        state="completed",
                        message=f"人物候補を{completed}枚生成しました",
                    ),
                )
                return
            await asyncio.sleep(poll_seconds)

        repository.fail_batch(job_id, "人物候補の生成待ちがタイムアウトしました")
        jobs.transition(
            job_id,
            JobTransition(
                state="failed",
                message="人物候補の生成待ちがタイムアウトしました",
                error_code="CANDIDATE_TIMEOUT",
            ),
        )
    except (
        CandidateNotFoundError,
        ComfyConnectionError,
        InvalidTransitionError,
        JobNotFoundError,
    ) as error:
        repository.fail_batch(job_id, str(error))
        try:
            jobs.transition(
                job_id,
                JobTransition(
                    state="failed",
                    message=str(error),
                    error_code="CANDIDATE_MONITOR_FAILED",
                ),
            )
        except (InvalidTransitionError, JobNotFoundError):
            return


async def _store_candidate_image(
    client: ComfyClient, data_dir: Path, candidate_id: str, image: dict[str, str]
) -> bool:
    """出来た候補をアプリの保存先へ控える。控えられたら True。"""
    try:
        content, _media_type = await client.view_image(image)
    except ComfyConnectionError:
        return False
    path = await asyncio.to_thread(
        write_local_image, data_dir, CANDIDATE_IMAGES, candidate_id, content
    )
    return path.is_file()
