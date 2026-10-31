"""カット動画を1本に結合して書き出す（ffmpeg concat）。

プロジェクト内のカット動画を、静止画の並び順→カット順で連結し、どの環境でも再生しやすい
H.264 mp4 として出力する。シーンの切り替わりには、作品とシーンの設定に従って暗転を
かけられる。ffmpeg は同梱せず、手元にあるものを探して使い、無ければアプリが取得した
ものを使う（取得は `ffmpeg_provision` が受け持つ）。
"""

from __future__ import annotations

import io
import re
import shutil
import subprocess
from dataclasses import dataclass
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


def _webp_frame_durations(video: Path) -> list[int] | None:
    """アニメーションwebpの各コマの表示時間（ミリ秒）。読めなければ None。"""
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
    return durations


def _webp_frame_rate(video: Path) -> float | None:
    """アニメーションwebpのfps。コマの表示時間はミリ秒の整数で入っているので、整数へ丸める
    （ComfyUI は 16fps を 62ms と書くため、そのまま割ると 16.13 になる）。"""
    durations = _webp_frame_durations(video)
    if not durations or sum(durations) <= 0:
        return None
    return float(round(len(durations) * 1000 / sum(durations)))


def video_duration(video: Path, ffmpeg_path: str) -> float | None:
    """カット動画の長さ（秒）。読めなければ None。暗転を始める時刻を決めるのに使う。

    アニメーションwebp（Wan 2.2）は ffmpeg が長さを出さない（Duration: N/A）。ffmpeg は
    各コマを書かれた表示時間どおりに並べて読むので、その合計が結合のときの長さになる。
    mp4（MiniMax H3）は、結合に使う ffmpeg 本体が出す長さを読む。
    """
    if video.suffix.lower() == ".webp":
        durations = _webp_frame_durations(video)
        total = sum(durations or [])
        return total / 1000 if total > 0 else None
    # 出力先を指定しないので ffmpeg は失敗で終わるが、入力の情報は stderr に出し終えている。
    command = [ffmpeg_path, "-hide_banner", "-i", str(video)]
    try:
        result = subprocess.run(command, capture_output=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return parse_duration(result.stderr.decode("utf-8", errors="replace"))


# ffmpeg が入力について出す1行（"  Duration: 00:00:05.17, start: 0.000000, bitrate: ..."）。
_DURATION = re.compile(r"Duration:\s*(?P<h>\d+):(?P<m>\d{2}):(?P<s>\d{2}(?:\.\d+)?)")


def parse_duration(info: str) -> float | None:
    """ffmpeg の入力情報から長さ（秒）を読む。出ていなければ（N/A など）None。"""
    match = _DURATION.search(info)
    if match is None:
        return None
    seconds = int(match["h"]) * 3600 + int(match["m"]) * 60 + float(match["s"])
    return seconds if seconds > 0 else None


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


@dataclass(frozen=True)
class ExportScene:
    """書き出しに並べる1シーン（静止画1枚）。動画と、前のシーンからの切り替えの指定。"""

    videos: list[Path]
    # project=作品の設定に従う / none / fade。秒数が None なら作品の秒数。
    transition: str = "project"
    transition_seconds: float | None = None


def list_project_scenes(database: Database, project_id: str) -> list[ExportScene]:
    """プロジェクトのシーンを物語順に返す。各シーンの動画は、カット→ビートの順。

    分割モードで作ったカットは、ffmpegが無い環境では1本にまとまらず区間動画のまま
    残る。その場合はビートの区間動画を順に並べる（結果として同じ並びになる）。
    動画が1本も無いシーンも返す（飛ばすかどうかは使う側が決める）。
    """
    with database.connect() as connection:
        still_images = connection.execute(
            """
            SELECT id, scene_transition, scene_transition_seconds FROM still_images
            WHERE project_id = ?
            ORDER BY ordinal
            """,
            (project_id,),
        ).fetchall()
        scenes: list[ExportScene] = []
        for still_image in still_images:
            shots = connection.execute(
                """
                SELECT id, output_video_path AS path FROM shots
                WHERE still_image_id = ?
                ORDER BY ordinal
                """,
                (still_image["id"],),
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
            scenes.append(
                ExportScene(
                    videos=videos,
                    transition=still_image["scene_transition"],
                    transition_seconds=still_image["scene_transition_seconds"],
                )
            )
    return scenes


def list_project_shot_videos(database: Database, project_id: str) -> list[Path]:
    """プロジェクト内の生成済みカット動画を、物語順（静止画→カット→ビート）で返す。"""
    return [video for scene in list_project_scenes(database, project_id) for video in scene.videos]


@dataclass(frozen=True)
class ClipFade:
    """カット動画1本にかける暗転。始まりを黒から明るくする秒数と、終わりを黒へ落とす秒数。"""

    fade_in: float = 0.0
    fade_out: float = 0.0


def plan_scene_fades(
    scenes: list[ExportScene],
    *,
    transition: str,
    seconds: float,
    edges: bool,
) -> list[ClipFade]:
    """シーンの切り替わりにかける暗転を、書き出しに並べるカット動画1本ずつへ割り当てる。

    返す並びは、各シーンの動画を順につなげたものと同じ。暗転はシーンの切り替わりだけに
    かけ、同じシーンの中のカット（前のカットの最後のコマから続けて作ったもの）と、
    分割の区間のつなぎ目には入れない。

    切り替わりの効果は後ろのシーンの指定で決まり、`project` なら作品の設定（`transition`
    と `seconds`）に従う。暗転なら前のシーンの最後の動画を黒へ落とし、後ろのシーンの
    最初の動画を黒から明るくする。動画の無いシーンは飛ばし、その指定も使わない。
    `edges` が真なら、動画全体の始まりと終わりにも作品の秒数で暗転をかける。
    """
    filled = [scene for scene in scenes if scene.videos]
    fades = [[ClipFade() for _ in scene.videos] for scene in filled]
    for index in range(1, len(filled)):
        scene = filled[index]
        kind = transition if scene.transition == "project" else scene.transition
        if kind != "fade":
            continue
        length = scene.transition_seconds if scene.transition_seconds is not None else seconds
        before = fades[index - 1]
        before[-1] = ClipFade(fade_in=before[-1].fade_in, fade_out=length)
        after = fades[index]
        after[0] = ClipFade(fade_in=length, fade_out=after[0].fade_out)
    if edges and fades:
        first, last = fades[0], fades[-1]
        first[0] = ClipFade(fade_in=seconds, fade_out=first[0].fade_out)
        last[-1] = ClipFade(fade_in=last[-1].fade_in, fade_out=seconds)
    return [fade for scene_fades in fades for fade in scene_fades]


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


def _fade_filters(name: str, fade: ClipFade, duration: float | None) -> list[str]:
    """暗転のフィルタ（映像は `fade`、音声は `afade`）。暗転が無ければ空。

    暗転の秒数は動画の長さを超えない（短いカットでは、その長さいっぱいで暗転する）。
    """
    filters: list[str] = []
    if fade.fade_in > 0 and duration is not None:
        filters.append(f"{name}=t=in:st=0:d={min(fade.fade_in, duration):.3f}")
    if fade.fade_out > 0 and duration is not None:
        length = min(fade.fade_out, duration)
        filters.append(f"{name}=t=out:st={duration - length:.3f}:d={length:.3f}")
    return filters


def concat_videos(
    ffmpeg_path: str,
    inputs: list[Path],
    output: Path,
    fps: float,
    fades: list[ClipFade] | None = None,
) -> bool:
    """入力動画群を連結して1本の mp4 を書き出し、音声を残せたかを返す。

    各入力（Wanのアニメーションwebp、またはMiniMax H3の音声付きmp4）をデコードして
    concat フィルタで連結し、H.264 で再エンコードする。

    concat フィルタは全入力の大きさが同じでないと失敗する。作り方を混ぜると大きさが
    違う（Wan 2.2 は1280x720、MiniMax H3 は1344x768）ので、各カットを書き出しの大きさ
    （`export_frame_size`）へ縦横比を保って拡大・縮小し、縦横比の違う分は黒い余白で
    埋めてから繋ぐ。絵を切り落とさないため、余白を付ける側を選ぶ。

    音声は全カットが持っているときだけ残す。無音のカットが混ざると concat が
    そろわないため、その場合は映像だけを繋ぐ（音声のあるカットを作り直せば残る）。

    `fades` は入力1本ずつの暗転（`plan_scene_fades`）。黒へ落とす時刻を決めるために
    その動画の長さを読み、音声を残すときは音も同じ時刻・秒数で消して戻す。
    """
    if not inputs:
        raise ExportError("結合する動画がありません")
    fades = fades or [ClipFade() for _ in inputs]
    if len(fades) != len(inputs):
        raise ExportError("暗転の指定と動画の数が合いません")

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
    durations: list[float | None] = []
    for number, (video, fade) in enumerate(zip(inputs, fades, strict=True), start=1):
        duration = None
        if fade.fade_in > 0 or fade.fade_out > 0:
            duration = video_duration(video, ffmpeg_path)
            if duration is None:
                raise ExportError(f"{number}本目のカット動画の長さを読み取れませんでした")
        durations.append(duration)
    chains = [
        f"[{index}:v]" + ",".join([fit, *_fade_filters("fade", fade, duration)]) + f"[v{index}]"
        for index, (fade, duration) in enumerate(zip(fades, durations, strict=True))
    ]
    audio_sources: list[str] = []
    if with_audio:
        for index, (fade, duration) in enumerate(zip(fades, durations, strict=True)):
            audio_fades = _fade_filters("afade", fade, duration)
            if audio_fades:
                chains.append(f"[{index}:a]" + ",".join(audio_fades) + f"[a{index}]")
                audio_sources.append(f"[a{index}]")
            else:
                audio_sources.append(f"[{index}:a]")
    fitted = ";".join(chains)
    if with_audio:
        concat_source = "".join(f"[v{index}]{audio}" for index, audio in enumerate(audio_sources))
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
