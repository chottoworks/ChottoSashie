"""カットのプロンプト組み立てと、フレーム数の刻み。

MiniMax H3 は1本のテキストで映像と音声の両方を条件付ける。組み立て方を外すと、動きの
説明文がそのままナレーションとして読み上げられる（実機で発生）。書き方は
ComfyUI同梱の公式テンプレートに合わせてあるので、文の形そのものを試験で押さえる。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chotto_sashie_api.shot_video import (
    CONTINUOUS_SHOT_PREFIX,
    DEFAULT_MOTION_PROMPT,
    FIELD_DESCRIPTION,
    FIELD_MUSIC,
    FIELD_SOUNDSCAPE,
    MAX_FRAMES,
    NARRATION_MIXED_DIRECTIVE,
    NARRATION_ONLY_DIRECTIVE,
    NOT_APPLICABLE,
    NO_VOICE_DIRECTIVE,
    OPENING_FRAME_DIRECTIVE,
    SILENCE_DIRECTIVE,
    UNKNOWN_SPEAKER,
    Beat,
    SpokenLine,
    build_recompose_prompt,
    build_shot_prompt,
    frames_for_seconds,
    prune_end_image,
    seconds_for_frames,
)


_FIELD_LABELS = (FIELD_DESCRIPTION, FIELD_SOUNDSCAPE, FIELD_MUSIC)

_HER = "the young woman on the left"
_HIM = "the young man on the right"


def _says(text: str, speaker: str = _HER) -> tuple[SpokenLine, ...]:
    return (SpokenLine(text=text, speaker=speaker),)


def _field(prompt: str, label: str) -> str:
    """欄の中身を取り出す（次の欄の見出しまで。欄の中の空行は保つ）。"""
    lines = prompt.splitlines()
    assert label in lines, f"{label} が無い: {prompt}"
    start = lines.index(label) + 1
    end = next(
        (index for index in range(start, len(lines)) if lines[index] in _FIELD_LABELS),
        len(lines),
    )
    return "\n".join(lines[start:end]).strip("\n")


def test_prompt_uses_the_three_fields() -> None:
    """H3のプロンプトは欄に分けた形にする（映像＋音／環境音／劇伴）。"""
    prompt = build_shot_prompt(engine="minimax_h3", beats=[Beat(5.0, "she waits")])
    assert prompt.startswith(FIELD_DESCRIPTION)
    assert FIELD_SOUNDSCAPE in prompt
    assert FIELD_MUSIC in prompt
    assert "[Shot 1]" in prompt


def test_silent_shot_spells_out_how_to_stay_silent() -> None:
    """回帰: ``No dialogue.`` だけでは、書いていないセリフを喋った。

    唇の動きまで含めて言い切り、発話の種類を数え上げる形にする。
    """
    prompt = build_shot_prompt(engine="minimax_h3", beats=[Beat(5.0, "she waits")])
    assert SILENCE_DIRECTIVE in prompt
    assert "lips remain naturally closed" in prompt
    assert _field(prompt, FIELD_SOUNDSCAPE).endswith(NO_VOICE_DIRECTIVE)


def test_unused_fields_say_not_applicable() -> None:
    """使わない欄は空にせず ``N/A`` と書く（要らないと言い切ったほうが効く）。"""
    prompt = build_shot_prompt(engine="minimax_h3", beats=[Beat(5.0, "she waits", _says("行こう"))])
    assert _field(prompt, FIELD_SOUNDSCAPE) == NOT_APPLICABLE
    assert _field(prompt, FIELD_MUSIC) == NOT_APPLICABLE


def test_music_goes_to_its_own_field() -> None:
    prompt = build_shot_prompt(
        engine="minimax_h3", beats=[Beat(5.0, "she waits")], music_prompt="quiet piano"
    )
    assert _field(prompt, FIELD_MUSIC) == "Quiet piano."


def test_ambience_goes_to_the_soundscape_field() -> None:
    prompt = build_shot_prompt(
        engine="minimax_h3",
        beats=[Beat(5.0, "she waits")],
        ambience_prompt="rain on the window",
    )
    soundscape = _field(prompt, FIELD_SOUNDSCAPE)
    assert "Rain on the window." in soundscape
    # 声を入れないカットなので、環境音があっても声は排する。
    assert NO_VOICE_DIRECTIVE in soundscape


def test_speech_keeps_the_silence_directive_away() -> None:
    """セリフがあるカットで無音指定を出すと、指定した声まで消えかねない。"""
    prompt = build_shot_prompt(engine="minimax_h3", beats=[Beat(5.0, "she waits", _says("行こう"))])
    assert SILENCE_DIRECTIVE not in prompt
    assert NO_VOICE_DIRECTIVE not in prompt
    assert f'At 0s {_HER} says "行こう".' in _field(prompt, FIELD_DESCRIPTION)


def test_speech_is_time_anchored() -> None:
    prompt = build_shot_prompt(
        engine="minimax_h3",
        beats=[
            Beat(duration_seconds=3.0, motion_prompt="she turns around", speech_lines=_says("行こう")),
            Beat(
                duration_seconds=3.0,
                motion_prompt="he walks away",
                speech_lines=_says("待ってて", _HIM),
            ),
        ],
    )
    description = _field(prompt, FIELD_DESCRIPTION)
    assert f'At 0s {_HER} says "行こう".' in description
    assert f'At 3s {_HIM} says "待ってて".' in description


def test_two_speakers_in_one_shot_are_named_in_order() -> None:
    """二人の掛け合いは、1区間の中で話す人を名指しして順に並べる。"""
    prompt = build_shot_prompt(
        engine="minimax_h3",
        beats=[],
        motion_prompt="they face each other",
        speech_lines=(
            SpokenLine(text="行こう", speaker=_HER),
            SpokenLine(text="「待って」", speaker=_HIM),
            SpokenLine(text="早く", speaker=_HER),
        ),
        duration_seconds=5.0,
    )
    lines = _field(prompt, FIELD_DESCRIPTION).splitlines()
    assert lines[-3:] == [
        f'At 0s {_HER} says "行こう".',
        f'Then {_HIM} says "待って".',
        f'Then {_HER} says "早く".',
    ]


def test_translated_speaker_phrase_fits_mid_sentence() -> None:
    """訳は "The young woman …." の形で返ることがある。文の途中に置ける形へ整える。"""
    prompt = build_shot_prompt(
        engine="minimax_h3",
        beats=[Beat(5.0, "she waits", _says("行こう", "The young woman on the left."))],
    )
    assert f'At 0s {_HER} says "行こう".' in prompt
    prompt = build_shot_prompt(
        engine="minimax_h3",
        beats=[Beat(5.0, "she waits", _says("行こう", "Young woman on the left"))],
    )
    assert f'At 0s {_HER} says "行こう".' in prompt


def test_line_without_a_known_speaker_does_not_invent_one() -> None:
    """話す人が外れた行（人物を消した）は、誰とも決めずに書く。"""
    prompt = build_shot_prompt(engine="minimax_h3", beats=[Beat(5.0, "she waits", _says("行こう", ""))])
    assert f'At 0s {UNKNOWN_SPEAKER} says "行こう".' in prompt


def test_narration_is_an_off_screen_voice() -> None:
    """ナレーションは画面の外の声。画面の人物に口を動かさせない。"""
    prompt = build_shot_prompt(
        engine="minimax_h3",
        beats=[Beat(5.0, "she waits", (SpokenLine(text="その日は雨だった", narration=True),))],
    )
    description = _field(prompt, FIELD_DESCRIPTION)
    assert 'At 0s an off-screen narrator says "その日は雨だった".' in description
    assert NARRATION_ONLY_DIRECTIVE in description
    assert NARRATION_MIXED_DIRECTIVE not in description


def test_narration_mixed_with_dialogue_is_kept_off_the_lips() -> None:
    prompt = build_shot_prompt(
        engine="minimax_h3",
        beats=[
            Beat(
                5.0,
                "she waits",
                (
                    SpokenLine(text="その日は雨だった", narration=True),
                    SpokenLine(text="行こう", speaker=_HER),
                ),
            )
        ],
    )
    description = _field(prompt, FIELD_DESCRIPTION)
    assert NARRATION_MIXED_DIRECTIVE in description
    assert NARRATION_ONLY_DIRECTIVE not in description


def test_empty_lines_count_as_silence() -> None:
    """書きかけの空の行しか無ければ、無音のカットとして扱う。"""
    prompt = build_shot_prompt(engine="minimax_h3", beats=[Beat(5.0, "she waits", _says("  "))])
    assert SILENCE_DIRECTIVE in prompt


def test_visual_lines_never_carry_speech() -> None:
    """回帰: 映像の行（Second …）にセリフを混ぜない。"""
    prompt = build_shot_prompt(
        engine="minimax_h3",
        beats=[
            Beat(duration_seconds=3.0, motion_prompt="she turns around", speech_lines=_says("行こう")),
            Beat(duration_seconds=3.0, motion_prompt="she walks away", speech_lines=_says("待ってて")),
        ],
    )
    for line in prompt.splitlines():
        if line.startswith("Second "):
            assert '"' not in line, line


def test_description_field_opens_on_the_start_image() -> None:
    """映像の欄は、起点の画像の場面から始まると言い切り、1続きの映像だと分かる足場を続ける。

    言わないと H3 は起点の画像を参考にとどめ、別の構図・画風から始める。
    """
    prompt = build_shot_prompt(engine="minimax_h3", beats=[Beat(5.0, "she waits")])
    lines = _field(prompt, FIELD_DESCRIPTION).splitlines()
    assert lines[0] == "[Shot 1]"
    assert lines[1] == OPENING_FRAME_DIRECTIVE
    assert "<Picture 1>" in lines[1]
    assert lines[2].startswith(CONTINUOUS_SHOT_PREFIX)


def test_wan_is_not_told_about_picture_tags() -> None:
    """Wan 2.2 は起点の画像を最初のコマとして使うので、H3向けの言い回しは渡さない。"""
    prompt = build_shot_prompt(engine="wan22", beats=[], motion_prompt="she waits")
    assert "Picture" not in prompt


def test_wan_never_receives_speech_or_ambience() -> None:
    """Wan 2.2 は音を作らない。セリフを渡すと映像の指示として効いてしまう。"""
    prompt = build_shot_prompt(
        engine="wan22",
        beats=[],
        motion_prompt="she slowly turns around",
        speech_lines=_says("行こう"),
        ambience_prompt="rain",
    )
    # H3向けの欄の見出しも付けない（umt5はこの構造を知らない）。
    assert prompt == "She slowly turns around"


def test_wan_joins_beats_as_plain_prose() -> None:
    prompt = build_shot_prompt(
        engine="wan22",
        beats=[
            Beat(duration_seconds=3.0, motion_prompt="she turns around"),
            Beat(duration_seconds=3.0, motion_prompt="she walks away"),
        ],
    )
    assert prompt == "she turns around. Then, she walks away"


def test_empty_motion_falls_back_to_the_default() -> None:
    prompt = build_shot_prompt(engine="wan22", beats=[])
    assert prompt.lower() == DEFAULT_MOTION_PROMPT.lower()


def test_beats_become_timed_choreography_lines() -> None:
    """時間割りは公式t2vテンプレートと同じ "Second A–B:" の形にする。"""
    prompt = build_shot_prompt(
        engine="minimax_h3",
        beats=[
            Beat(duration_seconds=3.0, motion_prompt="she turns around"),
            Beat(duration_seconds=2.5, motion_prompt="she walks away"),
        ],
    )
    lines = _field(prompt, FIELD_DESCRIPTION).splitlines()
    assert lines[2] == (
        "Single continuous shot, no cuts. Choreography for one unbroken 5.5-second arc:"
    )
    assert lines[3] == "Second 0\u20133: She turns around."
    assert lines[4] == "Second 3\u20135.5: She walks away."


def test_single_beat_still_gets_a_time_label() -> None:
    """区間が1つでも時間付きの形にする（1区間だけの指定で素直に動いた実機の結果）。"""
    prompt = build_shot_prompt(
        engine="minimax_h3",
        beats=[Beat(duration_seconds=3.0, motion_prompt="she turns around")],
    )
    lines = _field(prompt, FIELD_DESCRIPTION).splitlines()
    assert lines[2] == (
        "Single continuous shot, no cuts. Choreography for one unbroken 3-second arc:"
    )
    assert lines[3] == "Second 0\u20133: She turns around."


def test_shot_without_beats_uses_its_own_duration() -> None:
    prompt = build_shot_prompt(
        engine="minimax_h3", beats=[], motion_prompt="she waits", duration_seconds=5.0
    )
    assert "Second 0\u20135: She waits." in prompt


def test_unknown_duration_falls_back_to_a_plain_line() -> None:
    """尺が分からないときだけ、時刻の行を省く。"""
    prompt = build_shot_prompt(engine="minimax_h3", beats=[], motion_prompt="she waits")
    assert "Second" not in prompt
    description = _field(prompt, FIELD_DESCRIPTION).splitlines()
    assert description[1] == OPENING_FRAME_DIRECTIVE
    assert description[2] == "Single continuous shot, no cuts. She waits."


@pytest.mark.parametrize(
    ("engine", "seconds", "expected"),
    [
        # Wan: 16fpsで 4k+1。5秒=80フレーム→81へ切り上げ。
        ("wan22", 5.0, 81),
        # H3: 24fpsで 17k+5。5秒=120フレーム→124へ切り上げ。
        ("minimax_h3", 5.0, 124),
        ("minimax_h3", 8.0, 192),
        # 学習範囲の上限（15.1秒）を超えても、受け付ける最大で止める。
        ("minimax_h3", 60.0, MAX_FRAMES),
    ],
)
def test_frames_snap_to_the_engine_grid(engine: str, seconds: float, expected: int) -> None:
    frames = frames_for_seconds(engine, seconds)
    assert frames == expected


@pytest.mark.parametrize("engine", ["wan22", "minimax_h3"])
@pytest.mark.parametrize("seconds", [0.1, 1.0, 2.5, 5.0, 7.3, 12.0, 20.0])
def test_frames_always_satisfy_the_engine_constraint(engine: str, seconds: float) -> None:
    """刻みに合わない値を投げるとComfyUI側で落ちるため、全経路でここを通す。"""
    frames = frames_for_seconds(engine, seconds)
    modulus, remainder = (4, 1) if engine == "wan22" else (17, 5)
    assert frames % modulus == remainder
    assert 5 <= frames <= MAX_FRAMES
    assert seconds_for_frames(engine, frames) > 0


def test_prune_end_image_removes_the_optional_keyframe_input() -> None:
    """終端画像を使わないときは、ノードごと外す（未設定のLoadImageを走らせない）。"""
    template = json.loads(
        Path("workflows/video/minimax_h3_i2v.json").read_text(encoding="utf-8")
    )
    assert "last_frame" in template["7"]["inputs"]
    pruned = prune_end_image(template)
    assert "17" not in pruned
    assert "last_frame" not in pruned["7"]["inputs"]
    assert "end_image" not in pruned["_chotto_sashie_bindings"]
    # もとのテンプレートは書き換えない。
    assert "17" in template


def test_recompose_prompt_pins_identity_and_place() -> None:
    prompt = build_recompose_prompt("バストアップに寄る")
    assert "バストアップに寄る" in prompt
    # 人物と場所を変えないことを明示しないと、別人・別の場所になる。
    assert "Keep the same characters" in prompt
    assert "Change only the camera" in prompt
