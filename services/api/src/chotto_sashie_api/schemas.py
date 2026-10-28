from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator

from chotto_sashie_shared.jobs import JobRead
from chotto_sashie_shared.recommendations import HardwareRecommendation
from chotto_sashie_shared.character_schemas import (
    CandidateGenerateRequest as SharedCandidateGenerateRequest,
)


class CompositionQuality(StrEnum):
    """静止画の合成の丁寧さ。速さと、指示の反映の強さを取り替える。"""

    FAST = "fast"
    CAREFUL = "careful"


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    # 作品全体の画風。人物候補・参照画像の生成で共有リポジトリが読む。
    illustration_style: str = Field(default="", max_length=2000)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    illustration_style: str | None = Field(default=None, max_length=2000)


class ProjectRead(BaseModel):
    id: str
    name: str
    description: str
    illustration_style: str = ""
    created_at: datetime
    updated_at: datetime


class AssetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    category: str = Field(default="", max_length=100)
    description: str = Field(default="", max_length=2000)


class AssetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    category: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=2000)


class AssetRead(BaseModel):
    id: str
    project_id: str
    name: str
    category: str
    description: str
    has_image: bool
    # 今の画像を作るときに ComfyUI へ渡した指示文（作品の画風を足したもの）。
    source_prompt: str
    # 今の画像を作るときに「生成プロンプト」欄へ入れた文。欄の初期値に使う。
    input_prompt: str = ""
    # 背景だけ: このバリエーションの元（自分が元なら None）。
    base_asset_id: str | None = None
    # 背景だけ: 元と何が違うのか（「夕方」「机に本」）。
    variant_label: str = ""
    created_at: datetime
    updated_at: datetime


# 背景のバリエーションの作り方。入力の枚数がこれで決まる。
# prop: 元背景＋小物（1〜2つ）／time: 元背景1枚だけ。
BackgroundVariationKind = Literal["prop", "time"]


class BackgroundVariationRequest(BaseModel):
    """元の背景から、小物を置いた／時間帯を変えた背景を作る要求。

    **時間帯を変えるときは、元背景以外の画像を混ぜない。** 背景1枚だけを渡す編集なら
    時間帯を変えられるが、人物や小物を一緒に渡す合成では変えられない（実測）。作り方を
    分けているのはそのためで、`time` では小物を受け取らない。
    """

    base_url: str | None = Field(default=None, max_length=500)
    kind: BackgroundVariationKind = "prop"
    # 一覧でグループの中を見分けるための見出し（「夕方」「机に本」）。
    variant_label: str = Field(default="", max_length=200)
    instruction: str = Field(default="", max_length=2000)
    prop_asset_ids: list[str] = Field(default_factory=list, max_length=2)
    seed: int | None = Field(default=None, ge=0, le=9_007_199_254_740_967)

    @field_validator("instruction", "variant_label", mode="before")
    @classmethod
    def strip_text(cls, value: str | None) -> str:
        return value.strip() if isinstance(value, str) else ""

    @model_validator(mode="after")
    def check_inputs_match_the_kind(self) -> BackgroundVariationRequest:
        if self.kind == "time":
            if self.prop_asset_ids:
                raise ValueError(
                    "時間帯・天気を変えるときは小物を選べません"
                    "（一緒に渡すと時間帯が変わりません）"
                )
            if not self.instruction:
                raise ValueError("どう変えるかを書いてください（例: 夕方にする）")
        elif not self.prop_asset_ids:
            raise ValueError("置く小物を選んでください")
        return self


class BackgroundVariationResult(BaseModel):
    """作り始めた直後の返り。画像は順番が来てから作られる。"""

    asset: AssetRead
    job_id: str


class CharacterCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class CharacterUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)


