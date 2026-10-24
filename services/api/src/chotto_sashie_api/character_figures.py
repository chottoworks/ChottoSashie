"""立ち絵: その人物の全身・素背景の1枚。

人物の実体はこれで、合成はここから選んだ1枚を背景と組み合わせる。学習した
モデル（LoRA）は使わない。作り方は2つある。

1. 基準画像から作る（このファイル。Qwen-Image-Edit-2511＋角度LoRAで正面全身へ組み直す）
2. 既にある立ち絵の衣装・小物を変える（立ち絵の変更。`character_figure_edits`）

**画像の持ち主はこの記録**である。静止画は立ち絵をidで参照するだけで、実体は持たない。
片付けてよいのはここだけにしておかないと、静止画を消したときに他が使っている立ち絵まで
巻き添えで消える（カットの起点フレームで同じ取り違えを踏んでいる）。
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from sqlite3 import Row
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

from chotto_sashie_shared.comfy_client import ComfyClient, ComfyConnectionError, output_images
from chotto_sashie_shared.comfy_scratch import ComfyScratch
from chotto_sashie_shared.job_control import CANCEL_MESSAGE
from chotto_sashie_shared.jobs import (
    InvalidTransitionError,
    JobNotFoundError,
    JobRepository,
    JobTransition,
)
from chotto_sashie_shared.angle_prompts import FULL_BODY_FRONT_PROMPT

if TYPE_CHECKING:  # スキーマ定義をここに置くので、DBの型は実行時に取り込まない（循環参照）。
    from .database import Database

# 立ち絵の作り方（origin）。
ORIGIN_BASE = "base"  # 基準画像から作った
ORIGIN_VARIATION = "variation"  # 別の立ち絵の衣装・小物を変えて作った（立ち絵の変更）
ORIGIN_UPLOAD = "upload"  # 手持ちの画像を登録した

CHARACTER_FIGURES_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS character_figures (
    id TEXT PRIMARY KEY,
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    -- 利用者が付ける見出し（「制服」「コートと鞄」）。生成の指示文へは渡さない
    -- （素材の名前を渡すと、その名前が絵の中に文字として描かれる）。
    label TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL CHECK (origin IN ('base', 'variation', 'upload')),
    -- base のとき、元にした基準画像。候補の回を消しても立ち絵は残す。
    base_candidate_id TEXT REFERENCES character_candidates(id) ON DELETE SET NULL,
    -- 別の立ち絵から作ったとき、元にした立ち絵。元を消しても立ち絵は残す。
    source_figure_id TEXT REFERENCES character_figures(id) ON DELETE SET NULL,
    -- 立ち絵の変更で使った小物の記録（再生成の材料であって、持ち物ではない）。
    prop_asset_ids_json TEXT NOT NULL DEFAULT '[]',
    -- 利用者が書いた指示（立ち絵の変更で使う）。基準画像から作る分は空。
    instruction TEXT NOT NULL DEFAULT '',
    -- 実際にComfyUIへ送った文。あとから同じ条件を再現するために残す。
    prompt TEXT NOT NULL DEFAULT '',
    seed INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'completed', 'failed')),
    -- 1回の生成をまとめる印。
    job_id TEXT NOT NULL DEFAULT '',
    -- 人物の中での並び（左から1, 2, …）。新しい立ち絵は末尾、複製は元のすぐ右へ入り、
    -- 利用者が並べ替えられる。誰も既定を選んでいないときは先頭の1枚が既定になる。
    ordinal INTEGER NOT NULL DEFAULT 1 CHECK (ordinal >= 1),
    base_url TEXT NOT NULL DEFAULT '',
    prompt_id TEXT NOT NULL DEFAULT '',
    image_path TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    -- 何も選ばれていないときに使う1枚。人物ごとに1つだけ立てる。
    is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_character_figures_character_ordinal
ON character_figures(character_id, ordinal);
"""


def figures_dir(data_dir: Path) -> Path:
    """立ち絵の実体の置き場所。人物の代表写真とも、生成の控えとも別に持つ。"""
    return data_dir / "assets" / "character-figures"


def figure_image_path(data_dir: Path, figure_id: str) -> Path:
    """記録IDから実体の場所を決める（IDはUUIDなので経路の細工は起きない）。"""
    safe_id = figure_id.replace("/", "_").replace("\\", "_")
    return figures_dir(data_dir) / f"{safe_id}.png"


