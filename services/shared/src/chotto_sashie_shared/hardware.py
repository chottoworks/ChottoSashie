"""この端末のGPUとメモリを調べる（おすすめ設定の土台）。

見る順番は2つ:

1. **ComfyUI の ``/system_stats``** — 実際に生成を回す側の見立てなので最優先。
   専用ComfyUIが起動していれば、いま空いているVRAMまで分かる。
2. **``nvidia-smi``** — ComfyUIがまだ動いていない段階（＝セットアップ中）の代わり。

``nvidia-smi`` からは compute capability も取る。MiniMax H3 のテキストエンコーダー
（nvfp4）は Blackwell 世代（12.0）以降でしか動かないため、VRAMの量だけでは
「H3を勧めてよいか」を判断できない。ComfyUI の ``/system_stats`` は世代を返さないので、
つながる場合でも ``nvidia-smi`` は併せて実行し、数値だけをComfyUI側で上書きする。

どちらも取れない環境（GPU無し・ドライバ未導入・非Windows）でも例外にはしない。
``detected=False`` を返し、おすすめは既定のまま据え置く。
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
from pathlib import Path

import httpx
from pydantic import BaseModel

# Windowsでコンソールウィンドウを出さずに子プロセスを起動する（child_processesと同じ）。
_CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# nvidia-smi の応答待ち。応答しないドライバに引っ張られて画面を止めない。
_PROBE_TIMEOUT_SECONDS = 6.0

GIB = 1024**3


class GpuProbe(BaseModel):
    """GPU 1枚ぶんの見立て。取れなかった項目は空・0のままにする。"""

    name: str = ""
    vram_total_bytes: int = 0
    # いま空いているVRAM。ComfyUIから読めたときだけ入る。
    vram_free_bytes: int | None = None
    # 例: "12.0"（Blackwell）。nvidia-smi からしか取れない。
    compute_capability: str = ""
    driver_version: str = ""


class HardwareProbe(BaseModel):
    """この端末の見立て。"""

    detected: bool = False
    # comfyui | nvidia-smi | none
    source: str = "none"
    gpu: GpuProbe = GpuProbe()
    ram_total_bytes: int = 0
    # 調べられなかったときに、何が起きたかを利用者へ伝える一言。
    message: str = ""


def compute_capability_value(text: str) -> float:
    """"12.0" のような世代表記を数値にする。読めなければ 0.0（＝条件を満たさない扱い）。"""
    try:
        return float(text.strip())
    except (AttributeError, ValueError):
        return 0.0


def host_ram_total_bytes() -> int:
    """このPCの物理メモリ合計。読めなければ0。

    ComfyUI はモデルをホストRAMへ置いてからVRAMへ載せるため、VRAMが足りていても
    RAMが細いと大きなモデルで詰まる。おすすめの判断に要る。
    """
    if os.name == "nt":

        class _MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemoryStatus()
        status.dwLength = ctypes.sizeof(_MemoryStatus)
        try:
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys)
        except (AttributeError, OSError):
            return 0
        return 0
    try:
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, ValueError, OSError):
        return 0


def _nvidia_smi(fields: str) -> list[list[str]]:
    """nvidia-smi を1回叩き、CSVの各行を項目へ割ったものを返す。失敗したら空。"""
    try:
        completed = subprocess.run(  # noqa: S603 - 引数は固定、シェルは介さない
            ["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
            creationflags=_CREATION_FLAGS,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    rows: list[list[str]] = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        rows.append([cell.strip() for cell in line.split(",")])
    return rows


def _megabytes_to_bytes(text: str) -> int:
    try:
        return int(float(text)) * 1024 * 1024
    except ValueError:
        return 0


def detect_local_hardware() -> HardwareProbe:
    """nvidia-smi と OS から、この端末の構成を調べる。"""
    ram = host_ram_total_bytes()
    rows = _nvidia_smi("name,memory.total,driver_version")
    if not rows:
        return HardwareProbe(
            detected=False,
            source="none",
            ram_total_bytes=ram,
            message=(
                "NVIDIAのGPUを確認できませんでした（nvidia-smiが見つからないか、応答しません）。"
                "おすすめは判定できませんが、設定は手動で選べます。"
            ),
        )
    # 一番VRAMの多い1枚を代表にする。生成は1件ずつ・1枚のGPUで走らせるため。
    best = max(rows, key=lambda row: _megabytes_to_bytes(row[1]) if len(row) > 1 else 0)
    gpu = GpuProbe(
        name=best[0] if best else "",
        vram_total_bytes=_megabytes_to_bytes(best[1]) if len(best) > 1 else 0,
        driver_version=best[2] if len(best) > 2 else "",
    )
    # compute_cap は少し古いドライバだと通らない。通らなくても他の値は残したいので別に叩く。
    capabilities = _nvidia_smi("compute_cap")
    if capabilities and capabilities[0]:
        gpu = gpu.model_copy(update={"compute_capability": capabilities[0][0]})
    return HardwareProbe(
        detected=gpu.vram_total_bytes > 0,
        source="nvidia-smi",
        gpu=gpu,
        ram_total_bytes=ram,
    )


def _device_display_name(raw: str) -> str:
    """ComfyUIのデバイス名 "cuda:0 NVIDIA GeForce RTX 5090 : cudaMallocAsync" を製品名だけにする。"""
    text = raw.split(" : ")[0].strip()
    head, _, rest = text.partition(" ")
    return rest.strip() if head.startswith("cuda:") and rest.strip() else text


def probe_from_system_stats(payload: dict) -> HardwareProbe | None:
    """ComfyUIの ``/system_stats`` の応答から見立てを作る。形が違えば None。"""
    if not isinstance(payload, dict):
        return None
    devices = payload.get("devices")
    if not isinstance(devices, list):
        return None
    usable = [device for device in devices if isinstance(device, dict)]
    if not usable:
        return None
    best = max(usable, key=lambda device: int(device.get("vram_total") or 0))
    system = payload.get("system") if isinstance(payload.get("system"), dict) else {}
    free = best.get("vram_free")
    gpu = GpuProbe(
        name=_device_display_name(str(best.get("name", ""))),
        vram_total_bytes=int(best.get("vram_total") or 0),
        vram_free_bytes=int(free) if isinstance(free, (int, float)) else None,
    )
    return HardwareProbe(
        detected=gpu.vram_total_bytes > 0,
        source="comfyui",
        gpu=gpu,
        ram_total_bytes=int(system.get("ram_total") or 0),
    )


def _merge(local: HardwareProbe, comfy: HardwareProbe) -> HardwareProbe:
    """ComfyUIの数値を優先しつつ、そこには無い世代・ドライバをnvidia-smiから補う。"""
    gpu = comfy.gpu.model_copy(
        update={
            "name": comfy.gpu.name or local.gpu.name,
            "compute_capability": local.gpu.compute_capability,
            "driver_version": local.gpu.driver_version,
        }
    )
    return HardwareProbe(
        detected=comfy.detected or local.detected,
        source="comfyui",
        gpu=gpu,
        ram_total_bytes=comfy.ram_total_bytes or local.ram_total_bytes,
    )


async def fetch_system_stats(base_url: str, timeout: float = 5.0) -> dict | None:
    """ComfyUIの ``/system_stats`` を読む。つながらなければ None（失敗にはしない）。"""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{base_url.rstrip('/')}/system_stats")
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


async def probe_hardware(base_url: str | None = None) -> HardwareProbe:
    """この端末の構成を調べる。ComfyUIへつながるなら、その見立てを優先する。"""
    local = detect_local_hardware()
    if not base_url:
        return local
    payload = await fetch_system_stats(base_url)
    if payload is None:
        return local
    comfy = probe_from_system_stats(payload)
    if comfy is None or not comfy.detected:
        return local
    return _merge(local, comfy)


def load_profiles(path: Path) -> dict:
    """おすすめ設定のマニフェストを読む。"""
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("おすすめ設定マニフェストの形式が正しくありません")
    return manifest
