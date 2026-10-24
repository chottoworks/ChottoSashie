"""アプリの版が、版を書いている全ての場所で揃っているかを確かめる。

版は6か所に書いてある。インストーラの版（Tauri）、APIが名乗る版（pyproject）、
画面とデスクトップのパッケージ、Rustのクレート。1つでも食い違うと、配ったものと
画面に出る版がずれるので、リリースのビルドはここを通ってから始める。

使い方::

    python installer/check_version.py           # 全ての場所が同じ版か
    python installer/check_version.py 0.1.0     # 全ての場所がその版か
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


def _json_version(relative: str) -> str:
    return json.loads((_REPOSITORY_ROOT / relative).read_text(encoding="utf-8"))["version"]


def _pyproject_version() -> str:
    data = tomllib.loads((_REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def _cargo_version() -> str:
    data = tomllib.loads(
        (_REPOSITORY_ROOT / "apps/desktop/src-tauri/Cargo.toml").read_text(encoding="utf-8")
    )
    return data["package"]["version"]


def collect_versions() -> dict[str, str]:
    return {
        "package.json": _json_version("package.json"),
        "pyproject.toml": _pyproject_version(),
        "apps/web/package.json": _json_version("apps/web/package.json"),
        "apps/desktop/package.json": _json_version("apps/desktop/package.json"),
        "apps/desktop/src-tauri/Cargo.toml": _cargo_version(),
        "apps/desktop/src-tauri/tauri.conf.json": _json_version(
            "apps/desktop/src-tauri/tauri.conf.json"
        ),
    }


def mismatches(versions: dict[str, str], expected: str | None) -> list[str]:
    """揃っていない場所を返す。expected が無いときは、いちばん多い版を基準にする。"""
    if expected is None:
        counts: dict[str, int] = {}
        for version in versions.values():
            counts[version] = counts.get(version, 0) + 1
        expected = max(counts, key=lambda version: counts[version])
    return [f"{path}: {version}（期待: {expected}）" for path, version in versions.items() if version != expected]


def main(argv: list[str]) -> int:
    expected = argv[1] if len(argv) > 1 else None
    if expected is not None and not re.fullmatch(r"\d+\.\d+\.\d+", expected):
        print(f"版の形式が違います: {expected}", file=sys.stderr)
        return 2
    versions = collect_versions()
    wrong = mismatches(versions, expected)
    if wrong:
        print("版が揃っていません:", file=sys.stderr)
        for line in wrong:
            print(f"  {line}", file=sys.stderr)
        return 1
    print(f"版は揃っています: {next(iter(versions.values()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
