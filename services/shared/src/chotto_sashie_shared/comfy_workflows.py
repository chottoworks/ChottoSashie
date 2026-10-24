"""人物候補と立ち絵の生成で使うComfyUIワークフローの束縛（共通の土台）。

テンプレートは `workflows/z_image/` にあり、許可済みの入力だけを書き換える。
任意ノードの注入はできない。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .comfy_client import bind_workflow


def z_image_workflow_dir(resources_root: Path) -> Path:
    return resources_root / "workflows" / "z_image"


def candidate_workflow_path(resources_root: Path) -> Path:
    return z_image_workflow_dir(resources_root) / "zimage_candidate_core.json"


def angle_reference_workflow_path(resources_root: Path) -> Path:
    return (
        z_image_workflow_dir(resources_root)
        / "qwen_character_reference_2511_angles_external.json"
    )


def comfy_output_reference(subfolder: str, filename: str) -> str:
    """ComfyUIの注釈付きパスを組み立てる（既存outputをinputへ複製せずに読む）。

    アプリ本体の生成では使わない（出力フォルダは片付けるので、起点はアプリの控えから
    送る）。手元のComfyUIの出力をそのまま読む診断ツールのためのもの。
    """
    folder = subfolder.strip("\\/")
    return f"{folder}\\{filename} [output]" if folder else f"{filename} [output]"


def bind_turbo_workflow(
    template: dict[str, Any],
    *,
    prompt: str,
    negative_prompt: str,
    seed: int,
    width: int,
    height: int,
    diffusion_model: str,
    text_encoder: str,
    vae: str,
    filename_prefix: str = "ChottoSashie/turbo",
    cfg: float | None = None,
) -> dict[str, Any]:
    """管理対象テンプレートの許可済み入力だけを書き換える。

    cfg は候補生成のようにテンプレートがcfgバインディングを持つときだけ指定する。
    Turbo・ベンチマークのテンプレートはcfgを固定値のまま使うため未指定にする。
    """
    values: dict[str, Any] = {
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "seed": seed,
        "width": width,
        "height": height,
        "diffusion_model": diffusion_model,
        "text_encoder": text_encoder,
        "vae": vae,
        "filename_prefix": filename_prefix,
    }
    if cfg is not None:
        values["cfg"] = cfg
    return bind_workflow(template, values)


def bind_angle_reference_workflow(
    template: dict[str, Any],
    *,
    prompt: str,
    seed: int,
    source_image: str,
    diffusion_model: str,
    text_encoder: str,
    vae: str,
    angle_lora: str,
    lightning_lora: str,
    filename_prefix: str,
    weight_dtype: str = "fp8_e4m3fn",
    angle_lora_strength: float = 0.9,
) -> dict[str, Any]:
    """2511角度LoRA用テンプレートの許可入力だけを書き換える。"""
    values: dict[str, Any] = {
        "angle_lora_strength": angle_lora_strength,
        "prompt": prompt,
        # 配布元ワークフローは空の負例条件を使用する。
        "negative_prompt": "",
        "seed": seed,
        "source_image": source_image,
        "diffusion_model": diffusion_model,
        "text_encoder": text_encoder,
        "vae": vae,
        "angle_lora": angle_lora,
        "lightning_lora": lightning_lora,
        "filename_prefix": filename_prefix,
        "weight_dtype": weight_dtype,
    }
    return bind_workflow(template, values)


def apply_high_precision_angle(workflow: dict[str, Any]) -> None:
    """角度LoRAをLightningから切り離し、左右方位を優先する20ステップ設定にする。"""
    try:
        workflow["4"]["inputs"]["model"] = ["2", 0]
        workflow["15"]["inputs"]["steps"] = 20
        workflow["15"]["inputs"]["cfg"] = 3.5
    except (KeyError, TypeError) as error:
        raise ValueError("角度参照ワークフローの高精度設定先が正しくありません") from error