class PortraitCrop(BaseModel):
    """代表写真のうち一覧に出す範囲。画像の幅・高さに対する割合（0〜1）で持つ。

    割合で持つのは、元の画像の大きさに左右されないため。範囲が画像からはみ出す指定は
    受け付けない。
    """

    x: float = Field(ge=0, lt=1)
    y: float = Field(ge=0, lt=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _inside_image(self) -> PortraitCrop:
        # 画面側で割合を計算すると端で丸め誤差が出るので、わずかなはみ出しは許す。
        tolerance = 1e-6
        if self.x + self.width > 1 + tolerance or self.y + self.height > 1 + tolerance:
            raise ValueError("範囲が画像からはみ出しています")
        return self


class CharacterRead(BaseModel):
    id: str
    project_id: str
    name: str
    # 一覧に出す代表写真を、立ち絵とは別に持っているか。
    has_portrait_image: bool = False
    # 代表写真に選んでいる候補の指定子（未設定なら空）。
    portrait_source: str = ""
    # 代表写真のうち一覧に出す範囲。None なら全体を出す。
    portrait_crop: PortraitCrop | None = None
    # 出来上がっている立ち絵の枚数。0なら合成に使える人物画像がまだ無い。
    figure_count: int = 0
    # 一覧に出す写真の版。出ている画像が変わるたびに変わる（画像URLの付け分けに使う）。
    portrait_version: str = ""
    created_at: datetime
    updated_at: datetime


class PortraitOption(BaseModel):
    """代表写真に選べる画像1件。"""

    id: str
    label: str
    url: str
    selected: bool = False


class PortraitSelectRequest(BaseModel):
    option_id: str = Field(min_length=1, max_length=200)


class PortraitCropRequest(BaseModel):
    # None で範囲の指定をやめ、全体を出す。
    crop: PortraitCrop | None = None


# --- 立ち絵 ---

# 立ち絵の生成に使うモデル（アプリが取得するもの）。合成と同じ一式に、向きを
# 作るための角度LoRAを足す。
DEFAULT_FIGURE_DIFFUSION_MODEL = "qwen_image_edit_2511_fp8mixed.safetensors"
DEFAULT_FIGURE_TEXT_ENCODER = "qwen_2.5_vl_7b_fp8_scaled.safetensors"
DEFAULT_FIGURE_VAE = "qwen_image_vae.safetensors"
DEFAULT_FIGURE_ANGLE_LORA = "qwen-image-edit-2511-multiple-angles-lora.safetensors"
DEFAULT_FIGURE_LIGHTNING_LORA = "Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors"

class CharacterFigureGenerateRequest(BaseModel):
    """基準画像から立ち絵を1枚作る要求。

    1回に作るのは1枚。衣装や小物は出来た1枚を「変更」で変えるので、まとめて何枚も
    作って当たりを探す必要が無い。

    向きは正面だけなので、左右の効きで方式を選ぶ余地が無い。参照画像の
    「向きの作り方」のような選択肢は置かず、取得済みのモデルで固定する。
    """

    base_url: str | None = Field(default=None, max_length=500)
    seed: int | None = Field(default=None, ge=0, le=9_007_199_254_740_967)
    diffusion_model: str = Field(default=DEFAULT_FIGURE_DIFFUSION_MODEL, max_length=300)
    text_encoder: str = Field(default=DEFAULT_FIGURE_TEXT_ENCODER, max_length=300)
    vae: str = Field(default=DEFAULT_FIGURE_VAE, max_length=300)
    angle_lora: str = Field(default=DEFAULT_FIGURE_ANGLE_LORA, max_length=300)
    lightning_lora: str = Field(default=DEFAULT_FIGURE_LIGHTNING_LORA, max_length=300)
    angle_lora_strength: float = Field(default=1.0, ge=0.1, le=1.5)
    weight_dtype: str = Field(default="default", max_length=50)

    @field_validator("weight_dtype")
    @classmethod
    def allow_known_weight_dtype(cls, value: str) -> str:
        if value not in {"default", "fp8_e4m3fn", "fp8_e5m2"}:
            raise ValueError("拡散モデルの読み込み精度の指定が正しくありません")
        return value


class CharacterFigureEditRequest(BaseModel):
    """いまある立ち絵の衣装・小物を変える要求（立ち絵の変更）。

    指示だけでも、小物の画像を添えても作れる。どちらも無いと何も変わらないので、
    受け口で断る。
    """

    base_url: str | None = Field(default=None, max_length=500)
    # 利用者が日本語で書く指示（「制服をパーカーとジーンズに変える」）。
    instruction: str = Field(default="", max_length=2000)
    # 合わせる小物。画像の枚数は元の立ち絵と合わせて3枚までに収める。
    prop_asset_ids: list[str] = Field(default_factory=list, max_length=2)
    seed: int | None = Field(default=None, ge=0, le=9_007_199_254_740_967)

    @field_validator("instruction", mode="before")
    @classmethod
    def strip_instruction(cls, value: str | None) -> str:
        return value.strip() if isinstance(value, str) else ""

    @model_validator(mode="after")
    def require_something_to_change(self) -> CharacterFigureEditRequest:
        if not self.instruction and not self.prop_asset_ids:
            raise ValueError("変えたいことを書くか、合わせる小物を選んでください")
        return self


class CharacterFigureEditRead(BaseModel):
    """立ち絵の変更の結果（保存するまで預かっている1枚）。"""

    id: str
    figure_id: str
    state: str
    has_image: bool = False
    url: str | None = None
    instruction: str = ""
    prop_asset_ids: list[str] = Field(default_factory=list)
    job_id: str = ""
    error_message: str = ""
    created_at: datetime
    updated_at: datetime




class CharacterFigureRead(BaseModel):
    """立ち絵1枚。画像の持ち主はこの記録で、静止画は選ぶだけ。"""

    id: str
    character_id: str
    label: str = ""
    # base=基準画像から / variation=別の立ち絵の変更を残した / upload=手持ちの画像
    origin: str
    state: str
    seed: int = 0
    # 何も選ばれていないときに合成が使う1枚か。
    is_default: bool = False
    has_image: bool = False
    url: str | None = None
    instruction: str = ""
    prop_asset_ids: list[str] = Field(default_factory=list)
    source_figure_id: str | None = None
    job_id: str = ""
    ordinal: int = 1
    error_message: str = ""
    # この立ち絵へ預かっている変更の結果（保存前）。無ければ None。
    edit: CharacterFigureEditRead | None = None
    created_at: datetime
    updated_at: datetime


class CharacterFigureUpdate(BaseModel):
    label: str | None = Field(default=None, max_length=200)
    # True のときだけ意味がある（既定は「下ろす」ではなく「付け替える」操作なので）。
    is_default: bool | None = None


class CharacterFigureOrder(BaseModel):
    """立ち絵の並び。この人物の立ち絵すべてのidを、左から順に渡す。"""

    figure_ids: list[str] = Field(max_length=1000)


class CharacterFigureJobResult(BaseModel):
    """立ち絵を作る・変更するよう頼んだ直後の返り。画像は順番が来てから作られる。"""

    job: JobRead


# 背景・小物の生成に使う Z-Image Turbo モデルの既定名（ワークフローに焼き込み済み）。
DEFAULT_ASSET_DIFFUSION_MODEL = "z_image_turbo_bf16.safetensors"
DEFAULT_ASSET_TEXT_ENCODER = "qwen_3_4b.safetensors"
DEFAULT_ASSET_VAE = "ae.safetensors"


class AssetGenerateRequest(BaseModel):
    base_url: str | None = Field(default=None, max_length=500)
    prompt: str = Field(min_length=1, max_length=4000)
    negative_prompt: str = Field(default="", max_length=2000)
    seed: int | None = Field(default=None, ge=0)
    width: int = Field(default=1280, ge=256, le=2048, multiple_of=16)
    height: int = Field(default=720, ge=256, le=2048, multiple_of=16)
    diffusion_model: str = Field(default=DEFAULT_ASSET_DIFFUSION_MODEL, max_length=300)
    text_encoder: str = Field(default=DEFAULT_ASSET_TEXT_ENCODER, max_length=300)
    vae: str = Field(default=DEFAULT_ASSET_VAE, max_length=300)


class AssetGenerateResult(BaseModel):
    asset: AssetRead
    job_id: str


class StillImageCreate(BaseModel):
    name: str = Field(default="", max_length=200)
    background_asset_id: str | None = None
    character_ids: list[str] = Field(default_factory=list, max_length=8)


class StillImageUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    background_asset_id: str | None = None
    character_ids: list[str] | None = Field(default=None, max_length=8)


class StillImageRead(BaseModel):
    id: str
    project_id: str
    ordinal: int
    name: str
    background_asset_id: str | None
    character_ids: list[str]
    composition_job_id: str | None
    has_image: bool
    # 実行できない操作は押せないようにするための判定と、その理由。
    can_compose: bool = True
    compose_blocked_reason: str = ""
    created_at: datetime
    updated_at: datetime


class CompositionLimits(BaseModel):
    """合成の作りから決まる上限。画面はこれを見て枚数を出す。"""

    max_reference_images: int
    # 1枚に入れられる人物の数。背景1枚ぶんを差し引いた残りが上限になる。
    max_characters: int


# ワークフローに焼き込んである既定のQwen-Image-Editモデル名。呼び出し側が
# モデル探索の結果を持っていればそれで上書きし、無ければこの既定で動かす。
DEFAULT_COMPOSITION_DIFFUSION_MODEL = "qwen_image_edit_2511_fp8mixed.safetensors"
DEFAULT_COMPOSITION_TEXT_ENCODER = "qwen_2.5_vl_7b_fp8_scaled.safetensors"
DEFAULT_COMPOSITION_VAE = "qwen_image_vae.safetensors"
DEFAULT_COMPOSITION_LIGHTNING_LORA = (
    "Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors"
)


class CompositionRequest(BaseModel):
    # 未指定ならセットアップ設定の接続先を使う。
    base_url: str | None = Field(default=None, max_length=500)
    prompt: str = Field(default="", max_length=4000)
    negative_prompt: str = Field(default="", max_length=2000)
    seed: int | None = Field(default=None, ge=0)
    width: int = Field(default=1280, ge=256, le=2048)
    height: int = Field(default=720, ge=256, le=2048)
    diffusion_model: str = Field(default=DEFAULT_COMPOSITION_DIFFUSION_MODEL, max_length=300)
    text_encoder: str = Field(default=DEFAULT_COMPOSITION_TEXT_ENCODER, max_length=300)
    vae: str = Field(default=DEFAULT_COMPOSITION_VAE, max_length=300)
    lightning_lora: str = Field(default=DEFAULT_COMPOSITION_LIGHTNING_LORA, max_length=300)
    # 速さ優先（既定）は4ステップの高速LoRAで合成する。丁寧はそれを使わず、
    # ステップ数を増やして指示（姿勢の維持など）の反映を強める。時間はかかる。
    quality: CompositionQuality = CompositionQuality.FAST
    # 画面に出ている人物ごとの指定（人物のID → 指定）。合成するときに保存し、その内容で
    # 合成する。画面の指定と合成に使う指定が食い違わないよう、保存は合成と一緒にしか
    # 行わない。含まれない人物は保存済みの指定のまま。
    character_specs: dict[str, StillImageCharacterSpec] = Field(default_factory=dict)


class VideoEnabledUpdate(BaseModel):
    """「動画も作るか」だけの切り替え。接続方式の設定には触れない。"""

    enabled: bool


class CompositionResult(BaseModel):
    still_image: StillImageRead
    job_id: str


# ビートの実現方法。single=1回で生成（時刻ラベル付きプロンプト）/
# split=ビートごとに区間を生成し、最終フレームで繋ぐ。
TimelineMode = Literal["single", "split"]

# 起点フレームが実際にどこから来るか。押す前に画面へ出すために持つ
# （黙って静止画へ戻る形にしない）。
StartFrameSource = Literal["custom", "previous", "still_image", "none"]


class _ShotSpeechLineFields(BaseModel):
    """セリフ・ナレーションの1行。誰が話すかを行ごとに持つ。

    話す人を決めずにセリフだけ渡すと、二人いる場面ではどちらが口を動かすかがモデル任せに
    なる。ナレーションは画面の外の声として扱い、画面の人物には口を動かさせない。
    """

    # この静止画にいる人物。ナレーションのときは None。
    speaker_character_id: str | None = Field(default=None, max_length=100)
    narration: bool = False
    text: str = Field(default="", max_length=2000)


class ShotSpeechLine(_ShotSpeechLineFields):
    @model_validator(mode="after")
    def speaker_is_one_of(self) -> "ShotSpeechLine":
        if self.narration and self.speaker_character_id:
            raise ValueError("ナレーションの行に話す人は指定できません")
        if not self.narration and not self.speaker_character_id:
            raise ValueError("セリフの行には話す人を指定してください")
        return self


class ShotSpeechLineRead(_ShotSpeechLineFields):
    # 保存後に人物が消えると話す人が外れる（ON DELETE SET NULL）ので、読むときは検証しない。
    # 話す人を見分けるための記述（配置・年齢・髪）。生成に使う文ではこれを英語にして名指しする。
    # 名前は使わない——日本語の名前を文に入れると、セリフと区別されず読み上げられる。
    speaker_description: str = ""
    # 話す人がこの静止画にいるか。人物を外した・消したあとは False になる。
    speaker_present: bool = True


class ShotCreate(BaseModel):
    motion_prompt: str = Field(default="", max_length=4000)
    speech_lines: list[ShotSpeechLine] = Field(default_factory=list, max_length=50)
    ambience_prompt: str = Field(default="", max_length=2000)
    music_prompt: str = Field(default="", max_length=2000)
    composition_prompt: str = Field(default="", max_length=2000)
    timeline_mode: TimelineMode = "single"
    # 利用者の覚え書き。生成には渡さない。
    note: str = Field(default="", max_length=2000)
    duration_seconds: float = Field(default=5.0, gt=0, le=30)


class ShotUpdate(BaseModel):
    motion_prompt: str | None = Field(default=None, max_length=4000)
    # 行の並びをまるごと置き換える。
    speech_lines: list[ShotSpeechLine] | None = Field(default=None, max_length=50)
    ambience_prompt: str | None = Field(default=None, max_length=2000)
    music_prompt: str | None = Field(default=None, max_length=2000)
    composition_prompt: str | None = Field(default=None, max_length=2000)
    timeline_mode: TimelineMode | None = None
    note: str | None = Field(default=None, max_length=2000)
    duration_seconds: float | None = Field(default=None, gt=0, le=30)


class ShotBeatCreate(BaseModel):
    duration_seconds: float = Field(default=2.0, gt=0, le=30)
    motion_prompt: str = Field(default="", max_length=4000)
    speech_lines: list[ShotSpeechLine] = Field(default_factory=list, max_length=50)


class ShotBeatUpdate(BaseModel):
    duration_seconds: float | None = Field(default=None, gt=0, le=30)
    motion_prompt: str | None = Field(default=None, max_length=4000)
    # 行の並びをまるごと置き換える。
    speech_lines: list[ShotSpeechLine] | None = Field(default=None, max_length=50)


class ShotBeatRead(BaseModel):
    id: str
    shot_id: str
    ordinal: int
    # このビートが始まる時刻（先行ビートの長さの合計）。編集は長さで行い、時刻は導出する。
    start_seconds: float
    duration_seconds: float
    motion_prompt: str
    speech_lines: list[ShotSpeechLineRead] = Field(default_factory=list)
    # 分割モードで作った区間動画。
    has_segment_video: bool = False
    segment_video_format: Literal["webp", "mp4", ""] = ""
    has_last_frame: bool = False
    created_at: datetime
    updated_at: datetime


class ShotRead(BaseModel):
    id: str
    still_image_id: str
    ordinal: int
    motion_prompt: str
    # ビートが無いときに使う、カット全体のセリフ・ナレーション。
    speech_lines: list[ShotSpeechLineRead] = Field(default_factory=list)
    ambience_prompt: str = ""
    music_prompt: str = ""
    composition_prompt: str = ""
    timeline_mode: TimelineMode = "single"
    # 利用者の覚え書き。生成に使う文には入らない。
    note: str = ""
    duration_seconds: float
    has_start_frame: bool
    # 次の生成でどこから始まるか（前カットが未生成なら静止画に戻る、を先に見せる）。
    start_frame_source: StartFrameSource = "none"
    # 構図変更で作った起点フレーム／終端フレームがあるか。
    has_custom_start_frame: bool = False
    has_custom_end_frame: bool = False
    video_job_id: str | None
    has_video: bool
    has_last_frame: bool
    beats: list[ShotBeatRead] = Field(default_factory=list)
    # 実際に生成へ渡す尺（ビートの合計、無ければ duration_seconds）。
    total_seconds: float = 0.0
    # 生成済み動画の入れ物（webp=Wan / mp4=MiniMax H3）。画面の再生方法がこれで決まる。
    video_format: Literal["webp", "mp4", ""] = ""
    # 実行できない操作は押せないようにするための判定と、その理由。
    can_generate_video: bool = True
    video_blocked_reason: str = ""
    # 生成に使う文が用意できているか。用意できていない文は画面に出さない。
    has_prompt: bool = False
    # 用意したあとで指示が変わったか（＝その文はもう使われない）。
    prompt_is_stale: bool = False
    # 生成に使う文を直接書き直しているか。この間は指示の欄を変えられない。
    prompt_edit_mode: bool = False
    created_at: datetime
    updated_at: datetime


# Wan 2.2 I2V に使うモデルの既定名（ワークフローに焼き込み済み）。
DEFAULT_WAN_DIFFUSION_MODEL_HIGH = "wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors"
DEFAULT_WAN_DIFFUSION_MODEL_LOW = "wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors"
DEFAULT_WAN_TEXT_ENCODER = "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
DEFAULT_WAN_VAE = "wan_2.1_vae.safetensors"
DEFAULT_WAN_LIGHTNING_HIGH = "wan2.2_i2v_lightx2v_4steps_lora_v1_high_noise.safetensors"
DEFAULT_WAN_LIGHTNING_LOW = "wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors"

# MiniMax H3（fl2va）に使うモデルの既定名。公式テンプレートと同じ量子化版を使う。
DEFAULT_H3_DIFFUSION_MODEL = "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
DEFAULT_H3_TEXT_ENCODER = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
DEFAULT_H3_VIDEO_VAE = "minimax_h3_video_vae_fp16.safetensors"
DEFAULT_H3_AUDIO_VAE = "minimax_h3_audio_vae_fp32.safetensors"
DEFAULT_H3_TURBO_LORA = "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors"

# 動画生成エンジン。Wan 2.2 は無音のwebp、MiniMax H3 は音声付きmp4を出す。
VideoEngine = Literal["wan22", "minimax_h3"]

# エンジンごとの既定（解像度・フレーム数・fps）。未指定の項目はここから埋める。
_ENGINE_DEFAULTS: dict[str, dict[str, float | int]] = {
    # Wanは16fpsで81フレーム≒5秒。
    "wan22": {"width": 1280, "height": 720, "length": 81, "fps": 16.0},
    # H3の素の画面は短辺768・最大768x1344。24fpsで124フレーム≒5秒。
    "minimax_h3": {"width": 1344, "height": 768, "length": 124, "fps": 24.0},
}

# エンジンごとの動画の入れ物。保存時の拡張子と、画面での再生方法がこれで決まる。
ENGINE_VIDEO_FORMATS: dict[str, Literal["webp", "mp4"]] = {
    "wan22": "webp",
    "minimax_h3": "mp4",
}


class ShotGenerateRequest(BaseModel):
    base_url: str | None = Field(default=None, max_length=500)
    engine: VideoEngine = "wan22"
    # 未指定（None）ならカットに保存された値を使う。空文字は「意図的に空」を意味する。
    motion_prompt: str | None = Field(default=None, max_length=4000)
    # セリフ・ナレーションと環境音。動きの指示と混ぜない（MiniMax H3 のみ有効）。
    speech_lines: list[ShotSpeechLine] | None = Field(default=None, max_length=50)
    ambience_prompt: str | None = Field(default=None, max_length=2000)
    music_prompt: str | None = Field(default=None, max_length=2000)
    # ビートの実現方法。未指定ならカットに保存された値を使う。
    timeline_mode: TimelineMode | None = None
    # MiniMax H3 は否定条件を持たない（ガイダンス無しで進む）ため、Wanでのみ使う。
    negative_prompt: str = Field(default="", max_length=2000)
    seed: int | None = Field(default=None, ge=0)
    # 解像度・尺はエンジンごとに刻みが違うため、未指定なら engine の既定を使う。
    width: int | None = Field(default=None, ge=256, le=1920)
    height: int | None = Field(default=None, ge=256, le=1920)
    length: int | None = Field(default=None, ge=5, le=362)
    fps: float | None = Field(default=None, ge=4, le=60)
    # 前カットの最終フレームを起点にして繋ぐ（既定でオン）。
    continue_from_previous: bool = True
    diffusion_model_high: str = Field(default=DEFAULT_WAN_DIFFUSION_MODEL_HIGH, max_length=300)
    diffusion_model_low: str = Field(default=DEFAULT_WAN_DIFFUSION_MODEL_LOW, max_length=300)
    lightning_lora_high: str = Field(default=DEFAULT_WAN_LIGHTNING_HIGH, max_length=300)
    lightning_lora_low: str = Field(default=DEFAULT_WAN_LIGHTNING_LOW, max_length=300)
    text_encoder: str = Field(default=DEFAULT_WAN_TEXT_ENCODER, max_length=300)
    vae: str = Field(default=DEFAULT_WAN_VAE, max_length=300)
    h3_diffusion_model: str = Field(default=DEFAULT_H3_DIFFUSION_MODEL, max_length=300)
    h3_text_encoder: str = Field(default=DEFAULT_H3_TEXT_ENCODER, max_length=300)
    h3_video_vae: str = Field(default=DEFAULT_H3_VIDEO_VAE, max_length=300)
    h3_audio_vae: str = Field(default=DEFAULT_H3_AUDIO_VAE, max_length=300)
    h3_turbo_lora: str = Field(default=DEFAULT_H3_TURBO_LORA, max_length=300)
    # H3は高速化LoRAを前提に8ステップ。外すなら20前後まで上げる必要がある。
    h3_steps: int = Field(default=8, ge=1, le=60)
    h3_turbo_lora_strength: float = Field(default=1.0, ge=0.0, le=2.0)

    # 尺が要求で明示されたか。既定で埋めた後は `model_fields_set` では区別できないため、
    # 埋める前にここへ控える（未指定ならカットの尺・ビートの合計から決め直す）。
    _length_explicit: bool = PrivateAttr(default=False)

    @property
    def length_explicit(self) -> bool:
        return self._length_explicit

    @model_validator(mode="after")
    def fill_engine_defaults(self) -> ShotGenerateRequest:
        """未指定の解像度・尺・fpsをエンジンの既定で埋め、その刻みに合っているか見る。

        Wanは 4k+1 フレーム・16の倍数、H3は 17k+5 フレーム・32の倍数と刻みが違う。
        違う値のまま投げるとComfyUI側で落ちるので、ここで断る。
        """
        # 既定で埋めると「明示された」扱いになるため、埋める前に控える。
        self._length_explicit = "length" in self.model_fields_set
        defaults = _ENGINE_DEFAULTS[self.engine]
        if self.width is None:
            self.width = int(defaults["width"])
        if self.height is None:
            self.height = int(defaults["height"])
        if self.length is None:
            self.length = int(defaults["length"])
        if self.fps is None:
            self.fps = float(defaults["fps"])

        if self.engine == "wan22":
            if self.length % 4 != 1:
                raise ValueError("フレーム数は 4k+1（81, 121 など）である必要があります")
            if self.width % 16 or self.height % 16:
                raise ValueError("Wan 2.2 の幅と高さは16の倍数である必要があります")
        else:
            if self.length % 17 != 5:
                raise ValueError("フレーム数は 17k+5（124, 209 など）である必要があります")
            if self.width % 32 or self.height % 32:
                raise ValueError("MiniMax H3 の幅と高さは32の倍数である必要があります")
            if self.width * self.height > 768 * 1344:
                raise ValueError("MiniMax H3 の画面は 768x1344 相当までです")
        return self


class ShotGenerateResult(BaseModel):
    shot: ShotRead
    job_id: str


class ShotFrameTarget(StrEnum):
    """構図を変えて作り直すフレームの位置。"""

    START = "start"
    END = "end"


class ShotFrameRequest(BaseModel):
    """前カットの最終フレーム（または元の静止画）を編集して、起点・終端の絵を作る。"""

    base_url: str | None = Field(default=None, max_length=500)
    target: ShotFrameTarget = ShotFrameTarget.START
    # 起点の構図の指示。未指定ならカットに保存された composition_prompt を使う。
    composition_prompt: str | None = Field(default=None, max_length=2000)
    seed: int | None = Field(default=None, ge=0)
    width: int = Field(default=1280, ge=256, le=2048)
    height: int = Field(default=720, ge=256, le=2048)
    diffusion_model: str = Field(default=DEFAULT_COMPOSITION_DIFFUSION_MODEL, max_length=300)
    text_encoder: str = Field(default=DEFAULT_COMPOSITION_TEXT_ENCODER, max_length=300)
    vae: str = Field(default=DEFAULT_COMPOSITION_VAE, max_length=300)
    lightning_lora: str = Field(default=DEFAULT_COMPOSITION_LIGHTNING_LORA, max_length=300)
    # 構図の変更は指示の反映が肝なので、既定は丁寧側（高速LoRAを使わない）。
    quality: CompositionQuality = CompositionQuality.CAREFUL


class ShotFrameResult(BaseModel):
    shot: ShotRead
    job_id: str


class ShotPromptSegment(BaseModel):
    """生成1回ぶんの、実際に送られる文と尺。"""

    label: str
    prompt: str
    length_frames: int
    seconds: float
    # 利用者が直接書き直した文か。
    edited: bool = False


class ShotPromptState(BaseModel):
    """カットの生成に使う文の、いまの状態。

    文は「用意できたもの」だけを画面に出す。途中の姿を見せると、直したつもりの内容と
    実際に使われる文が食い違う。用意できていないときは `segments` を空で返す。
    """

    engine: VideoEngine
    timeline_mode: TimelineMode
    # 一括なら1件、分割ならビートごと。用意できていなければ空。
    segments: list[ShotPromptSegment] = Field(default_factory=list)
    total_seconds: float = 0.0
    # 用意できた文があるか。
    ready: bool = False
    # 用意したあとで指示・作り方が変わったか（＝この文はもう使われない）。
    stale: bool = False
    # 直接書き直しているか。この間は指示の欄を変えられない。
    edit_mode: bool = False


class ShotPromptRequest(BaseModel):
    """生成に使う文を用意する（動画は作らない）。"""

    engine: VideoEngine = "wan22"
    base_url: str | None = None


class ShotPromptResult(BaseModel):
    shot: ShotRead
    job_id: str


class ShotPromptEdit(BaseModel):
    """直接書き直した文。区間の数は用意した文と揃える。"""

    segments: list[str] = Field(default_factory=list)


class ShotPromptModeUpdate(BaseModel):
    edit_mode: bool


# 要件チェックの型は共通の土台にある。
from chotto_sashie_shared.requirements import (  # noqa: E402
    CapabilityRequirement,
    RequirementModelStatus,
    RequirementToolStatus,
    RequirementsReport,
)


# セットアップ設定の型。
from chotto_sashie_shared.setup_config import (  # noqa: E402
    SetupConfigRead,
    SetupConfigUpdate,
    SetupMode,
)


class ComfyUIStatus(BaseModel):
    provisioned: bool
    target_version: str
    # 実際に入っているComfyUIのバージョン（読めなければ空）。
    installed_version: str = ""
    # 入っているものが必要な版より古いか（持ち主が誰かに関係なく判定する）。
    update_available: bool = False
    comfyui_path: str | None
    python_path: str | None = None
    # shared（共有フォルダ）/ existing（利用者指定の借用。アプリからは変更しない）
    source: str = ""
    # このアプリから準備・更新してよいか（借用中だけ False）。
    can_update: bool = True
    # いま使っている本体の展開先と、共有フォルダの場所。
    install_root: str = ""
    shared_root: str = ""


class SetupStatus(BaseModel):
    mode: SetupMode
    configured: bool
    resolved_base_url: str
    # 実際に接続できたか（モードに関わらず確認する）。未設定のときだけ None。
    reachable: bool | None
    message: str
    # 生成物と設定の保存先。制作画面の状態表示に出す。
    data_dir: str = ""


class HardwareReport(BaseModel):
    """この端末の構成と、それに合わせた既定値の提案。

    調べられなかった項目は空・0のまま返す（推測で埋めない）。`applied` は、いま覚えている
    既定値がこの提案と一致しているか＝適用済みかどうか。
    """

    detected: bool
    # どこから読んだか。comfyui | nvidia-smi | none
    source: str
    gpu_name: str = ""
    vram_total_bytes: int = 0
    # いま空いているVRAM。ComfyUIから読めたときだけ入る。
    vram_free_bytes: int | None = None
    ram_total_bytes: int = 0
    # 例: "12.0"（Blackwell）。MiniMax H3 のnvfp4版が動くかの判断に使う。
    compute_capability: str = ""
    driver_version: str = ""
    # 調べられなかったときに、何が起きたかを利用者へ伝える一言。
    message: str = ""
    recommendation: HardwareRecommendation
    applied: bool = False


class ManagedRuntimeStatus(BaseModel):
    state: str
    owned: bool
    reachable: bool
    pid: int | None
    base_url: str
    profile_path: str | None
    log_path: str | None
    message: str


class ModelScanRequest(BaseModel):
    roots: list[str] = Field(min_length=1, max_length=32)
    calculate_sha256: bool = False
    max_files: int = Field(default=20_000, ge=1, le=200_000)


class StillImageUsageInfo(BaseModel):
    """この対象を参照している静止画（削除確認に「3つの静止画から外れます」と出す）。"""

    still_image_count: int = 0
    still_image_names: list[str] = Field(default_factory=list)


class CharacterDeletionPreview(BaseModel):
    character_id: str
    name: str
    usage: StillImageUsageInfo
    candidate_count: int
    # 一緒に消える立ち絵の枚数。
    figure_count: int = 0


class AssetDeletionPreview(BaseModel):
    asset_id: str
    name: str
    usage: StillImageUsageInfo


class StillImageDeletionPreview(BaseModel):
    still_image_id: str
    name: str
    ordinal: int
    shot_count: int
    video_count: int
    has_image: bool


class ShotDeletionPreview(BaseModel):
    shot_id: str
    ordinal: int
    has_video: bool
    # ビートと、そのうち区間動画を持つ数（削除確認に出す）。
    beat_count: int = 0
    beat_video_count: int = 0


class ProjectDeletionPreview(BaseModel):
    project_id: str
    name: str
    character_count: int
    background_count: int
    prop_count: int
    still_image_count: int
    shot_count: int
    video_count: int


class ExportStatus(BaseModel):
    ffmpeg_available: bool
    ffmpeg_path: str | None
    # 使えないときに、利用者がどこへ行けばよいか（用意は必要環境の確認で行う）。
    hint: str = ""


class ModelDownloadRequest(BaseModel):
    """取得するモデルの範囲。既定（空）は必須のモデルだけ。

    任意のまとまり（例: MiniMax H3 は合計約44GB）は、名前を挙げたときだけ取得する。
    """

    groups: list[str] = Field(default_factory=list, max_length=8)


class ExportRequest(BaseModel):
    # 未指定ならカット動画のfpsに合わせる。違う値で書き出すと、コマが間引かれたり水増しされたりする。
    fps: float | None = Field(default=None, ge=4, le=60)


class SaveAsRequest(BaseModel):
    """利用者がOSのダイアログで選んだ保存先。

    アプリの保存先の外へ書く唯一の経路。場所は利用者がその場で選んだものだけを受け取り、
    受け取った側でも実在と書ける先かを確かめてから複製する。
    """

    path: str = Field(min_length=1, max_length=4000)


class SaveAsResult(BaseModel):
    path: str
    size_bytes: int


class ProjectExportStatus(BaseModel):
    """この作品の、書き出し済みファイルの有無。

    書き出したmp4はアプリのデータ領域に残る。作り直すまで有効なので、画面を開き
    直しても取り出せることを伝えるために持つ。
    """

    exists: bool = False
    created_at: datetime | None = None
    size_bytes: int = 0
    # いまのカット動画のfps（重複なし・小さい順）。「カットに合わせる」の値として画面に出す。
    # 作り方を混ぜると複数になる。読めなければ空。
    cut_fps: list[float] = Field(default_factory=list)


class ExportResult(BaseModel):
    project_id: str
    shot_count: int
    download_url: str
    # 音声を残せたか（MiniMax H3のカットだけで揃っているときに真）。
    audio_included: bool = False
    # 実際に書き出したfps（「カットに合わせる」で決めた値も、ここで分かる）。
    fps: float



# --- 人物候補（共有スキーマに、このアプリの既定値を与える） ---


class VideoCandidateGenerateRequest(SharedCandidateGenerateRequest):
    """人物候補生成の要求。作り直しのときに前の回をどう扱うかを選べる。"""

    # True は前の回を履歴として残す。False は上書き——生成を始める前に、基準画像を
    # 除いたこの人物の候補を消す。None は設定（`candidate_history`）に従う。
    keep_history: bool | None = None


class BatchDeletionResult(BaseModel):
    """生成の回を削除した結果（画面の完了メッセージに使う）。"""

    batch_job_id: str
    deleted_count: int


class CandidateCleanupResult(BaseModel):
    """基準画像に選ばなかった人物候補を削除した結果。"""

    deleted_count: int


# --- 静止画ごとの人物の指定 ---


class StillImageCharacterSpec(BaseModel):
    """静止画1枚の中での、その人物の指定。

    衣装は立ち絵が持つので、ここでは指定しない（変えたいときは立ち絵の変更で別の
    立ち絵を作る）。構図は場面全体の話で人物ごとの指定ではないため、ここには無い。
    """

    pose: str = Field(default="", max_length=2000)
    expression: str = Field(default="", max_length=2000)
    direction: str = Field(default="", max_length=2000)
    placement: str = Field(default="", max_length=2000)
    notes: str = Field(default="", max_length=2000)

    @field_validator("pose", "expression", "direction", "placement", "notes", mode="before")
    @classmethod
    def strip_spec_fields(cls, value: str | None) -> str:
        return value.strip() if isinstance(value, str) else ""


class StillImageCharacterUpdate(BaseModel):
    """指定と、使う立ち絵の選択。`figure_id` が空なら既定の立ち絵を使う。

    `spec` を省くと、保存済みの指定に触れない。画面は立ち絵の選び直しだけをここで
    保存し、指定は合成のときに保存する（`CompositionRequest.character_specs`）。
    立ち絵と一緒に指定を送ると、画面が持っている古い指定で上書きしてしまう。
    """

    spec: StillImageCharacterSpec | None = None
    figure_id: str | None = None


class StillImageCharacterRead(BaseModel):
    character_id: str
    character_name: str
    spec: StillImageCharacterSpec
    # 選んでいる立ち絵。選んでいなければ既定の立ち絵が入る（実際に合成で使う1枚）。
    figure_id: str | None = None
    # その立ち絵を自分で選んだのか、既定のまま使っているのか。
    figure_is_default: bool = True
    figure_label: str = ""
    figure_url: str | None = None
    # 立ち絵が1枚も無い人物。合成はできない。
    has_figure: bool = False
