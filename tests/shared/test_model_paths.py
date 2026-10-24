"""生成に使うモデルの探索を確かめる。"""

from pathlib import Path

import pytest

from chotto_sashie_shared.shared_env import shared_models_root
from chotto_sashie_shared.model_paths import (
    ModelResolutionError,
    find_model_file,
    model_search_roots,
)
from chotto_sashie_shared.setup_config import SetupConfigRead, SetupMode


def _config(**overrides) -> SetupConfigRead:
    values = {
        "mode": SetupMode.MANAGED_EXTERNAL_MODELS,
        "base_url": "",
        "port": 8199,
        "external_model_roots": [],
        "comfyui_path": "",
        "python_path": "",
        "resolved_base_url": "http://127.0.0.1:8199",
        "configured": True,
        "setup_completed": True,
        "updated_at": None,
    }
    values.update(overrides)
    return SetupConfigRead(**values)


def test_search_roots_order_and_existence(tmp_path: Path) -> None:
    """共有フォルダ→既存モデルフォルダ→既存ComfyUIの順で、実在するものだけ返す。"""
    shared = shared_models_root()
    shared.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    comfyui = tmp_path / "ComfyUI"
    (comfyui / "models").mkdir(parents=True)

    roots = model_search_roots(
        _config(
            external_model_roots=[str(external), str(tmp_path / "missing")],
            comfyui_path=str(comfyui),
        )
    )
    assert roots == [shared, external, comfyui / "models"]


def test_search_roots_deduplicates(tmp_path: Path) -> None:
    external = tmp_path / "models"
    external.mkdir()
    roots = model_search_roots(_config(external_model_roots=[str(external), str(external)]))
    assert roots == [tmp_path / "models"]


def test_find_model_file_prefers_category_folder(tmp_path: Path) -> None:
    root = tmp_path / "models"
    (root / "diffusion_models").mkdir(parents=True)
    expected = root / "diffusion_models" / "z_image_bf16.safetensors"
    expected.write_bytes(b"model")
    # 別階層にも同名ファイルがあるが、category直下を優先する。
    (root / "other").mkdir()
    (root / "other" / "z_image_bf16.safetensors").write_bytes(b"other")

    found = find_model_file([root], "diffusion_models", "z_image_bf16.safetensors")
    assert found == expected.resolve()


def test_find_model_file_falls_back_to_recursive_search(tmp_path: Path) -> None:
    root = tmp_path / "models"
    nested = root / "deep" / "nested"
    nested.mkdir(parents=True)
    expected = nested / "ae.safetensors"
    expected.write_bytes(b"vae")

    assert find_model_file([root], "vae", "ae.safetensors") == expected.resolve()


def test_find_model_file_missing_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ModelResolutionError, match="vae/ae.safetensors"):
        find_model_file([tmp_path], "vae", "ae.safetensors")


def _provision_dedicated_comfyui() -> Path:
    """アプリが用意した専用ComfyUI（同梱Python付き）があることにする。"""
    portable = shared_comfyui_root() / "ComfyUI_windows_portable"
    (portable / "ComfyUI").mkdir(parents=True)
    (portable / "ComfyUI" / "main.py").write_text("", encoding="utf-8")
    python = portable / "python_embeded" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    return python.resolve()
