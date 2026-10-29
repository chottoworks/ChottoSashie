"""削除の内訳集計と、静止画が持つ参照の掃除。

`still_images.character_ids_json` はJSON配列なので外部キーが効かない。人物を消した
ままにすると、静止画には消えたIDが残り、合成時に「人物が見つかりません」で落ちる。
ここで削除と同時に取り除く。

削除するのはアプリ管理データ保存先の内側だけで、参照中のモデルフォルダや既存ComfyUIには触れない。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from chotto_sashie_shared.deletion import remove_within
from chotto_sashie_shared.local_images import CANDIDATE_IMAGES, delete_local_image

from .character_portraits import delete_portrait, portraits_dir
from .database import Database
from .exporting import project_export_path

# 静止画が人物を持つ列。("対象の種別", "列名") の対応。
# 小物はここに無い——合成へ渡さなくなったので、静止画は小物を参照しない。
_SCENE_LIST_COLUMNS = {
    "character": "character_ids_json",
}


@dataclass(frozen=True)
class StillImageUsage:
    """この対象を参照している静止画。削除確認に「3つの静止画から外れます」と出す。"""

    still_image_count: int = 0
    still_image_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class CharacterDeletionSummary:
    character_id: str
    name: str
    usage: StillImageUsage = field(default_factory=StillImageUsage)
    candidate_count: int = 0
    figure_count: int = 0


@dataclass(frozen=True)
class AssetDeletionSummary:
    asset_id: str
    name: str
    usage: StillImageUsage = field(default_factory=StillImageUsage)


@dataclass(frozen=True)
class StillImageDeletionSummary:
    still_image_id: str
    name: str
    ordinal: int
    shot_count: int
    video_count: int
    has_image: bool


@dataclass(frozen=True)
class ShotDeletionSummary:
    shot_id: str
    ordinal: int
    has_video: bool
    # ビートと、そのうち区間動画を持つ数。
    beat_count: int = 0
    beat_video_count: int = 0


@dataclass(frozen=True)
class ProjectDeletionSummary:
    project_id: str
    name: str
    character_count: int
    background_count: int
    prop_count: int
    still_image_count: int
    shot_count: int
    video_count: int


def _still_image_label(name: str, ordinal: int) -> str:
    return name or f"#{ordinal}"


def still_image_usage(database: Database, kind: str, target_id: str) -> StillImageUsage:
    """対象を参照している静止画を数える。いま数えられるのは人物だけ。"""
    column = _SCENE_LIST_COLUMNS[kind]
    with database.connect() as connection:
        rows = connection.execute(
            f"SELECT name, ordinal, {column} AS ids FROM still_images ORDER BY project_id, ordinal"  # noqa: S608
        ).fetchall()
    names = [
        _still_image_label(row["name"], row["ordinal"])
        for row in rows
        if target_id in json.loads(row["ids"])
    ]
    return StillImageUsage(still_image_count=len(names), still_image_names=tuple(names))


def detach_from_still_images(database: Database, kind: str, target_id: str) -> int:
    """すべての静止画の参照リストから対象IDを取り除く。取り除いた静止画の数を返す。"""
    column = _SCENE_LIST_COLUMNS[kind]
    detached = 0
    with database.connect() as connection:
        rows = connection.execute(
            f"SELECT id, {column} AS ids FROM still_images"  # noqa: S608
        ).fetchall()
        for row in rows:
            ids = json.loads(row["ids"])
            if target_id not in ids:
                continue
            remaining = [value for value in ids if value != target_id]
            connection.execute(
                f"UPDATE still_images SET {column} = ? WHERE id = ?",  # noqa: S608
                (json.dumps(remaining, ensure_ascii=False), row["id"]),
            )
            detached += 1
    return detached


def _count(database: Database, sql: str, parameter: str) -> int:
    with database.connect() as connection:
        row = connection.execute(sql, (parameter,)).fetchone()
    return int(row[0]) if row is not None else 0


def summarize_character_deletion(
    database: Database, character_id: str
) -> CharacterDeletionSummary:
    with database.connect() as connection:
        row = connection.execute(
            "SELECT name FROM characters WHERE id = ?", (character_id,)
        ).fetchone()
    if row is None:
        raise KeyError(character_id)
    return CharacterDeletionSummary(
        character_id=character_id,
        name=row["name"],
        usage=still_image_usage(database, "character", character_id),
        candidate_count=_count(
            database,
            "SELECT COUNT(*) FROM character_candidates WHERE character_id = ?",
            character_id,
        ),
        figure_count=_count(
            database,
            "SELECT COUNT(*) FROM character_figures WHERE character_id = ?",
            character_id,
        ),
    )


def summarize_asset_deletion(database: Database, table: str, asset_id: str) -> AssetDeletionSummary:
    with database.connect() as connection:
        row = connection.execute(
            f"SELECT name FROM {table} WHERE id = ?", (asset_id,)  # noqa: S608
        ).fetchone()
    if row is None:
        raise KeyError(asset_id)
    kind = "prop" if table == "prop_assets" else "background"
    # 小物は静止画に載らない（立ち絵の変更と背景のバリエーションの入力にしか使わない）ので、外れる静止画も無い。
    usage = StillImageUsage() if kind == "prop" else _background_usage(
        database, asset_id
    )
    return AssetDeletionSummary(asset_id=asset_id, name=row["name"], usage=usage)


def _background_usage(database: Database, asset_id: str) -> StillImageUsage:
    """背景は外部キー（ON DELETE SET NULL）で外れるが、件数は確認に出す。"""
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT name, ordinal FROM still_images WHERE background_asset_id = ? ORDER BY ordinal",
            (asset_id,),
        ).fetchall()
    names = tuple(_still_image_label(row["name"], row["ordinal"]) for row in rows)
    return StillImageUsage(still_image_count=len(names), still_image_names=names)


def summarize_still_image_deletion(database: Database, still_image_id: str) -> StillImageDeletionSummary:
    with database.connect() as connection:
        still_image = connection.execute(
            "SELECT name, ordinal, image_path FROM still_images WHERE id = ?", (still_image_id,)
        ).fetchone()
        if still_image is None:
            raise KeyError(still_image_id)
        shots = connection.execute(
            "SELECT output_video_path FROM shots WHERE still_image_id = ?", (still_image_id,)
        ).fetchall()
    return StillImageDeletionSummary(
        still_image_id=still_image_id,
        name=still_image["name"],
        ordinal=still_image["ordinal"],
        shot_count=len(shots),
        video_count=sum(1 for row in shots if row["output_video_path"]),
        has_image=bool(still_image["image_path"]),
    )


def summarize_shot_deletion(database: Database, shot_id: str) -> ShotDeletionSummary:
    with database.connect() as connection:
        row = connection.execute(
            "SELECT ordinal, output_video_path FROM shots WHERE id = ?", (shot_id,)
        ).fetchone()
        if row is None:
            raise KeyError(shot_id)
        beats = connection.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN segment_video_path != '' THEN 1 ELSE 0 END) AS with_video
            FROM shot_beats WHERE shot_id = ?
            """,
            (shot_id,),
        ).fetchone()
    return ShotDeletionSummary(
        shot_id=shot_id,
        ordinal=row["ordinal"],
        has_video=bool(row["output_video_path"]),
        beat_count=beats["total"] or 0,
        beat_video_count=beats["with_video"] or 0,
    )


