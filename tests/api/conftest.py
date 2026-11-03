"""APIの試験が、実機のGPUに結果を左右されないようにする。

設定の読み込み・モデルの取得計画・要件の確認は、この端末へのおすすめ（GPUの構成から
決まる）に従う。実機の nvidia-smi を読むと、試験を回す端末ごとに既定が変わってしまうので、
既定では「構成を判定できない」端末として扱う。構成ごとの振る舞いを確かめる試験は、
`main.probe_hardware` を自分で差し替える。
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from chotto_sashie_api import main
from chotto_sashie_shared.hardware import GIB, GpuProbe, HardwareProbe


@pytest.fixture(autouse=True)
def _undetected_hardware(monkeypatch: pytest.MonkeyPatch):
    async def undetected(base_url: str | None = None) -> HardwareProbe:
        return HardwareProbe(detected=False, source="none", message="試験では構成を調べない")

    monkeypatch.setattr(main, "probe_hardware", undetected)
    main._recommended_cache.clear()
    yield
    main._recommended_cache.clear()


@pytest.fixture
def set_hardware(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """この端末のGPUを、指定した構成として扱わせる。"""

    def install(*, vram_gb: float, capability: str = "12.0", ram_gb: float = 64) -> None:
        async def probe(base_url: str | None = None) -> HardwareProbe:
            return HardwareProbe(
                detected=True,
                source="nvidia-smi",
                gpu=GpuProbe(
                    name="TEST GPU",
                    vram_total_bytes=int(vram_gb * GIB),
                    compute_capability=capability,
                ),
                ram_total_bytes=int(ram_gb * GIB),
            )

        monkeypatch.setattr(main, "probe_hardware", probe)
        main._recommended_cache.clear()

    return install
