import json
from pathlib import Path

import pytest

from chotto_sashie_api.composition import (
    CompositionError,
    StillImageReference,
    build_composition_prompt,
    build_composition_workflow,
)

_TEMPLATE_PATH = Path("workflows/composition/qwen_scene_composition.json")


def _template() -> dict:
    return json.loads(_TEMPLATE_PATH.read_text(encoding="utf-8"))


def _bind(references: list[StillImageReference]) -> dict:
    return build_composition_workflow(
        _template(),
        references=references,
        prompt="scene",
        negative_prompt="low quality",
        seed=123,
        width=1280,
        height=720,
        diffusion_model="qwen.safetensors",
        text_encoder="clip.safetensors",
        vae="vae.safetensors",
        lightning_lora="lightning.safetensors",
        filename_prefix="ChottoSashie/still_images/abc",
    )


def test_two_references_prune_the_third_slot() -> None:
    workflow = _bind(
        [
            StillImageReference(kind="character", name="A", comfy_image="a.png"),
            StillImageReference(kind="background", name="B", comfy_image="b.png"),
        ]
    )

    assert "_chotto_sashie_bindings" not in workflow
    # 使ったスロットの画像が設定される。
    assert workflow["11"]["inputs"]["image"] == "a.png"
    assert workflow["12"]["inputs"]["image"] == "b.png"
    # 3枚目のLoadImage／スケールノードは枝刈りされる。
    assert "13" not in workflow
    assert "17" not in workflow
    # 正例・負例のテキストエンコードから image3 が外れ、image1/image2 は残る。
    for node_id in ("20", "22"):
        assert "image3" not in workflow[node_id]["inputs"]
        assert workflow[node_id]["inputs"]["image1"] == ["15", 0]
        assert workflow[node_id]["inputs"]["image2"] == ["16", 0]
    # モデル・プロンプト・サイズ・seedのバインド。
    assert workflow["1"]["inputs"]["unet_name"] == "qwen.safetensors"
    assert workflow["3"]["inputs"]["lora_name"] == "lightning.safetensors"
    assert workflow["20"]["inputs"]["prompt"] == "scene"
    assert workflow["22"]["inputs"]["prompt"] == "low quality"
    assert workflow["28"]["inputs"]["width"] == 1280
    assert workflow["28"]["inputs"]["height"] == 720
    assert workflow["30"]["inputs"]["seed"] == 123
    assert workflow["40"]["inputs"]["filename_prefix"] == "ChottoSashie/still_images/abc"


def test_single_reference_prunes_two_slots() -> None:
    workflow = _bind([StillImageReference(kind="character", name="A", comfy_image="a.png")])

    assert workflow["11"]["inputs"]["image"] == "a.png"
    for pruned in ("12", "13", "16", "17"):
        assert pruned not in workflow
    for node_id in ("20", "22"):
        assert "image2" not in workflow[node_id]["inputs"]
        assert "image3" not in workflow[node_id]["inputs"]
        assert workflow[node_id]["inputs"]["image1"] == ["15", 0]


def test_three_references_keep_all_slots() -> None:
    workflow = _bind(
        [
            StillImageReference(kind="character", name="A", comfy_image="a.png"),
            StillImageReference(kind="background", name="B", comfy_image="b.png"),
            StillImageReference(kind="prop", name="C", comfy_image="c.png"),
        ]
    )
    for kept in ("11", "12", "13", "15", "16", "17"):
        assert kept in workflow
    assert workflow["13"]["inputs"]["image"] == "c.png"
    assert workflow["20"]["inputs"]["image3"] == ["17", 0]


def test_no_reference_is_rejected() -> None:
    with pytest.raises(CompositionError):
        _bind([])


def test_too_many_references_are_rejected() -> None:
    with pytest.raises(CompositionError):
        _bind([StillImageReference(kind="prop", name=f"r{i}", comfy_image=f"{i}.png") for i in range(4)])


