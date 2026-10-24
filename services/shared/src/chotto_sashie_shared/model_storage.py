"""取得したモデルをどこへ置くかを決める（共通の土台）。

置き場所は2つから選ぶ。

- 共有フォルダ（既定）: `%LOCALAPPDATA%\\ChottoSashieShared\\models`。
  同じものを二重に持たずに済む。
- 既存モデルフォルダ: 利用者がすでに持っているフォルダ（例 `D:\\AI\\Models`）へ
  不足分を書き足す。ComfyUIを普段使いしている人が、アプリ用に同じモデルを
  もう一つ持たなくて済むようにするための選択。

既存フォルダへの書き込みは、設定で明示的に選ばれているときだけ行い、実行の
たびに「設定済みの参照フォルダであること」「実際に書き込めること」を確かめる。
選ばれていないフォルダは、これまでどおり読み取りしかしない。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .model_paths import model_search_roots
from .setup_config import SetupConfigRead, SetupMode
from .shared_env import shared_models_root, unique_existing_dirs

# 取得先の種類。
TARGET_SHARED = "shared"
TARGET_EXTERNAL = "external"


class ModelStorageError(RuntimeError):
    """選ばれた取得先が使えない状態（設定漏れ・書き込み不可など）。"""


@dataclass(frozen=True)
class ModelStorage:
    """モデルの取得先と、すでに持っているかを探すフォルダ一式。"""

    target: str
    root: Path
    label: str
    # 生成時にモデルを探すフォルダすべて（表示と走査に使う）。
    search_roots: list[Path]
    # アプリが自分で置いた場所。ここにある同名・同サイズのファイルは自分の控えと
    # みなして取得を省く。利用者の既存フォルダは、同名でも中身が違いうるため
    # 含めない（そちらはSHA-256で照合してから使う）。
    managed_roots: list[Path]

    @property
    def external(self) -> bool:
        return self.target == TARGET_EXTERNAL


def _managed_roots(target_root: Path, *, include_target: bool) -> list[Path]:
    roots = [shared_models_root()]
    if include_target:
        roots.insert(0, target_root)
    return unique_existing_dirs(roots)


def configured_model_dirs(config: SetupConfigRead) -> list[Path]:
    """モデルが置かれている可能性のあるフォルダ（models 相当）を優先順に返す。

    生成時にモデルを探す規則（`model_search_roots`）と同じものを使う。取得の判断と
    実行時の解決がずれると、「取得したのに見つからない」が起きるため。
    """
    return model_search_roots(config)


def _match_configured_root(config: SetupConfigRead, requested: Path) -> Path | None:
    """指定された取得先が、設定済みの既存モデルフォルダかどうかを確かめる。"""
    try:
        target = requested.expanduser().resolve()
    except OSError:
        return None
    for root in config.external_model_roots:
        if not root.strip():
            continue
        try:
            candidate = Path(root).expanduser().resolve()
        except OSError:
            continue
        if candidate == target:
            return candidate
    return None


def verify_writable(root: Path) -> None:
    """そこへ実際に書き込めるかを、小さなファイルで確かめる（後片付けまで行う）。"""
    probe = root / f".chotto-sashie-write-test-{uuid4().hex}"
    try:
        root.mkdir(parents=True, exist_ok=True)
        with probe.open("wb") as stream:
            stream.write(b"")
    except OSError as error:
        raise ModelStorageError(f"このフォルダへは書き込めません: {root}（{error}）") from error
    finally:
        try:
            os.unlink(probe)
        except OSError:
            pass


def resolve_model_storage(config: SetupConfigRead, *, verify: bool = True) -> ModelStorage:
    """設定に従って取得先を決める。既存フォルダ指定なら妥当性も確かめる。"""
    search_roots = configured_model_dirs(config)
    requested = config.model_download_root.strip()
    if not requested:
        root = shared_models_root()
        return ModelStorage(
            target=TARGET_SHARED,
            root=root,
            label=f"共有フォルダ（{root}）",
            search_roots=[root, *search_roots],
            managed_roots=_managed_roots(root, include_target=True),
        )

    if config.mode != SetupMode.MANAGED_EXTERNAL_MODELS:
        raise ModelStorageError(
            "既存モデルフォルダへの追加は「既存モデルを参照する」方式のときだけ選べます"
        )
    matched = _match_configured_root(config, Path(requested))
    if matched is None:
        raise ModelStorageError(
            f"取得先が参照フォルダとして設定されていません: {requested}"
        )
    if not matched.is_dir():
        raise ModelStorageError(f"取得先のフォルダが見つかりません: {matched}")
    if verify:
        verify_writable(matched)
    return ModelStorage(
        target=TARGET_EXTERNAL,
        root=matched,
        label=f"既存モデルフォルダ（{matched}）",
        search_roots=[matched, *search_roots],
        # 取得先そのものは、保存先の相対パスで直接確かめるため含めない。
        managed_roots=_managed_roots(matched, include_target=False),
    )
