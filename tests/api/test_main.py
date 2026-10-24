from pathlib import Path

from fastapi.testclient import TestClient

from chotto_sashie_api import main
from chotto_sashie_api.database import Database
from chotto_sashie_shared.jobs import JobRepository


def test_health(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / "video-api.sqlite3")
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(main, "jobs", JobRepository(database))

    with TestClient(main.app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        body = health.json()
        assert body["status"] == "ok"
        assert body["service"] == "ちょっと挿絵"
        # デスクトップ版が「同じポートに居るのは本当に自分のAPIか」を判断する材料。
        assert body["app"] == "chotto-sashie"
        assert body["data_dir"] == str(main.settings.data_dir)
