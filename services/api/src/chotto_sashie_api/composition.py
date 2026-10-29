"""静止画の合成: 人物の立ち絵と背景をQwen-Image-Edit-2511で1枚に合成する。

渡すのは**人物の立ち絵（最大2枚）と背景（1枚）**だけで、姿勢・表情・向きは指示文で
指定する。衣装と小物は立ち絵が持っているので、合成では渡さない（人物が持つ小物は
立ち絵の変更で、場に置く小物は背景のバリエーションへ写し込む）。

Qwen-Image-Edit-2511は複数の参照画像(image1/image2/image3)を受け取り、プロンプトに
従って1枚へ合成できる。ここではワークフローテンプレートの決まった画像スロットへ、
選ばれた画像を割り当て、使わないスロットは枝刈りする。任意ノードの注入はできず、
利用者入力は「どの立ち絵と背景を使うか」「人物ごとの指定」「状況の説明文」だけである。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chotto_sashie_shared.comfy_client import bind_workflow

MAX_REFERENCE_IMAGES = 3
# 1枚に入れられる人物の数。参照画像3枚のうち1枚は背景が使うため、残りが人物の枠になる。
# 背景を選んでいなければ3人を渡せてしまうが、上限を出し入れすると「さっきは選べたのに」が
# 起きるので、背景の有無にかかわらず2人で固定する。
MAX_CHARACTERS = MAX_REFERENCE_IMAGES - 1

# 高速化用LoRAとその前提（4ステップ・CFG無効）。丁寧に合成するときは外す。
_LIGHTNING_LORA_NODE = "3"
_SAMPLER_NODE = "4"
_UNET_NODE = "1"
FAST_STEPS = 4
FAST_CFG = 1.0
# 丁寧側。高速LoRA無しのQwen-Image-Editが指示を反映できる範囲で、
# 待ち時間が伸びすぎない値にする。
CAREFUL_STEPS = 24
CAREFUL_CFG = 2.5

# テンプレート(workflows/composition/qwen_scene_composition.json)のスロット構造。
# 各スロット: 参照名(bind), LoadImageノード, FluxKontextImageScaleノード,
# TextEncodeQwenImageEditPlusでの画像入力キー。
_SLOTS: list[dict[str, str]] = [
    {"bind": "source_image_1", "load": "11", "scale": "15", "image_input": "image1"},
    {"bind": "source_image_2", "load": "12", "scale": "16", "image_input": "image2"},
    {"bind": "source_image_3", "load": "13", "scale": "17", "image_input": "image3"},
]
# 参照画像を受け取るTextEncodeQwenImageEditPlusノード(正例・負例)。
_TEXT_ENCODE_NODES = ("20", "22")


# 参照画像の種別。プロンプトの言い回しをこれで決める。
KIND_CHARACTER = "character"
KIND_BACKGROUND = "background"
KIND_PROP = "prop"

# 何が写っているかを表す語。公式の例は "A woman in image 1" / "the dress in image 2"
# のように必ず種別を表す名詞を伴う。名前だけを置くと、モデルがそれを人物とも場所とも
# 判断できず、人数が増えたり別の場所になったりする。
_KIND_LABELS = {
    KIND_CHARACTER: "the person",
    KIND_BACKGROUND: "the background",
    KIND_PROP: "the object",
}


@dataclass(frozen=True)
class StillImageSource:
    """合成に使う1枚の参照画像（アップロード前のローカル情報）。"""

    kind: str
    name: str
    image_path: Path
    placement: str = ""
    # 人物だけが持つ、この静止画での指定。空なら立ち絵のままにする。
    pose: str = ""
    expression: str = ""
    direction: str = ""
    notes: str = ""


@dataclass(frozen=True)
class StillImageReference:
    """合成に使う1枚の参照画像。

    プロンプトは Qwen-Image-Edit が想定する `image 1 / image 2 / image 3` の形で
    組み立てる。そのため役割を文にした状態では持たず、種別と名前のまま渡す。
    """

    kind: str
    name: str
    comfy_image: str  # ComfyUIのLoadImageへ渡す名前（アップロード後の name）
    placement: str = ""
    pose: str = ""
    expression: str = ""
    direction: str = ""
    notes: str = ""

    @property
    def instruction(self) -> str:
        """この人物への指定を、日本語の文にまとめる。空なら立ち絵のまま。"""
        return person_instruction(
            pose=self.pose, direction=self.direction, expression=self.expression, notes=self.notes
        )


def person_instruction(*, pose: str, direction: str, expression: str, notes: str) -> str:
    """人物ごとの指定を、日本語の文にまとめる。空なら空文字。

    ラベルと読点で並べた形（「ポーズ: 走り、右向き」）は実機で反映されず、句点で
    区切った文にすると同じ指定語でも姿勢と向きが出た。指示文で描き起こす合成と、
    置いて合成するときの姿づくりが、同じ書き方を使う。
    """
    parts = [
        f"ポーズは{pose}。" if pose else "",
        f"体の向きは{direction}。" if direction else "",
        f"表情は{expression}。" if expression else "",
        f"{notes}。" if notes else "",
    ]
    return "".join(part for part in parts if part)


class CompositionError(ValueError):
    pass


def reference_image_count(
    *,
    background_asset_id: str | None,
    character_ids: list[str],
) -> int:
    """この組み合わせが使う参照画像の枚数。

    実際に集める `resolve_still_image_sources` と同じ数え方をする。まだ静止画を
    作っていない段階（追加する前）でも枚数を出せるように、選択だけから数える。
    """
    return len(character_ids) + (1 if background_asset_id else 0)


def over_limit_message(character_count: int) -> str:
    """人物が多すぎるときの言い方。作成時と合成時で同じ文面を使う。"""
    return (
        f"1枚に入れられる人物は{MAX_CHARACTERS}人までです（現在{character_count}人）。"
        "3人以上を出したいときは、人物を入れた1枚を背景として登録し直してください"
    )


def require_background(still_image: Any) -> None:
    """合成には背景が要る。どちらの作り方でも、人物を置く先・描き込む先になる。"""
    if still_image.background_asset_id is None:
        raise CompositionError(
            "背景が選ばれていません。「使う素材を変える」で背景を選んでください"
        )


def resolve_still_image_figure(database: Any, still_image_id: str, character_id: str) -> Any:
    """この静止画でこの人物に使う立ち絵。選んでいなければ既定の1枚。

    判定・表示・生成がここを共有する。別々に解決すると、画面が出しているサムネイルと
    実際に合成へ渡る1枚が食い違う。選んだ立ち絵が消えていたときも既定へ戻す
    （記録が残っていても実体が無ければ使えない）。
    """
    from .character_figures import CharacterFigureNotFoundError, CharacterFigureRepository
    from .still_image_characters import StillImageCharacterRepository

    figures = CharacterFigureRepository(database)
    chosen = StillImageCharacterRepository(database).get(still_image_id, character_id)
    if chosen is not None and chosen.figure_id:
        try:
            figure = figures.get(chosen.figure_id)
        except CharacterFigureNotFoundError:
            figure = None
        if (
            figure is not None
            and figure.character_id == character_id
            and figure.state == "completed"
            and figure.image_path
            and Path(figure.image_path).is_file()
        ):
            return figure
    default = figures.default_figure(character_id)
    if default is not None and Path(default.image_path).is_file():
        return default
    return None


def resolve_still_image_sources(database: Any, still_image: Any) -> list[StillImageSource]:
    """静止画が使う立ち絵と背景から、合成に渡す画像を順序付きで集める。

    順序は 人物 → 背景。合成の主対象である人物を先頭に置く（`_people_count` の
    「image 1 の人物」もこの順を前提にしている）。立ち絵が無い人物や、画像の無い
    背景があれば `CompositionError` を送出する。
    """
    # 遅延インポートで循環参照を避ける。
    from .assets import AssetRepository
    from .characters import CharacterRepository
    from .still_image_characters import StillImageCharacterRepository

    sources: list[StillImageSource] = []

    character_repo = CharacterRepository(database)
    chosen_repo = StillImageCharacterRepository(database)

    require_background(still_image)
    if len(still_image.character_ids) > MAX_CHARACTERS:
        raise CompositionError(over_limit_message(len(still_image.character_ids)))

    for character_id in still_image.character_ids:
        character = character_repo.get(character_id)
        figure = resolve_still_image_figure(database, still_image.id, character_id)
        if figure is None:
            raise CompositionError(
                f"人物「{character.name}」の立ち絵がありません。"
                "「登場人物」の画面で立ち絵を作ってください"
            )
        record = chosen_repo.get(still_image.id, character_id)
        spec = record.spec if record is not None else None
        sources.append(
            StillImageSource(
                kind=KIND_CHARACTER,
                name=character.name,
                image_path=Path(figure.image_path),
                placement=spec.placement if spec else "",
                pose=spec.pose if spec else "",
                expression=spec.expression if spec else "",
                direction=spec.direction if spec else "",
                notes=spec.notes if spec else "",
            )
        )

    if still_image.background_asset_id is not None:
        background_repo = AssetRepository(database, "background_assets")
        background = background_repo.get(still_image.background_asset_id)
        image_path = background_repo.image_path(still_image.background_asset_id)
        if image_path is None:
            raise CompositionError(f"背景「{background.name}」に画像が登録されていません")
        sources.append(
            StillImageSource(kind=KIND_BACKGROUND, name=background.name, image_path=image_path)
        )

    if not sources:
        raise CompositionError("合成には人物か背景を1つ以上選んでください")
    if len(sources) > MAX_REFERENCE_IMAGES:
        raise CompositionError(over_limit_message(len(still_image.character_ids)))
    return sources


# 同じ種別が複数あるときの呼び分け。名前の代わりに使う。
#
# 記号1文字（person A / person B）では区別が弱く、実測で人数が増え、眼鏡が別人へ
# 移った。序数の語に変えると、同じ種・同じ参照画像のまま人数も描き分けも直った。
_ORDINALS = ("first", "second", "third")


def _labels(references: list[StillImageReference]) -> list[str]:
    """各参照画像の呼び名を決める。素材名はプロンプトへ渡さない。

    素材名をそのまま渡すと、名前が持つ意味が絵の中身に効いてしまう。
    「実験用の教室」という名前を付けた教室の写真が、理科室として描き直された。
    名前は利用者が付けた見出しであって、写っているものの説明ではない。

    同じ種別が複数あるときだけ `the first person` / `the second person` と
    呼び分ける。1つしか無いなら `the person` のように種別だけで指せる。
    """
    totals: dict[str, int] = {}
    for reference in references:
        totals[reference.kind] = totals.get(reference.kind, 0) + 1

    used: dict[str, int] = {}
    labels: list[str] = []
    for reference in references:
        noun = _KIND_LABELS.get(reference.kind, "the subject")
        if totals[reference.kind] > 1:
            index = used.get(reference.kind, 0)
            used[reference.kind] = index + 1
            if index < len(_ORDINALS):
                noun = f"the {_ORDINALS[index]} {noun.removeprefix('the ')}"
        labels.append(noun)
    return labels


def _rewrite_situation(
    situation: str, references: list[StillImageReference], labels: list[str]
) -> str:
    """状況説明に書かれた素材名を、プロンプト側の呼び名へ置き換える。

    利用者は素材の名前で書く（「佐藤 花子が本を読んでいる」）。名前は分かって
    いるので、文字列としてそのまま置き換えれば、どの画像のことかが決まる。
    日本語を解釈する必要はない。

    姓だけ・名だけで書かれることもあるため、他の素材と重ならない部分も置き換える。
    """
    named = [(r.name.strip(), label) for r, label in zip(references, labels) if r.name.strip()]

    # 長い名前から先に置き換える。短い名前が長い名前の一部のことがある。
    for name, label in sorted(named, key=lambda pair: -len(pair[0])):
        situation = situation.replace(name, label)

    # 「佐藤 花子」を「花子」と書くような場合。1つの素材しか指さない語だけを扱う。
    owners: dict[str, set[str]] = {}
    for name, label in named:
        for part in name.split():
            if len(part) >= 2:
                owners.setdefault(part, set()).add(label)
    for part, labels_for_part in sorted(owners.items(), key=lambda pair: -len(pair[0])):
        if len(labels_for_part) == 1:
            situation = situation.replace(part, next(iter(labels_for_part)))
    return situation


def _subject(label: str, number: int, placement: str = "") -> str:
    """参照画像1枚を `... in image N` の形で書く。

    引用符は使わない。Qwenのプロンプト規約では引用符で囲んだ文字列は
    「画像に描き込む文字」の合図で、囲むと絵の中に文字として書かれてしまう。

    配置は利用者が書いた日本語がそのまま英語の句の中へ入る（`placed 画面の右寄り`）。
    見た目は不揃いだが、**英語へ直してはいけない**。実機で同じ人物・同じ種で比べた
    ところ、こうなった。

    - 配置なし … 中央。1人
    - `placed 画面の右寄り` … 中央のまま。1人（効かないが害もない）
    - `placed on the right side of the frame` … **人物が2人に増えた**
    - `placed far to the right of the frame, away from the centre` … 中央のまま。1人
    - 状況説明ごと英語 … **本人の複製が2人並んだ**

    英語で位置を言うと「右側に誰かを置く」と読まれ、`_people_count` の歯止めにも
    勝ってしまう。日本語のままなら、少なくとも人数は壊れない。

    日本語のままの配置は、**立ち絵を渡す経路では効く**（実測）。
    効かなかったのは顔のアップを渡していたときで、そのときは中央へ寄った。渡す1枚が
    全身かどうかで結果が変わる。
    """
    text = f"{label} in image {number}"
    if placement.strip():
        text = f"{text} placed {placement.strip()}"
    return text


_COUNT_WORDS = ("one", "two", "three")


def _people_count(count: int) -> str:
    """画面に何人いて、その人を何回描くのかを言い切る。

    渡した人物を保てとは言っていても、人数は誰も縛っていなかった。実機では人物1枚＋
    背景1枚しか渡していないのに人が増える。増えるのは知らない他人ではなく**主役の
    複製**で、単色背景の縦長ポートレートを横長の場面へ入れるときに、空いた側を同じ
    人物で埋める形で出る（背景を渡さないと4人まで増えた）。

    高速化LoRAの4ステップ経路は cfg 1.0 でCFGが働かず、負例プロンプトが一切効かない。
    歯止めは正例の文で掛けるしかない。

    言い回しは実機で比べて選んだ。人数を数えるだけでは減らず、「複製するな」と足しても
    減らず、「1人だけで他には誰もいない・1回だけ描け」で初めて減った。「それ以外は空の
    部屋」まで強めると机や黒板まで消えたので、そこまでは言わない。

    人物の参照画像は必ず先頭に並ぶので、1人のときは image 1 がその人を指す。
    """
    if count == 1:
        return (
            "The scene contains a single person and nobody else. "
            "Show the person from image 1 only once. Do not duplicate or repeat them."
        )
    word = _COUNT_WORDS[count - 1] if count <= len(_COUNT_WORDS) else str(count)
    return (
        f"The scene contains exactly {word} people and nobody else. "
        "Show each of them only once. Do not duplicate or repeat them."
    )


def _join(parts: list[str]) -> str:
    if len(parts) == 1:
        return parts[0]
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


def _sentence(text: str) -> str:
    """文末に句点が無ければ足す。状況説明は日本語で書かれることが多い。"""
    return text if text[-1] in "。.!?！？" else f"{text}."


def build_composition_prompt(
    references: list[StillImageReference],
    situation: str,
    illustration_style: str = "",
) -> str:
    """参照画像と指定から、合成用のプロンプトを組み立てる。

    書き方は Qwen-Image-Edit が想定している形に合わせる。公式の例は
    `A woman in image 1 is riding a vintage in image 2.` のように、image 番号を
    文の中に埋め込む。番号を宣言してから状況を別の文に置くと、その状況が
    どの画像のことなのか決まらず、人物が複数いると誰の話か定まらない。

    **保つのは顔・髪・目の色・体型・衣装・小物、変えるのは姿勢・しぐさ・向き・表情。**
    衣装を保つ側に入れるのは、立ち絵が衣装を持っているため（着替えは立ち絵の変更で別の
    立ち絵を作る）。姿勢を変える側に入れるのは、渡すのが立ち姿の1枚だからである。

    **姿勢の指定が無い人物には、変える指示を出さない。** 空欄に文を当てると、書いて
    いない指示が効く。指定のある人物だけに「この指定に従って変える」と言い、指定の
    無い人物には「立ち絵のまま」と言う。人物ごとに文を分けるのは、一方は姿勢を変え、
    もう一方はそのまま、という組み合わせが1つの文では言えないため。

    指定は**日本語のまま**渡す。配置を英語にすると人物が増えた実機の記録があるため、
    まとめて英語へ直すことはしない。並べ方・語順・英語化は、同じ種・同じ立ち絵で
    1つずつ比べて決める（この形が最初に測る形）。

    人物向けの言い回しは、人物が居るときだけ出す。人物を1枚も渡していない合成でも
    人物の話を続けると、居ないはずの人物が描かれる。

    画風を「illustration」へ固定する定型句は入れない。作品が写実指定でも
    イラスト調へ寄ってしまうため（人物生成側と同じ理由）。作品の画風指定が
    あればそれを渡し、無ければ参照画像の描かれ方をそのまま保たせる。
    """
    labels = _labels(references)
    numbered = [
        (number, reference, label)
        for number, (reference, label) in enumerate(zip(references, labels), start=1)
    ]
    characters = [item for item in numbered if item[1].kind == KIND_CHARACTER]
    backgrounds = [item for item in numbered if item[1].kind == KIND_BACKGROUND]
    background = backgrounds[0] if backgrounds else None

    lines: list[str] = []

    # 何がどの画像に写っていて、どこに置くのかを1文で言い切る。
    if characters:
        subjects = _join([_subject(label, n, r.placement) for n, r, label in characters])
        verb = "is" if len(characters) == 1 else "are"
        if background is not None:
            b_number, _b_reference, b_label = background
            lines.append(f"{subjects} {verb} in {_subject(b_label, b_number)}.")
        else:
            lines.append(f"{subjects} {verb} together in one scene.")
        lines.append(_people_count(len(characters)))

        # 人物ごとの指定。書かれている人にだけ出す。
        for number, reference, label in characters:
            instruction = reference.instruction
            if instruction:
                lines.append(f"{_subject(label, number)}: {instruction}")
    elif background is not None:
        b_number, _b_reference, b_label = background
        lines.append(f"Keep {_subject(b_label, b_number)} as the whole scene.")

    if situation.strip():
        lines.append(_sentence(_rewrite_situation(situation.strip(), references, labels)))

    if characters:
        lines.append(
            "Keep each person's face, hairstyle, hair color, eye color, body proportions, "
            "outfit and accessories exactly as in their own image."
        )
        changing = [item for item in characters if item[1].instruction]
        keeping = [item for item in characters if not item[1].instruction]
        if changing:
            subjects = _join([_subject(label, n) for n, _r, label in changing])
            lines.append(
                "Change the pose, gesture, body orientation and facial expression of "
                f"{subjects} to follow the instruction written for them above."
            )
        if keeping:
            subjects = _join([_subject(label, n) for n, _r, label in keeping])
            verb = "keeps" if len(keeping) == 1 else "keep"
            lines.append(
                f"{subjects} {verb} the pose, gesture, body orientation and facial "
                "expression from their own image."
            )
    if background is not None:
        lines.append(f"Keep the layout of image {background[0]} unchanged.")
    if len(references) > 1:
        lines.append(
            "Match lighting, perspective and scale across all elements so they belong to "
            "the same scene."
        )

    lines.append(
        "Render the whole scene in the same visual style as the reference images "
        "(photographic references stay photographic, illustrated references stay illustrated). "
        "Do not turn photographic subjects into drawings, paintings or anime."
    )
    if illustration_style.strip():
        lines.append(f"作品全体の画風: {illustration_style.strip()}")
    return " ".join(lines)


def build_composition_workflow(
    template: dict[str, Any],
    *,
    references: list[StillImageReference],
    prompt: str,
    negative_prompt: str,
    seed: int,
    width: int,
    height: int,
    diffusion_model: str,
    text_encoder: str,
    vae: str,
    lightning_lora: str,
    filename_prefix: str,
    careful: bool = False,
) -> dict[str, Any]:
    """合成テンプレートに参照画像とモデル・プロンプトを割り当てる。

    参照画像は1〜3枚。使わないスロットのLoadImage/スケールノードと、
    TextEncodeQwenImageEditPlusの該当image入力を取り除く。

    `careful` のときは高速化LoRAを外し、ステップ数とCFGを上げる。4ステップでは
    指示の反映が弱く、姿勢の維持などが崩れやすいため。時間は数倍になる。
    """
    if not references:
        raise CompositionError("合成には少なくとも1枚の参照画像が必要です")
    if len(references) > MAX_REFERENCE_IMAGES:
        raise CompositionError(
            f"参照画像は最大{MAX_REFERENCE_IMAGES}枚までです（指定{len(references)}枚）"
        )

    workflow = deepcopy(template)
    used = len(references)

    # 使わないスロットを枝刈りする。
    for slot in _SLOTS[used:]:
        workflow.pop(slot["load"], None)
        workflow.pop(slot["scale"], None)
        for node_id in _TEXT_ENCODE_NODES:
            node = workflow.get(node_id)
            if isinstance(node, dict):
                node.get("inputs", {}).pop(slot["image_input"], None)

    if careful:
        # 高速化LoRAのノードを外し、サンプラーを素のモデルへ直結する。
        workflow.pop(_LIGHTNING_LORA_NODE, None)
        sampler = workflow.get(_SAMPLER_NODE)
        if isinstance(sampler, dict):
            sampler.setdefault("inputs", {})["model"] = [_UNET_NODE, 0]

    values: dict[str, Any] = {
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "seed": seed,
        "steps": CAREFUL_STEPS if careful else FAST_STEPS,
        "cfg": CAREFUL_CFG if careful else FAST_CFG,
        "width": width,
        "height": height,
        "diffusion_model": diffusion_model,
        "text_encoder": text_encoder,
        "vae": vae,
        "filename_prefix": filename_prefix,
    }
    if not careful:
        # 丁寧のときはノードごと外しているので、割り当て先が無い。
        values["lightning_lora"] = lightning_lora
    for slot, reference in zip(_SLOTS, references):
        values[slot["bind"]] = reference.comfy_image

    return bind_workflow(workflow, values)
