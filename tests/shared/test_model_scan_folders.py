from pathlib import Path

from chotto_sashie_shared.model_scan import inspect_comfyui_folder, scan_model_roots


def test_scan_detects_categories_duplicates_and_partials(tmp_path: Path) -> None:
    first = tmp_path / "first" / "models" / "loras"
    second = tmp_path / "second" / "models" / "loras"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    (first / "character.safetensors").write_bytes(b"model")
    (second / "character.safetensors").write_bytes(b"model")
    (first / "unfinished.part").write_bytes(b"partial")

    result = scan_model_roots(
        [tmp_path / "first", tmp_path / "second"],
        calculate_sha256=False,
        max_files=10,
    )

    assert len(result.files) == 3
    assert all(item.category == "loras" for item in result.files)
    assert len(result.duplicate_groups) == 1
    assert any(item.incomplete for item in result.files)


def test_comfy_folder_is_inspected_without_writes(tmp_path: Path) -> None:
    comfy = tmp_path / "ComfyUI"
    (comfy / "models").mkdir(parents=True)
    (comfy / "main.py").write_text("", encoding="utf-8")

    result = inspect_comfyui_folder(comfy)

    assert result["valid"] is True
    assert sorted(path.name for path in comfy.iterdir()) == ["main.py", "models"]