def test_prompt_refers_to_each_image_the_way_the_model_expects() -> None:
    """Qwen-Image-Editが想定する `... in image N` の形で書く。

    公式の例は `A woman in image 1 is riding a vintage in image 2.` のように
    image 番号を文へ埋め込む。番号の宣言と状況説明を別の文に分けると、その状況が
    どの画像のことか決まらない。
    """
    prompt = build_composition_prompt(
        [
            StillImageReference(kind="character", name="Mina", comfy_image="a.png"),
            StillImageReference(kind="background", name="classroom", comfy_image="b.png"),
        ],
        "Mina waves at the window",
    )
    assert "the person in image 1 is in the background in image 2." in prompt
    assert "Keep each person's" in prompt
    # 素材名はプロンプトへ渡さない。名前の意味が絵の中身に効いてしまう。
    assert "Mina" not in prompt
    assert "classroom" not in prompt
    # 状況説明に書かれた名前は、その画像の呼び名へ置き換える。
    assert "the person waves at the window." in prompt


def test_prompt_places_a_character_where_the_state_says() -> None:
    prompt = build_composition_prompt(
        [
            StillImageReference(kind="character", name="Mina", comfy_image="a.png", placement="左側"),
            StillImageReference(kind="background", name="classroom", comfy_image="b.png"),
        ],
        "",
    )
    assert "the person in image 1 placed 左側" in prompt


def test_prompt_without_characters_never_mentions_people() -> None:
    """人物を1枚も渡していない合成で人物の話を続けると、居ない人物が描かれる。"""
    prompt = build_composition_prompt(
        [StillImageReference(kind="background", name="classroom", comfy_image="b.png")],
        "真昼",
    )
    assert "Keep the background in image 1 as the whole scene." in prompt
    assert "真昼." in prompt
    for word in ("person", "character", "people", "anyone"):
        assert word not in prompt.lower()


def test_prompt_keeps_the_outfit_and_changes_only_what_is_asked() -> None:
    """立ち絵が持つ衣装・小物は保ち、変えるのは姿勢・向き・表情だけにする。

    渡すのは立ち姿の1枚なので、「姿勢も表情も元画像どおり」と言い切ると
    指定と正面衝突する。逆に衣装まで変えてよいことにすると、立ち絵の変更で着せた服が
    合成で失われる。
    """
    prompt = build_composition_prompt(
        [
            StillImageReference(
                kind="character", name="Mina", comfy_image="a.png", pose="走っている"
            ),
            StillImageReference(kind="background", name="classroom", comfy_image="b.png"),
        ],
        "",
    )
    assert "the person in image 1: ポーズは走っている。" in prompt
    assert (
        "Keep each person's face, hairstyle, hair color, eye color, body proportions, "
        "outfit and accessories exactly as in their own image." in prompt
    )
    assert (
        "Change the pose, gesture, body orientation and facial expression of "
        "the person in image 1" in prompt
    )


def test_prompt_keeps_the_pose_when_nothing_is_asked() -> None:
    """指定の無い人物へ「変えろ」とは言わない（空欄に文を当てない）。"""
    prompt = build_composition_prompt(
        [
            StillImageReference(kind="character", name="Mina", comfy_image="a.png"),
            StillImageReference(kind="background", name="classroom", comfy_image="b.png"),
        ],
        "",
    )
    assert (
        "the person in image 1 keeps the pose, gesture, body orientation and facial "
        "expression from their own image." in prompt
    )
    assert "Change the pose" not in prompt


def test_prompt_keeps_a_japanese_situation_as_written() -> None:
    """状況説明は日本語で書かれる。句点があるなら足さない。"""
    prompt = build_composition_prompt(
        [StillImageReference(kind="background", name="classroom", comfy_image="b.png")],
        "夕方の日差しが差し込んでいる。",
    )
    assert "夕方の日差しが差し込んでいる。" in prompt
    assert "。." not in prompt


