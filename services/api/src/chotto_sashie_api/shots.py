from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from sqlite3 import Connection, Row
from uuid import uuid4

from .database import Database
from .still_images import StillImageNotFoundError, StillImageRepository
from .schemas import (
    ShotBeatCreate,
    ShotBeatRead,
    ShotBeatUpdate,
    ShotCreate,
    ShotPromptSegment,
    ShotRead,
    ShotSpeechLine,
    ShotSpeechLineRead,
    ShotUpdate,
)


class ShotPromptLockedError(RuntimeError):
    """生成に使う文を直接書き直している間は、そのもとになる指示を変えられない。"""


class ShotSpeakerError(ValueError):
    """セリフの話す人が、この静止画にいる人物ではない。"""


# 生成に使う文のもとになる指示。構図は起点の絵を作るためのもので、文には入らない。
PROMPT_SOURCE_FIELDS = {
    "motion_prompt",
    "speech_lines",
    "ambience_prompt",
    "music_prompt",
    "timeline_mode",
    "duration_seconds",
}


class ShotNotFoundError(KeyError):
    pass


class ShotBeatNotFoundError(KeyError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _with_readiness(database: Database, shot: ShotRead, row: Row) -> ShotRead:
    """「動画を生成できるか」と、できない理由を添える（UIはこれを表示するだけにする）。"""
    from .readiness import shot_start_frame_source, shot_video_block_reason

    reason = shot_video_block_reason(database, shot)
    lines = _speech_lines_for(database, shot.id)
    beats = [
        beat.model_copy(update={"speech_lines": lines.get(beat.id, [])})
        for beat in _beats_for(database, shot.id)
    ]
    total = sum(beat.duration_seconds for beat in beats) or shot.duration_seconds
    resolved = shot.model_copy(
        update={"beats": beats, "total_seconds": total, "speech_lines": lines.get(None, [])}
    )
    edit_mode = bool(row["prompt_edit_mode"])
    stored = _segments_from_json(
        row["edited_prompt_json"] if edit_mode else row["resolved_prompt_json"]
    )
    # 書き直している間は、もとの指示を変えられないので古くなりようがない。
    stale = bool(stored) and not edit_mode and (
        row["resolved_prompt_signature"] != prompt_signature(resolved)
    )
    return resolved.model_copy(
        update={
            "can_generate_video": not reason,
            "video_blocked_reason": reason,
            "start_frame_source": shot_start_frame_source(database, shot),
            "has_prompt": bool(stored),
            "prompt_is_stale": stale,
            "prompt_edit_mode": edit_mode,
        }
    )


def _segments_from_json(raw: str) -> list[ShotPromptSegment]:
    """保存した「生成に使う文」を読む。壊れていれば無かったことにする（生成は止めない）。"""
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(items, list):
        return []
    segments: list[ShotPromptSegment] = []
    for item in items:
        try:
            segments.append(ShotPromptSegment.model_validate(item))
        except ValueError:
            return []
    return segments


def segments_to_json(segments: list[ShotPromptSegment]) -> str:
    return json.dumps([segment.model_dump() for segment in segments], ensure_ascii=False)


def _lines_signature(lines: list[ShotSpeechLineRead]) -> list[list[object]]:
    # 話す人の見分け方（配置・年齢・髪）が変わっても、文の中の名指しが変わる。
    return [
        [line.speaker_character_id, line.narration, line.text.strip(), line.speaker_description]
        for line in lines
    ]


def prompt_signature(shot: ShotRead) -> str:
    """文のもとになる指示の指紋。ここが変われば、用意した文はもう使えない。

    起点の画像・構図・覚え書きはこの文に入らないので含めない（作り直しを促す必要がない）。
    """
    payload = json.dumps(
        {
            "motion": shot.motion_prompt.strip(),
            "speech": _lines_signature(shot.speech_lines),
            "ambience": shot.ambience_prompt.strip(),
            "music": shot.music_prompt.strip(),
            "timeline_mode": shot.timeline_mode,
            "duration": round(shot.duration_seconds, 3),
            "beats": [
                [
                    round(beat.duration_seconds, 3),
                    beat.motion_prompt.strip(),
                    _lines_signature(beat.speech_lines),
                ]
                for beat in shot.beats
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _exists(path: str | None) -> bool:
    """記録された道の先に実体があるか。

    道だけ残って実体が消えていることがある（過去の削除処理が、前のカットの最終フレームを
    巻き添えにしていた）。「動画あり」「続きから始められる」を実体で判断する。
    """
    return bool(path) and Path(path).is_file()


def _video_format(video_path: str | None) -> str:
    """保存済み動画の入れ物を拡張子から読む（webp=Wan / mp4=MiniMax H3）。"""
    if not video_path:
        return ""
    suffix = Path(video_path).suffix.lower().lstrip(".")
    return suffix if suffix in {"webp", "mp4"} else ""


def _to_shot(row: Row) -> ShotRead:
    return ShotRead(
        id=row["id"],
        still_image_id=row["still_image_id"],
        ordinal=row["ordinal"],
        motion_prompt=row["motion_prompt"],
        ambience_prompt=row["ambience_prompt"],
        music_prompt=row["music_prompt"],
        composition_prompt=row["composition_prompt"],
        timeline_mode=row["timeline_mode"] or "single",
        note=row["note"],
        duration_seconds=row["duration_seconds"],
        has_start_frame=_exists(row["start_frame_image_path"]),
        has_custom_start_frame=_exists(row["custom_start_frame_path"]),
        has_custom_end_frame=_exists(row["custom_end_frame_path"]),
        video_job_id=row["video_job_id"],
        has_video=_exists(row["output_video_path"]),
        video_format=_video_format(row["output_video_path"]),
        has_last_frame=_exists(row["last_frame_image_path"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _to_beat(row: Row, start_seconds: float) -> ShotBeatRead:
    return ShotBeatRead(
        id=row["id"],
        shot_id=row["shot_id"],
        ordinal=row["ordinal"],
        start_seconds=start_seconds,
        duration_seconds=row["duration_seconds"],
        motion_prompt=row["motion_prompt"],
        has_segment_video=_exists(row["segment_video_path"]),
        segment_video_format=_video_format(row["segment_video_path"]),
        has_last_frame=_exists(row["last_frame_image_path"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _beats_for(database: Database, shot_id: str) -> list[ShotBeatRead]:
    """カットのビートを順に返す。開始時刻は先行ビートの長さの合計から導く。"""
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT * FROM shot_beats WHERE shot_id = ? ORDER BY ordinal", (shot_id,)
        ).fetchall()
    beats: list[ShotBeatRead] = []
    start = 0.0
    for row in rows:
        beats.append(_to_beat(row, start))
        start += row["duration_seconds"]
    return beats


# 見分ける手がかり（配置・年齢・髪）が無くても、画面に1人しかいなければ名指しできる。
# 英語で持つので、訳さずにそのまま文へ入る。
ONLY_PERSON_DESCRIPTION = "the only person on screen"


def speaker_description(
    *, placement: str, gender_age: str, hair_color: str, hair_style: str
) -> str:
    """話す人を画面の中で見分けるための記述。生成に使う文では英語へ直して名指しに使う。

    名前は入れない。日本語の名前を文に入れると、セリフと区別されず読み上げられる
    （日本語の断片を順に読み上げることは実機で確認済み）。見た目と配置で名指しするのは、
    公式テンプレートが話し手を見た目で書く（"the little boy superhero ... delivers his line"）
    のに合わせている。
    """
    hair = " ".join(part for part in (hair_color.strip(), hair_style.strip()) if part)
    parts = [
        f"位置: {placement.strip()}" if placement.strip() else "",
        f"年齢・性別: {gender_age.strip()}" if gender_age.strip() else "",
        f"髪: {hair}" if hair else "",
    ]
    return " / ".join(part for part in parts if part)


def _speech_lines_for(
    database: Database, shot_id: str
) -> dict[str | None, list[ShotSpeechLineRead]]:
    """カットのセリフの行を、持ち主（None=カット全体、またはビートのid）ごとに返す。"""
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT l.beat_id, l.speaker_character_id, l.narration, l.text,
                   si.character_ids_json,
                   c.gender_age, c.hair_color, c.hair_style,
                   sic.spec_json
            FROM shot_speech_lines l
            JOIN shots s ON s.id = l.shot_id
            JOIN still_images si ON si.id = s.still_image_id
            LEFT JOIN characters c ON c.id = l.speaker_character_id
            LEFT JOIN still_image_characters sic
                ON sic.still_image_id = s.still_image_id
               AND sic.character_id = l.speaker_character_id
            WHERE l.shot_id = ?
            ORDER BY l.ordinal
            """,
            (shot_id,),
        ).fetchall()
    grouped: dict[str | None, list[ShotSpeechLineRead]] = {}
    for row in rows:
        speaker = row["speaker_character_id"]
        narration = bool(row["narration"])
        people = _character_ids(row["character_ids_json"])
        present = narration or (bool(speaker) and speaker in people)
        description = ""
        if speaker and row["gender_age"] is not None:
            description = speaker_description(
                placement=_placement(row["spec_json"]),
                gender_age=row["gender_age"],
                hair_color=row["hair_color"],
                hair_style=row["hair_style"],
            )
            if not description and present and len(people) == 1:
                description = ONLY_PERSON_DESCRIPTION
        grouped.setdefault(row["beat_id"], []).append(
            ShotSpeechLineRead(
                speaker_character_id=speaker,
                narration=narration,
                text=row["text"],
                speaker_description=description,
                speaker_present=present,
            )
        )
    return grouped


def _character_ids(raw: str | None) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except ValueError:
        return []
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _placement(raw: str | None) -> str:
    """静止画の中での配置（「右寄り」など）。指定が無ければ空。"""
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return ""
    placement = value.get("placement", "") if isinstance(value, dict) else ""
    return placement if isinstance(placement, str) else ""


def _replace_speech_lines(
    connection: Connection,
    shot_id: str,
    beat_id: str | None,
    lines: list[ShotSpeechLine],
) -> None:
    """セリフの行を、まるごと置き換える。

    話す人は、この静止画にいる人物に限る。別の静止画の人物を選べると、画面にいない人を
    名指しすることになる。
    """
    row = connection.execute(
        """
        SELECT si.character_ids_json FROM shots s
        JOIN still_images si ON si.id = s.still_image_id
        WHERE s.id = ?
        """,
        (shot_id,),
    ).fetchone()
    present = _character_ids(row["character_ids_json"]) if row else []
    for line in lines:
        if line.speaker_character_id and line.speaker_character_id not in present:
            raise ShotSpeakerError("話す人は、この静止画にいる人物から選んでください")
    connection.execute(
        "DELETE FROM shot_speech_lines WHERE shot_id = ? AND beat_id IS ?",
        (shot_id, beat_id),
    )
    timestamp = _now()
    for ordinal, line in enumerate(lines, start=1):
        connection.execute(
            """
            INSERT INTO shot_speech_lines(
                id, shot_id, beat_id, ordinal, speaker_character_id, narration, text,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                shot_id,
                beat_id,
                ordinal,
                line.speaker_character_id,
                1 if line.narration else 0,
                line.text,
                timestamp,
                timestamp,
            ),
        )


class ShotRepository:
    def __init__(self, database: Database):
        self.database = database

    def create(self, still_image_id: str, request: ShotCreate) -> ShotRead:
        StillImageRepository(self.database).get(still_image_id)
        shot_id = str(uuid4())
        timestamp = _now()
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 AS next FROM shots WHERE still_image_id = ?",
                (still_image_id,),
            ).fetchone()
            ordinal = row["next"]
            connection.execute(
                """
                INSERT INTO shots(
                    id, still_image_id, ordinal, motion_prompt,
                    ambience_prompt, music_prompt, composition_prompt, timeline_mode,
                    note, duration_seconds, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    shot_id,
                    still_image_id,
                    ordinal,
                    request.motion_prompt,
                    request.ambience_prompt,
                    request.music_prompt,
                    request.composition_prompt,
                    request.timeline_mode,
                    request.note,
                    request.duration_seconds,
                    timestamp,
                    timestamp,
                ),
            )
            _replace_speech_lines(connection, shot_id, None, request.speech_lines)
        return self.get(shot_id)

    def get(self, shot_id: str) -> ShotRead:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM shots WHERE id = ?", (shot_id,)).fetchone()
        if row is None:
            raise ShotNotFoundError(shot_id)
        return _with_readiness(self.database, _to_shot(row), row)

    def list(self, still_image_id: str) -> list[ShotRead]:
        StillImageRepository(self.database).get(still_image_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM shots WHERE still_image_id = ? ORDER BY ordinal", (still_image_id,)
            ).fetchall()
        return [_with_readiness(self.database, _to_shot(row), row) for row in rows]

    def update(self, shot_id: str, request: ShotUpdate) -> ShotRead:
        current = self.get(shot_id)
        updates = request.model_dump(exclude_unset=True)
        if not updates:
            return current
        allowed = {
            "motion_prompt",
            "speech_lines",
            "ambience_prompt",
            "music_prompt",
            "composition_prompt",
            "timeline_mode",
            "note",
            "duration_seconds",
        }
        if not set(updates).issubset(allowed):
            raise ValueError("更新対象に許可されていない項目があります")
        # 文を直接書き直している間は、その文のもとになる指示を変えさせない
        # （変えても文へ反映されず、画面の指示と実際に送る文が食い違う）。
        if current.prompt_edit_mode and set(updates) & PROMPT_SOURCE_FIELDS:
            raise ShotPromptLockedError(
                "生成に使う文を直接書き直している間は、指示を変えられません"
            )
        timestamp = _now()
        lines = updates.pop("speech_lines", None)
        assignments = "".join(f"{name} = ?, " for name in updates)
        with self.database.connect() as connection:
            cursor = connection.execute(
                f"UPDATE shots SET {assignments}updated_at = ? WHERE id = ?",
                (*updates.values(), timestamp, shot_id),
            )
            if cursor.rowcount != 1:
                raise ShotNotFoundError(shot_id)
            if lines is not None:
                _replace_speech_lines(connection, shot_id, None, request.speech_lines or [])
        return self.get(shot_id)

    def delete(self, shot_id: str) -> Path | None:
        """削除したショットの動画ファイルパスを返す（あれば呼び出し側で削除）。"""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT output_video_path FROM shots WHERE id = ?", (shot_id,)
            ).fetchone()
            if row is None:
                raise ShotNotFoundError(shot_id)
            video_path = row["output_video_path"]
            connection.execute("DELETE FROM shots WHERE id = ?", (shot_id,))
        return Path(video_path) if video_path else None

    def start_frame_path(self, shot_id: str) -> Path | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT start_frame_image_path FROM shots WHERE id = ?", (shot_id,)
            ).fetchone()
        if row is None:
            raise ShotNotFoundError(shot_id)
        return Path(row["start_frame_image_path"]) if row["start_frame_image_path"] else None

    def video_path(self, shot_id: str) -> Path | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT output_video_path FROM shots WHERE id = ?", (shot_id,)
            ).fetchone()
        if row is None:
            raise ShotNotFoundError(shot_id)
        return Path(row["output_video_path"]) if row["output_video_path"] else None

    def set_start_frame(self, shot_id: str, image_path: Path) -> ShotRead:
        with self.database.connect() as connection:
            cursor = connection.execute(
                "UPDATE shots SET start_frame_image_path = ?, updated_at = ? WHERE id = ?",
                (str(image_path), _now(), shot_id),
            )
            if cursor.rowcount != 1:
                raise ShotNotFoundError(shot_id)
        return self.get(shot_id)

    def set_video_job(self, shot_id: str, job_id: str) -> ShotRead:
        with self.database.connect() as connection:
            cursor = connection.execute(
                "UPDATE shots SET video_job_id = ?, updated_at = ? WHERE id = ?",
                (job_id, _now(), shot_id),
            )
            if cursor.rowcount != 1:
                raise ShotNotFoundError(shot_id)
        return self.get(shot_id)

    def set_output_video(self, shot_id: str, video_path: Path) -> ShotRead:
        with self.database.connect() as connection:
            cursor = connection.execute(
                "UPDATE shots SET output_video_path = ?, updated_at = ? WHERE id = ?",
                (str(video_path), _now(), shot_id),
            )
            if cursor.rowcount != 1:
                raise ShotNotFoundError(shot_id)
        return self.get(shot_id)

    def last_frame_path(self, shot_id: str) -> Path | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT last_frame_image_path FROM shots WHERE id = ?", (shot_id,)
            ).fetchone()
        if row is None:
            raise ShotNotFoundError(shot_id)
        return Path(row["last_frame_image_path"]) if row["last_frame_image_path"] else None

    def set_last_frame(self, shot_id: str, image_path: Path) -> ShotRead:
        with self.database.connect() as connection:
            cursor = connection.execute(
                "UPDATE shots SET last_frame_image_path = ?, updated_at = ? WHERE id = ?",
                (str(image_path), _now(), shot_id),
            )
            if cursor.rowcount != 1:
                raise ShotNotFoundError(shot_id)
        return self.get(shot_id)

    def _path(self, shot_id: str, column: str) -> Path | None:
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT {column} FROM shots WHERE id = ?", (shot_id,)
            ).fetchone()
        if row is None:
            raise ShotNotFoundError(shot_id)
        return Path(row[column]) if row[column] else None

    def _set_path(self, shot_id: str, column: str, image_path: Path | None) -> ShotRead:
        with self.database.connect() as connection:
            cursor = connection.execute(
                f"UPDATE shots SET {column} = ?, updated_at = ? WHERE id = ?",
                (str(image_path) if image_path else "", _now(), shot_id),
            )
            if cursor.rowcount != 1:
                raise ShotNotFoundError(shot_id)
        return self.get(shot_id)

    def clear_output_video(self, shot_id: str) -> Path | None:
        """記録していた動画を外し、その場所を返す（ファイルは呼び出し側が片付ける）。"""
        previous = self._path(shot_id, "output_video_path")
        self._set_path(shot_id, "output_video_path", None)
        return previous

    def clear_last_frame(self, shot_id: str) -> Path | None:
        """記録していた最終フレームを外し、その場所を返す。"""
        previous = self._path(shot_id, "last_frame_image_path")
        self._set_path(shot_id, "last_frame_image_path", None)
        return previous

    def custom_start_frame_path(self, shot_id: str) -> Path | None:
        """構図変更で作った起点フレーム。無ければ None。"""
        return self._path(shot_id, "custom_start_frame_path")

    def set_custom_start_frame(self, shot_id: str, image_path: Path | None) -> ShotRead:
        return self._set_path(shot_id, "custom_start_frame_path", image_path)

    def custom_end_frame_path(self, shot_id: str) -> Path | None:
        """終端の絵（MiniMax H3 の last_frame へ渡す）。無ければ None。"""
        return self._path(shot_id, "custom_end_frame_path")

    def set_custom_end_frame(self, shot_id: str, image_path: Path | None) -> ShotRead:
        return self._set_path(shot_id, "custom_end_frame_path", image_path)

    # --- 生成に使う文 ---

    def _prompt_row(self, shot_id: str) -> Row:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT resolved_prompt_json, resolved_prompt_engine,
                       resolved_prompt_signature, edited_prompt_json, prompt_edit_mode
                FROM shots WHERE id = ?
                """,
                (shot_id,),
            ).fetchone()
        if row is None:
            raise ShotNotFoundError(shot_id)
        return row

    def resolved_prompt(self, shot_id: str) -> tuple[list[ShotPromptSegment], str, str]:
        """用意できた文と、それを作ったときの作り方・指示の指紋。"""
        row = self._prompt_row(shot_id)
        return (
            _segments_from_json(row["resolved_prompt_json"]),
            row["resolved_prompt_engine"],
            row["resolved_prompt_signature"],
        )

    def edited_prompt(self, shot_id: str) -> list[ShotPromptSegment]:
        """直接書き直した文。書き直しモードを切ると空になる。"""
        return _segments_from_json(self._prompt_row(shot_id)["edited_prompt_json"])

    def set_resolved_prompt(
        self, shot_id: str, segments: list[ShotPromptSegment], engine: str, signature: str
    ) -> ShotRead:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE shots
                SET resolved_prompt_json = ?, resolved_prompt_engine = ?,
                    resolved_prompt_signature = ?, updated_at = ?
                WHERE id = ?
                """,
                (segments_to_json(segments), engine, signature, _now(), shot_id),
            )
            if cursor.rowcount != 1:
                raise ShotNotFoundError(shot_id)
        return self.get(shot_id)

    def set_edited_prompt(self, shot_id: str, segments: list[ShotPromptSegment]) -> ShotRead:
        with self.database.connect() as connection:
            cursor = connection.execute(
                "UPDATE shots SET edited_prompt_json = ?, updated_at = ? WHERE id = ?",
                (segments_to_json(segments) if segments else "", _now(), shot_id),
            )
            if cursor.rowcount != 1:
                raise ShotNotFoundError(shot_id)
        return self.get(shot_id)

    def set_prompt_edit_mode(self, shot_id: str, enabled: bool) -> ShotRead:
        """書き直しモードの入切。

        入にするときは、いま使われる文をそのまま写して書き始められるようにする。
        切るときは書き直した文を捨て、指示から作った文へ戻す（どちらが使われるのかを
        画面と一致させるため、両方を持ち続けない）。
        """
        row = self._prompt_row(shot_id)
        if enabled:
            edited = _segments_from_json(row["resolved_prompt_json"])
            if not edited:
                raise ValueError("先に、生成に使う文を作ってください")
            for segment in edited:
                segment.edited = True
            payload = segments_to_json(edited)
        else:
            payload = ""
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE shots SET prompt_edit_mode = ?, edited_prompt_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (1 if enabled else 0, payload, _now(), shot_id),
            )
            if cursor.rowcount != 1:
                raise ShotNotFoundError(shot_id)
        return self.get(shot_id)

    def previous_shot(self, still_image_id: str, ordinal: int) -> ShotRead | None:
        """同じ静止画から作った、1つ前（ordinalが直前）のカットを返す。無ければ None。"""
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM shots
                WHERE still_image_id = ? AND ordinal < ?
                ORDER BY ordinal DESC LIMIT 1
                """,
                (still_image_id, ordinal),
            ).fetchone()
        # 判定（`_with_readiness`）がこの関数を使うため、ここでは判定を足さない。
        return _to_shot(row) if row is not None else None


class ShotBeatRepository:
    """カット内のビート（時間区間）。順番と長さだけを持ち、開始時刻は導出する。"""

    def __init__(self, database: Database):
        self.database = database

    def _ensure_unlocked(self, shot_id: str) -> None:
        """文を直接書き直している間は、時間割りも変えさせない（文に反映されないため）。"""
        if ShotRepository(self.database).get(shot_id).prompt_edit_mode:
            raise ShotPromptLockedError(
                "生成に使う文を直接書き直している間は、時間割りを変えられません"
            )

    def create(self, shot_id: str, request: ShotBeatCreate) -> ShotBeatRead:
        ShotRepository(self.database).get(shot_id)
        self._ensure_unlocked(shot_id)
        beat_id = str(uuid4())
        timestamp = _now()
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 AS next FROM shot_beats WHERE shot_id = ?",
                (shot_id,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO shot_beats(
                    id, shot_id, ordinal, duration_seconds, motion_prompt,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    beat_id,
                    shot_id,
                    row["next"],
                    request.duration_seconds,
                    request.motion_prompt,
                    timestamp,
                    timestamp,
                ),
            )
            _replace_speech_lines(connection, shot_id, beat_id, request.speech_lines)
        return self.get(beat_id)

    def get(self, beat_id: str) -> ShotBeatRead:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM shot_beats WHERE id = ?", (beat_id,)
            ).fetchone()
        if row is None:
            raise ShotBeatNotFoundError(beat_id)
        # 開始時刻は同じカット内の先行ビートの合計。単体取得でも同じ値になるようにする。
        for beat in _beats_for(self.database, row["shot_id"]):
            if beat.id == beat_id:
                lines = _speech_lines_for(self.database, row["shot_id"]).get(beat_id, [])
                return beat.model_copy(update={"speech_lines": lines})
        raise ShotBeatNotFoundError(beat_id)

    def list(self, shot_id: str) -> list[ShotBeatRead]:
        return ShotRepository(self.database).get(shot_id).beats

    def update(self, beat_id: str, request: ShotBeatUpdate) -> ShotBeatRead:
        current = self.get(beat_id)
        updates = request.model_dump(exclude_unset=True)
        if not updates:
            return current
        self._ensure_unlocked(current.shot_id)
        allowed = {"duration_seconds", "motion_prompt", "speech_lines"}
        if not set(updates).issubset(allowed):
            raise ValueError("更新対象に許可されていない項目があります")
        lines = updates.pop("speech_lines", None)
        assignments = "".join(f"{name} = ?, " for name in updates)
        with self.database.connect() as connection:
            cursor = connection.execute(
                f"UPDATE shot_beats SET {assignments}updated_at = ? WHERE id = ?",
                (*updates.values(), _now(), beat_id),
            )
            if cursor.rowcount != 1:
                raise ShotBeatNotFoundError(beat_id)
            if lines is not None:
                _replace_speech_lines(
                    connection, current.shot_id, beat_id, request.speech_lines or []
                )
        return self.get(beat_id)

    def delete(self, beat_id: str) -> list[Path]:
        """ビートを消し、残ったビートの順番を詰める。消したファイルの候補を返す。"""
        self._ensure_unlocked(self.get(beat_id).shot_id)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM shot_beats WHERE id = ?", (beat_id,)
            ).fetchone()
            if row is None:
                raise ShotBeatNotFoundError(beat_id)
            shot_id = row["shot_id"]
            files = [
                Path(row[name])
                for name in ("segment_video_path", "last_frame_image_path")
                if row[name]
            ]
            connection.execute("DELETE FROM shot_beats WHERE id = ?", (beat_id,))
            # 番号に穴が空くと「何番目のビートか」が読めなくなるため、詰め直す。
            remaining = connection.execute(
                "SELECT id FROM shot_beats WHERE shot_id = ? ORDER BY ordinal", (shot_id,)
            ).fetchall()
            for index, remaining_row in enumerate(remaining, start=1):
                connection.execute(
                    "UPDATE shot_beats SET ordinal = ? WHERE id = ?",
                    (index, remaining_row["id"]),
                )
        return files

    def segment_video_path(self, beat_id: str) -> Path | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT segment_video_path FROM shot_beats WHERE id = ?", (beat_id,)
            ).fetchone()
        if row is None:
            raise ShotBeatNotFoundError(beat_id)
        return Path(row["segment_video_path"]) if row["segment_video_path"] else None

    def last_frame_path(self, beat_id: str) -> Path | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT last_frame_image_path FROM shot_beats WHERE id = ?", (beat_id,)
            ).fetchone()
        if row is None:
            raise ShotBeatNotFoundError(beat_id)
        return Path(row["last_frame_image_path"]) if row["last_frame_image_path"] else None

    def clear_segments(self, shot_id: str) -> list[Path]:
        """このカットのビートが持つ区間動画と最終フレームを外し、その場所を返す。

        カットを1本で作り直したとき、前に分割で作った区間はもう使わない。記録に
        残すと、使わない動画が保存先に居座り、画面にも古い区間が出続ける。
        """
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT segment_video_path, last_frame_image_path FROM shot_beats
                WHERE shot_id = ?
                """,
                (shot_id,),
            ).fetchall()
            connection.execute(
                """
                UPDATE shot_beats
                SET segment_video_path = '', last_frame_image_path = '', updated_at = ?
                WHERE shot_id = ?
                """,
                (_now(), shot_id),
            )
        return [
            Path(row[name])
            for row in rows
            for name in ("segment_video_path", "last_frame_image_path")
            if row[name]
        ]

    def set_segment(
        self, beat_id: str, video_path: Path, last_frame: Path | None
    ) -> ShotBeatRead:
        """分割モードで作った区間動画と、その最終フレームを記録する。"""
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE shot_beats
                SET segment_video_path = ?, last_frame_image_path = ?, updated_at = ?
                WHERE id = ?
                """,
                (str(video_path), str(last_frame) if last_frame else "", _now(), beat_id),
            )
            if cursor.rowcount != 1:
                raise ShotBeatNotFoundError(beat_id)
        return self.get(beat_id)
