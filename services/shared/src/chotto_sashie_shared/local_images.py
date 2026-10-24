"""生成画像のアプリ管理領域への保存。

人物候補は、これまでComfyUIのoutputフォルダから毎回取得していた。
そのため生成エンジンを入れ替えたり出力先が変わったりすると、記録は残っているのに
画像だけ表示できなくなる。ここではアプリ管理領域へ実体を持たせ、次の順で解決する。

1. アプリ管理領域の控え（あればそれを返す）
2. ComfyUIから取得し、その場で控えを作る
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

# 保存先の種別。アプリ管理領域 outputs/ 配下に種別ごとのフォルダを作る。
CANDIDATE_IMAGES = "candidates"


def local_image_path(data_dir: Path, kind: str, record_id: str) -> Path:
    """記録IDから控えの保存先を決める（IDはUUIDなので経路の細工は起きない）。"""
    safe_id = record_id.replace("/", "_").replace("\\", "_")
    return data_dir / "outputs" / kind / f"{safe_id}.png"


def read_local_image(data_dir: Path, kind: str, record_id: str) -> bytes | None:
    """控えがあれば読み出す。無ければ None。"""
    path = local_image_path(data_dir, kind, record_id)
    try:
        if path.is_file():
            return path.read_bytes()
    except OSError:
        return None
    return None


def write_local_image(data_dir: Path, kind: str, record_id: str, content: bytes) -> Path:
    """控えを原子的に保存する（保存に失敗しても表示は続けられるよう例外は投げない）。"""
    path = local_image_path(data_dir, kind, record_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(content)
            temporary.replace(path)
        finally:
            if temporary.exists():
                temporary.unlink()
    except OSError:
        return path
    return path


def delete_local_image(data_dir: Path, kind: str, record_id: str) -> None:
    """記録の画像が差し替わったとき、古い控えを捨てる（再生成で使う）。"""
    try:
        local_image_path(data_dir, kind, record_id).unlink(missing_ok=True)
    except OSError:
        return
