import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from chotto_sashie_shared.jobs import JobRepository
from chotto_sashie_api import exporting, main
from chotto_sashie_api.database import Database
from chotto_sashie_api.exporting import (
    ClipFade,
    ExportError,
    ExportScene,
    concat_videos,
    cut_frame_rates,
    export_frame_size,
    list_project_shot_videos,
    plan_scene_fades,
    video_frame_rate,
    video_frame_size,
)
from chotto_sashie_api.still_images import StillImageRepository
from chotto_sashie_api.shots import ShotRepository


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    database = Database(tmp_path / "video-api.sqlite3")
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(main, "jobs", JobRepository(database))
    monkeypatch.setattr(main.settings, "data_dir", tmp_path / "app-data")
    with TestClient(main.app) as test_client:
        yield test_client


def _webp(path: Path, fps: int, frames: int = 5) -> Path:
    """ComfyUI の SaveAnimatedWEBP と同じく、コマの表示時間をミリ秒の整数で書く。"""
    images = [Image.new("RGB", (8, 8), (index * 40, 0, 0)) for index in range(frames)]
    images[0].save(path, save_all=True, append_images=images[1:], duration=int(1000 / fps), loop=0)
    return path


def _project_with_shots(client: TestClient, tmp_path: Path, fps: int = 16) -> str:
    """2シーン×各1カット、動画あり（Wan と同じ形のwebp）。物語順の確認用に別々の動画を置く。"""
    project_id = client.post("/api/v1/projects", json={"name": "書き出し"}).json()["id"]
    still_images = StillImageRepository(main.database)
    shots = ShotRepository(main.database)
    from chotto_sashie_api.schemas import StillImageCreate, ShotCreate

    order = []
    for still_image_index in range(2):
        still_image = still_images.create(project_id, StillImageCreate(name=f"S{still_image_index}"))
        shot = shots.create(still_image.id, ShotCreate())
        video = _webp(tmp_path / f"vid-{still_image_index}.webp", fps, frames=3 + still_image_index)
        shots.set_output_video(shot.id, video)
        order.append(str(video))
    return project_id


def test_gather_orders_by_still_image_then_shot(client: TestClient, tmp_path: Path) -> None:
    project_id = _project_with_shots(client, tmp_path)
    videos = list_project_shot_videos(main.database, project_id)
    assert [p.name for p in videos] == ["vid-0.webp", "vid-1.webp"]


def test_gather_follows_the_reordered_still_images(client: TestClient, tmp_path: Path) -> None:
    """静止画を並べ替えると、書き出すときのシーンの順もそれに従う。"""
    project_id = _project_with_shots(client, tmp_path)
    listed = client.get(f"/api/v1/projects/{project_id}/still-images").json()
    client.put(
        f"/api/v1/projects/{project_id}/still-images/order",
        json={"still_image_ids": [item["id"] for item in reversed(listed)]},
    )
    videos = list_project_shot_videos(main.database, project_id)
    assert [p.name for p in videos] == ["vid-1.webp", "vid-0.webp"]


