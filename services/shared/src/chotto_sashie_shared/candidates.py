from __future__ import annotations

import json
from datetime import UTC, datetime
from sqlite3 import Row
from uuid import uuid4

from .sqlite import SqliteDatabase as Database
from .character_schemas import CandidateRead, CharacterRead


class CandidateNotFoundError(KeyError):
    pass


class CandidateSelectionError(ValueError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _to_candidate(row: Row) -> CandidateRead:
    image = json.loads(row["image_json"]) if row["image_json"] else None
    return CandidateRead(
        id=row["id"],
        character_id=row["character_id"],
        batch_job_id=row["batch_job_id"],
        ordinal=row["ordinal"],
        state=row["state"],
        prompt=row["prompt"],
        negative_prompt=row["negative_prompt"],
        seed=row["seed"],
        width=row["width"],
        height=row["height"],
        base_url=row["base_url"],
        prompt_id=row["prompt_id"],
        image=image,
        is_base=bool(row["is_base"]),
        error_message=row["error_message"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        url=f"/api/v1/character-candidates/{row['id']}/image" if image else None,
    )


# 顔立ちが未指定のとき、候補ごとに顔の造作そのものを探索する差分。
# ここで初めて輪郭・眉・目・鼻・口を決めるため、全体を大きく変える。
FACE_DESIGN_VARIANTS = [
    "compact facial proportions, softly rounded cheeks, a small tapered chin, gently arched eyebrows, balanced eye spacing, a small straight nose, a defined cupid's-bow mouth",
    "slightly longer facial proportions, high subtle cheekbones, a slim soft jaw, delicate straight eyebrows, wider eye spacing, a rounded nose tip, a calm narrow mouth",
    "broader upper-face proportions, fuller cheeks, a tapered lower face, expressive curved eyebrows, closer eye spacing, a short refined nose, a fuller lower lip",
    "narrow facial proportions, low cheek definition, a firmer jawline, low straight eyebrows, balanced eye spacing, a slender nose bridge, a small asymmetric mouth",
    "heart-shaped facial proportions, a wider upper face, a delicate pointed chin, soft feathered eyebrows, open eye spacing, a petite nose, a heart-shaped lip line",
    "round facial proportions, a short midface, a softly curved jaw, clean angled eyebrows, compact eye spacing, a defined nose bridge, a composed mouth",
    "long-oval facial proportions, sculpted cheeks, a narrow chin, thin graceful eyebrows, balanced eye spacing, an upturned nose, a delicate small mouth",
    "broad facial proportions, low full cheeks, a rounded chin, natural full eyebrows, wide eye spacing, a short nose bridge, a relaxed rounded mouth",
    "slender facial proportions, pronounced cheek transition, a crisp tapered jaw, subtle angular eyebrows, close-balanced eye spacing, a narrow nose tip, a crisp upper lip",
    "soft triangular facial proportions, a narrower forehead, gentle cheeks, a defined chin, long curved eyebrows, open eye spacing, a gentle nose, an expressive mouth",
    "short balanced facial proportions, subtle high cheeks, a compact jaw, short refined eyebrows, balanced eye spacing, a sculpted nose, a softly tapered mouth",
    "elongated facial proportions, shallow cheeks, a softly squared chin, shallow-arched eyebrows, offset eye spacing, a delicate nose, a restrained warm mouth",
]

# 顔立ちが指定済みのとき、指定した造作は動かさず、描写だけを少し変える差分。
# 造作を再指定すると指定顔を打ち消してしまうため、ここには輪郭・目鼻立ちを書かない。
FACE_RENDER_VARIANTS = [
    "keep the defined face exactly; render with even, soft frontal lighting and a calm closed-mouth expression",
    "keep the defined face exactly; render with slightly warmer lighting and a very faint natural asymmetry",
    "keep the defined face exactly; render with gentle side-fill lighting and a relaxed, barely-there expression",
    "keep the defined face exactly; render with crisp even lighting and finely detailed skin texture",
    "keep the defined face exactly; render with soft diffused lighting and a subtly softer gaze",
    "keep the defined face exactly; render with cool neutral lighting and a composed, alert expression",
    "keep the defined face exactly; render with warm soft lighting and lightly tousled hairline strands",
    "keep the defined face exactly; render with balanced studio lighting and a natural matte complexion",
    "keep the defined face exactly; render with gentle top lighting and a faintly serious expression",
    "keep the defined face exactly; render with even soft lighting and a slightly relaxed jaw",
    "keep the defined face exactly; render with clear frontal lighting and subtle under-eye realism",
    "keep the defined face exactly; render with soft ambient lighting and a quiet neutral expression",
]


def build_identity_clause(character: CharacterRead, project_style: str = "") -> str:
    """人物の識別条件をまとめた説明文を返す（バンドル書き出しなどからも使う公開版）。"""
    return _identity_clause(character, project_style)


def _identity_clause(character: CharacterRead, project_style: str) -> str:
    """顔立ちを先頭に置いた、人物固有の識別条件をまとめる。

    生成モデルは前方のトークンを強く反映するため、変えてはいけない造作ほど
    前に置く。作品画風は識別条件ではないので末尾へ回す。
    """
    traits = "、".join(character.immutable_features)
    parts = [
        (
            "DEFINING FACE (highest priority, follow literally, do not beautify away "
            f"plain or unusual features): {character.face}"
            if character.face
            else ""
        ),
        f"age and gender: {character.gender_age}" if character.gender_age else "",
        f"eyes: {character.eyes}" if character.eyes else "",
        f"eye color: {character.eye_color}" if character.eye_color else "",
        f"hair style: {character.hair_style}" if character.hair_style else "",
        f"hair color: {character.hair_color}" if character.hair_color else "",
        f"build: {character.body_type}" if character.body_type else "",
        f"must keep exactly: {traits}" if traits else "",
        f"outfit: {character.base_outfit}" if character.base_outfit else "",
        f"accessories: {character.accessories}" if character.accessories else "",
        f"impression: {character.personality}" if character.personality else "",
        (
            "illustration style shared by every character in the work: "
            f"{project_style}"
            if project_style
            else ""
        ),
        (
            "character-specific nuance (a small modifier, do not replace the shared style): "
            f"{character.art_style}"
            if character.art_style
            else ""
        ),
    ]
    return ", ".join(part for part in parts if part)


def build_character_candidate_prompts(
    character: CharacterRead,
    count: int,
    *,
    project_style: str = "",
) -> list[str]:
    """作品画風と人物固有条件を分離し、顔の差が出る候補プロンプトを作る。

    顔立ちを先頭へ置いて反映を強め、候補ごとの差分は顔指定の有無で切り替える。
    顔が指定済みなら造作は動かさず描写だけを変え、未指定なら造作そのものを探索する。
    """
    identity = _identity_clause(character, project_style)
    face_is_defined = bool(character.face)
    prompts: list[str] = []
    for index in range(count):
        if face_is_defined:
            variation = FACE_RENDER_VARIANTS[index]
        else:
            variation = (
                f"explore a distinct facial geometry for this candidate: "
                f"{FACE_DESIGN_VARIANTS[index]}"
            )
        prompts.append(
            "single character, solo, one person only, "
            "front-facing bust portrait, head and shoulders, neutral expression, "
            "looking at viewer, identical camera and framing, plain soft background. "
            f"{identity}. "
            f"{variation}. "
            "render a recognizably individual face with the exact geometry described above, "
            "not a generic or average idol face; preserve every specified and locked trait. "
            "detailed face, detailed eyes, consistent anatomy, clean silhouette, "
            "no text, no watermark."
        )
    return prompts


class CandidateRepository:
    def __init__(self, database: Database):
        self.database = database

    def create(
        self,
        *,
        character_id: str,
        batch_job_id: str,
        ordinal: int,
        prompt: str,
        negative_prompt: str,
        seed: int,
        width: int,
        height: int,
        base_url: str,
    ) -> CandidateRead:
        candidate_id = str(uuid4())
        timestamp = _now()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO character_candidates(
                    id, character_id, batch_job_id, ordinal, state, prompt,
                    negative_prompt, seed, width, height, base_url, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    character_id,
                    batch_job_id,
                    ordinal,
                    prompt,
                    negative_prompt,
                    seed,
                    width,
                    height,
                    base_url,
                    timestamp,
                    timestamp,
                ),
            )
        return self.get(candidate_id)

    def get(self, candidate_id: str) -> CandidateRead:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM character_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
        if row is None:
            raise CandidateNotFoundError(candidate_id)
        return _to_candidate(row)

    def mark_running(self, candidate_id: str, prompt_id: str) -> CandidateRead:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE character_candidates
                SET state = 'running', prompt_id = ?, updated_at = ?
                WHERE id = ? AND state = 'queued'
                """,
                (prompt_id, _now(), candidate_id),
            )
        if cursor.rowcount != 1:
            raise CandidateNotFoundError(candidate_id)
        return self.get(candidate_id)

    def complete(self, candidate_id: str, image: dict[str, str]) -> CandidateRead:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE character_candidates
                SET state = 'completed', image_json = ?, error_message = NULL, updated_at = ?
                WHERE id = ? AND state = 'running'
                """,
                (json.dumps(image, ensure_ascii=False), _now(), candidate_id),
            )
        if cursor.rowcount != 1:
            raise CandidateNotFoundError(candidate_id)
        return self.get(candidate_id)

    def fail(self, candidate_id: str, message: str) -> CandidateRead:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE character_candidates
                SET state = 'failed', error_message = ?, updated_at = ?
                WHERE id = ? AND state IN ('queued', 'running')
                """,
                (message[:1000], _now(), candidate_id),
            )
        if cursor.rowcount != 1:
            return self.get(candidate_id)
        return self.get(candidate_id)

    def fail_batch(self, batch_job_id: str, message: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE character_candidates
                SET state = 'failed', error_message = ?, updated_at = ?
                WHERE batch_job_id = ? AND state IN ('queued', 'running')
                """,
                (message[:1000], _now(), batch_job_id),
            )

    def recover_interrupted(self, message: str) -> int:
        """前回終了時に生成中だった人物候補を失敗へ倒す。

        親ジョブは `JobRepository.recover_interrupted` が中断へ倒す。こちらを忘れると、
        画面には終わらない「生成中」が残り続ける。
        """
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE character_candidates
                SET state = 'failed', error_message = ?, updated_at = ?
                WHERE state IN ('queued', 'running')
                """,
                (message[:1000], _now()),
            )
            return cursor.rowcount

    def list_for_character(
        self,
        character_id: str,
        *,
        latest_only: bool = True,
        batch_job_id: str | None = None,
    ) -> list[CandidateRead]:
        """既定は最新の生成回だけ。`batch_job_id` を指定すると前の回も見られる。"""
        with self.database.connect() as connection:
            if batch_job_id is not None:
                rows = connection.execute(
                    """
                    SELECT * FROM character_candidates
                    WHERE character_id = ? AND batch_job_id = ?
                    ORDER BY ordinal
                    """,
                    (character_id, batch_job_id),
                ).fetchall()
            elif latest_only:
                latest = connection.execute(
                    """
                    SELECT batch_job_id FROM character_candidates
                    WHERE character_id = ?
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (character_id,),
                ).fetchone()
                if latest is None:
                    return []
                rows = connection.execute(
                    """
                    SELECT * FROM character_candidates
                    WHERE character_id = ? AND batch_job_id = ?
                    ORDER BY ordinal
                    """,
                    (character_id, latest["batch_job_id"]),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM character_candidates
                    WHERE character_id = ?
                    ORDER BY created_at DESC, ordinal
                    """,
                    (character_id,),
                ).fetchall()
        return [_to_candidate(row) for row in rows]

    def get_base(self, character_id: str) -> CandidateRead:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM character_candidates
                WHERE character_id = ? AND is_base = 1
                ORDER BY updated_at DESC LIMIT 1
                """,
                (character_id,),
            ).fetchone()
        if row is None:
            raise CandidateSelectionError("先に人物候補から基準画像を選んでください")
        return _to_candidate(row)

    def select_base(self, candidate_id: str) -> CandidateRead:
        candidate = self.get(candidate_id)
        if candidate.state != "completed" or candidate.image is None:
            raise CandidateSelectionError("完成した候補画像だけを基準画像に選べます")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE character_candidates SET is_base = 0 WHERE character_id = ?",
                (candidate.character_id,),
            )
            connection.execute(
                "UPDATE character_candidates SET is_base = 1, updated_at = ? WHERE id = ?",
                (_now(), candidate_id),
            )
        return self.get(candidate_id)
