from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
import json
import os
import sqlite3
from functools import partial
from contextlib import asynccontextmanager
from pathlib import Path
from secrets import randbelow
from typing import TypeVar
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import HttpUrl
from fastapi.responses import JSONResponse, Response

from chotto_sashie_shared.comfy_client import (
    ComfyClient,
    ComfyConnectionError,
    bind_workflow,
    load_workflow,
    output_files,
)
from chotto_sashie_shared.comfy_progress import JobProgressWriter, close_progress_watchers
from chotto_sashie_shared.comfy_scratch import ComfyScratch
from chotto_sashie_shared.deletion import remove_within
from chotto_sashie_shared.comfy_monitor import (
    MonitorMessages,
    MonitorSettings,
    monitor_comfy_prompt,
)
from chotto_sashie_shared.download import (
    DownloadCancelled,
    DownloadError,
    DownloadSpec,
    build_setup_plan,
    download_with_resume,
    ensure_free_space,
    find_existing_model,
    find_matching_external_file,
    inspect_managed_file,
    load_download_specs,
)
from chotto_sashie_shared.ffmpeg_provision import (
    FfmpegProvisionError,
    extract_ffmpeg,
    ffmpeg_tools_root,
    load_ffmpeg_spec,
)
from chotto_sashie_shared.candidates import (
    CandidateNotFoundError,
    CandidateRepository,
    CandidateSelectionError,
)
from chotto_sashie_shared.batches import (
    BatchInUseError,
    delete_candidate_batches,
    delete_unused_candidates,
    list_candidate_batches,
)
from chotto_sashie_shared.child_processes import owned_processes
from chotto_sashie_shared.job_control import CANCEL_MESSAGE, cancel_running_job
from chotto_sashie_shared.character_schemas import (
    BatchSummaryRead,
    CandidateBatchResult,
    CandidateImageInfo,
    CandidateRead,
)
from chotto_sashie_shared.character_schemas import CharacterRead as SharedCharacterRead
from chotto_sashie_shared.character_schemas import CharacterUpdate as SharedCharacterUpdate
from chotto_sashie_shared.character_studio_service import (
    monitor_candidate_batch,
    submit_candidate_batch,
)
from chotto_sashie_shared.comfy_workflows import (
    angle_reference_workflow_path,
    apply_high_precision_angle,
    bind_angle_reference_workflow,
    candidate_workflow_path,
)
from chotto_sashie_shared.local_images import (
    CANDIDATE_IMAGES,
    delete_local_image,
    local_image_path,
    read_local_image,
    write_local_image,
)
from chotto_sashie_shared.profiles import CharacterNotFoundError as SharedCharacterNotFoundError
from chotto_sashie_shared.profiles import ProfileRepository
from chotto_sashie_shared.profiles import ProjectNotFoundError as SharedProjectNotFoundError
from chotto_sashie_shared.gpu_slot import GpuBusyError, GpuSlot
from chotto_sashie_shared.gpu_queue import GpuQueue, mark_queue_lost
from chotto_sashie_shared.model_scan import ModelScanResult, scan_model_roots
from chotto_sashie_shared.managed_config import ManagedConfigError
from chotto_sashie_shared.managed_runtime import (
    ManagedRuntimeOptions,
    ManagedRuntimeUnavailableError,
    autostart_managed_comfyui,
    start_managed_comfyui,
)
from chotto_sashie_shared.model_storage import (
    ModelStorage,
    ModelStorageError,
    resolve_model_storage,
)
from chotto_sashie_shared.model_layout import models_relative_path
from chotto_sashie_shared.shared_env import (
    app_resources_root,
    shared_comfyui_root,
    shared_models_root,
)
from chotto_sashie_shared.runtime import (
    RuntimeManager,
    RuntimeManagerError,
    comfy_io_directories,
)
from chotto_sashie_shared.jobs import (
    ACTIVE_JOB_STATES,
    INTERRUPTED_MESSAGE,
    InvalidTransitionError,
    JobCreate,
    JobNotFoundError,
    JobRepository,
    JobTransition,
)

from . import __version__
from .assets import AssetNotFoundError, AssetRepository, build_asset_prompt, variation_name
from .character_portraits import (
    CANDIDATE_OPTION_PREFIX,
    DEFAULT_OPTION_ID,
    FIGURE_OPTION_PREFIX,
    crop_portrait,
    delete_portrait,
    parse_candidate_option_id,
    parse_figure_option_id,
    portraits_dir,
    save_portrait,
)
from .character_figures import (
    FIGURE_PROMPT,
    ORIGIN_BASE,
    ORIGIN_UPLOAD,
    ORIGIN_VARIATION,
    CharacterFigureNotFoundError,
    CharacterFigureRecord,
    CharacterFigureRepository,
    FigureJobWording,
    figure_image_path,
    figure_image_version,
    figures_dir,
    monitor_figure_image,
)
from .character_figure_edits import (
    FigureEditNotFoundError,
    FigureEditRecord,
    FigureEditRepository,
    FigureEditStateError,
    edit_image_path,
)
from .characters import CharacterNotFoundError, CharacterRepository
from .comfyui_provision import (
    SOURCE_EXISTING,
    ComfyUIProvisionError,
    classify_comfyui_source,
    comfyui_update_available,
    extract_archive,
    install_root_of,
    installed_comfyui_version,
    is_provisioned,
    load_comfyui_spec,
    managed_comfyui_paths,
    resolve_comfyui_install,
)
from .composition import (
    MAX_CHARACTERS,
    MAX_REFERENCE_IMAGES,
    KIND_BACKGROUND,
    KIND_CHARACTER,
    CompositionError,
    StillImageReference,
    build_composition_prompt,
    build_composition_workflow,
    resolve_still_image_figure,
    resolve_still_image_sources,
)
from .placed_composition import (
    POSE_HEIGHT,
    POSE_WIDTH,
    blend_mask,
    build_blend_prompt,
    build_blend_workflow,
    build_pose_prompt,
    build_pose_workflow,
    compose_draft,
    cut_out,
    default_layout,
    finish_blend,
    pose_outdated,
    resolve_placed_sources,
)
from .database import Database
from .derivation import (
    DerivationError,
    build_background_variation_prompt,
    build_figure_variation_prompt,
    resolve_background_variation_sources,
    resolve_figure_variation_sources,
)
from .deletion import delete_asset as delete_asset_records
from .deletion import delete_character as delete_character_records
from .deletion import delete_project as delete_project_records
from .deletion import delete_still_image as delete_still_image_records
from .deletion import delete_shot as delete_shot_records
from .deletion import (
    summarize_asset_deletion,
    summarize_character_deletion,
    summarize_project_deletion,
    summarize_still_image_deletion,
    summarize_shot_deletion,
)
from .exporting import (
    ExportError,
    concat_videos,
    cut_frame_rates,
    export_root,
    find_ffmpeg,
    list_project_shot_videos,
    project_export_path,
)
from .image_files import (
    ImageNotAvailableError,
    UnsupportedImageError,
    guess_content_type,
    read_served_image,
    read_served_video,
    save_uploaded_image,
    validate_image_upload,
)
from .hardware import load_profiles, probe_hardware, recommend
from .preferences import PreferenceRepository, PreferencesRead, PreferencesUpdate
from .projects import ProjectNotFoundError, ProjectRepository
from .requirements import check_requirements, load_requirements
from .setup_config import SetupConfigError, SetupConfigRepository
from .still_image_characters import StillImageCharacterRepository
from .still_images import StillImageNotFoundError, StillImageReferenceError, StillImageRepository
from .shots import (
    ShotBeatNotFoundError,
    ShotBeatRepository,
    ShotNotFoundError,
    ShotPromptLockedError,
    ShotRepository,
    ShotSpeakerError,
    prompt_signature,
)
from .shot_video import (
    DEFAULT_VIDEO_NEGATIVE,
    Beat,
    SpokenLine,
    build_recompose_prompt,
    build_shot_prompt,
    frames_for_seconds,
    prune_end_image,
    seconds_for_frames,
)
from .translation import (
    TranslationCache,
    TranslationKind,
    TranslationSettings,
    TranslationUnavailableError,
    contains_japanese,
    translate_once,
    translation_workflow_path,
)
from .video_frames import extract_last_frame
import shutil
from .schemas import (
    AssetCreate,
    VideoCandidateGenerateRequest,
    AssetGenerateRequest,
    BatchDeletionResult,
    CandidateCleanupResult,
    AssetGenerateResult,
    AssetDeletionPreview,
    BackgroundVariationRequest,
    BackgroundVariationResult,
    AssetRead,
    AssetUpdate,
    DEFAULT_COMPOSITION_DIFFUSION_MODEL,
    DEFAULT_COMPOSITION_LIGHTNING_LORA,
    DEFAULT_COMPOSITION_TEXT_ENCODER,
    DEFAULT_COMPOSITION_VAE,
    COMPOSITION_HEIGHT,
    COMPOSITION_WIDTH,
    CompositionLimits,
    CompositionMethod,
    CharacterCreate,
    CharacterDeletionPreview,
    CharacterFigureEditRead,
    CharacterFigureEditRequest,
    CharacterFigureGenerateRequest,
    CharacterFigureJobResult,
    CharacterFigureRead,
    CharacterFigureOrder,
    CharacterFigureUpdate,
    CharacterRead,
    PortraitCropRequest,
    PortraitOption,
    PortraitSelectRequest,
    CharacterUpdate,
    StillImageCharacterRead,
    StillImageCharacterSpec,
    StillImageCharacterUpdate,
    StillImagePoseRequest,
    StillImagePoseResult,
    ComfyUIStatus,
    CompositionQuality,
    CompositionRequest,
    CompositionResult,
    ExportRequest,
    ExportResult,
    ProjectExportStatus,
    SaveAsRequest,
    SaveAsResult,
    ExportStatus,
    HardwareReport,
    ManagedRuntimeStatus,
    ModelScanRequest,
    ProjectCreate,
    ProjectDeletionPreview,
    ProjectRead,
    ProjectUpdate,
    RequirementToolStatus,
    RequirementsReport,
    SetupConfigRead,
    ENGINE_VIDEO_FORMATS,
    SetupConfigUpdate,
    SetupMode,
    SetupStatus,
    StillImageCreate,
    StillImageDeletionPreview,
    StillImageOrder,
    StillImageRead,
    StillImageUpdate,
    StillImageUsageInfo,
    ModelDownloadRequest,
    ShotBeatCreate,
    ShotBeatRead,
    ShotBeatUpdate,
    ShotCreate,
    ShotDeletionPreview,
    ShotFrameRequest,
    ShotFrameResult,
    ShotFrameTarget,
    ShotGenerateRequest,
    ShotGenerateResult,
    ShotPromptEdit,
    ShotPromptModeUpdate,
    ShotPromptRequest,
    ShotPromptResult,
    ShotPromptSegment,
    ShotPromptState,
    VideoEngine,
    ShotRead,
    ShotSpeechLineRead,
    ShotUpdate,
    VideoEnabledUpdate,
)
from .orphans import remove_unreferenced_files
from .settings import settings
from .storage import (
    asset_dir,
    shot_frame_dir,
    shot_last_frame_dir,
    shot_segment_dir,
    shot_video_dir,
    still_image_dir,
    still_image_pose_dir,
)

database = Database(settings.database_path)
jobs = JobRepository(database)
T = TypeVar("T")

# GPUを使うジョブ。順番待ち（gpu_queue）へ入れるものはすべてここに載せる。
# 載っていないと、再起動したときに順番待ちのまま取り残される。
VIDEO_GPU_JOB_KINDS: tuple[str, ...] = (
    "composition.still_image",
    "composition.still_image_pose",
    "composition.shot_frame",
    "generation.asset",
    "generation.prompt.shot",
    "generation.video.shot",
    "character.candidates",
    "character.figures",
)
_VIDEO_JOB_LABELS: dict[str, str] = {
    "composition.still_image": "静止画の合成",
    "composition.still_image_pose": "人物の姿づくり",
    "composition.shot_frame": "構図の作り直し",
    "generation.asset": "背景・小物の生成",
    "generation.prompt.shot": "生成に使う文の用意",
    "generation.video.shot": "カットの動画生成",
    "character.candidates": "人物候補の生成",
    "character.figures": "立ち絵の生成",
}


def describe_video_job_kind(kind: str) -> str:
    return _VIDEO_JOB_LABELS.get(kind, kind)


gpu_slot = GpuSlot(VIDEO_GPU_JOB_KINDS, describe_video_job_kind)
# GPUを使う処理の順番待ち。実行中でも断らず、前の処理が終わったら次を始める。
gpu_queue = GpuQueue(VIDEO_GPU_JOB_KINDS)
# managed_* モードで、このAPIが起動・所有する専用ComfyUI。
runtime_manager = RuntimeManager()
# 専用ComfyUIの管理領域（runtimeプロファイル・yaml等をここへ置く。アプリ固有）。
MANAGED_ROOT = settings.data_dir / "managed"
def managed_runtime_options() -> ManagedRuntimeOptions:
    """管理モードでComfyUIを起動するときの領域と名前空間。

    保存先はモジュール属性を都度読む。取り込み時に固定すると、差し替えた保存先を
    使う試験が実データへ書き込んでしまう。カスタムノードは許可する。
    """
    return ManagedRuntimeOptions(
        managed_root=MANAGED_ROOT,
        namespace="chotto_sashie",
        app_label="ちょっと挿絵",
        disable_custom_nodes=False,
    )

# ワークフロー・マニフェスト・同梱物の場所。リポジトリ内で動かしているか、
# インストールされているか、配布版かで違うため、決め方は shared_env に任せる。
_RESOURCES_ROOT = app_resources_root()
COMPOSITION_WORKFLOW_PATH = _RESOURCES_ROOT / "workflows" / "composition" / "qwen_scene_composition.json"
# 置いて合成するときに、人物の姿を切り抜く背景除去。
BACKGROUND_REMOVAL_WORKFLOW_PATH = (
    _RESOURCES_ROOT / "workflows" / "composition" / "background_removal.json"
)
# 背景・小物のtxt2imgは Z-Image Turbo のテンプレートを使う。
ASSET_GENERATION_WORKFLOW_PATH = _RESOURCES_ROOT / "workflows" / "z_image" / "zimage_basic_core.json"
# カットのI2Vは2種類から選ぶ。Wan 2.2（無音webp）とMiniMax H3（音声付きmp4）。
SHOT_VIDEO_WORKFLOW_PATHS = {
    "wan22": _RESOURCES_ROOT / "workflows" / "video" / "wan_i2v.json",
    "minimax_h3": _RESOURCES_ROOT / "workflows" / "video" / "minimax_h3_i2v.json",
}
# 各機能が要求するComfyUIノード・モデルの一覧。
REQUIREMENTS_MANIFEST_PATH = _RESOURCES_ROOT / "model-manifests" / "video-requirements.json"
# 専用環境モードで取得するモデル(URL・SHA256・サイズの実値)。
MODELS_MANIFEST_PATH = _RESOURCES_ROOT / "model-manifests" / "video-models.json"
# アプリが用意するComfyUI本体(公式ポータブル)。
COMFYUI_MANIFEST_PATH = _RESOURCES_ROOT / "model-manifests" / "comfyui-portable.json"
# 書き出し（カットの結合）に使う処理エンジン。
FFMPEG_MANIFEST_PATH = _RESOURCES_ROOT / "model-manifests" / "ffmpeg.json"
# この端末の構成から既定値を決めるための基準表（しきい値も文言もここに置く）。
HARDWARE_PROFILES_PATH = _RESOURCES_ROOT / "model-manifests" / "hardware-profiles.json"

# 否定条件の既定（Wanのみ。組み立ての既定文は shot_video 側に持つ）。
_DEFAULT_VIDEO_NEGATIVE = DEFAULT_VIDEO_NEGATIVE



async def _autostart_runtime() -> None:
    """管理モードなら、アプリ起動時に専用ComfyUIを自動で立ち上げる。"""
    message = await autostart_managed_comfyui(
        SetupConfigRepository(database).get(), managed_runtime_options(), runtime_manager
    )
    print(f"[chotto-sashie] {message}")


@asynccontextmanager
async def lifespan(_: FastAPI):
    database.initialize()
    jobs.recover_interrupted()
    # 1枚ごとの記録は別表にあり、親ジョブを倒すだけでは「生成中」のまま残って
    # 画面に居座る。ComfyUIごと落ちたときに取り残されるので、同じ理由を書いて倒す。
    for repository in (
        CandidateRepository(database),
        CharacterFigureRepository(database),
        FigureEditRepository(database),
    ):
        repository.recover_interrupted(INTERRUPTED_MESSAGE)
    # 削除の途中で落ちるなどして、どの記録からも辿れなくなったファイルを片付ける。
    # 生成はまだ1件も動いていないので、書きかけのものと取り違えない。
    removed = remove_unreferenced_files(database, settings.data_dir)
    if removed:
        print(f"[chotto-sashie] どの記録からも使われていないファイルを{removed}件削除しました")
    # 順番待ちはこのプロセス内にしか無い。前回の待ち行列は畳んでから始める。
    mark_queue_lost(jobs, VIDEO_GPU_JOB_KINDS)
    gpu_queue.start()
    # 起動完了を待たずに進める（ComfyUIの読み込みに時間がかかるため）。
    autostart = asyncio.create_task(_autostart_runtime())
    try:
        yield
    finally:
        autostart.cancel()
        await gpu_queue.stop()
        await close_progress_watchers()
        # このAPIが起動した専用ComfyUIだけを停止する。外部プロセスには触れない。
        runtime_manager.shutdown()


app = FastAPI(
    title="ちょっと挿絵 ローカルAPI",
    version=__version__,
    lifespan=lifespan,
)
# Tauri 2のWindows本番WebViewが使用する送信元。
_DESKTOP_ORIGIN = "http://tauri.localhost"


def _allowed_origins() -> list[str]:
    """このAPIの応答を読んでよい送信元。

    読み取り（GET）はトークンで守れない。ブラウザの ``<img>`` がヘッダを付けられず、
    画像がすべて表示できなくなるためである。そのぶん、応答を読める送信元は絞る。

    デスクトップ版が起動したAPI（＝トークンを渡されている）では、開発用の
    ``localhost:1421`` を許さない。配布した先でその口を開けたままにすると、
    たまたま1421番を使う別のプログラムから、作品や画像を読み出せてしまう。
    開発時（トークン未設定）は、Vite開発サーバーから使うので許す。
    """
    if settings.api_token:
        return [_DESKTOP_ORIGIN]
    return [settings.development_origin, "http://127.0.0.1:1421", _DESKTOP_ORIGIN]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins(),
    allow_credentials=False,
    # PUT はセットアップ設定の保存で使う。許可漏れがあると本番WebViewから
    # プリフライトで弾かれ、画面側では「押しても何も起きない」ように見える。
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "X-Chotto-Sashie-Token"],
)


@app.exception_handler(GpuBusyError)
async def handle_gpu_busy(_: Request, error: GpuBusyError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(error)})


# 状態を変える操作にトークンを要求する。読み取り（GET）と画像表示は、ブラウザの
# <img> がヘッダを付けられないため対象外にする。トークン未設定の開発時は無効。
_TOKEN_PROTECTED_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@app.middleware("http")
async def require_api_token(request: Request, call_next):
    token = settings.api_token
    if token and request.method in _TOKEN_PROTECTED_METHODS:
        if request.headers.get("X-Chotto-Sashie-Token") != token:
            return JSONResponse(
                status_code=401,
                content={"detail": "APIトークンが必要です"},
            )
    return await call_next(request)


@app.get("/health")
def health() -> dict[str, str]:
    """このポートで動いているAPIの正体を返す。

    デスクトップ版は、同じポートに居るAPIへ黙って繋がないためにこれを見る。
    別のアプリや、別の保存先を使うAPIだった場合に取り違えると、作品が
    見当たらない・保存先が食い違うといった分かりにくい事故になる。
    """
    return {
        "status": "ok",
        "app": "chotto-sashie",
        "service": settings.app_name,
        "version": app.version,
        "data_dir": str(settings.data_dir),
    }


def _resolve_base_url(explicit: str | None) -> str:
    """明示指定があればそれを、無ければセットアップ設定の接続先を使う。"""
    if explicit:
        return explicit.rstrip("/")
    configured = SetupConfigRepository(database).resolved_base_url()
    if configured:
        return configured
    raise HTTPException(
        status_code=409,
        detail="ComfyUIの接続先が未設定です。セットアップでモードと接続先を設定してください",
    )


@app.get("/api/v1/setup/config", response_model=SetupConfigRead)
def get_setup_config() -> SetupConfigRead:
    return SetupConfigRepository(database).get()


@app.put("/api/v1/setup/config", response_model=SetupConfigRead)
def put_setup_config(request: SetupConfigUpdate) -> SetupConfigRead:
    try:
        config = SetupConfigRepository(database).set(request)
    except SetupConfigError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return config


@app.put("/api/v1/setup/video-enabled", response_model=SetupConfigRead)
def put_video_enabled(request: VideoEnabledUpdate) -> SetupConfigRead:
    """動画も作るかどうかを切り替える。

    切ると、シーン・カット・書き出しの画面を出さず、動画用モデルの取得と要件確認も
    対象から外す。作った静止画や取得済みのモデルはそのまま残る。
    """
    return SetupConfigRepository(database).set_video_enabled(request.enabled)


@app.post("/api/v1/setup/complete", response_model=SetupConfigRead)
def complete_setup() -> SetupConfigRead:
    """初回セットアップの完了を記録する（以後メイン画面にセットアップを出さない）。"""
    repo = SetupConfigRepository(database)
    if not repo.get().configured:
        raise HTTPException(status_code=409, detail="先にセットアップ内容を保存してください")
    return repo.mark_completed()


@app.post("/api/v1/setup/reset", response_model=SetupConfigRead)
def reset_setup() -> SetupConfigRead:
    """再セットアップ。設定値は残したまま、ウィザードをもう一度表示する。"""
    return SetupConfigRepository(database).reset_completion()


@app.post("/api/v1/setup/config/clear", response_model=SetupConfigRead)
def clear_setup_config() -> SetupConfigRead:
    """設定を初期化して最初から。保存済みの設定レコードだけを消す。

    参照していた外部モデルフォルダや既存ComfyUIには触れないため、
    やり直しても手持ちの環境は元のまま残る。
    """
    # 覚えている既定値（前回選んだ生成方法・枚数）も一緒に忘れる。接続先だけ消えて
    # 前の端末向けのおすすめが残っていると、初期化した意味が無くなるため。
    PreferenceRepository(database).clear()
    return SetupConfigRepository(database).clear()


async def _hardware_report(explicit_base_url: str | None = None) -> HardwareReport:
    """この端末の構成を調べ、基準表と突き合わせて既定値を提案する。"""
    config = SetupConfigRepository(database).get()
    # 未設定のうちは、応答しないURLを待たない（セットアップの途中でも判定できるよう
    # nvidia-smiだけで済ませる）。
    target = explicit_base_url or (config.resolved_base_url if config.configured else "")
    probe = await probe_hardware(target or None)
    try:
        manifest = load_profiles(HARDWARE_PROFILES_PATH)
    except (OSError, ValueError) as error:
        raise HTTPException(
            status_code=500, detail=f"おすすめの基準表を読み込めません: {error}"
        ) from error
    recommendation = recommend(probe, manifest, video_enabled=config.video_enabled)
    current = PreferenceRepository(database).get()
    applied = bool(recommendation.settings) and all(
        str(getattr(current, setting.key, "")) == setting.value
        for setting in recommendation.settings
    )
    return HardwareReport(
        detected=probe.detected,
        source=probe.source,
        gpu_name=probe.gpu.name,
        vram_total_bytes=probe.gpu.vram_total_bytes,
        vram_free_bytes=probe.gpu.vram_free_bytes,
        ram_total_bytes=probe.ram_total_bytes,
        compute_capability=probe.gpu.compute_capability,
        driver_version=probe.gpu.driver_version,
        message=probe.message,
        recommendation=recommendation,
        applied=applied,
    )


