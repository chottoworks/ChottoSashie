"""置いて合成する: 人物ごとに姿を作り、背景の上へ置いた1枚をなじませる。

指示文だけで1枚に描き起こすと、位置と大きさは文で決まらず（英語で書くと人物が増える）、
背景も描き直される。そこで仕事を3つに分ける。

1. **姿を作る**（人物ごと）: 立ち絵1枚の編集でポーズ・向き・表情を付け、背景除去で
   切り抜く。1人ずつの編集なら指定は確実に効き、崩れても合成の前に見て作り直せる。
   指定が空なら編集はせず、立ち絵を切り抜くだけにする。
2. **置く**（画面）: 切り抜いた姿を背景の上へ、利用者が決めた位置と大きさで重ねる。
   ここはアプリが計算で行うので、置いたとおりの下書きになる。
3. **なじませる**: 下書き1枚だけを渡し、**人物のまわりだけ**を描き直して、光・影・
   色・描き方をそろえさせる。参照画像が1枚なので、人物の重複や、画像3枚で起きる寄り
   すぎを避けられる。

なじませる段で1枚まるごと描き直すと、構図は保たれても背景の描き方が変わる（筆致の
質感が消え、色が濃く・暗くなる。実測）。そこで、置いた人物の形を広げて足もとの影の分を
足した範囲（`blend_mask`）の中だけにノイズを掛けて描き、仕上げに範囲の外を下書きの
画素へ戻す（`finish_blend`）。背景は元の1枚のまま残る。

なじませる段では、場所・大きさ・姿勢・向きを image 1 のまま保てと言い切る。同じ1回の
生成で「向きを変えろ」と言うと、保てという指示とぶつかって向きが変わらなかった（実測）。
向きを変えるのは姿を作る段だけにする。
"""

from __future__ import annotations

import io
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter

from chotto_sashie_shared.comfy_client import bind_workflow

from .composition import (
    MAX_CHARACTERS,
    CompositionError,
    over_limit_message,
    person_instruction,
    require_background,
    resolve_still_image_figure,
)
from .schemas import StillImageCharacterLayout, StillImageCharacterSpec

# 姿を作る編集の出力の大きさ。立ち絵の変更と同じ縦長の全身。
POSE_WIDTH = 768
POSE_HEIGHT = 1024
# 背景除去モデル（ComfyUIの background_removal フォルダに置くもの）。
BACKGROUND_REMOVAL_MODEL = "birefnet.safetensors"
# 合成テンプレートで、編集した画像を出すノード。背景除去はここから受け取る。
_EDIT_DECODE_NODE = "31"
# 背景除去テンプレートの、画像を読み込むノード。編集の続きで抜くときは使わない。
_REMOVAL_LOAD_NODE = "101"
# 切り抜いた姿の範囲を決めるときに、無いものとみなす透明度（背景除去の薄いにじみ）。
_ALPHA_FLOOR = 8
# 合成テンプレートの、空の下地・下書きを読むノード・VAE・サンプラーとそのモデル。
_EMPTY_LATENT_NODE = "28"
_DRAFT_LOAD_NODE = "11"
_VAE_NODE = "7"
_KSAMPLER_NODE = "30"
# 人物のまわりだけを描くために足すノード。番号は合成テンプレートと重ならない。
_MASK_LOAD_NODE = "50"
_DRAFT_ENCODE_NODE = "51"
_NOISE_MASK_NODE = "52"
_DIFFERENTIAL_NODE = "53"
# 描き直す範囲の広げ方とぼかし（出来上がりの幅 1280 のときの画素数。幅に比例させる）。
# 人物の輪郭から少し外までを描き直し、境目は濃さを徐々に落として継ぎ目を出さない。
_MASK_GROW = 12
_MASK_BLUR = 10
_MASK_BASE_WIDTH = 1280


def default_layout(index: int, count: int) -> StillImageCharacterLayout:
    """まだ置いていない人物の位置。左から等間隔に、手前の地面へ同じ大きさで並べる。"""
    return StillImageCharacterLayout(x=(index + 1) / (count + 1), y=0.9, height=0.42)


def pose_outdated(
    *,
    pose_figure_id: str,
    pose_spec: StillImageCharacterSpec | None,
    figure_id: str | None,
    spec: StillImageCharacterSpec,
) -> bool:
    """姿を作ったあとに、立ち絵を選び直したか、姿の指定が変わったか。"""
    if pose_spec is None:
        return False
    return pose_figure_id != (figure_id or "") or pose_spec.posing() != spec.posing()


