"""削除は、アプリ管理領域の内側だけに限られることを確かめる。

外部フォルダ（利用者の既存ComfyUIやモデル置き場）は読み取り専用で扱うため、
どんな経路を渡されてもそこへは触れない。
"""

from pathlib import Path

from chotto_sashie_shared.deletion import remove_within


def test_remove_within_never_touches_outside_paths(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    managed.mkdir()
    outside = tmp_path / "existing-comfyui"
    outside.mkdir()
    (outside / "model.safetensors").write_bytes(b"keep")
    inside = managed / "training"
    inside.mkdir()

    assert remove_within(managed, outside) is False
    # 管理領域そのものは消さない（中身だけを対象にする）。
    assert remove_within(managed, managed) is False
    assert remove_within(managed, inside) is True
    assert (outside / "model.safetensors").exists()
    assert not inside.exists()
