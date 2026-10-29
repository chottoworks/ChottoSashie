"""配布物に入っている第三者のファイルの一覧（THIRD-PARTY-NOTICES.md）を作る。

**手で書かない。** 依存を1つ足しただけで一覧が古くなり、古い一覧は無いより悪い
（入っていないものを載せ、入っているものを載せない）。組み立てた実物と、実際に
ビルドへ入るものだけを見て毎回作り直す。

見るもの:

- 同梱Python本体（`installer/api-runtime.json`）
- 同梱したPythonパッケージ（`installer/dist/api-runtime/Lib/site-packages/*.dist-info`）
- 実行ファイルへ静的に入るRustクレート（`cargo tree` のWindows向け解決結果と、
  ローカルのレジストリにある各 `Cargo.toml`）
- 画面に入るnpmパッケージ（`pnpm licenses list --prod`）

アプリが取得するもの（ComfyUI・ffmpeg・モデル）は**ここに載せない**。再配布しておらず、
取得先と利用条件は取得の手前で画面に出している。ただし一覧の最後に、それらが別の条件で
あることだけは書く。

使い方::

    .\\.venv\\Scripts\\python.exe installer\\build_notices.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

_INSTALLER_ROOT = Path(__file__).resolve().parent
_REPOSITORY_ROOT = _INSTALLER_ROOT.parent
_DEFAULT_DIST = _INSTALLER_ROOT / "dist"
_OUTPUT = _REPOSITORY_ROOT / "THIRD-PARTY-NOTICES.md"

_CARGO_TARGET = "x86_64-pc-windows-msvc"
# 自分のクレートは第三者ではない。
_OWN_CRATES = {"chotto-sashie-desktop"}


class NoticeError(RuntimeError):
    pass


def _python_packages(dist: Path) -> list[tuple[str, str, str]]:
    """同梱したPythonパッケージの (名前, 版, ライセンス)。"""
    site_packages = dist / "api-runtime" / "Lib" / "site-packages"
    if not site_packages.is_dir():
        raise NoticeError(
            f"組み立て結果が見つかりません: {site_packages}。"
            "先に installer/build_api_runtime.py を実行してください"
        )
    rows: list[tuple[str, str, str]] = []
    for info in sorted(site_packages.glob("*.dist-info")):
        metadata = info / "METADATA"
        if not metadata.is_file():
            continue
        name = version = declared = ""
        classifiers: list[str] = []
        for line in metadata.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line:
                break
            if line.startswith("Name: "):
                name = line[6:].strip()
            elif line.startswith("Version: "):
                version = line[9:].strip()
            elif line.startswith("License-Expression: "):
                declared = line[20:].strip()
            elif line.startswith("License: ") and not declared:
                declared = line[9:].strip()
            elif line.startswith("Classifier: License ::"):
                classifiers.append(line.split("::")[-1].strip())
        if name in {"chotto-sashie"}:
            continue
        license_name = declared or (classifiers[0] if classifiers else "")
        # 「License: 」に本文をまるごと書く配布物があるので、長いものは分類名を採る。
        if len(license_name) > 60:
            license_name = classifiers[0] if classifiers else license_name[:60]
        rows.append((name, version, license_name or "不明"))
    return rows


def _cargo_crates() -> list[tuple[str, str, str]]:
    """実行ファイルへ入るRustクレートの (名前, 版, ライセンス)。"""
    try:
        completed = subprocess.run(  # noqa: S603 - 実行するのは cargo のみ
            [
                "cargo", "tree", "--offline", "--target", _CARGO_TARGET,
                "--edges", "normal", "--prefix", "none", "--no-dedupe",
            ],
            cwd=_REPOSITORY_ROOT / "apps" / "desktop" / "src-tauri",
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise NoticeError(f"cargo tree を実行できません: {error}") from error

    seen: dict[tuple[str, str], str] = {}
    registry = Path(os.path.expanduser("~/.cargo/registry/src"))
    sources = sorted(registry.glob("*")) if registry.is_dir() else []
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2 or not parts[1].startswith("v"):
            continue
        name, version = parts[0], parts[1][1:]
        if name in _OWN_CRATES or (name, version) in seen:
            continue
        license_name = ""
        for source in sources:
            manifest = source / f"{name}-{version}" / "Cargo.toml"
            if not manifest.is_file():
                continue
            text = manifest.read_text(encoding="utf-8", errors="replace")
            found = re.search(r'^license\s*=\s*"([^"]+)"', text, re.M)
            license_name = found.group(1) if found else (
                "同梱のライセンスファイル参照" if re.search(r"^license-file", text, re.M) else ""
            )
            break
        seen[(name, version)] = license_name or "不明"
    return [(name, version, lic) for (name, version), lic in sorted(seen.items())]


def _npm_packages() -> list[tuple[str, str, str]]:
    """画面に入るnpmパッケージの (名前, 版, ライセンス)。"""
    try:
        completed = subprocess.run(  # noqa: S603 - 実行するのは pnpm のみ
            ["pnpm", "licenses", "list", "--prod", "--json"],
            cwd=_REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=True,
            shell=os.name == "nt",
        )
        data = json.loads(completed.stdout)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise NoticeError(f"pnpm licenses list を実行できません: {error}") from error

    rows: list[tuple[str, str, str]] = []
    for license_name, packages in data.items():
        for package in packages:
            for version in package.get("versions", [""]):
                rows.append((package.get("name", ""), version, license_name))
    return sorted(rows)


def _embedded_python() -> tuple[str, str]:
    manifest = json.loads((_INSTALLER_ROOT / "api-runtime.json").read_text(encoding="utf-8"))
    spec = manifest["python"]
    return str(spec["version"]), str(spec["license"])


def _table(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for row in rows:
        lines.append("| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |")
    return "\n".join(lines)


def build() -> Path:
    python_version, python_license = _embedded_python()
    python_rows = _python_packages(_DEFAULT_DIST)
    crate_rows = _cargo_crates()
    npm_rows = _npm_packages()

    document = f"""# 第三者のファイルについて

