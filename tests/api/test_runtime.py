from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chotto_sashie_shared.jobs import JobRepository
from chotto_sashie_api import main
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


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    database = Database(tmp_path / "video-api.sqlite3")
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(main, "jobs", JobRepository(database))
    # 専用ComfyUIの管理領域をテストごとに隔離する。
    monkeypatch.setattr(main, "MANAGED_ROOT", tmp_path / "managed")
    with TestClient(main.app) as test_client:
        yield test_client


def _fake_comfy(tmp_path: Path) -> Path:
    source = tmp_path / "ComfyUI"
    source.mkdir()
    (source / "main.py").write_text(_FAKE_COMFY_MAIN, encoding="utf-8")
    return source


def _provision_fake_comfy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """アプリが用意する専用ComfyUIを、偽サーバmain.py + 実Pythonで模す。

    起動は共有の managed_runtime 経由なので、そこが参照する解決関数を差し替える。
    """
    from chotto_sashie_shared import managed_runtime

    source = _fake_comfy(tmp_path)
    resolved = (source, Path(sys.executable))
    monkeypatch.setattr(main, "is_provisioned", lambda _root: True)
    monkeypatch.setattr(main, "managed_comfyui_paths", lambda _root: resolved)
    monkeypatch.setattr(managed_runtime, "resolve_comfyui_install", lambda *_a, **_k: resolved)


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_start_requires_managed_mode(client: TestClient) -> None:
    client.put(
        "/api/v1/setup/config",
        json={"mode": "existing_comfyui", "base_url": "http://127.0.0.1:8190"},
    )
    response = client.post("/api/v1/setup/runtime/start")
    assert response.status_code == 409


def test_start_requires_provisioned_comfyui(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from chotto_sashie_shared import managed_runtime

    monkeypatch.setattr(main, "is_provisioned", lambda _root: False)
    monkeypatch.setattr(managed_runtime, "resolve_comfyui_install", lambda *_a, **_k: None)
    client.put("/api/v1/setup/config", json={"mode": "managed_dedicated", "port": _free_port()})
    response = client.post("/api/v1/setup/runtime/start")
    assert response.status_code == 409


@pytest.mark.slow
def test_start_and_stop_dedicated_runtime(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _provision_fake_comfy(monkeypatch, tmp_path)
    port = _free_port()
    client.put("/api/v1/setup/config", json={"mode": "managed_dedicated", "port": port})

    try:
        started = client.post("/api/v1/setup/runtime/start")
        assert started.status_code == 200
        body = started.json()
        assert body["state"] == "running"
        assert body["owned"] is True
        assert body["reachable"] is True

        # 専用モデル領域だけのyamlが作られている。
        yaml_path = (tmp_path / "managed" / "runtime" / "comfy" / "extra_model_paths.yaml")
        content = yaml_path.read_text(encoding="utf-8")
        assert "chotto_sashie_managed:" in content
    finally:
        stopped = client.post("/api/v1/setup/runtime/stop")
    assert stopped.status_code == 200
    assert stopped.json()["state"] == "stopped"


@pytest.mark.slow
def test_start_is_idempotent_when_already_running(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """自動起動済みで「今すぐ起動して確認」を押しても、失敗ではなく現在の状態を返す。"""
    _provision_fake_comfy(monkeypatch, tmp_path)
    port = _free_port()
    client.put("/api/v1/setup/config", json={"mode": "managed_dedicated", "port": port})

    try:
        assert client.post("/api/v1/setup/runtime/start").status_code == 200
        again = client.post("/api/v1/setup/runtime/start")
        assert again.status_code == 200
        body = again.json()
        assert body["reachable"] is True
        assert body["state"] == "running"
    finally:
        client.post("/api/v1/setup/runtime/stop")


def test_start_returns_starting_while_engine_is_loading(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """自動起動が読み込み中のときは、失敗ではなく「起動しています」を返す。

    ComfyUIの読み込みには数十秒かかる。ここで409を返すと、画面が
    「接続できません」で止まってしまう。
    """
    port = _free_port()
    client.put("/api/v1/setup/config", json={"mode": "managed_dedicated", "port": port})
    monkeypatch.setattr(
        main.runtime_manager,
        "status",
        lambda _port=port: {
            "state": "starting",
            "owned": True,
            "reachable": False,
            "pid": 1234,
            "base_url": f"http://127.0.0.1:{port}",
            "profile_path": None,
            "log_path": None,
            "message": "画像生成エンジンを起動しています",
        },
    )

    response = client.post("/api/v1/setup/runtime/start")
    assert response.status_code == 200
    assert response.json()["state"] == "starting"


@pytest.mark.slow
def test_external_models_are_written_into_config(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _provision_fake_comfy(monkeypatch, tmp_path)
    external = tmp_path / "existing-models"
    (external / "checkpoints").mkdir(parents=True)
    (external / "vae").mkdir(parents=True)
    port = _free_port()
    client.put(
        "/api/v1/setup/config",
        json={
            "mode": "managed_external_models",
            "port": port,
            "external_model_roots": [str(external)],
        },
    )

    try:
        started = client.post("/api/v1/setup/runtime/start")
        assert started.status_code == 200
        yaml_path = tmp_path / "managed" / "runtime" / "comfy" / "extra_model_paths.yaml"
        content = yaml_path.read_text(encoding="utf-8")
        assert "chotto_sashie_external_1:" in content
        # パスはJSONエンコードで（バックスラッシュがエスケープされて）書かれる。
        assert json.dumps(str(external.resolve()), ensure_ascii=False) in content
    finally:
        client.post("/api/v1/setup/runtime/stop")
