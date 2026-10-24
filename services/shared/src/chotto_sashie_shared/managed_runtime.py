"""管理モードの専用ComfyUIを起動する共通処理（手動起動・自動起動の両方から使う）。

managed_* モードでは ComfyUI をアプリが用意・起動する。アプリ起動時に自動で
立ち上げるのが既定の振る舞いで、セットアップ画面のボタンからも同じ経路を使う。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from .comfyui_provision import resolve_comfyui_install
from .managed_config import (
    ManagedConfigError,
    build_extra_model_paths_plan,
    write_extra_model_paths,
)
from .runtime import (
    RuntimeManager,
    RuntimeManagerError,
    build_runtime_plan,
    comfy_is_ready,
    port_is_open,
    wait_for_port_to_free,
    write_runtime_profile,
)
from .setup_config import SetupConfigRead, SetupMode
from .shared_env import shared_comfyui_root, shared_models_root


@dataclass(frozen=True)
class ManagedRuntimeOptions:
    """アプリごとに違う、管理領域と生成物の名前空間。"""

    managed_root: Path
    namespace: str
    app_label: str
    # カスタムノードは許可する（合成・動画のノードを使うため）。
    disable_custom_nodes: bool = True


class ManagedRuntimeUnavailableError(RuntimeError):
    """管理モードでないか、使えるComfyUIが無くて起動できない状態。"""


def _is_app_owned_comfyui(comfyui_dir: Path) -> bool:
    """アプリが自分で用意した専用ComfyUI（共有フォルダの展開先の内側）かどうか。"""
    try:
        return comfyui_dir.resolve().is_relative_to(shared_comfyui_root().expanduser().resolve())
    except OSError:
        return False


def start_managed_comfyui(
    config: SetupConfigRead,
    options: ManagedRuntimeOptions,
    runtime_manager: RuntimeManager,
) -> dict[str, object]:
    """設定に従って専用ComfyUIを起動し、状態を返す（同期・ブロッキング）。"""
    if not config.configured or config.mode == SetupMode.EXISTING_COMFYUI:
        raise ManagedRuntimeUnavailableError(
            "自動起動はmanaged（専用/既存モデル参照）モードでのみ使えます"
        )
    install = resolve_comfyui_install(config.comfyui_path, config.python_path)
    if install is None:
        raise ManagedRuntimeUnavailableError(
            "使用できるComfyUIがありません。「ComfyUI環境を準備」でアプリに用意させるか、"
            "既存インストールの場所を設定してください"
        )
    comfyui_dir, python_path = install
    external_roots = (
        [Path(root) for root in config.external_model_roots]
        if config.mode == SetupMode.MANAGED_EXTERNAL_MODELS
        else []
    )
    model_paths_plan = build_extra_model_paths_plan(
        options.managed_root,
        external_roots,
        namespace=options.namespace,
        app_label=options.app_label,
        allow_empty_external=config.mode == SetupMode.MANAGED_DEDICATED,
        # 取得したモデルは共有フォルダにあるため、生成エンジンから見えるよう参照に加える。
        shared_model_dirs=[shared_models_root()],
    )
    # モデル参照設定と起動プロファイルは、保存済みのセットアップ設定から毎回作り直す
    # アプリ管理領域内の派生ファイル。参照フォルダやポートを変えたときに古い内容が
    # 残って起動できなくならないよう、内容が変われば上書きする（外部環境は不変）。
    write_extra_model_paths(model_paths_plan, overwrite_existing=True)
    runtime_plan = build_runtime_plan(
        managed_root=options.managed_root,
        comfyui_path=comfyui_dir,
        python_path=python_path,
        port=config.port,
        disable_custom_nodes=options.disable_custom_nodes,
        # 専用ComfyUIの展開先が管理領域の内側にある場合でも起動できる
        # ようにする。借用した既存インストールでは分離を必須のままにする。
        allow_comfyui_inside_managed=_is_app_owned_comfyui(comfyui_dir),
    )
    write_runtime_profile(runtime_plan, overwrite_existing=True)
    return runtime_manager.start(runtime_plan)


async def autostart_managed_comfyui(
    config: SetupConfigRead,
    options: ManagedRuntimeOptions,
    runtime_manager: RuntimeManager,
    *,
    port_wait_seconds: float = 30.0,
) -> str:
    """アプリ起動時の自動起動。起動できない状況では黙って見送る（例外は出さない）。

    起動判断:
    - managed_* モードで設定済みのときだけ対象
    - そのポートで実際にComfyUIが応答していれば何もしない
      （利用者が自分で起動している場合を含む）
    - ポートは握られているのに応答しないときは、終了しかけの前のプロセスとみなして
      手放すまで待ってから起動する。ここで見送ると、前のプロセスが消えたあとに
      何も動いていない状態が残る（アプリを入れ直した直後に時々起きる）。
    """
    if not config.configured or config.mode == SetupMode.EXISTING_COMFYUI:
        return "自動起動の対象ではありません（既存ComfyUIモードまたは未設定）"
    base_url = f"http://127.0.0.1:{config.port}"
    if port_is_open(config.port):
        if comfy_is_ready(base_url):
            return f"ポート{config.port}で既に生成エンジンが動作しているため、自動起動しません"
        freed = await asyncio.to_thread(wait_for_port_to_free, config.port, port_wait_seconds)
        if not freed:
            return f"ポート{config.port}を別のプロセスが使用中のため、自動起動しません"
    try:
        # 起動完了まで待つ処理なので、ワーカースレッドへ逃がしてAPIを塞がない。
        status = await asyncio.to_thread(
            start_managed_comfyui, config, options, runtime_manager
        )
    except (ManagedRuntimeUnavailableError, ManagedConfigError, RuntimeManagerError) as error:
        return f"生成エンジンの自動起動を見送りました: {error}"
    return str(status.get("message") or "生成エンジンを自動起動しました")
