"""調べた構成から「この端末に合う既定値」を決める（共通の土台）。

判断の材料は `hardware.HardwareProbe`、判断の根拠は
`model-manifests/hardware-profiles.json` に置く。しきい値も文言もマニフェスト側にある
ので、モデル構成が変わったときはJSONだけを直せばよい。

決め方は2段階:

1. VRAMの多い順に並んだ tier から、最初に条件を満たしたものを選ぶ
2. tier が指す選択肢が、その項目固有の条件（VRAM・ホストRAM・GPUの世代）を満たすか
   確かめる。満たさなければ ``fallback`` をたどり、下げた理由を控えておく

「VRAMは足りているが世代が古いのでH3は勧めない」のような判断は、tier だけでは表せない
ため、選択肢ごとの条件で受ける。

ここで返すのはあくまで**既定値の提案**で、利用者の選択を上書きはしない。適用するかどうか
は画面側（＝利用者）が決める。
"""

from __future__ import annotations

from pydantic import BaseModel

from .hardware import GIB, HardwareProbe, compute_capability_value

# 製品の表記（"24GB" / "64GB"）と実測値（23.99GiB / 63.5GiB）のずれを吸収する。
# ホストRAMは、ハードウェア予約ぶんが引かれて1GB近く少なく見えることがある。
_TOLERANCE_GB = 1.0


class RecommendedSetting(BaseModel):
    """1項目ぶんのおすすめ。"""

    key: str
    display_name: str
    # 保存する値。数値の項目も文字列で持つ（保存側が型を決める）。
    value: str
    value_label: str
    reason: str
    # 条件を満たさず下げたとき、その理由。無ければ空。
    notes: list[str] = []


class RequirementCheck(BaseModel):
    """動作要件を満たしているか。

    下限を割っていても動かないわけではないので、止めはしない。ただ、数十GBを取得して
    から「1枚に数分かかる」と気づくのは遅すぎるので、先に伝えられるようにする。
    """

    # 判定できたか（構成を読めなかったときは False）。
    known: bool = False
    # met | below_recommended | below_minimum | unknown
    level: str = "unknown"
    meets_minimum: bool = True
    meets_recommended: bool = True
    # 画面に出す一言（満たしているときは空）。
    message: str = ""
    # 要件そのもの（画面へそのまま出せる形で持つ）。
    minimum_label: str = ""
    recommended_label: str = ""


class HardwareRecommendation(BaseModel):
    """この端末に対する提案ひとまとめ。"""

    # high | standard | light | minimum | unknown
    tier: str
    tier_label: str
    summary: str
    settings: list[RecommendedSetting] = []
    warnings: list[str] = []
    requirements: RequirementCheck = RequirementCheck()


def _gib(value: int) -> float:
    return value / GIB


def _select_tier(manifest: dict, vram_gb: float) -> dict | None:
    tiers = [tier for tier in manifest.get("tiers", []) if isinstance(tier, dict)]
    for tier in sorted(tiers, key=lambda item: float(item.get("min_vram_gb", 0)), reverse=True):
        if vram_gb + _TOLERANCE_GB >= float(tier.get("min_vram_gb", 0)):
            return tier
    return None


def _unmet_reason(
    option: dict,
    *,
    vram_gb: float,
    ram_gb: float,
    capability: float,
) -> str | None:
    """条件を満たさない理由を返す。満たしていれば None。"""
    minimum_vram = option.get("min_vram_gb")
    if minimum_vram is not None and vram_gb + _TOLERANCE_GB < float(minimum_vram):
        return "vram"
    minimum_ram = option.get("min_ram_gb")
    # RAMを読めなかった（0）ときは、読めないことを理由に下げない。
    if minimum_ram is not None and ram_gb > 0 and ram_gb + _TOLERANCE_GB < float(minimum_ram):
        return "ram"
    minimum_capability = option.get("min_compute_capability")
    if minimum_capability is not None:
        if capability <= 0:
            return "capability_unknown"
        if capability < float(minimum_capability):
            return "capability"
    return None


def _resolve_option(
    spec: dict,
    start_value: str,
    *,
    vram_gb: float,
    ram_gb: float,
    capability: float,
) -> tuple[dict, list[str]]:
    """条件を満たす選択肢まで ``fallback`` をたどる。降りた理由も返す。"""
    options = {
        str(option.get("value")): option
        for option in spec.get("options", [])
        if isinstance(option, dict)
    }
    notes: list[str] = []
    current = options.get(start_value)
    # tier が指す値がマニフェストに無い（＝書き間違い）ときは、先頭の選択肢へ落とす。
    if current is None:
        current = next(iter(options.values()), {"value": start_value})
    seen: set[str] = set()
    while True:
        value = str(current.get("value", ""))
        if value in seen:
            # 循環した fallback は、そこで止めて今の選択肢を採る。
            return current, notes
        seen.add(value)
        reason = _unmet_reason(current, vram_gb=vram_gb, ram_gb=ram_gb, capability=capability)
        if reason is None:
            return current, notes
        note = str(current.get("unmet_note_ja", ""))
        if note:
            notes.append(note)
        if reason == "capability_unknown":
            notes.append("GPUの世代を確認できなかったため、確実に動く方を既定にしました。")
        fallback = current.get("fallback")
        if not fallback or str(fallback) not in options:
            return current, notes
        current = options[str(fallback)]


