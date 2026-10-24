"""削除の後始末（共通の土台）。

利用者が指定した既存ComfyUIやモデル置き場は読み取り専用で扱う約束のため、
削除はアプリ管理領域の内側であることを必ず確かめてから行う。
"""

from __future__ import annotations

import shutil
from pathlib import Path


def remove_within(root: Path, target: Path) -> bool:
    """`root` の内側にあるときだけ `target`（ファイルまたはフォルダ）を削除する。

    `root` そのものや、`root` の外に出るパスは削除しない。消えた場合だけ True を返す。
    削除できなくても記録側の削除は続けたいので、例外は投げない。
    """
    try:
        resolved_root = root.resolve()
        resolved = target.resolve()
    except OSError:
        return False
    if resolved == resolved_root or resolved_root not in resolved.parents:
        return False
    try:
        if resolved.is_dir():
            shutil.rmtree(resolved)
            return True
        if resolved.exists():
            resolved.unlink()
            return True
    except OSError:
        return False
    return False
