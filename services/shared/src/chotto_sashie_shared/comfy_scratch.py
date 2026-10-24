"""専用ComfyUIの入力・出力フォルダに残る写しの片付け（共通の土台）。

ComfyUIは生成物をまず自分の出力フォルダへ保存し、アプリはそれを取り出して自分の保存先へ
写す。合成などで送り込んだ画像も、ComfyUIの入力フォルダに残る。どちらもアプリ側に実体が
あるので、写し終えたあとは二重の持ち物でしかない。放っておくと没にした画像まで溜まり続け、
作品を消しても消えない。

専用ComfyUIでは、この2つのフォルダはアプリの管理領域の中にあり、アプリの持ち物なので
使い終えたらここで消す。既存ComfyUIをそのまま使う場合は利用者のフォルダなので触らない
（書き込まれること自体は、そのComfyUIを使う以上の仕様として受け入れる）。
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from .deletion import remove_within


class ComfyScratch:
    """1つの接続先について、入力・出力フォルダの写しを片付ける。

    フォルダを持たないもの（既存ComfyUI向け）は何もしない。呼び出し側が接続先ごとに
    分岐を書かずに済むよう、同じ形で渡せるようにしている。
    """

    def __init__(self, input_dir: Path | None = None, output_dir: Path | None = None):
        self.input_dir = input_dir
        self.output_dir = output_dir

    @property
    def enabled(self) -> bool:
        return self.input_dir is not None and self.output_dir is not None

    def discard_outputs(self, files: Iterable[dict[str, str]]) -> None:
        """取り出し終えた出力ファイルを消す（ComfyUIの出力記述子をそのまま受け取る）。

        一時出力（type=temp）はComfyUIが自分で片付けるので対象にしない。
        """
        if self.output_dir is None:
            return
        for item in files:
            if item.get("type", "output") != "output":
                continue
            target = self.output_dir / item.get("subfolder", "") / item["filename"]
            self._remove(self.output_dir, target)

    def discard_inputs(self, names: Iterable[str]) -> None:
        """送り込んだ画像を消す。名前はLoadImageへ渡した形（`サブフォルダ/名前`）。"""
        if self.input_dir is None:
            return
        for name in names:
            if name:
                self._remove(self.input_dir, self.input_dir / name)

    def sweep(self) -> None:
        """入力・出力フォルダの中身をすべて消す。

        ComfyUIを起動する直前にだけ呼ぶ（その時点で使っている生成は無い）。アプリや
        ComfyUIが途中で落ちて、上の片付けまで届かなかった分を残さないため。
        """
        for root in (self.input_dir, self.output_dir):
            if root is None or not root.is_dir():
                continue
            # 外へ向いたリンクは、解決した先が外になるので remove_within が消さない。
            for child in root.iterdir():
                remove_within(root, child)

    @staticmethod
    def _remove(root: Path, target: Path) -> None:
        if not remove_within(root, target):
            return
        # 生成物は種類ごとの入れ子のフォルダへ保存されるので、空になった入れ物も畳む。
        try:
            resolved_root = root.resolve()
            parent = target.resolve().parent
        except OSError:
            return
        while parent != resolved_root and resolved_root in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                return
            parent = parent.parent
