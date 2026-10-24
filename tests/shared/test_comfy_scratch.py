"""専用ComfyUIの入力・出力フォルダに残る写しの片付けを確かめる。"""

from pathlib import Path

from chotto_sashie_shared.comfy_scratch import ComfyScratch


def _scratch(tmp_path: Path) -> ComfyScratch:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    return ComfyScratch(input_dir, output_dir)


def _write(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


def test_discard_outputs_removes_the_files_and_empty_folders(tmp_path: Path) -> None:
    scratch = _scratch(tmp_path)
    produced = _write(tmp_path / "output" / "ChottoSashie" / "still_images" / "a_00001_.png")
    neighbour = _write(tmp_path / "output" / "ChottoSashie" / "other.png")

    scratch.discard_outputs(
        [{"filename": "a_00001_.png", "subfolder": "ChottoSashie/still_images", "type": "output"}]
    )

    assert not produced.exists()
    # 空になった入れ物は畳むが、まだ中身のあるフォルダと出力フォルダ自体は残す。
    assert not produced.parent.exists()
    assert neighbour.exists()
    assert (tmp_path / "output").is_dir()


def test_discard_outputs_leaves_temporary_outputs_to_comfyui(tmp_path: Path) -> None:
    scratch = _scratch(tmp_path)
    produced = _write(tmp_path / "output" / "preview.png")

    scratch.discard_outputs([{"filename": "preview.png", "subfolder": "", "type": "temp"}])

    assert produced.exists()


def test_discard_inputs_removes_what_was_sent(tmp_path: Path) -> None:
    scratch = _scratch(tmp_path)
    sent = _write(tmp_path / "input" / "job-1.png")
    nested = _write(tmp_path / "input" / "frames" / "job-1-start.png")

    scratch.discard_inputs(["job-1.png", "frames/job-1-start.png"])

    assert not sent.exists()
    assert not nested.exists()
    assert not nested.parent.exists()


def test_nothing_outside_the_folders_is_touched(tmp_path: Path) -> None:
    scratch = _scratch(tmp_path)
    outside = _write(tmp_path / "keep.png")

    scratch.discard_inputs(["../keep.png"])
    scratch.discard_outputs([{"filename": "keep.png", "subfolder": "..", "type": "output"}])

    assert outside.exists()


def test_without_folders_nothing_is_removed(tmp_path: Path) -> None:
    """既存ComfyUI向け（フォルダを持たない）は、何も消さない。"""
    produced = _write(tmp_path / "output" / "a.png")
    scratch = ComfyScratch()

    scratch.discard_outputs([{"filename": "a.png", "subfolder": "", "type": "output"}])
    scratch.discard_inputs(["a.png"])
    scratch.sweep()

    assert not scratch.enabled
    assert produced.exists()


def test_sweep_empties_both_folders(tmp_path: Path) -> None:
    scratch = _scratch(tmp_path)
    _write(tmp_path / "input" / "left-over.png")
    _write(tmp_path / "output" / "ChottoSashie" / "x" / "left-over.png")

    scratch.sweep()

    assert list((tmp_path / "input").iterdir()) == []
    assert list((tmp_path / "output").iterdir()) == []