def test_status_reports_ffmpeg(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    body = client.get("/api/v1/export/status").json()
    assert body["ffmpeg_available"] is True
    assert body["ffmpeg_path"] == "/usr/bin/ffmpeg"


def test_export_requires_ffmpeg(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_id = _project_with_shots(client, tmp_path)
    monkeypatch.setattr(main, "find_ffmpeg", lambda: None)
    response = client.post(f"/api/v1/projects/{project_id}/export", json={})
    assert response.status_code == 409


def test_export_requires_videos(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    project_id = client.post("/api/v1/projects", json={"name": "空"}).json()["id"]
    monkeypatch.setattr(main, "find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    response = client.post(f"/api/v1/projects/{project_id}/export", json={})
    assert response.status_code == 409


def test_export_and_download(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_id = _project_with_shots(client, tmp_path)
    monkeypatch.setattr(main, "find_ffmpeg", lambda: "/usr/bin/ffmpeg")

    captured: dict = {}

    def fake_concat(ffmpeg, inputs, output, fps, fades=None):
        captured["inputs"] = [p.name for p in inputs]
        captured["fps"] = fps
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"MP4-DATA")
        # Wanのカット（無音webp）だけなので、音声は残らない。
        return False

    monkeypatch.setattr(main, "concat_videos", fake_concat)

    exported = client.post(f"/api/v1/projects/{project_id}/export", json={"fps": 24})
    assert exported.status_code == 200
    body = exported.json()
    assert body["shot_count"] == 2
    assert captured["inputs"] == ["vid-0.webp", "vid-1.webp"]
    assert captured["fps"] == 24
    assert body["audio_included"] is False

    downloaded = client.get(f"/api/v1/projects/{project_id}/export/download")
    assert downloaded.status_code == 200
    assert downloaded.content == b"MP4-DATA"
    assert downloaded.headers["content-type"] == "video/mp4"


# --- fpsはカットに合わせる ---


@pytest.mark.parametrize("fps", [16, 24, 30])
def test_webp_frame_rate_is_read_from_the_frame_timing(tmp_path: Path, fps: int) -> None:
    """Wan のwebpは、ffmpeg ではなくコマの表示時間から読む（ffprobe は16fpsを8と返した）。"""
    assert video_frame_rate(_webp(tmp_path / "cut.webp", fps), "ffmpeg-is-not-used") == fps


# 実機の MiniMax H3 のカットを ffmpeg 9.0.1 に読ませたときの出力（抜粋）。
_H3_INFO = """Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'cut.mp4':
  Duration: 00:00:05.17, start: 0.000000, bitrate: 1689 kb/s
  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(progressive), 1344x768, 1554 kb/s, 24 fps, 24 tbr, 12288 tbn (default)
  Stream #0:1[0x2](und): Audio: aac (LC) (mp4a / 0x6134706D), 32000 Hz, stereo, fltp, 127 kb/s (default)
At least one output file must be specified
"""


def test_mp4_frame_rate_is_read_with_the_ffmpeg_used_for_joining(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ffprobe は利用者が用意した ffmpeg に付いていないことがあるので、結合と同じ ffmpeg で読む。"""
    video = tmp_path / "cut.mp4"
    video.write_bytes(b"mp4")

    def fake_run(command, **_kwargs):
        assert command[0] == "my-ffmpeg"
        # 出力先を指定しないので、ffmpeg は失敗で終わる（情報は出し終えている）。
        return subprocess.CompletedProcess(command, 1, b"", _H3_INFO.encode())

    monkeypatch.setattr(exporting.subprocess, "run", fake_run)
    assert video_frame_rate(video, "my-ffmpeg") == 24


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        # 平均の速さ（fps）を、時間の刻みから推した値（tbr）より先に見る。
        ("yuv420p, 1344x768, 24 fps, 48 tbr, 12288 tbn", 24),
        ("yuv420p, 1920x1080, 29.97 fps, 29.97 tbr, 30k tbn", 29.97),
        # fps が出ないときは tbr で補う。
        ("yuv420p, 1280x720, 16 tbr, 16k tbn", 16),
        ("yuv420p, 1280x720, 1k tbr, 1k tbn", 1000),
    ],
)
def test_frame_rate_is_parsed_from_the_video_stream(detail: str, expected: float) -> None:
    info = f"  Stream #0:0(und): Video: h264 (High), {detail} (default)\n"
    assert exporting.parse_video_frame_rate(info) == expected


def test_frame_rate_is_none_without_a_video_stream() -> None:
    assert exporting.parse_video_frame_rate("  Stream #0:0: Audio: aac, 32000 Hz\n") is None


def test_unreadable_videos_are_not_counted(tmp_path: Path) -> None:
    broken = tmp_path / "broken.webp"
    broken.write_bytes(b"not a webp")
    mp4 = tmp_path / "cut.mp4"
    mp4.write_bytes(b"mp4")
    assert cut_frame_rates([broken, mp4], None) == []


def test_export_matches_the_cut_frame_rate(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回帰: 既定が16固定だったため、24fpsのH3のカットが16fpsで書き出されていた。"""
    project_id = _project_with_shots(client, tmp_path, fps=24)
    monkeypatch.setattr(main, "find_ffmpeg", lambda: "ffmpeg")
    captured: dict = {}

    def fake_concat(_ffmpeg, _inputs, output, fps, fades=None):
        captured["fps"] = fps
        return _fake_concat(_ffmpeg, _inputs, output, fps)

    monkeypatch.setattr(main, "concat_videos", fake_concat)

    status = client.get(f"/api/v1/projects/{project_id}/export").json()
    assert status["cut_fps"] == [24]

    response = client.post(f"/api/v1/projects/{project_id}/export", json={})

    assert response.status_code == 200, response.text
    assert captured["fps"] == 24
    assert response.json()["fps"] == 24


def test_mixed_frame_rates_export_at_the_highest(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """作り方を混ぜたときは高いほうに揃える（低いカットのコマを間引かない）。"""
    project_id = _project_with_shots(client, tmp_path, fps=16)
    still_image_id = client.get(f"/api/v1/projects/{project_id}/still-images").json()[1]["id"]
    shot_id = client.get(f"/api/v1/still-images/{still_image_id}/shots").json()[0]["id"]
    ShotRepository(main.database).set_output_video(shot_id, _webp(tmp_path / "h.webp", 24))
    monkeypatch.setattr(main, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(main, "concat_videos", _fake_concat)

    assert client.get(f"/api/v1/projects/{project_id}/export").json()["cut_fps"] == [16, 24]
    assert client.post(f"/api/v1/projects/{project_id}/export", json={}).json()["fps"] == 24


def test_unreadable_frame_rate_asks_for_a_choice(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """読めないときに16などを黙って使わない。選んでもらう。"""
    project_id = _project_with_shots(client, tmp_path)
    monkeypatch.setattr(main, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(main, "concat_videos", _fake_concat)
    monkeypatch.setattr(main, "cut_frame_rates", lambda _videos, _ffmpeg: [])

    response = client.post(f"/api/v1/projects/{project_id}/export", json={})

    assert response.status_code == 409
    assert "fps" in response.json()["detail"]
    # 選んだ値なら書き出せる。
    assert client.post(f"/api/v1/projects/{project_id}/export", json={"fps": 24}).status_code == 200


def test_download_before_export_is_409(client: TestClient) -> None:
    project_id = client.post("/api/v1/projects", json={"name": "未書き出し"}).json()["id"]
    response = client.get(f"/api/v1/projects/{project_id}/export/download")
    assert response.status_code == 409


# --- 書き出した動画は残る（開き直しても取り出せる） ---


def _fake_concat(_ffmpeg, _inputs, output, _fps, fades=None):
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"MP4-DATA")
    return False


def test_export_status_is_empty_before_exporting(client: TestClient, tmp_path: Path) -> None:
    project_id = _project_with_shots(client, tmp_path)

    body = client.get(f"/api/v1/projects/{project_id}/export").json()

    assert body["exists"] is False
    assert body["created_at"] is None


def test_export_stays_available_after_reopening(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一度書き出せば、押した直後でなくても取り出せる（画面を開き直しても消えない）。"""
    project_id = _project_with_shots(client, tmp_path)
    monkeypatch.setattr(main, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(main, "concat_videos", _fake_concat)

    assert client.post(f"/api/v1/projects/{project_id}/export", json={}).status_code == 200

    body = client.get(f"/api/v1/projects/{project_id}/export").json()
    assert body["exists"] is True
    assert body["size_bytes"] > 0
    assert body["created_at"] is not None
    # 状態の確認とは別に、実体もそのまま取り出せる。
    assert client.get(f"/api/v1/projects/{project_id}/export/download").status_code == 200


def test_export_status_of_a_missing_project_is_404(client: TestClient) -> None:
    assert client.get("/api/v1/projects/does-not-exist/export").status_code == 404


# --- 選んだ場所へ複製する（OSのダイアログで選ばせた先） ---


def test_saving_the_export_to_a_chosen_place(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = _project_with_shots(client, tmp_path)
    monkeypatch.setattr(main, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(main, "concat_videos", _fake_concat)
    client.post(f"/api/v1/projects/{project_id}/export", json={})
    chosen = tmp_path / "手元" / "海辺の町の物語_全カット.mp4"
    chosen.parent.mkdir()

    response = client.post(
        f"/api/v1/projects/{project_id}/export/save-as", json={"path": str(chosen)}
    )

    assert response.status_code == 200, response.text
    assert chosen.read_bytes() == b"MP4-DATA"
    assert response.json()["size_bytes"] == len(b"MP4-DATA")


def test_saving_a_single_cut_to_a_chosen_place(client: TestClient, tmp_path: Path) -> None:
    """1カットだけ使いたい人の経路。つなぐ準備が無くても使える。"""
    project_id = _project_with_shots(client, tmp_path)
    still_image_id = client.get(f"/api/v1/projects/{project_id}/still-images").json()[0]["id"]
    shot_id = client.get(f"/api/v1/still-images/{still_image_id}/shots").json()[0]["id"]
    chosen = tmp_path / "手元" / "カット.webp"
    chosen.parent.mkdir(exist_ok=True)

    response = client.post(f"/api/v1/shots/{shot_id}/video/save-as", json={"path": str(chosen)})

    assert response.status_code == 200, response.text
    assert chosen.read_bytes() == (tmp_path / "vid-0.webp").read_bytes()


def test_a_place_that_does_not_exist_is_refused(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """アプリの保存先の外へ書く唯一の経路なので、受け取った場所をそのまま信じない。"""
    project_id = _project_with_shots(client, tmp_path)
    monkeypatch.setattr(main, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(main, "concat_videos", _fake_concat)
    client.post(f"/api/v1/projects/{project_id}/export", json={})

    missing = client.post(
        f"/api/v1/projects/{project_id}/export/save-as",
        json={"path": str(tmp_path / "無いフォルダ" / "x.mp4")},
    )
    assert missing.status_code == 422

    relative = client.post(
        f"/api/v1/projects/{project_id}/export/save-as", json={"path": "x.mp4"}
    )
    assert relative.status_code == 422


def test_saving_before_exporting_is_refused(client: TestClient, tmp_path: Path) -> None:
    project_id = _project_with_shots(client, tmp_path)

    response = client.post(
        f"/api/v1/projects/{project_id}/export/save-as",
        json={"path": str(tmp_path / "x.mp4")},
    )

    assert response.status_code == 409


def test_saving_a_still_image_to_a_chosen_place(client: TestClient, tmp_path: Path) -> None:
    """挿絵として使う人の完成品。合成した1枚をそのまま外へ取り出せる。"""
    project_id = client.post("/api/v1/projects", json={"name": "挿絵"}).json()["id"]
    still_image_id = client.post(
        f"/api/v1/projects/{project_id}/still-images", json={"name": "夕暮れ"}
    ).json()["id"]
    composed = tmp_path / "keyframes" / f"{still_image_id}.png"
    composed.parent.mkdir()
    composed.write_bytes(b"PNG-DATA")
    StillImageRepository(main.database).set_image(still_image_id, composed, source_prompt="")
    chosen = tmp_path / "手元 の 挿絵" / "夕暮れ.png"
    chosen.parent.mkdir()

    response = client.post(
        f"/api/v1/still-images/{still_image_id}/image/save-as", json={"path": str(chosen)}
    )

    assert response.status_code == 200, response.text
    assert chosen.read_bytes() == b"PNG-DATA"
    # 外へ出すのは複製。アプリの中の1枚はそのまま残る。
    assert composed.read_bytes() == b"PNG-DATA"


def test_saving_a_still_image_before_composing_is_refused(
    client: TestClient, tmp_path: Path
) -> None:
    project_id = client.post("/api/v1/projects", json={"name": "挿絵"}).json()["id"]
    still_image_id = client.post(
        f"/api/v1/projects/{project_id}/still-images", json={"name": "未合成"}
    ).json()["id"]

    response = client.post(
        f"/api/v1/still-images/{still_image_id}/image/save-as",
        json={"path": str(tmp_path / "x.png")},
    )

    assert response.status_code == 409


# --- 大きさの違うカットを繋ぐ（Wan 2.2 と MiniMax H3 を混ぜたとき） ---


def test_export_frame_size_follows_the_largest_cut() -> None:
    # Wan 2.2（1280x720）と MiniMax H3（1344x768）を混ぜたら、画素数の多い H3 に合わせる。
    assert export_frame_size([(1280, 720), (1344, 768), (1280, 720)]) == (1344, 768)
    # 画素数が同じなら先のカットに合わせる。
    assert export_frame_size([(768, 1344), (1344, 768)]) == (768, 1344)
    # H.264 は奇数の幅・高さで書き出せないので、偶数へ足す。
    assert export_frame_size([(1279, 719)]) == (1280, 720)


_FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(_FFMPEG is None, reason="ffmpeg が無い環境では実際の結合を試せない")


def _real_ffmpeg(*arguments: str) -> None:
    subprocess.run([_FFMPEG, "-y", "-v", "error", *arguments], check=True, capture_output=True)


@needs_ffmpeg
def test_cuts_of_different_sizes_are_joined_with_bars(tmp_path: Path) -> None:
    """Wan のwebp（1280x720・無音）と H3 のmp4（1344x768・音声付き）を実際の ffmpeg で繋ぐ。

    concat フィルタは大きさが違うと失敗するので、大きいほうに合わせ、縦横比の違う分は
    上下の黒い余白で埋める（1280x720 は 1344x756 へ拡大され、上下に6画素ずつ余白が付く）。
    """
    # 空白と日本語を含むフォルダでも繋げること。
    folder = tmp_path / "作品 フォルダ"
    folder.mkdir()
    wan = folder / "カット 1.webp"
    frames = [Image.new("RGB", (1280, 720), (255, 255, 255)) for _ in range(4)]
    frames[0].save(wan, save_all=True, append_images=frames[1:], duration=62, loop=0)
    h3 = folder / "カット 2.mp4"
    _real_ffmpeg(
        "-f", "lavfi", "-i", "color=c=white:s=1344x768:r=24:d=0.5",
        "-f", "lavfi", "-i", "sine=d=0.5",
        "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(h3),
    )
    output = folder / "書き出し.mp4"

    audio_included = concat_videos(_FFMPEG, [wan, h3], output, 24.0)

    # 無音のカットが混ざるので音声は残らない。
    assert audio_included is False
    assert video_frame_size(_FFMPEG, output) == (1344, 768)
    first = folder / "先頭.png"
    _real_ffmpeg("-i", str(output), "-frames:v", "1", str(first))
    with Image.open(first) as image:
        pixels = image.convert("L")
        assert pixels.getpixel((672, 2)) < 32  # 上の余白
        assert pixels.getpixel((672, 765)) < 32  # 下の余白
        assert pixels.getpixel((672, 384)) > 220  # 絵そのもの
        assert pixels.getpixel((2, 384)) > 220  # 左右は切れずに端まで絵


@needs_ffmpeg
def test_export_is_encoded_as_finely_as_the_cuts(tmp_path: Path) -> None:
    """カット動画（ComfyUI の SaveVideo）と同じ CRF 18 で作り直す。x264 は使った設定を
    ファイルの中に書き残すので、それを読んで確かめる。"""
    cut = tmp_path / "cut.mp4"
    _real_ffmpeg("-f", "lavfi", "-i", "testsrc=s=320x180:r=24:d=0.5", "-c:v", "mpeg4", str(cut))
    output = tmp_path / "out.mp4"

    concat_videos(_FFMPEG, [cut], output, 24.0)

    assert b"crf=18.0" in output.read_bytes()


@needs_ffmpeg
def test_an_unreadable_cut_is_reported_by_its_position(tmp_path: Path) -> None:
    good = tmp_path / "good.webp"
    frames = [Image.new("RGB", (64, 64)) for _ in range(2)]
    frames[0].save(good, save_all=True, append_images=frames[1:], duration=62, loop=0)
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video")

    with pytest.raises(ExportError, match="2本目"):
        concat_videos(_FFMPEG, [good, broken], tmp_path / "out.mp4", 16.0)


# --- シーンの切り替わりの暗転 ---


def _scene(*names: str, transition: str = "project", seconds: float | None = None) -> ExportScene:
    return ExportScene(videos=[Path(name) for name in names], transition=transition, transition_seconds=seconds)


def test_fades_go_only_where_the_scene_changes() -> None:
    """同じシーンの中のカットは前のカットから続けて作るので、つなぎ目に暗転を入れない。"""
    fades = plan_scene_fades(
        [_scene("a1", "a2"), _scene("b1", "b2")], transition="fade", seconds=0.5, edges=False
    )
    assert fades == [
        ClipFade(),
        ClipFade(fade_out=0.5),
        ClipFade(fade_in=0.5),
        ClipFade(),
    ]


def test_without_a_transition_scenes_are_joined_as_they_are() -> None:
    fades = plan_scene_fades([_scene("a"), _scene("b")], transition="none", seconds=0.5, edges=False)
    assert fades == [ClipFade(), ClipFade()]


def test_a_scene_overrides_the_transition_into_it() -> None:
    """切り替わりの効果は後ろのシーンの指定で決まる。作品の設定と違う指定も、秒数も効く。"""
    fades = plan_scene_fades(
        [_scene("a"), _scene("b", transition="fade", seconds=1.2), _scene("c", transition="none")],
        transition="none",
        seconds=0.5,
        edges=False,
    )
    assert fades == [ClipFade(fade_out=1.2), ClipFade(fade_in=1.2), ClipFade()]

    fades = plan_scene_fades(
        [_scene("a"), _scene("b", transition="none"), _scene("c")],
        transition="fade",
        seconds=0.5,
        edges=False,
    )
    assert fades == [ClipFade(), ClipFade(fade_out=0.5), ClipFade(fade_in=0.5)]


def test_a_scene_without_videos_is_skipped() -> None:
    """動画の無いシーンは書き出しに出てこないので、前後のシーンが直接つながる。
    その切り替わりは、後ろの動画のあるシーンの指定に従う。"""
    fades = plan_scene_fades(
        [_scene("a"), _scene(transition="none"), _scene("c", transition="fade", seconds=0.8)],
        transition="none",
        seconds=0.5,
        edges=False,
    )
    assert fades == [ClipFade(fade_out=0.8), ClipFade(fade_in=0.8)]


def test_the_first_scene_has_no_transition_into_it() -> None:
    fades = plan_scene_fades(
        [_scene("a", transition="fade"), _scene("b")], transition="none", seconds=0.5, edges=False
    )
    assert fades == [ClipFade(), ClipFade()]


def test_the_start_and_end_of_the_video_can_fade_too() -> None:
    fades = plan_scene_fades([_scene("a")], transition="none", seconds=0.7, edges=True)
    assert fades == [ClipFade(fade_in=0.7, fade_out=0.7)]
    fades = plan_scene_fades(
        [_scene("a"), _scene("b", seconds=0.3)], transition="fade", seconds=0.7, edges=True
    )
    assert fades == [ClipFade(fade_in=0.7, fade_out=0.3), ClipFade(fade_in=0.3, fade_out=0.7)]


def test_transition_settings_are_kept_on_the_project_and_the_scene(client: TestClient) -> None:
    project = client.post("/api/v1/projects", json={"name": "切り替え"}).json()
    project_id = project["id"]
    # 作った作品は、これまでどおりそのままつなぐ。
    assert project["scene_transition"] == "none"
    assert project["scene_transition_seconds"] == 0.5
    assert project["scene_transition_edges"] is False

    updated = client.patch(
        f"/api/v1/projects/{project_id}",
        json={"scene_transition": "fade", "scene_transition_seconds": 1.5, "scene_transition_edges": True},
    ).json()
    assert updated["scene_transition"] == "fade"
    assert updated["scene_transition_seconds"] == 1.5
    assert updated["scene_transition_edges"] is True
    # 1つだけ変えても、ほかの設定は残る。
    assert client.patch(
        f"/api/v1/projects/{project_id}", json={"scene_transition_edges": False}
    ).json()["scene_transition"] == "fade"

    scene = client.post(f"/api/v1/projects/{project_id}/still-images", json={"name": "S"}).json()
    assert scene["scene_transition"] == "project"
    assert scene["scene_transition_seconds"] is None

    scene = client.patch(
        f"/api/v1/still-images/{scene['id']}",
        json={"scene_transition": "fade", "scene_transition_seconds": 0.8},
    ).json()
    assert (scene["scene_transition"], scene["scene_transition_seconds"]) == ("fade", 0.8)
    # 秒数は null で作品の秒数に戻せる。種類は変わらない。
    scene = client.patch(
        f"/api/v1/still-images/{scene['id']}", json={"scene_transition_seconds": None}
    ).json()
    assert (scene["scene_transition"], scene["scene_transition_seconds"]) == ("fade", None)


@pytest.mark.parametrize("seconds", [0, 0.05, 5.1])
def test_transition_seconds_out_of_range_are_refused(client: TestClient, seconds: float) -> None:
    project_id = client.post("/api/v1/projects", json={"name": "切り替え"}).json()["id"]
    response = client.patch(
        f"/api/v1/projects/{project_id}", json={"scene_transition_seconds": seconds}
    )
    assert response.status_code == 422


def test_export_fades_between_scenes_as_set(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = _project_with_shots(client, tmp_path)
    client.patch(f"/api/v1/projects/{project_id}", json={"scene_transition": "fade"})
    monkeypatch.setattr(main, "find_ffmpeg", lambda: "ffmpeg")
    captured: dict = {}

    def fake_concat(ffmpeg, inputs, output, fps, fades=None):
        captured["fades"] = fades
        return _fake_concat(ffmpeg, inputs, output, fps)

    monkeypatch.setattr(main, "concat_videos", fake_concat)

    assert client.post(f"/api/v1/projects/{project_id}/export", json={}).status_code == 200
    assert captured["fades"] == [ClipFade(fade_out=0.5), ClipFade(fade_in=0.5)]


@pytest.mark.parametrize(
    ("info", "expected"),
    [
        ("  Duration: 00:00:05.17, start: 0.000000, bitrate: 1689 kb/s\n", 5.17),
        ("  Duration: 01:02:03.50, start: 0.000000\n", 3723.5),
        # アニメーションwebpは長さを出さない。
        ("  Duration: N/A, start: 0.000000, bitrate: N/A\n", None),
    ],
)
def test_duration_is_parsed_from_the_input_info(info: str, expected: float | None) -> None:
    assert exporting.parse_duration(info) == expected


def test_webp_duration_is_the_sum_of_the_frame_timing(tmp_path: Path) -> None:
    """ffmpeg はwebpの長さを出さないが、各コマを書かれた表示時間どおりに読む。"""
    video = _webp(tmp_path / "cut.webp", 16, frames=10)
    assert exporting.video_duration(video, "ffmpeg-is-not-used") == pytest.approx(0.62)


def _brightness(video: Path, at: float, folder: Path) -> float:
    """書き出した動画の、指定した時刻のコマの真ん中の明るさ（0〜255）。"""
    frame = folder / f"frame-{at}.png"
    _real_ffmpeg("-ss", str(at), "-i", str(video), "-frames:v", "1", str(frame))
    with Image.open(frame) as image:
        return image.convert("L").getpixel((image.width // 2, image.height // 2))


@needs_ffmpeg
def test_the_scene_change_fades_through_black(tmp_path: Path) -> None:
    """白いカット2本を、間に暗転を入れて実際の ffmpeg で繋ぐ。切り替わりの前後は黒く、
    それ以外は白いまま。音声付きのカットどうしなら、音も残る。"""
    cuts = []
    for index in range(2):
        cut = tmp_path / f"cut-{index}.mp4"
        _real_ffmpeg(
            "-f", "lavfi", "-i", "color=c=white:s=160x90:r=24:d=2",
            "-f", "lavfi", "-i", "sine=d=2",
            "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(cut),
        )
        cuts.append(cut)
    output = tmp_path / "out.mp4"

    audio_included = concat_videos(
        _FFMPEG, cuts, output, 24.0, fades=[ClipFade(fade_out=0.5), ClipFade(fade_in=0.5)]
    )

    assert audio_included is True
    assert _brightness(output, 1.0, tmp_path) > 220  # 前のシーンの途中
    assert _brightness(output, 1.96, tmp_path) < 40  # 前のシーンの終わり
    assert _brightness(output, 2.02, tmp_path) < 40  # 次のシーンの始まり
    assert _brightness(output, 3.0, tmp_path) > 220  # 次のシーンの途中


@needs_ffmpeg
def test_a_webp_cut_fades_at_its_own_end(tmp_path: Path) -> None:
    """Wan のwebpは ffmpeg が長さを出さないので、コマの表示時間から終わりを決めて暗転する。"""
    wan = tmp_path / "cut.webp"
    frames = [Image.new("RGB", (160, 90), (255, 255, 255)) for _ in range(32)]
    # 同じ絵のコマは Pillow が1コマにまとめてしまうので、隅の1画素だけ変えて動画にする。
    for index, frame in enumerate(frames):
        frame.putpixel((0, 0), (index * 8, 0, 0))
    frames[0].save(wan, save_all=True, append_images=frames[1:], duration=62, loop=0)
    output = tmp_path / "out.mp4"

    concat_videos(_FFMPEG, [wan], output, 16.0, fades=[ClipFade(fade_out=0.5)])

    assert _brightness(output, 0.5, tmp_path) > 220
    assert _brightness(output, 1.9, tmp_path) < 40
