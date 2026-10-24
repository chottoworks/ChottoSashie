"""この端末の構成調べと、それに合う既定値の提案。

実体は chotto_sashie_shared.hardware / chotto_sashie_shared.recommendations（共通の土台）。
"""

from __future__ import annotations

from chotto_sashie_shared.hardware import (
    HardwareProbe,
    load_profiles,
    probe_hardware,
)
from chotto_sashie_shared.recommendations import (
    HardwareRecommendation,
    RecommendedSetting,
    recommend,
)

__all__ = [
    "HardwareProbe",
    "HardwareRecommendation",
    "RecommendedSetting",
    "load_profiles",
    "probe_hardware",
    "recommend",
]
