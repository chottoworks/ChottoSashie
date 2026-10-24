"""各機能が要求するComfyUIノード・モデル・この端末の道具の充足チェック（共通の土台）。

実ComfyUIの ``/object_info`` を突き合わせ、機能ごとに「不足しているノード／モデル」
を報告する。セットアップ時に「何を導入すればよいか」を利用者へ示す土台であり、
マニフェストへ download_url/sha256/size を足せば自動DLへもつなげられる。

ComfyUIの外で動く道具（書き出しに使う処理エンジンなど）も、同じ枠組みで扱う。
確認の仕方は道具ごとに違うので、ここでは「何が要るか」だけをマニフェストから読み、
「あるか」の判定は呼び出し側から受け取る（この土台が個々の道具を知らずに済むように）。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class RequirementModelStatus(BaseModel):
    file: str
    display_name: str
    present: bool
    # present | missing | loader_missing（ローダーノード自体が無く確認できない）
    reason: str


class RequirementToolStatus(BaseModel):
    """ComfyUIの外で動く道具の充足状態。"""

    id: str
    display_name: str
    present: bool
    # 使っているものの場所（分かるときだけ）。
    detail: str = ""
    # アプリが用意できるか。できるなら、押す前に量を出せるようにする。
    can_install: bool = False
    install_size_bytes: int = 0
    # 何をどこから取得するのか（外部の配布物を入れるので、押す前に見せる）。
    source: str = ""
    license: str = ""
    # 用意できないときに、利用者が何をすればよいか。
    hint: str = ""


class CapabilityRequirement(BaseModel):
    id: str
    display_name: str
    ready: bool
    # 任意の機能。不足していても全体の判定（ready）は下げない。
    optional: bool = False
    present_nodes: list[str]
    missing_nodes: list[str]
    models: list[RequirementModelStatus]
    # この端末に要る道具（ComfyUIのノード・モデルではないもの）。
    tools: list[RequirementToolStatus] = []


class RequirementsReport(BaseModel):
    ready: bool
    capabilities: list[CapabilityRequirement]


def load_requirements(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("要件マニフェストの形式が正しくありません")
    return manifest


def _available_files(object_info: dict[str, Any], loader: str, input_name: str) -> list[str] | None:
    """ローダーノードの入力候補（導入済みモデルファイル一覧）を返す。

    ローダーノード自体が無ければ None（=確認不能）を返す。ComfyUIのobject_infoでは
    コンボ入力は ``[[選択肢...], {設定}]`` の形で、先頭要素が候補一覧になっている。
    """
    node = object_info.get(loader)
    if not isinstance(node, dict):
        return None
    inputs = node.get("input", {})
    if not isinstance(inputs, dict):
        return None
    for group in ("required", "optional"):
        section = inputs.get(group, {})
        if isinstance(section, dict) and input_name in section:
            spec = section[input_name]
            if isinstance(spec, list) and spec and isinstance(spec[0], list):
                return [str(item) for item in spec[0]]
            return []
    return []


def check_requirements(
    object_info: dict[str, Any],
    manifest: dict[str, Any],
    *,
    include_video: bool = True,
    tool_status: Callable[[str, str], RequirementToolStatus] | None = None,
) -> RequirementsReport:
    """機能ごとの充足を調べる。

    `include_video` が False のときは、動画を作るときだけ使う機能を report から丸ごと
    外す。挿絵だけを作る設定の人に、使わない機能の不足を見せないため。

    `tool_status` は「道具のidと表示名を渡すと、その充足状態を返す」関数。渡されない
    ときは、確認できないものとして扱う（この土台は個々の道具の調べ方を持たない）。
    """
    available_nodes = set(object_info.keys())
    capabilities: list[CapabilityRequirement] = []

    for capability in manifest.get("capabilities", []):
        if not isinstance(capability, dict):
            continue
        if not include_video and bool(capability.get("video_only", False)):
            continue
        required_nodes = [str(node) for node in capability.get("nodes", [])]
        present_nodes = [node for node in required_nodes if node in available_nodes]
        missing_nodes = [node for node in required_nodes if node not in available_nodes]

        models: list[RequirementModelStatus] = []
        for model in capability.get("models", []):
            if not isinstance(model, dict):
                continue
            file = str(model.get("file", ""))
            available = _available_files(object_info, str(model.get("loader", "")), str(model.get("input", "")))
            if available is None:
                present, reason = False, "loader_missing"
            elif file in available:
                present, reason = True, "present"
            else:
                present, reason = False, "missing"
            models.append(
                RequirementModelStatus(
                    file=file,
                    display_name=str(model.get("display_name_ja", file)),
                    present=present,
                    reason=reason,
                )
            )

        tools: list[RequirementToolStatus] = []
        for tool in capability.get("tools", []):
            if not isinstance(tool, dict):
                continue
            tool_id = str(tool.get("id", ""))
            display_name = str(tool.get("display_name_ja", tool_id))
            if tool_status is None:
                tools.append(
                    RequirementToolStatus(id=tool_id, display_name=display_name, present=False)
                )
                continue
            tools.append(tool_status(tool_id, display_name))

        ready = (
            not missing_nodes
            and all(model.present for model in models)
            and all(tool.present for tool in tools)
        )
        capabilities.append(
            CapabilityRequirement(
                id=str(capability.get("id", "")),
                display_name=str(capability.get("display_name_ja", "")),
                ready=ready,
                optional=bool(capability.get("optional", False)),
                present_nodes=present_nodes,
                missing_nodes=missing_nodes,
                models=models,
                tools=tools,
            )
        )

    return RequirementsReport(
        # 任意の機能は、使わない人の足止めにしない。
        ready=all(capability.ready for capability in capabilities if not capability.optional),
        capabilities=capabilities,
    )
