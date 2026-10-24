"""Qwen-Image-Edit-2511 の角度LoRAが解釈する語彙と、立ち絵を作る指示文。

角度LoRAは配布元が学習させた語順しか受け付けない。長い自然文へ角度指定を埋め込むと
正面へ戻りやすいので、先頭のトリガーと語順は変えない。
https://huggingface.co/fal/Qwen-Image-Edit-2511-Multiple-Angles-LoRA
"""

from __future__ import annotations

# 学習されている語彙。方位8×高さ4×距離3の組み合わせだけで、ここに無い言い回しは効かない。
# 並びは配布元の方位図と同じ（正面から時計回り）。
ANGLE_AZIMUTHS: tuple[str, ...] = (
    "front view",
    "front-right quarter view",
    "right side view",
    "back-right quarter view",
    "back view",
    "back-left quarter view",
    "left side view",
    "front-left quarter view",
)
ANGLE_ELEVATIONS: tuple[str, ...] = (
    "low-angle shot",
    "eye-level shot",
    "elevated shot",
    "high-angle shot",
)
ANGLE_DISTANCES: tuple[str, ...] = ("close-up", "medium shot", "wide shot")


def build_angle_prompt(
    azimuth: str, elevation: str = "eye-level shot", distance: str = "medium shot"
) -> str:
    """角度LoRAが学習している語順で指示文を作る（`<sks> 方位 高さ 距離`）。"""
    return f"<sks> {azimuth} {elevation} {distance}"


# 基準画像を正面全身へ組み直す指示文。立ち絵はこれで作る。
#
# 距離の語（wide shot）だけでは胸像のまま足元が入らないため、トリガーのうしろへ
# 「頭から靴まで入れる」を足してある。正面は回転が起きないぶん元画像の構図を
# 引き継ぎやすく、向きが変わらない指定なので、文を足しても角度の効きは損なわれない。
#
# 左右の対応に注意（横向きを作るときに効いてくる）。実機の出力では
# `front-left/right quarter view` は被写体基準（front-left → 顔の左側が見える）だが、
# `left/right side view` はカメラ位置基準（左へ回り込む → 顔の右側が写る）で
# 学習されている。
FULL_BODY_FRONT_PROMPT = (
    "<sks> front view eye-level shot wide shot. "
    "Recompose as a true full-body reference from the top of the head down to both shoes, "
    "with the feet inside the frame. Never crop at the waist or thighs."
)
