"""カットの連続性・ビート（時間分割）・構図変更の通し試験。

ここで押さえたいのは3点。
1. 最終フレームはワークフローが保存したPNGから採る（mp4でもffmpegに依存しない）
2. 分割モードはビートごとに区間を生成し、前区間の最終フレームから続ける
3. 構図変更で作った起点フレームは、連続性より優先して使われる
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from chotto_sashie_shared.jobs import JobRepository
from chotto_sashie_api import main
from chotto_sashie_api import shots
from chotto_sashie_api.database import Database
from chotto_sashie_api.shot_video import OPENING_FRAME_DIRECTIVE
from chotto_sashie_api.still_images import StillImageRepository

_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
# 「動画」の中身は見ないので、入れ物が分かる程度のバイト列で足りる。
_FAKE_MP4 = b"\x00\x00\x00\x18ftypmp42fake-video-bytes"


def _png(color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _color_of(data: bytes) -> tuple[int, int, int]:
    with Image.open(io.BytesIO(data)) as image:
        return image.convert("RGB").getpixel((0, 0))


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    database = Database(tmp_path / "timeline-api.sqlite3")
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(main, "jobs", JobRepository(database))
    # ffmpegの有無で結果が変わらないことを確かめたいので、無い環境に固定する。
    monkeypatch.setattr(main, "find_ffmpeg", lambda: None)
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture
def fake_comfy(monkeypatch: pytest.MonkeyPatch) -> dict:
    """ワークフローどおり「動画1本＋最終フレームPNG1枚」を返すComfyUI。

    最終フレームの色は提出のたびに変え、どの区間の1枚が次へ渡ったか追えるようにする。
    """
    state: dict = {"uploads": [], "workflows": [], "kinds": [], "translated": [], "submits": 0}

    async def upload_image(_self, data, filename, content_type="image/png", *, subfolder="", overwrite=True):
        state["uploads"].append({"filename": filename, "data": data})
        return {"name": filename, "subfolder": "", "type": "input"}

    async def submit(_self, workflow, _client_id):
        state["submits"] += 1
        state["workflows"].append(workflow)
        # 翻訳・合成・動画では、出てくる成果物が違う。
        kinds = {node.get("class_type") for node in workflow.values() if isinstance(node, dict)}
        if "TextGenerate" in kinds:
            instruction = workflow["2"]["inputs"]["prompt"]
            # 話す人の記述は、名指しの句として訳させる。
            state["kinds"].append("person" if "noun phrase" in instruction else "text")
            state["translated"].append(instruction)
            return f"prompt-{state['submits']}"
        state["kinds"].append("image" if "SaveImage" in kinds and "KSampler" in kinds else "video")
        return f"prompt-{state['submits']}"

    async def history(_self, prompt_id):
        index = int(prompt_id.rsplit("-", 1)[-1])
        if state["kinds"][index - 1] == "text":
            # 実機の Gemma 4 と同じく、英語1行を返す。
            return {
                "status": {"completed": True, "status_str": "success"},
                "outputs": {"3": {"text": [f"english line {index}"]}},
            }
        if state["kinds"][index - 1] == "person":
            # 文頭が大文字・末尾に句点の形で返ることがある（文の途中へ置けるよう整える側を試す）。
            return {
                "status": {"completed": True, "status_str": "success"},
                "outputs": {"3": {"text": [f"The person {index}."]}},
            }
        if state["kinds"][index - 1] == "image":
            return {
                "status": {"completed": True, "status_str": "success"},
                "outputs": {
                    "40": {"images": [{"filename": f"last{index}.png", "subfolder": "f", "type": "output"}]},
                },
            }
        return {
            "status": {"completed": True, "status_str": "success"},
            "outputs": {
                "16": {"images": [{"filename": f"v{index}.mp4", "subfolder": "shots", "type": "output"}]},
                "21": {"images": [{"filename": f"last{index}.png", "subfolder": "f", "type": "output"}]},
            },
        }

    async def view_image(_self, image):
        name = image["filename"]
        if name.endswith(".png"):
            index = int(name.removeprefix("last").removesuffix(".png"))
            return _png((10 * index, 20 * index, 30 * index)), "image/png"
        return _FAKE_MP4, "video/mp4"

    monkeypatch.setattr(main.ComfyClient, "upload_image", upload_image)
    monkeypatch.setattr(main.ComfyClient, "submit", submit)
    monkeypatch.setattr(main.ComfyClient, "history", history)
    monkeypatch.setattr(main.ComfyClient, "view_image", view_image)
    return state


def _still_image(client: TestClient) -> str:
    project_id = client.post("/api/v1/projects", json={"name": "タイムライン試験"}).json()["id"]
    still_image_id = client.post(
        f"/api/v1/projects/{project_id}/still-images", json={"name": "S"}
    ).json()["id"]
    root = main._STILL_IMAGE_ROOT
    root.mkdir(parents=True, exist_ok=True)
    image_file = root / f"{still_image_id}.png"
    image_file.write_bytes(_TINY_PNG)
    StillImageRepository(main.database).set_image(still_image_id, image_file)
    return still_image_id


def _people(client: TestClient, still_image_id: str, *people: dict) -> list[str]:
    """静止画に人物を入れる。見た目（性別・年齢帯・髪）と配置は、話す人の名指しに使われる。"""
    project_id = client.get(f"/api/v1/still-images/{still_image_id}").json()["project_id"]
    character_ids: list[str] = []
    for person in people:
        character_id = client.post(
            f"/api/v1/projects/{project_id}/characters", json={"name": person["name"]}
        ).json()["id"]
        profile = {
            key: person[key] for key in ("gender_age", "hair_color", "hair_style") if key in person
        }
        if profile:
            response = client.patch(f"/api/v1/characters/{character_id}/profile", json=profile)
            assert response.status_code == 200, response.text
        character_ids.append(character_id)
    response = client.patch(
        f"/api/v1/still-images/{still_image_id}", json={"character_ids": character_ids}
    )
    assert response.status_code == 200, response.text
    for character_id, person in zip(character_ids, people):
        if "placement" in person:
            response = client.put(
                f"/api/v1/still-images/{still_image_id}/characters/{character_id}",
                json={"spec": {"placement": person["placement"]}},
            )
            assert response.status_code == 200, response.text
    return character_ids


def _line(character_id: str, text: str) -> dict:
    return {"speaker_character_id": character_id, "text": text}


def _generate(client: TestClient, shot_id: str, **body) -> dict:
    payload = {"base_url": "http://127.0.0.1:8190", "engine": "minimax_h3", **body}
    response = client.post(f"/api/v1/shots/{shot_id}/generate-video", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


# --- 1. 最終フレームはワークフローの保存したPNGから採る ---


def test_mp4_shot_keeps_its_last_frame_without_ffmpeg(client: TestClient, fake_comfy: dict) -> None:
    """音声付き(mp4)でも、ffmpegが無くても連続性が使える。"""
    still_image_id = _still_image(client)
    shot1 = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]
    _generate(client, shot1)
    assert client.get(f"/api/v1/shots/{shot1}").json()["has_last_frame"] is True


def test_second_shot_starts_from_the_workflow_saved_frame(client: TestClient, fake_comfy: dict) -> None:
    still_image_id = _still_image(client)
    shot1 = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]
    shot2 = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]

    _generate(client, shot1)
    fake_comfy["uploads"].clear()
    _generate(client, shot2)

    # カット1の最終フレーム（1回目の提出なので色は (10,20,30)）が起点になる。
    assert _color_of(fake_comfy["uploads"][0]["data"]) == (10, 20, 30)


# --- 2. セリフと動きの指示を混ぜない ---


def test_speech_is_kept_out_of_the_visual_lines(client: TestClient, fake_comfy: dict) -> None:
    still_image_id = _still_image(client)
    [hana] = _people(client, still_image_id, {"name": "ハナ", "placement": "左"})
    shot = client.post(
        f"/api/v1/still-images/{still_image_id}/shots",
        json={"motion_prompt": "she turns around", "speech_lines": [_line(hana, "行こう")]},
    ).json()["id"]
    _generate(client, shot)

    prompt = _video_prompts(fake_comfy)[0]
    # 映像の行に動き、そのあとの行にセリフ。Second の行には混ぜない。
    assert "Second 0–5.2: She turns around." in prompt
    assert 'says "行こう".' in prompt
    for line in prompt.splitlines():
        if line.startswith("Second "):
            assert '"' not in line, line


# --- 2b. 話す人を行ごとに決める ---


def test_two_people_each_get_their_own_lines(client: TestClient, fake_comfy: dict) -> None:
    """二人いる場面では、行ごとに話す人を名指しする（誰でも "the character" にしない）。"""
    still_image_id = _still_image(client)
    hana, sora = _people(
        client,
        still_image_id,
        {"name": "ハナ", "gender_age": "10代の女性", "placement": "左"},
        {"name": "ソラ", "gender_age": "10代の男性", "placement": "右"},
    )
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots",
        json={
            "motion_prompt": "they face each other",
            "speech_lines": [_line(hana, "行こう"), _line(sora, "待って"), _line(hana, "早く")],
        },
    ).json()["id"]

    lines = client.get(f"/api/v1/shots/{shot_id}").json()["speech_lines"]
    assert [line["speaker_character_id"] for line in lines] == [hana, sora, hana]
    assert all(line["speaker_present"] for line in lines)

    _generate(client, shot_id)

    # 二人の記述が、名指しの句として訳される（セリフそのものは訳さない）。
    people = [text for text in fake_comfy["translated"] if "noun phrase" in text]
    assert len(people) == 2
    assert any("位置: 左" in text and "10代の女性" in text for text in people)
    assert not any("行こう" in text or "待って" in text for text in fake_comfy["translated"])

    prompt = _video_prompts(fake_comfy)[0]
    said = [line for line in prompt.splitlines() if " says " in line]
    assert len(said) == 3
    assert said[0].startswith("At 0s the person ") and said[0].endswith('says "行こう".')
    assert said[1].startswith("Then the person ") and said[1].endswith('says "待って".')
    # 同じ人の行は、同じ名指しになる。
    assert said[2].split(" says ")[0].removeprefix("Then ") == said[0].split(" says ")[0].removeprefix(
        "At 0s "
    )
    assert said[0].split(" says ")[0] != said[1].split(" says ")[0].replace("Then", "At 0s")
    # 名前は文に入れない（日本語の名前は、セリフと区別されず読み上げられる）。
    assert "ハナ" not in prompt and "ソラ" not in prompt


def test_speaker_must_be_in_the_still_image(client: TestClient) -> None:
    still_image_id = _still_image(client)
    other_still_image = _still_image(client)
    [stranger] = _people(client, other_still_image, {"name": "よその人"})
    response = client.post(
        f"/api/v1/still-images/{still_image_id}/shots",
        json={"speech_lines": [_line(stranger, "行こう")]},
    )
    assert response.status_code == 400
    assert "この静止画にいる人物" in response.json()["detail"]


def test_line_needs_a_speaker_unless_it_is_narration(client: TestClient) -> None:
    still_image_id = _still_image(client)
    response = client.post(
        f"/api/v1/still-images/{still_image_id}/shots",
        json={"speech_lines": [{"text": "行こう"}]},
    )
    assert response.status_code == 422


def test_narration_needs_no_speaker_and_is_not_named(client: TestClient, fake_comfy: dict) -> None:
    still_image_id = _still_image(client)
    _people(client, still_image_id, {"name": "ハナ", "placement": "左"})
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots",
        json={
            "motion_prompt": "she waits",
            "speech_lines": [{"narration": True, "text": "その日は雨だった"}],
        },
    ).json()["id"]

    _generate(client, shot_id)

    assert not any("noun phrase" in text for text in fake_comfy["translated"])
    prompt = _video_prompts(fake_comfy)[0]
    assert 'At 0s an off-screen narrator says "その日は雨だった".' in prompt


def test_speaker_removed_from_the_still_image_is_reported(client: TestClient, fake_comfy: dict) -> None:
    """人物を静止画から外しても行は残し、話す人がいないことを知らせる。"""
    still_image_id = _still_image(client)
    hana, sora = _people(client, still_image_id, {"name": "ハナ"}, {"name": "ソラ"})
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots",
        json={"motion_prompt": "she waits", "speech_lines": [_line(sora, "待って")]},
    ).json()["id"]
    client.patch(f"/api/v1/still-images/{still_image_id}", json={"character_ids": [hana]})

    [line] = client.get(f"/api/v1/shots/{shot_id}").json()["speech_lines"]
    assert line["speaker_character_id"] == sora
    assert line["speaker_present"] is False

    _generate(client, shot_id)
    # いない人を名指ししない。
    assert 'At 0s one of the characters says "待って".' in _video_prompts(fake_comfy)[0]


def test_deleted_speaker_keeps_the_line(client: TestClient) -> None:
    """人物を消しても、書いたセリフは消さない（話す人だけ外して、選び直してもらう）。"""
    still_image_id = _still_image(client)
    [hana] = _people(client, still_image_id, {"name": "ハナ"})
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots",
        json={"speech_lines": [_line(hana, "行こう")]},
    ).json()["id"]
    assert client.delete(f"/api/v1/characters/{hana}").status_code == 204

    [line] = client.get(f"/api/v1/shots/{shot_id}").json()["speech_lines"]
    assert line["text"] == "行こう"
    assert line["speaker_character_id"] is None
    assert line["speaker_present"] is False


def test_moving_the_speaker_makes_the_prompt_stale(client: TestClient, fake_comfy: dict) -> None:
    """配置を変えると名指しが変わるので、用意した文はもう使えない。"""
    still_image_id = _still_image(client)
    [hana] = _people(client, still_image_id, {"name": "ハナ", "placement": "左"})
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots",
        json={"motion_prompt": "she waits", "speech_lines": [_line(hana, "行こう")]},
    ).json()["id"]
    assert _prepare_prompt(client, shot_id)["ready"] is True

    client.put(
        f"/api/v1/still-images/{still_image_id}/characters/{hana}",
        json={"spec": {"placement": "右"}},
    )

    assert _prompt_state(client, shot_id, engine="minimax_h3")["stale"] is True


def test_speech_lines_are_replaced_as_a_whole(client: TestClient) -> None:
    still_image_id = _still_image(client)
    hana, sora = _people(client, still_image_id, {"name": "ハナ"}, {"name": "ソラ"})
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots",
        json={"speech_lines": [_line(hana, "行こう"), _line(sora, "待って")]},
    ).json()["id"]

    response = client.patch(
        f"/api/v1/shots/{shot_id}", json={"speech_lines": [_line(sora, "待って")]}
    )

    assert response.status_code == 200, response.text
    assert [line["text"] for line in response.json()["speech_lines"]] == ["待って"]


# --- 3. ビート（分割モード） ---


def _shot_with_beats(client: TestClient, still_image_id: str, durations: list[float]) -> str:
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots", json={"timeline_mode": "split"}
    ).json()["id"]
    for index, duration in enumerate(durations, start=1):
        response = client.post(
            f"/api/v1/shots/{shot_id}/beats",
            json={"duration_seconds": duration, "motion_prompt": f"beat {index}"},
        )
        assert response.status_code == 201, response.text
    return shot_id


def test_beats_report_their_start_time(client: TestClient) -> None:
    still_image_id = _still_image(client)
    shot_id = _shot_with_beats(client, still_image_id, [3.0, 2.0, 4.0])
    beats = client.get(f"/api/v1/shots/{shot_id}/beats").json()
    assert [beat["start_seconds"] for beat in beats] == [0.0, 3.0, 5.0]
    # カットの尺は、ビートの合計から決まる。
    assert client.get(f"/api/v1/shots/{shot_id}").json()["total_seconds"] == 9.0


def test_split_mode_generates_one_segment_per_beat(client: TestClient, fake_comfy: dict) -> None:
    still_image_id = _still_image(client)
    shot_id = _shot_with_beats(client, still_image_id, [3.0, 3.0])

    _generate(client, shot_id)

    assert fake_comfy["submits"] == 2
    beats = client.get(f"/api/v1/shots/{shot_id}/beats").json()
    assert all(beat["has_segment_video"] for beat in beats)
    assert all(beat["segment_video_format"] == "mp4" for beat in beats)



def test_regenerating_split_segments_moves_the_video_version(
    client: TestClient, fake_comfy: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """区間動画もビートごとの場所へ上書きする。時刻が同じでも、作り直せば動画の版が変わる。"""
    monkeypatch.setattr(shots, "_now", lambda: "2026-01-01T00:00:00+00:00")
    still_image_id = _still_image(client)
    shot_id = _shot_with_beats(client, still_image_id, [3.0, 3.0])
    _generate(client, shot_id)
    before = client.get(f"/api/v1/shots/{shot_id}").json()["video_version"]

    _generate(client, shot_id)

    assert client.get(f"/api/v1/shots/{shot_id}").json()["video_version"] != before


def test_split_mode_chains_beats_through_their_last_frames(client: TestClient, fake_comfy: dict) -> None:
    still_image_id = _still_image(client)
    shot_id = _shot_with_beats(client, still_image_id, [3.0, 3.0])

    _generate(client, shot_id)

    starts = [item for item in fake_comfy["uploads"] if "-start" in item["filename"]]
    # 1区間目は元の静止画、2区間目は1区間目の最終フレーム。
    assert starts[0]["data"] == _TINY_PNG
    assert _color_of(starts[1]["data"]) == (10, 20, 30)


def test_split_mode_gives_each_beat_its_own_length(client: TestClient, fake_comfy: dict) -> None:
    """区間ごとに尺を割り振れないと、時間分割の意味が無い。"""
    still_image_id = _still_image(client)
    shot_id = _shot_with_beats(client, still_image_id, [3.0, 6.0])

    _generate(client, shot_id)

    lengths = [workflow["7"]["inputs"]["length"] for workflow in fake_comfy["workflows"]]
    # 24fpsで 17k+5 に切り上げ: 3秒→72→73、6秒→144→158。
    assert lengths == [73, 158]


def test_single_mode_length_comes_from_the_beat_total(client: TestClient, fake_comfy: dict) -> None:
    still_image_id = _still_image(client)
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots", json={"timeline_mode": "single"}
    ).json()["id"]
    for duration in (3.0, 3.0):
        client.post(f"/api/v1/shots/{shot_id}/beats", json={"duration_seconds": duration})

    _generate(client, shot_id)

    assert fake_comfy["submits"] == 1
    # 6秒→144フレーム→158へ切り上げ。
    assert fake_comfy["workflows"][0]["7"]["inputs"]["length"] == 158


def test_single_mode_labels_beats_with_time_ranges(client: TestClient, fake_comfy: dict) -> None:
    still_image_id = _still_image(client)
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots", json={"timeline_mode": "single"}
    ).json()["id"]
    for index, duration in enumerate((3.0, 3.0), start=1):
        client.post(
            f"/api/v1/shots/{shot_id}/beats",
            json={"duration_seconds": duration, "motion_prompt": f"beat {index}"},
        )

    _generate(client, shot_id)

    prompt = fake_comfy["workflows"][0]["7"]["inputs"]["prompt"]
    assert "Second 0\u20133: Beat 1." in prompt
    assert "Second 3\u20136: Beat 2." in prompt


def test_first_beat_keeps_the_shot_directions_as_its_own_first_beat(client: TestClient) -> None:
    """カット全体の指示はビートが無いときだけ使う。ビートを足しても、書いた動きを失わない。"""
    still_image_id = _still_image(client)
    [hana] = _people(client, still_image_id, {"name": "ハナ"})
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots",
        json={
            "motion_prompt": "ゆっくり振り返る",
            "speech_lines": [_line(hana, "待って")],
            "duration_seconds": 4.0,
        },
    ).json()["id"]

    response = client.post(
        f"/api/v1/shots/{shot_id}/beats",
        json={"duration_seconds": 3.0, "motion_prompt": "歩き出す"},
    )

    assert response.status_code == 201, response.text
    shot = client.get(f"/api/v1/shots/{shot_id}").json()
    assert [beat["motion_prompt"] for beat in shot["beats"]] == ["ゆっくり振り返る", "歩き出す"]
    assert [beat["duration_seconds"] for beat in shot["beats"]] == [4.0, 3.0]
    assert [line["text"] for line in shot["beats"][0]["speech_lines"]] == ["待って"]
    # 同じ指示を2か所に持たない（ビートを全部消したときに古い指示が蘇らない）。
    assert shot["motion_prompt"] == ""
    assert shot["speech_lines"] == []
    assert shot["total_seconds"] == 7.0


def test_first_beat_on_an_empty_shot_adds_only_that_beat(client: TestClient) -> None:
    still_image_id = _still_image(client)
    shot_id = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]

    client.post(f"/api/v1/shots/{shot_id}/beats", json={"duration_seconds": 3.0, "motion_prompt": "x"})

    assert [beat["motion_prompt"] for beat in client.get(f"/api/v1/shots/{shot_id}/beats").json()] == ["x"]


def test_deleting_a_beat_renumbers_the_rest(client: TestClient) -> None:
    still_image_id = _still_image(client)
    shot_id = _shot_with_beats(client, still_image_id, [1.0, 2.0, 3.0])
    beats = client.get(f"/api/v1/shots/{shot_id}/beats").json()

    assert client.delete(f"/api/v1/shot-beats/{beats[0]['id']}").status_code == 204

    remaining = client.get(f"/api/v1/shots/{shot_id}/beats").json()
    assert [beat["ordinal"] for beat in remaining] == [1, 2]
    assert [beat["start_seconds"] for beat in remaining] == [0.0, 2.0]


# --- 4. 構図の変更 ---


def test_composed_start_frame_wins_over_continuity(client: TestClient, fake_comfy: dict) -> None:
    still_image_id = _still_image(client)
    shot1 = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]
    shot2 = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]
    _generate(client, shot1)

    response = client.post(
        f"/api/v1/shots/{shot2}/frame",
        json={"base_url": "http://127.0.0.1:8190", "composition_prompt": "バストアップに寄る"},
    )
    assert response.status_code == 200, response.text
    shot = client.get(f"/api/v1/shots/{shot2}").json()
    assert shot["has_custom_start_frame"] is True

    fake_comfy["uploads"].clear()
    _generate(client, shot2)
    # 前カットの最終フレーム(10,20,30)ではなく、作り直した起点(2回目の提出=20,40,60)を使う。
    assert _color_of(fake_comfy["uploads"][0]["data"]) == (20, 40, 60)


def test_composition_frame_needs_an_instruction(client: TestClient, fake_comfy: dict) -> None:
    still_image_id = _still_image(client)
    shot_id = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]
    response = client.post(f"/api/v1/shots/{shot_id}/frame", json={})
    assert response.status_code == 409
    assert "構図の変更内容" in response.json()["detail"]


def test_clearing_the_composed_frame_returns_to_continuity(client: TestClient, fake_comfy: dict) -> None:
    still_image_id = _still_image(client)
    shot1 = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]
    shot2 = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]
    _generate(client, shot1)
    client.post(
        f"/api/v1/shots/{shot2}/frame",
        json={"base_url": "http://127.0.0.1:8190", "composition_prompt": "寄る"},
    )

    cleared = client.delete(f"/api/v1/shots/{shot2}/frame/start")
    assert cleared.status_code == 200
    assert cleared.json()["has_custom_start_frame"] is False

    fake_comfy["uploads"].clear()
    _generate(client, shot2)
    assert _color_of(fake_comfy["uploads"][0]["data"]) == (10, 20, 30)


# --- 5. 終端キーフレーム（MiniMax H3 のみ） ---


def test_end_frame_is_wired_only_when_one_exists(client: TestClient, fake_comfy: dict) -> None:
    still_image_id = _still_image(client)
    shot_id = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]

    _generate(client, shot_id)
    # 終端画像が無いときは、任意入力ごと外す（未設定のLoadImageを走らせない）。
    assert "last_frame" not in fake_comfy["workflows"][0]["7"]["inputs"]
    assert "17" not in fake_comfy["workflows"][0]

    client.post(
        f"/api/v1/shots/{shot_id}/frame",
        json={
            "base_url": "http://127.0.0.1:8190",
            "target": "end",
            "composition_prompt": "ドアの前で止まる",
        },
    )
    assert client.get(f"/api/v1/shots/{shot_id}").json()["has_custom_end_frame"] is True

    fake_comfy["workflows"].clear()
    _generate(client, shot_id)
    assert fake_comfy["workflows"][0]["7"]["inputs"]["last_frame"] == ["17", 0]


def test_wan_never_receives_an_end_frame(client: TestClient, fake_comfy: dict) -> None:
    """Wanのテンプレートに終端キーフレームの入力は無い。渡そうとすれば束縛で落ちる。"""
    still_image_id = _still_image(client)
    shot_id = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]
    client.post(
        f"/api/v1/shots/{shot_id}/frame",
        json={
            "base_url": "http://127.0.0.1:8190",
            "target": "end",
            "composition_prompt": "止まる",
        },
    )
    fake_comfy["workflows"].clear()

    _generate(client, shot_id, engine="wan22")

    assert fake_comfy["workflows"][0]["10"]["class_type"] == "WanImageToVideo"


# --- 6. 生成に使う文は、用意できてから見せる ---


def _prompt_state(client: TestClient, shot_id: str, **params) -> dict:
    response = client.get(f"/api/v1/shots/{shot_id}/prompt", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _prepare_prompt(client: TestClient, shot_id: str, engine: str = "minimax_h3") -> dict:
    response = client.post(
        f"/api/v1/shots/{shot_id}/prompt",
        json={"base_url": "http://127.0.0.1:8190", "engine": engine},
    )
    assert response.status_code == 200, response.text
    job = client.get(f"/api/v1/jobs/{response.json()['job_id']}").json()
    assert job["state"] == "completed", job.get("message")
    return _prompt_state(client, shot_id, engine=engine)


def test_prompt_is_hidden_until_it_is_prepared(client: TestClient) -> None:
    """作りかけの文は見せない。出ている文と実際に送られる文を食い違わせないため。"""
    still_image_id = _still_image(client)
    [hana] = _people(client, still_image_id, {"name": "ハナ"})
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots",
        json={"motion_prompt": "she turns around", "speech_lines": [_line(hana, "行こう")]},
    ).json()["id"]

    state = _prompt_state(client, shot_id, engine="minimax_h3")

    assert state["ready"] is False
    assert state["segments"] == []


def test_preparing_the_prompt_shows_what_will_be_sent(client: TestClient, fake_comfy: dict) -> None:
    """動画を作らずに、送る文だけを先に用意して確かめられる。"""
    still_image_id = _still_image(client)
    [hana] = _people(client, still_image_id, {"name": "ハナ"})
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots",
        json={"motion_prompt": "she turns around", "speech_lines": [_line(hana, "行こう")]},
    ).json()["id"]

    state = _prepare_prompt(client, shot_id)

    assert state["ready"] is True
    assert state["timeline_mode"] == "single"
    assert len(state["segments"]) == 1
    prompt = state["segments"][0]["prompt"]
    assert "Second 0–5.2: She turns around." in prompt
    # 見分ける手がかりが無くても、1人しかいなければその人を名指しする。
    assert 'At 0s the only person on screen says "行こう".' in prompt
    assert state["segments"][0]["length_frames"] == 124
    # 動画は作らない（用意したのは文だけ）。
    assert client.get(f"/api/v1/shots/{shot_id}").json()["has_video"] is False


def test_prepared_prompt_splits_per_beat_in_split_mode(client: TestClient, fake_comfy: dict) -> None:
    still_image_id = _still_image(client)
    shot_id = _shot_with_beats(client, still_image_id, [3.0, 6.0])

    state = _prepare_prompt(client, shot_id)

    assert state["timeline_mode"] == "split"
    assert [segment["label"] for segment in state["segments"]] == ["ビート #1", "ビート #2"]
    assert [segment["length_frames"] for segment in state["segments"]] == [73, 158]
    # 区間ごとに投げても、それぞれ0秒から始まる時間付きの形にする。
    assert all("Second 0–" in s["prompt"] for s in state["segments"])


def test_changing_the_direction_hides_the_prepared_prompt(client: TestClient, fake_comfy: dict) -> None:
    """指示を変えたら、前に用意した文はもう使われない。古い文を見せ続けない。"""
    still_image_id = _still_image(client)
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots", json={"motion_prompt": "she waits"}
    ).json()["id"]
    assert _prepare_prompt(client, shot_id)["ready"] is True

    client.patch(f"/api/v1/shots/{shot_id}", json={"motion_prompt": "she runs"})

    state = _prompt_state(client, shot_id, engine="minimax_h3")
    assert state["ready"] is False
    assert state["stale"] is True
    assert state["segments"] == []


def test_changing_the_engine_hides_the_prepared_prompt(client: TestClient, fake_comfy: dict) -> None:
    """作り方ごとに送る文の形が違う。別の作り方向けの文をそのまま見せない。"""
    still_image_id = _still_image(client)
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots", json={"motion_prompt": "she waits"}
    ).json()["id"]
    _prepare_prompt(client, shot_id, engine="minimax_h3")

    assert _prompt_state(client, shot_id, engine="wan22")["stale"] is True


def test_reading_the_prompt_never_triggers_a_generation(client: TestClient, fake_comfy: dict) -> None:
    """画面を開くたびに走る読み取りで、GPUを使わせない。"""
    still_image_id = _still_image(client)
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots", json={"motion_prompt": "ゆっくり振り返る"}
    ).json()["id"]

    _prompt_state(client, shot_id, engine="minimax_h3")

    assert fake_comfy["submits"] == 0


# --- 7. セリフ以外の指示を自動で英語にする ---


def _video_prompts(fake_comfy: dict) -> list[str]:
    return [
        workflow["7"]["inputs"]["prompt"]
        for workflow in fake_comfy["workflows"]
        if "7" in workflow and workflow["7"]["class_type"] == "MiniMaxH3ImageToVideo"
    ]


def test_japanese_directions_are_translated_before_sending(client: TestClient, fake_comfy: dict) -> None:
    """動きの指示は英語で送る。セリフは日本語のまま引用符の中に残す。"""
    still_image_id = _still_image(client)
    [hana] = _people(client, still_image_id, {"name": "ハナ"})
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots",
        json={"motion_prompt": "ゆっくり振り返る", "speech_lines": [_line(hana, "行こう")]},
    ).json()["id"]

    _generate(client, shot_id)

    prompt = _video_prompts(fake_comfy)[0]
    assert "ゆっくり振り返る" not in prompt
    # 文頭は大文字に整えられる。
    assert "english line 1" in prompt.lower()
    # セリフは訳さない（読み上げてほしい言葉そのもの）。
    assert '"行こう"' in prompt


def test_speech_is_never_sent_to_the_translator(client: TestClient, fake_comfy: dict) -> None:
    still_image_id = _still_image(client)
    [hana] = _people(client, still_image_id, {"name": "ハナ", "placement": "左"})
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots",
        json={"motion_prompt": "ゆっくり振り返る", "speech_lines": [_line(hana, "行こう")]},
    ).json()["id"]

    _generate(client, shot_id)

    assert any("ゆっくり振り返る" in text for text in fake_comfy["translated"])
    assert not any("行こう" in text for text in fake_comfy["translated"])


def test_english_directions_skip_the_translator(client: TestClient, fake_comfy: dict) -> None:
    """英語で書かれていれば訳す必要がない（1回ぶんの生成を無駄にしない）。"""
    still_image_id = _still_image(client)
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots",
        json={"motion_prompt": "she slowly turns around"},
    ).json()["id"]

    _generate(client, shot_id)

    assert fake_comfy["translated"] == []


def test_wan_does_not_translate(client: TestClient, fake_comfy: dict) -> None:
    """Wan 2.2 は音を作らないので、日本語のままでも読み上げは起きない。"""
    still_image_id = _still_image(client)
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots",
        json={"motion_prompt": "ゆっくり振り返る"},
    ).json()["id"]

    _generate(client, shot_id, engine="wan22")

    assert fake_comfy["translated"] == []


def test_translation_is_remembered_across_generations(client: TestClient, fake_comfy: dict) -> None:
    """同じ文を何度も訳し直さない（生成のたびに1回ぶん待たされないように）。"""
    still_image_id = _still_image(client)
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots",
        json={"motion_prompt": "ゆっくり振り返る"},
    ).json()["id"]

    _generate(client, shot_id)
    first = len(fake_comfy["translated"])
    _generate(client, shot_id)

    assert len(fake_comfy["translated"]) == first


def test_prepared_prompt_holds_the_translation(client: TestClient, fake_comfy: dict) -> None:
    """用意した文には、実際に送られる英語がそのまま入っている（生成前に読める）。"""
    still_image_id = _still_image(client)
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots",
        json={"motion_prompt": "ゆっくり振り返る"},
    ).json()["id"]

    state = _prepare_prompt(client, shot_id)

    prompt = state["segments"][0]["prompt"]
    assert "ゆっくり振り返る" not in prompt
    assert "english line 1" in prompt.lower()


def test_generating_reuses_the_prepared_prompt(client: TestClient, fake_comfy: dict) -> None:
    """先に用意した文で作る。生成のときに組み立て直して別の文を送らない。"""
    still_image_id = _still_image(client)
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots",
        json={"motion_prompt": "ゆっくり振り返る"},
    ).json()["id"]
    prepared = _prepare_prompt(client, shot_id)["segments"][0]["prompt"]

    _generate(client, shot_id)

    assert _video_prompts(fake_comfy)[-1] == prepared


def test_beat_directions_are_translated_too(client: TestClient, fake_comfy: dict) -> None:
    still_image_id = _still_image(client)
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots", json={"timeline_mode": "single"}
    ).json()["id"]
    [hana] = _people(client, still_image_id, {"name": "ハナ"})
    client.post(
        f"/api/v1/shots/{shot_id}/beats",
        json={
            "duration_seconds": 3.0,
            "motion_prompt": "ゆっくり振り返る",
            "speech_lines": [_line(hana, "行こう")],
        },
    )
    client.post(
        f"/api/v1/shots/{shot_id}/beats",
        json={"duration_seconds": 3.0, "motion_prompt": "歩き出す"},
    )

    _generate(client, shot_id)

    prompt = _video_prompts(fake_comfy)[0]
    assert "ゆっくり振り返る" not in prompt
    assert "歩き出す" not in prompt
    assert '"行こう"' in prompt


# --- 8. 用意した文を直接書き直す ---


def _set_edit_mode(client: TestClient, shot_id: str, enabled: bool, engine: str = "minimax_h3"):
    return client.post(
        f"/api/v1/shots/{shot_id}/prompt/edit-mode",
        params={"engine": engine},
        json={"edit_mode": enabled},
    )


def _write_prompt(client: TestClient, shot_id: str, *segments: str, engine: str = "minimax_h3"):
    return client.patch(
        f"/api/v1/shots/{shot_id}/prompt",
        params={"engine": engine},
        json={"segments": list(segments)},
    )


def test_edit_mode_starts_from_the_prepared_prompt(client: TestClient, fake_comfy: dict) -> None:
    """白紙からではなく、実際に送られる文を直すところから始める。"""
    still_image_id = _still_image(client)
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots", json={"motion_prompt": "she waits"}
    ).json()["id"]
    prepared = _prepare_prompt(client, shot_id)["segments"][0]["prompt"]

    state = _set_edit_mode(client, shot_id, True).json()

    assert state["edit_mode"] is True
    assert state["segments"][0]["prompt"] == prepared


def test_edit_mode_needs_a_prepared_prompt(client: TestClient) -> None:
    still_image_id = _still_image(client)
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots", json={"motion_prompt": "she waits"}
    ).json()["id"]

    assert _set_edit_mode(client, shot_id, True).status_code == 409


def test_written_prompt_is_sent_verbatim(client: TestClient, fake_comfy: dict) -> None:
    """書いた文はそのまま送る（組み立て直さない・訳し直さない）。"""
    still_image_id = _still_image(client)
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots", json={"motion_prompt": "ゆっくり振り返る"}
    ).json()["id"]
    _prepare_prompt(client, shot_id)
    _set_edit_mode(client, shot_id, True)
    assert _write_prompt(client, shot_id, "A woman squats. No dialogue.").status_code == 200
    fake_comfy["translated"].clear()

    _generate(client, shot_id)

    assert _video_prompts(fake_comfy)[-1] == "A woman squats. No dialogue."
    assert fake_comfy["translated"] == []


def test_edit_mode_locks_the_directions_behind_it(client: TestClient, fake_comfy: dict) -> None:
    """書き直している間に指示を変えられると、画面の指示と送る文が食い違う。"""
    still_image_id = _still_image(client)
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots", json={"motion_prompt": "she waits"}
    ).json()["id"]
    _prepare_prompt(client, shot_id)
    _set_edit_mode(client, shot_id, True)

    assert client.patch(f"/api/v1/shots/{shot_id}", json={"motion_prompt": "she runs"}).status_code == 409
    assert client.post(
        f"/api/v1/shots/{shot_id}/beats", json={"duration_seconds": 2.0, "motion_prompt": "x"}
    ).status_code == 409
    # 構図は起点の絵を作るためのもので、送る文には入らないので変えられる。
    assert client.patch(
        f"/api/v1/shots/{shot_id}", json={"composition_prompt": "バストアップに寄る"}
    ).status_code == 200


def test_leaving_edit_mode_returns_to_the_prepared_prompt(client: TestClient, fake_comfy: dict) -> None:
    """書き直しをやめたら、指示から作った文へ戻る（どちらが使われるかを曖昧にしない）。"""
    still_image_id = _still_image(client)
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots", json={"motion_prompt": "she waits"}
    ).json()["id"]
    prepared = _prepare_prompt(client, shot_id)["segments"][0]["prompt"]
    _set_edit_mode(client, shot_id, True)
    _write_prompt(client, shot_id, "A woman squats.")

    state = _set_edit_mode(client, shot_id, False).json()

    assert state["edit_mode"] is False
    assert state["segments"][0]["prompt"] == prepared
    # 指示も変えられるように戻る。
    assert client.patch(f"/api/v1/shots/{shot_id}", json={"motion_prompt": "she runs"}).status_code == 200


def test_writing_needs_edit_mode(client: TestClient, fake_comfy: dict) -> None:
    still_image_id = _still_image(client)
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots", json={"motion_prompt": "she waits"}
    ).json()["id"]
    _prepare_prompt(client, shot_id)

    assert _write_prompt(client, shot_id, "A woman squats.").status_code == 409


def test_generating_prepares_the_prompt_and_keeps_it(client: TestClient, fake_comfy: dict) -> None:
    """生成を押しただけでも文は用意される。作ったあとに、実際に送った文を読める。"""
    still_image_id = _still_image(client)
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots", json={"motion_prompt": "ゆっくり振り返る"}
    ).json()["id"]

    _generate(client, shot_id)

    state = _prompt_state(client, shot_id, engine="minimax_h3")
    assert state["ready"] is True
    assert state["segments"][0]["prompt"] == _video_prompts(fake_comfy)[-1]


# --- 9. カットの覚え書き（生成には渡さない） ---


def test_note_never_reaches_the_prompt(client: TestClient, fake_comfy: dict) -> None:
    """覚え書きは利用者のためのもの。生成へ渡すと、書いた覚えのない指示として効く。"""
    still_image_id = _still_image(client)
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots",
        json={"motion_prompt": "she waits", "note": "ここは主人公の心変わりの場面。あとで撮り直す"},
    ).json()["id"]

    _generate(client, shot_id)

    prompt = _video_prompts(fake_comfy)[-1]
    assert "心変わり" not in prompt
    assert "撮り直す" not in prompt
    # 訳す対象にもしない（訳せば1回ぶんの生成を無駄に使う）。
    assert not any("心変わり" in text for text in fake_comfy["translated"])


def test_note_does_not_invalidate_the_prepared_prompt(client: TestClient, fake_comfy: dict) -> None:
    """覚え書きを書き足しただけで、用意した文を作り直させない。"""
    still_image_id = _still_image(client)
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots", json={"motion_prompt": "she waits"}
    ).json()["id"]
    _prepare_prompt(client, shot_id)

    client.patch(f"/api/v1/shots/{shot_id}", json={"note": "夕方の光で撮り直したい"})

    state = _prompt_state(client, shot_id, engine="minimax_h3")
    assert state["ready"] is True
    assert state["stale"] is False


def test_prompt_prepared_by_an_older_format_is_rebuilt(
    client: TestClient, fake_comfy: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """文の組み立て方を変えたら、指示が同じでも、前の組み立て方で用意した文は使わない。

    使い回すと、組み立て方の直し（起点の画像から始めさせる一文など）が、文を用意済みの
    カットにだけ効かない。
    """
    still_image_id = _still_image(client)
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots", json={"motion_prompt": "she waits"}
    ).json()["id"]
    _prepare_prompt(client, shot_id)

    monkeypatch.setattr(shots, "PROMPT_FORMAT_VERSION", shots.PROMPT_FORMAT_VERSION + 1)

    assert _prompt_state(client, shot_id, engine="minimax_h3")["stale"] is True
    _generate(client, shot_id)
    assert _prompt_state(client, shot_id, engine="minimax_h3")["stale"] is False


def test_h3_video_opens_on_the_start_image(client: TestClient, fake_comfy: dict) -> None:
    """動きだけを書いたカットでも、起点の画像の場面から始めるよう言い切って送る。"""
    still_image_id = _still_image(client)
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots", json={"motion_prompt": "she waits"}
    ).json()["id"]

    _generate(client, shot_id)

    assert OPENING_FRAME_DIRECTIVE in _video_prompts(fake_comfy)[-1]


def test_note_stays_writable_while_the_prompt_is_locked(client: TestClient, fake_comfy: dict) -> None:
    """覚え書きは文のもとにならないので、文を書き直している間も書ける。"""
    still_image_id = _still_image(client)
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots", json={"motion_prompt": "she waits"}
    ).json()["id"]
    _prepare_prompt(client, shot_id)
    _set_edit_mode(client, shot_id, True)

    response = client.patch(f"/api/v1/shots/{shot_id}", json={"note": "この文で1回試す"})

    assert response.status_code == 200
    assert response.json()["note"] == "この文で1回試す"


# --- 10. カットを消しても、他のカットの持ち物を巻き添えにしない ---


def test_deleting_a_shot_keeps_the_previous_shots_last_frame(client: TestClient, fake_comfy: dict) -> None:
    """回帰: カット2を消したらカット1の最終フレームまで消えていた。

    `start_frame_image_path` は「何を起点に使ったか」の記録で、実体は前のカットのもの。
    削除で巻き添えにすると、カットを1つ消しただけで連続性が壊れる（実機で発生）。
    """
    still_image_id = _still_image(client)
    shot1 = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]
    shot2 = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]
    _generate(client, shot1)
    _generate(client, shot2)
    assert client.get(f"/api/v1/shots/{shot1}").json()["has_last_frame"] is True

    assert client.delete(f"/api/v1/shots/{shot2}").status_code == 204

    # カット1の最終フレームは実体ごと残る。
    assert (main._SHOT_LAST_FRAME_ROOT / f"{shot1}.png").is_file()
    assert client.get(f"/api/v1/shots/{shot1}").json()["has_last_frame"] is True


def test_recreated_shot_still_continues_from_the_previous_one(client: TestClient, fake_comfy: dict) -> None:
    """カットを消して作り直しても、前のカットの続きから始まる。"""
    still_image_id = _still_image(client)
    shot1 = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]
    shot2 = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]
    _generate(client, shot1)
    _generate(client, shot2)
    client.delete(f"/api/v1/shots/{shot2}")

    again = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]

    shot = client.get(f"/api/v1/shots/{again}").json()
    assert shot["start_frame_source"] == "previous"
    response = client.get(f"/api/v1/shots/{again}/start-frame")
    assert response.status_code == 200
    assert _color_of(response.content) == (10, 20, 30)


def test_start_frame_falls_back_when_the_file_is_gone(client: TestClient, fake_comfy: dict) -> None:
    """道が残っていても実体が無ければ、その出どころは数えない。

    数えてしまうと、「前カットの最終フレーム」と表示したまま、画像の取得だけ404になる。
    """
    still_image_id = _still_image(client)
    shot1 = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]
    shot2 = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]
    _generate(client, shot1)
    assert client.get(f"/api/v1/shots/{shot2}").json()["start_frame_source"] == "previous"

    # 何らかの理由で実体だけ失われた状態を作る。
    (main._SHOT_LAST_FRAME_ROOT / f"{shot1}.png").unlink()

    shot = client.get(f"/api/v1/shots/{shot2}").json()
    assert shot["start_frame_source"] == "still_image"
    # 表示も生成も、同じ1枚（シーンの静止画）へ揃う。
    response = client.get(f"/api/v1/shots/{shot2}/start-frame")
    assert response.status_code == 200
    assert response.content == _TINY_PNG


def test_deleting_a_shot_still_removes_its_own_files(client: TestClient, fake_comfy: dict) -> None:
    """自分の持ち物（動画・最終フレーム）は今までどおり片付ける。"""
    still_image_id = _still_image(client)
    shot_id = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]
    _generate(client, shot_id)
    video = main._SHOT_VIDEO_ROOT / f"{shot_id}.mp4"
    last_frame = main._SHOT_LAST_FRAME_ROOT / f"{shot_id}.png"
    assert video.is_file() and last_frame.is_file()

    client.delete(f"/api/v1/shots/{shot_id}")

    assert not video.exists()
    assert not last_frame.exists()


def test_missing_files_are_reported_as_missing(client: TestClient, fake_comfy: dict) -> None:
    """道だけ残って実体が消えている記録を「あり」と言わない。

    「動画あり」なのに再生できない、「続きから始められる」のに繋がらない、を防ぐ。
    """
    still_image_id = _still_image(client)
    shot_id = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]
    _generate(client, shot_id)
    assert client.get(f"/api/v1/shots/{shot_id}").json()["has_video"] is True

    (main._SHOT_VIDEO_ROOT / f"{shot_id}.mp4").unlink()
    (main._SHOT_LAST_FRAME_ROOT / f"{shot_id}.png").unlink()

    shot = client.get(f"/api/v1/shots/{shot_id}").json()
    assert shot["has_video"] is False
    assert shot["has_last_frame"] is False


def test_start_frame_is_not_cached_as_immutable(client: TestClient, fake_comfy: dict) -> None:
    """起点は前のカットを作り直せば変わる。変わらない絵として覚えさせない。

    immutable を付けると、ラベルは「前のカットの最終フレーム」なのに
    表示だけ古い静止画のまま、という食い違いが起きる。
    """
    still_image_id = _still_image(client)
    shot_id = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]

    response = client.get(f"/api/v1/shots/{shot_id}/start-frame")

    assert response.status_code == 200
    assert "immutable" not in response.headers.get("cache-control", "")


def test_start_frame_follows_the_previous_shot_being_generated(client: TestClient, fake_comfy: dict) -> None:
    """前のカットを生成したら、同じURLでも新しい起点が返る。"""
    still_image_id = _still_image(client)
    shot1 = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]
    shot2 = client.post(f"/api/v1/still-images/{still_image_id}/shots", json={}).json()["id"]

    before = client.get(f"/api/v1/shots/{shot2}/start-frame")
    assert before.content == _TINY_PNG
    assert before.headers["X-Start-Frame-Source"] == "still_image"

    _generate(client, shot1)

    after = client.get(f"/api/v1/shots/{shot2}/start-frame")
    assert after.headers["X-Start-Frame-Source"] == "previous"
    assert _color_of(after.content) == (10, 20, 30)


# --- 10. 音を出さない指定と、欄ごとの N/A ---


def test_silent_shot_sends_the_full_silence_directive(client: TestClient, fake_comfy: dict) -> None:
    """セリフを空にしたカットには、人物を黙らせる指定を付けて送る。"""
    still_image_id = _still_image(client)
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots",
        json={"motion_prompt": "squat with hands clasped behind head"},
    ).json()["id"]

    _generate(client, shot_id)

    prompt = _video_prompts(fake_comfy)[0]
    assert "All visible people remain completely silent" in prompt
    assert "lips remain naturally closed" in prompt
    assert "No human voices or vocal sounds of any kind." in prompt
    assert prompt.rstrip().endswith("non_diegetic_music:\nN/A")


def test_music_field_is_filled_when_asked_for(client: TestClient, fake_comfy: dict) -> None:
    still_image_id = _still_image(client)
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots",
        json={"motion_prompt": "she waits", "music_prompt": "quiet piano"},
    ).json()["id"]

    _generate(client, shot_id)

    prompt = _video_prompts(fake_comfy)[0]
    assert "non_diegetic_music:\nQuiet piano." in prompt


def test_music_is_translated_but_speech_is_not(client: TestClient, fake_comfy: dict) -> None:
    still_image_id = _still_image(client)
    [hana] = _people(client, still_image_id, {"name": "ハナ"})
    shot_id = client.post(
        f"/api/v1/still-images/{still_image_id}/shots",
        json={
            "motion_prompt": "she waits",
            "music_prompt": "静かなピアノ",
            "speech_lines": [_line(hana, "行こう")],
        },
    ).json()["id"]

    _generate(client, shot_id)

    assert any("静かなピアノ" in text for text in fake_comfy["translated"])
    assert not any("行こう" in text for text in fake_comfy["translated"])


# --- 作り方を切り替えたとき、使わなくなった動画を残さない ---


def test_single_generation_after_split_drops_the_segments(
    client: TestClient, fake_comfy: dict
) -> None:
    """1本で作り直したら、前に分割で作った区間は使わないので消す。"""
    still_image_id = _still_image(client)
    shot_id = _shot_with_beats(client, still_image_id, [3.0, 3.0])
    _generate(client, shot_id)
    beat_ids = [beat["id"] for beat in client.get(f"/api/v1/shots/{shot_id}/beats").json()]
    segments = [main._SHOT_SEGMENT_ROOT / f"{beat_id}.mp4" for beat_id in beat_ids]
    beat_frames = [main._SHOT_LAST_FRAME_ROOT / f"{beat_id}.png" for beat_id in beat_ids]
    assert all(path.is_file() for path in segments + beat_frames)

    _generate(client, shot_id, timeline_mode="single")

    beats = client.get(f"/api/v1/shots/{shot_id}/beats").json()
    assert not any(beat["has_segment_video"] for beat in beats)
    assert not any(path.exists() for path in segments + beat_frames)
    shot = client.get(f"/api/v1/shots/{shot_id}").json()
    assert shot["has_video"] is True


def test_split_without_ffmpeg_drops_the_previous_single_video(
    client: TestClient, fake_comfy: dict
) -> None:
    """区間のまま残すときは、前に1本で作った動画を外して消す（古い方が使われないように）。"""
    still_image_id = _still_image(client)
    shot_id = _shot_with_beats(client, still_image_id, [3.0, 3.0])
    _generate(client, shot_id, timeline_mode="single")
    single_video = main._SHOT_VIDEO_ROOT / f"{shot_id}.mp4"
    single_last = main._SHOT_LAST_FRAME_ROOT / f"{shot_id}.png"
    assert single_video.is_file() and single_last.is_file()

    _generate(client, shot_id, timeline_mode="split")

    shot = client.get(f"/api/v1/shots/{shot_id}").json()
    assert shot["has_video"] is False
    assert not single_video.exists()
    # カットの最終フレームは最後のビートのものになり、1本のときのものは残らない。
    assert not single_last.exists()
    beats = client.get(f"/api/v1/shots/{shot_id}/beats").json()
    assert all(beat["has_segment_video"] for beat in beats)