def build_pose_prompt(spec: StillImageCharacterSpec, illustration_style: str = "") -> str:
    """姿を作る編集の指示文。指定が空なら空文字（編集せず、切り抜くだけにする）。

    保つものと立ち絵の条件（全身・素背景・1人だけ）は、立ち絵の変更と同じ言い切り方に
    する。素背景を保たせるのは、次の背景除去できれいに抜けるようにするため。
    """
    instruction = person_instruction(
        pose=spec.pose, direction=spec.direction, expression=spec.expression, notes=spec.notes
    )
    if not instruction:
        return ""
    lines = [
        instruction,
        "Keep the person's face, hairstyle, hair color, eye color, body proportions, outfit, "
        "accessories and the items they carry exactly as in image 1. Change only the pose, the "
        "direction the body faces and the facial expression, following the instruction above.",
        "Show the whole body from the top of the head down to both shoes, with the feet inside "
        "the frame. Never crop at the waist or thighs.",
        "Plain light-gray background, no scenery, no props on the floor.",
        "The scene contains a single person and nobody else. "
        "Show the person from image 1 only once. Do not duplicate or repeat them.",
        "Render in the same visual style as image 1 "
        "(photographic references stay photographic, illustrated references stay illustrated). "
        "Do not turn photographic subjects into drawings, paintings or anime.",
    ]
    if illustration_style.strip():
        lines.append(f"作品全体の画風: {illustration_style.strip()}")
    return " ".join(lines)


def build_blend_prompt(person_count: int, illustration_style: str = "") -> str:
    """下書きをなじませる指示文。

    状況説明は渡さない。姿勢は姿を作る段、位置は置く段が持つので、ここで書けることが
    無い（出来事を書いても姿勢も位置も動かず、様子も足されなかった。実測）。
    """
    people = "a person" if person_count == 1 else "people"
    each = "The person keeps" if person_count == 1 else "Each person keeps"
    once = (
        "Show the person only once. Do not duplicate or repeat them."
        if person_count == 1
        else "Show each of them only once. Do not duplicate or repeat them."
    )
    # 描き直すのは人物のまわりだけなので、影は言わないと描かれない（足もとが浮く。実測）。
    shadow = (
        "The person casts a soft shadow on the ground at their feet, matching the lights of "
        "the scene."
        if person_count == 1
        else "Each person casts a soft shadow on the ground at their feet, matching the lights "
        "of the scene."
    )
    lines = [
        f"Edit image 1. Image 1 is the scene with {people} roughly pasted in.",
        f"Blend {'the person' if person_count == 1 else 'them'} naturally into the scene: match "
        "the lighting, shadows, colors and rendering of the scene so they belong to it.",
        f"{each} standing on the same spot, at the same size, in the same pose and facing the "
        "same direction as in image 1, with the same face, hairstyle, hair color, outfit and "
        "accessories.",
        shadow,
        once,
        "Everything else in image 1 stays exactly as it is, in the same position, size and shape: "
        "do not remove, move, redraw or replace anything, and do not add anything.",
        "Render the whole scene in the same visual style as image 1. "
        "Do not turn photographic subjects into drawings, paintings or anime.",
    ]
    if illustration_style.strip():
        lines.append(f"作品全体の画風: {illustration_style.strip()}")
    return " ".join(lines)


def build_pose_workflow(
    removal_template: dict[str, Any],
    *,
    edit_workflow: dict[str, Any] | None,
    figure_image: str,
    removal_prefix: str,
) -> dict[str, Any]:
    """姿を作るワークフロー。編集があればその出力を、無ければ立ち絵をそのまま抜く。

    編集（合成テンプレートから作ったもの）と背景除去を1件のプロンプトにつなぐ。
    2件に分けると、その間に別の生成が割り込み、モデルを積み直すことになる。
    """
    values: dict[str, Any] = {
        "background_removal_model": BACKGROUND_REMOVAL_MODEL,
        "filename_prefix": removal_prefix,
    }
    if edit_workflow is None:
        values["source_image"] = figure_image
        return bind_workflow(removal_template, values)
    values["removal_input"] = [_EDIT_DECODE_NODE, 0]
    removal = bind_workflow(removal_template, values)
    removal.pop(_REMOVAL_LOAD_NODE, None)
    overlap = set(removal) & set(edit_workflow)
    if overlap:
        raise ValueError(f"ワークフローのノード番号が重なっています: {sorted(overlap)}")
    return {**deepcopy(edit_workflow), **removal}