@app.get("/api/v1/setup/hardware", response_model=HardwareReport)
async def get_hardware(base_url: str | None = Query(default=None)) -> HardwareReport:
    """GPU・メモリを調べ、この端末に合う既定値を提案する。

    ComfyUIが動いていればその見立て（空きVRAMまで分かる）を使い、動いていなければ
    nvidia-smiで判断する。どちらも取れなくても失敗にはせず、`detected=false` を返す。
    提案するだけで、保存済みの選択は書き換えない。
    """
    return await _hardware_report(base_url)


@app.post("/api/v1/setup/hardware/apply", response_model=PreferencesRead)
async def apply_hardware_recommendation() -> PreferencesRead:
    """提案された既定値を、次回からの既定として保存する。"""
    report = await _hardware_report()
    if not report.recommendation.settings:
        raise HTTPException(
            status_code=409,
            detail="この端末の構成を確認できないため、おすすめを適用できません",
        )
    values = {setting.key: setting.value for setting in report.recommendation.settings}
    values["recommendation_tier"] = report.recommendation.tier
    return PreferenceRepository(database).apply(values)


@app.get("/api/v1/preferences", response_model=PreferencesRead)
def get_preferences() -> PreferencesRead:
    """前回選んだ生成方法・枚数（画面の初期表示に使う）。"""
    return PreferenceRepository(database).get()


@app.put("/api/v1/preferences", response_model=PreferencesRead)
def put_preferences(request: PreferencesUpdate) -> PreferencesRead:
    """画面で選び直した値を覚える。送らなかった項目は今の値のまま。"""
    return PreferenceRepository(database).set(request)


@app.get("/api/v1/setup/status", response_model=SetupStatus)
async def get_setup_status() -> SetupStatus:
    config = SetupConfigRepository(database).get()
    if not config.configured:
        return SetupStatus(
            mode=config.mode,
            configured=False,
            resolved_base_url="",
            reachable=None,
            message="セットアップが未設定です。モードと接続先を設定してください",
            data_dir=str(settings.data_dir),
        )
    # モードに関わらず実際につながるかを確かめる。制作画面の状態表示がこれを根拠にする。
    existing = config.mode == SetupMode.EXISTING_COMFYUI
    try:
        await ComfyClient(config.resolved_base_url).object_info()
        reachable = True
        message = "既存のComfyUIへ接続できました" if existing else "専用ComfyUIに接続できました"
    except ComfyConnectionError:
        reachable = False
        message = (
            "既存のComfyUIへ接続できません。ComfyUIが起動しているか確認してください"
            if existing
            else "専用ComfyUIが起動していません。起動するまで生成は始められません"
        )
    return SetupStatus(
        mode=config.mode,
        configured=True,
        resolved_base_url=config.resolved_base_url,
        reachable=reachable,
        message=message,
        data_dir=str(settings.data_dir),
    )


@app.get("/api/v1/setup/requirements", response_model=RequirementsReport)
async def get_setup_requirements(base_url: str | None = Query(default=None)) -> RequirementsReport:
    """指定したComfyUIに、各機能が必要とするノード・モデルが揃っているか確認する。"""
    resolved = _resolve_base_url(base_url)
    try:
        object_info = await ComfyClient(resolved).object_info()
    except ComfyConnectionError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    manifest = load_requirements(REQUIREMENTS_MANIFEST_PATH)
    return check_requirements(
        object_info,
        manifest,
        include_video=SetupConfigRepository(database).get().video_enabled,
        tool_status=_local_tool_status,
    )


def _local_tool_status(tool_id: str, display_name: str) -> RequirementToolStatus:
    """ComfyUIの外で動く道具の充足を答える（要件チェックから呼ばれる）。"""
    if tool_id == "ffmpeg":
        return _ffmpeg_tool_status(display_name)
    return RequirementToolStatus(
        id=tool_id,
        display_name=display_name,
        present=False,
        hint="このアプリでは確認できません。",
    )


def _ffmpeg_tool_status(display_name: str) -> RequirementToolStatus:
    """書き出しに使う処理エンジンの充足。無いときは、用意できるかと量を添える。"""
    found = find_ffmpeg()
    if found is not None:
        return RequirementToolStatus(
            id="ffmpeg",
            display_name=display_name,
            present=True,
            detail=found,
        )
    spec = _ffmpeg_spec()
    configured = settings.ffmpeg_path
    if configured:
        # 場所を指定しているときは、その指定を直すのが先（黙って別のものを用意しない）。
        return RequirementToolStatus(
            id="ffmpeg",
            display_name=display_name,
            present=False,
            detail=configured,
            hint=f"指定された場所に見つかりません: {configured}",
        )
    return RequirementToolStatus(
        id="ffmpeg",
        display_name=display_name,
        present=False,
        can_install=spec is not None,
        install_size_bytes=spec.size_bytes if spec is not None else 0,
        # 外部の配布物を入れることになるので、取得元と利用条件を押す前に見せる。
        source=spec.source if spec is not None else "",
        license=spec.license if spec is not None else "",
        hint="" if spec is not None else "用意するものの情報が見つかりません。",
    )


