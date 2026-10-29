"""保存先の中で、記録ごとの画像・動画を置くフォルダ。

どこに何を置くかをここ1か所で決める。書き込む側・読み出す側・片付ける側
（`orphans`）が同じ場所を見ないと、片付けが別の場所を探して空振りしたり、
逆に置いた覚えのない場所を消したりするため。
"""

from __future__ import annotations

from pathlib import Path


def asset_dir(data_dir: Path, table: str) -> Path:
    """背景（background_assets）・小物（prop_assets）の画像。"""
    return data_dir / "assets" / table


def still_image_dir(data_dir: Path) -> Path:
    """合成した静止画。"""
    return data_dir / "keyframes"


def still_image_pose_dir(data_dir: Path) -> Path:
    """置いて合成するときの、静止画ごとの人物の姿（切り抜いた透過PNG）。"""
    return data_dir / "still-image-poses"


def shot_video_dir(data_dir: Path) -> Path:
    """カットの動画。"""
    return data_dir / "videos"


def shot_last_frame_dir(data_dir: Path) -> Path:
    """カット・ビートの最終フレーム（次の起点に使う）。"""
    return data_dir / "shot-last-frames"


def shot_frame_dir(data_dir: Path) -> Path:
    """構図変更で作った起点・終端のフレーム。"""
    return data_dir / "shot-frames"


def shot_segment_dir(data_dir: Path) -> Path:
    """分割モードで作ったビートごとの区間動画。"""
    return data_dir / "shot-segments"
