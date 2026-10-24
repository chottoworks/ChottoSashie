"""書き出しに使うffmpegを、アプリが用意する。

書き出し（カット動画を1本に結合）と、mp4から最終フレームを取り出す予備の経路は
ffmpegに頼っている。無いと書き出しがまったくできず、「導入してPATHを通して
ください」と伝えるだけでは利用者には手の打ちようがない。ComfyUI本体やモデルと
同じように、アプリが取得して共有フォルダへ置けるようにする。

    %LOCALAPPDATA%\\ChottoSashieShared\\
        tools\\ffmpeg\\bin\\ffmpeg.exe    アプリが取得したffmpeg
        tools\\ffmpeg\\LICENSE.txt        配布物に付いてくる利用条件

置き場所を共有フォルダにするのは、ComfyUIやモデルと同じ理由（アプリのデータ領域に
置くと、保存先を移したときに一緒に動いてしまう）。取得物はGPLのビルドで、アプリは
これを別プロセスとして呼ぶだけなので、同梱も再配布もしない。利用条件のファイルは
実行ファイルと一緒に残す。

すでに手元にあるものは使う。取得を勧める前に、PATHとよくある導入先を探す。
"""

from __future__ import annotations

import os
import shutil
import zipfile
from pathlib import Path

from .download import DownloadSpec, load_download_specs
from .shared_env import shared_root


class FfmpegProvisionError(RuntimeError):
    pass


# 取得物から取り出すもの。ffplay は再生用で、このアプリでは使わないので展開しない
# （静的ビルドでは1つ100MB近くあり、置いても場所を取るだけになる）。
_WANTED_EXECUTABLES = ("ffmpeg.exe", "ffprobe.exe")
# 配布物に付いてくる利用条件。実行ファイルと一緒に残す。
_WANTED_DOCUMENTS = ("LICENSE.txt", "LICENSE", "COPYING.txt", "README.txt")


def load_ffmpeg_spec(manifest_path: Path) -> DownloadSpec:
    specs = load_download_specs(manifest_path)
    if not specs:
        raise FfmpegProvisionError("ffmpeg配布物のマニフェストが空です")
    return specs[0]


def ffmpeg_tools_root() -> Path:
    """アプリが取得したffmpegを置く場所。"""
    return shared_root() / "tools" / "ffmpeg"


def managed_ffmpeg_path(tools_root: Path | None = None) -> Path:
    return (tools_root or ffmpeg_tools_root()) / "bin" / _executable_name("ffmpeg")


def is_provisioned(tools_root: Path | None = None) -> bool:
    return managed_ffmpeg_path(tools_root).is_file()


def _executable_name(stem: str) -> str:
    return f"{stem}.exe" if os.name == "nt" else stem


def common_install_locations() -> list[Path]:
    """利用者が自分で入れた場合に置かれがちな場所。

    PATHを通さずに入れる導入方法（インストーラ、書庫を展開しただけ）が多いため、
    PATHだけを見ていると「入れてあるのに見つからない」が起きる。
    """
    if os.name != "nt":
        return [Path("/usr/local/bin/ffmpeg"), Path("/usr/bin/ffmpeg"), Path("/opt/homebrew/bin/ffmpeg")]
    candidates: list[Path] = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        # winget は実体への転送用の小さな実行ファイルをここに置く。
        candidates.append(Path(local_app_data) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe")
    user_profile = os.environ.get("USERPROFILE")
    if user_profile:
        candidates.append(Path(user_profile) / "scoop" / "shims" / "ffmpeg.exe")
    program_data = os.environ.get("ProgramData") or "C:\\ProgramData"
    candidates.append(Path(program_data) / "chocolatey" / "bin" / "ffmpeg.exe")
    for program_files in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if program_files:
            candidates.append(Path(program_files) / "ffmpeg" / "bin" / "ffmpeg.exe")
    candidates.append(Path("C:\\ffmpeg\\bin\\ffmpeg.exe"))
    return candidates


def locate_ffmpeg(tools_root: Path | None = None) -> Path | None:
    """使えるffmpegを探す。アプリが取得したもの→PATH→よくある導入先の順。

    アプリが取得したものを先に見るのは、検証済みで結合に使う符号化器（libx264）が
    確実に入っているため。手元のものが先に見つかれば、取得は勧めない。
    """
    managed = managed_ffmpeg_path(tools_root)
    if managed.is_file():
        return managed
    on_path = shutil.which("ffmpeg")
    if on_path:
        return Path(on_path)
    for candidate in common_install_locations():
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _target_for(member: str) -> str | None:
    """書庫の中身のうち、取り出すものと置き先の名前を決める。

    配布物の中では版名のフォルダが1階層挟まる（`ffmpeg-n9.0.1-.../bin/ffmpeg.exe`）。
    その名前は版で変わるので、末尾の形だけで判断して置き先はこちらで組み立てる
    （書庫の中の道をそのまま使わないので、外へ抜ける細工も効かない）。
    """
    path = member.replace("\\", "/")
    if path.endswith("/"):
        return None
    parts = path.split("/")
    name = parts[-1]
    if len(parts) >= 2 and parts[-2] == "bin" and name in _WANTED_EXECUTABLES:
        return f"bin/{name}"
    if name in _WANTED_DOCUMENTS and len(parts) <= 2:
        return name
    return None


def extract_ffmpeg(archive: Path, tools_root: Path) -> Path:
    """取得した書庫から実行ファイルと利用条件を取り出し、ffmpegの道を返す。"""
    tools_root.mkdir(parents=True, exist_ok=True)
    found: set[str] = set()
    try:
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.namelist():
                relative = _target_for(member)
                if relative is None:
                    continue
                destination = tools_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as source, destination.open("wb") as target:
                    shutil.copyfileobj(source, target)
                found.add(relative)
    except (zipfile.BadZipFile, OSError) as error:
        raise FfmpegProvisionError(f"取得した書庫を展開できませんでした: {error}") from error

    executable = managed_ffmpeg_path(tools_root)
    if f"bin/{_executable_name('ffmpeg')}" not in found or not executable.is_file():
        raise FfmpegProvisionError("展開後にffmpegの実行ファイルを確認できませんでした")
    if os.name != "nt":
        for name in _WANTED_EXECUTABLES:
            target = tools_root / "bin" / name
            if target.is_file():
                target.chmod(0o755)
    return executable
