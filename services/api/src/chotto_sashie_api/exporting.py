"""カット動画を1本に結合して書き出す（ffmpeg concat）。

プロジェクト内のカット動画を、静止画の並び順→カット順で連結し、どの環境でも再生しやすい
H.264 mp4 として出力する。ffmpeg は同梱せず、手元にあるものを探して使い、無ければ
アプリが取得したものを使う（取得は `ffmpeg_provision` が受け持つ）。
"""

from __future__ import annotations

import io
import re
import shutil
import subprocess
from pathlib import Path

from PIL import Image

from chotto_sashie_shared.ffmpeg_provision import locate_ffmpeg

from .database import Database
from .settings import settings


class ExportError(RuntimeError):
    pass


def export_root(data_dir: Path) -> Path:
    """書き出した動画を置くフォルダ（アプリの保存先の中）。"""
    return data_dir / "exports"


def project_export_path(data_dir: Path, project_id: str) -> Path:
    """作品1つぶんの書き出し先。作り直すと同じ場所へ上書きする。

    書き出し・取り出し・作品の削除が同じ場所を見るよう、ここ1か所で決める。
    """
    return export_root(data_dir) / f"{project_id}.mp4"


def find_ffprobe(ffmpeg_path: str) -> str | None:
    """ffmpeg と対になる ffprobe を返す。無ければ None。

    同梱物ではないので、まず ffmpeg と同じフォルダを見て、無ければ PATH を探す。
    """
    sibling = Path(ffmpeg_path).with_name("ffprobe" + Path(ffmpeg_path).suffix)
    if sibling.is_file():
        return str(sibling)
    return shutil.which("ffprobe")


