"""カットの動画生成: プロンプトの組み立てと、ワークフローの枝刈り。

このアプリのカットは「動きの指示」だけでなく、セリフ・環境音・時間の割り振り（ビート）
を別々に持つ。MiniMax H3 は1本のテキストで映像と音声の両方を条件付けるため、組み立て方を
外すと、映像の説明文がそのまま読み上げられる。

書き方は ComfyUI 同梱の公式テンプレート
（`comfyui_workflow_templates_json/templates/video_minimax_h3_*.json`）に合わせる。
同梱の説明文が言うとおり、**セリフは映像の記述ではなく音の記述（`Audio:`）に属する**
（"describe the shots, motion, and the accompanying audio (dialogue, SFX, music) in one block"）。
時刻の指定も `Audio:` の中で `at 4s` の形で書く（公式t2vの
"the score bursting at 4s, closing the last 1s" と同じ）。

- 1行目は場面全体の説明。ここが「映像の記述」の足場になる
- 時間分割は ``Second 0–3:`` の行にし、**映像のことだけ**を書く。区間が1つでもこの形にする
  （尺が分かっているなら常に時間付きで渡す）
- セリフは ``At 0s the young woman on the left says "…"`` の形で、映像の記述の後半へまとめる。
  **話す人は見た目と配置で名指しする**（公式r2vの "the little boy superhero … delivers his
  line" と同じ）。名前は使わない——日本語の名前はセリフと区別されず読み上げられる。
  同じ区間に続くセリフは ``Then … says "…"`` で順に繋ぐ。ナレーションは画面の外の声とし、
  画面の人物が口を動かさないことを添える
- **``Audio:`` 行は、利用者が音を指定したときだけ出す**（この行に書いた文字は
  読み上げられることがある。無音のつもりの一言でも喋った）
- 否定はプロンプトの最後に並べる（公式t2vの "No text, no logos, no dialogue."）。
  **声を入れないときの no dialogue は、ここに置く。**``Audio:`` の中へ入れると、
  書いていないセリフを喋ることがある（実機で確認）

**言語の問題（実機で2度外した）**: 公式テンプレートのプロンプトは全編英語で、CJK文字は
1つも無い。動きの指示とセリフの両方が日本語だと、引用符だけでは区別の手がかりにならず、
モデルは日本語の断片を順に読み上げてしまう（尺に入るだけ読む）。日本語はセリフ欄だけに
留め、動きの指示は英語で書くのが安全。画面でもそう案内している。

独立行に置いた ``The character says: "…"``、および参照動画用の ``<T.T seconds>`` ラベル
（`comfy/text_encoders/minimax.py`）は、どちらも実機で外れを確認済みなので戻さない。

Wan 2.2 は音を作らず、この構造も知らないので、動きの記述だけを素の文として渡す。
時間配分を確実に守らせたい場合は、ビートごとに区間を分けて生成する（分割モード）。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

# MiniMax H3 テンプレートの、終端キーフレーム用ノード。
# 終端画像を指定しないときは、このノードごと外す（`last_frame` は任意入力のため）。
_H3_END_IMAGE_NODE = "17"
_H3_VIDEO_NODE = "7"
_H3_END_IMAGE_INPUT = "last_frame"

# 動きの指示が空のときの既定。
DEFAULT_MOTION_PROMPT = "the scene comes to life with gentle, natural motion"
DEFAULT_VIDEO_NEGATIVE = (
    "static, still, frozen, jitter, flicker, distortion, warping, extra limbs, low quality"
)
# 末尾の否定。H3には否定条件の入力が無いので、プロンプトの最後に並べる。
# 公式t2vテンプレートの "No text, no logos, no dialogue." と同じ書き方にする。
# 声を入れないカットでは、ここに no dialogue を並べる——音の記述（Audio:）の中へ
# 入れると、書いていないセリフを喋ることがある（実機で確認）。
NO_SPEECH_DIRECTIVE = "No text, no logos, no dialogue."
# `Audio:` 行は、利用者が音を指定したときだけ出す。こちらから埋めない——
# `Audio:` の後ろに書いた文字は読み上げられることがある（実機で確認。無音のつもりの
# "ambient background sound only" でも喋った）。公式t2vの無音例にも `Audio:` 行は無い。
# 1カットが「切り替わらない1続きの映像」であることを先に置く足場。
CONTINUOUS_SHOT_PREFIX = "Single continuous shot, no cuts."

# H3のプロンプトは、欄に分けた形で書く。使わない欄には ``N/A`` を置く
# （空にするより、要らないと言い切ったほうが効く）。
FIELD_DESCRIPTION = "integrated_multimodal_description:"
FIELD_SOUNDSCAPE = "overall_soundscape:"
FIELD_MUSIC = "non_diegetic_music:"
NOT_APPLICABLE = "N/A"

# 声を出させないための指定。「唇を閉じたまま」「リップシンクの動きなし」まで書き、
# 発話の種類を数え上げる。``No dialogue.`` だけでは、書いていないセリフを喋った。
SILENCE_DIRECTIVE = (
    "All visible people remain completely silent throughout the video.\n"
    "Their lips remain naturally closed with no speaking or lip-sync motion.\n"
    "There is no dialogue, narration, voice-over, off-screen speech, "
    "whispering, singing, humming, or other human vocalization."
)
# ナレーションの話し手。画面の人物ではなく、画面の外の声として扱う。
NARRATOR = "an off-screen narrator"
# 話す人が分からない行（人物を消した・静止画から外した）の名指し。
UNKNOWN_SPEAKER = "one of the characters"
# ナレーションだけのとき、画面の人物に口を動かさせない。
NARRATION_ONLY_DIRECTIVE = (
    "All visible people remain silent with their lips closed; "
    "the narration is an off-screen voice-over."
)
# 人物のセリフとナレーションが混ざるとき、ナレーションに口を合わせさせない。
NARRATION_MIXED_DIRECTIVE = (
    "The narration is an off-screen voice-over; no one on screen lip-syncs to it."
)
# 音の欄に置く、声を排する一言。
SOUNDSCAPE_HEADER = "Natural environmental and physical sounds only."
NO_VOICE_DIRECTIVE = "No human voices or vocal sounds of any kind."

# エンジンごとのフレーム刻み（`length` の制約）と既定のfps。
# Wan 2.2 は 4k+1 フレームを16fpsで、MiniMax H3 は 17k+5 フレームを24fpsで扱う。
_FRAME_GRIDS: dict[str, dict[str, float | int]] = {
    "wan22": {"modulus": 4, "remainder": 1, "fps": 16.0},
    "minimax_h3": {"modulus": 17, "remainder": 5, "fps": 24.0},
}
# `ShotGenerateRequest.length` が受け付ける範囲。
MIN_FRAMES = 5
MAX_FRAMES = 362


@dataclass(frozen=True)
class SpokenLine:
    """セリフ・ナレーションの1行。`speaker` は英語の名指し（"the young woman on the left"）。"""

    text: str
    speaker: str = ""
    narration: bool = False


@dataclass(frozen=True)
class Beat:
    """カットの中の1区間。時間の起点は前のビートの終わりで、長さで表す。"""

    duration_seconds: float
    motion_prompt: str = ""
    speech_lines: tuple[SpokenLine, ...] = ()


def frames_for_seconds(engine: str, seconds: float) -> int:
    """尺（秒）を、そのエンジンが受け付けるフレーム数へ切り上げる。

    刻みに合わない値を投げるとComfyUI側で落ちるため、必ずここを通す。
    """
    grid = _FRAME_GRIDS[engine]
    frames = round(max(0.0, seconds) * float(grid["fps"]))
    modulus = int(grid["modulus"])
    remainder = int(grid["remainder"])
    if frames < MIN_FRAMES:
        frames = MIN_FRAMES
    while frames % modulus != remainder:
        frames += 1
    return min(frames, MAX_FRAMES)


def seconds_for_frames(engine: str, frames: int) -> float:
    """フレーム数から実際の尺（秒）を返す。ビートの時刻ラベルを実尺へ合わせるのに使う。"""
    return frames / float(_FRAME_GRIDS[engine]["fps"])


def _speaker_phrase(line: SpokenLine) -> str:
    """文の途中に置ける名指しへ整える（訳は "The young woman …." の形で返ることがある）。"""
    if line.narration:
        return NARRATOR
    phrase = line.speaker.strip().rstrip(".").strip()
    if not phrase:
        return UNKNOWN_SPEAKER
    head, _, rest = phrase.partition(" ")
    if head.lower() in {"the", "a", "an"}:
        return f"{head.lower()} {rest}".strip()
    return f"the {phrase[:1].lower()}{phrase[1:]}"


def _spoken_lines(lines: tuple[SpokenLine, ...], at_seconds: float | None) -> list[str]:
    """1区間ぶんのセリフ。最初の行に時刻を付け、続く行は ``Then`` で順に繋ぐ。

    時刻の書き方は、公式テンプレートが音の出来事を ``the score bursting at 4s`` と
    書くのに合わせる。
    """
    sentences: list[str] = []
    for line in lines:
        text = line.text.strip().strip("「」\"\u201c\u201d'")
        if not text:
            continue
        said = f'{_speaker_phrase(line)} says "{text}"'
        if sentences:
            sentences.append(f"Then {said}")
        elif at_seconds is None:
            sentences.append(_sentence(said))
        else:
            sentences.append(f"At {_format_seconds(at_seconds)}s {said}")
    return sentences


def _sentence(text: str) -> str:
    """文として置けるように、先頭を大文字にする（利用者は小文字で書き始めることが多い）。"""
    stripped = text.strip()
    return stripped[:1].upper() + stripped[1:] if stripped else stripped


def _format_seconds(value: float) -> str:
    """時刻の見せ方（3.0→"3"、2.5→"2.5"）。公式テンプレートの書き方に合わせる。"""
    return f"{value:.1f}".rstrip("0").rstrip(".") or "0"


def build_shot_prompt(
    *,
    engine: str,
    beats: list[Beat],
    motion_prompt: str = "",
    speech_lines: tuple[SpokenLine, ...] = (),
    ambience_prompt: str = "",
    music_prompt: str = "",
    duration_seconds: float = 0.0,
) -> str:
    """カット1つぶんのプロンプトを組み立てる。

    `beats` が空なら、カット自身の `motion_prompt` / `speech_lines` を1区間として扱う。
    MiniMax H3 では欄に分けた形にし、区間は ``Second A–B:`` の行へ割る。
    Wan 2.2 は音を作らず、この構造も知らないので、動きの記述だけを素の文として渡す。
    """
    if not beats:
        beats = [
            Beat(
                duration_seconds=duration_seconds,
                motion_prompt=motion_prompt,
                speech_lines=speech_lines,
            )
        ]

    motions = [beat.motion_prompt.strip() for beat in beats]
    if not any(motions):
        motions[0] = DEFAULT_MOTION_PROMPT
    motions = [motion or "the motion continues" for motion in motions]

    if engine != "minimax_h3":
        # Wanは1本の素の文にする。区間があれば順序が分かるように繋ぐだけ。
        return ". Then, ".join(motions) if len(motions) > 1 else _sentence(motions[0])

    # H3は常に時間付きの形で動かす。区間が1つでも ``Second 0–5:`` の行にする。
    labelled = any(beat.duration_seconds > 0 for beat in beats)
    total = sum(max(0.0, beat.duration_seconds) for beat in beats)

    description: list[str] = ["[Shot 1]"]
    spoken: list[str] = []
    if labelled:
        description.append(
            f"{CONTINUOUS_SHOT_PREFIX} Choreography for one unbroken "
            f"{_format_seconds(total)}-second arc:"
        )
        start = 0.0
        for beat, motion in zip(beats, motions):
            end = start + max(0.0, beat.duration_seconds)
            description.append(
                f"Second {_format_seconds(start)}\u2013{_format_seconds(end)}: "
                f"{_sentence(motion).rstrip('.')}."
            )
            spoken.extend(_spoken_lines(beat.speech_lines, start))
            start = end
    else:
        description.append(f"{CONTINUOUS_SHOT_PREFIX} {_sentence(motions[0]).rstrip('.')}.")
        spoken.extend(_spoken_lines(beats[0].speech_lines, None))

    if spoken:
        # セリフは映像と同じ欄へ置く（欄の名前どおり、映像と音を1つにまとめた記述）。
        description.append("")
        description.extend(f"{line}." for line in spoken)
        voiced = [line for beat in beats for line in beat.speech_lines if line.text.strip()]
        if all(line.narration for line in voiced):
            description.append(NARRATION_ONLY_DIRECTIVE)
        elif any(line.narration for line in voiced):
            description.append(NARRATION_MIXED_DIRECTIVE)
    else:
        description.append("")
        description.append(SILENCE_DIRECTIVE)

    soundscape: list[str] = []
    if ambience_prompt.strip():
        soundscape.append(SOUNDSCAPE_HEADER)
        soundscape.append(_sentence(ambience_prompt.strip()).rstrip(".") + ".")
        if not spoken:
            soundscape.append(NO_VOICE_DIRECTIVE)
    elif not spoken:
        soundscape.append(SOUNDSCAPE_HEADER)
        soundscape.append(NO_VOICE_DIRECTIVE)
    else:
        soundscape.append(NOT_APPLICABLE)

    music = (
        _sentence(music_prompt.strip()).rstrip(".") + "."
        if music_prompt.strip()
        else NOT_APPLICABLE
    )

    return "\n".join(
        [
            FIELD_DESCRIPTION,
            *description,
            "",
            FIELD_SOUNDSCAPE,
            *soundscape,
            "",
            FIELD_MUSIC,
            music,
        ]
    )


def prune_end_image(workflow: dict[str, Any]) -> dict[str, Any]:
    """終端キーフレームを使わない生成のために、その入力ごと取り除く。

    `MiniMaxH3ImageToVideo` の `last_frame` は任意入力なので、繋いだままにすると
    存在しない画像を読みにいってしまう。使うときだけ残す。
    """
    pruned = deepcopy(workflow)
    pruned.pop(_H3_END_IMAGE_NODE, None)
    node = pruned.get(_H3_VIDEO_NODE)
    if isinstance(node, dict):
        node.get("inputs", {}).pop(_H3_END_IMAGE_INPUT, None)
    bindings = pruned.get("_chotto_sashie_bindings")
    if isinstance(bindings, dict):
        bindings.pop("end_image", None)
    return pruned


def build_recompose_prompt(instruction: str, illustration_style: str = "") -> str:
    """構図・カメラを変えた1枚を作るための、画像編集プロンプトを組み立てる。

    直前のカットの最終フレーム（または元の静止画）を1枚だけ渡し、「同じ人物・同じ服装・
    同じ場所のまま、見え方だけを変える」ことを明示する。ここを曖昧にすると別人や別の
    場所になり、カットを分けた意味が無くなる。
    """
    lines = [
        "Image 1 is the current frame of this scene.",
        "Re-frame the same moment as a new camera setup.",
    ]
    if instruction.strip():
        lines.append(f"New framing: {instruction.strip()}.")
    lines.append(
        "Keep the same characters, faces, hairstyles, hair colors, eye colors, outfits and "
        "the same location, time of day and lighting. Change only the camera: framing, shot "
        "size, camera angle and viewpoint. Do not add or remove characters, and do not change "
        "anyone's identity or clothing."
    )
    lines.append(
        "Render it in the same visual style as the reference image "
        "(photographic stays photographic, illustrated stays illustrated)."
    )
    if illustration_style.strip():
        lines.append(f"作品全体の画風: {illustration_style.strip()}")
    return " ".join(lines)
