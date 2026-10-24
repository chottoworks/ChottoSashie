from pathlib import Path

from fastapi.testclient import TestClient

from chotto_sashie_api import main
from chotto_sashie_api.database import Database
from chotto_sashie_shared.jobs import JobRepository


def _client(tmp_path: Path, monkeypatch) -> TestClient:
    database = Database(tmp_path / "token.sqlite3")
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(main, "jobs", JobRepository(database))
    return TestClient(main.app)


def test_token_not_required_when_unset(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(main.settings, "api_token", "")
    with _client(tmp_path, monkeypatch) as client:
        # トークン未設定なら、ヘッダなしでも変更系が通る。
        created = client.post("/api/v1/projects", json={"name": "無トークン"})
    assert created.status_code == 201


def test_mutation_requires_token_when_set(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(main.settings, "api_token", "s3cret")
    with _client(tmp_path, monkeypatch) as client:
        missing = client.post("/api/v1/projects", json={"name": "拒否"})
        wrong = client.post(
            "/api/v1/projects",
            json={"name": "拒否"},
            headers={"X-Chotto-Sashie-Token": "wrong"},
        )
        ok = client.post(
            "/api/v1/projects",
            json={"name": "許可"},
            headers={"X-Chotto-Sashie-Token": "s3cret"},
        )
    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert ok.status_code == 201


def test_reads_and_health_are_open_even_with_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(main.settings, "api_token", "s3cret")
    with _client(tmp_path, monkeypatch) as client:
        # 読み取りと死活監視はトークン無しでも許可（画像は<img>でヘッダを付けられない）。
        health = client.get("/health")
        projects = client.get("/api/v1/projects")
    assert health.status_code == 200
    assert projects.status_code == 200
