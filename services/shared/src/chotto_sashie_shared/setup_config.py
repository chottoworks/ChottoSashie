"""接続モード設定（セットアップ）の共有実装。

接続方式は3つ:
- existing_comfyui        既存のComfyUIをそのまま使う（base_urlへ接続、起動はしない）
- managed_external_models 専用ComfyUIを起動し、既存モデルを読み取り参照する
- managed_dedicated       完全に専用の環境（ComfyUI本体もモデルもアプリが用意）

「リクエストで明示されたbase_url優先、無ければこの設定値」で
接続先を解決する。既定ポートだけアプリごとに異なるため、リポジトリの引数で渡す。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from sqlite3 import Row
from typing import Protocol

from pydantic import BaseModel, Field, field_validator


# 専用ComfyUIの既定の番号。ComfyUIの既定（8188）とずらし、手元で普段使っている
# ComfyUIと同時に動かしても取り合わないようにする。
DEFAULT_MANAGED_PORT = 8199


class SetupMode(StrEnum):
    EXISTING_COMFYUI = "existing_comfyui"
    MANAGED_EXTERNAL_MODELS = "managed_external_models"
    MANAGED_DEDICATED = "managed_dedicated"


# 日本語配列のキーボードはバックスラッシュの位置に円記号があり、入力方式や
# フォントによって U+00A5 / U+FFE5 がそのまま送られてくる。Windowsのパス
# 区切りとして解釈されず「フォルダが見つかりません」になるため、受け取った
# 時点でバックスラッシュへ直す。エクスプローラーの「パスのコピー」が付ける
# 引用符も同じ理由で外す。
_SEPARATOR_ALIASES = ("¥", "￥")


def normalize_path_input(value: str) -> str:
    """利用者が入力・貼り付けしたフォルダ文字列をWindowsのパスとして扱える形に直す。"""
    text = value.strip().strip('"').strip()
    # 区切りとして使われた円記号だけを置き換える。バックスラッシュが既に含まれる
    # 入力は区切りが打てている＝円記号はフォルダ名の一部と判断し、そのまま残す。
    if "\\" not in text:
        for alias in _SEPARATOR_ALIASES:
            text = text.replace(alias, "\\")
    return text


class SetupConfigUpdate(BaseModel):
    mode: SetupMode
    # existing_comfyui で使用。
    base_url: str = Field(default="", max_length=500)
    # managed_* の起動ポート。
    port: int = Field(default=DEFAULT_MANAGED_PORT, ge=1024, le=65535)
    # managed_external_models で使用。
    external_model_roots: list[str] = Field(default_factory=list, max_length=32)
    # 不足モデルの取得先。空なら共有フォルダへ取得する。
    # 既存モデルフォルダを指定すると、そのフォルダへ不足分を書き足す（明示的な選択）。
    model_download_root: str = Field(default="", max_length=4000)
    # 既存のComfyUIインストールを使う場合の場所（任意）。
    # 未指定ならアプリが用意したポータブル版を使う。指定すると管理モードでこれを起動する。
    comfyui_path: str = Field(default="", max_length=4000)
    python_path: str = Field(default="", max_length=4000)
    # 動画も作るか。false なら静止画（挿絵）までで、シーン・カット・書き出しは出さない。
    # 動画用モデルの取得と要件チェックも対象から外れる。
    # None は「保存済みの値を変えない」。接続方式だけを保存し直す経路が、
    # 利用者の選択を勝手に戻さないようにする。
    video_enabled: bool | None = None

    @field_validator("comfyui_path", "python_path", "model_download_root")
    @classmethod
    def _normalize_path(cls, value: str) -> str:
        return normalize_path_input(value)

    @field_validator("external_model_roots")
    @classmethod
    def _normalize_roots(cls, value: list[str]) -> list[str]:
        return [normalized for root in value if (normalized := normalize_path_input(root))]


class SetupConfigRead(BaseModel):
    mode: SetupMode
    base_url: str
    port: int
    external_model_roots: list[str]
    comfyui_path: str
    python_path: str
    # 不足モデルの取得先。空なら共有フォルダ。
    model_download_root: str = ""
    # 動画も作るか。false なら静止画（挿絵）までの画面だけを出す。
    video_enabled: bool = True
    # 解決済みの接続先。生成時に使うComfyUIのURL。
    resolved_base_url: str
    configured: bool
    # 初回セットアップを完了したか。falseなら起動時にウィザードを出す。
    setup_completed: bool
    updated_at: datetime | None


class SetupConfigError(ValueError):
    pass


# comfyui_path/python_path は、既存のComfyUI本体を流用するときだけ値が入る（空なら
# 共有フォルダのComfyUIを使う）。
SETUP_CONFIG_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS setup_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mode TEXT NOT NULL,
    base_url TEXT NOT NULL DEFAULT '',
    comfyui_path TEXT NOT NULL DEFAULT '',
    python_path TEXT NOT NULL DEFAULT '',
    port INTEGER NOT NULL DEFAULT 8199,
    external_model_roots_json TEXT NOT NULL DEFAULT '[]',
    -- 初回セットアップを完了した時刻。空なら未完了で、起動時にウィザードを出す。
    completed_at TEXT NOT NULL DEFAULT '',
    -- 不足モデルの取得先。空なら共有フォルダ、値があればその既存モデルフォルダ。
    model_download_root TEXT NOT NULL DEFAULT '',
    -- 動画も作るか（1=作る）。0 なら静止画までの画面だけを出す。
    video_enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
"""


