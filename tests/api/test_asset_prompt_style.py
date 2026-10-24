"""背景・小物の指示文に作品の画風が乗ること。"""

from __future__ import annotations

from chotto_sashie_api.assets import build_asset_prompt


def test_the_project_style_is_appended() -> None:
    """合成や人物画像と同じく、素材にも作品の画風を効かせる。

    ここだけ渡していないと、背景・小物だけが別の描かれ方になり、合成したときに
    人物と食い違う（実機では、アニメ調の人物を置く作品なのに背景だけ写真になった）。
    """
    prompt = build_asset_prompt("放課後の教室", "昭和の映画風実写写真")
    assert prompt.startswith("放課後の教室")
    assert "作品全体の画風: 昭和の映画風実写写真" in prompt


def test_no_style_leaves_the_prompt_alone() -> None:
    """画風を決めていない作品では、利用者の書いた文をそのまま使う。"""
    assert build_asset_prompt("放課後の教室", "") == "放課後の教室"
    assert build_asset_prompt("放課後の教室", "   ") == "放課後の教室"
