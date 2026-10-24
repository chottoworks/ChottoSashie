"""ComfyUIのモデルフォルダの並び（種別ごとのフォルダ名）を扱う。

マニフェストの保存先は `models/<種別>/<ファイル名>` で書いてあるが、実際に置く
場所は「アプリの管理領域」「共有フォルダ」「利用者の既存モデルフォルダ」と複数
あり、既存フォルダでは種別のフォルダ名に別名（unet, clip など）が使われている
ことがある。ここでは models フォルダ相当を基準にした相対パスへ揃える。
"""

from __future__ import annotations

from pathlib import Path

# 種別と、環境によって使われる別名。先頭が標準名（新規作成するときの名前）。
MODEL_DIRECTORY_ALIASES: dict[str, tuple[str, ...]] = {
    "checkpoints": ("checkpoints",),
    "diffusion_models": ("diffusion_models", "unet"),
    "text_encoders": ("text_encoders", "clip"),
    "vae": ("vae",),
    "loras": ("loras", "lora"),
    "controlnet": ("controlnet", "control_net", "adapters"),
    "upscale_models": ("upscale_models", "upscale"),
    "latent_upscale_models": ("latent_upscale_models", "latentupscale"),
    "clip_vision": ("clip_vision",),
    "embeddings": ("embeddings",),
    "audio_encoders": ("audio_encoders", "audioencoders"),
    "model_patches": ("model_patches",),
}


def strip_models_prefix(destination: str) -> str:
    """マニフェストの `models/...` を models フォルダ基準の相対パスにする。"""
    relative = destination.replace("\\", "/").lstrip("/")
    prefix = "models/"
    return relative[len(prefix) :] if relative.startswith(prefix) else relative


def models_relative_path(models_root: Path, destination: str) -> str:
    """そのモデルフォルダの中での保存先（相対パス）を返す。

    種別のフォルダがすでに別名で存在するならその名前に合わせ、無ければ標準名で
    作る。既存フォルダに `unet` があるのに `diffusion_models` を新たに作って
    しまうと、同じ種別が二か所に散らばるため。
    """
    relative = strip_models_prefix(destination)
    category, _, name = relative.partition("/")
    if not name:
        return relative
    aliases = MODEL_DIRECTORY_ALIASES.get(category)
    if not aliases:
        return relative
    try:
        existing = {
            child.name.casefold(): child.name
            for child in models_root.expanduser().iterdir()
            if child.is_dir()
        }
    except OSError:
        existing = {}
    for alias in aliases:
        if alias in existing:
            return f"{existing[alias]}/{name}"
    return f"{category}/{name}"
