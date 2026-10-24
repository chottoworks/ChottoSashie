"""日本語環境で貼り付けられるフォルダ指定を、そのまま受け取れることを確かめる。

日本語配列のキーボードはバックスラッシュの位置に円記号があり、入力方式によっては
U+00A5 / U+FFE5 がそのまま送られてくる。エクスプローラーの「パスのコピー」は
引用符も付ける。どちらも直さないと「フォルダが見つかりません」になる。
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chotto_sashie_shared.jobs import JobRepository
from chotto_sashie_api import main
from chotto_sashie_api.database import Database


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    database = Database(tmp_path / "setup-paths.sqlite3")
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(main, "jobs", JobRepository(database))
    with TestClient(main.app) as test_client:
        yield test_client


def _existing_install(tmp_path: Path) -> dict[str, str]:
    comfy = tmp_path / "ExistingComfyUI"
    comfy.mkdir()
    (comfy / "main.py").write_text("", encoding="utf-8")
    python = tmp_path / "py.exe"
    python.write_text("", encoding="utf-8")
    return {"comfyui_path": str(comfy), "python_path": str(python)}


def test_model_roots_accept_yen_separator(client: TestClient, tmp_path: Path) -> None:
    """円記号区切り（D:¥AI¥Models）でも同じ場所として通る。"""
    models = tmp_path / "models"
    models.mkdir()
    typed = str(models).replace("\\", "¥")

    saved = client.put(
        "/api/v1/setup/config",
        json={"mode": "managed_external_models", "external_model_roots": [typed]},
    )
    assert saved.status_code == 200
    # 保存されるのは正規化後のパス。
    assert saved.json()["external_model_roots"] == [str(models)]


def test_model_root_with_yen_in_folder_name_is_kept(client: TestClient, tmp_path: Path) -> None:
    """区切りが打てている入力なら、フォルダ名の円記号はそのまま扱う。"""
    models = tmp_path / "料金¥表"
    models.mkdir()

    saved = client.put(
        "/api/v1/setup/config",
        json={"mode": "managed_external_models", "external_model_roots": [str(models)]},
    )
    assert saved.status_code == 200
    assert saved.json()["external_model_roots"] == [str(models)]


def test_existing_install_accepts_quoted_yen_paths(client: TestClient, tmp_path: Path) -> None:
    """「パスのコピー」の引用符付き・円記号区切りでも登録できる。"""
    install = _existing_install(tmp_path)
    quoted_comfyui = '"' + install["comfyui_path"].replace("\\", "¥") + '"'

    saved = client.put(
        "/api/v1/setup/config",
        json={
            "mode": "existing_comfyui",
            "base_url": "http://127.0.0.1:8190",
            "comfyui_path": quoted_comfyui,
            "python_path": install["python_path"].replace("\\", "¥"),
        },
    )
    assert saved.status_code == 200
    body = saved.json()
    assert body["comfyui_path"] == install["comfyui_path"]
    assert body["python_path"] == install["python_path"]


def test_invalid_existing_install_is_rejected(client: TestClient, tmp_path: Path) -> None:
    """ComfyUIのmain.pyが無いフォルダは受け付けない。"""
    empty = tmp_path / "NotComfyUI"
    empty.mkdir()

    rejected = client.put(
        "/api/v1/setup/config",
        json={
            "mode": "existing_comfyui",
            "base_url": "http://127.0.0.1:8190",
            "comfyui_path": str(empty),
            "python_path": str(tmp_path / "py.exe"),
        },
    )
    assert rejected.status_code == 422