def summarize_project_deletion(database: Database, project_id: str) -> ProjectDeletionSummary:
    with database.connect() as connection:
        project = connection.execute(
            "SELECT name FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if project is None:
            raise KeyError(project_id)
        shots = connection.execute(
            """
            SELECT s.output_video_path AS video
            FROM shots s JOIN still_images sc ON sc.id = s.still_image_id
            WHERE sc.project_id = ?
            """,
            (project_id,),
        ).fetchall()
    return ProjectDeletionSummary(
        project_id=project_id,
        name=project["name"],
        character_count=_count(
            database, "SELECT COUNT(*) FROM characters WHERE project_id = ?", project_id
        ),
        background_count=_count(
            database, "SELECT COUNT(*) FROM background_assets WHERE project_id = ?", project_id
        ),
        prop_count=_count(
            database, "SELECT COUNT(*) FROM prop_assets WHERE project_id = ?", project_id
        ),
        still_image_count=_count(
            database, "SELECT COUNT(*) FROM still_images WHERE project_id = ?", project_id
        ),
        shot_count=len(shots),
        video_count=sum(1 for row in shots if row["video"]),
    )


def delete_character(database: Database, data_dir: Path, character_id: str) -> None:
    """登場人物と、その候補・立ち絵を削除し、静止画の参照からも外す。"""
    with database.connect() as connection:
        found = connection.execute(
            "SELECT id FROM characters WHERE id = ?", (character_id,)
        ).fetchone()
        if found is None:
            raise KeyError(character_id)
        # 立ち絵の実体はこの記録の持ち物なので、人物ごと消すときにここで片付ける。
        # 静止画ごとの姿も、人物を消すと行が外部キーで消えるので一緒に片付ける。
        paths = [
            Path(row["path"])
            for row in connection.execute(
                """
                SELECT image_path AS path FROM character_figures WHERE character_id = ?
                UNION ALL
                SELECT image_path AS path FROM character_figure_edits WHERE character_id = ?
                UNION ALL
                SELECT pose_image_path AS path FROM still_image_characters WHERE character_id = ?
                """,
                (character_id, character_id, character_id),
            ).fetchall()
            if row["path"]
        ]
        records = [
            (CANDIDATE_IMAGES, row["id"])
            for row in connection.execute(
                "SELECT id FROM character_candidates WHERE character_id = ?", (character_id,)
            ).fetchall()
        ]
    detach_from_still_images(database, "character", character_id)
    with database.connect() as connection:
        connection.execute("DELETE FROM characters WHERE id = ?", (character_id,))
    for path in paths:
        remove_within(data_dir, path)
    for kind, record_id in records:
        delete_local_image(data_dir, kind, record_id)
    # 代表写真も片付ける。
    delete_portrait(portraits_dir(data_dir), character_id)


def delete_asset(database: Database, data_dir: Path, table: str, asset_id: str) -> None:
    """背景・小物を削除する。背景を使っていた静止画は外部キーで自動的に外れる。"""
    with database.connect() as connection:
        row = connection.execute(
            f"SELECT image_path FROM {table} WHERE id = ?", (asset_id,)  # noqa: S608
        ).fetchone()
        if row is None:
            raise KeyError(asset_id)
        image_path = row["image_path"]
    with database.connect() as connection:
        connection.execute(f"DELETE FROM {table} WHERE id = ?", (asset_id,))  # noqa: S608
    if image_path:
        remove_within(data_dir, Path(image_path))


def delete_still_image(database: Database, data_dir: Path, still_image_id: str) -> None:
    """静止画と、そこから作ったカットを削除する。画像と動画の実体も片付ける。"""
    with database.connect() as connection:
        still_image = connection.execute(
            "SELECT image_path FROM still_images WHERE id = ?", (still_image_id,)
        ).fetchone()
        if still_image is None:
            raise KeyError(still_image_id)
        paths = [Path(still_image["image_path"])] if still_image["image_path"] else []
        # 人物の姿は静止画ごとの記録の持ち物。行は外部キーで一緒に消えるので、実体だけ集める。
        paths.extend(
            Path(row["path"])
            for row in connection.execute(
                "SELECT pose_image_path AS path FROM still_image_characters "
                "WHERE still_image_id = ? AND pose_image_path != ''",
                (still_image_id,),
            ).fetchall()
        )
        paths.extend(_shot_paths(connection, "still_image_id", still_image_id))
        connection.execute("DELETE FROM still_images WHERE id = ?", (still_image_id,))
    for path in paths:
        remove_within(data_dir, path)


def delete_shot(database: Database, data_dir: Path, shot_id: str) -> None:
    """カットを削除する。生成済みの動画と起点・最終フレームも片付ける。"""
    with database.connect() as connection:
        if connection.execute("SELECT 1 FROM shots WHERE id = ?", (shot_id,)).fetchone() is None:
            raise KeyError(shot_id)
        paths = _shot_paths(connection, "id", shot_id)
        connection.execute("DELETE FROM shots WHERE id = ?", (shot_id,))
    for path in paths:
        remove_within(data_dir, path)


def _shot_paths(connection, column: str, value: str) -> list[Path]:  # noqa: ANN001
    """カットが持つ動画・各種フレーム・ビートの区間動画の実体パス。

    `start_frame_image_path` は**含めない**。あれは「何を起点に使ったか」の記録で、
    実体は他の記録のもの（前カットの最終フレーム、元の静止画、構図変更で作った1枚）を
    指している。ここで消すと、カットを1つ削除しただけで前のカットの最終フレームが
    失われ、連続性が壊れる（実機で発生）。
    """
    rows = connection.execute(
        f"""
        SELECT output_video_path, last_frame_image_path,
               custom_start_frame_path, custom_end_frame_path
        FROM shots WHERE {column} = ?
        """,  # noqa: S608
        (value,),
    ).fetchall()
    paths = [
        Path(row[name])
        for row in rows
        for name in (
            "output_video_path",
            "last_frame_image_path",
            "custom_start_frame_path",
            "custom_end_frame_path",
        )
        if row[name]
    ]
    # ビートは shots への外部キーで一緒に消えるため、実体だけここで集めておく。
    beat_rows = connection.execute(
        f"""
        SELECT b.segment_video_path, b.last_frame_image_path
        FROM shot_beats b JOIN shots s ON s.id = b.shot_id
        WHERE s.{column} = ?
        """,  # noqa: S608
        (value,),
    ).fetchall()
    paths.extend(
        Path(row[name])
        for row in beat_rows
        for name in ("segment_video_path", "last_frame_image_path")
        if row[name]
    )
    return paths


def _project_file_paths(database: Database, project_id: str) -> list[Path]:
    """プロジェクト配下で作られた画像・動画の実体パスを集める。"""
    queries = (
        "SELECT portrait_image_path AS path FROM characters WHERE project_id = ?",
        "SELECT image_path AS path FROM background_assets WHERE project_id = ?",
        "SELECT image_path AS path FROM prop_assets WHERE project_id = ?",
        "SELECT image_path AS path FROM still_images WHERE project_id = ?",
        """
        SELECT p.pose_image_path AS path FROM still_image_characters p
        JOIN still_images sc ON sc.id = p.still_image_id WHERE sc.project_id = ?
        """,
        """
        SELECT f.image_path AS path FROM character_figures f
        JOIN characters c ON c.id = f.character_id WHERE c.project_id = ?
        """,
        """
        SELECT e.image_path AS path FROM character_figure_edits e
        JOIN characters c ON c.id = e.character_id WHERE c.project_id = ?
        """,
        """
        SELECT s.output_video_path AS path FROM shots s
        JOIN still_images sc ON sc.id = s.still_image_id WHERE sc.project_id = ?
        """,
        """
        SELECT s.last_frame_image_path AS path FROM shots s
        JOIN still_images sc ON sc.id = s.still_image_id WHERE sc.project_id = ?
        """,
        """
        SELECT s.custom_start_frame_path AS path FROM shots s
        JOIN still_images sc ON sc.id = s.still_image_id WHERE sc.project_id = ?
        """,
        """
        SELECT s.custom_end_frame_path AS path FROM shots s
        JOIN still_images sc ON sc.id = s.still_image_id WHERE sc.project_id = ?
        """,
        """
        SELECT b.segment_video_path AS path FROM shot_beats b
        JOIN shots s ON s.id = b.shot_id
        JOIN still_images sc ON sc.id = s.still_image_id WHERE sc.project_id = ?
        """,
        """
        SELECT b.last_frame_image_path AS path FROM shot_beats b
        JOIN shots s ON s.id = b.shot_id
        JOIN still_images sc ON sc.id = s.still_image_id WHERE sc.project_id = ?
        """,
    )
    paths: list[Path] = []
    with database.connect() as connection:
        for query in queries:
            paths.extend(
                Path(row["path"])
                for row in connection.execute(query, (project_id,)).fetchall()
                if row["path"]
            )
    return paths


def _character_image_records(database: Database, project_id: str) -> list[tuple[str, str]]:
    """人物候補の控え（アプリ管理領域 outputs/）の (種別, 記録ID)。"""
    with database.connect() as connection:
        candidates = connection.execute(
            """
            SELECT cc.id AS id FROM character_candidates cc
            JOIN characters c ON c.id = cc.character_id WHERE c.project_id = ?
            """,
            (project_id,),
        ).fetchall()
    return [(CANDIDATE_IMAGES, row["id"]) for row in candidates]


def delete_project(database: Database, data_dir: Path, project_id: str) -> None:
    """作品と、その人物・素材・静止画・カット・生成物をまとめて削除する。"""
    paths = _project_file_paths(database, project_id)
    records = _character_image_records(database, project_id)
    with database.connect() as connection:
        # 代表写真の場所は人物IDで決まるため、削除前に控える。
        character_ids = [
            row["id"]
            for row in connection.execute(
                "SELECT id FROM characters WHERE project_id = ?", (project_id,)
            ).fetchall()
        ]
        cursor = connection.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        if cursor.rowcount != 1:
            raise KeyError(project_id)
    for path in paths:
        remove_within(data_dir, path)
    for kind, record_id in records:
        delete_local_image(data_dir, kind, record_id)
    for character_id in character_ids:
        delete_portrait(portraits_dir(data_dir), character_id)
    # 書き出した1本も作品の持ち物。利用者が「名前を付けて保存」で外へ出したものは
    # アプリの保存先の外にあるので、ここでは消えない。
    remove_within(data_dir, project_export_path(data_dir, project_id))
