from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import IO
from uuid import uuid4

import httpx

from .comfy_scratch import ComfyScratch
from .comfyui_provision import offline_flags
from .download import safe_destination
from .setup_config import DEFAULT_MANAGED_PORT

# 専用ComfyUIの入力・出力フォルダ（管理領域からの相対）。生成物の写しの片付けも
# 同じ場所を見るので、ここ1か所で決める。
_COMFY_INPUT_DIR = "runtime/comfy/input"
_COMFY_OUTPUT_DIR = "runtime/comfy/output"


class RuntimeManagerError(RuntimeError):
    """管理対象の生成エンジンを安全に起動できない状態を表す。"""


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_file(path: Path, label: str) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as error:
        raise RuntimeManagerError(f"{label}にアクセスできません: {path}") from error
    if not resolved.is_file():
        raise RuntimeManagerError(f"{label}ではありません: {resolved}")
    return resolved


def comfy_io_directories(managed_root: Path) -> tuple[Path, Path]:
    """専用ComfyUIの (入力フォルダ, 出力フォルダ)。"""
    resolved = managed_root.expanduser().resolve()
    return (
        safe_destination(resolved, _COMFY_INPUT_DIR),
        safe_destination(resolved, _COMFY_OUTPUT_DIR),
    )


def build_runtime_plan(
    *,
    managed_root: Path,
    comfyui_path: Path,
    python_path: Path,
    port: int,
    disable_custom_nodes: bool = True,
    disable_api_nodes: bool = True,
    allow_comfyui_inside_managed: bool = False,
) -> dict[str, object]:
    """書き込みを行わず、管理起動に必要な保存先とコマンドを組み立てる。

    disable_custom_nodes/disable_api_nodes で起動フラグを切り替える。既定(両方True)は
    最小構成で、合成・動画のノードを使うときはカスタムノードを許可する。

    allow_comfyui_inside_managed=True は、アプリが自分で用意した専用ComfyUIを
    管理領域の内側に置く配置向け。外部インストールを借用する場合は
    既定のまま分離を必須にする。
    """
    if port < 1024 or port > 65535:
        raise RuntimeManagerError("生成エンジンのポートは1024～65535で指定してください")

    resolved_managed = managed_root.expanduser().resolve()
    try:
        resolved_comfy = comfyui_path.expanduser().resolve(strict=True)
    except OSError as error:
        raise RuntimeManagerError(f"ComfyUIフォルダにアクセスできません: {comfyui_path}") from error
    if not resolved_comfy.is_dir():
        raise RuntimeManagerError(f"ComfyUIフォルダではありません: {resolved_comfy}")
    main_script = _resolve_file(resolved_comfy / "main.py", "ComfyUI main.py")
    resolved_python = _resolve_file(python_path, "ComfyUI用Python")

    # 管理領域が既存ComfyUIの内側にある配置は、外部フォルダへ書き込むため常に拒否する。
    if _is_inside(resolved_managed, resolved_comfy):
        raise RuntimeManagerError("アプリ管理領域と既存ComfyUIフォルダは分離してください")
    if not allow_comfyui_inside_managed and _is_inside(resolved_comfy, resolved_managed):
        raise RuntimeManagerError("アプリ管理領域と既存ComfyUIフォルダは分離してください")

    relative_directories = {
        "input": _COMFY_INPUT_DIR,
        "output": _COMFY_OUTPUT_DIR,
        "temp": "runtime/comfy/temp",
        "user": "runtime/comfy/user",
        "logs": "runtime/comfy/logs",
        "huggingface_cache": "runtime/comfy/cache/huggingface",
        "torch_cache": "runtime/comfy/cache/torch",
        "xdg_cache": "runtime/comfy/cache/xdg",
        "python_cache": "runtime/comfy/cache/pycache",
    }
    directories = {
        name: str(safe_destination(resolved_managed, relative))
        for name, relative in relative_directories.items()
    }
    model_paths_config = safe_destination(
        resolved_managed, "runtime/comfy/extra_model_paths.yaml"
    )
    profile_path = safe_destination(resolved_managed, "runtime/comfy/runtime-profile.json")
    log_path = safe_destination(resolved_managed, "runtime/comfy/logs/comfyui.log")
    base_url = f"http://127.0.0.1:{port}"

    command = [
        str(resolved_python),
        str(main_script),
        "--listen",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    if disable_custom_nodes:
        command.append("--disable-all-custom-nodes")
    if disable_api_nodes:
        command.extend(offline_flags(resolved_comfy))
    # 大きなモデルは、ホストRAMへ抱え込ませずディスクから読ませる。
    # 既定では読み込んだ重みをピン留めRAMへ載せたまま次のプロンプトへ進むため、
    # 40.9GBのQwen-Image-Edit-2511 bf16（参照画像の「向き」を高品質で作るときに使う）
    # で64GB機のRAMを使い切り、生成が MemoryError で落ちていた。このオプションを
    # 付けた同じ1枚は、空きRAMをほとんど減らさずに完走する（実測 47.9GB→46.0GB）。
    # モデルは速いディスクに置く前提。
    command.append("--fast-disk")
    command += [
        "--extra-model-paths-config",
        str(model_paths_config),
        "--input-directory",
        directories["input"],
        "--output-directory",
        directories["output"],
        "--temp-directory",
        directories["temp"],
        "--user-directory",
        directories["user"],
    ]
    environment = {
        "HF_HOME": directories["huggingface_cache"],
        "TORCH_HOME": directories["torch_cache"],
        "XDG_CACHE_HOME": directories["xdg_cache"],
        "PYTHONPYCACHEPREFIX": directories["python_cache"],
        "PYTHONDONTWRITEBYTECODE": "1",
        "TEMP": directories["temp"],
        "TMP": directories["temp"],
    }
    profile = {
        "schema_version": 1,
        "managed_root": str(resolved_managed),
        "comfyui_path": str(resolved_comfy),
        "python_path": str(resolved_python),
        "base_url": base_url,
        "model_paths_config": str(model_paths_config),
        "directories": directories,
        "command": command,
        "environment": environment,
        "external_environment_read_only": True,
    }
    return {
        **profile,
        "profile_path": str(profile_path),
        "log_path": str(log_path),
        "model_paths_ready": model_paths_config.is_file(),
        "profile_content": json.dumps(profile, ensure_ascii=False, indent=2) + "\n",
    }


def write_runtime_profile(
    plan: dict[str, object], *, overwrite_existing: bool = False
) -> tuple[bool, bool]:
    """確認済みの起動プロファイルと管理ディレクトリだけを作成する。"""
    managed_root = Path(str(plan["managed_root"])).resolve()
    profile_path = safe_destination(managed_root, "runtime/comfy/runtime-profile.json")
    expected_profile = Path(str(plan["profile_path"]))
    if profile_path != expected_profile:
        raise RuntimeManagerError("起動プロファイルの保存先が検査時から変わりました")
    content = str(plan["profile_content"])

    if profile_path.is_file():
        current = profile_path.read_text(encoding="utf-8")
        if current == content:
            return False, True
        if not overwrite_existing:
            raise RuntimeManagerError(
                "既存の起動プロファイルと内容が異なります。自動では上書きしません"
            )

    for raw_directory in dict(plan["directories"]).values():
        directory = Path(str(raw_directory))
        relative = directory.relative_to(managed_root)
        safe_destination(managed_root, str(relative)).mkdir(parents=True, exist_ok=True)

    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path = safe_destination(managed_root, "runtime/comfy/runtime-profile.json")
    temporary = profile_path.with_name(f".{profile_path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, profile_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return True, False


def port_is_open(port: int) -> bool:
    """指定ポートで既に何かが待ち受けているかを返す（自動起動の重複防止に使う）。"""
    return _port_is_open(port)


def _port_is_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


def wait_for_port_to_free(port: int, timeout_seconds: float = 30.0) -> bool:
    """終了しかけのプロセスがポートを手放すまで待つ。空いたら True。

    アプリを入れ直すと、前のComfyUIがまだ終了処理中でポートを握っていることがある。
    その一瞬で「使用中」と判断して起動を見送ると、前のプロセスが消えたあとに
    何も動いていない状態が残る（利用者には「接続できません」だけが見える）。
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _port_is_open(port):
            return True
        time.sleep(0.5)
    return not _port_is_open(port)


def comfy_is_ready(base_url: str) -> bool:
    """その接続先で実際にComfyUIが応答しているか（ポートが開いているだけと区別する）。"""
    return _comfy_is_ready(base_url)


def _comfy_is_ready(base_url: str) -> bool:
    try:
        response = httpx.get(f"{base_url}/system_stats", timeout=1.0)
        return response.status_code == 200
    except httpx.HTTPError:
        return False


class RuntimeManager:
    """このAPIが起動したComfyUIだけを所有・停止する。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._log_stream: IO[bytes] | None = None
        self._plan: dict[str, object] | None = None

    def _owned_process_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def status(self, port: int = DEFAULT_MANAGED_PORT) -> dict[str, object]:
        with self._lock:
            plan = self._plan
            process = self._process
            owned = self._owned_process_running()
            base_url = str(plan["base_url"]) if plan else f"http://127.0.0.1:{port}"
            reachable = _comfy_is_ready(base_url)
            if owned and reachable:
                state = "running"
                message = "アプリ管理の画像生成エンジンが動作しています"
            elif owned:
                state = "starting"
                message = "画像生成エンジンを起動しています"
            elif reachable:
                state = "external"
                message = "このAPIが起動したものではない生成エンジンが応答しています"
            else:
                state = "stopped"
                message = "画像生成エンジンは停止しています"
            return {
                "state": state,
                "owned": owned,
                "reachable": reachable,
                "pid": process.pid if owned and process else None,
                "base_url": base_url,
                "profile_path": str(plan["profile_path"]) if plan else None,
                "log_path": str(plan["log_path"]) if plan else None,
                "message": message,
            }

    def start(
        self, plan: dict[str, object], *, timeout_seconds: float = 300.0
    ) -> dict[str, object]:
        """専用ComfyUIを起動し、応答するまで待つ。

        待ち時間は長めに取る。モデルフォルダが大きいと、読み込みだけで数分かかる
        ことがあり、短く切ると起動途中のプロセスを終了させてしまう（利用者からは
        「起動しても繋がらない」に見える）。応答しないまま落ちた場合は poll で
        すぐ気づくので、この上限は固まったときの保険。
        """
        with self._lock:
            if self._owned_process_running():
                raise RuntimeManagerError("アプリ管理の画像生成エンジンはすでに動作しています")
            base_url = str(plan["base_url"])
            port = int(base_url.rsplit(":", 1)[1])
            # 応答するComfyUIが居るなら、それを尊重して止めない。応答しないのに
            # 握られているだけなら、終了しかけの前のプロセスなので手放すまで待つ。
            if _port_is_open(port) and not _comfy_is_ready(base_url) and wait_for_port_to_free(port):
                pass
            if _port_is_open(port):
                raise RuntimeManagerError(
                    f"ポート{port}は別のプロセスが使用中です。既存プロセスは停止しません"
                )
            model_paths_config = Path(str(plan["model_paths_config"]))
            if not model_paths_config.is_file():
                raise RuntimeManagerError(
                    "外部モデルの参照設定がありません。先に参照設定を作成してください"
                )

            # 起動する前は、この入出力フォルダを使っている生成が無い。途中で落ちて
            # 片付けまで届かなかった写しを、ここで残さず消しておく。
            directories = dict(plan["directories"])
            ComfyScratch(
                Path(str(directories["input"])), Path(str(directories["output"]))
            ).sweep()

            log_path = Path(str(plan["log_path"]))
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_stream = log_path.open("ab", buffering=0)
            environment = os.environ.copy()
            environment.update({str(key): str(value) for key, value in dict(plan["environment"]).items()})
            creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            try:
                self._process = subprocess.Popen(
                    [str(value) for value in list(plan["command"])],
                    cwd=str(plan["comfyui_path"]),
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=self._log_stream,
                    stderr=subprocess.STDOUT,
                    creationflags=creation_flags,
                )
                self._plan = plan
            except OSError as error:
                self._close_log()
                raise RuntimeManagerError(f"画像生成エンジンを起動できません: {error}") from error

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            with self._lock:
                process = self._process
                if process is None:
                    raise RuntimeManagerError("画像生成エンジンの起動状態を取得できません")
                return_code = process.poll()
            if return_code is not None:
                tail = self._log_tail(Path(str(plan["log_path"])))
                self._clear_finished_process()
                raise RuntimeManagerError(
                    f"画像生成エンジンが起動直後に終了しました（終了コード {return_code}）{tail}"
                )
            if _comfy_is_ready(base_url):
                return self.status(port)
            time.sleep(0.25)

        self.shutdown()
        tail = self._log_tail(Path(str(plan["log_path"])))
        raise RuntimeManagerError(f"画像生成エンジンが時間内に応答しませんでした{tail}")

    def shutdown(self) -> None:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                self._clear_finished_process()
                return
            process.terminate()

        try:
            process.wait(timeout=12)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        finally:
            with self._lock:
                self._process = None
                self._close_log()

    def stop(self) -> dict[str, object]:
        with self._lock:
            plan = self._plan
            port = int(str(plan["base_url"]).rsplit(":", 1)[1]) if plan else DEFAULT_MANAGED_PORT
        self.shutdown()
        return self.status(port)

    def _clear_finished_process(self) -> None:
        with self._lock:
            self._process = None
            self._close_log()

    def _close_log(self) -> None:
        if self._log_stream is not None:
            self._log_stream.close()
            self._log_stream = None

    @staticmethod
    def _log_tail(path: Path) -> str:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-8:]
        except OSError:
            return ""
        return f"。ログ末尾: {' | '.join(lines)}" if lines else ""
