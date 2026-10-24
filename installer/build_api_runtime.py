"""配布版に同梱する、API用のPythonランタイムを組み立てる。

デスクトップ版のアプリはPythonのAPIを子プロセスとして起動する。開発時はリポジトリの
`.venv` を使うが、配布先にはリポジトリも仮想環境も無い。そこで、公式のWindows埋め込み
可能パッケージ（embeddable）へ依存とこのアプリを入れた一式をここで作り、Tauriの
resources として同梱する。

作るもの（既定では `installer/dist/` の下）:

    api-runtime/    Python本体と依存とアプリ本体（site-packages）
    app/            同梱物（workflows・model-manifests）

同梱物を `api-runtime` の奥へ置かずに外へ出すのは、Windowsのパス長（260文字）のため。
site-packagesの下だと `chotto_sashie_shared/_resources/` のぶんだけ深くなり、利用者名の
長い環境で展開に失敗する。外へ出したうえで、起動時に `CHOTTO_SASHIE_RESOURCES_DIR`
でその場所を教える。

使い方::

    .\\.venv\\Scripts\\python.exe installer\\build_api_runtime.py
    .\\.venv\\Scripts\\python.exe installer\\build_api_runtime.py --pin   # 版の実測値を記録する

`--pin` は、埋め込み版Pythonの実物を取得してSHA256とサイズを `api-runtime.json` へ
書き込む。版を上げるときだけ使う。通常は記録済みの値と一致しないものは使わない。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

_INSTALLER_ROOT = Path(__file__).resolve().parent
_REPOSITORY_ROOT = _INSTALLER_ROOT.parent
_MANIFEST_PATH = _INSTALLER_ROOT / "api-runtime.json"
_DEFAULT_OUTPUT = _INSTALLER_ROOT / "dist"
_DOWNLOAD_CACHE = _INSTALLER_ROOT / "download"
# 依存の版をすべて固定した一覧。pyproject が固定するのは直接の依存だけで、
# その先（anyio・idna など）は浮く。同じコミットから違うものが出来ないよう、
# 解決結果そのものをここへ焼いておく。
_LOCK_PATH = _INSTALLER_ROOT / "api-runtime-lock.txt"

# 埋め込み版Pythonは既定で `._pth` に書かれた場所しか見ない。site-packages を足し、
# `import site` を有効にしないと、入れた依存を読み込めない。
_PTH_ADDITIONS = ("Lib\\site-packages", "import site")


class BuildError(RuntimeError):
    pass


def _load_manifest() -> dict:
    try:
        return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BuildError(f"設定を読めません: {_MANIFEST_PATH}") from error


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _download(url: str, destination: Path) -> Path:
    """まだ手元に無ければ取得する。途中で失敗した中身を残さないよう、別名で受けてから移す。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        return destination
    partial = destination.with_name(f"{destination.name}.part")
    print(f"取得しています: {url}")
    with urllib.request.urlopen(url) as response, partial.open("wb") as stream:  # noqa: S310
        shutil.copyfileobj(response, stream)
    partial.replace(destination)
    return destination


def _embedded_python_archive(manifest: dict, *, pin: bool) -> Path:
    spec = manifest["python"]
    archive = _download(spec["download_url"], _DOWNLOAD_CACHE / Path(spec["download_url"]).name)
    digest = _sha256(archive)
    size = archive.stat().st_size

    if pin:
        spec["sha256"] = digest
        spec["size_bytes"] = size
        _MANIFEST_PATH.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"記録しました: sha256={digest} size={size:,}")
        return archive

    if not spec["sha256"]:
        raise BuildError(
            "埋め込み版Pythonの実測値がまだ記録されていません。"
            "`--pin` を付けて一度実行してください"
        )
    if digest != spec["sha256"] or size != spec["size_bytes"]:
        raise BuildError(
            "埋め込み版Pythonが記録と一致しません。"
            f"期待 sha256={spec['sha256']} size={spec['size_bytes']:,} / "
            f"実物 sha256={digest} size={size:,}"
        )
    return archive


