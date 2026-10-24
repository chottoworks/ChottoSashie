"""素材の派生: いまある素材を元に、別の1枚を作る（立ち絵の変更・背景のバリエーション）。

作るものは2つ。どちらも Qwen-Image-Edit-2511 で、静止画の合成と同じテンプレートを使う。
**違うのは指示文の組み立てだけ**で、テンプレートを写して分岐させることはしない。

1. 立ち絵の変更 — 立ち絵＋衣装・小物 → 衣装を変えた立ち絵
2. 背景のバリエーション — 元背景＋小物、または時間帯の変更 → 別の背景

立ち絵の変更で肝心なのは、**出来上がりが立ち絵の条件を満たすこと**である。
全身・素背景・1人だけ、が崩れると、次の合成で使えない画像が立ち絵の一覧に混ざる。
指示文でそこを担保する。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DerivationSource:
    """派生に渡す1枚（アップロード前のローカル情報）。

    `kind` は "figure"（元の立ち絵）か "prop"（衣装・小物）。順序は 立ち絵 → 小物 で、
    元になる1枚を image 1 に置く（指示文がその番号を指す）。
    """

    kind: str
    image_path: Path


KIND_FIGURE = "figure"
KIND_BACKGROUND = "background"
KIND_PROP = "prop"

# 小物の呼び名。素材の名前は渡さない——名前を渡すと、その文字が絵の中に描かれる
# （実機で、背景に付けた名前が黒板の上へ書かれた）。
_PROP_LABELS = ("the object in image 2", "the object in image 3")


class DerivationError(ValueError):
    pass


def build_figure_variation_prompt(
    instruction: str,
    prop_count: int,
    illustration_style: str = "",
) -> str:
    """立ち絵の変更に使う指示文。

    **変えるのは衣装と持ち物だけ**で、顔・髪・目の色・体型・画風は元の立ち絵のまま。
    姿勢を変える指示はここでは出さない（姿勢は静止画の合成のときに指定する）。

    出来上がりが立ち絵として使えるよう、全身・素背景・1人だけを毎回言い切る。
    書かないと、合成のときに腰から上だけの画像や、背景の付いた画像が混ざる。

    利用者の指示は**日本語のまま**入れる。静止画の合成と同じ扱いで、英語へ直すと
    人数が壊れた実機の記録がある。
    """
    lines: list[str] = []

    if prop_count:
        objects = " and ".join(_PROP_LABELS[:prop_count])
        keep = (
            "Keep that object exactly as in its own image."
            if prop_count == 1
            else "Keep those objects exactly as in their own images."
        )
        lines.append(f"The person in image 1 is wearing or holding {objects}. {keep}")

    if instruction.strip():
        text = instruction.strip()
        lines.append(text if text[-1] in "。.!?！？" else f"{text}.")

    lines.append(
        "Keep the person's face, hairstyle, hair color, eye color, body proportions and "
        "identifying features exactly as in image 1. Change only their clothing and the "
        "items they carry."
    )
    # 立ち絵の条件。ここが崩れると、次の合成で使えない1枚が一覧に混ざる。
    lines.append(
        "Show the whole body from the top of the head down to both shoes, with the feet "
        "inside the frame, standing naturally and facing the camera. "
        "Never crop at the waist or thighs."
    )
    lines.append("Plain light-gray background, no scenery, no props on the floor.")
    lines.append(
        "The scene contains a single person and nobody else. "
        "Show the person from image 1 only once. Do not duplicate or repeat them."
    )
    lines.append(
        "Render in the same visual style as image 1 "
        "(photographic references stay photographic, illustrated references stay illustrated). "
        "Do not turn photographic subjects into drawings, paintings or anime."
    )
    if illustration_style.strip():
        lines.append(f"作品全体の画風: {illustration_style.strip()}")
    return " ".join(lines)


def resolve_figure_variation_sources(
    database: object, figure: object, prop_asset_ids: list[str]
) -> list[DerivationSource]:
    """元の立ち絵と小物から、派生に渡す画像を順序付きで集める。

    順序は 立ち絵 → 小物。画像の無い小物があれば `DerivationError` を送出する。
    """
    from .assets import AssetRepository
    from .composition import MAX_REFERENCE_IMAGES

    source_path = Path(figure.image_path)  # type: ignore[attr-defined]
    if not figure.image_path or not source_path.is_file():  # type: ignore[attr-defined]
        raise DerivationError("元になる立ち絵の画像がありません")

    sources = [DerivationSource(kind=KIND_FIGURE, image_path=source_path)]
    prop_repo = AssetRepository(database, "prop_assets")
    for prop_id in prop_asset_ids:
        prop = prop_repo.get(prop_id)
        image_path = prop_repo.image_path(prop_id)
        if image_path is None:
            raise DerivationError(f"小物「{prop.name}」に画像が登録されていません")
        sources.append(DerivationSource(kind=KIND_PROP, image_path=image_path))

    if len(sources) > MAX_REFERENCE_IMAGES:
        raise DerivationError(
            f"一度に渡せる画像は{MAX_REFERENCE_IMAGES}枚までです"
            f"（元の立ち絵1枚と、小物{MAX_REFERENCE_IMAGES - 1}つまで）"
        )
    return sources


# 背景のバリエーションの作り方。入力の枚数と指示文がこれで決まる。
KIND_PROP_VARIATION = "prop"
KIND_TIME_VARIATION = "time"


def build_background_variation_prompt(
    kind: str,
    instruction: str,
    prop_count: int,
    illustration_style: str = "",
) -> str:
    """背景のバリエーションを作る指示文。

    変えるのは「置いた小物」か「時間帯・天気」だけで、場所・構図・カメラ位置・遠近は
    元の背景のまま。合成のときに背景の構図が変わると、同じ場所の別カットに見えなくなる。

    **人物を描かないと毎回言い切る。** 状況説明を書くと、渡していない人物が湧く実機の
    記録がある（状況説明が空の3件は無人、空でない5件はすべて3〜6人が出現）。背景の派生は
    必ず文が入るので、ここは外せない。

    時間帯を変える回では小物を渡さない（`schemas.BackgroundVariationRequest` が断る）。
    背景1枚だけの編集なら時間帯は変えられるが、他の画像を混ぜた合成では変えられない。

    **小物を置く回は「image 1 の編集」だと言い切り、小物の画像は見た目の参考だけだと
    断る。** 「小物を image 1 の場所へ置け。小物は自分の画像のまま保て」とだけ
    書くと、実機（同じ種）では元の背景が別の場所に描き直された。
    校門の背景が、看板を正面に据えた無人の広場になった。小物の画像を素背景の1枚に
    替えても、「自分の画像のまま保て」の一文だけを外しても変わらなかった。描き直されなく
    なったのは、image 1 の編集だと言い、小物の画像の構図・カメラ・周りを使うなと言い、
    元からあるものを動かさない・消さないと言ったときである。言わないと、小物を主役に
    据えて、その周りへ image 1 の雰囲気だけを借りた場面が作られる。

    同じ条件で種を3つ変えて比べると、言い切らない書き方では1つが別の場所、1つが門の形の
    描き直しになり、言い切ると3つとも元の背景が残った。小物を2つ渡す（画像3枚）と元の背景の
    効きが弱まり、3つのうち1つはカメラが寄って構図が変わった。そこはまだ直り切って
    いない。テンプレートの `FluxKontextImageScale`（参照画像の縦横比を変える）を外すと
    悪化したので、縮尺は原因ではない。
    """
    lines: list[str] = []

    if kind == KIND_PROP_VARIATION and prop_count:
        objects = " and ".join(_PROP_LABELS[:prop_count])
        if prop_count == 1:
            references = (
                "Image 2 is only a reference for what the object looks like; do not use its "
                "framing, camera angle or surroundings."
            )
            once = "Show the object only once. Do not duplicate or repeat it."
        else:
            references = (
                "Images 2 and 3 are only references for what the objects look like; do not "
                "use their framing, camera angle or surroundings."
            )
            once = "Show each object only once. Do not duplicate or repeat them."
        lines.append(
            f"Edit image 1. Add {objects} into the scene of image 1, at a natural size for "
            f"where it is placed. {references}"
        )
        # 言わないと、崩れた複製がもう1つ描かれることがある（人物の複製と同じ形）。
        lines.append(once)
        lines.append(
            "Everything that is already in image 1 stays exactly as it is, in the same "
            "position, size and shape: do not remove, move, redraw or replace anything."
        )
    elif kind == KIND_TIME_VARIATION:
        lines.append(
            "Change only the time of day, the light and the weather of the place in image 1. "
            "Everything in the place stays exactly where it is."
        )

    if instruction.strip():
        text = instruction.strip()
        lines.append(text if text[-1] in "。.!?！？" else f"{text}.")

    lines.append(
        "Keep the same place, the same layout, the same camera position and the same "
        "perspective as image 1."
    )
    # 状況説明を書くと人が湧く。背景には必ず文が入るので、毎回打ち消す。
    lines.append("There are no people anywhere in the scene.")
    lines.append(
        "Render in the same visual style as image 1 "
        "(photographic references stay photographic, illustrated references stay illustrated). "
        "Do not turn photographic subjects into drawings, paintings or anime."
    )
    if illustration_style.strip():
        lines.append(f"作品全体の画風: {illustration_style.strip()}")
    return " ".join(lines)


def resolve_background_variation_sources(
    database: object, background: object, prop_asset_ids: list[str]
) -> list[DerivationSource]:
    """元の背景と小物から、派生に渡す画像を順序付きで集める。

    順序は 背景 → 小物。元になる1枚を image 1 に置く（指示文がその番号を指す）。
    """
    from .assets import AssetRepository
    from .composition import MAX_REFERENCE_IMAGES

    background_repo = AssetRepository(database, "background_assets")
    image_path = background_repo.image_path(background.id)  # type: ignore[attr-defined]
    if image_path is None or not image_path.is_file():
        raise DerivationError("元になる背景の画像がありません")

    sources = [DerivationSource(kind=KIND_BACKGROUND, image_path=image_path)]
    prop_repo = AssetRepository(database, "prop_assets")
    for prop_id in prop_asset_ids:
        prop = prop_repo.get(prop_id)
        prop_path = prop_repo.image_path(prop_id)
        if prop_path is None:
            raise DerivationError(f"小物「{prop.name}」に画像が登録されていません")
        sources.append(DerivationSource(kind=KIND_PROP, image_path=prop_path))

    if len(sources) > MAX_REFERENCE_IMAGES:
        raise DerivationError(
            f"一度に渡せる画像は{MAX_REFERENCE_IMAGES}枚までです"
            f"（元の背景1枚と、小物{MAX_REFERENCE_IMAGES - 1}つまで）"
        )
    return sources