def video_frame_rate(video: Path, ffmpeg_path: str | None) -> float | None:
    """カット動画が実際に持っているフレームレート。読めなければ None。

    書き出しはこれに合わせる。違う値で書き出すと、コマが間引かれたり水増しされたりする。
    mp4（MiniMax H3）は、結合に使う ffmpeg 本体で読む。ffprobe は利用者が用意した
    ffmpeg には付いていないことがあり、それに頼ると「カットに合わせる」が使えなくなる。
    アニメーションwebp（Wan 2.2）は ffmpeg 系では正しい値が出ない（ffprobe は16fpsの
    ものを8と返した）ので、各コマの表示時間から求める。
    """
    if video.suffix.lower() == ".webp":
        return _webp_frame_rate(video)
    if ffmpeg_path is None:
        return None
    # 出力先を指定しないので ffmpeg は失敗で終わるが、入力の情報は stderr に出し終えている。
    command = [ffmpeg_path, "-hide_banner", "-i", str(video)]
    try:
        result = subprocess.run(command, capture_output=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return parse_video_frame_rate(result.stderr.decode("utf-8", errors="replace"))


# ffmpeg が入力の映像について出す1行（"Stream #0:0...: Video: h264 ..., 24 fps, 24 tbr, ..."）。
# fps は平均の速さ、tbr は時間の刻みから推した速さ。平均を先に見る。
_VIDEO_STREAM = re.compile(r"Stream #\S+.*?: Video: (?P<detail>.*)")
_RATE = re.compile(r"(?:^|,)\s*(?P<value>\d+(?:\.\d+)?)(?P<kilo>k)?\s+(?P<unit>fps|tbr)\b")


def parse_video_frame_rate(info: str) -> float | None:
    """ffmpeg の入力情報から、最初の映像のfpsを読む。見つからなければ None。"""
    for line in info.splitlines():
        stream = _VIDEO_STREAM.search(line)
        if stream is None:
            continue
        rates = {
            match["unit"]: float(match["value"]) * (1000 if match["kilo"] else 1)
            for match in _RATE.finditer(stream["detail"])
        }
        for unit in ("fps", "tbr"):
            if rates.get(unit, 0) > 0:
                return round(rates[unit], 3)
        return None
    return None


def _webp_frame_rate(video: Path) -> float | None:
    """アニメーションwebpのfps。コマの表示時間はミリ秒の整数で入っているので、整数へ丸める
    （ComfyUI は 16fps を 62ms と書くため、そのまま割ると 16.13 になる）。"""
    durations: list[int] = []
    try:
        with Image.open(video) as image:
            # 表示時間はコマを読み込んだあとで入る（seek だけでは空のまま）。
            for index in range(getattr(image, "n_frames", 1)):
                image.seek(index)
                image.load()
                durations.append(int(image.info.get("duration", 0)))
    except (OSError, ValueError):
        return None
    total = sum(durations)
    if total <= 0:
        return None
    return float(round(len(durations) * 1000 / total))


def cut_frame_rates(videos: list[Path], ffmpeg_path: str | None) -> list[float]:
    """カット動画のfps（重複なし・小さい順）。読めなかったカットは数えない。"""
    rates = {video_frame_rate(video, ffmpeg_path) for video in videos}
    return sorted(rate for rate in rates if rate is not None)


def has_audio_stream(video: Path, ffprobe_path: str | None) -> bool:
    """動画に音声が入っているか。ffprobe が無いときは入れ物から推定する。

    このアプリが作る動画は、Wan 2.2 が無音webp、MiniMax H3 が音声付きmp4なので、
    確認できないときは拡張子で判断しても実用上ずれない。
    """
    if ffprobe_path is None:
        return video.suffix.lower() == ".mp4"
    command = [
        ffprobe_path,
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=index",
        "-of",
        "csv=p=0",
        str(video),
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, timeout=60)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return video.suffix.lower() == ".mp4"
    return bool(result.stdout.strip())


def find_ffmpeg() -> str | None:
    """使用する ffmpeg 実行ファイルのパスを返す。無ければ None。

    明示的に指定されたパスが最優先。指定が無ければ、アプリが取得したもの→PATH→
    よくある導入先の順に探す（PATHを通さずに入れる導入方法が多く、PATHだけを見ると
    「入れてあるのに見つからない」が起きる）。
    """
    if settings.ffmpeg_path:
        candidate = Path(settings.ffmpeg_path).expanduser()
        if candidate.is_file():
            return str(candidate)
        return None
    found = locate_ffmpeg()
    return str(found) if found is not None else None


def list_project_shot_videos(database: Database, project_id: str) -> list[Path]:
    """プロジェクト内の生成済みカット動画を、物語順（静止画→カット→ビート）で返す。

    分割モードで作ったカットは、ffmpegが無い環境では1本にまとまらず区間動画のまま
    残る。その場合はビートの区間動画を順に並べる（結果として同じ並びになる）。
    """
    with database.connect() as connection:
        shots = connection.execute(
            """
            SELECT sh.id AS id, sh.output_video_path AS path
            FROM shots sh
            JOIN still_images sc ON sh.still_image_id = sc.id
            WHERE sc.project_id = ?
            ORDER BY sc.ordinal, sh.ordinal
            """,
            (project_id,),
        ).fetchall()
        videos: list[Path] = []
        for shot in shots:
            if shot["path"]:
                videos.append(Path(shot["path"]))
                continue
            segments = connection.execute(
                """
                SELECT segment_video_path AS path FROM shot_beats
                WHERE shot_id = ? AND segment_video_path != ''
                ORDER BY ordinal
                """,
                (shot["id"],),
            ).fetchall()
            videos.extend(Path(row["path"]) for row in segments)
    return videos


def video_frame_size(ffmpeg_path: str, video: Path) -> tuple[int, int] | None:
    """カット動画の先頭のコマを ffmpeg で読み、画面に出る大きさ（幅, 高さ）を返す。

    ffprobe は利用者が用意した ffmpeg には付いていないことがあるので使わない。
    結合と同じ ffmpeg で実際に読むので、読めた大きさがそのまま結合時の大きさになる
    （回転の指定がある動画も、回したあとの大きさになる）。読めなければ None。
    """
    command = [
        ffmpeg_path,
        "-v",
        "error",
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-f",
        "image2pipe",
        "-c:v",
        "png",
        "-",
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, timeout=120)
        with Image.open(io.BytesIO(result.stdout)) as frame:
            return frame.size
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None


def export_frame_size(sizes: list[tuple[int, int]]) -> tuple[int, int]:
    """書き出しの画面の大きさ。いちばん画素数の多いカットに合わせる（同じなら先のカット）。

    小さいカットに合わせると、大きく作ったカットの細部を捨てることになるため。
    H.264（yuv420p）は幅と高さが偶数でないと書き出せないので、奇数なら1つ足す。
    """
    width, height = max(sizes, key=lambda size: size[0] * size[1])
    return width + width % 2, height + height % 2


# 書き出しの画質（x264 の CRF）。ComfyUI の SaveVideo はカット動画を CRF 18 で書くので、
# 書き出しも同じ細かさで作り直す。指定しないと x264 の既定（23）になり、カットより粗く
# 圧縮し直して、模様や髪の筋などの細部が失われる。
EXPORT_CRF = 18


def concat_videos(ffmpeg_path: str, inputs: list[Path], output: Path, fps: float) -> bool:
    """入力動画群を連結して1本の mp4 を書き出し、音声を残せたかを返す。

    各入力（Wanのアニメーションwebp、またはMiniMax H3の音声付きmp4）をデコードして
    concat フィルタで連結し、H.264 で再エンコードする。

    concat フィルタは全入力の大きさが同じでないと失敗する。作り方を混ぜると大きさが
    違う（Wan 2.2 は1280x720、MiniMax H3 は1344x768）ので、各カットを書き出しの大きさ
    （`export_frame_size`）へ縦横比を保って拡大・縮小し、縦横比の違う分は黒い余白で
    埋めてから繋ぐ。絵を切り落とさないため、余白を付ける側を選ぶ。

    音声は全カットが持っているときだけ残す。無音のカットが混ざると concat が
    そろわないため、その場合は映像だけを繋ぐ（音声のあるカットを作り直せば残る）。
    """
    if not inputs:
        raise ExportError("結合する動画がありません")

    sizes: list[tuple[int, int]] = []
    for number, video in enumerate(inputs, start=1):
        size = video_frame_size(ffmpeg_path, video)
        if size is None:
            raise ExportError(f"{number}本目のカット動画を読み取れませんでした")
        sizes.append(size)
    width, height = export_frame_size(sizes)

    ffprobe = find_ffprobe(ffmpeg_path)
    with_audio = all(has_audio_stream(video, ffprobe) for video in inputs)

    output.parent.mkdir(parents=True, exist_ok=True)
    command = [ffmpeg_path, "-y"]
    for video in inputs:
        command += ["-i", str(video)]
    # 大きさが同じカットでは scale も pad も何もしないので、揃っている作品の画質は変わらない。
    fit = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
    )
    fitted = ";".join(f"[{index}:v]{fit}[v{index}]" for index in range(len(inputs)))
    if with_audio:
        concat_source = "".join(f"[v{index}][{index}:a]" for index in range(len(inputs)))
        filter_complex = f"{fitted};{concat_source}concat=n={len(inputs)}:v=1:a=1[outv][outa]"
        mapping = ["-map", "[outv]", "-map", "[outa]", "-c:a", "aac", "-b:a", "192k"]
    else:
        concat_source = "".join(f"[v{index}]" for index in range(len(inputs)))
        filter_complex = f"{fitted};{concat_source}concat=n={len(inputs)}:v=1:a=0[outv]"
        mapping = ["-map", "[outv]"]
    command += [
        "-filter_complex",
        filter_complex,
        *mapping,
        "-r",
        str(fps),
        "-c:v",
        "libx264",
        "-crf",
        str(EXPORT_CRF),
        "-pix_fmt",
        "yuv420p",
        str(output),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=1800)
    except subprocess.CalledProcessError as error:
        tail = (error.stderr or b"").decode("utf-8", errors="replace").splitlines()[-5:]
        raise ExportError("ffmpegでの結合に失敗しました: " + " | ".join(tail)) from error
    except (subprocess.TimeoutExpired, OSError) as error:
        raise ExportError(f"ffmpegを実行できませんでした: {error}") from error
    return with_audio