# 基準画像を正面全身へ組み直す指示文。角度LoRAが学習している語順そのままなので、
# ここを言い換えない（言い換えると胸像のまま返る）。
FIGURE_PROMPT = FULL_BODY_FRONT_PROMPT


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class CharacterFigureRecord:
    id: str
    character_id: str
    label: str
    origin: str
    base_candidate_id: str | None
    source_figure_id: str | None
    prop_asset_ids: list[str]
    instruction: str
    prompt: str
    seed: int
    state: str
    job_id: str
    ordinal: int
    base_url: str
    prompt_id: str
    image_path: str
    error_message: str
    is_default: bool
    created_at: str
    updated_at: str


class CharacterFigureNotFoundError(KeyError):
    pass


def _to_figure(row: Row) -> CharacterFigureRecord:
    try:
        prop_ids = json.loads(row["prop_asset_ids_json"])
    except (ValueError, TypeError):
        prop_ids = []
    return CharacterFigureRecord(
        id=row["id"],
        character_id=row["character_id"],
        label=row["label"],
        origin=row["origin"],
        base_candidate_id=row["base_candidate_id"],
        source_figure_id=row["source_figure_id"],
        prop_asset_ids=prop_ids if isinstance(prop_ids, list) else [],
        instruction=row["instruction"],
        prompt=row["prompt"],
        seed=row["seed"],
        state=row["state"],
        job_id=row["job_id"],
        ordinal=row["ordinal"],
        base_url=row["base_url"],
        prompt_id=row["prompt_id"],
        image_path=row["image_path"],
        error_message=row["error_message"],
        is_default=bool(row["is_default"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# 一覧の並び。並び位置が同じ値になるのは、位置を振る前の記録だけ
# （そのときは頼んだ順。作った時刻が同じ値になることがあるので、最後は登録した順）。
FIGURE_ORDER = "ordinal ASC, created_at ASC, rowid ASC"


def _effective_default_id(connection, character_id: str) -> str | None:  # noqa: ANN001
    """いまこの人物の既定になっている立ち絵。

    `is_default` は**利用者が選んだ1枚**の印で、誰も選んでいないうちは全部 0 になる。
    そのときは「一覧の先頭にある、出来上がっている1枚」を既定として扱う。画面で見えている
    並びと既定が食い違わないように、並べ替えで先頭へ出した1枚がそのまま既定になる。

    出来た順で自動的に印を付けてしまうと、まとめて作ったときにどれが先に仕上がるかで
    既定が変わる。生成側の都合で選択が入れ替わると、利用者からは理由の分からない
    選ばれ方に見える。だから印は保存せず、読むときに決める。
    """
    chosen = connection.execute(
        """
        SELECT id FROM character_figures
        WHERE character_id = ? AND is_default = 1 AND state = 'completed' AND image_path <> ''
        LIMIT 1
        """,
        (character_id,),
    ).fetchone()
    if chosen is not None:
        return chosen["id"]
    earliest = connection.execute(
        f"""
        SELECT id FROM character_figures
        WHERE character_id = ? AND state = 'completed' AND image_path <> ''
        ORDER BY {FIGURE_ORDER}
        LIMIT 1
        """,  # noqa: S608
        (character_id,),
    ).fetchone()
    return earliest["id"] if earliest is not None else None


def _ordered_ids(connection, character_id: str) -> list[str]:  # noqa: ANN001
    rows = connection.execute(
        f"SELECT id FROM character_figures WHERE character_id = ? ORDER BY {FIGURE_ORDER}",  # noqa: S608
        (character_id,),
    ).fetchall()
    return [row["id"] for row in rows]


def _write_order(connection, figure_ids: list[str]) -> None:  # noqa: ANN001
    """並び位置を左から 1, 2, … と振り直す。"""
    connection.executemany(
        "UPDATE character_figures SET ordinal = ? WHERE id = ?",
        [(position, figure_id) for position, figure_id in enumerate(figure_ids, start=1)],
    )


def _with_default(figure: CharacterFigureRecord, default_id: str | None) -> CharacterFigureRecord:
    """画面へ返す `is_default` を、いま効いている既定に合わせる。"""
    return replace(figure, is_default=figure.id == default_id)


class CharacterFigureRepository:
    def __init__(self, database: Database):
        self.database = database

    def create(
        self,
        character_id: str,
        *,
        origin: str,
        figure_id: str | None = None,
        job_id: str = "",
        label: str = "",
        base_candidate_id: str | None = None,
        source_figure_id: str | None = None,
        prop_asset_ids: list[str] | None = None,
        instruction: str = "",
        prompt: str = "",
        seed: int = 0,
        base_url: str = "",
        state: str = "queued",
        image_path: str = "",
    ) -> CharacterFigureRecord:
        """立ち絵の記録を作る。並びは末尾（いちばん右）。"""
        # 手持ちの画像を取り込むときは、ファイル名を先に決めてから記録を作る
        # （記録だけ先に作ると、取り込みに失敗したときに消せない行が残る）。
        figure_id = figure_id or str(uuid4())
        timestamp = _now()
        with self.database.connect() as connection:
            # 末尾の位置は同じ文の中で決める（別の文で読むと、同時に作った分と重なる）。
            connection.execute(
                """
                INSERT INTO character_figures(
                    id, character_id, label, origin, base_candidate_id, source_figure_id,
                    prop_asset_ids_json, instruction, prompt, seed, state, job_id, ordinal,
                    base_url, image_path, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    (SELECT COALESCE(MAX(ordinal), 0) + 1 FROM character_figures
                     WHERE character_id = ?),
                    ?, ?, ?, ?
                )
                """,
                (
                    figure_id,
                    character_id,
                    label,
                    origin,
                    base_candidate_id,
                    source_figure_id,
                    json.dumps(prop_asset_ids or [], ensure_ascii=False),
                    instruction,
                    prompt,
                    seed,
                    state,
                    job_id,
                    character_id,
                    base_url,
                    image_path,
                    timestamp,
                    timestamp,
                ),
            )
        return self.get(figure_id)

    def get(self, figure_id: str) -> CharacterFigureRecord:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM character_figures WHERE id = ?", (figure_id,)
            ).fetchone()
            if row is None:
                raise CharacterFigureNotFoundError(figure_id)
            default_id = _effective_default_id(connection, row["character_id"])
        return _with_default(_to_figure(row), default_id)

    def list_for_character(self, character_id: str) -> list[CharacterFigureRecord]:
        """利用者が決めた並び（左から）で返す。"""
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM character_figures WHERE character_id = ? ORDER BY {FIGURE_ORDER}",  # noqa: S608
                (character_id,),
            ).fetchall()
            default_id = _effective_default_id(connection, character_id)
        return [_with_default(_to_figure(row), default_id) for row in rows]

    def completed_count(self, character_id: str) -> int:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM character_figures
                WHERE character_id = ? AND state = 'completed' AND image_path <> ''
                """,
                (character_id,),
            ).fetchone()
        return int(row["count"]) if row else 0

    def default_figure(self, character_id: str) -> CharacterFigureRecord | None:
        """合成で何も選ばれていないときに使う1枚。判定・表示・生成はここを共有する。"""
        with self.database.connect() as connection:
            default_id = _effective_default_id(connection, character_id)
            if default_id is None:
                return None
            row = connection.execute(
                "SELECT * FROM character_figures WHERE id = ?", (default_id,)
            ).fetchone()
        return _with_default(_to_figure(row), default_id) if row else None

    def mark_running(self, figure_id: str, prompt_id: str) -> CharacterFigureRecord:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE character_figures
                SET state = 'running', prompt_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (prompt_id, _now(), figure_id),
            )
        return self.get(figure_id)

    def complete(self, figure_id: str, image_path: Path) -> CharacterFigureRecord:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE character_figures
                SET state = 'completed', image_path = ?, error_message = '', updated_at = ?
                WHERE id = ?
                """,
                (str(image_path), _now(), figure_id),
            )
        return self.get(figure_id)

    def fail(self, figure_id: str, message: str) -> CharacterFigureRecord:
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE character_figures SET state = 'failed', error_message = ?, updated_at = ? WHERE id = ?",
                (message[:1000], _now(), figure_id),
            )
        return self.get(figure_id)

    def fail_batch(self, job_id: str, message: str) -> int:
        """この回でまだ終わっていない立ち絵をまとめて失敗にする。"""
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE character_figures
                SET state = 'failed', error_message = ?, updated_at = ?
                WHERE job_id = ? AND state IN ('queued', 'running')
                """,
                (message[:1000], _now(), job_id),
            )
        return cursor.rowcount

    def recover_interrupted(self, message: str) -> int:
        """前回終了時に生成中だった立ち絵を失敗へ倒す。

        親ジョブは `JobRepository.recover_interrupted` が中断へ倒す。こちらを忘れると、
        画面に終わらない「生成中」が残り続ける。
        """
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE character_figures
                SET state = 'failed', error_message = ?, updated_at = ?
                WHERE state IN ('queued', 'running')
                """,
                (message[:1000], _now()),
            )
        return cursor.rowcount

    def set_label(self, figure_id: str, label: str) -> CharacterFigureRecord:
        with self.database.connect() as connection:
            cursor = connection.execute(
                "UPDATE character_figures SET label = ?, updated_at = ? WHERE id = ?",
                (label, _now(), figure_id),
            )
            if cursor.rowcount != 1:
                raise CharacterFigureNotFoundError(figure_id)
        return self.get(figure_id)

    def set_default(self, figure_id: str) -> CharacterFigureRecord:
        """この1枚を既定にする。同じ人物の他の印は下ろす。"""
        figure = self.get(figure_id)
        if figure.state != "completed" or not figure.image_path:
            raise ValueError("まだ出来上がっていない立ち絵は既定にできません")
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE character_figures SET is_default = 0, updated_at = ? WHERE character_id = ?",
                (_now(), figure.character_id),
            )
            connection.execute(
                "UPDATE character_figures SET is_default = 1, updated_at = ? WHERE id = ?",
                (_now(), figure_id),
            )
        return self.get(figure_id)

    def reorder(self, character_id: str, figure_ids: list[str]) -> list[CharacterFigureRecord]:
        """この人物の立ち絵を、渡された順（左から）に並べ替える。

        渡すのはこの人物の立ち絵すべて。画面が古い一覧を見ている間に増減していたら、
        どこへ入れるつもりだったのか決められないので断る（`ValueError`）。
        """
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = _ordered_ids(connection, character_id)
            if len(figure_ids) != len(set(figure_ids)) or set(figure_ids) != set(current):
                raise ValueError("立ち絵の一覧が変わっています。開き直してから並べ替えてください")
            _write_order(connection, figure_ids)
        return self.list_for_character(character_id)

    def duplicate(self, figure_id: str, *, data_dir: Path) -> CharacterFigureRecord:
        """立ち絵をそのまま複製し、元のすぐ右へ置く。画像も写して、新しい記録の持ち物にする。

        作り方の記録（元・指示・seed など）はそのまま写す。見出しには「（複製）」を
        添え、既定の印は付けない（勝手に付け替えない）。画像を先に書いてから記録を作り、
        記録を作れなかったら画像も消す。
        """
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM character_figures WHERE id = ?", (figure_id,)
            ).fetchone()
        if row is None:
            raise CharacterFigureNotFoundError(figure_id)
        source_path = Path(row["image_path"]) if row["image_path"] else None
        if row["state"] != "completed" or source_path is None or not source_path.is_file():
            raise ValueError("まだ出来ていない立ち絵は複製できません")

        new_id = str(uuid4())
        destination = figures_dir(data_dir) / f"{new_id}{source_path.suffix or '.png'}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        try:
            shutil.copyfile(source_path, temporary)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

        timestamp = _now()
        label = f"{row['label']}（複製）" if row["label"] else ""
        try:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                order = _ordered_ids(connection, row["character_id"])
                connection.execute(
                    """
                    INSERT INTO character_figures(
                        id, character_id, label, origin, base_candidate_id, source_figure_id,
                        prop_asset_ids_json, instruction, prompt, seed, state, job_id, ordinal,
                        base_url, prompt_id, image_path, error_message, is_default,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, '', 0, ?, ?)
                    """,
                    (
                        new_id,
                        row["character_id"],
                        label,
                        row["origin"],
                        row["base_candidate_id"],
                        row["source_figure_id"],
                        row["prop_asset_ids_json"],
                        row["instruction"],
                        row["prompt"],
                        row["seed"],
                        row["job_id"],
                        len(order) + 1,  # すぐ下で元の右へ並べ直す
                        row["base_url"],
                        row["prompt_id"],
                        str(destination),
                        timestamp,
                        timestamp,
                    ),
                )
                # 元が直前に消えていたら、末尾に置く。
                position = order.index(figure_id) + 1 if figure_id in order else len(order)
                _write_order(connection, [*order[:position], new_id, *order[position:]])
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return self.get(new_id)

    def delete(self, figure_id: str) -> Path | None:
        """記録を消し、実体の場所を返す（ファイルは呼び出し側が片付ける）。

        既定の繰り上げはここでは行わない。選ばれていた1枚が消えれば、残りの
        先頭の1枚が自動的に既定になる（`_effective_default_id`）。
        """
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT image_path, state FROM character_figures WHERE id = ?",
                (figure_id,),
            ).fetchone()
            if row is None:
                raise CharacterFigureNotFoundError(figure_id)
            if row["state"] in ("queued", "running"):
                raise ValueError("生成中の立ち絵は削除できません")
            image_path = row["image_path"]
            connection.execute("DELETE FROM character_figures WHERE id = ?", (figure_id,))
        return Path(image_path) if image_path else None

    def image_paths_for_character(self, character_id: str) -> list[Path]:
        """人物を消すときに片付ける実体の一覧。"""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT image_path FROM character_figures WHERE character_id = ?",
                (character_id,),
            ).fetchall()
        return [Path(row["image_path"]) for row in rows if row["image_path"]]

    def image_paths_for_project(self, project_id: str) -> list[Path]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT f.image_path AS image_path FROM character_figures f
                JOIN characters c ON c.id = f.character_id
                WHERE c.project_id = ?
                """,
                (project_id,),
            ).fetchall()
        return [Path(row["image_path"]) for row in rows if row["image_path"]]


class FigureImageRecords(Protocol):
    """出来た1枚を受け取る記録（立ち絵、または変更の結果）。"""

    def get(self, record_id: str) -> Any: ...

    def complete(self, record_id: str, image_path: Path) -> Any: ...

    def fail(self, record_id: str, message: str) -> Any: ...


@dataclass(frozen=True)
class FigureJobWording:
    """進み具合の文言。立ち絵を作るときと変更するときで言い分ける。"""

    working: str = "立ち絵を作っています"
    finished: str = "立ち絵を作りました"
    failed: str = "立ち絵を作れませんでした"


async def monitor_figure_image(
    job_id: str,
    record_id: str,
    base_url: str,
    *,
    records: FigureImageRecords,
    destination: Path,
    jobs: JobRepository,
    scratch: ComfyScratch | None = None,
    wording: FigureJobWording = FigureJobWording(),
    poll_seconds: float = 0.5,
    max_polls: int = 3600,
) -> None:
    """立ち絵1枚の生成を見届け、出来た画像をアプリの持ち物として `destination` へ置く。

    ComfyUIのoutputを指し続けずに実体を取り込むのは、生成エンジンを入れ替えたり
    出力先が変わったりしても、合成に使う1枚が失われないようにするため。取り込んだ
    あとのComfyUI側の出力は、`scratch` があれば片付ける。
    """
    client = ComfyClient(base_url)
    try:
        jobs.transition(
            job_id, JobTransition(state="running", progress=0.1, message=wording.working)
        )
        for _ in range(max_polls):
            if jobs.is_cancelled(job_id):
                records.fail(record_id, CANCEL_MESSAGE)
                return
            record = records.get(record_id)
            entry = await client.history(record.prompt_id) if record.prompt_id else None
            status = entry.get("status", {}) if entry is not None else {}
            if not isinstance(status, dict) or not status.get("completed"):
                await asyncio.sleep(poll_seconds)
                continue
            images = output_images(entry)
            if status.get("status_str") == "success" and images:
                await _store_figure_image(records, client, destination, record_id, images[0])
            else:
                records.fail(record_id, f"ComfyUIで{wording.failed}")
            if scratch is not None:
                scratch.discard_outputs(images)
            if records.get(record_id).state != "completed":
                jobs.transition(
                    job_id,
                    JobTransition(
                        state="failed", message=wording.failed, error_code="FIGURE_FAILED"
                    ),
                )
                return
            jobs.transition(
                job_id,
                JobTransition(state="validating", progress=0.95, message="出来た画像を保存しています"),
            )
            jobs.transition(job_id, JobTransition(state="completed", message=wording.finished))
            return

        records.fail(record_id, "生成待ちがタイムアウトしました")
        jobs.transition(
            job_id,
            JobTransition(
                state="failed", message="生成待ちがタイムアウトしました", error_code="FIGURE_TIMEOUT"
            ),
        )
    except (
        KeyError,  # 待っている間に記録が消えた（人物や立ち絵の削除）
        ComfyConnectionError,
        InvalidTransitionError,
        JobNotFoundError,
        OSError,
    ) as error:
        try:
            records.fail(record_id, str(error))
        except KeyError:
            pass
        try:
            jobs.transition(
                job_id,
                JobTransition(
                    state="failed", message=str(error), error_code="FIGURE_MONITOR_FAILED"
                ),
            )
        except (InvalidTransitionError, JobNotFoundError):
            return


async def _store_figure_image(
    records: FigureImageRecords,
    client: ComfyClient,
    destination: Path,
    record_id: str,
    image: dict[str, str],
) -> None:
    """出来た1枚を取り込み、この記録の持ち物として置く。"""
    try:
        content, _media_type = await client.view_image(image)
    except ComfyConnectionError as error:
        records.fail(record_id, str(error))
        return
    try:
        await asyncio.to_thread(_write_image, destination, content)
    except OSError as error:
        records.fail(record_id, f"画像を保存できませんでした: {error}")
        return
    records.complete(record_id, destination)


def _write_image(destination: Path, content: bytes) -> None:
    """書き途中の絵を見せないよう、別名で書いてから置き換える。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        temporary.replace(destination)
    finally:
        if temporary.exists():
            temporary.unlink()