def test_prompt_does_not_force_an_illustration_look() -> None:
    """写実指定の作品がイラスト調へ寄らないよう、画風を固定する定型句を入れない。"""
    prompt = build_composition_prompt(
        [StillImageReference(kind="character", name="Mina", comfy_image="a.png")],
        "",
    )
    assert "illustration that merges" not in prompt
    assert "photographic references stay photographic" in prompt


def test_prompt_carries_the_project_style() -> None:
    """作品全体の画風を指定していれば、合成にも同じ画風を伝える。"""
    prompt = build_composition_prompt(
        [StillImageReference(kind="character", name="Mina", comfy_image="a.png")],
        "",
        "昭和の映画風実写写真。フォトリアリスティック。",
    )
    assert "作品全体の画風: 昭和の映画風実写写真。フォトリアリスティック。" in prompt


def test_careful_quality_drops_the_speed_lora_and_raises_steps() -> None:
    """丁寧に合成するときは高速化LoRAを外し、ステップ数とCFGを上げる。

    4ステップでは指示の反映が弱く、姿勢の維持などが崩れやすいため。
    """
    from chotto_sashie_api.composition import CAREFUL_CFG, CAREFUL_STEPS, FAST_STEPS

    fast = _bind([StillImageReference(kind="prop", name="r1", comfy_image="1.png")])
    assert fast["30"]["inputs"]["steps"] == FAST_STEPS
    assert "3" in fast
    assert fast["4"]["inputs"]["model"] == ["3", 0]

    careful = build_composition_workflow(
        _template(),
        references=[StillImageReference(kind="prop", name="r1", comfy_image="1.png")],
        prompt="p",
        negative_prompt="n",
        seed=1,
        width=1280,
        height=720,
        diffusion_model="d.safetensors",
        text_encoder="t.safetensors",
        vae="v.safetensors",
        lightning_lora="l.safetensors",
        filename_prefix="pre",
        careful=True,
    )
    # 高速化LoRAのノードごと外し、サンプラーは素のモデルへ直結する。
    assert "3" not in careful
    assert careful["4"]["inputs"]["model"] == ["1", 0]
    assert careful["30"]["inputs"]["steps"] == CAREFUL_STEPS
    assert careful["30"]["inputs"]["cfg"] == CAREFUL_CFG


def test_prompt_tells_two_people_apart_without_using_their_names() -> None:
    """人物が複数いるときは序数で呼び分ける。

    素材名をそのまま渡すと、名前の意味が絵の中身に効いてしまう。かといって
    呼び分けが無いと、状況説明がどちらの人物のことか決まらない。
    """
    prompt = build_composition_prompt(
        [
            StillImageReference(kind="character", name="佐藤 花子", comfy_image="a.png"),
            StillImageReference(kind="character", name="鈴木 陽菜", comfy_image="b.png"),
            StillImageReference(kind="background", name="実験用の教室", comfy_image="c.png"),
        ],
        "佐藤 花子が窓辺で本を読んでいる",
    )
    assert (
        "the first person in image 1 and the second person in image 2 "
        "are in the background in image 3."
    ) in prompt
    # 状況説明の名前は呼び名へ置き換わる。これで誰のことかが決まる。
    assert "the first personが窓辺で本を読んでいる." in prompt
    for name in ("佐藤", "花子", "鈴木", "陽菜", "実験用の教室"):
        assert name not in prompt


def test_prompt_accepts_a_partial_name_in_the_situation() -> None:
    """姓だけ・名だけで書かれても、他と重ならなければ結びつける。"""
    prompt = build_composition_prompt(
        [
            StillImageReference(kind="character", name="佐藤 花子", comfy_image="a.png"),
            StillImageReference(kind="character", name="鈴木 陽菜", comfy_image="b.png"),
        ],
        "花子は本を読み、陽菜は窓の外を見ている",
    )
    assert "the first personは本を読み、the second personは窓の外を見ている." in prompt
