"""アンインストールで「アプリデータも削除」を選んだときに、作品の保存先を片付ける。

インストーラ（Tauri の NSIS）が標準で消すのは、識別子の名前のフォルダ
（`%LOCALAPPDATA%\\jp.chottoworks.sashie` など）だけで、このアプリが作品を置く保存先は
知らない。保存先は利用者が変えられるので、場所は実行ファイル側（`paths.json` を読む側）が
決めてここへ渡す。

消すのは**このアプリが保存先の直下に作るものだけ**（`APP_ENTRIES`）。保存先は空の
フォルダか、このアプリのデータが入ったフォルダしか選べないが、あとから利用者が別の
ファイルを置くことはありうる。それには触れず、空になったときだけフォルダごと消す。

ComfyUI本体とモデルの共有フォルダは消さない。数十GBあり、別のインストールや開発環境と
共用していることがあるため。

    python -m chotto_sashie_shared.data_removal --data-dir <保存先>

最後に結果を1行のJSONで出す（呼び出し側が読む）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .comfyui_provision import long_path
from .data_location import DATABASE_RELATIVE

# このアプリが保存先の直下に作るもの。保存先へ新しい種類のものを置くときは、ここにも
# 足す（足し忘れると、アンインストールで消えずに残る。試験で突き合わせている）。
APP_ENTRIES: tuple[str, ...] = (
    "database",
    "assets",
    "outputs",
    "keyframes",
    "videos",
    "shot-last-frames",
    "shot-frames",
    "shot-segments",
    "exports",
    "managed",
)


@dataclass
class RemovalResult:
    data_dir: str
    # このアプリのデータが入っていたか（入っていなければ何も消さない）。
    found: bool = False
    removed: list[str] = field(default_factory=list)
    # 消せなかったもの（使用中など）。
    failed: list[str] = field(default_factory=list)
    # 利用者が置いたなどで残したもの。
    kept: list[str] = field(default_factory=list)
    # 保存先のフォルダごと消せたか。
    removed_folder: bool = False


def remove_app_data(data_dir: Path) -> RemovalResult:
    """保存先から、このアプリが作ったものを消す。"""
    result = RemovalResult(data_dir=str(data_dir))
    # DBが無いフォルダは、このアプリの保存先と確かめられない。指し間違いで別の
    # フォルダの中身を消さないよう、何もしない。
    if not (data_dir / DATABASE_RELATIVE).is_file():
        return result
    result.found = True

    for name in APP_ENTRIES:
        target = data_dir / name
        if not target.exists() and not target.is_symlink():
            continue
        try:
            if target.is_dir() and not target.is_symlink():
                # 専用ComfyUIのキャッシュなどは階層が深く、260文字を超えることがある。
                shutil.rmtree(long_path(target))
            else:
                target.unlink()
        except OSError:
            result.failed.append(name)
            continue
        result.removed.append(name)

    try:
        result.kept = sorted(child.name for child in data_dir.iterdir())
    except OSError:
        return result
    if not result.kept:
        try:
            data_dir.rmdir()
            result.removed_folder = True
        except OSError:
            pass
    return result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="作品の保存先から、このアプリが作ったものを消す")
    parser.add_argument("--data-dir", required=True)
    arguments = parser.parse_args(argv)
    result = remove_app_data(Path(arguments.data_dir).expanduser())
    print(json.dumps(asdict(result), ensure_ascii=False), flush=True)
    return 1 if result.failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
