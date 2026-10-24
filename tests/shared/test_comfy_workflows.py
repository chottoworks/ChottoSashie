import json
from pathlib import Path

from chotto_sashie_shared.comfy_client import output_filenames, output_images
from chotto_sashie_shared.comfy_workflows import (
    bind_angle_reference_workflow,
    bind_turbo_workflow,
)


def test_bind_only_updates_declared_workflow_inputs() -> None:
    path = Path("workflows/z_image/zimage_basic_core.json")
    template = json.loads(path.read_text(encoding="utf-8"))

    workflow = bind_turbo_workflow(
        template,
        prompt="夜の図書館",
        negative_prompt="low quality",
        seed=123,
        width=768,
        height=1024,
        diffusion_model="diffusion.safetensors",
        text_encoder="text.safetensors",
        vae="vae.safetensors",
        filename_prefix="ChottoSashie/characters/test/01",
    )

    assert "_chotto_sashie_bindings" not in workflow
    assert workflow["4"]["inputs"]["text"] == "夜の図書館"
    assert workflow["7"]["inputs"]["seed"] == 123
    assert workflow["6"]["inputs"]["width"] == 768
    assert workflow["9"]["inputs"]["filename_prefix"] == "ChottoSashie/characters/test/01"
    # cfgバインディングを持たないTurboテンプレートは、cfg未指定で固定値を保つ。
    assert workflow["7"]["inputs"]["cfg"] == 1.0


def test_candidate_template_binds_variation_strength_to_cfg() -> None:
    path = Path("workflows/z_image/zimage_candidate_core.json")
    template = json.loads(path.read_text(encoding="utf-8"))

    workflow = bind_turbo_workflow(
        template,
        prompt="丸顔の女性",
        negative_prompt="low quality",
        seed=7,
        cfg=2.5,
        width=512,
        height=688,
        diffusion_model="z_image_bf16.safetensors",
        text_encoder="text.safetensors",
        vae="vae.safetensors",
        filename_prefix="ChottoSashie/characters/test/01",
    )

    # 変化強度はcfgへ反映され、ベースモデルとステップ数は候補用の固定値を保つ。
    assert workflow["7"]["inputs"]["cfg"] == 2.5
    assert workflow["7"]["inputs"]["steps"] == 24
    assert workflow["1"]["inputs"]["unet_name"] == "z_image_bf16.safetensors"


def test_output_filenames_reads_comfy_history() -> None:
    entry = {
        "outputs": {
            "9": {
                "images": [
                    {
                        "filename": "turbo_00001_.png",
                        "subfolder": "ChottoSashie",
                        "type": "output",
                    }
                ]
            }
        }
    }

    assert output_filenames(entry) == ["ChottoSashie/turbo_00001_.png"]
    assert output_images(entry) == [
        {
            "filename": "turbo_00001_.png",
            "subfolder": "ChottoSashie",
            "type": "output",
        }
    ]


def test_bind_angle_reference_workflow_uses_2511_inputs() -> None:
    path = Path("workflows/z_image/qwen_character_reference_2511_angles_external.json")
    template = json.loads(path.read_text(encoding="utf-8"))

    workflow = bind_angle_reference_workflow(
        template,
        prompt="<sks> right side view eye-level shot close-up",
        seed=2511,
        source_image="figure-job-base.png",
        diffusion_model="qwen-2511.safetensors",
        text_encoder="vision.safetensors",
        vae="image-vae.safetensors",
        angle_lora="angles.safetensors",
        lightning_lora="lightning-2511.safetensors",
        filename_prefix="ChottoSashie/references/right-profile",
    )

    assert "_chotto_sashie_bindings" not in workflow
    assert workflow["8"]["inputs"]["image"] == "figure-job-base.png"
    assert workflow["10"]["inputs"]["prompt"].startswith("<sks>")
    assert workflow["12"]["inputs"]["prompt"] == ""
    assert workflow["15"]["inputs"]["seed"] == 2511
    assert workflow["2"]["inputs"]["lora_name"] == "angles.safetensors"
    assert workflow["3"]["inputs"]["lora_name"] == "lightning-2511.safetensors"
