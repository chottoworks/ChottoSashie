from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from .download import safe_destination
from .model_layout import MODEL_DIRECTORY_ALIASES

__all__ = [
    "MODEL_DIRECTORY_ALIASES",
    "ManagedConfigError",
    "build_extra_model_paths_plan",
    "write_extra_model_paths",
]


class ManagedConfigError(RuntimeError):
    """管理領域外への書き込みや既存設定の衝突を表す。"""


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolved_external_roots(external_roots: list[Path], *, allow_empty: bool) -> list[Path]:
    resolved: list[Path] = []
    for requested in external_roots:
        try:
            root = requested.expanduser().resolve(strict=True)
        except OSError as error:
            raise ManagedConfigError(f"外部モデルフォルダにアクセスできません: {requested}") from error
        if not root.is_dir():
            raise ManagedConfigError(f"外部モデルフォルダではありません: {root}")
        if root not in resolved:
            resolved.append(root)
    if not resolved and not allow_empty:
        raise ManagedConfigError("外部モデルフォルダを1件以上指定してください")
    return resolved


def build_extra_model_paths_plan(
    managed_root: Path,
    external_roots: list[Path],
    *,
    namespace: str = "chotto_sashie",
    app_label: str = "ちょっと挿絵",
    allow_empty_external: bool = False,
    shared_model_dirs: list[Path] | None = None,
) -> dict[str, object]:
    """管理領域へ置く設定内容を作るだけで、ファイルは作成しない。

    namespace/app_label は生成物の名前空間（extra_model_paths.yaml の見出し）。
    allow_empty_external=True のとき、外部フォルダ無しの「専用環境のみ」設定も作れる。
    shared_model_dirs は共有フォルダのモデル置き場（models 相当）。取得先を
    どこにしていても生成エンジンから見えるように、まとめて参照へ加える。
    """
    resolved_managed_root = managed_root.expanduser().resolve()
    resolved_external_roots = _resolved_external_roots(
        external_roots, allow_empty=allow_empty_external
    )
    for external_root in resolved_external_roots:
        if _is_inside(resolved_managed_root, external_root):
            raise ManagedConfigError("管理領域を外部モデルフォルダの内側には作成できません")

    destination = safe_destination(
        resolved_managed_root,
        "runtime/comfy/extra_model_paths.yaml",
    )
    lines = [
        f"# {app_label}が管理領域内に生成したモデル参照設定",
        "# 外部モデルフォルダ側のファイルや設定は変更しません。",
        "",
        f"{namespace}_managed:",
        f"  base_path: {json.dumps(str(resolved_managed_root / 'models'), ensure_ascii=False)}",
        "  is_default: true",
    ]
    lines.extend(f"  {category}: {category}" for category in MODEL_DIRECTORY_ALIASES)
    lines.append("")
    mapped_categories: dict[str, list[str]] = {}
    ignored_directories: dict[str, list[str]] = {}

    # 共有フォルダのモデル置き場（アプリが取得したもの）。標準の並びなので
    # 別名の探索はせず、そのまま種別ごとに割り当てる。
    seen_shared: set[str] = set()
    for index, shared_dir in enumerate(shared_model_dirs or [], start=1):
        try:
            resolved_shared = shared_dir.expanduser().resolve()
        except OSError:
            continue
        key = str(resolved_shared).casefold()
        if key in seen_shared or not resolved_shared.is_dir():
            continue
        if resolved_shared == (resolved_managed_root / "models").resolve():
            continue
        seen_shared.add(key)
        lines.extend(
            [
                f"{namespace}_shared_{index}:",
                f"  base_path: {json.dumps(str(resolved_shared), ensure_ascii=False)}",
                "  is_default: false",
            ]
        )
        lines.extend(f"  {category}: {category}" for category in MODEL_DIRECTORY_ALIASES)
        lines.append("")
        for category in MODEL_DIRECTORY_ALIASES:
            if (resolved_shared / category).is_dir():
                mapped_categories.setdefault(category, []).append(str(resolved_shared / category))

    for index, external_root in enumerate(resolved_external_roots, start=1):
        directories = [child for child in external_root.iterdir() if child.is_dir()]
        by_lower_name = {directory.name.casefold(): directory.name for directory in directories}
        used_names: set[str] = set()
        mappings: dict[str, list[str]] = {}
        for category, aliases in MODEL_DIRECTORY_ALIASES.items():
            matches = [by_lower_name[alias] for alias in aliases if alias in by_lower_name]
            if matches:
                mappings[category] = matches
                used_names.update(matches)
                mapped_categories.setdefault(category, []).extend(
                    str(external_root / match) for match in matches
                )

        ignored_directories[str(external_root)] = sorted(
            directory.name for directory in directories if directory.name not in used_names
        )
        lines.extend(
            [
                f"{namespace}_external_{index}:",
                f"  base_path: {json.dumps(str(external_root), ensure_ascii=False)}",
                "  is_default: false",
            ]
        )
        for category, paths in mappings.items():
            if len(paths) == 1:
                lines.append(f"  {category}: {json.dumps(paths[0], ensure_ascii=False)}")
            else:
                lines.append(f"  {category}: |")
                lines.extend(f"    {path}" for path in paths)
        lines.append("")

    content = "\n".join(lines).rstrip() + "\n"
    return {
        "managed_root": str(resolved_managed_root),
        "destination": str(destination),
        "external_roots": [str(root) for root in resolved_external_roots],
        "content": content,
        "mapped_categories": mapped_categories,
        "ignored_directories": ignored_directories,
    }


def write_extra_model_paths(
    plan: dict[str, object],
    *,
    overwrite_existing: bool = False,
) -> tuple[bool, bool]:
    """確認済み計画を管理領域へ原子的に保存する。"""
    managed_root = Path(str(plan["managed_root"])).resolve()
    expected_destination = Path(str(plan["destination"]))
    destination = safe_destination(managed_root, str(expected_destination.relative_to(managed_root)))
    content = str(plan["content"])

    if destination.is_file():
        current = destination.read_text(encoding="utf-8")
        if current == content:
            return False, True
        if not overwrite_existing:
            raise ManagedConfigError("既存の管理設定と内容が異なります。自動では上書きしません")

    destination.parent.mkdir(parents=True, exist_ok=True)
    # ディレクトリ作成後にシンボリックリンク等を再解決し、境界を再検証する。
    destination = safe_destination(managed_root, str(destination.relative_to(managed_root)))
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return True, False
