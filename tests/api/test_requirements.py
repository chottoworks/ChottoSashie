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
        "prompt_translation",
        "export",
    }
    # MiniMax H3・日本語→英語変換・書き出しは任意なので、全体の判定を左右しない印が付く
    # （書き出しが無くても、カット1本ずつは保存できる）。
    optional = {c["id"] for c in body["capabilities"] if c["optional"]}
    assert optional == {"i2v_minimax_h3", "prompt_translation", "export"}


def test_requirements_endpoint_502_when_comfy_unreachable(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from chotto_sashie_shared.comfy_client import ComfyConnectionError

    async def object_info(_self):
        raise ComfyConnectionError("接続できません")

    monkeypatch.setattr(main.ComfyClient, "object_info", object_info)

    response = client.get("/api/v1/setup/requirements", params={"base_url": "http://127.0.0.1:9999"})
    assert response.status_code == 502