def _unpack_python(archive: Path, runtime_dir: Path) -> None:
    if runtime_dir.exists():
        shutil.rmtree(runtime_dir)
    runtime_dir.mkdir(parents=True)
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(runtime_dir)

    pth_files = sorted(runtime_dir.glob("python*._pth"))
    if not pth_files:
        raise BuildError(f"埋め込み版Pythonの `._pth` が見つかりません: {runtime_dir}")
    pth = pth_files[0]
    lines = pth.read_text(encoding="utf-8").splitlines()
    for addition in _PTH_ADDITIONS:
        if addition not in lines:
            lines.append(addition)
    # 既定で無効にされている `import site` の行は、有効にしないと site-packages を見ない。
    lines = [line[1:] if line.strip() == "#import site" else line for line in lines]
    pth.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_wheel(output: Path) -> Path:
    """このリポジトリのwheelを作る。同梱物も一緒に入る（pyproject の force-include）。"""
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    subprocess.run(  # noqa: S603 - 実行するのはこのリポジトリのPython
        [sys.executable, "-m", "pip", "wheel", str(_REPOSITORY_ROOT), "--no-deps", "-w", str(output)],
        check=True,
    )
    wheels = sorted(output.glob("chotto_sashie-*.whl"))
    if not wheels:
        raise BuildError("アプリのwheelを作れませんでした")
    return wheels[0]


def _write_lock(runtime_dir: Path) -> int:
    """入った版をすべて書き出す（次回からはこの版で固定する）。"""
    target = runtime_dir / "Lib" / "site-packages"
    rows: list[str] = []
    for info in sorted(target.glob("*.dist-info")):
        metadata = info / "METADATA"
        if not metadata.is_file():
            continue
        name = version = ""
        for line in metadata.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line:
                break
            if line.startswith("Name: "):
                name = line[6:].strip()
            elif line.startswith("Version: "):
                version = line[9:].strip()
        # アプリ自身は版を固定する対象ではない。
        if name and version and name != "chotto-sashie":
            rows.append(f"{name}=={version}")
    # 中身はASCIIだけにする。pipは制約ファイルをロケールの文字コードで読むため、
    # 日本語のコメントを入れると cp932 の環境で UnicodeDecodeError になって組み立てが
    # 止まる（実際に止まった）。説明はこの関数と README に日本語で置く。
    header = (
        "# Generated by installer/build_api_runtime.py --relock. Do not edit by hand."
        + chr(10)
        + "# Direct dependencies come from pyproject.toml; this file pins the rest."
        + chr(10)
    )
    body = chr(10).join(sorted(rows, key=str.lower)) + chr(10)
    _LOCK_PATH.write_text(header + body, encoding="ascii")
    return len(rows)


def _install_packages(manifest: dict, wheel: Path, runtime_dir: Path, *, relock: bool) -> None:
    """依存とアプリ本体を、埋め込み版Pythonの site-packages へ入れる。

    組み立てるPythonと配る先のPythonは版が違ってよいので、`--target` と版・環境の指定で
    出来合いのwheelだけを取る。ソースからの組み立てを許すと、利用者の環境にコンパイラが
    要るものが混ざる。

    バイトコンパイルは切る。pipは既定で「いま動いているPython」で `.pyc` を作るが、
    それは組み立てる側の版であって配る先の版ではない。使われないものが増えるだけで、
    Windowsのパス長も食う。配布先では起動時に必要なぶんだけ作られる。
    """
    target = runtime_dir / "Lib" / "site-packages"
    target.mkdir(parents=True, exist_ok=True)
    wheels = manifest["wheels"]
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--target",
        str(target),
        "--platform",
        wheels["platform"],
        "--python-version",
        wheels["python_version"],
        "--only-binary=:all:",
        "--no-compile",
        "--upgrade",
        str(wheel),
    ]
    if not relock:
        if not _LOCK_PATH.is_file():
            raise BuildError(
                f"依存の固定一覧がありません: {_LOCK_PATH}。"
                "`--relock` を付けて一度実行してください"
            )
        command += ["--constraint", str(_LOCK_PATH)]
    subprocess.run(command, check=True)  # noqa: S603 - 実行するのはこのリポジトリのPython


