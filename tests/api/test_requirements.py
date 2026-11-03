import ast
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chotto_sashie_shared.jobs import JobRepository
from chotto_sashie_api import main
from chotto_sashie_api.database import Database
from chotto_sashie_api.requirements import (
    RequirementToolStatus,
    check_requirements,
    load_requirements,
)


def _object_info(*, nodes: set[str], files_by_loader: dict[str, dict[str, list[str]]]) -> dict:
    """指定したノードと、ローダーごとの導入済みファイルを持つobject_infoを組み立てる。"""
    info: dict = {}
    for node in nodes:
        entry: dict = {"input": {"required": {}}}
        for input_name, files in files_by_loader.get(node, {}).items():
            entry["input"]["required"][input_name] = [files, {}]
        info[node] = entry
    return info


def test_check_requirements_reports_missing(tmp_path: Path) -> None:
    manifest = load_requirements(Path("model-manifests/video-requirements.json"))

    # 合成に必要なノードは全部あるが、Lightning LoRAだけ未導入。I2Vノードは丸ごと欠落。
    composition_nodes = {
        "UNETLoader", "CLIPLoader", "VAELoader", "LoadImage", "LoraLoaderModelOnly",
        "ModelSamplingAuraFlow", "CFGNorm", "FluxKontextImageScale",
        "TextEncodeQwenImageEditPlus", "FluxKontextMultiReferenceLatentMethod",
        "EmptySD3LatentImage", "KSampler", "VAEDecode", "SaveImage",
        "LoadBackgroundRemovalModel", "RemoveBackground", "MaskToImage",
        "LoadImageMask", "VAEEncode", "SetLatentNoiseMask", "DifferentialDiffusion",
    }
    object_info = _object_info(
        nodes=composition_nodes,
        files_by_loader={
            "UNETLoader": {"unet_name": ["qwen_image_edit_2511_bf16.safetensors"]},
            "CLIPLoader": {"clip_name": ["qwen_2.5_vl_7b_fp8_scaled.safetensors"]},
            "VAELoader": {"vae_name": ["qwen_image_vae.safetensors"]},
            "LoraLoaderModelOnly": {"lora_name": ["some-other-lora.safetensors"]},
        },
    )

    report = check_requirements(object_info, manifest)
    assert report.ready is False

    by_id = {capability.id: capability for capability in report.capabilities}

    composition = by_id["composition"]
    assert composition.missing_nodes == []
    lightning = next(m for m in composition.models if "Lightning" in m.display_name)
    assert lightning.present is False
    assert lightning.reason == "missing"
    assert composition.ready is False

    i2v = by_id["i2v"]
    assert "WanImageToVideo" in i2v.missing_nodes
    # Wanのモデルはローダー(UNETLoader)自体はあるが未導入 → missing。
    assert all(not model.present for model in i2v.models)
    assert i2v.ready is False


def test_check_requirements_reads_the_new_style_model_list() -> None:
    """背景除去のローダーは、候補を ["COMBO", {"options": [...]}] の形で返す。"""
    manifest = load_requirements(Path("model-manifests/video-requirements.json"))
    object_info = {
        "LoadBackgroundRemovalModel": {
            "input": {
                "required": {
                    "bg_removal_name": ["COMBO", {"options": ["birefnet.safetensors"]}]
                }
            }
        }
    }

    report = check_requirements(object_info, manifest)

    composition = next(c for c in report.capabilities if c.id == "composition")
    removal = next(m for m in composition.models if m.file == "birefnet.safetensors")
    assert removal.present is True


def _tool_present(tool_id: str, display_name: str) -> RequirementToolStatus:
    return RequirementToolStatus(id=tool_id, display_name=display_name, present=True)


def test_check_requirements_all_present(tmp_path: Path) -> None:
    manifest = load_requirements(Path("model-manifests/video-requirements.json"))
    nodes: set[str] = set()
    files_by_loader: dict[str, dict[str, list[str]]] = {}
    for capability in manifest["capabilities"]:
        nodes.update(capability["nodes"])
        for model in capability["models"]:
            files_by_loader.setdefault(model["loader"], {}).setdefault(model["input"], []).append(model["file"])

    report = check_requirements(
        _object_info(nodes=nodes, files_by_loader=files_by_loader),
        manifest,
        tool_status=_tool_present,
    )
    assert report.ready is True
    assert all(capability.ready for capability in report.capabilities)


