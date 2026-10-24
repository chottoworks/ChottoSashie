"""管理モードのComfyUI自動起動の判断と実起動を確かめる。"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import time
from pathlib import Path
from pathlib import Path as pathlib_Path

import pytest

from chotto_sashie_shared.comfyui_provision import managed_comfyui_paths
from chotto_sashie_shared.shared_env import shared_comfyui_root
from chotto_sashie_shared.managed_runtime import (
    ManagedRuntimeOptions,
    ManagedRuntimeUnavailableError,
    autostart_managed_comfyui,
    start_managed_comfyui,
)
from chotto_sashie_shared.runtime import RuntimeManager, RuntimeManagerError, comfy_is_ready
from chotto_sashie_shared.setup_config import (
    SetupConfigRepository,
    SetupConfigUpdate,
    SetupMode,
)
from chotto_sashie_api.database import Database

_FAKE_COMFY_MAIN = """
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer

parser = argparse.ArgumentParser(add_help=False)
parser.add_argument('--port', type=int, required=True)
args, _ = parser.parse_known_args()

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/system_stats':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{}')
        else:
            self.send_response(404)
            self.end_headers()
    def log_message(self, *_args):
        pass

HTTPServer(('127.0.0.1', args.port), Handler).serve_forever()
"""


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _options(tmp_path: Path) -> ManagedRuntimeOptions:
    return ManagedRuntimeOptions(
        managed_root=tmp_path / "managed",
        namespace="test_app",
        app_label="Test App",
    )


def _provisioned_comfy(tmp_path: Path) -> None:
    """アプリ用意版ComfyUIとして、偽サーバのmain.pyを既定レイアウトへ置く。"""
    comfy_dir, python = managed_comfyui_paths(shared_comfyui_root())
    comfy_dir.mkdir(parents=True, exist_ok=True)
    (comfy_dir / "main.py").write_text(_FAKE_COMFY_MAIN, encoding="utf-8")
    python.parent.mkdir(parents=True, exist_ok=True)
    # 同梱Pythonの代わりに実Pythonへのコピーではなくファイル存在だけ用意し、
    # 実起動には設定側の既存インストール指定（実Python）を使う。
    python.write_text("", encoding="utf-8")


def _repository(tmp_path: Path) -> SetupConfigRepository:
    database = Database(tmp_path / "cfg.sqlite3")
    database.initialize()
    return SetupConfigRepository(database, default_port=8199)


def test_autostart_skips_existing_comfyui_mode(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    config = repo.set(
        SetupConfigUpdate(mode=SetupMode.EXISTING_COMFYUI, base_url="http://127.0.0.1:8190")
    )
    message = asyncio.run(autostart_managed_comfyui(config, _options(tmp_path), RuntimeManager()))
    assert "対象ではありません" in message


def test_autostart_skips_unconfigured(tmp_path: Path) -> None:
    config = _repository(tmp_path).get()
    assert config.configured is False
    message = asyncio.run(autostart_managed_comfyui(config, _options(tmp_path), RuntimeManager()))
    assert "対象ではありません" in message


def test_autostart_skips_when_a_comfyui_is_already_answering(tmp_path: Path) -> None:
    """兄弟アプリが同じ設定で先に起動している場合は二重起動しない。"""
    repo = _repository(tmp_path)
    port = _free_port()
    process = subprocess.Popen(
        [sys.executable, "-c", _FAKE_COMFY_MAIN, "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            if comfy_is_ready(f"http://127.0.0.1:{port}"):
                break
            time.sleep(0.1)
        config = repo.set(SetupConfigUpdate(mode=SetupMode.MANAGED_DEDICATED, port=port))
        message = asyncio.run(
            autostart_managed_comfyui(config, _options(tmp_path), RuntimeManager())
        )
    finally:
        process.terminate()
        process.wait(timeout=10)
    assert "既に生成エンジンが動作している" in message


def test_autostart_waits_for_a_dying_process_to_release_the_port(tmp_path: Path) -> None:
    """応答しないままポートだけ握られているときは、手放すのを待ってから起動する。

    アプリを入れ直した直後、前のComfyUIがまだ終了処理中だと起きる。ここで
    見送ると、前のプロセスが消えたあと何も動いていない状態が残ってしまう。
    """
    repo = _repository(tmp_path)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = int(listener.getsockname()[1])
        config = repo.set(SetupConfigUpdate(mode=SetupMode.MANAGED_DEDICATED, port=port))
        message = asyncio.run(
            autostart_managed_comfyui(
                config, _options(tmp_path), RuntimeManager(), port_wait_seconds=0.5
            )
        )
    # 待っても空かなければ、その旨を伝えて見送る（「動作している」とは言わない）。
    assert "使用中のため" in message

    # 空いたあとは、通常どおり起動を試みる（ここではComfyUIが無いので見送りになる）。
    config = repo.set(SetupConfigUpdate(mode=SetupMode.MANAGED_DEDICATED, port=_free_port()))
    message = asyncio.run(autostart_managed_comfyui(config, _options(tmp_path), RuntimeManager()))
    assert "見送りました" in message


def test_autostart_reports_when_no_comfyui_available(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    config = repo.set(SetupConfigUpdate(mode=SetupMode.MANAGED_DEDICATED, port=_free_port()))
    message = asyncio.run(autostart_managed_comfyui(config, _options(tmp_path), RuntimeManager()))
    assert "見送りました" in message


@pytest.mark.slow
def test_autostart_launches_configured_install(tmp_path: Path) -> None:
    """既存インストールを設定してあれば、自動起動で実際に立ち上がる。"""
    source = tmp_path / "ExistingComfyUI"
    source.mkdir()
    (source / "main.py").write_text(_FAKE_COMFY_MAIN, encoding="utf-8")
    repo = _repository(tmp_path)
    config = repo.set(
        SetupConfigUpdate(
            mode=SetupMode.MANAGED_DEDICATED,
            port=_free_port(),
            comfyui_path=str(source),
            python_path=sys.executable,
        )
    )
    manager = RuntimeManager()
    try:
        message = asyncio.run(autostart_managed_comfyui(config, _options(tmp_path), manager))
        assert "動作しています" in message
        assert manager.status(config.port)["owned"] is True
    finally:
        manager.shutdown()


@pytest.mark.slow
def test_start_allows_app_comfyui_inside_managed_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """共有フォルダを管理領域の内側に置いた配置でも、アプリ用意版なら起動できる。"""
    from chotto_sashie_shared import managed_runtime

    managed_root = tmp_path / "ChottoSashie"
    monkeypatch.setenv("CHOTTO_SASHIE_SHARED_DIR", str(managed_root))
    comfyui_root = shared_comfyui_root()
    comfy_dir, _python = managed_comfyui_paths(comfyui_root)
    comfy_dir.mkdir(parents=True)
    (comfy_dir / "main.py").write_text(_FAKE_COMFY_MAIN, encoding="utf-8")
    # 同梱Pythonの代わりに実Pythonで起動する（配置の判定だけを確かめる）。
    monkeypatch.setattr(
        managed_runtime,
        "resolve_comfyui_install",
        lambda *_a, **_k: (comfy_dir, pathlib_Path(sys.executable)),
    )

    options = ManagedRuntimeOptions(
        managed_root=managed_root,
        namespace="chotto_sashie",
        app_label="ちょっと挿絵",
    )
    config = _repository(tmp_path).set(
        SetupConfigUpdate(mode=SetupMode.MANAGED_DEDICATED, port=_free_port())
    )
    manager = RuntimeManager()
    try:
        status = start_managed_comfyui(config, options, manager)
        assert status["reachable"] is True
        assert status["owned"] is True
        # 可変データは管理領域内、ComfyUI本体の中には書き込まない。
        assert (managed_root / "runtime" / "comfy" / "extra_model_paths.yaml").is_file()
        assert not (comfy_dir / "runtime-profile.json").exists()
    finally:
        manager.shutdown()


def test_start_still_requires_separation_for_borrowed_install(tmp_path: Path) -> None:
    """借用した既存ComfyUIの内側に管理領域がある配置は、従来どおり拒否する。"""
    source = tmp_path / "ExistingComfyUI"
    source.mkdir()
    (source / "main.py").write_text(_FAKE_COMFY_MAIN, encoding="utf-8")
    options = ManagedRuntimeOptions(
        managed_root=source / "managed",
        namespace="test_app",
        app_label="Test App",
    )
    config = _repository(tmp_path).set(
        SetupConfigUpdate(
            mode=SetupMode.MANAGED_DEDICATED,
            port=_free_port(),
            comfyui_path=str(source),
            python_path=sys.executable,
        )
    )
    with pytest.raises(RuntimeManagerError, match="分離"):
        start_managed_comfyui(config, options, RuntimeManager())


@pytest.mark.slow
def test_start_refreshes_derived_files_after_settings_change(tmp_path: Path) -> None:
    """参照フォルダを変えた後でも起動できる（派生ファイルを作り直す）。"""
    source = tmp_path / "ExistingComfyUI"
    source.mkdir()
    (source / "main.py").write_text(_FAKE_COMFY_MAIN, encoding="utf-8")
    first_models = tmp_path / "models-a"
    first_models.mkdir()
    second_models = tmp_path / "models-b"
    second_models.mkdir()
    repo = _repository(tmp_path)
    options = _options(tmp_path)

    def configure(models: Path) -> object:
        return repo.set(
            SetupConfigUpdate(
                mode=SetupMode.MANAGED_EXTERNAL_MODELS,
                port=_free_port(),
                external_model_roots=[str(models)],
                comfyui_path=str(source),
                python_path=sys.executable,
            )
        )

    manager = RuntimeManager()
    try:
        start_managed_comfyui(configure(first_models), options, manager)
    finally:
        manager.shutdown()

    yaml_path = tmp_path / "managed" / "runtime" / "comfy" / "extra_model_paths.yaml"
    assert str(first_models.resolve()) in yaml_path.read_text(encoding="utf-8").replace("\\\\", "\\")

    manager = RuntimeManager()
    try:
        # 参照先を変更しても、古い設定が残って失敗しない。
        start_managed_comfyui(configure(second_models), options, manager)
        content = yaml_path.read_text(encoding="utf-8").replace("\\\\", "\\")
        assert str(second_models.resolve()) in content
        assert str(first_models.resolve()) not in content
    finally:
        manager.shutdown()


def test_start_rejects_non_managed_mode(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    config = repo.set(
        SetupConfigUpdate(mode=SetupMode.EXISTING_COMFYUI, base_url="http://127.0.0.1:8190")
    )
    with pytest.raises(ManagedRuntimeUnavailableError):
        start_managed_comfyui(config, _options(tmp_path), RuntimeManager())


def test_dedicated_portable_is_used_when_reuse_not_configured(tmp_path: Path) -> None:
    """流用を指定していなければ、アプリ専用のComfyUIが使われる。"""
    from chotto_sashie_shared.comfyui_provision import resolve_comfyui_install

    comfyui_root = shared_comfyui_root()
    # 既存インストール相当のフォルダは存在するが、設定では指定していない。
    existing = tmp_path / "ExistingComfyUI"
    existing.mkdir()
    (existing / "main.py").write_text("", encoding="utf-8")

    assert resolve_comfyui_install() is None

    _provisioned_comfy(tmp_path)
    resolved = resolve_comfyui_install()
    assert resolved is not None
    assert resolved[0] == managed_comfyui_paths(comfyui_root)[0]


def test_configured_reuse_wins_when_explicitly_set(tmp_path: Path) -> None:
    """流用を明示したときだけ既存インストールを使う。"""
    from chotto_sashie_shared.comfyui_provision import resolve_comfyui_install

    comfyui_root = shared_comfyui_root()
    _provisioned_comfy(tmp_path)
    existing = tmp_path / "ExistingComfyUI"
    existing.mkdir()
    (existing / "main.py").write_text("", encoding="utf-8")

    resolved = resolve_comfyui_install(str(existing), sys.executable)
    assert resolved == (existing, pathlib_Path(sys.executable))


def test_broken_reuse_falls_back_to_dedicated(tmp_path: Path) -> None:
    """流用先が壊れていても、専用環境があれば起動できる。"""
    from chotto_sashie_shared.comfyui_provision import resolve_comfyui_install

    comfyui_root = shared_comfyui_root()
    _provisioned_comfy(tmp_path)
    resolved = resolve_comfyui_install(str(tmp_path / "gone"), sys.executable)
    assert resolved is not None
    assert resolved[0] == managed_comfyui_paths(comfyui_root)[0]