ちょっと挿絵本体は MIT ライセンスです（[LICENSE](LICENSE)）。配布物にはそれ以外に、
次の第三者のファイルが入っています。**この文書は
`installer/build_notices.py` が組み立て結果から作ります。手で書き足さないでください。**

アプリが利用者の環境へ取得するもの（ComfyUI・ffmpeg・各モデル）は、再配布していないため
ここには載せていません。取得先と利用条件は、取得を押す前に画面へ出しています。

## 同梱するPython本体

| 名前 | 版 | ライセンス |
|---|---|---|
| CPython（Windows embeddable package） | {python_version} | {python_license} |

## 同梱するPythonパッケージ（{len(python_rows)}件）

このうち **py7zr・pybcj・pyppmd・inflate64・multivolumefile は LGPL-2.1** です。
いずれも `api-runtime\\Lib\\site-packages` に普通のファイルとして置いてあり、
同じ版のものへ差し替えて動かせます。**certifi は MPL-2.0** です。どちらも
上流のまま変更を加えていません。ソースは各配布元（PyPI）から入手できます。

{_table(("名前", "版", "ライセンス"), python_rows)}

## 実行ファイルへ入るRustクレート（{len(crate_rows)}件）

{_table(("名前", "版", "ライセンス"), crate_rows)}

## 画面に入るnpmパッケージ（{len(npm_rows)}件）

{_table(("名前", "版", "ライセンス"), npm_rows)}

## アプリが取得するもの（再配布していない）

| 名前 | ライセンス | 備考 |
|---|---|---|
| ComfyUI（Windows ポータブル） | GPL-3.0 | 別プロセスとして起動し、HTTPで呼ぶだけ |
| ffmpeg（GyanD の Windows ビルド） | GPL-3.0 | 別プロセスとして呼ぶだけ |
| Z-Image / Qwen-Image-Edit / Wan 2.2 ほか | Apache-2.0 | 取得先は `model-manifests/video-models.json` |
| BiRefNet（背景除去） | MIT | 取得先は `model-manifests/video-models.json` |
| MiniMax H3（任意） | MiniMax H3 Community License Agreement | 地域と用途に制限あり。取得の手前で条件を出し、同意を得る |
"""
    _OUTPUT.write_text(document, encoding="utf-8")
    return _OUTPUT


def main() -> int:
    try:
        path = build()
    except NoticeError as error:
        print(f"作れませんでした: {error}", file=sys.stderr)
        return 1
    print(f"書き出しました: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
