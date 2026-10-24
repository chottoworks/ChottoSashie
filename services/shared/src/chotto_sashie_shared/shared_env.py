r"""ComfyUI本体とモデルを置く共有領域と、アプリのデータ領域。

ComfyUI本体やモデルは大きく、アプリのデータ領域に置くと保存先を移したときに
一緒に動いてしまう。そこでアプリのデータ領域とは別の共有フォルダを1つ決め、
そこを既定の置き場所にする。

    %LOCALAPPDATA%\ChottoSashieShared\
        comfyui\           ComfyUI本体（公式ポータブル）
        models\            アプリが取得したモデル

環境変数 ``CHOTTO_SASHIE_SHARED_DIR`` で置き場所を変えられる（試験と、別ドライブへ
逃がしたい利用者向け）。呼ばれるたびに環境変数を読むので、取り込み後でも差し替わる。
"""

from __future__ import annotations

import os
from pathlib import Path

SHARED_DIR_ENV = "CHOTTO_SASHIE_SHARED_DIR"
DATA_DIR_ENV = "CHOTTO_SASHIE_DATA_DIR"
RESOURCES_DIR_ENV = "CHOTTO_SASHIE_RESOURCES_DIR"
_SHARED_FOLDER = "ChottoSashieShared"
_DATA_FOLDER = "ChottoSashie"
# 保存先のフォルダの既定の名前。ほかのファイルが入ったフォルダを保存先に選ばれたときも、
# その中にこの名前で作って使う。
DATA_FOLDER_NAME = _DATA_FOLDER

# アプリに付いてくる読み取り専用のファイル群。この2つが揃っている場所を資源の場所とみなす。
# 片方でも欠けていたら、そこは資源の場所ではない（別の似た構成を掴まないための目印）。
_RESOURCE_FOLDERS = ("workflows", "model-manifests")
# インストールした状態で資源が入る場所（このパッケージの隣）。
_INSTALLED_RESOURCES_FOLDER = "_resources"


class ResourcesNotFoundError(RuntimeError):
    """アプリに同梱されているはずのファイル群が見つからない。"""


def _has_all_resource_folders(root: Path) -> bool:
    try:
        return all((root / name).is_dir() for name in _RESOURCE_FOLDERS)
    except OSError:
        return False


def app_resources_root() -> Path:
    r"""ワークフロー・マニフェスト・同梱物が入っている場所。

    アプリと一緒に配られる読み取り専用のファイルで、利用者の作ったものは入らない。
    動かし方が3通りあり、それぞれで場所が違うため、次の順に探す。

    1. 環境変数 ``CHOTTO_SASHIE_RESOURCES_DIR``（配布版が自分の場所を教えるときと、試験用）
    2. このパッケージの隣の ``_resources``（``pip install`` で入れた状態）
    3. このファイルから親をたどって、4つのフォルダが揃っている場所（リポジトリ内で動かす開発時）

    どれにも当たらなければ止める。ここで当てずっぽうの場所を返すと、
    「起動はするが生成だけが失敗する」という分かりにくい壊れ方になる。
    """
    override = os.environ.get(RESOURCES_DIR_ENV)
    if override and override.strip() not in {"", "."}:
        return Path(override).expanduser().resolve()

    here = Path(__file__).resolve()
    installed = here.parent / _INSTALLED_RESOURCES_FOLDER
    if _has_all_resource_folders(installed):
        return installed

    for candidate in here.parents:
        if _has_all_resource_folders(candidate):
            return candidate

    raise ResourcesNotFoundError(
        "アプリに同梱されているはずのファイル（"
        + "、".join(_RESOURCE_FOLDERS)
        + f"）が見つかりません。環境変数 {RESOURCES_DIR_ENV} で場所を指定できます"
    )


def app_data_root() -> Path:
    """アプリのデータ領域（DBと生成物の保存先）。"""
    override = os.environ.get(DATA_DIR_ENV)
    if override and override.strip() not in {"", "."}:
        return Path(override).expanduser().resolve()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / _DATA_FOLDER
    return Path.cwd() / f".{_DATA_FOLDER.lower()}"


def shared_root() -> Path:
    """ComfyUI本体とモデルの共有フォルダ。"""
    override = os.environ.get(SHARED_DIR_ENV)
    if override and override.strip() not in {"", "."}:
        return Path(override).expanduser().resolve()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / _SHARED_FOLDER
    return Path.cwd() / f".{_SHARED_FOLDER.lower()}"


def shared_comfyui_root() -> Path:
    """共有のComfyUI本体を展開する場所。"""
    return shared_root() / "comfyui"


def shared_models_root() -> Path:
    """共有のモデル置き場（ComfyUIの models フォルダと同じ構成）。"""
    return shared_root() / "models"


def is_within(path: Path, root: Path) -> bool:
    """path が root の内側にあるか（解決できないときは False）。"""
    try:
        return path.expanduser().resolve().is_relative_to(root.expanduser().resolve())
    except OSError:
        return False


def is_app_managed_location(path: Path) -> bool:
    """アプリが管理している場所（共有フォルダまたはデータ領域）かどうか。

    利用者が指定した既存インストールと区別するために使う。ここに該当する場所は
    「借用」ではなくアプリの持ち物なので、そのまま更新してよい。
    """
    return any(is_within(path, root) for root in (shared_root(), app_data_root()))


def unique_existing_dirs(paths: list[Path]) -> list[Path]:
    """実在するフォルダだけを、順序を保ったまま重複なく返す。"""
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        try:
            resolved = path.expanduser().resolve()
            if not resolved.is_dir():
                continue
        except OSError:
            continue
        key = str(resolved).casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(resolved)
    return unique
