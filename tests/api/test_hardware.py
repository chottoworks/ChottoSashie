"""この端末の構成調べと、それに合う既定値の提案。

実機のGPUに結果が左右されないよう、構成は差し替えて確かめる。
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chotto_sashie_shared.hardware import GIB, GpuProbe, HardwareProbe, probe_from_system_stats
from chotto_sashie_shared.jobs import JobRepository
from chotto_sashie_api import main
from chotto_sashie_api.database import Database


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    database = Database(tmp_path / "chotto-sashie.sqlite3")
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(main, "jobs", JobRepository(database))
    with TestClient(main.app) as test_client:
        yield test_client


def _install_probe(
    monkeypatch: pytest.MonkeyPatch,
    *,
    vram_gb: float,
    ram_gb: float = 64,
    capability: str = "12.0",
    detected: bool = True,
) -> None:
    async def fake_probe(base_url: str | None = None) -> HardwareProbe:
        return HardwareProbe(
            detected=detected,
            source="nvidia-smi" if detected else "none",
            gpu=GpuProbe(
                name="TEST GPU",
                vram_total_bytes=int(vram_gb * GIB),
                compute_capability=capability,
                driver_version="999.99",
            ),
            ram_total_bytes=int(ram_gb * GIB),
            message="" if detected else "GPUを確認できませんでした",
        )

    monkeypatch.setattr(main, "probe_hardware", fake_probe)


def _setting(body: dict, key: str) -> dict:
    return next(item for item in body["recommendation"]["settings"] if item["key"] == key)


def test_reports_gpu_and_recommends_for_large_vram(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_probe(monkeypatch, vram_gb=32)
    body = client.get("/api/v1/setup/hardware").json()

    assert body["detected"] is True
    assert body["gpu_name"] == "TEST GPU"
    assert body["recommendation"]["tier"] == "high"
    # VRAMも世代も足りているので、音声付きと丁寧な合成を勧める。
    assert _setting(body, "video_engine")["value"] == "minimax_h3"
    assert _setting(body, "composition_quality")["value"] == "careful"
    # まだ適用していないので「おすすめのまま」ではない。
    assert body["applied"] is False


def test_old_generation_does_not_get_minimax_h3(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """VRAMは足りていても、nvfp4が動かない世代にH3は勧めない。"""
    _install_probe(monkeypatch, vram_gb=48, capability="8.9")
    body = client.get("/api/v1/setup/hardware").json()

    engine = _setting(body, "video_engine")
    assert engine["value"] == "wan22"
    # 下げた理由を必ず添える（黙って別のものを勧めない）。
    assert engine["notes"]


def test_unknown_generation_falls_back_with_reason(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_probe(monkeypatch, vram_gb=32, capability="")
    engine = _setting(client.get("/api/v1/setup/hardware").json(), "video_engine")

    assert engine["value"] == "wan22"
    assert any("世代を確認できなかった" in note for note in engine["notes"])


def test_small_vram_gets_light_settings_and_warnings(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_probe(monkeypatch, vram_gb=8, ram_gb=16)
    body = client.get("/api/v1/setup/hardware").json()

    assert body["recommendation"]["tier"] == "minimum"
    assert _setting(body, "composition_quality")["value"] == "fast"
    assert _setting(body, "candidate_count")["value"] == "8"
    # 合成モデルが収まらないこと・RAMが細いことは、先に伝える。
    assert len(body["recommendation"]["warnings"]) >= 2


def test_video_only_warning_hidden_when_video_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_probe(monkeypatch, vram_gb=24, ram_gb=32)
    with_video = client.get("/api/v1/setup/hardware").json()["recommendation"]["warnings"]

    client.put("/api/v1/setup/video-enabled", json={"enabled": False})
    without_video = client.get("/api/v1/setup/hardware").json()["recommendation"]["warnings"]

    assert len(without_video) < len(with_video)


def test_nominal_24gb_card_counts_as_24gb(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """製品表記24GBのカードは実測23.99GiB。しきい値の直下で落とさない。"""
    _install_probe(monkeypatch, vram_gb=23.99)
    assert client.get("/api/v1/setup/hardware").json()["recommendation"]["tier"] == "high"


def test_apply_stores_recommendation_as_defaults(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_probe(monkeypatch, vram_gb=32)
    applied = client.post("/api/v1/setup/hardware/apply")
    assert applied.status_code == 200
    assert applied.json()["video_engine"] == "minimax_h3"
    assert applied.json()["recommendation_tier"] == "high"

    # 覚えた値が次回の既定になる。
    assert client.get("/api/v1/preferences").json()["video_engine"] == "minimax_h3"
    assert client.get("/api/v1/setup/hardware").json()["applied"] is True


def test_manual_change_makes_report_not_applied(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """おすすめを適用した後に選び直したら、その選択が残る（勝手に戻さない）。"""
    _install_probe(monkeypatch, vram_gb=32)
    client.post("/api/v1/setup/hardware/apply")
    client.put("/api/v1/preferences", json={"video_engine": "wan22"})

    body = client.get("/api/v1/setup/hardware").json()
    assert body["applied"] is False
    assert client.get("/api/v1/preferences").json()["video_engine"] == "wan22"


def test_undetected_hardware_recommends_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_probe(monkeypatch, vram_gb=0, detected=False)
    body = client.get("/api/v1/setup/hardware").json()

    assert body["detected"] is False
    assert body["recommendation"]["tier"] == "unknown"
    assert body["recommendation"]["settings"] == []
    # 当てずっぽうの既定を書き込ませない。
    assert client.post("/api/v1/setup/hardware/apply").status_code == 409


def test_system_stats_parsed_from_comfyui() -> None:
    probe = probe_from_system_stats(
        {
            "system": {"ram_total": 68_719_476_736},
            "devices": [
                {
                    "name": "cuda:0 NVIDIA GeForce RTX 5090 : cudaMallocAsync",
                    "vram_total": 34_359_738_368,
                    "vram_free": 30_000_000_000,
                }
            ],
        }
    )

    assert probe is not None
    assert probe.gpu.name == "NVIDIA GeForce RTX 5090"
    assert probe.gpu.vram_total_bytes == 34_359_738_368
    assert probe.gpu.vram_free_bytes == 30_000_000_000
    assert probe.ram_total_bytes == 68_719_476_736


def test_system_stats_without_devices_is_ignored() -> None:
    assert probe_from_system_stats({"system": {}, "devices": []}) is None


def test_requirements_report_whether_this_machine_qualifies(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """動作要件に届いているかを、提案とは別に返す。

    数十GBを取得したあとで「1枚に数分かかる」と気づくのは遅い。取得の手前で
    伝えられるよう、判定を画面から読めるところへ出す。
    """
    _install_probe(monkeypatch, vram_gb=32, ram_gb=64)
    met = client.get("/api/v1/setup/hardware").json()["recommendation"]["requirements"]
    assert met["known"] is True
    assert met["level"] == "met"
    assert met["message"] == ""
    # 要件そのものは、満たしていても画面へ出せる。
    assert "12GB" in met["minimum_label"]

    _install_probe(monkeypatch, vram_gb=12, ram_gb=32)
    light = client.get("/api/v1/setup/hardware").json()["recommendation"]["requirements"]
    assert light["meets_minimum"] is True
    assert light["meets_recommended"] is False
    assert light["level"] == "below_recommended"

    _install_probe(monkeypatch, vram_gb=8, ram_gb=32)
    small = client.get("/api/v1/setup/hardware").json()["recommendation"]["requirements"]
    assert small["meets_minimum"] is False
    assert small["level"] == "below_minimum"
    assert "動作要件" in small["message"]


def test_requirements_also_look_at_memory(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """VRAMが足りていても、メモリが細ければ要件未満とする。

    大きなモデルはいったんホストRAMを通るため、そこで詰まる。
    """
    _install_probe(monkeypatch, vram_gb=16, ram_gb=16)
    body = client.get("/api/v1/setup/hardware").json()["recommendation"]["requirements"]

    assert body["meets_minimum"] is False


def test_requirements_do_not_judge_when_memory_is_unknown(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """メモリを読めなかったことを理由に、要件未満とはしない。"""
    _install_probe(monkeypatch, vram_gb=16, ram_gb=0)
    body = client.get("/api/v1/setup/hardware").json()["recommendation"]["requirements"]

    assert body["meets_minimum"] is True


def test_requirements_stay_unknown_when_hardware_is_not_detected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """構成を読めなかったときは、満たしていない扱いにしない（当てずっぽうで止めない）。"""
    _install_probe(monkeypatch, vram_gb=0, detected=False)
    body = client.get("/api/v1/setup/hardware").json()["recommendation"]["requirements"]

    assert body["known"] is False
    assert body["level"] == "unknown"
    # 判定できなくても、要件そのものは読めるようにしておく。
    assert body["minimum_label"]