def cut_out(image: bytes, mask: bytes) -> bytes:
    """画像と背景除去のマスクから、人物だけの透過PNGを作る（人物の範囲で切り詰める）。"""
    with Image.open(io.BytesIO(image)) as source, Image.open(io.BytesIO(mask)) as alpha:
        figure = source.convert("RGBA")
        figure.putalpha(alpha.convert("L").resize(figure.size))
    box = figure.getchannel("A").point(lambda value: 255 if value > _ALPHA_FLOOR else 0).getbbox()
    if box is None:
        raise CompositionError("人物を切り抜けませんでした。立ち絵か指定を変えて、作り直してください")
    output = io.BytesIO()
    figure.crop(box).save(output, format="PNG")
    return output.getvalue()


@dataclass(frozen=True)
class PlacedFigure:
    """背景の上に置く1人。"""

    name: str
    pose_path: Path
    layout: StillImageCharacterLayout


@dataclass(frozen=True)
class PlacedSources:
    background_path: Path
    figures: list[PlacedFigure]


def resolve_placed_sources(database: Any, still_image: Any) -> PlacedSources:
    """置いて合成するのに使う背景と人物の姿を集める。足りなければ理由を添えて断る。

    判定（押せるか）と合成の実行が同じここを通る。別々に決めると、押せるのに失敗する。
    """
    from chotto_sashie_shared.jobs import ACTIVE_JOB_STATES, JobNotFoundError, JobRepository

    from .assets import AssetRepository
    from .characters import CharacterRepository
    from .still_image_characters import StillImageCharacterRepository

    require_background(still_image)
    if not still_image.character_ids:
        raise CompositionError("人物を1人以上選んでください。人物のいない絵は背景として作ります")
    if len(still_image.character_ids) > MAX_CHARACTERS:
        raise CompositionError(over_limit_message(len(still_image.character_ids)))

    backgrounds = AssetRepository(database, "background_assets")
    background = backgrounds.get(still_image.background_asset_id)
    background_path = backgrounds.image_path(still_image.background_asset_id)
    if background_path is None:
        raise CompositionError(f"背景「{background.name}」に画像が登録されていません")

    characters = CharacterRepository(database)
    records = StillImageCharacterRepository(database)
    jobs = JobRepository(database)
    figures: list[PlacedFigure] = []
    count = len(still_image.character_ids)
    for index, character_id in enumerate(still_image.character_ids):
        name = characters.get(character_id).name
        figure = resolve_still_image_figure(database, still_image.id, character_id)
        if figure is None:
            raise CompositionError(
                f"人物「{name}」の立ち絵がありません。「登場人物」の画面で立ち絵を作ってください"
            )
        record = records.get(still_image.id, character_id)
        if record is not None and record.pose_job_id:
            try:
                working = jobs.get(record.pose_job_id).state in ACTIVE_JOB_STATES
            except JobNotFoundError:
                working = False
            if working:
                raise CompositionError(f"人物「{name}」の姿を作っています。出来上がるまで待ってください")
        pose_path = Path(record.pose_image_path) if record and record.pose_image_path else None
        if pose_path is None or not pose_path.is_file():
            raise CompositionError(f"人物「{name}」の姿がまだありません。「姿を作る」を押してください")
        if pose_outdated(
            pose_figure_id=record.pose_figure_id,
            pose_spec=record.pose_spec,
            figure_id=figure.id,
            spec=record.spec,
        ):
            raise CompositionError(
                f"人物「{name}」の立ち絵か指定が、姿を作ったときから変わっています。"
                "「姿を作る」を押して作り直してください"
            )
        figures.append(
            PlacedFigure(
                name=name,
                pose_path=pose_path,
                layout=record.layout or default_layout(index, count),
            )
        )
    return PlacedSources(background_path=background_path, figures=figures)


def _placed_figures(
    sources: PlacedSources, width: int, height: int
) -> list[tuple[Image.Image, int, int]]:
    """置く順（奥から）に、出来上がりの大きさへ縮めた姿と、その左上の位置を返す。

    下書きと描き直す範囲が同じ計算を通る。別々に計算すると、範囲が人物からずれる。
    """
    placed: list[tuple[Image.Image, int, int]] = []
    for item in sorted(sources.figures, key=lambda figure: figure.layout.y):
        with Image.open(item.pose_path) as pose:
            figure = pose.convert("RGBA")
        figure_height = max(1, round(item.layout.height * height))
        figure_width = max(1, round(figure.width * figure_height / figure.height))
        figure = figure.resize((figure_width, figure_height), Image.LANCZOS)
        x = round(item.layout.x * width - figure_width / 2)
        y = round(item.layout.y * height - figure_height)
        placed.append((figure, x, y))
    return placed


