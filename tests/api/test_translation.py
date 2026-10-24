"""指示文の日本語→英語変換。

MiniMax H3 は、動きの指示とセリフの両方が日本語だと区別できず、説明文まで読み上げる。
セリフ以外だけを英語へ直してから送る。ここでは、その判定と後始末を押さえる。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chotto_sashie_api.translation import (
    DEFAULT_TRANSLATION_CLIP,
    build_instruction,
    build_translation_workflow,
    clean_output,
    contains_japanese,
    extract_generated_text,
    translation_key,
    TranslationSettings,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ゆっくり振り返る", True),
        ("髪が風に揺れる", True),
        ("寄る", True),
        ("she slowly turns around", False),
        ("", False),
        ("close-up, 35mm", False),
    ],
)
def test_japanese_detection(text: str, expected: bool) -> None:
    assert contains_japanese(text) is expected


def test_same_text_shares_one_key() -> None:
    """同じ文を何度も訳し直さないため、前後の空白は鍵に含めない。"""
    assert translation_key(" ゆっくり振り返る ") == translation_key("ゆっくり振り返る")
    assert translation_key("A") != translation_key("B")


def test_person_is_remembered_apart_from_directions() -> None:
    """話す人の記述は名指しの句に訳すので、同じ文でも指示の訳とは別に覚える。

    指示の鍵は、種類を省いたときと同じにする。
    """
    assert translation_key("左") == translation_key("左", "direction")
    assert translation_key("左", "person") != translation_key("左")


def test_instruction_forbids_extra_output() -> None:
    instruction = build_instruction("ゆっくり振り返る")
    assert "ゆっくり振り返る" in instruction
    # 余計な説明や引用符が付くと、そのまま映像の指示に混ざる。
    assert "nothing else" in instruction


def test_person_instruction_asks_for_a_noun_phrase() -> None:
    """「〜が言う」の主語に置くので、文ではなく句で返させる。名前も入れさせない。"""
    instruction = build_instruction("位置: 左 / 年齢・性別: 10代の女性", "person")
    assert "位置: 左" in instruction
    assert "noun phrase" in instruction
    assert "no name" in instruction
    assert "nothing else" in instruction


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Slowly turning around", "Slowly turning around"),
        # 指示しても前置きが付くことがある。
        ("English: Hair blowing in the wind", "Hair blowing in the wind"),
        ("Translation: She walks away", "She walks away"),
        ('"She turns around"', "She turns around"),
        # 2行目以降の余談は落とす。
        ("She turns around\n\nNote: this is a translation.", "She turns around"),
        ("<think>考え中</think>\nShe turns around", "She turns around"),
    ],
)
def test_output_is_reduced_to_one_english_line(raw: str, expected: str) -> None:
    assert clean_output(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "ゆっくり振り返る", "English: 振り返る"])
def test_untranslated_output_is_rejected(raw: str) -> None:
    """日本語が残った結果は使わない（使うと読み上げの原因がそのまま残る）。"""
    assert clean_output(raw) == ""


def test_workflow_binds_the_instruction_and_model() -> None:
    template = json.loads(
        Path("workflows/text/translate_to_english.json").read_text(encoding="utf-8")
    )
    workflow = build_translation_workflow(template, "ゆっくり振り返る", TranslationSettings())
    assert workflow["2"]["class_type"] == "TextGenerate"
    assert "ゆっくり振り返る" in workflow["2"]["inputs"]["prompt"]
    assert workflow["1"]["inputs"]["clip_name"] == DEFAULT_TRANSLATION_CLIP
    # 翻訳は毎回同じ結果になってほしいので、標本化は切ったままにする。
    assert workflow["2"]["inputs"]["sampling_mode"] == "off"
    # 束縛の対象外は書き換えられない。
    assert "_chotto_sashie_bindings" not in workflow


def test_generated_text_is_read_from_the_history() -> None:
    """`PreviewAny` はファイルを書かない（既存ComfyUIの出力を汚さない）ので履歴から読む。"""
    entry = {"outputs": {"3": {"text": ["Slowly turning around"]}}}
    assert extract_generated_text(entry) == "Slowly turning around"
    assert extract_generated_text({"outputs": {}}) == ""