class _SupportsConnect(Protocol):
    def connect(self): ...  # noqa: ANN201 - sqlite3.Connection context manager


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _validate(request: SetupConfigUpdate) -> None:
    # 既存インストールの指定はどのモードでも検証する（指定があるときだけ）。
    # existing_comfyui でも、相手が停止していたとき自分で起動できるよう記録する。
    if request.comfyui_path.strip() or request.python_path.strip():
        comfy = Path(request.comfyui_path).expanduser()
        if not request.comfyui_path.strip() or not comfy.is_dir():
            raise SetupConfigError(f"ComfyUIフォルダが見つかりません: {request.comfyui_path}")
        if not (comfy / "main.py").is_file():
            raise SetupConfigError("指定フォルダにComfyUIのmain.pyがありません")
        python = Path(request.python_path).expanduser()
        if not request.python_path.strip() or not python.is_file():
            raise SetupConfigError(
                f"ComfyUI用のPython実行ファイルが見つかりません: {request.python_path}"
            )

    # 取得先の指定は、どの方式でも先に確かめる（方式に合わない指定を素通ししない）。
    _validate_download_root(request)

    if request.mode == SetupMode.EXISTING_COMFYUI:
        if not request.base_url.strip():
            raise SetupConfigError("既存のComfyUIを使うときは、接続先のアドレスを指定してください")
        if not request.base_url.startswith(("http://", "https://")):
            raise SetupConfigError(
                "接続先のアドレスは http:// または https:// で始めてください"
            )
        return

    # 既存モデル参照モードだけ、参照する外部モデルフォルダの存在を確認する。
    if request.mode == SetupMode.MANAGED_EXTERNAL_MODELS:
        if not request.external_model_roots:
            raise SetupConfigError("既存モデル参照モードでは外部モデルフォルダを1件以上指定してください")
        for root in request.external_model_roots:
            if not Path(root).expanduser().is_dir():
                raise SetupConfigError(f"外部モデルフォルダが見つかりません: {root}")


def _validate_download_root(request: SetupConfigUpdate) -> None:
    """既存モデルフォルダへの取得を選んだときだけ、その指定を確かめる。

    書き込み先は「利用者が参照フォルダとして挙げた場所」に限る。ここで受け付けた
    フォルダ以外へは、取得処理も書き込まない。
    """
    requested = request.model_download_root.strip()
    if not requested:
        return
    if request.mode != SetupMode.MANAGED_EXTERNAL_MODELS:
        raise SetupConfigError(
            "既存モデルフォルダへの取得は「既存モデルを参照する」方式のときだけ選べます"
        )
    target = Path(requested).expanduser()
    if not target.is_dir():
        raise SetupConfigError(f"モデルの取得先フォルダが見つかりません: {requested}")
    resolved = target.resolve()
    known = []
    for root in request.external_model_roots:
        try:
            known.append(Path(root).expanduser().resolve())
        except OSError:
            continue
    if resolved not in known:
        raise SetupConfigError(
            "モデルの取得先は、参照する既存モデルフォルダの中から選んでください"
        )


