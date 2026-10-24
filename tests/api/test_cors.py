"""デスクトップ版のWebViewからAPIを呼べることを確かめる。

Tauri 2 のWindows本番WebViewは `http://tauri.localhost` を送信元にする。ここが
許可されていないと、画面側では「押しても何も起きない」ように見える。
"""

from pathlib import Path

from fastapi.testclient import TestClient

from chotto_sashie_shared.jobs import JobRepository
from chotto_sashie_api import main
from chotto_sashie_api.database import Database


def test_tauri_production_origin_is_allowed(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / "cors-api.sqlite3")
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(main, "jobs", JobRepository(database))

    with TestClient(main.app) as client:
        response = client.get(
            "/api/v1/projects",
            headers={"Origin": "http://tauri.localhost"},
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://tauri.localhost"


def test_setup_config_can_be_saved_from_the_production_webview(
    tmp_path: Path, monkeypatch
) -> None:
    """PUT のプリフライトも通ること（セットアップの保存で使う）。"""
    database = Database(tmp_path / "cors-preflight.sqlite3")
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(main, "jobs", JobRepository(database))

    with TestClient(main.app) as client:
        response = client.options(
            "/api/v1/setup/config",
            headers={
                "Origin": "http://tauri.localhost",
                "Access-Control-Request-Method": "PUT",
                "Access-Control-Request-Headers": "content-type,x-chotto-sashie-token",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://tauri.localhost"


def test_development_origin_is_not_allowed_in_the_packaged_app(monkeypatch) -> None:
    """配布版（トークンあり）では、開発用の送信元を許さない。

    読み取り（GET）はトークンで守れない——ブラウザの `<img>` がヘッダを付けられず、
    画像がすべて表示できなくなる。そのぶん応答を読める送信元を絞る。ここを開けたまま
    配ると、たまたま1421番を使う別のプログラムから作品や画像を読み出せてしまう。
    """
    monkeypatch.setattr(main.settings, "api_token", "packaged-token")
    packaged = main._allowed_origins()

    assert packaged == ["http://tauri.localhost"]

    monkeypatch.setattr(main.settings, "api_token", "")
    development = main._allowed_origins()

    # 開発時はVite開発サーバーから使うので、そちらは許す。
    assert "http://127.0.0.1:1421" in development
    assert "http://tauri.localhost" in development
