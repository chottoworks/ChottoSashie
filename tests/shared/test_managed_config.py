from pathlib import Path

import pytest

from chotto_sashie_shared.managed_config import (
    ManagedConfigError,
    build_extra_model_paths_plan,
    write_extra_model_paths,
)


def make_external_root(tmp_path: Path) -> Path:
    root = tmp_path / "external-models"
    for name in ["Checkpoints", "diffusion_models", "text_encoders", "CLIP", "VAE", "Unknown"]:
        (root / name).mkdir(parents=True)
    return root


def test_preview_maps_existing_directories_without_writes(tmp_path: Path) -> None:
    external_root = make_external_root(tmp_path)
    managed_root = tmp_path / "managed"

    plan = build_extra_model_paths_plan(managed_root, [external_root])

    assert plan["destination"] == str(
        (managed_root / "runtime" / "comfy" / "extra_model_paths.yaml").resolve()
    )
    assert plan["mapped_categories"]["checkpoints"] == [str(external_root / "Checkpoints")]
    assert plan["mapped_categories"]["text_encoders"] == [
        str(external_root / "text_encoders"),
        str(external_root / "CLIP"),
    ]
    assert plan["ignored_directories"][str(external_root.resolve())] == ["Unknown"]
    assert "is_default: false" in plan["content"]
    assert "chotto_sashie_managed:" in plan["content"]
    assert f'base_path: "{str((managed_root / "models").resolve()).replace(chr(92), chr(92) * 2)}"' in plan["content"]
    assert "  loras: loras" in plan["content"]
    assert not managed_root.exists()


def test_apply_writes_only_managed_root_and_is_idempotent(tmp_path: Path) -> None:
    external_root = make_external_root(tmp_path)
    managed_root = tmp_path / "managed"
    before = {path: path.stat().st_mtime_ns for path in external_root.iterdir()}
    plan = build_extra_model_paths_plan(managed_root, [external_root])

    applied, already_current = write_extra_model_paths(plan)
    applied_again, already_current_again = write_extra_model_paths(plan)

    destination = Path(str(plan["destination"]))
    assert applied is True
    assert already_current is False
    assert applied_again is False
    assert already_current_again is True
    assert destination.read_text(encoding="utf-8") == plan["content"]
    assert {path: path.stat().st_mtime_ns for path in external_root.iterdir()} == before


def test_managed_root_inside_external_root_is_rejected(tmp_path: Path) -> None:
    external_root = make_external_root(tmp_path)

    with pytest.raises(ManagedConfigError, match="内側"):
        build_extra_model_paths_plan(external_root / "managed", [external_root])


def test_different_existing_config_is_not_overwritten(tmp_path: Path) -> None:
    external_root = make_external_root(tmp_path)
    plan = build_extra_model_paths_plan(tmp_path / "managed", [external_root])
    write_extra_model_paths(plan)
    destination = Path(str(plan["destination"]))
    destination.write_text("user configuration\n", encoding="utf-8")

    with pytest.raises(ManagedConfigError, match="上書き"):
        write_extra_model_paths(plan)

    assert destination.read_text(encoding="utf-8") == "user configuration\n"
