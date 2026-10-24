"""生成に使うモデルを探す場所を解決する（共通の土台）。

`setup_config` を唯一の出どころにして、取得の判断（不足モデルの一覧）と生成時の
探索が同じ規則を見るようにする。

探索するのは読み取りのみで、外部フォルダへは一切書き込まない。
"""

from __future__ import annotations

from pathlib import Path

from .setup_config import SetupConfigRead
from .shared_env import shared_models_root, unique_existing_dirs


class ModelResolutionError(RuntimeError):
    pass


def model_search_roots(config: SetupConfigRead) -> list[Path]:
    """モデルファイルを探すフォルダを優先順に返す。

    共有フォルダ → 設定済みの既存モデルフォルダ → 既存ComfyUI本体の models。
    実在するフォルダだけを、重複を除いて返す。取得先をどこにしていても同じ規則で
    見つかるようにする。
    """
    roots: list[Path] = [shared_models_root()]
    roots.extend(Path(root).expanduser() for root in config.external_model_roots if root.strip())
    if config.comfyui_path.strip():
        roots.append(Path(config.comfyui_path).expanduser() / "models")
    return unique_existing_dirs(roots)


def find_model_file(roots: list[Path], category: str, filename: str) -> Path:
    """`category/filename` を優先し、無ければ再帰探索でモデルファイルを探す。"""
    for root in roots:
        direct = root / category / filename
        try:
            if direct.is_file():
                return direct.resolve()
        except OSError:
            continue
    for root in roots:
        try:
            if not root.is_dir():
                continue
            for candidate in root.rglob(filename):
                if candidate.is_file():
                    return candidate.resolve()
        except OSError:
            continue
    raise ModelResolutionError(f"必要なモデルが見つかりません: {category}/{filename}")
