"""モデルフォルダの走査（共通の土台）。

外部フォルダは読み取りだけで、変更は一切行わない。「既存モデルを参照する」設定の
前に、そこへ何が入っているかを利用者へ示すために使う。
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

MODEL_CATEGORIES = {
    "diffusion_models",
    "checkpoints",
    "text_encoders",
    "clip",
    "vae",
    "loras",
    "controlnet",
    "upscale_models",
    "insightface",
}
MODEL_SUFFIXES = {".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".onnx"}
INCOMPLETE_SUFFIXES = {".part", ".partial", ".tmp", ".download"}


class ModelFileInfo(BaseModel):
    path: str
    category: str
    name: str
    size_bytes: int
    modified_at: datetime
    sha256: str | None = None
    incomplete: bool = False


class ModelScanResult(BaseModel):
    roots: list[str]
    files: list[ModelFileInfo]
    duplicate_groups: list[list[str]]
    inaccessible_paths: list[str]
    truncated: bool


def categorize(path: Path) -> str:
    lowered_parts = {part.lower() for part in path.parts}
    for category in MODEL_CATEGORIES:
        if category in lowered_parts:
            return category
    return "unknown"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def scan_model_roots(
    roots: list[Path], *, calculate_sha256: bool, max_files: int
) -> ModelScanResult:
    files: list[ModelFileInfo] = []
    inaccessible: list[str] = []
    canonical_roots: list[str] = []
    truncated = False

    for requested_root in roots:
        try:
            root = requested_root.expanduser().resolve(strict=True)
        except OSError:
            inaccessible.append(str(requested_root))
            continue
        if not root.is_dir():
            inaccessible.append(str(requested_root))
            continue
        canonical_roots.append(str(root))

        try:
            for path in root.rglob("*"):
                suffix = path.suffix.lower()
                incomplete = suffix in INCOMPLETE_SUFFIXES
                if not path.is_file() or (suffix not in MODEL_SUFFIXES and not incomplete):
                    continue
                try:
                    stat = path.stat()
                    files.append(
                        ModelFileInfo(
                            path=str(path),
                            category=categorize(path),
                            name=path.name,
                            size_bytes=stat.st_size,
                            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                            sha256=file_sha256(path) if calculate_sha256 else None,
                            incomplete=incomplete,
                        )
                    )
                except OSError:
                    inaccessible.append(str(path))
                if len(files) >= max_files:
                    truncated = True
                    break
        except OSError:
            inaccessible.append(str(root))
        if truncated:
            break

    by_signature: dict[tuple[str, int], list[str]] = defaultdict(list)
    for item in files:
        by_signature[(item.name.lower(), item.size_bytes)].append(item.path)
    duplicates = [paths for paths in by_signature.values() if len(paths) > 1]

    return ModelScanResult(
        roots=canonical_roots,
        files=files,
        duplicate_groups=duplicates,
        inaccessible_paths=sorted(set(inaccessible)),
        truncated=truncated,
    )


def inspect_comfyui_folder(path: Path) -> dict[str, object]:
    """既存ComfyUIを変更せず、代表ファイルとモデルディレクトリだけを確認する。"""
    try:
        root = path.expanduser().resolve(strict=True)
    except OSError:
        return {"valid": False, "path": str(path), "message": "フォルダにアクセスできません"}
    expected = [root / "main.py", root / "models"]
    missing = [str(candidate.name) for candidate in expected if not candidate.exists()]
    return {
        "valid": not missing,
        "path": str(root),
        "missing": missing,
        "portable_python": str(root.parent / "python_embeded")
        if (root.parent / "python_embeded").is_dir()
        else None,
        "message": "ComfyUI候補を確認しました" if not missing else "必要ファイルが不足しています",
    }
