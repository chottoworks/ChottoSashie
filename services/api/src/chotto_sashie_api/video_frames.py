"""生成した動画から静止フレームを取り出す（カット間の連続性用）。

あるカットの最終フレームを次のカットの起点フレームにすることで、カットを滑らかに
繋ぐ。抽出はベストエフォートで、失敗しても動画自体の保存は妨げない。

入れ物はエンジンによって変わる。Wan 2.2 はアニメーションwebp（Pillowで読める）、
MiniMax H3 は音声付きmp4（ffmpegが要る）なので、拡張子で読み方を切り替える。
"""

from __future__ import annotations

import io
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

from .exporting import find_ffmpeg


def extract_last_frame_png(video_bytes: bytes) -> bytes | None:
    """アニメーション画像（webp等）の最終フレームをPNGバイト列で返す。

    読み込みや変換に失敗した場合は None を返す（連続性が使えないだけで、動画の
    保存は継続する）。
    """
    try:
        with Image.open(io.BytesIO(video_bytes)) as image:
            frame_count = getattr(image, "n_frames", 1)
            image.seek(frame_count - 1)
            frame = image.convert("RGB")
            buffer = io.BytesIO()
            frame.save(buffer, format="PNG")
            return buffer.getvalue()
    except (OSError, ValueError, EOFError):
        return None


def _extract_last_frame_with_ffmpeg(video_path: Path) -> bytes | None:
    """mp4等の最終フレームをffmpegでPNGとして取り出す。ffmpegが無ければ None。

    末尾1秒だけをデコードし、``-update 1`` で同じファイルへ上書きし続けることで、
    最後に書かれたフレーム＝最終フレームが残る。
    """
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        return None
    with tempfile.TemporaryDirectory() as workspace:
        destination = Path(workspace) / "last.png"
        command = [
            ffmpeg,
            "-y",
            "-sseof",
            "-1",
            "-i",
            str(video_path),
            "-update",
            "1",
            "-frames:v",
            "60",
            str(destination),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, timeout=120)
            return destination.read_bytes() if destination.is_file() else None
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return None


def extract_last_frame(video_path: Path, video_bytes: bytes) -> bytes | None:
    """保存済みのカット動画から最終フレームをPNGで取り出す。失敗時は None。"""
    if video_path.suffix.lower() == ".mp4":
        return _extract_last_frame_with_ffmpeg(video_path)
    return extract_last_frame_png(video_bytes)
