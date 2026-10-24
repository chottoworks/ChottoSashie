"""専用環境モードで、ComfyUI本体をアプリが用意する（利用者は準備不要）。

公式Windowsポータブル(.7z, Python・torch同梱)を共有フォルダへダウンロードして
展開する。利用者にComfyUIやPythonのパスを入力させない。

置き場所はアプリに属さない共有フォルダ（`shared_env.shared_comfyui_root()`）で、
アプリから準備・更新できる。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from itertools import takewhile
from pathlib import Path

import py7zr

from chotto_sashie_shared.download import DownloadSpec, load_download_specs
from chotto_sashie_shared.shared_env import (
    is_app_managed_location,
    is_within,
    shared_comfyui_root,
)

# ComfyUI本体の出どころ。
SOURCE_SHARED = "shared"  # アプリの共有フォルダ（そのまま更新できる）
SOURCE_EXISTING = "existing"  # 利用者が指定した既存インストールの借用（変更しない）

# 2GB級の展開でも余裕を持たせ、それでも終わらない場合は止まったと判断する。
_EXTRACT_TIMEOUT_SECONDS = 7200


class ComfyUIProvisionError(RuntimeError):
    pass


def load_comfyui_spec(manifest_path: Path) -> DownloadSpec:
    specs = load_download_specs(manifest_path)
    if not specs:
        raise ComfyUIProvisionError("ComfyUI配布物のマニフェストが空です")
    return specs[0]


def portable_root(comfyui_root: Path) -> Path:
    # 公式ポータブルは ComfyUI_windows_portable/ 直下に展開される。
    return comfyui_root / "ComfyUI_windows_portable"


def managed_comfyui_paths(comfyui_root: Path) -> tuple[Path, Path]:
    """(ComfyUIフォルダ, 同梱Python実行ファイル) を返す。"""
    portable = portable_root(comfyui_root)
    return portable / "ComfyUI", portable / "python_embeded" / "python.exe"


def is_provisioned(comfyui_root: Path) -> bool:
    comfyui_dir, python = managed_comfyui_paths(comfyui_root)
    return (comfyui_dir / "main.py").is_file() and python.is_file()


def installed_comfyui_version(comfyui_dir: Path) -> str:
    """展開済みComfyUI本体のバージョンを返す。読めなければ空文字。

    ComfyUIは ``comfyui_version.py`` に ``__version__`` を持つ。これを読むことで、
    「入っているのが古くて必要なノードが無い」状況を、利用者に見える形にできる。
    """
    version_file = comfyui_dir / "comfyui_version.py"
    try:
        text = version_file.read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r"""__version__\s*=\s*['"]([^'"]+)['"]""", text)
    return match.group(1) if match else ""


def _version_tuple(version: str) -> tuple[int, ...]:
    parts = version.strip().lstrip("vV").split(".")
    numbers: list[int] = []
    for part in parts:
        digits = "".join(takewhile(str.isdigit, part))
        if not digits:
            break
        numbers.append(int(digits))
    return tuple(numbers)


# 有料APIノード（パートナーノード）を止めるオプションが分かれた版。これより前は
# `--disable-api-nodes` が「APIノードを止め、画面をネットへつながせない」を兼ねていた。
# これ以降は `--disable-api-nodes` が `--offline` の別名になり、パートナーノードは止まらない。
PARTNER_NODE_FLAGS_SINCE = (0, 39, 0)


def offline_flags(comfyui_dir: Path) -> list[str]:
    """APIノードを止め、ネットへつながせないための起動オプションを、その版に合わせて返す。

    新しい版へ古いオプションを渡すと、パートナーノードが読み込まれたまま黙って動く。
    古い版へ新しいオプションを渡すと、知らないオプションとして起動が止まる。版が
    読めないとき（流用した既存インストールなど）は、どちらの版も受け付ける古い方を渡す。
    """
    version = _version_tuple(installed_comfyui_version(comfyui_dir))
    if version and version >= PARTNER_NODE_FLAGS_SINCE:
        return ["--disable-partner-nodes", "--offline"]
    return ["--disable-api-nodes"]


def comfyui_update_available(installed: str, target: str) -> bool:
    """入っているComfyUIが、必要なバージョンより古いか。

    どちらかが読めないときは False（分からないものを「古い」とは言わない）。
    """
    installed_parts = _version_tuple(installed)
    target_parts = _version_tuple(target)
    if not installed_parts or not target_parts:
        return False
    return installed_parts < target_parts


def resolve_comfyui_install(
    configured_comfyui: str = "", configured_python: str = ""
) -> tuple[Path, Path] | None:
    """起動に使うComfyUIを決める。

    既定は共有フォルダのComfyUI（ポータブル版）。設定で既存インストールの流用を
    明示的に指定した場合だけそれを使う（設定が空＝流用しない、という意味）。
    アプリが管理している場所を指した設定は「流用」ではないため無視し、共有物と
    して自分で探し直す。こうすると、本体を共有フォルダへ移したあとも追従できる。
    どれも使えない場合は None。
    """
    reuse_requested = bool(configured_comfyui.strip() and configured_python.strip())
    if reuse_requested:
        comfy_dir = Path(configured_comfyui).expanduser()
        python = Path(configured_python).expanduser()
        if (
            (comfy_dir / "main.py").is_file()
            and python.is_file()
            and not is_app_managed_location(comfy_dir)
        ):
            return comfy_dir, python
        # 流用指定が壊れている（または共有物を指している）場合は共有フォルダを探す。
    root = shared_comfyui_root()
    if is_provisioned(root):
        return managed_comfyui_paths(root)
    return None


def install_root_of(comfyui_dir: Path) -> Path | None:
    """そのComfyUI本体が共有フォルダの展開先の内側にあれば、その展開先を返す。"""
    root = shared_comfyui_root()
    return root if is_within(comfyui_dir, root) else None


def classify_comfyui_source(comfyui_dir: Path) -> str:
    """使用中のComfyUI本体がどこのものかを返す。

    共有フォルダのものは、アプリが準備・更新してよい持ち物。利用者が指定した既存
    インストールは借り物として区別し、アプリからは変更しない。
    """
    if is_within(comfyui_dir, shared_comfyui_root()):
        return SOURCE_SHARED
    return SOURCE_EXISTING


def long_path(path: Path) -> Path:
    """Windowsの260文字制限を回避する拡張パス表記（\\\\?\\）へ直す。

    公式ポータブルには同梱torchのライセンス類など非常に深い階層が含まれ、
    通常表記のままでは展開途中で WinError 206（名前が長すぎる）になる。
    OSの長いパス設定は利用者環境の変更になるため触らず、アプリ側の
    書き込み経路だけを拡張表記にする。
    """
    if os.name != "nt":
        return path
    absolute = Path(os.path.abspath(path))
    text = str(absolute)
    if text.startswith("\\\\?\\"):
        return absolute
    if text.startswith("\\\\"):
        # UNCパス（\\server\share\...）は \\?\UNC\server\share\... と書く。
        return Path("\\\\?\\UNC" + text[1:])
    return Path("\\\\?\\" + text)


def bundled_bsdtar() -> Path | None:
    """Windows同梱のbsdtar(libarchive)を返す。無ければ None。

    PATH上の `tar` はGit付属のGNU tarなど .7z を読めないものが来るため、
    System32 の実体だけを使う。
    """
    if os.name != "nt":
        return None
    system_root = os.environ.get("SystemRoot") or "C:\\Windows"
    candidate = Path(system_root) / "System32" / "tar.exe"
    return candidate if candidate.is_file() else None


def _extract_with_bsdtar(bsdtar: Path, archive: Path, destination: Path) -> None:
    """Windows同梱のbsdtarで展開する（既存ファイルは上書き）。"""
    try:
        completed = subprocess.run(
            [str(bsdtar), "-xf", str(archive), "-C", str(destination)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_EXTRACT_TIMEOUT_SECONDS,
            # デスクトップ版でコンソール窓が開かないようにする。
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ComfyUIProvisionError(f"ComfyUIの展開に失敗しました: {error}") from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        reason = detail[-1] if detail else f"終了コード {completed.returncode}"
        raise ComfyUIProvisionError(f"ComfyUIの展開に失敗しました: {reason}")


def extract_archive(archive: Path, destination: Path) -> None:
    """.7z を展開する。既存の展開物があっても上書き展開する。

    公式ポータブルは実行ファイル向けのBCJ2フィルタで圧縮されており、純Python実装
    (py7zr)では展開できない。Windows同梱のbsdtar(libarchive)はこれを解けるため、
    あればそちらを使い、無い環境ではpy7zrへ退避する。
    """
    destination.mkdir(parents=True, exist_ok=True)
    bsdtar = bundled_bsdtar()
    if bsdtar is not None:
        # 260文字超の階層も libarchive 側で扱えるため、展開先は通常表記で渡す。
        _extract_with_bsdtar(bsdtar, archive, destination)
        return
    try:
        with py7zr.SevenZipFile(archive, mode="r") as archive_file:
            # 深い階層でも失敗しないよう、展開先は拡張パス表記で渡す。
            archive_file.extractall(path=long_path(destination))
    except (py7zr.exceptions.ArchiveError, OSError) as error:
        raise ComfyUIProvisionError(f"ComfyUIの展開に失敗しました: {error}") from error
