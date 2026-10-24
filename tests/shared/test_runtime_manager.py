from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

from chotto_sashie_shared.runtime import (
    RuntimeManager,
    RuntimeManagerError,
    build_runtime_plan,
    write_runtime_profile,
)


def make_comfy_source(tmp_path: Path, *, server: bool = False) -> Path:
    source = tmp_path / "external-comfy"
    source.mkdir()
    script = ""
    if server:
        script = """
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
    (source / "main.py").write_text(script, encoding="utf-8")
    return source


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_runtime_preview_redirects_all_mutable_paths_without_writes(tmp_path: Path) -> None:
    source = make_comfy_source(tmp_path)
    managed = tmp_path / "managed"

    plan = build_runtime_plan(
        managed_root=managed,
        comfyui_path=source,
        python_path=Path(sys.executable),
        port=8191,
    )

    assert plan["external_environment_read_only"] is True
    assert plan["base_url"] == "http://127.0.0.1:8191"
    assert "--disable-all-custom-nodes" in plan["command"]
    assert "--disable-api-nodes" in plan["command"]
    for directory in plan["directories"].values():
        assert Path(str(directory)).is_relative_to(managed.resolve())
    assert plan["environment"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert plan["model_paths_ready"] is False
    assert not managed.exists()


def test_runtime_plan_reads_large_models_from_disk(tmp_path: Path) -> None:
    """大きなモデルはホストRAMへ抱え込ませない。

    既定では読み込んだ重みをピン留めRAMへ載せたまま次のプロンプトへ進むため、
    40.9GBのQwen-Image-Edit-2511 bf16で64GB機のRAMを使い切り、生成が MemoryError
    で落ちていた。実機ではこの指定だけで同じ1枚が完走する。
    """
    plan = build_runtime_plan(
        managed_root=tmp_path / "managed",
        comfyui_path=make_comfy_source(tmp_path),
        python_path=Path(sys.executable),
        port=8191,
    )

    assert "--fast-disk" in plan["command"]


def test_runtime_profile_requires_separate_source_and_is_idempotent(tmp_path: Path) -> None:
    source = make_comfy_source(tmp_path)
    managed = tmp_path / "managed"
    plan = build_runtime_plan(
        managed_root=managed,
        comfyui_path=source,
        python_path=Path(sys.executable),
        port=8191,
    )

    written, current = write_runtime_profile(plan)
    written_again, current_again = write_runtime_profile(plan)

    assert (written, current) == (True, False)
    assert (written_again, current_again) == (False, True)
    assert Path(str(plan["profile_path"])).is_file()
    assert not (source / "runtime-profile.json").exists()
    with pytest.raises(RuntimeManagerError, match="分離"):
        build_runtime_plan(
            managed_root=source / "managed",
            comfyui_path=source,
            python_path=Path(sys.executable),
            port=8191,
        )


@pytest.mark.slow
def test_runtime_manager_starts_and_stops_owned_process(tmp_path: Path) -> None:
    source = make_comfy_source(tmp_path, server=True)
    managed = tmp_path / "managed"
    port = free_port()
    plan = build_runtime_plan(
        managed_root=managed,
        comfyui_path=source,
        python_path=Path(sys.executable),
        port=port,
    )
    config = Path(str(plan["model_paths_config"]))
    config.parent.mkdir(parents=True)
    config.write_text("test: {}\n", encoding="utf-8")
    write_runtime_profile(plan)
    manager = RuntimeManager()

    started = manager.start(plan, timeout_seconds=10)
    stopped = manager.stop()

    assert started["state"] == "running"
    assert started["owned"] is True
    assert started["reachable"] is True
    assert stopped["state"] == "stopped"
    assert stopped["owned"] is False


def test_shutdown_without_owned_process_does_not_check_http(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def _fail_ready(_base_url: str) -> bool:
        nonlocal called
        called = True
        return True

    monkeypatch.setattr("chotto_sashie_shared.runtime._comfy_is_ready", _fail_ready)
    manager = RuntimeManager()
    manager.shutdown()
    assert called is False


def test_runtime_manager_does_not_take_over_used_port(tmp_path: Path) -> None:
    source = make_comfy_source(tmp_path)
    managed = tmp_path / "managed"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = int(listener.getsockname()[1])
        plan = build_runtime_plan(
            managed_root=managed,
            comfyui_path=source,
            python_path=Path(sys.executable),
            port=port,
        )
        config = Path(str(plan["model_paths_config"]))
        config.parent.mkdir(parents=True)
        config.write_text("test: {}\n", encoding="utf-8")
        with pytest.raises(RuntimeManagerError, match="別のプロセス"):
            RuntimeManager().start(plan, timeout_seconds=1)