def _move_resources(manifest: dict, runtime_dir: Path, resources_dir: Path) -> None:
    """同梱物を site-packages の奥から出す（Windowsのパス長のため）。"""
    source = runtime_dir / "Lib" / "site-packages" / "chotto_sashie_shared" / "_resources"
    if not source.is_dir():
        raise BuildError(f"同梱物がwheelに入っていません: {source}")
    if resources_dir.exists():
        shutil.rmtree(resources_dir)
    resources_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(resources_dir))


def _copy_license_documents(resources_dir: Path) -> list[str]:
    """本体のライセンスと第三者の一覧を、同梱物の隣へ置く。

    配る先で「何が入っているのか」を読めないと、LGPLやMPLの求める告知を
    果たせない。一覧は build_notices.py が作るので、無ければそう伝える。
    """
    copied: list[str] = []
    for name in ("LICENSE", "THIRD-PARTY-NOTICES.md"):
        source = _REPOSITORY_ROOT / name
        if not source.is_file():
            raise BuildError(
                f"{name} がありません。"
                "THIRD-PARTY-NOTICES.md は installer/build_notices.py で作ります"
            )
        shutil.copy2(source, resources_dir / name)
        copied.append(name)
    return copied


def _strip_bytecode(root: Path) -> int:
    """組み立てた一式から `.pyc` の残りを落とす。

    pipへ `--no-compile` を渡していても、依存のwheelが自分で持ち込むことがある。
    出来上がるものを、手元の状態に左右されないようにするための最後の掃除。
    """
    removed = 0
    for cache in sorted(root.rglob("__pycache__"), key=lambda path: -len(path.parts)):
        if cache.is_dir():
            shutil.rmtree(cache)
            removed += 1
    for compiled in root.rglob("*.pyc"):
        compiled.unlink()
        removed += 1
    return removed


def _longest_path(root: Path) -> tuple[int, Path]:
    files = [path for path in root.rglob("*") if path.is_file()]
    longest = max(files, key=lambda path: len(str(path)))
    return len(str(longest)), longest


def build(output_root: Path, *, pin: bool, relock: bool) -> None:
    manifest = _load_manifest()
    layout = manifest["layout"]
    runtime_dir = output_root / layout["runtime_dir"]
    resources_dir = output_root / layout["resources_dir"]

    archive = _embedded_python_archive(manifest, pin=pin)
    if pin:
        # 記録し直しただけで終わると、書き込んだ値で組み立て直したか分からなくなる。
        manifest = _load_manifest()

    _unpack_python(archive, runtime_dir)
    wheel = _build_wheel(_INSTALLER_ROOT / "build" / "wheel")
    _install_packages(manifest, wheel, runtime_dir, relock=relock)
    if relock:
        print(f"依存の版を{_write_lock(runtime_dir)}件固定しました: {_LOCK_PATH}")
    _move_resources(manifest, runtime_dir, resources_dir)
    _copy_license_documents(resources_dir)
    _strip_bytecode(output_root)

    python = runtime_dir / "python.exe"
    if not python.is_file():
        raise BuildError(f"Pythonが見つかりません: {python}")

    length, longest = _longest_path(output_root)
    print()
    print(f"組み立てました: {output_root}")
    print(f"  Python      : {python}")
    print(f"  同梱物      : {resources_dir}")
    print(f"  最長のパス  : {length}文字 {longest}")
    print(
        "  ※ ここに導入先のパスが足されて260文字を超えないこと"
        f"（同梱物の相対長 {length - len(str(output_root)) - 1}文字）"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", type=Path, default=_DEFAULT_OUTPUT, help="組み立て先（既定: installer/dist）"
    )
    parser.add_argument(
        "--pin",
        action="store_true",
        help="埋め込み版Pythonの実測値を api-runtime.json へ記録する（版を上げるときだけ）",
    )
    parser.add_argument(
        "--relock",
        action="store_true",
        help="依存の版を解決し直して api-runtime-lock.txt へ焼き直す（依存を変えたときだけ）",
    )
    arguments = parser.parse_args()
    try:
        build(arguments.out.resolve(), pin=arguments.pin, relock=arguments.relock)
    except (BuildError, subprocess.CalledProcessError) as error:
        print(f"組み立てに失敗しました: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