def _requirement_labels(manifest: dict) -> tuple[str, str]:
    spec = manifest.get("requirements", {})
    spec = spec if isinstance(spec, dict) else {}
    minimum = spec.get("minimum", {}) if isinstance(spec.get("minimum"), dict) else {}
    recommended = spec.get("recommended", {}) if isinstance(spec.get("recommended"), dict) else {}
    return str(minimum.get("summary_ja", "")), str(recommended.get("summary_ja", ""))


def check_requirements(
    manifest: dict, *, vram_gb: float, ram_gb: float, known: bool
) -> RequirementCheck:
    """動作要件に届いているかを見る。

    メモリを読めなかった（0）ときは、読めないことを理由に下げない。VRAMだけで判断する。
    """
    minimum_label, recommended_label = _requirement_labels(manifest)
    if not known:
        return RequirementCheck(
            minimum_label=minimum_label, recommended_label=recommended_label
        )
    spec = manifest.get("requirements", {})
    spec = spec if isinstance(spec, dict) else {}

    def _meets(level: str) -> bool:
        values = spec.get(level, {})
        values = values if isinstance(values, dict) else {}
        needed_vram = float(values.get("vram_gb", 0))
        needed_ram = float(values.get("ram_gb", 0))
        if vram_gb + _TOLERANCE_GB < needed_vram:
            return False
        return not (ram_gb > 0 and ram_gb + _TOLERANCE_GB < needed_ram)

    meets_minimum = _meets("minimum")
    meets_recommended = meets_minimum and _meets("recommended")
    if not meets_minimum:
        level, message = "below_minimum", str(spec.get("below_minimum_ja", ""))
    elif not meets_recommended:
        level, message = "below_recommended", str(spec.get("below_recommended_ja", ""))
    else:
        level, message = "met", ""
    return RequirementCheck(
        known=True,
        level=level,
        meets_minimum=meets_minimum,
        meets_recommended=meets_recommended,
        message=message,
        minimum_label=minimum_label,
        recommended_label=recommended_label,
    )


def _warnings(manifest: dict, *, vram_gb: float, ram_gb: float, video_enabled: bool) -> list[str]:
    messages: list[str] = []
    for warning in manifest.get("warnings", []):
        if not isinstance(warning, dict):
            continue
        if warning.get("video_only") and not video_enabled:
            continue
        limit_vram = warning.get("max_vram_gb")
        if limit_vram is not None and vram_gb + _TOLERANCE_GB >= float(limit_vram):
            continue
        limit_ram = warning.get("max_ram_gb")
        if limit_ram is not None and (ram_gb <= 0 or ram_gb + _TOLERANCE_GB >= float(limit_ram)):
            continue
        if limit_vram is None and limit_ram is None:
            continue
        message = str(warning.get("message_ja", ""))
        if message:
            messages.append(message)
    return messages


def recommend(
    probe: HardwareProbe,
    manifest: dict,
    *,
    video_enabled: bool = True,
) -> HardwareRecommendation:
    """調べた構成に対する、既定値の提案を作る。

    構成が分からなかったときは何も勧めない（`tier="unknown"`）。当てずっぽうの既定を
    押し付けるより、利用者が自分で選ぶ方が確かなため。
    """
    if not probe.detected:
        return HardwareRecommendation(
            tier="unknown",
            tier_label="判定できません",
            summary=probe.message or "この端末の構成を確認できませんでした。設定は手動で選べます。",
            requirements=check_requirements(manifest, vram_gb=0, ram_gb=0, known=False),
        )

    vram_gb = _gib(probe.gpu.vram_total_bytes)
    ram_gb = _gib(probe.ram_total_bytes)
    capability = compute_capability_value(probe.gpu.compute_capability)

    tier = _select_tier(manifest, vram_gb)
    if tier is None:
        return HardwareRecommendation(
            tier="unknown",
            tier_label="判定できません",
            summary="おすすめの基準表が読めませんでした。設定は手動で選べます。",
            requirements=check_requirements(manifest, vram_gb=0, ram_gb=0, known=False),
        )

    tier_values = tier.get("settings", {})
    tier_values = tier_values if isinstance(tier_values, dict) else {}

    settings: list[RecommendedSetting] = []
    for spec in manifest.get("settings", []):
        if not isinstance(spec, dict):
            continue
        key = str(spec.get("key", ""))
        if not key or key not in tier_values:
            continue
        option, notes = _resolve_option(
            spec,
            str(tier_values[key]),
            vram_gb=vram_gb,
            ram_gb=ram_gb,
            capability=capability,
        )
        extra = str(option.get("extra_note_ja", ""))
        if extra:
            notes = [*notes, extra]
        settings.append(
            RecommendedSetting(
                key=key,
                display_name=str(spec.get("display_name_ja", key)),
                value=str(option.get("value", "")),
                value_label=str(option.get("label_ja", option.get("value", ""))),
                reason=str(option.get("reason_ja", "")),
                notes=notes,
            )
        )

    return HardwareRecommendation(
        tier=str(tier.get("id", "")),
        tier_label=str(tier.get("label_ja", "")),
        summary=str(tier.get("summary_ja", "")),
        settings=settings,
        warnings=_warnings(
            manifest, vram_gb=vram_gb, ram_gb=ram_gb, video_enabled=video_enabled
        ),
        requirements=check_requirements(
            manifest, vram_gb=vram_gb, ram_gb=ram_gb, known=True
        ),
    )