def _required_model_files() -> set[str]:
    manifest = load_requirements(Path("model-manifests/video-requirements.json"))
    return {
        model["file"] for capability in manifest["capabilities"] for model in capability["models"]
    }


def test_every_model_the_code_names_is_checked() -> None:
    """コードが既定として名指しするモデルは、どれも「ノードとモデルの確認」に載っている。

    載っていないと、確認が揃っていると言ったまま、その生成だけがComfyUIで失敗する。
    手元に別のモデルフォルダがある端末では気づけないので、文字列で突き合わせる。
    ワークフローのテンプレートに書いたファイル名は、生成のたびに既定で差し替えるため見ない。
    """
    named: set[str] = set()
    for source in Path("services").glob("*/src/**/*.py"):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.endswith(".safetensors")
                and not node.value.startswith(".")  # 拡張子だけの文字列はファイル名ではない
            ):
                named.add(node.value)

    assert named, "既定のモデル名を1つも見つけられない（探し方が古い）"
    assert named - _required_model_files() == set()


def test_every_checked_model_can_be_fetched() -> None:
    """「ノードとモデルの確認」で足りないと言うモデルは、どれもアプリが取得できる。"""
    downloadable = {
        Path(component["destination"]).name
        for component in json.loads(
            Path("model-manifests/video-models.json").read_text(encoding="utf-8")
        )["components"]
    }

    assert _required_model_files() - downloadable == set()


def test_a_tool_that_cannot_be_checked_is_not_called_ready() -> None:
    """調べ方を渡さないときは「揃っている」と言わない（黙って通さない）。"""
    manifest = load_requirements(Path("model-manifests/video-requirements.json"))

    report = check_requirements(_object_info(nodes=set(), files_by_loader={}), manifest)

    export = next(c for c in report.capabilities if c.id == "export")
    assert export.tools[0].present is False
    assert export.ready is False


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    database = Database(tmp_path / "video-api.sqlite3")
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(main, "jobs", JobRepository(database))
    with TestClient(main.app) as test_client:
        yield test_client


def test_requirements_endpoint(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def object_info(_self):
        return {"UNETLoader": {"input": {"required": {"unet_name": [[], {}]}}}}

    monkeypatch.setattr(main.ComfyClient, "object_info", object_info)

    response = client.get("/api/v1/setup/requirements", params={"base_url": "http://127.0.0.1:8190"})
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is False
    assert {c["id"] for c in body["capabilities"]} == {
        "asset_generation",
        "composition",
        "i2v",
        "i2v_minimax_h3",
        "character_figure",
        "character_candidate",
        "character_candidate_int8",
        "prompt_translation",
        "export",
    }
    # 書き出しは任意（無くてもカット1本ずつは保存できる）。設定の選択肢に結び付いた機能は、
    # 選んでいない方が任意になる。構成を判定できない端末では設定の既定（Wan 2.2・bf16）に従う。
    optional = {c["id"] for c in body["capabilities"] if c["optional"]}
    assert optional == {
        "i2v_minimax_h3",
        "prompt_translation",
        "export",
        "character_candidate_int8",
    }


def test_requirements_follow_recommendation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, set_hardware
) -> None:
    """取得と同じく、おすすめの選択肢だけを必須として確かめる。"""
    async def object_info(_self):
        return {"UNETLoader": {"input": {"required": {"unet_name": [[], {}]}}}}

    monkeypatch.setattr(main.ComfyClient, "object_info", object_info)
    set_hardware(vram_gb=32)

    body = client.get(
        "/api/v1/setup/requirements", params={"base_url": "http://127.0.0.1:8190"}
    ).json()
    optional = {c["id"] for c in body["capabilities"] if c["optional"]}
    assert optional == {"i2v", "export", "character_candidate_int8"}

    set_hardware(vram_gb=12)
    body = client.get(
        "/api/v1/setup/requirements", params={"base_url": "http://127.0.0.1:8190"}
    ).json()
    optional = {c["id"] for c in body["capabilities"] if c["optional"]}
    assert optional == {"i2v_minimax_h3", "prompt_translation", "export", "character_candidate"}


def test_requirements_endpoint_502_when_comfy_unreachable(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from chotto_sashie_shared.comfy_client import ComfyConnectionError

    async def object_info(_self):
        raise ComfyConnectionError("接続できません")

    monkeypatch.setattr(main.ComfyClient, "object_info", object_info)

    response = client.get("/api/v1/setup/requirements", params={"base_url": "http://127.0.0.1:9999"})
    assert response.status_code == 502
