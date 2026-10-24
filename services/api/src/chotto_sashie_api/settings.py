from __future__ import annotations

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from chotto_sashie_shared.shared_env import app_data_root


def default_data_dir() -> Path:
    """Windowsの小容量データ保存先を返す。

    旧配置のComfyUI・モデルを探す側（`shared_env`）と同じ規則で決める。ここだけ
    別に決めると、保存先を変えたときに探す場所とずれる。
    """
    return app_data_root()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CHOTTO_SASHIE_",
        extra="ignore",
    )

    app_name: str = "ちょっと挿絵"
    api_host: str = "127.0.0.1"
    api_port: int = 8766
    data_dir: Path = default_data_dir()
    development_origin: str = "http://localhost:1421"
    # 設定されている場合のみ、変更系リクエストにこのトークンを要求する。
    # デスクトップ版が起動時に発行して注入する。開発時は未設定で無効。
    api_token: str = ""
    # ffmpegの明示パス。未設定ならPATHから探す。
    ffmpeg_path: str = ""

    @field_validator("data_dir", mode="before")
    @classmethod
    def _resolve_data_dir(cls, value: object) -> object:
        if value is None or str(value).strip() in {"", "."}:
            return default_data_dir()
        return value

    @property
    def database_path(self) -> Path:
        return self.data_dir / "database" / "app.sqlite3"


settings = Settings()
