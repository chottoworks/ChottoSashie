"""ComfyUIとのHTTP通信、およびAPI形式ワークフローのバインディング（複数アプリ共有版）。"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx

from .comfy_progress import PromptProgress, progress_watcher


CORE_REQUIRED_NODES = {
    "UNETLoader",
    "CLIPLoader",
    "VAELoader",
    "CLIPTextEncode",
    "KSampler",
    "VAEDecode",
    "SaveImage",
}


class ComfyConnectionError(RuntimeError):
    pass


class ComfyClient:
    def __init__(self, base_url: str, timeout: float = 10):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def object_info(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(f"{self.base_url}/object_info")
                response.raise_for_status()
                value = response.json()
                if not isinstance(value, dict):
                    raise ComfyConnectionError("ノード一覧の形式が正しくありません")
                return value
        except (httpx.HTTPError, ValueError) as error:
            raise ComfyConnectionError(f"ComfyUIへ接続できません: {error}") from error

    async def submit(self, workflow: dict[str, Any]) -> str:
        """ワークフローを依頼し、prompt IDを返す。

        途中経過はこのアプリの線（`comfy_progress`）の `client_id` で依頼したものにしか
        届かないため、依頼の前に線をつないでおき、その `client_id` で送る。
        """
        watcher = progress_watcher(self.base_url)
        await watcher.ensure_listening()
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{self.base_url}/prompt",
                    json={"prompt": workflow, "client_id": watcher.client_id},
                )
                response.raise_for_status()
                prompt_id = response.json().get("prompt_id")
                if not isinstance(prompt_id, str) or not prompt_id:
                    raise ComfyConnectionError("ComfyUIからprompt IDが返りませんでした")
                watcher.track(prompt_id, workflow)
                return prompt_id
        except httpx.HTTPStatusError as error:
            detail = error.response.text[:2000].strip()
            suffix = f": {detail}" if detail else ""
            raise ComfyConnectionError(
                f"生成要求をComfyUIが受理しませんでした（HTTP {error.response.status_code}）{suffix}"
            ) from error
        except (httpx.HTTPError, ValueError) as error:
            raise ComfyConnectionError(f"生成要求を送信できません: {error}") from error

    async def history(self, prompt_id: str) -> dict[str, Any] | None:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(f"{self.base_url}/history/{prompt_id}")
                response.raise_for_status()
                history = response.json()
                if not isinstance(history, dict):
                    raise ComfyConnectionError("生成履歴の形式が正しくありません")
                entry = history.get(prompt_id)
                return entry if isinstance(entry, dict) else None
        except (httpx.HTTPError, ValueError) as error:
            raise ComfyConnectionError(f"生成履歴を確認できません: {error}") from error

    def prompt_progress(self, prompt_id: str) -> PromptProgress | None:
        """依頼したプロンプトの途中経過。何も届いていなければ None。"""
        return progress_watcher(self.base_url).progress(prompt_id)

    async def view_image(self, image: dict[str, str]) -> tuple[bytes, str]:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    f"{self.base_url}/view",
                    params={
                        "filename": image["filename"],
                        "subfolder": image.get("subfolder", ""),
                        "type": image.get("type", "output"),
                    },
                )
                response.raise_for_status()
                return response.content, response.headers.get("content-type", "image/png")
        except (httpx.HTTPError, KeyError) as error:
            raise ComfyConnectionError(f"生成画像を取得できません: {error}") from error

    async def upload_image(
        self,
        data: bytes,
        filename: str,
        content_type: str = "image/png",
        *,
        subfolder: str = "",
        overwrite: bool = True,
    ) -> dict[str, str]:
        """ローカルの画像をComfyUIのinputフォルダへ送り込み、参照情報を返す。

        合成では人物・背景・小物の静止画がこのアプリのディスク上にあり、ComfyUIの
        出力ではない。LoadImageで読ませるには、まずComfyUIのinputへ登録する必要がある。
        戻り値は ``{"name", "subfolder", "type"}`` で、LoadImageの ``image`` 入力へ渡す。
        """
        try:
            files = {"image": (filename, data, content_type)}
            payload = {"type": "input", "overwrite": "true" if overwrite else "false"}
            if subfolder:
                payload["subfolder"] = subfolder
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{self.base_url}/upload/image",
                    files=files,
                    data=payload,
                )
                response.raise_for_status()
                body = response.json()
            if not isinstance(body, dict) or not isinstance(body.get("name"), str):
                raise ComfyConnectionError("ComfyUIから画像登録結果が返りませんでした")
            return {
                "name": body["name"],
                "subfolder": body.get("subfolder", "") if isinstance(body.get("subfolder", ""), str) else "",
                "type": body.get("type", "input") if isinstance(body.get("type", "input"), str) else "input",
            }
        except (httpx.HTTPError, ValueError) as error:
            raise ComfyConnectionError(f"画像をComfyUIへ登録できません: {error}") from error

    async def interrupt(self) -> bool:
        """実行中のプロンプトを中止するよう依頼する（best-effort）。

        利用者が「中止」を押したときに、GPUを掴んだままの生成を止めるために使う。
        ComfyUIが止まっている・古い等で失敗しても中止操作自体は成立させるので、
        例外は返さずに成否だけ返す。
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(f"{self.base_url}/interrupt")
                return response.status_code in {200, 204}
        except httpx.HTTPError:
            return False

    async def free_vram(self) -> bool:
        """常駐モデルを退避しVRAMを解放するよう依頼する（best-effort）。

        学習・高品質生成はDiffSynthの別プロセスで同じGPUを使う。直前にComfyUIの
        VRAMを空けておかないと割り当てに失敗して落ちるため、その前処理に使う。
        ComfyUIが動いていない・古い等で失敗しても学習は止めないので、例外は返さない。
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/free",
                    json={"unload_models": True, "free_memory": True},
                )
                return response.status_code == 200
        except httpx.HTTPError:
            return False


def output_filenames(history_entry: dict[str, Any]) -> list[str]:
    filenames: list[str] = []
    outputs = history_entry.get("outputs", {})
    if not isinstance(outputs, dict):
        return filenames
    for node_output in outputs.values():
        if not isinstance(node_output, dict):
            continue
        images = node_output.get("images", [])
        if not isinstance(images, list):
            continue
        for image in images:
            if not isinstance(image, dict) or not isinstance(image.get("filename"), str):
                continue
            subfolder = image.get("subfolder")
            filenames.append(f"{subfolder}/{image['filename']}" if subfolder else image["filename"])
    return filenames


def output_images(history_entry: dict[str, Any]) -> list[dict[str, str]]:
    images: list[dict[str, str]] = []
    outputs = history_entry.get("outputs", {})
    if not isinstance(outputs, dict):
        return images
    for node_output in outputs.values():
        if not isinstance(node_output, dict):
            continue
        candidates = node_output.get("images", [])
        if not isinstance(candidates, list):
            continue
        for image in candidates:
            if not isinstance(image, dict) or not isinstance(image.get("filename"), str):
                continue
            images.append(
                {
                    "filename": image["filename"],
                    "subfolder": image.get("subfolder", "") if isinstance(image.get("subfolder", ""), str) else "",
                    "type": image.get("type", "output") if isinstance(image.get("type", "output"), str) else "output",
                }
            )
    return images


def output_files(history_entry: dict[str, Any]) -> list[dict[str, str]]:
    """履歴から画像・動画いずれの出力ファイル記述子も拾う。

    保存ノードによって出力キーが異なる（SaveImage/SaveAnimatedWEBPは "images"、
    動画系ノードは "gifs" や "videos"）。どの保存ノードでも動くよう、
    ファイル記述子を持つリストをすべて走査する。
    """
    files: list[dict[str, str]] = []
    outputs = history_entry.get("outputs", {})
    if not isinstance(outputs, dict):
        return files
    for node_output in outputs.values():
        if not isinstance(node_output, dict):
            continue
        for value in node_output.values():
            if not isinstance(value, list):
                continue
            for item in value:
                if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
                    continue
                files.append(
                    {
                        "filename": item["filename"],
                        "subfolder": item.get("subfolder", "") if isinstance(item.get("subfolder", ""), str) else "",
                        "type": item.get("type", "output") if isinstance(item.get("type", "output"), str) else "output",
                    }
                )
    return files


def load_workflow(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        workflow = json.load(stream)
    if not isinstance(workflow, dict):
        raise ValueError("APIワークフローはJSONオブジェクトである必要があります")
    return workflow


def bind_workflow(template: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """管理対象テンプレートの許可済み入力だけを書き換える。

    `_chotto_sashie_bindings`（node id + input name の対応表）に載っている
    フィールドしか書き換えられないため、任意のノード注入はできない。
    キャラクター・背景・小物いずれのワークフローにも共通して使う。
    """
    workflow = deepcopy(template)
    bindings = workflow.pop("_chotto_sashie_bindings", None)
    if not isinstance(bindings, dict):
        raise ValueError("ワークフローにバインディング定義がありません")

    for name, value in values.items():
        binding = bindings.get(name)
        if not isinstance(binding, dict):
            raise ValueError(f"ワークフローに{name}のバインディングがありません")
        node_id = str(binding.get("node"))
        input_name = binding.get("input")
        if node_id not in workflow or not isinstance(input_name, str):
            raise ValueError(f"{name}のバインディング先が正しくありません")
        workflow[node_id].setdefault("inputs", {})[input_name] = value
    return workflow
