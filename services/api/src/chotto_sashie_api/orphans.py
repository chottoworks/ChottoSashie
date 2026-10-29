"""どの記録からも辿れなくなったファイルを、保存先から片付ける。

画像・動画は記録（作品・人物・静止画・カット…）の持ち物で、記録を消すときに一緒に
消している。ただ、記録を消してからファイルを消すまでの間にアプリが落ちたり、
ファイルが他のプログラムに掴まれていて消せなかったりすると、行き先の無いファイルが
残る。利用者からは見えず、二度と消す手段も無いので、起動のたびにここで片付ける。

見るのは、このアプリが記録ごとのファイルだけを置くフォルダに限る。どのフォルダも
記録のIDから名前を作っているので、記録が持っている場所の**ファイル名**と突き合わせる
（保存先を移したあとでも、記録の中の場所がずれていても取り違えない）。
"""

from __future__ import annotations

from pathlib import Path

from chotto_sashie_shared.local_images import CANDIDATE_IMAGES, local_image_path

from .character_figures import figures_dir
from .character_portraits import portraits_dir
from .database import Database
from .exporting import export_root, project_export_path
from .storage import (
    asset_dir,
    shot_frame_dir,
    shot_last_frame_dir,
    shot_segment_dir,
    shot_video_dir,
    still_image_dir,
    still_image_pose_dir,
)

# 記録が持っている、保存先の中のファイルの場所。start_frame_image_path は他の記録の
# ファイルを指す覚え書きだが、指している間は消さないよう、ここにも含める。
_PATH_COLUMNS: tuple[tuple[str, str], ...] = (
    ("background_assets", "image_path"),
    ("prop_assets", "image_path"),
    ("character_figures", "image_path"),
    ("character_figure_edits", "image_path"),
    ("characters", "portrait_image_path"),
    ("still_images", "image_path"),
    ("still_image_characters", "pose_image_path"),
    ("shots", "output_video_path"),
    ("shots", "last_frame_image_path"),
    ("shots", "custom_start_frame_path"),
    ("shots", "custom_end_frame_path"),
    ("shots", "start_frame_image_path"),
    ("shot_beats", "segment_video_path"),
    ("shot_beats", "last_frame_image_path"),
)


def managed_folders(data_dir: Path) -> list[Path]:
    """記録ごとのファイルだけを置くフォルダ。ほかの用途のファイルは置かない。"""
    return [
        asset_dir(data_dir, "background_assets"),
        asset_dir(data_dir, "prop_assets"),
        figures_dir(data_dir),
        portraits_dir(data_dir),
        local_image_path(data_dir, CANDIDATE_IMAGES, "_").parent,
        still_image_dir(data_dir),
        still_image_pose_dir(data_dir),
        shot_video_dir(data_dir),
        shot_last_frame_dir(data_dir),
        shot_frame_dir(data_dir),
        shot_segment_dir(data_dir),
        export_root(data_dir),
    ]


def referenced_names(database: Database, data_dir: Path) -> set[str]:
    """記録から辿れるファイルの名前。"""
    names: set[str] = set()
    with database.connect() as connection:
        for table, column in _PATH_COLUMNS:
            rows = connection.execute(
                f"SELECT {column} AS path FROM {table} WHERE {column} != ''"  # noqa: S608 - 定数表から
            ).fetchall()
            names.update(Path(row["path"]).name for row in rows)
        # 人物候補の控えと書き出した動画は、場所を記録に書かずIDから決めている。
        names.update(
            local_image_path(data_dir, CANDIDATE_IMAGES, row["id"]).name
            for row in connection.execute("SELECT id FROM character_candidates").fetchall()
        )
        names.update(
            project_export_path(data_dir, row["id"]).name
            for row in connection.execute("SELECT id FROM projects").fetchall()
        )
    return names


def remove_unreferenced_files(database: Database, data_dir: Path) -> int:
    """どの記録からも辿れないファイルを消し、消した数を返す。

    生成が1件も動いていないとき（起動時）にだけ呼ぶ。生成中は、ファイルを書いてから
    記録へ結び付けるまでの間があり、その途中のものを消してしまうため。
    """
    keep = referenced_names(database, data_dir)
    removed = 0
    for folder in managed_folders(data_dir):
        if not folder.is_dir():
            continue
        for path in folder.iterdir():
            # 中にフォルダは作らない。あっても自分の置いたものではないので触らない。
            if not path.is_file() or path.name in keep:
                continue
            try:
                path.unlink()
            except OSError:
                # 掴まれていて消せないものは、次の起動で消す。
                continue
            removed += 1
    return removed
