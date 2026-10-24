"""試験が実データへ触れないよう、APIを取り込む前に保存先を隔離する。

各試験は個別に保存先を差し替えるが、差し替え漏れが一箇所でもあると
`%LOCALAPPDATA%` の実データベースや実ComfyUIへ書き込んでしまう。
取り込み時に確定する既定値そのものを一時フォルダへ向け、最後の砦とする。

隔離そのものを取り違えることもあるため、試験のたびに行き先を確かめる
（`_reject_real_locations`）。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_isolated_data_dir = Path(tempfile.mkdtemp(prefix="chotto-sashie-tests-"))
os.environ["CHOTTO_SASHIE_DATA_DIR"] = str(_isolated_data_dir)

# 共有フォルダ（ComfyUI本体とモデルの置き場）も実データへ触れないよう隔離する。
_isolated_shared_dir = Path(tempfile.mkdtemp(prefix="chotto-sashie-shared-tests-"))
os.environ["CHOTTO_SASHIE_SHARED_DIR"] = str(_isolated_shared_dir)

import pytest  # noqa: E402 - 保存先の隔離を先に行う

from chotto_sashie_shared.shared_env import (  # noqa: E402
    app_data_root,
    default_app_data_root,
    default_shared_root,
    shared_root,
)


def _real_data_roots() -> list[Path]:
    """利用者の実データが置かれる場所（ここへ向いていたら隔離できていない）。

    `%LOCALAPPDATA%` 全体ではなく、アプリが既定で使うフォルダだけを見る。
    Windows の一時フォルダは既定で `%LOCALAPPDATA%\\Temp` にあり、全体を見ると
    一時フォルダに作った試験用の保存先まで実データとみなしてしまう。
    """
    roots: list[Path] = []
    for root in (default_app_data_root(), default_shared_root()):
        try:
            roots.append(root.resolve())
        except OSError:
            continue
    return roots


def _reject_real_locations() -> None:
    """保存先が実データの場所を指していたら、書き込む前に止める。

    環境変数の名前を変えたのに conftest を直し忘れる、といった取り違えが起きると、
    差し替えたつもりの保存先が既定へ戻る。実際に一度これが起き、共有フォルダの実パス
    へ偽の一式（0バイトの main.py と python.exe、2バイトの配布物）が書き出された。
    そのとき本物のComfyUIが同じ場所にあれば、0バイトで上書きしていた。
    """
    locations = {"共有フォルダ": shared_root(), "保存先": app_data_root()}
    for root in _real_data_roots():
        for label, path in locations.items():
            try:
                inside = path.resolve().is_relative_to(root)
            except OSError:
                continue
            if inside:
                raise AssertionError(
                    f"試験の{label}が実データの場所を指しています: {path}。"
                    "conftest.py の隔離（環境変数名）を確かめてください"
                )


@pytest.fixture(autouse=True)
def isolated_shared_root(tmp_path, monkeypatch):
    """試験ごとに共有フォルダを分ける。

    共有フォルダは既定のままだと、前の試験が用意したComfyUIを次の試験が
    「すでに準備済み」と判断してしまう。
    """
    monkeypatch.setenv("CHOTTO_SASHIE_SHARED_DIR", str(tmp_path / "shared-env"))
    _reject_real_locations()
