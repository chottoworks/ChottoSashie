"""前回選んだ生成方法・枚数を覚えているか。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chotto_sashie_shared.jobs import JobRepository
from chotto_sashie_api import main
from chotto_sashie_api.database import Database
from chotto_sashie_api.preferences import PreferenceRepository


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    database = Database(tmp_path / "chotto-sashie.sqlite3")
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(main, "jobs", JobRepository(database))
    with TestClient(main.app) as test_client:
        yield test_client


def test_defaults_when_nothing_selected_yet(client: TestClient) -> None:
    body = client.get("/api/v1/preferences").json()
    assert body["video_engine"] == "wan22"
    assert body["composition_quality"] == "fast"
    assert body["updated_at"] is None


def test_remembers_selection(client: TestClient) -> None:
    client.put("/api/v1/preferences", json={"video_engine": "minimax_h3"})
    assert client.get("/api/v1/preferences").json()["video_engine"] == "minimax_h3"


def test_partial_update_keeps_other_values(client: TestClient) -> None:
    client.put("/api/v1/preferences", json={"video_engine": "minimax_h3"})
    client.put("/api/v1/preferences", json={"candidate_count": 12})

    body = client.get("/api/v1/preferences").json()
    assert body["video_engine"] == "minimax_h3"
    assert body["candidate_count"] == 12


def test_rejects_unknown_value(client: TestClient) -> None:
    assert client.put("/api/v1/preferences", json={"video_engine": "svd"}).status_code == 422


def test_rejects_out_of_range_count(client: TestClient) -> None:
    assert client.put("/api/v1/preferences", json={"candidate_count": 0}).status_code == 422


def test_broken_value_falls_back_only_for_that_item(tmp_path: Path) -> None:
    """1項目が壊れていても、他の項目まで既定へ戻さない。"""
    database = Database(tmp_path / "chotto-sashie.sqlite3")
    database.initialize()
    repository = PreferenceRepository(database)
    repository.apply({"video_engine": "minimax_h3", "candidate_count": "12"})
    with database.connect() as connection:
        connection.execute("UPDATE preferences SET value = 'zzz' WHERE key = 'candidate_count'")

    current = repository.get()
    assert current.video_engine == "minimax_h3"
    assert current.candidate_count == 8


def test_unknown_key_is_ignored(tmp_path: Path) -> None:
    """知らない鍵（古い版が書いたもの）が残っていても読める。"""
    database = Database(tmp_path / "chotto-sashie.sqlite3")
    database.initialize()
    repository = PreferenceRepository(database)
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO preferences(key, value, updated_at) VALUES ('legacy_mode', 'x', '2026-01-01')"
        )
    assert repository.get().video_engine == "wan22"


def test_apply_skips_values_that_do_not_validate(tmp_path: Path) -> None:
    database = Database(tmp_path / "chotto-sashie.sqlite3")
    database.initialize()
    repository = PreferenceRepository(database)
    current = repository.apply({"video_engine": "minimax_h3", "candidate_count": "999"})

    assert current.video_engine == "minimax_h3"
    assert current.candidate_count == 8


def test_setup_clear_forgets_remembered_defaults(client: TestClient) -> None:
    """設定の初期化は、前の端末向けの既定値も一緒に忘れる。"""
    client.put("/api/v1/preferences", json={"video_engine": "minimax_h3"})
    client.post("/api/v1/setup/config/clear")

    assert client.get("/api/v1/preferences").json()["video_engine"] == "wan22"


def test_reset_keeps_remembered_defaults(client: TestClient) -> None:
    """「設定を変更する」（再セットアップ）は、選んだ既定値を消さない。"""
    client.put("/api/v1/preferences", json={"video_engine": "minimax_h3"})
    client.post("/api/v1/setup/reset")

    assert client.get("/api/v1/preferences").json()["video_engine"] == "minimax_h3"