def compose_draft(sources: PlacedSources, width: int, height: int) -> bytes:
    """背景の上へ姿を置いた下書き（PNG）。

    背景は出来上がりの大きさへ、縦横比を保って隙間なく広げ、はみ出した分を中央で切る。
    人物を置く画面も同じ見せ方をするので、画面で置いた位置と下書きの位置が一致する。
    奥（足もとが上）の人物から先に描き、手前の人物が重なって前に出るようにする。
    """
    with Image.open(sources.background_path) as source:
        background = source.convert("RGBA")
    scale = max(width / background.width, height / background.height)
    resized = background.resize(
        (max(width, round(background.width * scale)), max(height, round(background.height * scale))),
        Image.LANCZOS,
    )
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    scene = resized.crop((left, top, left + width, top + height))

    for figure, x, y in _placed_figures(sources, width, height):
        layer = Image.new("RGBA", scene.size, (0, 0, 0, 0))
        layer.paste(figure, (x, y), figure)
        scene = Image.alpha_composite(scene, layer)

    output = io.BytesIO()
    scene.convert("RGB").save(output, format="PNG")
    return output.getvalue()


def blend_mask(sources: PlacedSources, width: int, height: int) -> bytes:
    """なじませる段で描き直す範囲（白）。人物の形を少し広げ、足もとの影の分を足してぼかす。

    影の向きは背景の光で決まり、こちらでは分からないので、足もとを中心に左右へ広く取る。
    """
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    for figure, x, y in _placed_figures(sources, width, height):
        silhouette = figure.getchannel("A").point(lambda value: 255 if value > _ALPHA_FLOOR else 0)
        mask.paste(255, (x, y), silhouette)
        feet_x = x + figure.width / 2
        feet_y = y + figure.height
        draw.ellipse(
            (
                feet_x - figure.width * 1.3,
                feet_y - figure.height * 0.06,
                feet_x + figure.width * 1.3,
                feet_y + figure.height * 0.08,
            ),
            fill=255,
        )
    scale = width / _MASK_BASE_WIDTH
    grow = max(1, round(_MASK_GROW * scale))
    mask = mask.filter(ImageFilter.MaxFilter(grow * 2 + 1))
    mask = mask.filter(ImageFilter.GaussianBlur(max(1.0, _MASK_BLUR * scale)))
    output = io.BytesIO()
    mask.save(output, format="PNG")
    return output.getvalue()


def build_blend_workflow(composition_workflow: dict[str, Any], mask_image: str) -> dict[str, Any]:
    """合成テンプレートから作ったなじませのワークフローを、人物のまわりだけ描く形にする。

    空の下地の代わりに下書きを潜在へ写し、範囲の中だけにノイズを掛ける。範囲はぼかして
    あるので、濃さに応じて描き直す強さを変える（DifferentialDiffusion）。
    """
    workflow = deepcopy(composition_workflow)
    sampler = workflow[_KSAMPLER_NODE]["inputs"]
    added = {
        _MASK_LOAD_NODE: {
            "class_type": "LoadImageMask",
            "inputs": {"image": mask_image, "channel": "red"},
        },
        _DRAFT_ENCODE_NODE: {
            "class_type": "VAEEncode",
            "inputs": {"pixels": [_DRAFT_LOAD_NODE, 0], "vae": [_VAE_NODE, 0]},
        },
        _NOISE_MASK_NODE: {
            "class_type": "SetLatentNoiseMask",
            "inputs": {"samples": [_DRAFT_ENCODE_NODE, 0], "mask": [_MASK_LOAD_NODE, 0]},
        },
        _DIFFERENTIAL_NODE: {
            "class_type": "DifferentialDiffusion",
            "inputs": {"model": sampler["model"]},
        },
    }
    overlap = set(added) & set(workflow)
    if overlap:
        raise ValueError(f"ワークフローのノード番号が重なっています: {sorted(overlap)}")
    workflow.pop(_EMPTY_LATENT_NODE, None)
    workflow.update(added)
    sampler["model"] = [_DIFFERENTIAL_NODE, 0]
    sampler["latent_image"] = [_NOISE_MASK_NODE, 0]
    return workflow


def finish_blend(generated: bytes, draft: bytes, mask: bytes) -> bytes:
    """描き直した1枚の、範囲の外を下書きの画素へ戻す。

    範囲の外も一度VAEを通っているので、わずかに細部が落ちる。下書きの画素へ戻せば、
    背景は元の1枚と同じになる。
    """
    with Image.open(io.BytesIO(draft)) as base_image, Image.open(io.BytesIO(mask)) as mask_image:
        base = base_image.convert("RGB")
        weight = mask_image.convert("L").resize(base.size)
    with Image.open(io.BytesIO(generated)) as generated_image:
        painted = generated_image.convert("RGB").resize(base.size, Image.LANCZOS)
    output = io.BytesIO()
    Image.composite(painted, base, weight).save(output, format="PNG")
    return output.getvalue()
