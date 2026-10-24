"""既存ComfyUIインストールを設定して起動できることを確かめる（アプリ用意版なしでも動く）。"""

from __future__ import annotations

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
    monkeypatch.setattr(main, "MANAGED_ROOT", tmp_path / "managed")
    # アプリ用意版は未準備の状態（試験ごとの共有フォルダは空）で、既存インストール
    # だけで起動できることを見る。
    with TestClient(main.app) as test_client:
        yield test_client


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _existing_comfy(tmp_path: Path) -> Path:
    source = tmp_path / "ExistingComfyUI"
    source.mkdir()
    (source / "main.py").write_text(_FAKE_COMFY_MAIN, encoding="utf-8")
    return source


@pytest.mark.slow
def test_start_without_provisioned_but_with_existing_install(client: TestClient, tmp_path: Path) -> None:
    source = _existing_comfy(tmp_path)
    port = _free_port()
    saved = client.put(
        "/api/v1/setup/config",
        json={
            "mode": "managed_dedicated",
            "port": port,
            "comfyui_path": str(source),
            "python_path": sys.executable,
        },
    )
    assert saved.status_code == 200

    try:
        started = client.post("/api/v1/setup/runtime/start")
        assert started.status_code == 200
        assert started.json()["state"] == "running"
    finally:
        stopped = client.post("/api/v1/setup/runtime/stop")
    assert stopped.json()["state"] == "stopped"


def test_start_without_any_comfyui_is_409(client: TestClient) -> None:
    client.put("/api/v1/setup/config", json={"mode": "managed_dedicated", "port": _free_port()})
    response = client.post("/api/v1/setup/runtime/start")
    assert response.status_code == 409
    assert "ComfyUI" in response.json()["detail"]