class SetupConfigRepository:
    def __init__(self, database: _SupportsConnect, *, default_port: int = DEFAULT_MANAGED_PORT):
        self.database = database
        self.default_port = default_port

    def _to_config(self, row: Row | None) -> SetupConfigRead:
        if row is None:
            # 既定は「既存ComfyUIをそのまま使う」未設定状態。
            return SetupConfigRead(
                mode=SetupMode.EXISTING_COMFYUI,
                base_url="",
                port=self.default_port,
                external_model_roots=[],
                comfyui_path="",
                python_path="",
                model_download_root="",
                video_enabled=True,
                resolved_base_url="",
                configured=False,
                setup_completed=False,
                updated_at=None,
            )
        mode = SetupMode(row["mode"])
        external = json.loads(row["external_model_roots_json"])
        resolved = (
            row["base_url"].rstrip("/")
            if mode == SetupMode.EXISTING_COMFYUI
            else f"http://127.0.0.1:{row['port']}"
        )
        return SetupConfigRead(
            mode=mode,
            base_url=row["base_url"],
            port=row["port"],
            external_model_roots=external,
            comfyui_path=row["comfyui_path"],
            python_path=row["python_path"],
            model_download_root=row["model_download_root"],
            video_enabled=bool(row["video_enabled"]),
            resolved_base_url=resolved,
            # 行があるだけでは設定済みとしない。「動画も作るか」だけを先に選んだ段階では
            # 接続先がまだ決まっていないため、そこを見て判断する。
            configured=mode != SetupMode.EXISTING_COMFYUI or bool(row["base_url"]),
            setup_completed=bool(row["completed_at"]),
            updated_at=row["updated_at"],
        )

    def get(self) -> SetupConfigRead:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM setup_config WHERE id = 1").fetchone()
        return self._to_config(row)

    def set(self, request: SetupConfigUpdate) -> SetupConfigRead:
        _validate(request)
        timestamp = _now()
        # 「動画も作るか」は未指定なら現状維持。接続方式だけの保存で選択を戻さない。
        current = self.get()
        video_enabled = (
            current.video_enabled if request.video_enabled is None else request.video_enabled
        )
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO setup_config(
                    id, mode, base_url, comfyui_path, python_path, port,
                    external_model_roots_json, model_download_root,
                    video_enabled, updated_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    mode = excluded.mode,
                    base_url = excluded.base_url,
                    comfyui_path = excluded.comfyui_path,
                    python_path = excluded.python_path,
                    port = excluded.port,
                    external_model_roots_json = excluded.external_model_roots_json,
                    model_download_root = excluded.model_download_root,
                    video_enabled = excluded.video_enabled,
                    updated_at = excluded.updated_at
                """,
                (
                    request.mode.value,
                    request.base_url.rstrip("/"),
                    request.comfyui_path,
                    request.python_path,
                    request.port,
                    json.dumps(request.external_model_roots, ensure_ascii=False),
                    request.model_download_root,
                    int(video_enabled),
                    timestamp,
                ),
            )
        return self.get()

    def reset_completion(self) -> SetupConfigRead:
        """完了印だけを外す。設定値は残すので、続きから調整できる。"""
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE setup_config SET completed_at = '', updated_at = ? WHERE id = 1",
                (_now(),),
            )
        return self.get()

    def set_video_enabled(self, enabled: bool) -> SetupConfigRead:
        """「動画も作るか」だけを差し替える（他の設定は検証も更新もしない）。

        接続方式より先に選べるようにするため、保存先の行が無ければここで作る。接続先は
        空のままなので、この時点では「設定済み」にはならない。
        """
        timestamp = _now()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO setup_config(id, mode, port, video_enabled, updated_at)
                VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    video_enabled = excluded.video_enabled,
                    updated_at = excluded.updated_at
                """,
                (SetupMode.EXISTING_COMFYUI.value, self.default_port, int(enabled), timestamp),
            )
        return self.get()

    def mark_completed(self) -> SetupConfigRead:
        """初回セットアップを完了として記録する（以後ウィザードを出さない）。"""
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE setup_config SET completed_at = ?, updated_at = ? WHERE id = 1",
                (_now(), _now()),
            )
        return self.get()

    def clear(self) -> SetupConfigRead:
        """保存済みの設定レコードを消し、初回起動と同じ未設定状態へ戻す。

        消すのはアプリが持つ設定レコードだけで、参照していた外部モデルフォルダや
        既存ComfyUIには一切触れない（記録を忘れるだけ）。
        """
        with self.database.connect() as connection:
            connection.execute("DELETE FROM setup_config WHERE id = 1")
        return self.get()

    def resolved_base_url(self) -> str | None:
        """設定済みなら接続先URLを、未設定なら None を返す。"""
        config = self.get()
        if not config.configured:
            return None
        return config.resolved_base_url or None
