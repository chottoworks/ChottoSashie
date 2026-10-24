"""人物ドメインのスキーマ（共通の土台）。

人物プロフィールと人物候補の入出力をここへ集約する。画面とAPI、共通の土台が
同じ定義を見て、食い違いを防ぐ。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, HttpUrl, field_validator

from .jobs import JobRead


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    illustration_style: str = Field(default="", max_length=1000)

    @field_validator("name", "description", "illustration_style", mode="before")
    @classmethod
    def strip_project_text(cls, value: str) -> str:
        return value.strip()


class ProjectRead(ProjectCreate):
    id: str
    character_count: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    illustration_style: str | None = Field(default=None, max_length=1000)

    @field_validator("name", "description", "illustration_style", mode="before")
    @classmethod
    def strip_optional_project_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class CharacterProfileFields(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    gender_age: str = Field(default="", max_length=200)
    face: str = Field(default="", max_length=1000)
    hair_style: str = Field(default="", max_length=500)
    hair_color: str = Field(default="", max_length=100)
    eyes: str = Field(default="", max_length=500)
    eye_color: str = Field(default="", max_length=100)
    body_type: str = Field(default="", max_length=500)
    base_outfit: str = Field(default="", max_length=1000)
    accessories: str = Field(default="", max_length=1000)
    personality: str = Field(default="", max_length=1000)
    immutable_features: list[str] = Field(default_factory=list, max_length=32)
    art_style: str = Field(default="", max_length=500)
    notes: str = Field(default="", max_length=4000)

    @field_validator(
        "name",
        "gender_age",
        "face",
        "hair_style",
        "hair_color",
        "eyes",
        "eye_color",
        "body_type",
        "base_outfit",
        "accessories",
        "personality",
        "art_style",
        "notes",
        mode="before",
    )
    @classmethod
    def strip_character_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("immutable_features")
    @classmethod
    def normalize_immutable_features(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            item = value.strip()
            if not item or item in normalized:
                continue
            if len(item) > 200:
                raise ValueError("変えてはいけない特徴は1件200文字以内で入力してください")
            normalized.append(item)
        return normalized


class CharacterCreate(CharacterProfileFields):
    pass


class CharacterUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    gender_age: str | None = Field(default=None, max_length=200)
    face: str | None = Field(default=None, max_length=1000)
    hair_style: str | None = Field(default=None, max_length=500)
    hair_color: str | None = Field(default=None, max_length=100)
    eyes: str | None = Field(default=None, max_length=500)
    eye_color: str | None = Field(default=None, max_length=100)
    body_type: str | None = Field(default=None, max_length=500)
    base_outfit: str | None = Field(default=None, max_length=1000)
    accessories: str | None = Field(default=None, max_length=1000)
    personality: str | None = Field(default=None, max_length=1000)
    immutable_features: list[str] | None = Field(default=None, max_length=32)
    art_style: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=4000)

    @field_validator("immutable_features")
    @classmethod
    def normalize_optional_features(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        return CharacterProfileFields.normalize_immutable_features(values)

    @field_validator(
        "name",
        "gender_age",
        "face",
        "hair_style",
        "hair_color",
        "eyes",
        "eye_color",
        "body_type",
        "base_outfit",
        "accessories",
        "personality",
        "art_style",
        "notes",
        mode="before",
    )
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class CharacterRead(CharacterProfileFields):
    id: str
    project_id: str
    created_at: datetime
    updated_at: datetime


class CandidateGenerateRequest(BaseModel):
    # 未指定ならセットアップ設定の接続先を使う。
    base_url: HttpUrl | None = None
    count: int = Field(default=8, ge=8, le=12)
    # 未指定時はバッチごとに新しい値を割り当てる。明示値は再現実行に使用する。
    # 画面側でも1刻みの連番を失わないJavaScript安全整数範囲に限定する。
    seed_start: int | None = Field(default=None, ge=0, le=9_007_199_254_740_979)
    width: int = Field(default=512, ge=256, le=1024, multiple_of=16)
    height: int = Field(default=688, ge=256, le=1536, multiple_of=16)
    negative_prompt: str = Field(
        default="multiple people, duplicate person, generic average face, default idol face, same face syndrome, text, watermark, blurry, low quality, malformed hands, extra limbs",
        max_length=2000,
    )
    # 変化強度。UIのスライダーに対応し、内部的には生成のcfgを動かす。値が大きいほど
    # 指定と候補ごとの差を強く出すが、上げすぎると戯画化・画風の硬化が起きる。
    variation_strength: float = Field(default=3.0, ge=2.0, le=3.5)
    diffusion_model: str = "z_image_bf16.safetensors"
    text_encoder: str = "qwen_3_4b.safetensors"
    vae: str = "ae.safetensors"

    @field_validator("base_url")
    @classmethod
    def allow_local_candidate_comfy_only(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is None:
            return value
        if value.scheme not in {"http", "https"} or value.host not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("候補生成にはこのPC上のComfyUIだけを指定できます")
        return value

    @field_validator("count")
    @classmethod
    def allow_candidate_presets_only(cls, value: int) -> int:
        if value not in {8, 10, 12}:
            raise ValueError("候補枚数は8、10、12枚から選択してください")
        return value


class CandidateImageInfo(BaseModel):
    filename: str
    subfolder: str = ""
    type: str = "output"


class CandidateRead(BaseModel):
    id: str
    character_id: str
    batch_job_id: str
    ordinal: int
    state: str
    prompt: str
    negative_prompt: str
    seed: int
    width: int
    height: int
    base_url: str
    prompt_id: str | None = None
    image: CandidateImageInfo | None = None
    is_base: bool
    error_message: str | None = None
    url: str | None = None
    created_at: datetime
    updated_at: datetime


class CandidateBatchResult(BaseModel):
    job: JobRead
    candidates: list[CandidateRead]


class BatchSummaryRead(BaseModel):
    """人物候補を作った1回ぶんの要約（前の回へ戻るための一覧）。"""

    batch_job_id: str
    created_at: str
    image_count: int
    completed_count: int
    is_latest: bool
    # 基準画像がこの回から選ばれているか。
    has_base: bool = False
