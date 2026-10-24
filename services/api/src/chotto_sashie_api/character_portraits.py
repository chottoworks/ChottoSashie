"""代表写真（一覧に出す顔写真）の保存と、選び直しの候補づくり。

合成に使う立ち絵は全身なので、そのままサムネイルにすると胴体のあたりだけが写り、
誰なのか分からない。そこで表示専用の1枚を別に持ち、利用者が好みのものへ選び直せる
ようにする。立ち絵にも合成にも一切触れないため、代表写真を変えても生成結果は
変わらない。
"""

from __future__ import annotations

from pathlib import Path

from .image_files import ALLOWED_IMAGE_CONTENT_TYPES, save_uploaded_image

# 候補の指定子。利用者に見せるのは見出しだけで、この文字列はUIとAPIの取り決め。
# 既定へ戻す指定（以後は既定の立ち絵が一覧に出る）。
DEFAULT_OPTION_ID = "default"
CANDIDATE_OPTION_PREFIX = "candidate-"
FIGURE_OPTION_PREFIX = "figure-"

_IMAGE_SUFFIXES = tuple(ALLOWED_IMAGE_CONTENT_TYPES.values())


def portraits_dir(data_dir: Path) -> Path:
    """代表写真の保存先。立ち絵とは別の場所に置く。"""
    return data_dir / "assets" / "character-portraits"


def _remove_stale(directory: Path, stem: str, keep: Path) -> None:
    """同じ用途で拡張子だけ違う古いファイルを消す（表示が入れ替わらない事故を防ぐ）。"""
    for suffix in _IMAGE_SUFFIXES:
        candidate = directory / f"{stem}{suffix}"
        if candidate != keep:
            candidate.unlink(missing_ok=True)


def save_portrait(portrait_root: Path, character_id: str, data: bytes, extension: str) -> Path:
    """代表写真の実体を保存し、そのパスを返す。"""
    destination = save_uploaded_image(portrait_root, character_id, data, extension)
    _remove_stale(portrait_root, character_id, destination)
    return destination


def parse_candidate_option_id(option_id: str) -> str | None:
    """`candidate-<id>` から人物候補のIDを取り出す。合わなければ None。"""
    if not option_id.startswith(CANDIDATE_OPTION_PREFIX):
        return None
    return option_id[len(CANDIDATE_OPTION_PREFIX) :] or None


def parse_figure_option_id(option_id: str) -> str | None:
    """`figure-<id>` から立ち絵のIDを取り出す。合わなければ None。"""
    if not option_id.startswith(FIGURE_OPTION_PREFIX):
        return None
    return option_id[len(FIGURE_OPTION_PREFIX) :] or None


def delete_portrait(portrait_root: Path, character_id: str) -> None:
    """代表写真の実体を消す（既定へ戻したとき、人物を消したとき）。"""
    for suffix in _IMAGE_SUFFIXES:
        (portrait_root / f"{character_id}{suffix}").unlink(missing_ok=True)