@app.post("/api/v1/setup/runtime/start", response_model=ManagedRuntimeStatus)
def start_managed_runtime() -> ManagedRuntimeStatus:
    """設定済みのmanaged_*モードで、このAPIが所有する専用ComfyUIを起動する。

    既存モデル参照モードでは外部モデルを読み取り専用で参照し、外部環境は変更しない。
    すでに応答しているとき、または読み込み中でまだ応答しないときは二重起動せず
    現在の状態を返す。
    """
    config = SetupConfigRepository(database).get()
    if config.configured and config.mode != SetupMode.EXISTING_COMFYUI:
        current = runtime_manager.status(config.port)
        if current["reachable"] or current["state"] == "starting":
            return ManagedRuntimeStatus.model_validate(current)
    try:
        status = start_managed_comfyui(config, managed_runtime_options(), runtime_manager)
    except (ManagedRuntimeUnavailableError, ManagedConfigError, RuntimeManagerError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return ManagedRuntimeStatus.model_validate(status)


@app.post("/api/v1/setup/runtime/stop", response_model=ManagedRuntimeStatus)
def stop_managed_runtime() -> ManagedRuntimeStatus:
    """このAPIが起動した専用ComfyUIだけを停止する。外部プロセスは停止しない。"""
    return ManagedRuntimeStatus.model_validate(runtime_manager.stop())


@app.get("/api/v1/setup/runtime/status", response_model=ManagedRuntimeStatus)
def managed_runtime_status() -> ManagedRuntimeStatus:
    config = SetupConfigRepository(database).get()
    return ManagedRuntimeStatus.model_validate(runtime_manager.status(config.port))


@app.get("/api/v1/setup/comfyui/status", response_model=ComfyUIStatus)
def get_comfyui_status() -> ComfyUIStatus:
    """使えるComfyUI本体があるかを返す。"""
    spec_version = ""
    try:
        manifest = json.loads(COMFYUI_MANIFEST_PATH.read_text(encoding="utf-8"))
        spec_version = str(manifest.get("target_version", ""))
    except (OSError, ValueError):
        spec_version = ""
    config = SetupConfigRepository(database).get()
    install = resolve_comfyui_install(config.comfyui_path, config.python_path)
    source = classify_comfyui_source(install[0]) if install else ""
    installed_version = installed_comfyui_version(install[0]) if install else ""
    install_root = install_root_of(install[0]) if install else None
    return ComfyUIStatus(
        provisioned=install is not None,
        target_version=spec_version,
        installed_version=installed_version,
        update_available=comfyui_update_available(installed_version, spec_version),
        comfyui_path=str(install[0]) if install else None,
        python_path=str(install[1]) if install else None,
        source=source,
        # 共有フォルダのものはアプリから更新できる。
        # 利用者が指定した既存インストールだけは、こちらから触らない。
        can_update=source != SOURCE_EXISTING,
        install_root=str(install_root) if install_root else "",
        shared_root=str(shared_comfyui_root()),
    )


async def run_comfyui_provision(job_id: str) -> None:
    """公式ComfyUIポータブルを取得・展開して共有フォルダへ用意する。"""
    try:
        spec = load_comfyui_spec(COMFYUI_MANIFEST_PATH)
        jobs.transition(
            job_id,
            JobTransition(state="preparing", progress=0.02, message="ComfyUIを取得します"),
        )
        # 入れ替える前に、自分が起動したComfyUIを止めて本体を掴んだままにしない。
        runtime_manager.stop()
        target_root = shared_comfyui_root()
        jobs.transition(
            job_id,
            JobTransition(state="running", progress=0.05, message="ComfyUIをダウンロードしています"),
        )
        archive = await asyncio.to_thread(
            download_with_resume,
            target_root,
            spec,
            should_cancel=lambda: jobs.is_cancelled(job_id),
        )
        jobs.transition(
            job_id,
            JobTransition(state="running", progress=0.7, message="ComfyUIを展開しています"),
        )
        await asyncio.to_thread(extract_archive, archive, target_root)
        if not is_provisioned(target_root):
            raise ComfyUIProvisionError("展開後にComfyUI本体を確認できませんでした")
        jobs.transition(
            job_id,
            JobTransition(state="validating", progress=0.98, message="ComfyUIを確認しています"),
        )
        jobs.transition(
            job_id,
            JobTransition(state="completed", message="ComfyUI環境の準備が完了しました"),
        )
    except (DownloadError, ComfyUIProvisionError, OSError) as error:
        try:
            jobs.transition(
                job_id,
                JobTransition(state="failed", message=str(error), error_code="COMFYUI_PROVISION_FAILED"),
            )
        except (InvalidTransitionError, JobNotFoundError):
            return


@app.post("/api/v1/setup/comfyui/provision", response_model=None)
async def start_comfyui_provision(background_tasks: BackgroundTasks):
    """ComfyUI本体の準備(ダウンロード+展開)ジョブを開始する。二重起動は防ぐ。"""
    if jobs.find_active(["setup.comfyui.provision"]) is not None:
        raise HTTPException(status_code=409, detail="ComfyUIの準備がすでに実行中です")
    # 入れ替え・移動の前に、自分が起動したComfyUIを止めて本体を掴んだままにしない。
    runtime_manager.stop()
    job = jobs.create(JobCreate(kind="setup.comfyui.provision", payload={}))
    background_tasks.add_task(run_comfyui_provision, job.id)
    return job


@app.post("/api/v1/models/scan", response_model=ModelScanResult)
def scan_models(request: ModelScanRequest) -> ModelScanResult:
    """既存モデルフォルダの中身を読み取りだけで確認する（外部は変更しない）。"""
    return scan_model_roots(
        [Path(root) for root in request.roots],
        calculate_sha256=request.calculate_sha256,
        max_files=request.max_files,
    )


def configured_external_model_files(max_files: int = 20000) -> list[Path]:
    """設定済みの既存モデルフォルダにあるモデルファイルを集める（読み取りのみ）。

    「既存モデルを参照する」モードのときだけ対象。ここで集めた候補と照合して、
    手持ちと同一のファイルはダウンロードを省く。
    """
    config = SetupConfigRepository(database).get()
    if not config.configured or config.mode != SetupMode.MANAGED_EXTERNAL_MODELS:
        return []
    roots = [Path(root) for root in config.external_model_roots if root.strip()]
    # 既存ComfyUI本体を流用する設定なら、その models フォルダも候補に含める。
    if config.comfyui_path.strip():
        models = Path(config.comfyui_path).expanduser() / "models"
        if models.is_dir():
            roots.append(models)
    if not roots:
        return []
    scanned = scan_model_roots(roots, calculate_sha256=False, max_files=max_files)
    return [Path(found.path) for found in scanned.files if not found.incomplete]


def model_storage() -> ModelStorage:
    """設定に従ってモデルの取得先を決める（既存フォルダ指定なら妥当性も確かめる）。"""
    config = SetupConfigRepository(database).get()
    return resolve_model_storage(config, verify=False)


def _download_specs_for_setup() -> list:
    """この設定で必要になるモデルだけを返す。

    動画を作らない設定では、動画用のモデル（数十GB）を必要容量にも取得対象にも含めない。
    """
    specs = load_download_specs(MODELS_MANIFEST_PATH)
    if SetupConfigRepository(database).get().video_enabled:
        return specs
    return [spec for spec in specs if not spec.video_only]


@app.get("/api/v1/setup/models/plan")
def get_models_plan() -> dict:
    """必要なモデルの取得計画（既存/不足/必要容量/取得先）を返す。"""
    specs = _download_specs_for_setup()
    external = configured_external_model_files()
    try:
        storage = model_storage()
    except ModelStorageError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    # 画面から即座に返すため、外部ファイルはサイズ一致までの高速判定にする。
    # 実際に取得を省くかどうかは取得ジョブでSHA-256まで照合して決める。
    plan = build_setup_plan(
        storage.root,
        specs,
        external_files=external,
        extra_roots=storage.managed_roots,
        verify_external_hashes=False,
    )
    # 参照フォルダの指定が正しいか画面で分かるよう、走査できた件数も返す。
    plan["external_candidate_files"] = len(external)
    plan["download_target"] = storage.target
    plan["download_root"] = str(storage.root)
    plan["download_label"] = storage.label
    plan["search_roots"] = [str(root) for root in storage.search_roots]
    # 取得を押す前に見せる利用条件（制限のあるものだけ）。文面はマニフェスト側にある。
    plan["license_notices"] = _model_license_notices()
    return plan


def _model_license_notices() -> dict:
    """まとまりごとの利用条件を、マニフェストからそのまま返す。

    Apache-2.0 のように制限のないものは載せない。利用者の地域や用途によって
    使えないことがあるものだけを、取得の手前で読めるようにする。
    """
    try:
        manifest = json.loads(MODELS_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    notices = manifest.get("license_notices")
    if not isinstance(notices, dict):
        return {}
    return {
        key: value
        for key, value in notices.items()
        if isinstance(value, dict) and value.get("points_ja")
    }


async def run_model_download(job_id: str, specs: list) -> None:
    """不足しているモデルだけを、検証付き・再開可能で選ばれた取得先へ取得する。"""
    try:
        jobs.transition(
            job_id,
            JobTransition(state="preparing", progress=0.02, message="モデルの取得計画を確認しています"),
        )
        # 書き込み先は毎回ここで確かめ直す（既存フォルダへ書き足す設定のときは、
        # 参照フォルダとして登録済みで、実際に書き込めることを確認してから進む）。
        storage = await asyncio.to_thread(
            resolve_model_storage,
            SetupConfigRepository(database).get(),
        )
        jobs.transition(
            job_id,
            JobTransition(
                state="running",
                progress=0.05,
                message=f"モデルを取得しています（保存先: {storage.label}）",
            ),
        )
        external = await asyncio.to_thread(configured_external_model_files)
        total = len(specs)
        for index, spec in enumerate(specs, start=1):
            relative = models_relative_path(storage.root, spec.destination)
            if inspect_managed_file(storage.root, spec, relative_path=relative).state == "ready":
                continue
            # 別のモデルフォルダ（共有フォルダ・旧配置・既存フォルダ）にあるものは使う。
            if find_existing_model(storage.managed_roots, spec) is not None:
                jobs.transition(
                    job_id,
                    JobTransition(
                        state="running",
                        progress=min(0.95, 0.05 + 0.9 * index / total),
                        message=f"{spec.display_name_ja} はすでにあるものを使います（{index}/{total}）",
                    ),
                )
                continue
            if external and spec.external_read_only_reuse:
                jobs.transition(
                    job_id,
                    JobTransition(
                        state="running",
                        progress=min(0.95, 0.05 + 0.9 * (index - 1) / total),
                        message=f"{spec.display_name_ja} を手持ちのモデルと照合しています",
                    ),
                )
                # 手持ちと同一（SHA-256一致）なら読み取り専用で使い、取得しない。
                reusable = await asyncio.to_thread(find_matching_external_file, external, spec)
                if reusable is not None:
                    jobs.transition(
                        job_id,
                        JobTransition(
                            state="running",
                            progress=min(0.95, 0.05 + 0.9 * index / total),
                            message=(
                                f"{spec.display_name_ja} は手持ちのモデルを利用します"
                                f"（{index}/{total}）"
                            ),
                        ),
                    )
                    continue
            # 同期・ブロッキングな取得をワーカースレッドへ逃がしイベントループを止めない。
            await asyncio.to_thread(
                download_with_resume,
                storage.root,
                spec,
                should_cancel=lambda: jobs.is_cancelled(job_id),
                relative_path=relative,
            )
            jobs.transition(
                job_id,
                JobTransition(
                    state="running",
                    progress=min(0.95, 0.05 + 0.9 * index / total),
                    message=f"{spec.display_name_ja} を取得しました（{index}/{total}）",
                ),
            )
        jobs.transition(
            job_id,
            JobTransition(state="validating", progress=0.98, message="取得したモデルを確認しています"),
        )
        jobs.transition(
            job_id,
            JobTransition(state="completed", message=f"モデルの準備が完了しました（{storage.label}）"),
        )
    except (DownloadError, ModelStorageError, OSError) as error:
        try:
            jobs.transition(
                job_id,
                JobTransition(state="failed", message=str(error), error_code="MODEL_DOWNLOAD_FAILED"),
            )
        except (InvalidTransitionError, JobNotFoundError):
            return


@app.post("/api/v1/setup/models/download", response_model=None)
async def start_model_download(
    background_tasks: BackgroundTasks,
    request: ModelDownloadRequest | None = None,
):
    """不足モデルの取得ジョブを開始する。二重起動は防ぐ。

    任意のモデル（MiniMax H3 など）は、まとまりの名前を指定したときだけ取得する。
    """
    if jobs.find_active(["setup.models.download"]) is not None:
        raise HTTPException(status_code=409, detail="モデルの取得がすでに実行中です")
    groups = set(request.groups) if request else set()
    specs = [
        spec
        for spec in _download_specs_for_setup()
        if not spec.optional or spec.group in groups
    ]
    if not specs:
        raise HTTPException(status_code=409, detail="取得対象のモデルがありません")
    job = jobs.create(JobCreate(kind="setup.models.download", payload={}))
    background_tasks.add_task(run_model_download, job.id, specs)
    return job


# --- 画像アップロード／配信の共通処理（背景・小物・人物で共有） ---


async def _store_uploaded_image(image_root: Path, entity_id: str, file: UploadFile) -> Path:
    data = await file.read()
    try:
        extension = validate_image_upload(file.content_type, data)
    except UnsupportedImageError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return save_uploaded_image(image_root, entity_id, data, extension)


def _record_or_discard(
    path: Path, record: Callable[[], T], missing: tuple[type[BaseException], ...]
) -> T:
    """書いたファイルを記録へ結び付ける。記録が消えていたら、ファイルも消す。

    生成の途中で利用者が人物や静止画を消すと、出来上がったファイルの行き先が無くなる。
    結び付けられなかったものを保存先に残すと、どこからも辿れないごみになる。
    """
    try:
        return record()
    except missing:
        # いま自分で書いたファイルなので、場所を確かめ直さずに消してよい。
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass  # 掴まれていて消せなくても、次の起動で片付く（orphans）。
        raise


def _comfy_scratch(base_url: str) -> ComfyScratch:
    """この接続先のComfyUIに残る写しを片付ける係。

    片付けるのは専用ComfyUIのときだけ。入力・出力フォルダがアプリの管理領域にあり、
    アプリの持ち物だから。既存ComfyUIのフォルダは利用者のものなので触らない。
    """
    config = SetupConfigRepository(database).get()
    if (
        config.mode == SetupMode.EXISTING_COMFYUI
        or base_url.rstrip("/") != config.resolved_base_url
    ):
        return ComfyScratch()
    input_dir, output_dir = comfy_io_directories(managed_runtime_options().managed_root)
    return ComfyScratch(input_dir, output_dir)


async def _upload_local_image(
    client: ComfyClient, image_path: Path, name: str, uploaded: list[str]
) -> str:
    """アプリの保存先にある画像をComfyUIへ送り、LoadImageへ渡す名前を返す。

    送った名前は `uploaded` へ足す。生成を見届けたら `ComfyScratch.discard_inputs` で
    片付ける（送った画像はアプリ側に実体があるので、ComfyUI側に残す理由が無い）。
    """
    return await _upload_image_bytes(
        client,
        image_path.read_bytes(),
        f"{name}{image_path.suffix}",
        guess_content_type(image_path),
        uploaded,
    )


async def _upload_image_bytes(
    client: ComfyClient, data: bytes, filename: str, content_type: str, uploaded: list[str]
) -> str:
    """作った画像（置いて合成するときの下書きなど）をComfyUIへ送り、LoadImageへ渡す名前を返す。"""
    sent = await client.upload_image(data, filename=filename, content_type=content_type)
    comfy_name = f"{sent['subfolder']}/{sent['name']}" if sent["subfolder"] else sent["name"]
    uploaded.append(comfy_name)
    return comfy_name


async def _candidate_image_path(candidate: CandidateRead) -> Path:
    """人物候補の画像の、アプリの保存先にある控え。無ければComfyUIから取って作る。"""
    path = local_image_path(settings.data_dir, CANDIDATE_IMAGES, candidate.id)
    if path.is_file():
        return path
    if candidate.image is None:
        raise HTTPException(status_code=409, detail="この画像はまだ生成中です")
    try:
        content, _media_type = await ComfyClient(candidate.base_url).view_image(
            candidate.image.model_dump()
        )
    except ComfyConnectionError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    stored = await asyncio.to_thread(
        write_local_image, settings.data_dir, CANDIDATE_IMAGES, candidate.id, content
    )
    if not stored.is_file():
        raise HTTPException(status_code=500, detail="人物候補の画像を保存できませんでした")
    return stored


def _serve_image_response(image_root: Path, image_path: Path | None) -> Response:
    if image_path is None:
        raise HTTPException(status_code=404, detail="画像が登録されていません")
    try:
        content, media_type = read_served_image(image_root, image_path)
    except ImageNotAvailableError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return Response(
        content=content,
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


# --- プロジェクト ---


@app.post("/api/v1/projects", response_model=ProjectRead, status_code=201)
def create_project(request: ProjectCreate) -> ProjectRead:
    return ProjectRepository(database).create(request)


@app.get("/api/v1/projects", response_model=list[ProjectRead])
def list_projects() -> list[ProjectRead]:
    return ProjectRepository(database).list()


@app.get("/api/v1/projects/{project_id}", response_model=ProjectRead)
def get_project(project_id: str) -> ProjectRead:
    try:
        return ProjectRepository(database).get(project_id)
    except ProjectNotFoundError as error:
        raise HTTPException(status_code=404, detail="作品が見つかりません") from error


@app.patch("/api/v1/projects/{project_id}", response_model=ProjectRead)
def update_project(project_id: str, request: ProjectUpdate) -> ProjectRead:
    """作品名・説明・作品全体の画風を更新する。"""
    try:
        return ProjectRepository(database).update(project_id, request)
    except ProjectNotFoundError as error:
        raise HTTPException(status_code=404, detail="作品が見つかりません") from error


@app.get(
    "/api/v1/projects/{project_id}/deletion-preview",
    response_model=ProjectDeletionPreview,
)
def preview_project_deletion(project_id: str) -> ProjectDeletionPreview:
    """この作品を消すと何が一緒に消えるかを返す（削除確認の材料）。"""
    try:
        return ProjectDeletionPreview(**vars(summarize_project_deletion(database, project_id)))
    except KeyError as error:
        raise HTTPException(status_code=404, detail="作品が見つかりません") from error


@app.delete("/api/v1/projects/{project_id}", status_code=204)
def delete_project(project_id: str) -> Response:
    """作品と、その人物・素材・静止画・カット・生成物をまとめて削除する。

    削除するのはアプリ管理データ保存先の内側だけで、参照中のモデルフォルダや
    既存ComfyUIには触れない。
    """
    try:
        delete_project_records(database, settings.data_dir, project_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="作品が見つかりません") from error
    return Response(status_code=204)


# --- 登場人物 ---

# 一覧に出す代表写真（顔が見える1枚）。合成に使う立ち絵とは別に持つ。
_CHARACTER_PORTRAIT_ROOT = portraits_dir(settings.data_dir)


@app.post("/api/v1/projects/{project_id}/characters", response_model=CharacterRead, status_code=201)
def create_character(project_id: str, request: CharacterCreate) -> CharacterRead:
    try:
        return CharacterRepository(database).create(project_id, request)
    except ProjectNotFoundError as error:
        raise HTTPException(status_code=404, detail="作品が見つかりません") from error


@app.get("/api/v1/projects/{project_id}/characters", response_model=list[CharacterRead])
def list_characters(project_id: str) -> list[CharacterRead]:
    try:
        return CharacterRepository(database).list(project_id)
    except ProjectNotFoundError as error:
        raise HTTPException(status_code=404, detail="作品が見つかりません") from error


@app.get("/api/v1/characters/{character_id}", response_model=CharacterRead)
def get_character(character_id: str) -> CharacterRead:
    try:
        return CharacterRepository(database).get(character_id)
    except CharacterNotFoundError as error:
        raise HTTPException(status_code=404, detail="登場人物が見つかりません") from error


@app.patch("/api/v1/characters/{character_id}", response_model=CharacterRead)
def update_character(character_id: str, request: CharacterUpdate) -> CharacterRead:
    try:
        return CharacterRepository(database).update(character_id, request)
    except CharacterNotFoundError as error:
        raise HTTPException(status_code=404, detail="登場人物が見つかりません") from error


@app.get(
    "/api/v1/characters/{character_id}/deletion-preview",
    response_model=CharacterDeletionPreview,
)
def preview_character_deletion(character_id: str) -> CharacterDeletionPreview:
    """この登場人物を消すと何が一緒に消え、どの静止画から外れるかを返す。"""
    try:
        summary = summarize_character_deletion(database, character_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="登場人物が見つかりません") from error
    return CharacterDeletionPreview(
        **{**vars(summary), "usage": StillImageUsageInfo(**vars(summary.usage))}
    )


@app.delete("/api/v1/characters/{character_id}", status_code=204)
def delete_character(character_id: str) -> Response:
    """登場人物と、その候補・立ち絵を削除し、静止画の参照からも外す。"""
    try:
        delete_character_records(database, settings.data_dir, character_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="登場人物が見つかりません") from error
    return Response(status_code=204)


# --- 代表写真（一覧に出す顔写真） ---
#
# 合成に使う立ち絵は全身なので、そのまま一覧に出すと胴体のあたりだけが写り、誰なのか
# 分からない。表示専用の1枚をここで別に持つ。差し替えても立ち絵にも合成にも影響しない。


def _portrait_source_image(character_id: str) -> tuple[Path, Path, bool]:
    """一覧に出す写真の元画像（範囲を切り出す前）の保存領域・場所と、代表写真かどうか。

    代表写真を決めていなければ既定の立ち絵（このときは範囲の指定を当てない）。
    """
    repository = CharacterRepository(database)
    try:
        portrait_path = repository.portrait_image_path(character_id)
    except CharacterNotFoundError as error:
        raise HTTPException(status_code=404, detail="登場人物が見つかりません") from error
    if portrait_path is not None and portrait_path.is_file():
        return _CHARACTER_PORTRAIT_ROOT, portrait_path, True
    figure = CharacterFigureRepository(database).default_figure(character_id)
    if figure is None:
        raise HTTPException(status_code=404, detail="画像が登録されていません")
    return figures_dir(settings.data_dir), Path(figure.image_path), False


@app.get("/api/v1/characters/{character_id}/portrait")
def get_character_portrait(character_id: str) -> Response:
    """一覧に出す代表写真。範囲を決めていればその範囲だけ、決めていなければ全体を返す。"""
    image_root, image_path, is_portrait = _portrait_source_image(character_id)
    # 範囲は代表写真に合わせて決めたもの。写真が見当たらず立ち絵が出るときには当てない。
    crop = CharacterRepository(database).portrait_crop(character_id) if is_portrait else None
    if crop is None:
        return _serve_image_response(image_root, image_path)
    try:
        content, _ = read_served_image(image_root, image_path)
    except ImageNotAvailableError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    cropped, media_type = crop_portrait(content, crop)
    return Response(
        content=cropped,
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@app.get("/api/v1/characters/{character_id}/portrait/source")
def get_character_portrait_source(character_id: str) -> Response:
    """範囲を切り出す前の代表写真（範囲を決める画面で、全体を見せるために使う）。"""
    image_root, image_path, _ = _portrait_source_image(character_id)
    return _serve_image_response(image_root, image_path)


@app.put("/api/v1/characters/{character_id}/portrait/crop", response_model=CharacterRead)
def set_character_portrait_crop(
    character_id: str, request: PortraitCropRequest
) -> CharacterRead:
    """一覧に出す範囲を決める（None で全体へ戻す）。保存してある画像そのものは切らない。

    代表写真を決めていない（既定の立ち絵が出ている）ときは、いま出ている立ち絵を
    代表写真として固定してから範囲を記録する。既定の立ち絵は後から入れ替わることが
    あり、範囲だけが別の画像へ当たってしまうため。
    """
    repository = CharacterRepository(database)
    try:
        portrait_path = repository.portrait_image_path(character_id)
    except CharacterNotFoundError as error:
        raise HTTPException(status_code=404, detail="登場人物が見つかりません") from error
    if request.crop is not None and (portrait_path is None or not portrait_path.is_file()):
        figure = CharacterFigureRepository(database).default_figure(character_id)
        if figure is None or not figure.image_path:
            raise HTTPException(status_code=404, detail="画像が登録されていません")
        _save_figure_as_portrait(repository, character_id, figure)
    return repository.set_portrait_crop(character_id, request.crop)


@app.post("/api/v1/characters/{character_id}/portrait", response_model=CharacterRead)
async def upload_character_portrait(
    character_id: str, file: UploadFile = File(...)
) -> CharacterRead:
    """手持ちの写真を代表写真にする（合成に使う立ち絵はそのまま）。"""
    repository = CharacterRepository(database)
    try:
        repository.get(character_id)
    except CharacterNotFoundError as error:
        raise HTTPException(status_code=404, detail="登場人物が見つかりません") from error
    data = await file.read()
    try:
        extension = validate_image_upload(file.content_type, data)
    except UnsupportedImageError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    destination = save_portrait(_CHARACTER_PORTRAIT_ROOT, character_id, data, extension)
    return repository.set_portrait_image(character_id, destination, source="upload")


@app.get(
    "/api/v1/characters/{character_id}/portrait-options",
    response_model=list[PortraitOption],
)
def list_character_portrait_options(character_id: str) -> list[PortraitOption]:
    """代表写真に選べる画像を並べる。

    人物候補（顔が大きく写る） → 立ち絵、の順。手持ちの画像を使いたいときは、
    この一覧ではなくアップロードで差し替える。

    人物候補を先に置くのは、一覧で見分けるのは顔だから。立ち絵は全身なので、
    サムネイルにすると誰なのか分かりにくい。
    """
    repository = CharacterRepository(database)
    try:
        character = repository.get(character_id)
    except CharacterNotFoundError as error:
        raise HTTPException(status_code=404, detail="登場人物が見つかりません") from error

    options: list[PortraitOption] = []
    candidates = [
        candidate
        for candidate in CandidateRepository(database).list_for_character(character_id)
        if candidate.state == "completed" and candidate.url
    ]
    # 基準画像を先頭に出す（その人物の顔として選ばれている1枚）。
    for candidate in sorted(candidates, key=lambda item: (not item.is_base, item.ordinal)):
        option_id = f"{CANDIDATE_OPTION_PREFIX}{candidate.id}"
        options.append(
            PortraitOption(
                id=option_id,
                label="基準画像" if candidate.is_base else f"人物候補{candidate.ordinal}",
                url=str(candidate.url),
                selected=character.portrait_source == option_id,
            )
        )

    figures = [
        figure
        for figure in CharacterFigureRepository(database).list_for_character(character_id)
        if figure.state == "completed" and figure.image_path
    ]
    for index, figure in enumerate(figures, start=1):
        option_id = f"{FIGURE_OPTION_PREFIX}{figure.id}"
        options.append(
            PortraitOption(
                id=option_id,
                label=figure.label or f"立ち絵{index}",
                # 立ち絵は置き換えられることがあるので、版を付けて古い画像を出さない。
                url=(
                    f"/api/v1/character-figures/{figure.id}/image?v="
                    f"{quote(figure_image_version(figure.image_path), safe='')}"
                ),
                selected=character.portrait_source == option_id,
            )
        )
    return options


@app.post("/api/v1/characters/{character_id}/portrait/select", response_model=CharacterRead)
async def select_character_portrait(
    character_id: str, request: PortraitSelectRequest
) -> CharacterRead:
    """一覧の中から代表写真を選び直す。合成に使う立ち絵はそのまま残る。"""
    repository = CharacterRepository(database)
    try:
        repository.get(character_id)
    except CharacterNotFoundError as error:
        raise HTTPException(status_code=404, detail="登場人物が見つかりません") from error

    option_id = request.option_id
    if option_id == DEFAULT_OPTION_ID:
        # 既定に戻す。以後は既定の立ち絵がそのまま一覧に出る。写しはもう使わない。
        updated = repository.set_portrait_image(character_id, None)
        delete_portrait(_CHARACTER_PORTRAIT_ROOT, character_id)
        return updated

    figure_id = parse_figure_option_id(option_id)
    if figure_id is not None:
        try:
            figure = CharacterFigureRepository(database).get(figure_id)
        except CharacterFigureNotFoundError as error:
            raise HTTPException(status_code=404, detail="立ち絵が見つかりません") from error
        if figure.character_id != character_id or not figure.image_path:
            raise HTTPException(status_code=404, detail="立ち絵が見つかりません")
        return _save_figure_as_portrait(repository, character_id, figure)

    candidate_id = parse_candidate_option_id(option_id)
    if candidate_id is None:
        raise HTTPException(status_code=422, detail="代表写真に選べない指定です")
    try:
        candidate = CandidateRepository(database).get(candidate_id)
    except CandidateNotFoundError as error:
        raise HTTPException(status_code=404, detail="人物候補が見つかりません") from error
    if candidate.character_id != character_id:
        raise HTTPException(status_code=404, detail="人物候補が見つかりません")
    content = (await _candidate_image_path(candidate)).read_bytes()
    destination = save_portrait(_CHARACTER_PORTRAIT_ROOT, character_id, content, ".png")
    return repository.set_portrait_image(character_id, destination, source=option_id)


def _save_figure_as_portrait(
    repository: CharacterRepository, character_id: str, figure: CharacterFigureRead
) -> CharacterRead:
    """立ち絵の写しを代表写真にする（立ち絵が置き換えられても一覧の写真は変わらない）。"""
    content = Path(figure.image_path).read_bytes()
    destination = save_portrait(_CHARACTER_PORTRAIT_ROOT, character_id, content, ".png")
    return repository.set_portrait_image(
        character_id, destination, source=f"{FIGURE_OPTION_PREFIX}{figure.id}"
    )


# --- 立ち絵 ---

# 立ち絵の実体は `assets/character-figures/` に置く（代表写真とも、生成の控えとも別）。
# 場所は呼ばれたときに引く。取り込みと取り出しで同じ関数を通し、保存先の設定が
# 変わったときに片方だけ古い場所を指すことがないようにする。


def _figure_edit_read(edit: FigureEditRecord) -> CharacterFigureEditRead:
    has_image = edit.state == "completed" and bool(edit.image_path)
    return CharacterFigureEditRead(
        id=edit.id,
        figure_id=edit.figure_id,
        state=edit.state,
        has_image=has_image,
        url=f"/api/v1/character-figure-edits/{edit.id}/image" if has_image else None,
        instruction=edit.instruction,
        prop_asset_ids=edit.prop_asset_ids,
        job_id=edit.job_id,
        error_message=edit.error_message,
        created_at=edit.created_at,
        updated_at=edit.updated_at,
    )


def _figure_read(
    figure: CharacterFigureRecord, edit: FigureEditRecord | None = None
) -> CharacterFigureRead:
    has_image = figure.state == "completed" and bool(figure.image_path)
    return CharacterFigureRead(
        id=figure.id,
        character_id=figure.character_id,
        label=figure.label,
        origin=figure.origin,
        state=figure.state,
        seed=figure.seed,
        is_default=figure.is_default,
        has_image=has_image,
        url=f"/api/v1/character-figures/{figure.id}/image" if has_image else None,
        image_version=figure_image_version(figure.image_path) if has_image else "",
        instruction=figure.instruction,
        prop_asset_ids=figure.prop_asset_ids,
        source_figure_id=figure.source_figure_id,
        job_id=figure.job_id,
        ordinal=figure.ordinal,
        error_message=figure.error_message,
        edit=_figure_edit_read(edit) if edit is not None else None,
        created_at=figure.created_at,
        updated_at=figure.updated_at,
    )


def _figure_or_404(figure_id: str) -> CharacterFigureRecord:
    try:
        return CharacterFigureRepository(database).get(figure_id)
    except CharacterFigureNotFoundError as error:
        raise HTTPException(status_code=404, detail="立ち絵が見つかりません") from error


@app.get("/api/v1/characters/{character_id}/figures", response_model=list[CharacterFigureRead])
def list_character_figures(character_id: str) -> list[CharacterFigureRead]:
    _character_or_404(character_id)
    figures = CharacterFigureRepository(database).list_for_character(character_id)
    edits = FigureEditRepository(database).by_figure_for_character(character_id)
    return [_figure_read(figure, edits.get(figure.id)) for figure in figures]


@app.put("/api/v1/characters/{character_id}/figures/order", response_model=list[CharacterFigureRead])
def reorder_character_figures(
    character_id: str, request: CharacterFigureOrder
) -> list[CharacterFigureRead]:
    """立ち絵を並べ替える。誰も既定を選んでいなければ、先頭の1枚が既定になる。"""
    _character_or_404(character_id)
    try:
        figures = CharacterFigureRepository(database).reorder(character_id, request.figure_ids)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    edits = FigureEditRepository(database).by_figure_for_character(character_id)
    return [_figure_read(figure, edits.get(figure.id)) for figure in figures]


@app.post(
    "/api/v1/characters/{character_id}/figures/generate",
    response_model=CharacterFigureJobResult,
)
async def generate_character_figure(
    character_id: str,
    request: CharacterFigureGenerateRequest,
) -> CharacterFigureJobResult:
    """基準画像から、正面全身の立ち絵を1枚作る。

    作るのは正面だけで、向きは変えない。姿勢は合成のときに指定すれば付くので、
    ここで向き違いや姿勢違いを揃える必要はない（正面1枚で足りることは実機で確認済み）。
    衣装や小物は、出来た1枚を「変更」で変える。
    """
    _character_or_404(character_id)
    base_url = _resolve_base_url(request.base_url)
    # 投入してからComfyUIの英語エラーで落ちるより先に、不足を日本語で伝える。
    await _ensure_diffusion_model_available(base_url, request.diffusion_model)

    candidates = CandidateRepository(database)
    try:
        base_candidate = candidates.get_base(character_id)
    except CandidateSelectionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    # 起点はアプリの保存先の控えから送る。ComfyUIの出力フォルダを指すと、そこを
    # 片付けたり接続先を変えたりした時点で、基準画像があるのに作れなくなる。
    base_image_path = await _candidate_image_path(base_candidate)
    seed = request.seed if request.seed is not None else randbelow(9_007_199_254_740_967)
    payload = {
        **request.model_dump(mode="json"),
        "character_id": character_id,
        "base_candidate_id": base_candidate.id,
        "seed": seed,
    }
    repository = CharacterFigureRepository(database)
    scratch = _comfy_scratch(base_url)

    async def run(job_id: str) -> None:
        uploaded: list[str] = []
        try:
            template = load_workflow(angle_reference_workflow_path(_RESOURCES_ROOT))
            client = ComfyClient(base_url)
            source_image = await _upload_local_image(
                client, base_image_path, f"{job_id}-base", uploaded
            )
            figure = repository.create(
                character_id,
                origin=ORIGIN_BASE,
                job_id=job_id,
                base_candidate_id=base_candidate.id,
                prompt=FIGURE_PROMPT,
                seed=seed,
                base_url=base_url,
            )
            workflow = bind_angle_reference_workflow(
                template,
                prompt=FIGURE_PROMPT,
                seed=seed,
                source_image=source_image,
                diffusion_model=request.diffusion_model,
                text_encoder=request.text_encoder,
                vae=request.vae,
                angle_lora=request.angle_lora,
                lightning_lora=request.lightning_lora,
                filename_prefix=f"{OUTPUT_ROOT_NAME}/characters/{character_id}/figures/{job_id}",
                weight_dtype=request.weight_dtype,
                angle_lora_strength=request.angle_lora_strength,
            )
            # 4ステップでは「頭から靴まで入れる」という組み直しの指示が弱く、
            # 胸像のまま返ることがある。参照画像の全身と同じ20ステップで作る。
            apply_high_precision_angle(workflow)
            prompt_id = await client.submit(workflow)
            repository.mark_running(figure.id, prompt_id)

            jobs.transition(
                job_id,
                JobTransition(
                    state="preparing",
                    progress=0.05,
                    message="立ち絵の生成をキューへ登録しました",
                    result={"figure_id": figure.id},
                ),
            )
            await monitor_figure_image(
                job_id,
                figure.id,
                base_url,
                records=repository,
                destination=figure_image_path(settings.data_dir, figure.id),
                jobs=jobs,
                scratch=scratch,
            )
        except (ComfyConnectionError, OSError, ValueError) as error:
            repository.fail_batch(job_id, str(error))
            _fail_job(job_id, str(error), "FIGURE_SUBMIT_FAILED")
        finally:
            scratch.discard_inputs(uploaded)

    job = await gpu_queue.submit(
        jobs,
        JobCreate(kind="character.figures", payload=payload),
        lambda job_id: partial(run, job_id),
    )
    # 実体は順番が来てから作られる。画面はジョブを見て取り直す。
    return CharacterFigureJobResult(job=job)


@app.post(
    "/api/v1/character-figures/{figure_id}/edits",
    response_model=CharacterFigureJobResult,
)
async def start_character_figure_edit(
    figure_id: str, request: CharacterFigureEditRequest
) -> CharacterFigureJobResult:
    """いまある立ち絵の衣装・小物を変える（立ち絵の変更）。

    出来た1枚はすぐには反映せず、この立ち絵に預けておく。採用すると元の立ち絵を置き換える
    （元も残したいときは、先に複製しておく）。前に預かっていた結果は捨てる。

    **丁寧な設定（高速化LoRA無し）で作る。** 4ステップでは「衣装を変える」という指示の
    反映が弱いうえ、全身・素背景という立ち絵の条件も崩れやすい。腰から上だけの1枚が
    できても立ち絵としては使えないので、ここは速さより出来上がりを取る。1枚あたりの
    時間は数分になる。
    """
    source = _figure_or_404(figure_id)
    if source.state != "completed" or not source.image_path:
        raise HTTPException(status_code=409, detail="まだ出来ていない立ち絵は変更できません")
    character = _character_or_404(source.character_id)

    base_url = _resolve_base_url(request.base_url)
    await _ensure_diffusion_model_available(base_url, DEFAULT_COMPOSITION_DIFFUSION_MODEL)

    try:
        sources = resolve_figure_variation_sources(database, source, request.prop_asset_ids)
    except (DerivationError, AssetNotFoundError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error

    prompt = build_figure_variation_prompt(
        request.instruction,
        len(sources) - 1,
        _project_illustration_style(character.project_id),
    )
    seed = request.seed if request.seed is not None else randbelow(9_007_199_254_740_967)
    edits = FigureEditRepository(database)
    try:
        edit, previous_image = edits.start(
            source,
            instruction=request.instruction,
            prop_asset_ids=request.prop_asset_ids,
            prompt=prompt,
            seed=seed,
            base_url=base_url,
        )
    except FigureEditStateError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if previous_image is not None:
        previous_image.unlink(missing_ok=True)
    payload = {
        **request.model_dump(mode="json"),
        "character_id": source.character_id,
        "figure_id": source.id,
        "edit_id": edit.id,
        "seed": seed,
    }
    scratch = _comfy_scratch(base_url)

    async def run(job_id: str) -> None:
        uploaded: list[str] = []
        try:
            # 順番待ちの間に中止されたものは、ComfyUIへ送らずに終える。
            if jobs.is_cancelled(job_id):
                edits.fail(edit.id, CANCEL_MESSAGE)
                return
            template = load_workflow(COMPOSITION_WORKFLOW_PATH)
            client = ComfyClient(base_url)
            references: list[StillImageReference] = []
            for index, item in enumerate(sources, start=1):
                comfy_name = await _upload_local_image(
                    client, item.image_path, f"{job_id}-{index}", uploaded
                )
                references.append(
                    StillImageReference(kind=item.kind, name="", comfy_image=comfy_name)
                )
            workflow = build_composition_workflow(
                template,
                references=references,
                prompt=prompt,
                negative_prompt="",
                seed=seed,
                width=768,
                height=1024,
                diffusion_model=DEFAULT_COMPOSITION_DIFFUSION_MODEL,
                text_encoder=DEFAULT_COMPOSITION_TEXT_ENCODER,
                vae=DEFAULT_COMPOSITION_VAE,
                lightning_lora=DEFAULT_COMPOSITION_LIGHTNING_LORA,
                filename_prefix=(
                    f"{OUTPUT_ROOT_NAME}/characters/{source.character_id}/figure-edits/{job_id}"
                ),
                careful=True,
            )
            prompt_id = await client.submit(workflow)
            edits.mark_running(edit.id, prompt_id)
            jobs.transition(
                job_id,
                JobTransition(
                    state="preparing",
                    progress=0.05,
                    message="立ち絵の変更をキューへ登録しました",
                    result={"edit_id": edit.id},
                ),
            )
            await monitor_figure_image(
                job_id,
                edit.id,
                base_url,
                records=edits,
                destination=edit_image_path(settings.data_dir, edit.id),
                jobs=jobs,
                scratch=scratch,
                wording=FigureJobWording(
                    working="立ち絵を変更しています",
                    finished="変更した立ち絵ができました",
                    failed="立ち絵を変更できませんでした",
                ),
            )
        except (
            ComfyConnectionError,
            CompositionError,
            DerivationError,
            InvalidTransitionError,
            OSError,
            ValueError,
        ) as error:
            edits.fail(edit.id, str(error))
            _fail_job(job_id, str(error), "FIGURE_EDIT_SUBMIT_FAILED")
        finally:
            scratch.discard_inputs(uploaded)
            # どの道筋で終わっても「変更中」のまま残さない（画面が待ち続けるため）。
            # 出来上がったものは fail が上書きしない。
            edits.fail(edit.id, "立ち絵の変更を最後まで進められませんでした")

    try:
        job = await gpu_queue.submit(
            jobs,
            JobCreate(kind="character.figures", payload=payload),
            lambda job_id: partial(run, job_id),
        )
    except Exception:
        # 頼めなかった変更を「変更中」のまま残さない。
        edits.fail(edit.id, "立ち絵の変更を始められませんでした")
        raise
    edits.set_job(edit.id, job.id)
    return CharacterFigureJobResult(job=job)


def _figure_edit_or_404(edit_id: str) -> FigureEditRecord:
    try:
        return FigureEditRepository(database).get(edit_id)
    except FigureEditNotFoundError as error:
        raise HTTPException(status_code=404, detail="変更した立ち絵が見つかりません") from error


@app.get("/api/v1/character-figure-edits/{edit_id}/image")
def get_character_figure_edit_image(edit_id: str) -> Response:
    edit = _figure_edit_or_404(edit_id)
    if not edit.image_path:
        raise HTTPException(status_code=404, detail="変更した立ち絵はまだ出来ていません")
    return _serve_image_response(figures_dir(settings.data_dir), Path(edit.image_path))


@app.post("/api/v1/character-figure-edits/{edit_id}/save", response_model=CharacterFigureRead)
def save_character_figure_edit(edit_id: str) -> CharacterFigureRead:
    """変更の結果を採用し、元の立ち絵を置き換える。"""
    _figure_edit_or_404(edit_id)
    try:
        figure_id = FigureEditRepository(database).save(edit_id, data_dir=settings.data_dir)
    except FigureEditNotFoundError as error:
        raise HTTPException(status_code=404, detail="変更した立ち絵が見つかりません") from error
    except FigureEditStateError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _figure_read(CharacterFigureRepository(database).get(figure_id))


@app.post(
    "/api/v1/character-figures/{figure_id}/duplicate",
    response_model=CharacterFigureRead,
    status_code=201,
)
def duplicate_character_figure(figure_id: str) -> CharacterFigureRead:
    """立ち絵をそのまま複製して、同じ人物の立ち絵を1枚増やす。

    元を残したまま衣装や小物を変えたいときは、複製してから変更する。
    """
    _figure_or_404(figure_id)
    try:
        duplicate = CharacterFigureRepository(database).duplicate(
            figure_id, data_dir=settings.data_dir
        )
    except CharacterFigureNotFoundError as error:
        raise HTTPException(status_code=404, detail="立ち絵が見つかりません") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _figure_read(duplicate)


@app.delete("/api/v1/character-figure-edits/{edit_id}", status_code=204)
def discard_character_figure_edit(edit_id: str) -> Response:
    """変更の結果を保存せずに捨てる。元の立ち絵はそのまま。"""
    _figure_edit_or_404(edit_id)
    try:
        image_path = FigureEditRepository(database).discard(edit_id)
    except FigureEditStateError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if image_path is not None:
        image_path.unlink(missing_ok=True)
    return Response(status_code=204)


@app.post("/api/v1/characters/{character_id}/figures/upload", response_model=CharacterFigureRead)
async def upload_character_figure(
    character_id: str, file: UploadFile = File(...)
) -> CharacterFigureRead:
    """手持ちの全身画像を、この人物の立ち絵として登録する。"""
    _character_or_404(character_id)
    # 先に受け取って置いてから記録を作る。記録を先に作ると、受け取れなかったときに
    # 「まだ出来ていない立ち絵」が一覧へ残り、生成中扱いで消せなくなる。
    figure_id = str(uuid4())
    destination = await _store_uploaded_image(figures_dir(settings.data_dir), figure_id, file)
    figure = CharacterFigureRepository(database).create(
        character_id,
        origin=ORIGIN_UPLOAD,
        figure_id=figure_id,
        state="completed",
        image_path=str(destination),
    )
    return _figure_read(figure)


@app.get("/api/v1/character-figures/{figure_id}/image")
def get_character_figure_image(figure_id: str) -> Response:
    figure = _figure_or_404(figure_id)
    if not figure.image_path:
        raise HTTPException(status_code=404, detail="この立ち絵はまだ出来ていません")
    return _serve_image_response(figures_dir(settings.data_dir), Path(figure.image_path))


@app.patch("/api/v1/character-figures/{figure_id}", response_model=CharacterFigureRead)
def update_character_figure(
    figure_id: str, request: CharacterFigureUpdate
) -> CharacterFigureRead:
    """見出しを付け替える／既定の1枚にする。"""
    repository = CharacterFigureRepository(database)
    figure = _figure_or_404(figure_id)
    if request.label is not None:
        figure = repository.set_label(figure_id, request.label)
    if request.is_default:
        try:
            figure = repository.set_default(figure_id)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
    return _figure_read(figure)


@app.delete("/api/v1/character-figures/{figure_id}", status_code=204)
def delete_character_figure(figure_id: str) -> Response:
    """立ち絵を消す。実体の持ち主はこの記録なので、ファイルもここで片付ける。

    預かっている変更の結果も一緒に消える。変更中のものがあるうちは断る（出来上がった
    画像の行き先が無くなるため）。
    """
    repository = CharacterFigureRepository(database)
    edit = FigureEditRepository(database).for_figure(figure_id)
    if edit is not None and edit.active:
        raise HTTPException(
            status_code=409, detail="変更中の立ち絵は削除できません。先に変更を中止してください"
        )
    try:
        image_path = repository.delete(figure_id)
    except CharacterFigureNotFoundError as error:
        raise HTTPException(status_code=404, detail="立ち絵が見つかりません") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if image_path is not None:
        image_path.unlink(missing_ok=True)
    if edit is not None and edit.image_path:
        Path(edit.image_path).unlink(missing_ok=True)
    return Response(status_code=204)


# --- 背景資産・小物資産（同一構造をルーターファクトリで生成） ---


async def monitor_asset_generation(
    job_id: str,
    table: str,
    asset_id: str,
    prompt_id: str,
    base_url: str,
    prompt: str,
    *,
    input_prompt: str = "",
    poll_seconds: float = 0.5,
    max_polls: int = 3600,
) -> None:
    """背景・小物の生成を見届け、完成画像を素材へ保存する。

    `prompt` は ComfyUI へ渡した指示文、`input_prompt` は「生成プロンプト」欄へ入れた文
    （欄から作らないバリエーションでは空）。どちらも出来上がった画像と一緒に記録する。
    """
    repository = AssetRepository(database, table)
    image_root = asset_dir(settings.data_dir, table)
    client = ComfyClient(base_url)

    async def save(files: list[dict[str, str]]) -> dict[str, object]:
        previous = repository.image_path(asset_id)
        content, _media_type = await client.view_image(files[0])
        destination = save_uploaded_image(image_root, asset_id, content, ".png")
        _record_or_discard(
            destination,
            lambda: repository.set_image(
                asset_id, destination, source_prompt=prompt, input_prompt=input_prompt
            ),
            (AssetNotFoundError,),
        )
        if previous is not None and previous != destination:
            previous.unlink(missing_ok=True)
        return {"asset_id": asset_id, "table": table}

    await monitor_comfy_prompt(
        client=client,
        jobs=jobs,
        job_id=job_id,
        prompt_id=prompt_id,
        on_success=save,
        messages=MonitorMessages(
            running="素材を生成しています",
            saving="生成した素材を保存しています",
            completed="素材を生成しました",
            failed="ComfyUIで素材を生成できませんでした",
            failed_code="ASSET_GENERATION_FAILED",
            timeout="素材生成の待機がタイムアウトしました",
            timeout_code="ASSET_GENERATION_TIMEOUT",
            monitor_failed_code="ASSET_GENERATION_MONITOR_FAILED",
        ),
        settings=MonitorSettings(
            poll_seconds=poll_seconds,
            max_polls=max_polls,
            extra_errors=(AssetNotFoundError,),
        ),
        scratch=_comfy_scratch(base_url),
    )


@app.post(
    "/api/v1/background-assets/{asset_id}/variations",
    response_model=BackgroundVariationResult,
)
async def create_background_variation(
    asset_id: str, request: BackgroundVariationRequest
) -> BackgroundVariationResult:
    """元の背景から、小物を置いた／時間帯を変えた背景を作る。

    出来上がりは同じグループの背景として一覧に並ぶ。静止画では、そのどれか1枚を選ぶ。

    **時間帯は、背景1枚だけを渡す編集でしか変えられない。** 人物や小物を一緒に渡す
    合成の状況説明では変えられなかった（夕日の教室に「真昼」と書いても8件すべて夕日が
    残った）。だから時間帯の回では小物を受け取らない。

    合成と同じく**丁寧な設定（高速化LoRA無し）で作る**。4ステップでは「置く」「時間帯を
    変える」という指示の反映が弱い。1枚あたりの時間は数分になる。
    """
    backgrounds = AssetRepository(database, "background_assets")
    try:
        source = backgrounds.get(asset_id)
    except AssetNotFoundError as error:
        raise HTTPException(status_code=404, detail="背景が見つかりません") from error

    base_url = _resolve_base_url(request.base_url)
    await _ensure_diffusion_model_available(base_url, DEFAULT_COMPOSITION_DIFFUSION_MODEL)

    try:
        sources = resolve_background_variation_sources(database, source, request.prop_asset_ids)
    except (DerivationError, AssetNotFoundError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error

    prompt = build_background_variation_prompt(
        request.kind,
        request.instruction,
        len(sources) - 1,
        _project_illustration_style(source.project_id),
    )
    seed = request.seed if request.seed is not None else randbelow(9_007_199_254_740_967)
    label = request.variant_label or request.instruction[:30]
    # グループの元をたどってから渡す。派生の派生でも群れが割れないようにする。
    # 名前も元の背景の名前から付ける（派生の派生で「教室（夕方）（雨）」のように重ねない）。
    group_base = backgrounds.get(backgrounds.resolve_group_base(source.id))
    variation = backgrounds.create(
        source.project_id,
        AssetCreate(name=variation_name(group_base.name, label), category=source.category),
        base_asset_id=group_base.id,
        variant_label=label,
    )
    payload = {
        **request.model_dump(mode="json"),
        "source_asset_id": source.id,
        "asset_id": variation.id,
        "seed": seed,
    }

    async def run(job_id: str) -> None:
        client = ComfyClient(base_url)
        uploaded: list[str] = []
        try:
            template = load_workflow(COMPOSITION_WORKFLOW_PATH)
            references: list[StillImageReference] = []
            for index, item in enumerate(sources, start=1):
                comfy_name = await _upload_local_image(
                    client, item.image_path, f"{job_id}-{index}", uploaded
                )
                references.append(
                    StillImageReference(kind=item.kind, name="", comfy_image=comfy_name)
                )
            workflow = build_composition_workflow(
                template,
                references=references,
                prompt=prompt,
                negative_prompt="",
                seed=seed,
                width=1280,
                height=720,
                diffusion_model=DEFAULT_COMPOSITION_DIFFUSION_MODEL,
                text_encoder=DEFAULT_COMPOSITION_TEXT_ENCODER,
                vae=DEFAULT_COMPOSITION_VAE,
                lightning_lora=DEFAULT_COMPOSITION_LIGHTNING_LORA,
                filename_prefix=f"{OUTPUT_ROOT_NAME}/assets/background_assets/{variation.id}",
                careful=True,
            )
            prompt_id = await client.submit(workflow)
            jobs.transition(
                job_id,
                JobTransition(
                    state="preparing",
                    progress=0.1,
                    message="背景の作成をキューへ登録しました",
                    external_id=prompt_id,
                    result={"asset_id": variation.id, "table": "background_assets"},
                ),
            )
            await monitor_asset_generation(
                job_id, "background_assets", variation.id, prompt_id, base_url, prompt
            )
        except (ComfyConnectionError, CompositionError, DerivationError, OSError, ValueError) as error:
            _fail_job(job_id, str(error), "BACKGROUND_VARIATION_SUBMIT_FAILED")
        finally:
            _comfy_scratch(base_url).discard_inputs(uploaded)

    job = await gpu_queue.submit(
        jobs,
        JobCreate(kind="generation.asset", payload=payload),
        lambda job_id: partial(run, job_id),
    )
    return BackgroundVariationResult(asset=variation, job_id=job.id)


def _build_asset_router(*, table: str, path_prefix: str, not_found_message: str) -> APIRouter:
    router = APIRouter()
    image_root = asset_dir(settings.data_dir, table)

    def repository() -> AssetRepository:
        return AssetRepository(database, table)

    @router.post(
        f"/api/v1/projects/{{project_id}}/{path_prefix}",
        response_model=AssetRead,
        status_code=201,
    )
    def create_asset(project_id: str, request: AssetCreate) -> AssetRead:
        try:
            return repository().create(project_id, request)
        except ProjectNotFoundError as error:
            raise HTTPException(status_code=404, detail="作品が見つかりません") from error

    @router.get(f"/api/v1/projects/{{project_id}}/{path_prefix}", response_model=list[AssetRead])
    def list_assets(project_id: str) -> list[AssetRead]:
        try:
            return repository().list(project_id)
        except ProjectNotFoundError as error:
            raise HTTPException(status_code=404, detail="作品が見つかりません") from error

    @router.get(f"/api/v1/{path_prefix}/{{asset_id}}", response_model=AssetRead)
    def get_asset(asset_id: str) -> AssetRead:
        try:
            return repository().get(asset_id)
        except AssetNotFoundError as error:
            raise HTTPException(status_code=404, detail=not_found_message) from error

    @router.patch(f"/api/v1/{path_prefix}/{{asset_id}}", response_model=AssetRead)
    def update_asset(asset_id: str, request: AssetUpdate) -> AssetRead:
        try:
            return repository().update(asset_id, request)
        except AssetNotFoundError as error:
            raise HTTPException(status_code=404, detail=not_found_message) from error

    @router.get(
        f"/api/v1/{path_prefix}/{{asset_id}}/deletion-preview",
        response_model=AssetDeletionPreview,
    )
    def preview_asset_deletion(asset_id: str) -> AssetDeletionPreview:
        """この素材を消すと、どの静止画から外れるかを返す（削除確認の材料）。"""
        try:
            summary = summarize_asset_deletion(database, table, asset_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=not_found_message) from error
        return AssetDeletionPreview(
            **{**vars(summary), "usage": StillImageUsageInfo(**vars(summary.usage))}
        )

    @router.delete(f"/api/v1/{path_prefix}/{{asset_id}}", status_code=204)
    def delete_asset(asset_id: str) -> Response:
        """素材を削除する。小物は静止画の参照リストからも外す（背景は自動で外れる）。"""
        try:
            delete_asset_records(database, settings.data_dir, table, asset_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=not_found_message) from error
        return Response(status_code=204)

    @router.post(f"/api/v1/{path_prefix}/{{asset_id}}/image", response_model=AssetRead)
    async def upload_asset_image(asset_id: str, file: UploadFile = File(...)) -> AssetRead:
        repo = repository()
        try:
            previous_image = repo.image_path(asset_id)
        except AssetNotFoundError as error:
            raise HTTPException(status_code=404, detail=not_found_message) from error
        destination = await _store_uploaded_image(image_root, asset_id, file)
        updated = repo.set_image(asset_id, destination)
        if previous_image is not None and previous_image != destination:
            previous_image.unlink(missing_ok=True)
        return updated

    @router.get(f"/api/v1/{path_prefix}/{{asset_id}}/image")
    def get_asset_image(asset_id: str) -> Response:
        try:
            image_path = repository().image_path(asset_id)
        except AssetNotFoundError as error:
            raise HTTPException(status_code=404, detail=not_found_message) from error
        return _serve_image_response(image_root, image_path)

    @router.post(f"/api/v1/{path_prefix}/{{asset_id}}/generate", response_model=AssetGenerateResult)
    async def generate_asset_image(
        asset_id: str,
        request: AssetGenerateRequest,
        background_tasks: BackgroundTasks,
    ) -> AssetGenerateResult:
        repo = repository()
        try:
            asset = repo.get(asset_id)
        except AssetNotFoundError as error:
            raise HTTPException(status_code=404, detail=not_found_message) from error

        base_url = _resolve_base_url(request.base_url)
        seed = request.seed if request.seed is not None else randbelow(9_007_199_254_740_967)
        # 作品の画風は素材にも効かせる。ここだけ渡していないと、背景・小物だけが
        # 別の描かれ方になり、合成したときに人物と食い違う。
        prompt = build_asset_prompt(request.prompt, _project_illustration_style(asset.project_id))
        payload = {**request.model_dump(mode="json"), "asset_id": asset_id, "table": table, "seed": seed}
        async def run(job_id: str) -> None:
            client = ComfyClient(base_url)
            try:
                template = load_workflow(ASSET_GENERATION_WORKFLOW_PATH)
                workflow = bind_workflow(
                    template,
                    {
                        "prompt": prompt,
                        "negative_prompt": request.negative_prompt,
                        "seed": seed,
                        "width": request.width,
                        "height": request.height,
                        "diffusion_model": request.diffusion_model,
                        "text_encoder": request.text_encoder,
                        "vae": request.vae,
                        "filename_prefix": f"{OUTPUT_ROOT_NAME}/assets/{table}/{asset_id}",
                    },
                )
                prompt_id = await client.submit(workflow)
                jobs.transition(
                    job_id,
                    JobTransition(
                        state="preparing",
                        progress=0.1,
                        message="素材生成をキューへ登録しました",
                        external_id=prompt_id,
                        result={"asset_id": asset_id, "table": table},
                    ),
                )
                await monitor_asset_generation(
                    job_id, table, asset_id, prompt_id, base_url, prompt,
                    input_prompt=request.prompt,
                )
            except (ComfyConnectionError, OSError, ValueError) as error:
                _fail_job(job_id, str(error), "ASSET_GENERATION_SUBMIT_FAILED")

        job = await gpu_queue.submit(
            jobs,
            JobCreate(kind="generation.asset", payload=payload),
            lambda job_id: partial(run, job_id),
        )
        return AssetGenerateResult(asset=asset, job_id=job.id)

    return router


app.include_router(
    _build_asset_router(
        table="background_assets",
        path_prefix="background-assets",
        not_found_message="背景が見つかりません",
    )
)
app.include_router(
    _build_asset_router(
        table="prop_assets",
        path_prefix="prop-assets",
        not_found_message="小物が見つかりません",
    )
)


# --- 静止画 ---


@app.get("/api/v1/composition/limits", response_model=CompositionLimits)
def get_composition_limits() -> CompositionLimits:
    """合成が受け取れる参照画像の枚数と、1枚に入れられる人物の数。

    上限はQwen-Image-Editの合成ノードが持つ画像入力の数そのもので、こちらの
    都合で増やせない。画面が同じ数字を持たずに済むよう、ここから配る。
    """
    return CompositionLimits(
        max_reference_images=MAX_REFERENCE_IMAGES,
        max_characters=MAX_CHARACTERS,
        width=COMPOSITION_WIDTH,
        height=COMPOSITION_HEIGHT,
    )


@app.post("/api/v1/projects/{project_id}/still-images", response_model=StillImageRead, status_code=201)
def create_still_image(project_id: str, request: StillImageCreate) -> StillImageRead:
    try:
        return StillImageRepository(database).create(project_id, request)
    except ProjectNotFoundError as error:
        raise HTTPException(status_code=404, detail="作品が見つかりません") from error
    except StillImageReferenceError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/api/v1/projects/{project_id}/still-images", response_model=list[StillImageRead])
def list_still_images(project_id: str) -> list[StillImageRead]:
    try:
        return StillImageRepository(database).list(project_id)
    except ProjectNotFoundError as error:
        raise HTTPException(status_code=404, detail="作品が見つかりません") from error


@app.put("/api/v1/projects/{project_id}/still-images/order", response_model=list[StillImageRead])
def reorder_still_images(project_id: str, request: StillImageOrder) -> list[StillImageRead]:
    """静止画を並べ替える。シーンの並びと、全カットを書き出すときの順もこれに従う。"""
    try:
        return StillImageRepository(database).reorder(project_id, request.still_image_ids)
    except ProjectNotFoundError as error:
        raise HTTPException(status_code=404, detail="作品が見つかりません") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/v1/still-images/{still_image_id}", response_model=StillImageRead)
def get_still_image(still_image_id: str) -> StillImageRead:
    try:
        return StillImageRepository(database).get(still_image_id)
    except StillImageNotFoundError as error:
        raise HTTPException(status_code=404, detail="静止画が見つかりません") from error


@app.patch("/api/v1/still-images/{still_image_id}", response_model=StillImageRead)
def update_still_image(still_image_id: str, request: StillImageUpdate) -> StillImageRead:
    try:
        return StillImageRepository(database).update(still_image_id, request)
    except StillImageNotFoundError as error:
        raise HTTPException(status_code=404, detail="静止画が見つかりません") from error
    except StillImageReferenceError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/api/v1/still-images/{still_image_id}/deletion-preview", response_model=StillImageDeletionPreview)
def preview_still_image_deletion(still_image_id: str) -> StillImageDeletionPreview:
    """この静止画を消すと、いくつのカットと動画が一緒に消えるかを返す。"""
    try:
        return StillImageDeletionPreview(**vars(summarize_still_image_deletion(database, still_image_id)))
    except KeyError as error:
        raise HTTPException(status_code=404, detail="静止画が見つかりません") from error


@app.delete("/api/v1/still-images/{still_image_id}", status_code=204)
def delete_still_image(still_image_id: str) -> Response:
    """静止画と、そこから作ったカット・生成済み動画を削除する。"""
    try:
        delete_still_image_records(database, settings.data_dir, still_image_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="静止画が見つかりません") from error
    return Response(status_code=204)


# --- 静止画の合成 ---

_STILL_IMAGE_ROOT = still_image_dir(settings.data_dir)


async def monitor_still_image_composition(
    job_id: str,
    still_image_id: str,
    prompt_id: str,
    base_url: str,
    *,
    poll_seconds: float = 0.5,
    max_polls: int = 3600,
    source_prompt: str,
    finish: Callable[[bytes], bytes] | None = None,
) -> None:
    """合成のComfyUI履歴を監視し、完成した静止画をローカルへ保存する。

    `source_prompt` は ComfyUI へ渡した指示文で、画像と一緒に記録する（画面で参照する）。
    `finish` を渡すと、出来上がった画像をそれで仕上げてから保存する（置いて合成で、
    描き直した範囲の外を下書きの画素へ戻す）。
    """
    still_images = StillImageRepository(database)
    client = ComfyClient(base_url)

    async def save(files: list[dict[str, str]]) -> dict[str, object]:
        content, _media_type = await client.view_image(files[0])
        if finish is not None:
            content = await asyncio.to_thread(finish, content)
        _STILL_IMAGE_ROOT.mkdir(parents=True, exist_ok=True)
        destination = _STILL_IMAGE_ROOT / f"{still_image_id}.png"
        destination.write_bytes(content)
        _record_or_discard(
            destination,
            lambda: still_images.set_image(
                still_image_id, destination, source_prompt=source_prompt
            ),
            (StillImageNotFoundError,),
        )
        return {"still_image_id": still_image_id}

    await monitor_comfy_prompt(
        client=client,
        jobs=jobs,
        job_id=job_id,
        prompt_id=prompt_id,
        on_success=save,
        messages=MonitorMessages(
            running="静止画を合成しています",
            saving="静止画を保存しています",
            completed="静止画を生成しました",
            failed="ComfyUIで静止画を合成できませんでした",
            failed_code="COMPOSITION_FAILED",
            timeout="静止画の合成がタイムアウトしました",
            timeout_code="COMPOSITION_TIMEOUT",
            monitor_failed_code="COMPOSITION_MONITOR_FAILED",
        ),
        settings=MonitorSettings(
            poll_seconds=poll_seconds,
            max_polls=max_polls,
            extra_errors=(StillImageNotFoundError,),
        ),
        scratch=_comfy_scratch(base_url),
    )


def _fail_job(job_id: str, message: str, error_code: str) -> None:
    """順番待ちで実行した処理の失敗を、ジョブへ書いて終わらせる。

    実行はHTTP応答を返したあとなので、例外を投げても誰も受け取れない。
    利用者が見るのはジョブの状態と理由だけになるため、必ずここへ残す。
    """
    try:
        jobs.transition(
            job_id,
            JobTransition(state="failed", message=message[:1000], error_code=error_code),
        )
    except (InvalidTransitionError, JobNotFoundError):
        pass


def _project_illustration_style(project_id: str) -> str:
    """作品全体の画風。取れなければ空（画風指定は任意のため止めない）。"""
    try:
        return ProjectRepository(database).get(project_id).illustration_style
    except (ProjectNotFoundError, sqlite3.Error):
        return ""


@app.post("/api/v1/still-images/{still_image_id}/compose", response_model=CompositionResult)
async def compose_still_image(
    still_image_id: str,
    request: CompositionRequest,
    background_tasks: BackgroundTasks,
) -> CompositionResult:
    still_images = StillImageRepository(database)
    try:
        still_image = still_images.get(still_image_id)
    except StillImageNotFoundError as error:
        raise HTTPException(status_code=404, detail="静止画が見つかりません") from error
    if still_image.composition_method == CompositionMethod.PLACED:
        return await _compose_placed_still_image(still_image, request)
    if not set(request.character_specs) <= set(still_image.character_ids):
        raise HTTPException(status_code=404, detail="指定した人物はこの静止画に含まれていません")

    try:
        resolve_still_image_sources(database, still_image)
    except (CompositionError, CharacterNotFoundError, AssetNotFoundError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    base_url = _resolve_base_url(request.base_url)

    # 画面の指定は、合成を受け付けられると分かってから保存する。合成しなければ保存しない。
    chosen = StillImageCharacterRepository(database)
    for character_id, spec in request.character_specs.items():
        record = chosen.get(still_image_id, character_id)
        chosen.upsert(still_image_id, character_id, spec, record.figure_id if record else None)
    # 保存した指定から組み立て直す。合成に使う指定は、常に保存されている指定と同じになる。
    sources = resolve_still_image_sources(database, still_image)

    seed = request.seed if request.seed is not None else randbelow(9_007_199_254_740_967)
    payload = {**request.model_dump(mode="json"), "still_image_id": still_image_id, "seed": seed}

    async def run(job_id: str) -> None:
        """順番が来てから、ComfyUIへ送って終わるまで見届ける。"""
        client = ComfyClient(base_url)
        uploaded: list[str] = []
        try:
            template = load_workflow(COMPOSITION_WORKFLOW_PATH)
            references: list[StillImageReference] = []
            for index, source in enumerate(sources, start=1):
                comfy_name = await _upload_local_image(
                    client, source.image_path, f"{still_image_id}-{index}", uploaded
                )
                references.append(
                    StillImageReference(
                        kind=source.kind,
                        name=source.name,
                        comfy_image=comfy_name,
                        placement=source.placement,
                        pose=source.pose,
                        expression=source.expression,
                        direction=source.direction,
                        notes=source.notes,
                    )
                )

            # 作品の画風を渡し、写実指定の作品がイラスト調へ寄らないようにする。
            prompt = build_composition_prompt(
                references, request.prompt, _project_illustration_style(still_image.project_id)
            )
            workflow = build_composition_workflow(
                template,
                references=references,
                prompt=prompt,
                negative_prompt=request.negative_prompt,
                seed=seed,
                width=request.width,
                height=request.height,
                diffusion_model=request.diffusion_model,
                text_encoder=request.text_encoder,
                vae=request.vae,
                lightning_lora=request.lightning_lora,
                filename_prefix=f"{OUTPUT_ROOT_NAME}/still_images/{still_image_id}",
                careful=request.quality is CompositionQuality.CAREFUL,
            )
            prompt_id = await client.submit(workflow)
            jobs.transition(
                job_id,
                JobTransition(
                    state="preparing",
                    progress=0.1,
                    message="静止画の合成をキューへ登録しました",
                    external_id=prompt_id,
                    result={"still_image_id": still_image_id},
                ),
            )
            await monitor_still_image_composition(
                job_id, still_image_id, prompt_id, base_url, source_prompt=prompt
            )
        except (ComfyConnectionError, CompositionError, OSError, ValueError) as error:
            _fail_job(job_id, str(error), "COMPOSITION_SUBMIT_FAILED")
        finally:
            _comfy_scratch(base_url).discard_inputs(uploaded)

    job = await gpu_queue.submit(
        jobs,
        JobCreate(kind="composition.still_image", payload=payload),
        lambda job_id: partial(run, job_id),
    )
    still_image = still_images.set_composition_job(still_image_id, job.id)
    return CompositionResult(still_image=still_image, job_id=job.id)


async def _compose_placed_still_image(
    still_image: StillImageRead, request: CompositionRequest
) -> CompositionResult:
    """人物の姿を背景の上へ置いた下書きを作り、その1枚だけを渡してなじませる。

    位置は画面で置いた内容を、合成を受け付けられると分かってから保存する（指定と同じく、
    合成しなければ保存しない）。保存した位置から下書きを作るので、画面と合成が食い違わない。
    """
    still_image_id = still_image.id
    if not set(request.layouts) <= set(still_image.character_ids):
        raise HTTPException(status_code=404, detail="指定した人物はこの静止画に含まれていません")
    try:
        resolve_placed_sources(database, still_image)
    except (CompositionError, CharacterNotFoundError, AssetNotFoundError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    base_url = _resolve_base_url(request.base_url)

    chosen = StillImageCharacterRepository(database)
    for character_id, layout in request.layouts.items():
        chosen.set_layout(still_image_id, character_id, layout)
    sources = resolve_placed_sources(database, still_image)

    seed = request.seed if request.seed is not None else randbelow(9_007_199_254_740_967)
    payload = {**request.model_dump(mode="json"), "still_image_id": still_image_id, "seed": seed}

    async def run(job_id: str) -> None:
        """順番が来てから下書きを作り、ComfyUIへ送って終わるまで見届ける。"""
        client = ComfyClient(base_url)
        uploaded: list[str] = []
        try:
            # 下書きは順番が来てから作る。待っている間に姿を作り直していれば、新しい姿で置く。
            draft = await asyncio.to_thread(
                compose_draft, sources, request.width, request.height
            )
            mask = await asyncio.to_thread(blend_mask, sources, request.width, request.height)
            comfy_name = await _upload_image_bytes(
                client, draft, f"{still_image_id}-draft.png", "image/png", uploaded
            )
            mask_name = await _upload_image_bytes(
                client, mask, f"{still_image_id}-mask.png", "image/png", uploaded
            )
            blend_prompt = build_blend_prompt(
                len(sources.figures), _project_illustration_style(still_image.project_id)
            )
            composition_workflow = build_composition_workflow(
                load_workflow(COMPOSITION_WORKFLOW_PATH),
                references=[
                    StillImageReference(kind=KIND_BACKGROUND, name="", comfy_image=comfy_name)
                ],
                prompt=blend_prompt,
                negative_prompt=request.negative_prompt,
                seed=seed,
                width=request.width,
                height=request.height,
                diffusion_model=request.diffusion_model,
                text_encoder=request.text_encoder,
                vae=request.vae,
                lightning_lora=request.lightning_lora,
                filename_prefix=f"{OUTPUT_ROOT_NAME}/still_images/{still_image_id}",
                careful=request.quality is CompositionQuality.CAREFUL,
            )
            # 描き直すのは人物のまわりだけ。背景は下書き（＝元の背景）の画素のまま残す。
            workflow = build_blend_workflow(composition_workflow, mask_name)
            prompt_id = await client.submit(workflow)
            jobs.transition(
                job_id,
                JobTransition(
                    state="preparing",
                    progress=0.1,
                    message="静止画の合成をキューへ登録しました",
                    external_id=prompt_id,
                    result={"still_image_id": still_image_id},
                ),
            )
            await monitor_still_image_composition(
                job_id,
                still_image_id,
                prompt_id,
                base_url,
                source_prompt=blend_prompt,
                finish=lambda generated: finish_blend(generated, draft, mask),
            )
        except (ComfyConnectionError, CompositionError, OSError, ValueError) as error:
            _fail_job(job_id, str(error), "COMPOSITION_SUBMIT_FAILED")
        finally:
            _comfy_scratch(base_url).discard_inputs(uploaded)

    job = await gpu_queue.submit(
        jobs,
        JobCreate(kind="composition.still_image", payload=payload),
        lambda job_id: partial(run, job_id),
    )
    still_image = StillImageRepository(database).set_composition_job(still_image_id, job.id)
    return CompositionResult(still_image=still_image, job_id=job.id)


@app.get("/api/v1/still-images/{still_image_id}/image")
def get_still_image_image(still_image_id: str) -> Response:
    try:
        image_file = StillImageRepository(database).image_path(still_image_id)
    except StillImageNotFoundError as error:
        raise HTTPException(status_code=404, detail="静止画が見つかりません") from error
    if image_file is None:
        raise HTTPException(status_code=409, detail="静止画はまだ生成されていません")
    return _serve_image_response(_STILL_IMAGE_ROOT, image_file)


# --- 静止画ごとの人物の指定（どの立ち絵を使うか・姿勢・表情・向き・配置） ---


def _still_image_character_read(
    still_image: StillImageRead, character_id: str
) -> StillImageCharacterRead:
    """画面へ返す1人ぶん。実際に合成で使う立ち絵と同じ解決を通す。

    ここで別の決め方をすると、画面に出るサムネイルと合成に渡る1枚が食い違う。
    """
    still_image_id = still_image.id
    try:
        character_name = CharacterRepository(database).get(character_id).name
    except CharacterNotFoundError:
        character_name = ""
    record = StillImageCharacterRepository(database).get(still_image_id, character_id)
    figure = resolve_still_image_figure(database, still_image_id, character_id)
    spec = record.spec if record is not None else StillImageCharacterSpec()
    has_pose = record is not None and bool(record.pose_image_path)
    count = len(still_image.character_ids)
    index = (
        still_image.character_ids.index(character_id)
        if character_id in still_image.character_ids
        else 0
    )
    return StillImageCharacterRead(
        character_id=character_id,
        character_name=character_name,
        spec=spec,
        figure_id=figure.id if figure is not None else None,
        # 自分で選んだのか、既定のまま使っているのか。
        figure_is_default=not (record is not None and record.figure_id),
        figure_label=figure.label if figure is not None else "",
        figure_url=(
            f"/api/v1/character-figures/{figure.id}/image?v="
            f"{quote(figure_image_version(figure.image_path), safe='')}"
            if figure is not None
            else None
        ),
        has_figure=figure is not None,
        pose_url=(
            f"/api/v1/still-images/{still_image_id}/characters/{character_id}/pose-image"
            f"?v={record.pose_revision}"
            if has_pose
            else None
        ),
        pose_spec=record.pose_spec if has_pose else None,
        pose_prompt=record.pose_prompt if has_pose else None,
        pose_outdated=has_pose
        and pose_outdated(
            pose_figure_id=record.pose_figure_id,
            pose_spec=record.pose_spec,
            figure_id=figure.id if figure is not None else None,
            spec=spec,
        ),
        layout=(record.layout if record is not None else None) or default_layout(index, count),
    )


@app.get(
    "/api/v1/still-images/{still_image_id}/characters",
    response_model=list[StillImageCharacterRead],
)
def list_still_image_characters(still_image_id: str) -> list[StillImageCharacterRead]:
    try:
        still_image = StillImageRepository(database).get(still_image_id)
    except StillImageNotFoundError as error:
        raise HTTPException(status_code=404, detail="静止画が見つかりません") from error
    return [
        _still_image_character_read(still_image, character_id)
        for character_id in still_image.character_ids
    ]


@app.put(
    "/api/v1/still-images/{still_image_id}/characters/{character_id}",
    response_model=StillImageCharacterRead,
)
def update_still_image_character(
    still_image_id: str, character_id: str, request: StillImageCharacterUpdate
) -> StillImageCharacterRead:
    """この静止画で使う立ち絵と、姿勢・表情・向き・配置の指定を保存する。

    `figure_id` を空にすると既定の立ち絵に戻る（選び直しを取り消せる）。
    """
    try:
        still_image = StillImageRepository(database).get(still_image_id)
    except StillImageNotFoundError as error:
        raise HTTPException(status_code=404, detail="静止画が見つかりません") from error
    if character_id not in still_image.character_ids:
        raise HTTPException(status_code=404, detail="指定した人物はこの静止画に含まれていません")

    figure_id = request.figure_id or None
    if figure_id is not None:
        try:
            figure = CharacterFigureRepository(database).get(figure_id)
        except CharacterFigureNotFoundError as error:
            raise HTTPException(status_code=404, detail="立ち絵が見つかりません") from error
        if figure.character_id != character_id:
            raise HTTPException(status_code=409, detail="その立ち絵は別の人物のものです")
        if figure.state != "completed" or not figure.image_path:
            raise HTTPException(status_code=409, detail="まだ出来ていない立ち絵は使えません")

    chosen = StillImageCharacterRepository(database)
    spec = request.spec
    if spec is None:
        record = chosen.get(still_image_id, character_id)
        spec = record.spec if record is not None else StillImageCharacterSpec()
    chosen.upsert(still_image_id, character_id, spec, figure_id)
    return _still_image_character_read(still_image, character_id)


_STILL_IMAGE_POSE_ROOT = still_image_pose_dir(settings.data_dir)


@app.post(
    "/api/v1/still-images/{still_image_id}/characters/{character_id}/pose",
    response_model=StillImagePoseResult,
)
async def make_still_image_pose(
    still_image_id: str, character_id: str, request: StillImagePoseRequest
) -> StillImagePoseResult:
    """この静止画用の人物の姿を作る（置いて合成するときの1人ぶん）。

    指定があれば、立ち絵1枚の編集でポーズ・向き・表情を付けてから背景を抜く。空なら
    立ち絵をそのまま抜く。指定はここで保存する（姿と、姿を作った指定が食い違わないよう、
    作るときにしか保存しない）。配置は姿ではなく位置で決めるので、保存済みのまま触れない。

    丁寧な設定（高速化LoRA無し）で作る。立ち絵の変更と同じく、4ステップでは指定の反映も
    全身・素背景も崩れやすく、崩れた姿は置いても使えない。
    """
    try:
        still_image = StillImageRepository(database).get(still_image_id)
    except StillImageNotFoundError as error:
        raise HTTPException(status_code=404, detail="静止画が見つかりません") from error
    if character_id not in still_image.character_ids:
        raise HTTPException(status_code=404, detail="指定した人物はこの静止画に含まれていません")
    figure = resolve_still_image_figure(database, still_image_id, character_id)
    if figure is None:
        raise HTTPException(
            status_code=409,
            detail="この人物の立ち絵がありません。「登場人物」の画面で立ち絵を作ってください",
        )
    chosen = StillImageCharacterRepository(database)
    record = chosen.get(still_image_id, character_id)
    if record is not None and record.pose_job_id:
        try:
            working = jobs.get(record.pose_job_id).state in ACTIVE_JOB_STATES
        except JobNotFoundError:
            working = False
        if working:
            raise HTTPException(status_code=409, detail="この人物の姿はいま作っています")
    base_url = _resolve_base_url(request.base_url)

    current = record.spec if record is not None else StillImageCharacterSpec()
    spec = request.spec.posing().model_copy(update={"placement": current.placement})
    chosen.upsert(still_image_id, character_id, spec, record.figure_id if record else None)
    prompt = build_pose_prompt(spec, _project_illustration_style(still_image.project_id))
    seed = request.seed if request.seed is not None else randbelow(9_007_199_254_740_967)
    payload = {
        **request.model_dump(mode="json"),
        "still_image_id": still_image_id,
        "character_id": character_id,
        "figure_id": figure.id,
        "seed": seed,
    }
    destination = _STILL_IMAGE_POSE_ROOT / f"{still_image_id}-{character_id}.png"
    figure_path = Path(figure.image_path)
    scratch = _comfy_scratch(base_url)

    async def run(job_id: str) -> None:
        client = ComfyClient(base_url)
        uploaded: list[str] = []
        image_prefix = f"{job_id}-image"
        mask_prefix = f"{job_id}-mask"

        async def save(files: list[dict[str, str]]) -> dict[str, object]:
            mask = next((f for f in files if f["filename"].startswith(mask_prefix)), None)
            edited = next((f for f in files if f["filename"].startswith(image_prefix)), None)
            if mask is None or (prompt and edited is None):
                raise CompositionError("姿を作れませんでした。もう一度「姿を作る」を押してください")
            mask_bytes, _ = await client.view_image(mask)
            if edited is not None:
                image_bytes, _ = await client.view_image(edited)
            else:
                image_bytes = figure_path.read_bytes()
            pose = await asyncio.to_thread(cut_out, image_bytes, mask_bytes)
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.{job_id}.tmp")
            temporary.write_bytes(pose)
            os.replace(temporary, destination)

            def record_pose() -> None:
                if not chosen.set_pose(
                    still_image_id,
                    character_id,
                    image_path=destination,
                    figure_id=figure.id,
                    spec=spec,
                    prompt=prompt,
                ):
                    raise StillImageNotFoundError(still_image_id)

            _record_or_discard(destination, record_pose, (StillImageNotFoundError,))
            return {"still_image_id": still_image_id, "character_id": character_id}

        try:
            # 順番待ちの間に中止されたものは、ComfyUIへ送らずに終える。
            if jobs.is_cancelled(job_id):
                return
            comfy_figure = await _upload_local_image(
                client, figure_path, f"{job_id}-figure", uploaded
            )
            edit_workflow = None
            if prompt:
                edit_workflow = build_composition_workflow(
                    load_workflow(COMPOSITION_WORKFLOW_PATH),
                    references=[
                        StillImageReference(kind=KIND_CHARACTER, name="", comfy_image=comfy_figure)
                    ],
                    prompt=prompt,
                    negative_prompt="",
                    seed=seed,
                    width=POSE_WIDTH,
                    height=POSE_HEIGHT,
                    diffusion_model=DEFAULT_COMPOSITION_DIFFUSION_MODEL,
                    text_encoder=DEFAULT_COMPOSITION_TEXT_ENCODER,
                    vae=DEFAULT_COMPOSITION_VAE,
                    lightning_lora=DEFAULT_COMPOSITION_LIGHTNING_LORA,
                    filename_prefix=f"{OUTPUT_ROOT_NAME}/still_images/poses/{image_prefix}",
                    careful=True,
                )
            workflow = build_pose_workflow(
                load_workflow(BACKGROUND_REMOVAL_WORKFLOW_PATH),
                edit_workflow=edit_workflow,
                figure_image=comfy_figure,
                removal_prefix=f"{OUTPUT_ROOT_NAME}/still_images/poses/{mask_prefix}",
            )
            prompt_id = await client.submit(workflow)
            jobs.transition(
                job_id,
                JobTransition(
                    state="preparing",
                    progress=0.05,
                    message="人物の姿づくりをキューへ登録しました",
                    external_id=prompt_id,
                    result={"still_image_id": still_image_id, "character_id": character_id},
                ),
            )
            await monitor_comfy_prompt(
                client=client,
                jobs=jobs,
                job_id=job_id,
                prompt_id=prompt_id,
                on_success=save,
                messages=MonitorMessages(
                    running="人物の姿を作っています" if prompt else "人物を切り抜いています",
                    saving="人物の姿を保存しています",
                    completed="人物の姿ができました",
                    failed="ComfyUIで人物の姿を作れませんでした",
                    failed_code="POSE_FAILED",
                    timeout="人物の姿づくりがタイムアウトしました",
                    timeout_code="POSE_TIMEOUT",
                    monitor_failed_code="POSE_MONITOR_FAILED",
                ),
                settings=MonitorSettings(extra_errors=(StillImageNotFoundError, CompositionError)),
                scratch=scratch,
            )
        except (ComfyConnectionError, CompositionError, OSError, ValueError) as error:
            _fail_job(job_id, str(error), "POSE_SUBMIT_FAILED")
        finally:
            scratch.discard_inputs(uploaded)

    job = await gpu_queue.submit(
        jobs,
        JobCreate(kind="composition.still_image_pose", payload=payload),
        lambda job_id: partial(run, job_id),
    )
    chosen.set_pose_job(still_image_id, character_id, job.id)
    return StillImagePoseResult(
        job_id=job.id, character=_still_image_character_read(still_image, character_id)
    )


@app.get("/api/v1/still-images/{still_image_id}/characters/{character_id}/pose-image")
def get_still_image_pose_image(still_image_id: str, character_id: str) -> Response:
    record = StillImageCharacterRepository(database).get(still_image_id, character_id)
    if record is None or not record.pose_image_path:
        raise HTTPException(status_code=404, detail="人物の姿はまだありません")
    return _serve_image_response(_STILL_IMAGE_POSE_ROOT, Path(record.pose_image_path))


# --- カット（ショット）と I2V 動画生成 ---

_SHOT_VIDEO_ROOT = shot_video_dir(settings.data_dir)
# カットの最終フレーム（次カットの起点に使う連続性用）。
_SHOT_LAST_FRAME_ROOT = shot_last_frame_dir(settings.data_dir)
# 構図変更で作った起点フレーム・終端フレーム。
_SHOT_FRAME_ROOT = shot_frame_dir(settings.data_dir)
# 分割モードで作った、ビートごとの区間動画。
_SHOT_SEGMENT_ROOT = shot_segment_dir(settings.data_dir)


@app.post("/api/v1/still-images/{still_image_id}/shots", response_model=ShotRead, status_code=201)
def create_shot(still_image_id: str, request: ShotCreate) -> ShotRead:
    try:
        return ShotRepository(database).create(still_image_id, request)
    except StillImageNotFoundError as error:
        raise HTTPException(status_code=404, detail="静止画が見つかりません") from error
    except ShotSpeakerError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/v1/still-images/{still_image_id}/shots", response_model=list[ShotRead])
def list_shots(still_image_id: str) -> list[ShotRead]:
    try:
        return ShotRepository(database).list(still_image_id)
    except StillImageNotFoundError as error:
        raise HTTPException(status_code=404, detail="静止画が見つかりません") from error


@app.get("/api/v1/shots/{shot_id}", response_model=ShotRead)
def get_shot(shot_id: str) -> ShotRead:
    try:
        return ShotRepository(database).get(shot_id)
    except ShotNotFoundError as error:
        raise HTTPException(status_code=404, detail="カットが見つかりません") from error


@app.patch("/api/v1/shots/{shot_id}", response_model=ShotRead)
def update_shot(shot_id: str, request: ShotUpdate) -> ShotRead:
    try:
        return ShotRepository(database).update(shot_id, request)
    except ShotNotFoundError as error:
        raise HTTPException(status_code=404, detail="カットが見つかりません") from error
    except ShotPromptLockedError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ShotSpeakerError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/v1/shots/{shot_id}/deletion-preview", response_model=ShotDeletionPreview)
def preview_shot_deletion(shot_id: str) -> ShotDeletionPreview:
    """このカットを消すと動画も消えるかどうかを返す（削除確認の材料）。"""
    try:
        return ShotDeletionPreview(**vars(summarize_shot_deletion(database, shot_id)))
    except KeyError as error:
        raise HTTPException(status_code=404, detail="カットが見つかりません") from error


@app.delete("/api/v1/shots/{shot_id}", status_code=204)
def delete_shot(shot_id: str) -> Response:
    """カットと、生成済みの動画・起点フレーム・最終フレームを削除する。"""
    try:
        delete_shot_records(database, settings.data_dir, shot_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="カットが見つかりません") from error
    return Response(status_code=204)


class ShotVideoError(RuntimeError):
    """カットの動画生成が続けられない状態（利用者へそのまま見せる文言を持つ）。"""


def _split_shot_outputs(
    files: list[dict[str, str]],
) -> tuple[dict[str, str] | None, dict[str, str] | None]:
    """ComfyUIの出力を「動画本体」と「最終フレームPNG」に振り分ける。

    ワークフローには保存ノードが2つある（動画＝webp/mp4、最終フレーム＝png）。
    履歴の出力は1つのリストへ平らに並ぶため、拡張子で見分ける。
    """
    video: dict[str, str] | None = None
    last_frame: dict[str, str] | None = None
    for entry in files:
        name = entry.get("filename", "").lower()
        if name.endswith(".png"):
            last_frame = last_frame or entry
        elif name.endswith((".webp", ".mp4", ".gif", ".webm")):
            video = video or entry
    if video is None:
        video = next((entry for entry in files if entry is not last_frame), None)
    return video, last_frame


async def _fetch_last_frame(
    client: ComfyClient,
    last_frame_file: dict[str, str] | None,
    video_path: Path,
    video_bytes: bytes,
) -> bytes | None:
    """次の起点に使う最終フレームPNGを得る。

    ワークフロー側で保存した1枚を最優先で使う。ここが本命で、mp4でもffmpegが要らず、
    再エンコードも挟まない。取れなかったときだけ、保存済み動画からの抽出へ落ちる
    （webpはPillow、mp4はffmpeg。ffmpegが無い環境ではここで諦める）。
    """
    if last_frame_file is not None:
        try:
            content, _media_type = await client.view_image(last_frame_file)
            return content
        except ComfyConnectionError:
            pass
    return extract_last_frame(video_path, video_bytes)


async def _await_prompt_files(
    client: ComfyClient,
    job_id: str,
    prompt_id: str,
    *,
    message: str,
    progress: float,
    progress_end: float,
    poll_seconds: float = 1.0,
    max_polls: int = 7200,
) -> list[dict[str, str]] | None:
    """1件のプロンプトの完了を待ち、出力ファイル記述子を返す。

    `monitor_comfy_prompt` はジョブを終端（completed/failed）まで進めてしまうため、
    複数のプロンプトを1ジョブで順に流す分割モードでは使えない。ここは待つだけにして、
    ジョブの終端は呼び出し側が決める。利用者が中止したときは None を返す。
    進み具合は `progress`（始まり）から `progress_end`（終わり）までの間で進める。
    """
    writer = JobProgressWriter(jobs, job_id)
    writer.write(progress, message)
    for _ in range(max_polls):
        if jobs.is_cancelled(job_id):
            return None
        current = client.prompt_progress(prompt_id)
        if current is not None:
            writer.write(
                progress + (progress_end - progress) * current.fraction,
                current.describe(message),
            )
        entry = await client.history(prompt_id)
        if entry is not None:
            status = entry.get("status", {})
            if isinstance(status, dict) and status.get("completed"):
                files = output_files(entry)
                if status.get("status_str") == "success" and files:
                    return files
                raise ShotVideoError("ComfyUIでカットの動画を生成できませんでした")
        await asyncio.sleep(poll_seconds)
    raise ShotVideoError("動画生成の待機がタイムアウトしました")


async def monitor_shot_video(
    job_id: str,
    shot_id: str,
    prompt_id: str,
    base_url: str,
    *,
    video_format: str = "webp",
    poll_seconds: float = 1.0,
    max_polls: int = 7200,
) -> None:
    """I2Vの履歴を監視し、完成した動画をローカルへ保存する（一括モード）。

    入れ物はエンジンで変わる（Wan 2.2=webp / MiniMax H3=音声付きmp4）ため、
    保存する拡張子は呼び出し側から受け取る。
    """
    shots = ShotRepository(database)
    client = ComfyClient(base_url)

    async def save(files: list[dict[str, str]]) -> dict[str, object]:
        video_file, last_frame_file = _split_shot_outputs(files)
        if video_file is None:
            raise ShotVideoError("生成した動画を取得できませんでした")
        content, _media_type = await client.view_image(video_file)
        _SHOT_VIDEO_ROOT.mkdir(parents=True, exist_ok=True)
        # 同じカットをエンジンを変えて作り直したとき、前の入れ物を残さない。
        for stale in _SHOT_VIDEO_ROOT.glob(f"{shot_id}.*"):
            stale.unlink(missing_ok=True)
        destination = _SHOT_VIDEO_ROOT / f"{shot_id}.{video_format}"
        destination.write_bytes(content)
        _record_or_discard(
            destination,
            lambda: shots.set_output_video(shot_id, destination),
            (ShotNotFoundError,),
        )
        # 1本で作り直したので、前に分割で作った区間はもう使わない。
        for stale in ShotBeatRepository(database).clear_segments(shot_id):
            remove_within(settings.data_dir, stale)
        # 最終フレームを次カットの起点に使えるようにする（ベストエフォート）。
        last_frame = await _fetch_last_frame(client, last_frame_file, destination, content)
        if last_frame is not None:
            _SHOT_LAST_FRAME_ROOT.mkdir(parents=True, exist_ok=True)
            last_path = _SHOT_LAST_FRAME_ROOT / f"{shot_id}.png"
            last_path.write_bytes(last_frame)
            _record_or_discard(
                last_path,
                lambda: shots.set_last_frame(shot_id, last_path),
                (ShotNotFoundError,),
            )
        else:
            # 前の動画の最終フレームを残すと、作り直した動画と続かない起点になる。
            previous = shots.clear_last_frame(shot_id)
            if previous is not None:
                remove_within(settings.data_dir, previous)
        return {"shot_id": shot_id, "continuity_available": last_frame is not None}

    await monitor_comfy_prompt(
        client=client,
        jobs=jobs,
        job_id=job_id,
        prompt_id=prompt_id,
        on_success=save,
        messages=MonitorMessages(
            running="カットの動画を生成しています",
            saving="生成した動画を保存しています",
            completed="カットの動画を生成しました",
            failed="ComfyUIでカットの動画を生成できませんでした",
            failed_code="SHOT_VIDEO_FAILED",
            timeout="動画生成の待機がタイムアウトしました",
            timeout_code="SHOT_VIDEO_TIMEOUT",
            monitor_failed_code="SHOT_VIDEO_MONITOR_FAILED",
        ),
        settings=MonitorSettings(
            poll_seconds=poll_seconds,
            max_polls=max_polls,
            extra_errors=(ShotNotFoundError, ShotVideoError),
        ),
        scratch=_comfy_scratch(base_url),
    )


def _shot_video_values(
    request: ShotGenerateRequest,
    *,
    seed: int,
    prompt: str,
    length: int,
    start_image: str,
    end_image: str,
    prefix: str,
) -> dict[str, object]:
    """選んだエンジンのワークフローへ渡す値を組み立てる。

    2つのテンプレートは入力の名前も構成も違う（Wanは高低2エキスパート＋否定条件、
    H3は単一モデル＋音声VAEで否定条件なし）ため、束縛表もここで分ける。
    終端キーフレーム（`end_image`）を持てるのはH3だけで、Wanには渡さない。
    """
    common = {
        "prompt": prompt,
        "width": request.width,
        "height": request.height,
        "length": length,
        "fps": request.fps,
        "start_image": start_image,
        "filename_prefix": f"{OUTPUT_ROOT_NAME}/shots/{prefix}",
        "last_frame_prefix": f"{OUTPUT_ROOT_NAME}/shot-last-frames/{prefix}",
    }
    if request.engine == "minimax_h3":
        values: dict[str, object] = {
            **common,
            "seed": seed,
            "steps": request.h3_steps,
            "diffusion_model": request.h3_diffusion_model,
            "turbo_lora": request.h3_turbo_lora,
            "turbo_lora_strength": request.h3_turbo_lora_strength,
            "text_encoder": request.h3_text_encoder,
            "video_vae": request.h3_video_vae,
            "audio_vae": request.h3_audio_vae,
        }
        if end_image:
            values["end_image"] = end_image
        return values
    return {
        **common,
        "negative_prompt": request.negative_prompt or _DEFAULT_VIDEO_NEGATIVE,
        "seed_high": seed,
        "seed_low": seed,
        "diffusion_model_high": request.diffusion_model_high,
        "diffusion_model_low": request.diffusion_model_low,
        "lightning_lora_high": request.lightning_lora_high,
        "lightning_lora_low": request.lightning_lora_low,
        "text_encoder": request.text_encoder,
        "vae": request.vae,
    }


def _shot_video_workflow(
    request: ShotGenerateRequest,
    *,
    seed: int,
    prompt: str,
    length: int,
    start_image: str,
    end_image: str,
    prefix: str,
) -> dict[str, object]:
    """テンプレートを読み、終端キーフレームの有無に合わせて枝刈りしてから束縛する。"""
    template = load_workflow(SHOT_VIDEO_WORKFLOW_PATHS[request.engine])
    if request.engine == "minimax_h3" and not end_image:
        template = prune_end_image(template)
    return bind_workflow(
        template,
        _shot_video_values(
            request,
            seed=seed,
            prompt=prompt,
            length=length,
            start_image=start_image,
            end_image=end_image,
            prefix=prefix,
        ),
    )


def _resolve_shot_start_frame(shot: ShotRead, *, continue_from_previous: bool) -> Path | None:
    """カットの起点になる画像。判定・表示と同じ解決を使う（食い違わせない）。"""
    from .readiness import resolve_shot_start_frame

    return resolve_shot_start_frame(
        database, shot, continue_from_previous=continue_from_previous
    )[1]


def _resolve_shot_beats(shot: ShotRead) -> list[Beat]:
    """カットのビートを、プロンプト組み立て用の形へ移す。無ければ空。

    音を作らない作り方（Wan）向けなので、セリフは渡さない。
    """
    return [
        Beat(duration_seconds=beat.duration_seconds, motion_prompt=beat.motion_prompt)
        for beat in shot.beats
    ]


async def _run_split_shot_video(
    job_id: str,
    shot: ShotRead,
    request: ShotGenerateRequest,
    *,
    base_url: str,
    seed: int,
    prompts: list[str],
    start_frame: Path,
    end_frame: Path | None,
    uploaded: list[str],
) -> None:
    """分割モード: ビートごとに区間を生成し、最終フレームで繋ぐ。

    MiniMax H3 の土台は中間キーフレームを受け付けない（先頭と末尾のみ）ため、
    時間配分を確実に守らせるにはここで区間を分けるしかない。区間の連結には ffmpeg が
    要るので、無い環境では区間動画のまま残し、書き出しのときに繋ぐ。

    送る文は呼び出し側で用意済みのものを受け取る（区間の数だけ、順番どおり）。
    ここで組み立て直すと、画面で確かめた文と実際に送る文が食い違う。
    """
    shots = ShotRepository(database)
    beats_repo = ShotBeatRepository(database)
    client = ComfyClient(base_url)
    scratch = _comfy_scratch(base_url)
    video_format = ENGINE_VIDEO_FORMATS[request.engine]
    _SHOT_SEGMENT_ROOT.mkdir(parents=True, exist_ok=True)
    _SHOT_LAST_FRAME_ROOT.mkdir(parents=True, exist_ok=True)

    current_start = start_frame
    segments: list[Path] = []
    last_frame_path: Path | None = None
    total = len(shot.beats)

    for index, beat in enumerate(shot.beats):
        # 最後の区間だけ、指定があれば終端キーフレームで着地を固定する。
        beat_end = end_frame if index == total - 1 else None
        length = frames_for_seconds(request.engine, beat.duration_seconds)
        prompt = prompts[index]
        start_name = await _upload_local_image(
            client, current_start, f"{beat.id}-start", uploaded
        )
        end_name = (
            await _upload_local_image(client, beat_end, f"{beat.id}-end", uploaded)
            if beat_end is not None and request.engine == "minimax_h3"
            else ""
        )
        workflow = _shot_video_workflow(
            request,
            # 区間ごとに種を変え、同じ動きの繰り返しにならないようにする。
            seed=seed + index,
            prompt=prompt,
            length=length,
            start_image=start_name,
            end_image=end_name,
            prefix=beat.id,
        )
        prompt_id = await client.submit(workflow)
        files = await _await_prompt_files(
            client,
            job_id,
            prompt_id,
            message=f"ビート {index + 1}/{total} を生成しています",
            progress=0.15 + 0.75 * index / max(1, total),
            progress_end=0.15 + 0.75 * (index + 1) / max(1, total),
        )
        if files is None:
            return
        try:
            video_file, last_frame_file = _split_shot_outputs(files)
            if video_file is None:
                raise ShotVideoError(f"ビート {index + 1} の動画を取得できませんでした")
            content, _media_type = await client.view_image(video_file)
            for stale in _SHOT_SEGMENT_ROOT.glob(f"{beat.id}.*"):
                stale.unlink(missing_ok=True)
            segment = _SHOT_SEGMENT_ROOT / f"{beat.id}.{video_format}"
            segment.write_bytes(content)
            segments.append(segment)
            frame_bytes = await _fetch_last_frame(client, last_frame_file, segment, content)
        finally:
            scratch.discard_outputs(files)
        beat_last: Path | None = None
        if frame_bytes is not None:
            beat_last = _SHOT_LAST_FRAME_ROOT / f"{beat.id}.png"
            beat_last.write_bytes(frame_bytes)
            current_start = beat_last
            last_frame_path = beat_last
        elif index + 1 < total:
            raise ShotVideoError(
                f"ビート {index + 1} の最終フレームを取り出せませんでした。"
                "次のビートを繋げられないため中止します"
            )
        try:
            beats_repo.set_segment(beat.id, segment, beat_last)
        except ShotBeatNotFoundError:
            # 生成中にビートが消された。行き先の無い区間を残さない。
            for orphan in (segment, beat_last):
                if orphan is not None:
                    remove_within(settings.data_dir, orphan)
            raise

    jobs.transition(
        job_id,
        JobTransition(state="running", progress=0.9, message="区間をつないでいます"),
    )
    # カット自身の最終フレームは、最後のビートのものを指す。1本で作っていたときの
    # 最終フレームは使わなくなるので片付ける。
    previous_last = shots.last_frame_path(shot.id)
    if last_frame_path is not None:
        shots.set_last_frame(shot.id, last_frame_path)
    else:
        shots.clear_last_frame(shot.id)
    if previous_last is not None and previous_last != last_frame_path:
        remove_within(settings.data_dir, previous_last)

    # ffmpeg があれば1本にまとめる。無ければ区間のまま残し、書き出しのときに繋ぐ。
    joined = False
    ffmpeg_path = find_ffmpeg()
    if ffmpeg_path is not None and segments:
        _SHOT_VIDEO_ROOT.mkdir(parents=True, exist_ok=True)
        for stale in _SHOT_VIDEO_ROOT.glob(f"{shot.id}.*"):
            stale.unlink(missing_ok=True)
        destination = _SHOT_VIDEO_ROOT / f"{shot.id}.mp4"
        concat_videos(ffmpeg_path, segments, destination, float(request.fps or 24.0))
        if destination.is_file():
            shots.set_output_video(shot.id, destination)
            joined = True
    if not joined:
        # 区間のまま残すときは、前に1本で作った動画を外す。残すと、書き出しや保存で
        # 作り直す前の動画が使われる。
        previous_video = shots.clear_output_video(shot.id)
        if previous_video is not None:
            remove_within(settings.data_dir, previous_video)
    jobs.transition(
        job_id,
        JobTransition(
            state="validating",
            progress=0.95,
            message="区間動画を保存しています",
            result={"shot_id": shot.id, "beat_count": total, "joined": joined},
        ),
    )
    jobs.transition(
        job_id,
        JobTransition(
            state="completed",
            message=(
                f"{total}件のビートを生成しました"
                if joined
                else f"{total}件のビートを生成しました（ffmpegが無いため区間のまま保存しました）"
            ),
        ),
    )


async def _await_prompt_history(
    client: ComfyClient,
    prompt_id: str,
    *,
    poll_seconds: float = 0.5,
    max_polls: int = 1200,
) -> dict[str, object] | None:
    """1件のプロンプトの完了を待って履歴を返す（ジョブの状態には触れない）。"""
    for _ in range(max_polls):
        entry = await client.history(prompt_id)
        if entry is not None and entry.get("status", {}).get("completed"):
            return entry
        await asyncio.sleep(poll_seconds)
    return None


async def _translate_texts(
    client: ComfyClient, texts: list[str], kind: TranslationKind = "direction"
) -> dict[str, str]:
    """日本語の指示文を英語へ直し、覚えておく。

    セリフは渡さない（読み上げてほしい言葉そのものなので、日本語のまま使う）。
    訳せなかった分は表に載せず、呼び出し側で元の文のまま進める。翻訳できないことで
    生成そのものを止めない。`kind="person"` は、セリフの話す人を名指しする句にする。
    """
    cache = TranslationCache(database)
    options = TranslationSettings()
    table: dict[str, str] = {}
    template: dict[str, object] | None = None
    for text in texts:
        source = text.strip()
        if not source or not contains_japanese(source) or source in table:
            continue
        cached = cache.get(source, kind)
        if cached:
            table[source] = cached
            continue
        if template is None:
            try:
                template = load_workflow(translation_workflow_path(_RESOURCES_ROOT))
            except (OSError, ValueError):
                return table
        try:
            english = await translate_once(
                client,
                template,
                source,
                options,
                wait_for=partial(_await_prompt_history, client),
                kind=kind,
            )
        except (TranslationUnavailableError, ComfyConnectionError, OSError, ValueError):
            # 翻訳用のモデルが無い環境もある。日本語のまま進め、画面の注意で知らせる。
            continue
        cache.put(source, english, options.clip_name, datetime.now(UTC).isoformat(), kind)
        table[source] = english
    return table


def _translated(text: str, table: dict[str, str]) -> str:
    """訳があればそれを、無ければ元の文を返す。"""
    return table.get(text.strip(), text)


def _translatable_texts(shot: ShotRead, ambience: str, motion: str, music: str = "") -> list[str]:
    """英語へ直す対象。セリフは含めない（読み上げてほしい言葉そのものなので訳さない）。"""
    return [motion, ambience, music, *(beat.motion_prompt for beat in shot.beats)]


def _speaker_texts(shot: ShotRead) -> list[str]:
    """セリフの話す人の記述（配置・年齢・髪）。名指しの句へ直す対象。"""
    lines = [*shot.speech_lines, *(line for beat in shot.beats for line in beat.speech_lines)]
    return [line.speaker_description for line in lines if not line.narration]


def _spoken(lines: list[ShotSpeechLineRead], speakers: dict[str, str]) -> tuple[SpokenLine, ...]:
    """セリフの行を、名指しを英語にした形へ移す。

    名指しが英語にできなかったときは、日本語の記述を文へ入れない（項目名ごと読み上げられる）。
    話す人が分からない行と同じ扱いにして、生成に使う文で気づけるようにする。
    """
    spoken: list[SpokenLine] = []
    for line in lines:
        description = line.speaker_description.strip()
        speaker = speakers.get(description, "" if contains_japanese(description) else description)
        spoken.append(
            SpokenLine(
                text=line.text,
                speaker="" if line.narration or not line.speaker_present else speaker,
                narration=line.narration,
            )
        )
    return tuple(spoken)


def _translated_beats(
    shot: ShotRead, table: dict[str, str], speakers: dict[str, str]
) -> list[Beat]:
    return [
        Beat(
            duration_seconds=beat.duration_seconds,
            motion_prompt=_translated(beat.motion_prompt, table),
            speech_lines=_spoken(beat.speech_lines, speakers),
        )
        for beat in shot.beats
    ]


def _has_japanese(text: str) -> bool:
    """かな・漢字が混ざっているか（読み上げの取り違えを見つけるため）。"""
    return any(
        "\u3040" <= ch <= "\u30ff" or "\u4e00" <= ch <= "\u9fff" for ch in text
    )


@app.get("/api/v1/shots/{shot_id}/start-frame")
def get_shot_start_frame(shot_id: str) -> Response:
    """このカットの起点として実際に使う1枚を返す。

    「前カットの続きから始まらない」を目で確かめられるようにするための入口。
    出どころと実体を同じ解決から受け取るので、置き場所を取り違えて404になることはない。
    """
    from .readiness import resolve_shot_start_frame

    shots = ShotRepository(database)
    try:
        shot = shots.get(shot_id)
    except ShotNotFoundError as error:
        raise HTTPException(status_code=404, detail="カットが見つかりません") from error

    source, image_path = resolve_shot_start_frame(database, shot)
    if image_path is None:
        raise HTTPException(status_code=409, detail="起点となる画像がまだありません")
    roots = {
        "custom": _SHOT_FRAME_ROOT,
        "previous": _SHOT_LAST_FRAME_ROOT,
        "still_image": _STILL_IMAGE_ROOT,
    }
    try:
        content, media_type = read_served_image(roots[source], image_path)
    except ImageNotAvailableError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    # ここが返すのは「いまの解決結果」で、中身は前のカットを作り直せば変わる。
    # 変わらない絵として覚えさせると、画面のラベルと出てくる絵が食い違う（実機で発生）。
    return Response(
        content=content,
        media_type=media_type,
        headers={"Cache-Control": "no-cache", "X-Start-Frame-Source": source},
    )


def _shot_split_mode(shot: ShotRead) -> bool:
    """ビートごとに分けて作るか（区間が1つしか無ければ分ける意味がない）。"""
    return shot.timeline_mode == "split" and len(shot.beats) > 1


def _shot_total_seconds(shot: ShotRead) -> float:
    return sum(beat.duration_seconds for beat in shot.beats) or shot.duration_seconds


def _build_shot_segments(
    shot: ShotRead,
    engine: VideoEngine,
    translations: dict[str, str],
    speakers: dict[str, str] | None = None,
) -> list[ShotPromptSegment]:
    """指示から「生成に使う文」を組み立てる。区間ごとに分けるならその数だけ返す。

    音を作る作り方では、セリフ以外の指示を英語で渡す（日本語のままだとセリフと
    区別されず、説明文まで読み上げられる）。訳の表は呼び出し側が用意する。
    `speakers` は、セリフの話す人の記述から英語の名指しへの表。
    """
    speakers = speakers or {}
    beats = (
        _translated_beats(shot, translations, speakers)
        if engine == "minimax_h3"
        else _resolve_shot_beats(shot)
    )
    ambience = (
        _translated(shot.ambience_prompt, translations)
        if engine == "minimax_h3"
        else shot.ambience_prompt
    )
    music = (
        _translated(shot.music_prompt, translations)
        if engine == "minimax_h3"
        else shot.music_prompt
    )
    motion = (
        _translated(shot.motion_prompt, translations)
        if engine == "minimax_h3"
        else shot.motion_prompt
    )
    total = _shot_total_seconds(shot)

    if _shot_split_mode(shot):
        return [
            ShotPromptSegment(
                label=f"ビート #{index}",
                prompt=build_shot_prompt(
                    engine=engine,
                    beats=[beat],
                    ambience_prompt=ambience,
                    music_prompt=music,
                ),
                length_frames=frames_for_seconds(engine, beat.duration_seconds),
                seconds=beat.duration_seconds,
            )
            for index, beat in enumerate(beats, start=1)
        ]
    return [
        ShotPromptSegment(
            label="カット全体",
            prompt=build_shot_prompt(
                engine=engine,
                beats=beats,
                motion_prompt=motion,
                speech_lines=_spoken(shot.speech_lines, speakers) if engine == "minimax_h3" else (),
                ambience_prompt=ambience,
                music_prompt=music,
                duration_seconds=seconds_for_frames(engine, frames_for_seconds(engine, total)),
            ),
            length_frames=frames_for_seconds(engine, total),
            seconds=total,
        )
    ]


async def _prepare_shot_segments(
    client: ComfyClient,
    shot: ShotRead,
    engine: VideoEngine,
    *,
    on_translating: Callable[[], None] | None = None,
) -> list[ShotPromptSegment]:
    """必要な下ごしらえまで済ませて、生成に使う文を作る。

    ここだけがComfyUIを使う（訳の生成）。画面から読むだけのときは呼ばない。
    """
    translations: dict[str, str] = {}
    speakers: dict[str, str] = {}
    if engine == "minimax_h3":
        sources = _translatable_texts(
            shot, shot.ambience_prompt, shot.motion_prompt, shot.music_prompt
        )
        people = _speaker_texts(shot)
        if any(contains_japanese(text) for text in [*sources, *people]):
            if on_translating is not None:
                on_translating()
            translations = await _translate_texts(client, sources)
            speakers = await _translate_texts(client, people, "person")
    return _build_shot_segments(shot, engine, translations, speakers)


def _stored_shot_segments(
    shot: ShotRead, engine: VideoEngine
) -> tuple[list[ShotPromptSegment], bool]:
    """いま使われる文と、それが古くなっていないか。

    直接書き直しているならその文、そうでなければ指示から作った文。作ったときと
    作り方・指示が違えば古い（画面には出さず、作り直しを促す）。
    """
    shots = ShotRepository(database)
    resolved, resolved_engine, signature = shots.resolved_prompt(shot.id)
    if shot.prompt_edit_mode:
        edited = shots.edited_prompt(shot.id)
        # 書き直している間は指示を変えられないので、古くなるのは作り方を変えたときだけ。
        return edited, bool(edited) and resolved_engine != engine
    stale = resolved_engine != engine or signature != prompt_signature(shot)
    return resolved, bool(resolved) and stale


def _shot_prompt_state(shot: ShotRead, engine: VideoEngine) -> ShotPromptState:
    """画面へ返す、文の状態。用意できていない文と古い文は中身を出さない。"""
    segments, stale = _stored_shot_segments(shot, engine)
    ready = bool(segments) and not stale
    return ShotPromptState(
        engine=engine,
        timeline_mode="split" if _shot_split_mode(shot) else "single",
        segments=segments if ready else [],
        total_seconds=_shot_total_seconds(shot),
        ready=ready,
        stale=bool(segments) and stale,
        edit_mode=shot.prompt_edit_mode,
    )


def _get_shot(shot_id: str) -> ShotRead:
    try:
        return ShotRepository(database).get(shot_id)
    except ShotNotFoundError as error:
        raise HTTPException(status_code=404, detail="カットが見つかりません") from error


@app.get("/api/v1/shots/{shot_id}/prompt", response_model=ShotPromptState)
def get_shot_prompt(shot_id: str, engine: VideoEngine = "wan22") -> ShotPromptState:
    """このカットの生成に使う文（用意できているとき）を返す。

    ここでは文を作らない。作りかけの文を見せると、画面に出ている文と実際に送られる
    文が食い違う。作るのは「文を用意する」か「動画を生成する」を押したときだけ。
    """
    return _shot_prompt_state(_get_shot(shot_id), engine)


@app.post("/api/v1/shots/{shot_id}/prompt", response_model=ShotPromptResult)
async def prepare_shot_prompt(shot_id: str, request: ShotPromptRequest) -> ShotPromptResult:
    """生成に使う文だけを先に用意する（動画は作らない）。

    動画を1本作るには時間がかかる。送る文を先に確かめられるようにして、作ってから
    直すのではなく、作る前に直せるようにする。
    """
    shots = ShotRepository(database)
    shot = _get_shot(shot_id)
    if shot.prompt_edit_mode:
        raise HTTPException(
            status_code=409,
            detail="直接書き直している間は作り直せません。書き直しをやめてからにしてください",
        )
    base_url = _resolve_base_url(request.base_url)
    engine = request.engine

    async def run(job_id: str) -> None:
        client = ComfyClient(base_url)
        try:
            jobs.transition(
                job_id,
                JobTransition(
                    state="preparing",
                    progress=0.1,
                    message="生成に使う文を用意しています",
                    result={"shot_id": shot_id},
                ),
            )
            current = shots.get(shot_id)
            jobs.transition(
                job_id,
                JobTransition(state="running", progress=0.3, message="生成に使う文を用意しています"),
            )
            segments = await _prepare_shot_segments(client, current, engine)
            shots.set_resolved_prompt(shot_id, segments, engine, prompt_signature(current))
            jobs.transition(
                job_id,
                JobTransition(
                    state="completed",
                    progress=1.0,
                    message="生成に使う文を用意しました",
                    result={"shot_id": shot_id},
                ),
            )
        except (ComfyConnectionError, OSError, ValueError) as error:
            _fail_job(job_id, str(error), "SHOT_PROMPT_FAILED")

    job = await gpu_queue.submit(
        jobs,
        JobCreate(kind="generation.prompt.shot", payload={"shot_id": shot_id, "engine": engine}),
        lambda job_id: partial(run, job_id),
    )
    return ShotPromptResult(shot=shots.get(shot_id), job_id=job.id)


@app.patch("/api/v1/shots/{shot_id}/prompt", response_model=ShotPromptState)
def edit_shot_prompt(
    shot_id: str, request: ShotPromptEdit, engine: VideoEngine = "wan22"
) -> ShotPromptState:
    """直接書き直した文を保存する（書き直しモードのときだけ）。"""
    shots = ShotRepository(database)
    shot = _get_shot(shot_id)
    if not shot.prompt_edit_mode:
        raise HTTPException(
            status_code=409, detail="直接書き直すには、書き直しモードを入にしてください"
        )
    current = shots.edited_prompt(shot_id)
    if len(request.segments) != len(current):
        raise HTTPException(status_code=409, detail="区間の数が合いません")
    for segment, text in zip(current, request.segments):
        segment.prompt = text
        segment.edited = True
    shots.set_edited_prompt(shot_id, current)
    return _shot_prompt_state(shots.get(shot_id), engine)


@app.post("/api/v1/shots/{shot_id}/prompt/edit-mode", response_model=ShotPromptState)
def set_shot_prompt_edit_mode(
    shot_id: str, request: ShotPromptModeUpdate, engine: VideoEngine = "wan22"
) -> ShotPromptState:
    """文を直接書き直すモードの入切。

    入の間は、文のもとになる指示を変えられない（変えても文へ反映されないため）。
    切ると書き直した文は捨て、指示から作った文へ戻る。
    """
    shots = ShotRepository(database)
    shot = _get_shot(shot_id)
    if request.edit_mode:
        _segments, stale = _stored_shot_segments(shot, engine)
        if stale:
            raise HTTPException(
                status_code=409,
                detail="指示が変わっています。先に、生成に使う文を作り直してください",
            )
    try:
        updated = shots.set_prompt_edit_mode(shot_id, request.edit_mode)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _shot_prompt_state(updated, engine)


@app.post("/api/v1/shots/{shot_id}/generate-video", response_model=ShotGenerateResult)
async def generate_shot_video(
    shot_id: str,
    request: ShotGenerateRequest,
    background_tasks: BackgroundTasks,
) -> ShotGenerateResult:
    shots = ShotRepository(database)
    shot = _get_shot(shot_id)

    # 指示が要求に添えられていれば、まずカットへ書き戻す（生成に使う文はカットから作る）。
    # 直接書き直している間は、その文が使われるので書き戻さない。
    if not shot.prompt_edit_mode:
        overrides = {
            name: value
            for name, value in (
                ("motion_prompt", request.motion_prompt),
                ("speech_lines", request.speech_lines),
                ("ambience_prompt", request.ambience_prompt),
                ("music_prompt", request.music_prompt),
                ("timeline_mode", request.timeline_mode),
            )
            if value is not None
        }
        if overrides:
            try:
                shot = shots.update(shot_id, ShotUpdate(**overrides))
            except ShotSpeakerError as error:
                raise HTTPException(status_code=400, detail=str(error)) from error

    start_frame = _resolve_shot_start_frame(
        shot, continue_from_previous=request.continue_from_previous
    )
    if start_frame is None:
        raise HTTPException(
            status_code=409,
            detail="起点となる画像がありません。先に静止画を合成してください",
        )
    # 終端キーフレームを扱えるのはMiniMax H3だけ（Wanのテンプレートに入力が無い）。
    end_frame = shots.custom_end_frame_path(shot_id) if request.engine == "minimax_h3" else None
    if end_frame is not None and not end_frame.is_file():
        end_frame = None

    split = _shot_split_mode(shot)

    # 尺は、明示指定があればそれ、無ければビートの合計（無ければカットの尺）から決める。
    if request.length_explicit and request.length is not None:
        length = request.length
    else:
        length = frames_for_seconds(request.engine, _shot_total_seconds(shot))

    base_url = _resolve_base_url(request.base_url)
    seed = request.seed if request.seed is not None else randbelow(9_007_199_254_740_967)
    payload = {
        **request.model_dump(mode="json"),
        "shot_id": shot_id,
        "seed": seed,
        "timeline_mode": "split" if split else "single",
        "length": length,
    }

    async def run(job_id: str) -> None:
        client = ComfyClient(base_url)
        uploaded: list[str] = []
        try:
            # 順番待ちを抜けたことを先に記録する（この後の遷移はすべて running）。
            jobs.transition(
                job_id,
                JobTransition(
                    state="preparing",
                    progress=0.02,
                    message="生成の準備をしています",
                    result={"shot_id": shot_id},
                ),
            )
            # 用意できた文があればそれを送る。無ければ（あるいは指示が変わっていれば）
            # ここで用意してから進む——押す回数を増やさないため。
            current = shots.get(shot_id)
            segments, stale = _stored_shot_segments(current, request.engine)
            if current.prompt_edit_mode and segments:
                # 直接書いた文は、そのまま送る。
                pass
            elif not segments or stale or (split and len(segments) != len(current.beats)):
                def announce() -> None:
                    jobs.transition(
                        job_id,
                        JobTransition(
                            state="running",
                            progress=0.03,
                            message="生成に使う文を用意しています",
                        ),
                    )

                segments = await _prepare_shot_segments(
                    client, current, request.engine, on_translating=announce
                )
                shots.set_resolved_prompt(
                    shot_id, segments, request.engine, prompt_signature(current)
                )
            prompts = [segment.prompt for segment in segments]

            if split:
                jobs.transition(
                    job_id,
                    JobTransition(
                        state="running",
                        progress=0.05,
                        message=f"{len(current.beats)}件のビートを順に生成します",
                        result={"shot_id": shot_id},
                    ),
                )
                await _run_split_shot_video(
                    job_id,
                    current,
                    request,
                    base_url=base_url,
                    seed=seed,
                    prompts=prompts,
                    start_frame=start_frame,
                    end_frame=end_frame,
                    uploaded=uploaded,
                )
                return

            start_name = await _upload_local_image(
                client, start_frame, f"{shot_id}-start", uploaded
            )
            end_name = (
                await _upload_local_image(client, end_frame, f"{shot_id}-end", uploaded)
                if end_frame is not None
                else ""
            )
            workflow = _shot_video_workflow(
                request,
                seed=seed,
                prompt=prompts[0],
                length=length,
                start_image=start_name,
                end_image=end_name,
                prefix=shot_id,
            )
            prompt_id = await client.submit(workflow)
            jobs.transition(
                job_id,
                JobTransition(
                    state="running",
                    progress=0.1,
                    message="カットの動画生成をキューへ登録しました",
                    external_id=prompt_id,
                    result={"shot_id": shot_id},
                ),
            )
            await monitor_shot_video(
                job_id,
                shot_id,
                prompt_id,
                base_url,
                video_format=ENGINE_VIDEO_FORMATS[request.engine],
            )
        except (ComfyConnectionError, ShotVideoError, OSError, ValueError) as error:
            _fail_job(job_id, str(error), "SHOT_VIDEO_SUBMIT_FAILED")
        finally:
            _comfy_scratch(base_url).discard_inputs(uploaded)

    job = await gpu_queue.submit(
        jobs,
        JobCreate(kind="generation.video.shot", payload=payload),
        lambda job_id: partial(run, job_id),
    )
    shots.set_start_frame(shot_id, start_frame)
    shot = shots.set_video_job(shot_id, job.id)
    return ShotGenerateResult(shot=shot, job_id=job.id)

# --- ビート（カット内の時間区間） ---


@app.post("/api/v1/shots/{shot_id}/beats", response_model=ShotBeatRead, status_code=201)
def create_shot_beat(shot_id: str, request: ShotBeatCreate) -> ShotBeatRead:
    try:
        return ShotBeatRepository(database).create(shot_id, request)
    except ShotNotFoundError as error:
        raise HTTPException(status_code=404, detail="カットが見つかりません") from error
    except ShotPromptLockedError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ShotSpeakerError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/v1/shots/{shot_id}/beats", response_model=list[ShotBeatRead])
def list_shot_beats(shot_id: str) -> list[ShotBeatRead]:
    try:
        return ShotBeatRepository(database).list(shot_id)
    except ShotNotFoundError as error:
        raise HTTPException(status_code=404, detail="カットが見つかりません") from error


@app.patch("/api/v1/shot-beats/{beat_id}", response_model=ShotBeatRead)
def update_shot_beat(beat_id: str, request: ShotBeatUpdate) -> ShotBeatRead:
    try:
        return ShotBeatRepository(database).update(beat_id, request)
    except ShotBeatNotFoundError as error:
        raise HTTPException(status_code=404, detail="ビートが見つかりません") from error
    except ShotPromptLockedError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ShotSpeakerError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.delete("/api/v1/shot-beats/{beat_id}", status_code=204)
def delete_shot_beat(beat_id: str) -> Response:
    """ビートと、分割モードで作った区間動画・最終フレームを削除する。"""
    try:
        files = ShotBeatRepository(database).delete(beat_id)
    except ShotBeatNotFoundError as error:
        raise HTTPException(status_code=404, detail="ビートが見つかりません") from error
    except ShotPromptLockedError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    for path in files:
        # 保存先の外を消さない（削除は必ずアプリのデータ領域の中だけで行う）。
        if path.is_file() and path.is_relative_to(settings.data_dir):
            path.unlink(missing_ok=True)
    return Response(status_code=204)


@app.get("/api/v1/shot-beats/{beat_id}/video")
def get_shot_beat_video(beat_id: str) -> Response:
    """分割モードで作った、このビートの区間動画。"""
    try:
        video = ShotBeatRepository(database).segment_video_path(beat_id)
    except ShotBeatNotFoundError as error:
        raise HTTPException(status_code=404, detail="ビートが見つかりません") from error
    if video is None:
        raise HTTPException(status_code=409, detail="このビートの動画はまだ生成されていません")
    try:
        content, media_type = read_served_video(_SHOT_SEGMENT_ROOT, video)
    except ImageNotAvailableError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return Response(
        content=content,
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


# --- 構図を変えた起点・終端フレーム ---


def _shot_frame_base(shot: ShotRead) -> Path | None:
    """構図変更のもとにする1枚。前カットの最終フレーム、無ければ元の静止画。"""
    shots = ShotRepository(database)
    previous = shots.previous_shot(shot.still_image_id, shot.ordinal)
    if previous is not None:
        last_frame = shots.last_frame_path(previous.id)
        if last_frame is not None and last_frame.is_file():
            return last_frame
    return StillImageRepository(database).image_path(shot.still_image_id)


async def monitor_shot_frame(
    job_id: str,
    shot_id: str,
    target: ShotFrameTarget,
    prompt_id: str,
    base_url: str,
    *,
    poll_seconds: float = 0.5,
    max_polls: int = 3600,
) -> None:
    """構図変更の履歴を監視し、出来た1枚を起点／終端フレームとして保存する。"""
    shots = ShotRepository(database)
    client = ComfyClient(base_url)

    async def save(files: list[dict[str, str]]) -> dict[str, object]:
        content, _media_type = await client.view_image(files[0])
        _SHOT_FRAME_ROOT.mkdir(parents=True, exist_ok=True)
        destination = _SHOT_FRAME_ROOT / f"{shot_id}-{target.value}.png"
        destination.write_bytes(content)
        _record_or_discard(
            destination,
            lambda: (
                shots.set_custom_start_frame(shot_id, destination)
                if target is ShotFrameTarget.START
                else shots.set_custom_end_frame(shot_id, destination)
            ),
            (ShotNotFoundError,),
        )
        return {"shot_id": shot_id, "target": target.value}

    label = "起点" if target is ShotFrameTarget.START else "終端"
    await monitor_comfy_prompt(
        client=client,
        jobs=jobs,
        job_id=job_id,
        prompt_id=prompt_id,
        on_success=save,
        messages=MonitorMessages(
            running=f"{label}の構図を作り直しています",
            saving=f"{label}のフレームを保存しています",
            completed=f"{label}のフレームを作りました",
            failed=f"ComfyUIで{label}のフレームを作れませんでした",
            failed_code="SHOT_FRAME_FAILED",
            timeout="構図の作り直しがタイムアウトしました",
            timeout_code="SHOT_FRAME_TIMEOUT",
            monitor_failed_code="SHOT_FRAME_MONITOR_FAILED",
        ),
        settings=MonitorSettings(
            poll_seconds=poll_seconds,
            max_polls=max_polls,
            extra_errors=(ShotNotFoundError,),
        ),
        scratch=_comfy_scratch(base_url),
    )


@app.post("/api/v1/shots/{shot_id}/frame", response_model=ShotFrameResult)
async def compose_shot_frame(
    shot_id: str,
    request: ShotFrameRequest,
    background_tasks: BackgroundTasks,
) -> ShotFrameResult:
    """前カットの最終フレームを編集して、このカットの起点（または終端）の絵を作る。

    2カット目以降を「同じ絵の続き」から抜け出させるための入口。人物や場所はそのままで
    見え方だけを変えるので、静止画の合成と同じ Qwen-Image-Edit を1枚だけ渡して使う。
    動画生成より先に結果を確認できるよう、生成とは別のジョブにしている。
    """
    shots = ShotRepository(database)
    try:
        shot = shots.get(shot_id)
    except ShotNotFoundError as error:
        raise HTTPException(status_code=404, detail="カットが見つかりません") from error

    instruction = (
        request.composition_prompt
        if request.composition_prompt is not None
        else shot.composition_prompt
    )
    if not instruction.strip():
        raise HTTPException(
            status_code=409,
            detail="構図の変更内容を入力してください（例: バストアップにする、背後から見た構図にする）",
        )

    base_image = _shot_frame_base(shot)
    if base_image is None:
        raise HTTPException(
            status_code=409,
            detail="もとにする画像がありません。先に静止画を合成するか、前のカットを生成してください",
        )

    still_image = StillImageRepository(database).get(shot.still_image_id)
    base_url = _resolve_base_url(request.base_url)
    seed = request.seed if request.seed is not None else randbelow(9_007_199_254_740_967)
    payload = {
        **request.model_dump(mode="json"),
        "shot_id": shot_id,
        "seed": seed,
        "composition_prompt": instruction,
    }

    async def run(job_id: str) -> None:
        client = ComfyClient(base_url)
        uploaded: list[str] = []
        try:
            comfy_name = await _upload_local_image(
                client, base_image, f"{shot_id}-frame-base", uploaded
            )
            prompt = build_recompose_prompt(
                instruction, _project_illustration_style(still_image.project_id)
            )
            workflow = build_composition_workflow(
                load_workflow(COMPOSITION_WORKFLOW_PATH),
                references=[
                    # 1枚だけを渡し直す。文面は build_recompose_prompt が持つ。
                    StillImageReference(kind="frame", name="", comfy_image=comfy_name),
                ],
                prompt=prompt,
                negative_prompt="",
                seed=seed,
                width=request.width,
                height=request.height,
                diffusion_model=request.diffusion_model,
                text_encoder=request.text_encoder,
                vae=request.vae,
                lightning_lora=request.lightning_lora,
                filename_prefix=f"{OUTPUT_ROOT_NAME}/shot_frames/{shot_id}-{request.target.value}",
                careful=request.quality is CompositionQuality.CAREFUL,
            )
            prompt_id = await client.submit(workflow)
            jobs.transition(
                job_id,
                JobTransition(
                    state="preparing",
                    progress=0.1,
                    message="構図の作り直しをキューへ登録しました",
                    external_id=prompt_id,
                    result={"shot_id": shot_id},
                ),
            )
            await monitor_shot_frame(job_id, shot_id, request.target, prompt_id, base_url)
        except (ComfyConnectionError, CompositionError, OSError, ValueError) as error:
            _fail_job(job_id, str(error), "SHOT_FRAME_SUBMIT_FAILED")
        finally:
            _comfy_scratch(base_url).discard_inputs(uploaded)

    job = await gpu_queue.submit(
        jobs,
        JobCreate(kind="composition.shot_frame", payload=payload),
        lambda job_id: partial(run, job_id),
    )
    return ShotFrameResult(shot=shots.get(shot_id), job_id=job.id)


@app.get("/api/v1/shots/{shot_id}/frame/{target}")
def get_shot_frame(shot_id: str, target: ShotFrameTarget) -> Response:
    """構図変更で作った起点／終端フレームの画像。"""
    shots = ShotRepository(database)
    try:
        image_file = (
            shots.custom_start_frame_path(shot_id)
            if target is ShotFrameTarget.START
            else shots.custom_end_frame_path(shot_id)
        )
    except ShotNotFoundError as error:
        raise HTTPException(status_code=404, detail="カットが見つかりません") from error
    if image_file is None:
        raise HTTPException(status_code=409, detail="このフレームはまだ作られていません")
    return _serve_image_response(_SHOT_FRAME_ROOT, image_file)


@app.delete("/api/v1/shots/{shot_id}/frame/{target}", response_model=ShotRead)
def clear_shot_frame(shot_id: str, target: ShotFrameTarget) -> ShotRead:
    """作った起点／終端フレームを外し、既定の起点（連続性・静止画）へ戻す。"""
    shots = ShotRepository(database)
    try:
        image_file = (
            shots.custom_start_frame_path(shot_id)
            if target is ShotFrameTarget.START
            else shots.custom_end_frame_path(shot_id)
        )
        shot = (
            shots.set_custom_start_frame(shot_id, None)
            if target is ShotFrameTarget.START
            else shots.set_custom_end_frame(shot_id, None)
        )
    except ShotNotFoundError as error:
        raise HTTPException(status_code=404, detail="カットが見つかりません") from error
    if image_file is not None and image_file.is_file() and image_file.is_relative_to(settings.data_dir):
        image_file.unlink(missing_ok=True)
    return shot


@app.get("/api/v1/shots/{shot_id}/video")
def get_shot_video(shot_id: str) -> Response:
    try:
        video = ShotRepository(database).video_path(shot_id)
    except ShotNotFoundError as error:
        raise HTTPException(status_code=404, detail="カットが見つかりません") from error
    if video is None:
        raise HTTPException(status_code=409, detail="カットの動画はまだ生成されていません")
    try:
        content, media_type = read_served_video(_SHOT_VIDEO_ROOT, video)
    except ImageNotAvailableError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return Response(
        content=content,
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


# --- 書き出し（カット動画をつないで1本のmp4に） ---

def _ffmpeg_spec() -> DownloadSpec | None:
    """用意するものの情報。マニフェストが読めない配布では None（案内だけにする）。"""
    try:
        return load_ffmpeg_spec(FFMPEG_MANIFEST_PATH)
    except (FfmpegProvisionError, OSError, ValueError):
        return None


@app.get("/api/v1/export/status", response_model=ExportStatus)
def export_status() -> ExportStatus:
    ffmpeg = find_ffmpeg()
    if ffmpeg is not None:
        return ExportStatus(ffmpeg_available=True, ffmpeg_path=ffmpeg, hint="")
    # 使えないときは、無効にするだけでなく行き先を示す。用意そのものは初期設定の
    # 「必要環境の確認」で行う——足りないものを1か所にまとめて出したいため。
    configured = settings.ffmpeg_path
    hint = (
        f"指定された場所に ffmpeg が見つかりません: {configured}"
        if configured
        else (
            "全カットを1本につなぐには ffmpeg（動画をつなぐ処理エンジン）が要ります。"
            "「設定を確認・変更」→「設定を変更する」→「ComfyUI環境を用意」の「ノードとモデルの確認」から用意できます。"
        )
    )
    return ExportStatus(ffmpeg_available=False, ffmpeg_path=None, hint=hint)


async def run_ffmpeg_provision(job_id: str) -> None:
    """ffmpeg（カットをつなぐ処理エンジン）を取得し、共有フォルダへ用意する。"""
    try:
        spec = _ffmpeg_spec()
        if spec is None:
            raise FfmpegProvisionError("用意するものの情報が見つかりません")
        tools_root = ffmpeg_tools_root()
        jobs.transition(
            job_id,
            JobTransition(state="preparing", progress=0.02, message="書き出しの準備を始めます"),
        )
        await asyncio.to_thread(ensure_free_space, tools_root, spec.size_bytes)
        jobs.transition(
            job_id,
            JobTransition(state="running", progress=0.05, message="必要なものを取得しています"),
        )

        # 数百MBを黙って待たせないよう、5%進むごとに知らせる（毎回書くほどの粒度は要らない）。
        last_step = 0

        def report(received: int, total: int) -> None:
            nonlocal last_step
            if total <= 0:
                return
            step = int(20 * received / total)
            if step == last_step:
                return
            last_step = step
            try:
                jobs.transition(
                    job_id,
                    JobTransition(
                        state="running",
                        progress=0.05 + 0.85 * received / total,
                        message=(
                            "必要なものを取得しています"
                            f"（{received // (1024 * 1024)} / {total // (1024 * 1024)} MB）"
                        ),
                    ),
                )
            except (InvalidTransitionError, JobNotFoundError):
                # 進みの知らせは補助なので、書けなくても取得は続ける。
                return

        archive = await asyncio.to_thread(
            download_with_resume,
            tools_root,
            spec,
            progress=report,
            should_cancel=lambda: jobs.is_cancelled(job_id),
        )
        jobs.transition(
            job_id,
            JobTransition(state="running", progress=0.92, message="取り出しています"),
        )
        await asyncio.to_thread(extract_ffmpeg, archive, tools_root)
        # 展開が済めば書庫は要らない（数百MBを置いたままにしない）。
        await asyncio.to_thread(archive.unlink, True)
        jobs.transition(
            job_id,
            JobTransition(state="validating", progress=0.98, message="使えるか確認しています"),
        )
        if find_ffmpeg() is None:
            raise FfmpegProvisionError("用意したものを確認できませんでした")
        jobs.transition(
            job_id,
            JobTransition(state="completed", message="書き出しの準備ができました"),
        )
    except DownloadCancelled:
        try:
            jobs.transition(job_id, JobTransition(state="cancelled", message="中止しました"))
        except (InvalidTransitionError, JobNotFoundError):
            return
    except (DownloadError, FfmpegProvisionError, OSError) as error:
        try:
            jobs.transition(
                job_id,
                JobTransition(
                    state="failed", message=str(error), error_code="FFMPEG_PROVISION_FAILED"
                ),
            )
        except (InvalidTransitionError, JobNotFoundError):
            return


@app.post("/api/v1/setup/tools/{tool_id}/provision", response_model=None)
async def start_tool_provision(tool_id: str, background_tasks: BackgroundTasks):
    """必要環境の確認で足りなかった道具を用意するジョブを開始する。二重起動は防ぐ。

    GPUを使わないので順番待ちには入れない（生成の裏で用意できる）。
    """
    if tool_id != "ffmpeg":
        raise HTTPException(status_code=404, detail="用意できる対象ではありません")
    if jobs.find_active(["setup.ffmpeg.provision"]) is not None:
        raise HTTPException(status_code=409, detail="この準備がすでに実行中です")
    if find_ffmpeg() is not None:
        raise HTTPException(status_code=409, detail="すでに揃っています")
    if settings.ffmpeg_path:
        raise HTTPException(
            status_code=409, detail="使う場所が指定されています。その指定を確かめてください"
        )
    if _ffmpeg_spec() is None:
        raise HTTPException(status_code=409, detail="用意するものの情報が見つかりません")
    job = jobs.create(JobCreate(kind="setup.ffmpeg.provision", payload={"tool_id": tool_id}))
    background_tasks.add_task(run_ffmpeg_provision, job.id)
    return job


@app.post("/api/v1/projects/{project_id}/export", response_model=ExportResult)
def export_project(project_id: str, request: ExportRequest) -> ExportResult:
    """プロジェクトのカット動画を、物語順に1本のmp4へ結合する。"""
    try:
        ProjectRepository(database).get(project_id)
    except ProjectNotFoundError as error:
        raise HTTPException(status_code=404, detail="作品が見つかりません") from error

    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "ffmpeg（動画をつなぐ処理エンジン）が見つかりません。"
                "「設定を確認・変更」→「設定を変更する」→「ComfyUI環境を用意」の「ノードとモデルの確認」から用意できます"
            ),
        )
    videos = list_project_shot_videos(database, project_id)
    if not videos:
        raise HTTPException(status_code=409, detail="書き出せるカット動画がありません")

    fps = request.fps
    if fps is None:
        rates = cut_frame_rates(videos, ffmpeg)
        if not rates:
            raise HTTPException(
                status_code=409,
                detail="カットのfpsを読み取れませんでした。fpsを選んでから書き出してください",
            )
        # 作り方を混ぜたときは、いちばん高い値に揃える（低いカットのコマを間引かないため）。
        fps = rates[-1]

    output = project_export_path(settings.data_dir, project_id)
    try:
        audio_included = concat_videos(ffmpeg, videos, output, fps)
    except ExportError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return ExportResult(
        project_id=project_id,
        shot_count=len(videos),
        download_url=f"/api/v1/projects/{project_id}/export/download",
        audio_included=audio_included,
        fps=fps,
    )


@app.get("/api/v1/projects/{project_id}/export", response_model=ProjectExportStatus)
def get_project_export(project_id: str) -> ProjectExportStatus:
    """この作品の書き出し済みファイルの有無を返す。

    書き出したmp4はデータ領域に残るので、画面を開き直しても取り出せる。
    「さっき押したかどうか」ではなく実体で判断する。
    """
    try:
        ProjectRepository(database).get(project_id)
    except ProjectNotFoundError as error:
        raise HTTPException(status_code=404, detail="作品が見つかりません") from error
    cut_fps = cut_frame_rates(list_project_shot_videos(database, project_id), find_ffmpeg())
    exported = project_export_path(settings.data_dir, project_id)
    if not exported.is_file():
        return ProjectExportStatus(cut_fps=cut_fps)
    stat = exported.stat()
    return ProjectExportStatus(
        exists=True,
        created_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
        size_bytes=stat.st_size,
        cut_fps=cut_fps,
    )


@app.get("/api/v1/projects/{project_id}/export/download")
def download_project_export(project_id: str) -> Response:
    try:
        ProjectRepository(database).get(project_id)
    except ProjectNotFoundError as error:
        raise HTTPException(status_code=404, detail="作品が見つかりません") from error
    try:
        resolved = project_export_path(settings.data_dir, project_id).resolve(strict=True)
        expected_root = export_root(settings.data_dir).resolve(strict=True)
    except (FileNotFoundError, OSError) as error:
        raise HTTPException(status_code=409, detail="まだ書き出されていません") from error
    if resolved.parent != expected_root or resolved.suffix.lower() != ".mp4":
        raise HTTPException(status_code=404, detail="書き出しファイルが見つかりません")
    return Response(
        content=resolved.read_bytes(),
        media_type="video/mp4",
        headers={
            "Cache-Control": "private, max-age=0, must-revalidate",
            "Content-Disposition": f'attachment; filename="{project_id}.mp4"',
        },
    )


def _copy_to_chosen_path(source: Path, chosen: str) -> SaveAsResult:
    """利用者が選んだ場所へ複製する。

    アプリの保存先の外へ書く唯一の経路なので、受け取った場所をそのまま信じない。
    「その場でOSのダイアログを開いて選んだ場所」であることが前提で、実在する
    フォルダの中の、絶対パスであることをここでも確かめる。上書きは、ダイアログが
    すでに確認しているので妨げない。
    """
    if not source.is_file():
        raise HTTPException(status_code=409, detail="保存できるファイルがありません")
    destination = Path(chosen.strip()).expanduser()
    if not destination.is_absolute() or not destination.name:
        raise HTTPException(status_code=422, detail="保存先の指定が正しくありません")
    if not destination.parent.is_dir():
        raise HTTPException(status_code=422, detail="保存先のフォルダーが見つかりません")
    try:
        shutil.copyfile(source, destination)
    except OSError as error:
        raise HTTPException(status_code=500, detail=f"保存できませんでした: {error}") from error
    return SaveAsResult(path=str(destination), size_bytes=destination.stat().st_size)


@app.post("/api/v1/projects/{project_id}/export/save-as", response_model=SaveAsResult)
def save_project_export_as(project_id: str, request: SaveAsRequest) -> SaveAsResult:
    """書き出した動画を、利用者が選んだ場所へ複製する。"""
    try:
        ProjectRepository(database).get(project_id)
    except ProjectNotFoundError as error:
        raise HTTPException(status_code=404, detail="作品が見つかりません") from error
    return _copy_to_chosen_path(project_export_path(settings.data_dir, project_id), request.path)


@app.post("/api/v1/still-images/{still_image_id}/image/save-as", response_model=SaveAsResult)
def save_still_image_as(still_image_id: str, request: SaveAsRequest) -> SaveAsResult:
    """合成した静止画（挿絵）を、利用者が選んだ場所へ複製する。"""
    try:
        image_file = StillImageRepository(database).image_path(still_image_id)
    except StillImageNotFoundError as error:
        raise HTTPException(status_code=404, detail="静止画が見つかりません") from error
    if image_file is None:
        raise HTTPException(status_code=409, detail="この静止画はまだ合成されていません")
    return _copy_to_chosen_path(image_file, request.path)


@app.post("/api/v1/shots/{shot_id}/video/save-as", response_model=SaveAsResult)
def save_shot_video_as(shot_id: str, request: SaveAsRequest) -> SaveAsResult:
    """カット1本の動画を、利用者が選んだ場所へ複製する。"""
    try:
        video = ShotRepository(database).video_path(shot_id)
    except ShotNotFoundError as error:
        raise HTTPException(status_code=404, detail="カットが見つかりません") from error
    if video is None:
        raise HTTPException(status_code=409, detail="このカットの動画はまだ生成されていません")
    return _copy_to_chosen_path(video, request.path)


# --- ジョブ参照 ---


@app.get("/api/v1/jobs/{job_id}")
def get_job(job_id: str):
    try:
        return jobs.get(job_id)
    except JobNotFoundError as error:
        raise HTTPException(status_code=404, detail="ジョブが見つかりません") from error


@app.get("/api/v1/jobs")
def list_jobs(limit: int = Query(default=100, ge=1, le=500)):
    return jobs.list(limit)


@app.post("/api/v1/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    """実行中の処理を中止する。ComfyUIの生成と、このアプリが起動した子プロセスも止める。"""
    try:
        base_url: str | None = _resolve_base_url(None)
    except HTTPException:
        # 接続先が未設定でも、記録上の中止と子プロセスの停止は行う。
        base_url = None
    try:
        return await cancel_running_job(
            jobs, job_id, comfy_base_url=base_url, processes=owned_processes
        )
    except JobNotFoundError as error:
        raise HTTPException(status_code=404, detail="ジョブが見つかりません") from error
    except InvalidTransitionError as error:
        raise HTTPException(status_code=409, detail="この処理はすでに終了しています") from error


# ============================ 人物候補 ============================
#
# 実装は chotto_sashie_shared にある。
# 候補生成 → 基準選択 → 参照画像 → 採否判定 → 学習データ → 学習 → 採否レビュー。

# ComfyUIの出力フォルダに作るアプリ用の名前空間。これから作るものの置き場所を
# 決めるだけで、すでにある記録の読み出しには関わらない。画像はアプリ側に記録ID単位で
# 控えてあり、控えが無いときの取り出しにも記録自身が持っている道筋を使うため、
# ここを改めても過去の画像は見失わない。
OUTPUT_ROOT_NAME = "ChottoSashie"
def _character_or_404(character_id: str) -> SharedCharacterRead:
    try:
        return ProfileRepository(database).get_character(character_id)
    except SharedCharacterNotFoundError as error:
        raise HTTPException(status_code=404, detail="登場人物が見つかりません") from error


def _project_style(character: SharedCharacterRead) -> str:
    try:
        return ProfileRepository(database).get_project(character.project_id).illustration_style
    except SharedProjectNotFoundError:
        return ""


async def _serve_generated_image(
    kind: str, record_id: str, base_url: str, image: CandidateImageInfo | None
) -> Response:
    """アプリ管理領域の控えを優先し、無ければComfyUIから取得して控えを作る。"""
    local = read_local_image(settings.data_dir, kind, record_id)
    if local is not None:
        return Response(content=local, media_type="image/png", headers={"Cache-Control": "no-store"})
    if image is None:
        raise HTTPException(status_code=409, detail="画像はまだ生成中です")
    try:
        content, media_type = await ComfyClient(base_url).view_image(image.model_dump())
    except ComfyConnectionError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    await asyncio.to_thread(write_local_image, settings.data_dir, kind, record_id, content)
    return Response(content=content, media_type=media_type, headers={"Cache-Control": "no-store"})


@app.get("/api/v1/characters/{character_id}/profile", response_model=SharedCharacterRead)
def get_character_profile(character_id: str) -> SharedCharacterRead:
    """人物候補の生成に使うプロフィールを返す。"""
    return _character_or_404(character_id)


@app.patch("/api/v1/characters/{character_id}/profile", response_model=SharedCharacterRead)
def update_character_profile(
    character_id: str, request: SharedCharacterUpdate
) -> SharedCharacterRead:
    _character_or_404(character_id)
    try:
        return ProfileRepository(database).update_character(character_id, request)
    except SharedCharacterNotFoundError as error:
        raise HTTPException(status_code=404, detail="登場人物が見つかりません") from error


@app.post(
    "/api/v1/characters/{character_id}/candidates/generate",
    response_model=CandidateBatchResult,
)
async def generate_character_candidates(
    character_id: str,
    request: VideoCandidateGenerateRequest,
    background_tasks: BackgroundTasks,
) -> CandidateBatchResult:
    """人物候補をまとめて生成する（顔立ちの未固定部分を比べて基準を選ぶ）。"""
    character = _character_or_404(character_id)
    request = request.model_copy(
        update={
            # 未指定ならセットアップ設定の接続先へ寄せる（型は共有スキーマに合わせる）。
            "base_url": HttpUrl(
                _resolve_base_url(str(request.base_url) if request.base_url else None)
            )
        }
    )
    # 上書きのときは、順番待ちに入れる前に前の回を消す（残す指定なら触らない）。
    # 指定が無ければ設定に従う。基準画像は残す——新しい回から選び直すまでは、
    # それが立ち絵の起点なので。
    keep_history = (
        request.keep_history
        if request.keep_history is not None
        else PreferenceRepository(database).get().candidate_history == "keep"
    )
    if not keep_history:
        try:
            delete_candidate_batches(
                database, settings.data_dir, character_id, keep_base=True
            )
        except BatchInUseError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
    project_style = _project_style(character)
    seed_start = (
        request.seed_start if request.seed_start is not None else randbelow(9_007_199_254_740_979)
    )
    payload = request.model_dump(mode="json")
    payload.update(
        {
            "seed_start": seed_start,
            "character": character.model_dump(mode="json"),
            "illustration_style": project_style,
        }
    )
    repository = CandidateRepository(database)

    async def run(job_id: str) -> None:
        try:
            candidate_ids, first_prompt_id = await submit_candidate_batch(
                database=database,
                job_id=job_id,
                character=character,
                project_style=project_style,
                request=request,
                seed_start=seed_start,
                template=load_workflow(candidate_workflow_path(_RESOURCES_ROOT)),
                output_root=OUTPUT_ROOT_NAME,
            )
            jobs.transition(
                job_id,
                JobTransition(
                    state="preparing",
                    progress=0.05,
                    message=f"人物候補{request.count}枚を生成キューへ登録しました",
                    external_id=first_prompt_id,
                    result={"candidate_ids": candidate_ids},
                ),
            )
            await monitor_candidate_batch(
                job_id,
                candidate_ids,
                str(request.base_url),
                database=database,
                jobs=jobs,
                data_dir=settings.data_dir,
                scratch=_comfy_scratch(str(request.base_url)),
            )
        except (ComfyConnectionError, OSError, ValueError) as error:
            repository.fail_batch(job_id, str(error))
            _fail_job(job_id, str(error), "CANDIDATE_SUBMIT_FAILED")

    job = await gpu_queue.submit(
        jobs,
        JobCreate(kind="character.candidates", payload=payload),
        lambda job_id: partial(run, job_id),
    )
    # 候補の実体は順番が来てから作られる。画面はジョブの進み具合を見て取り直す。
    return CandidateBatchResult(job=job, candidates=[])


@app.get("/api/v1/characters/{character_id}/candidates", response_model=list[CandidateRead])
def list_character_candidates(
    character_id: str,
    latest_only: bool = Query(default=True),
    batch_job_id: str | None = Query(default=None),
) -> list[CandidateRead]:
    _character_or_404(character_id)
    return CandidateRepository(database).list_for_character(
        character_id, latest_only=latest_only, batch_job_id=batch_job_id
    )


@app.get(
    "/api/v1/characters/{character_id}/candidate-batches",
    response_model=list[BatchSummaryRead],
)
def list_character_candidate_batches(character_id: str) -> list[BatchSummaryRead]:
    """人物候補を作った回の一覧。作り直しても前の回へ戻れるようにする。"""
    _character_or_404(character_id)
    return [
        BatchSummaryRead(**vars(item)) for item in list_candidate_batches(database, character_id)
    ]


@app.delete(
    "/api/v1/characters/{character_id}/candidate-batches/{batch_job_id}",
    response_model=BatchDeletionResult,
)
def delete_character_candidate_batch(character_id: str, batch_job_id: str) -> BatchDeletionResult:
    """人物候補の回を、画像の控えごと完全に削除する。"""
    _character_or_404(character_id)
    try:
        deleted = delete_candidate_batches(
            database, settings.data_dir, character_id, batch_job_ids=[batch_job_id]
        )
    except BatchInUseError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if deleted == 0:
        raise HTTPException(status_code=404, detail="その回の人物候補が見つかりません")
    return BatchDeletionResult(batch_job_id=batch_job_id, deleted_count=deleted)


@app.delete(
    "/api/v1/characters/{character_id}/candidates/unused",
    response_model=CandidateCleanupResult,
)
def delete_unused_character_candidates(character_id: str) -> CandidateCleanupResult:
    """基準画像に選ばなかった人物候補を、画像の控えごと削除する。"""
    _character_or_404(character_id)
    try:
        deleted = delete_unused_candidates(database, settings.data_dir, character_id)
    except BatchInUseError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return CandidateCleanupResult(deleted_count=deleted)


@app.get("/api/v1/character-candidates/{candidate_id}/image")
async def get_character_candidate_image(candidate_id: str) -> Response:
    try:
        candidate = CandidateRepository(database).get(candidate_id)
    except CandidateNotFoundError as error:
        raise HTTPException(status_code=404, detail="人物候補が見つかりません") from error
    return await _serve_generated_image(
        CANDIDATE_IMAGES, candidate_id, candidate.base_url, candidate.image
    )


@app.post("/api/v1/character-candidates/{candidate_id}/select-base", response_model=CandidateRead)
def select_character_base_image(candidate_id: str) -> CandidateRead:
    """立ち絵を作る起点になる基準画像を決める。

    ここで決めた顔から立ち絵を作る。基準画像そのものは合成へ渡さない
    （合成が使うのは立ち絵）。
    """
    try:
        return CandidateRepository(database).select_base(candidate_id)
    except CandidateNotFoundError as error:
        raise HTTPException(status_code=404, detail="人物候補が見つかりません") from error
    except CandidateSelectionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


def _model_not_found_message(filename: str) -> str:
    """このモデルが見えていないときに、利用者が何をすればよいかを伝える文。"""
    return (
        f"{filename} がComfyUIから見つかりません。"
        "この向きの作り方には、このモデルが置かれたフォルダを"
        "「既存のモデルを参照する」設定に含めてください。"
    )


async def _visible_diffusion_models(base_url: str) -> set[str] | None:
    """接続先のComfyUIから見えている拡散モデルのファイル名。

    確かめられないときは None を返す（起動していないだけのことがあるため、
    分からないことを「無い」とは言わない）。
    """
    try:
        object_info = await ComfyClient(base_url).object_info()
    except ComfyConnectionError:
        return None
    node = object_info.get("UNETLoader", {})
    required = node.get("input", {}).get("required", {}) if isinstance(node, dict) else {}
    specification = required.get("unet_name") if isinstance(required, dict) else None
    options = specification[0] if isinstance(specification, list) and specification else None
    return {str(name) for name in options} if isinstance(options, list) else None


async def _ensure_diffusion_model_available(base_url: str, filename: str) -> None:
    """指定した拡散モデルが、その接続先のComfyUIから見えているか確かめる。

    見えないまま投入するとComfyUIの英語エラーで失敗するため、先に日本語で伝える。
    一覧を取得できないときは止めない（生成側で分かる）。
    """
    visible = await _visible_diffusion_models(base_url)
    if visible is not None and filename not in visible:
        raise HTTPException(status_code=409, detail=_model_not_found_message(filename))
