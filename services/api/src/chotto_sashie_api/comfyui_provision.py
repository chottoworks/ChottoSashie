"""ComfyUI本体の自動用意。実体は chotto_sashie_shared.comfyui_provision（共通の土台）。"""

from __future__ import annotations

from chotto_sashie_shared.comfyui_provision import (
    SOURCE_EXISTING,
    SOURCE_SHARED,
    ComfyUIProvisionError,
    classify_comfyui_source,
    comfyui_update_available,
    extract_archive,
    install_root_of,
    installed_comfyui_version,
    is_provisioned,
    load_comfyui_spec,
    managed_comfyui_paths,
    portable_root,
    resolve_comfyui_install,
)

__all__ = [
    "SOURCE_EXISTING",
    "SOURCE_SHARED",
    "ComfyUIProvisionError",
    "classify_comfyui_source",
    "comfyui_update_available",
    "extract_archive",
    "install_root_of",
    "installed_comfyui_version",
    "is_provisioned",
    "load_comfyui_spec",
    "managed_comfyui_paths",
    "portable_root",
    "resolve_comfyui_install",
]
