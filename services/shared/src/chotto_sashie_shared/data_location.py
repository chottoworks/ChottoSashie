r"""保存先を変える（今のデータを移す／新しく始める／そこにあるものを使う）。

APIは自分のDBを掴んだままなので、掴んだ本人が自分の足元を移すことはできない。
そこでこの処理は**APIを止めた状態で**、デスクトップ版が単発の子プロセスとして呼ぶ。

    python -m chotto_sashie_shared.data_location --from <今> --to <新しい場所> --mode migrate

やることは3つ。

- **下見**（`plan`）: 何がどれだけあるか、その場所へ移せるか、どの選び方ができるか
- **移動**: 同じドライブなら付け替え（一瞬）、別ドライブなら複製してから元を消す
- **書き換え**: DBに残る絶対パスを新しい場所へ直す

標準出力は1行1件のJSONで、途中経過（``{"progress": {...}}``）が先に流れ、**最後の1行**が
結果（または ``{"error": ...}``）になる。複製は数十GBに及ぶため、終わるまで黙っていると
画面には止まったのか進んでいるのか出せない。

フォルダを移すだけでは足りない。生成した画像・動画の置き場所は絶対パスで
DBに入っているため、書き換えないと「記録はあるのに開けない」状態になる。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .shared_env import DATA_FOLDER_NAME

# 保存先の中で、これがあれば「すでにこのアプリのデータが入っている」とみなす。
DATABASE_RELATIVE = Path("database") / "app.sqlite3"

MODE_MIGRATE = "migrate"
MODE_FRESH = "fresh"
MODE_ADOPT = "adopt"


# 複製のとき、この大きさずつ読み書きする。1ファイルが数GBあっても途中経過が動く。
COPY_CHUNK_BYTES = 4 * 1024 * 1024
# 途中経過を出す間隔。細かく出しても画面は追えず、行が増えるだけになる。
PROGRESS_INTERVAL_SECONDS = 0.15


class DataLocationError(RuntimeError):
    """保存先を変えられない（変えると今のデータを壊す）。"""


class ProgressReporter:
    """途中経過を1行1件のJSONで流す（``{"progress": {...}}``）。

    別ドライブへの移動は中身を複製するため、数十GBあれば何分もかかる。何も出さないと
    画面は「この内容で変える」を押したまま黙るので、止まったのか進んでいるのか
    分からない。行の出しどころは標準出力で、最後の1行だけが結果になる。
    """

    def __init__(self, stream=None, interval_seconds: float = PROGRESS_INTERVAL_SECONDS) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._interval = interval_seconds
        self.phase = "scan"
        self.current: str | None = None
        self.total_files = 0
        self.total_bytes = 0
        self.copied_files = 0
        self.copied_bytes = 0
        self._last_sent = 0.0

    def begin(self, phase: str, *, total_files: int = 0, total_bytes: int = 0) -> None:
        """段階が変わったことを伝える（数が分かる段階なら全体量も）。"""
        self.phase = phase
        self.current = None
        if total_files or total_bytes:
            self.total_files = total_files
            self.total_bytes = total_bytes
        self._emit(force=True)

    def start_file(self, relative: str) -> None:
        self.current = relative
        self._emit()

    def advance(self, *, files: int = 0, copied_bytes: int = 0) -> None:
        self.copied_files += files
        self.copied_bytes += copied_bytes
        self._emit()

    def flush(self) -> None:
        """いま分かっている数を、間隔を待たずに出す（段階の終わりで使う）。"""
        self._emit(force=True)

    def _emit(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_sent < self._interval:
            return
        self._last_sent = now
        payload = {
            "progress": {
                "phase": self.phase,
                "current": self.current,
                "total_files": self.total_files,
                "total_bytes": self.total_bytes,
                "copied_files": self.copied_files,
                "copied_bytes": self.copied_bytes,
            }
        }
        print(json.dumps(payload, ensure_ascii=False), file=self._stream, flush=True)


@dataclass(frozen=True)
class DirectoryFacts:
    """あるフォルダの中身の見立て。"""

    exists: bool = False
    # このアプリのデータが入っているか（DBの有無で見る）。
    has_data: bool = False
    file_count: int = 0
    total_bytes: int = 0
    # 中身が無い（または存在しない）。移し先として使える。
    is_empty: bool = True


@dataclass(frozen=True)
class MovePlan:
    """今の場所と移し先を見比べた結果。画面へそのまま出せる形にする。"""

    source: str
    # 実際に使う場所。選んだフォルダにほかのファイルがあるときは、その中の
    # `ChottoSashie` になる（`requested` と違うことがある）。
    target: str
    source_facts: DirectoryFacts
    target_facts: DirectoryFacts
    same_drive: bool
    # 移し先のドライブの空き（読めなければ None）。
    target_free_bytes: int | None
    # 選べるやり方（migrate / fresh / adopt）。
    available_modes: list[str] = field(default_factory=list)
    # 選べないやり方と、その理由。
    blocked: dict[str, str] = field(default_factory=dict)
    # 利用者が選んだフォルダ。
    requested: str = ""

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "target": self.target,
            "requested": self.requested or self.target,
            "source_facts": vars(self.source_facts),
            "target_facts": vars(self.target_facts),
            "same_drive": self.same_drive,
            "target_free_bytes": self.target_free_bytes,
            "available_modes": self.available_modes,
            "blocked": self.blocked,
        }


def inspect_directory(path: Path) -> DirectoryFacts:
    """そのフォルダに何が入っているかを数える。"""
    try:
        if not path.is_dir():
            return DirectoryFacts()
    except OSError:
        return DirectoryFacts()
    count = 0
    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                count += 1
                try:
                    total += entry.stat().st_size
                except OSError:
                    continue
    except OSError:
        pass
    return DirectoryFacts(
        exists=True,
        has_data=(path / DATABASE_RELATIVE).is_file(),
        file_count=count,
        total_bytes=total,
        is_empty=count == 0,
    )


def _drive_of(path: Path) -> str:
    try:
        return path.resolve().drive.lower()
    except OSError:
        return path.drive.lower()


def _free_bytes(path: Path) -> int | None:
    """まだ無いフォルダでも、実在する親までさかのぼって空きを見る。"""
    existing = path
    try:
        while not existing.exists() and existing.parent != existing:
            existing = existing.parent
        return shutil.disk_usage(existing).free
    except OSError:
        return None


def resolve_target(chosen: Path) -> Path:
    """選ばれたフォルダから、実際に保存先にする場所を決める。

    空のフォルダか、このアプリのデータが入ったフォルダならそのまま使う。ほかのファイルが
    入っていれば、その中に `ChottoSashie` を作って使う——ドキュメントのような普段使いの
    フォルダを選んでも、そこへ直にばらまかず、自分のフォルダにまとめるため。
    """
    chosen = chosen.expanduser()
    if (chosen / DATABASE_RELATIVE).is_file() or not _has_any_file(chosen):
        return chosen
    return chosen / DATA_FOLDER_NAME


def _has_any_file(path: Path) -> bool:
    """1つでもファイルがあるか。全部は数えない（ドライブ丸ごとを選ばれても待たせない）。"""
    try:
        return any(entry.is_file() for entry in path.rglob("*"))
    except OSError:
        return False


def plan_move(source: Path, target: Path) -> MovePlan:
    """今の場所と移し先を見比べ、どの選び方ができるかまで返す。"""
    source = source.expanduser()
    requested = target.expanduser()
    target = resolve_target(requested)
    source_facts = inspect_directory(source)
    target_facts = inspect_directory(target)
    same_drive = _drive_of(source) == _drive_of(target)
    free = _free_bytes(target)

    available: list[str] = []
    blocked: dict[str, str] = {}

    if _same_place(target, source):
        reason = "いまの保存先と同じ場所です"
        return MovePlan(
            source=str(source),
            target=str(target),
            requested=str(requested),
            source_facts=source_facts,
            target_facts=target_facts,
            same_drive=same_drive,
            target_free_bytes=free,
            blocked={MODE_MIGRATE: reason, MODE_FRESH: reason, MODE_ADOPT: reason},
        )

    if _is_inside(target, source):
        reason = "今の保存先の中は選べません。別の場所を選んでください"
        return MovePlan(
            source=str(source),
            target=str(target),
            requested=str(requested),
            source_facts=source_facts,
            target_facts=target_facts,
            same_drive=same_drive,
            target_free_bytes=free,
            blocked={MODE_MIGRATE: reason, MODE_FRESH: reason, MODE_ADOPT: reason},
        )

    if target_facts.has_data:
        # すでに作品が入っている場所。上書きも移動もせず、そのまま使う道だけ残す。
        available.append(MODE_ADOPT)
        message = "移し先にすでに作品があります。そのまま使うことはできます"
        blocked[MODE_MIGRATE] = message
        blocked[MODE_FRESH] = message
        return MovePlan(
            source=str(source),
            target=str(target),
            requested=str(requested),
            source_facts=source_facts,
            target_facts=target_facts,
            same_drive=same_drive,
            target_free_bytes=free,
            available_modes=available,
            blocked=blocked,
        )

    if not target_facts.is_empty:
        # 選んだフォルダの中の ChottoSashie にまで、ほかのファイルが入っている。
        message = (
            f"「{target}」にほかのファイルが入っています。"
            "空のフォルダか、新しいフォルダを選んでください"
        )
        blocked[MODE_MIGRATE] = message
        blocked[MODE_FRESH] = message
        return MovePlan(
            source=str(source),
            target=str(target),
            requested=str(requested),
            source_facts=source_facts,
            target_facts=target_facts,
            same_drive=same_drive,
            target_free_bytes=free,
            blocked=blocked,
        )

    available.append(MODE_FRESH)
    if not source_facts.has_data:
        blocked[MODE_MIGRATE] = "今の保存先にまだ作品がありません"
    elif not same_drive and free is not None and free < source_facts.total_bytes:
        blocked[MODE_MIGRATE] = "移し先の空き容量が足りません"
    else:
        available.append(MODE_MIGRATE)
    return MovePlan(
        source=str(source),
        target=str(target),
        requested=str(requested),
        source_facts=source_facts,
        target_facts=target_facts,
        same_drive=same_drive,
        target_free_bytes=free,
        available_modes=available,
        blocked=blocked,
    )


def _same_place(left: Path, right: Path) -> bool:
    try:
        return left.expanduser().resolve() == right.expanduser().resolve()
    except OSError:
        return False


def _is_inside(path: Path, root: Path) -> bool:
    try:
        return path.expanduser().resolve().is_relative_to(root.expanduser().resolve())
    except (OSError, ValueError):
        return False


def rewrite_database_paths(database: Path, old_root: Path, new_root: Path) -> int:
    r"""DBに残る古い絶対パスを、新しい保存先へ直す。書き換えた行数を返す。

    JSON列（ジョブの控えや再現情報）には区切りがエスケープされた形（``C:\\Users\\…``）で
    入っているため、素の表記と両方を置き換える。前方一致には絞らない——JSONの中では
    値の途中に現れる。
    """
    plain_old, plain_new = str(old_root), str(new_root)
    replacements = [(plain_old, plain_new)]
    json_old = json.dumps(plain_old)[1:-1]
    if json_old != plain_old:
        replacements.append((json_old, json.dumps(plain_new)[1:-1]))

    changed = 0
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        for table, column in _text_columns(connection):
            for old, new in replacements:
                cursor = connection.execute(
                    f'UPDATE "{table}" SET "{column}" = replace("{column}", ?, ?) '
                    f'WHERE "{column}" LIKE ?',
                    (old, new, f"%{old}%"),
                )
                changed += cursor.rowcount if cursor.rowcount > 0 else 0
        connection.commit()
    finally:
        connection.close()
    return changed


def _text_columns(connection: sqlite3.Connection) -> list[tuple[str, str]]:
    """全ての表の文字列らしい列。パスはこのどこかに入っている。"""
    columns: list[tuple[str, str]] = []
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    ]
    for table in tables:
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall():
            declared = (row[2] or "").upper()
            if declared == "" or "CHAR" in declared or "TEXT" in declared or "CLOB" in declared:
                columns.append((table, row[1]))
    return columns


def copy_tree(source: Path, target: Path, progress: ProgressReporter | None = None) -> None:
    """中身を1ファイルずつ複製する。

    `shutil.copytree` を使わないのは、終わるまで何も返さないため。数十GBを別ドライブへ
    写す間、画面に出せる材料が要る。読み書きも分けて、大きな1ファイルの途中でも進む。
    """
    target.mkdir(parents=True, exist_ok=True)
    for entry in sorted(source.rglob("*")):
        destination = target / entry.relative_to(source)
        if entry.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if progress:
            progress.start_file(entry.relative_to(source).as_posix())
        _copy_file(entry, destination, progress)
        if progress:
            progress.advance(files=1)
    if progress:
        # 写し終わりは必ず出す。間隔待ちで最後の一伸びが落ちると、画面が
        # 「あと少し」のまま次の段階へ飛ぶ。
        progress.flush()


def _copy_file(source: Path, destination: Path, progress: ProgressReporter | None) -> None:
    with source.open("rb") as reader, destination.open("wb") as writer:
        while True:
            chunk = reader.read(COPY_CHUNK_BYTES)
            if not chunk:
                break
            writer.write(chunk)
            if progress:
                progress.advance(copied_bytes=len(chunk))
    shutil.copystat(source, destination)


def move_data_directory(
    source: Path,
    target: Path,
    source_facts: DirectoryFacts | None = None,
    progress: ProgressReporter | None = None,
) -> None:
    """保存先を移す。同じドライブなら付け替え、別ドライブなら複製してから元を消す。

    別ドライブのときに元をすぐ消さないのは、途中で失敗したときに何も残らなくなるため。
    数と大きさが一致したことを確かめてから消す。

    `source_facts` は下見で数え終えた結果。渡されれば数え直さない（大きな保存先では
    数えるだけでも時間がかかるため）。
    """
    source = source.expanduser()
    target = target.expanduser()
    if _drive_of(source) == _drive_of(target):
        if progress:
            progress.begin("relink")
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
        return

    before = source_facts or inspect_directory(source)
    if progress:
        progress.begin("copy", total_files=before.file_count, total_bytes=before.total_bytes)
    copy_tree(source, target, progress)
    if progress:
        progress.begin("verify")
    after = inspect_directory(target)
    if after.file_count != before.file_count or after.total_bytes != before.total_bytes:
        raise DataLocationError(
            "移し先の中身が元と一致しません。元のデータはそのまま残してあります"
            f"（元 {before.file_count}件 {before.total_bytes:,}バイト / "
            f"先 {after.file_count}件 {after.total_bytes:,}バイト）"
        )
    if progress:
        progress.begin("cleanup")
    shutil.rmtree(source)


def apply_change(
    source: Path, target: Path, mode: str, progress: ProgressReporter | None = None
) -> dict:
    """選ばれたやり方で保存先を変える。結果を画面へ返せる形で返す。"""
    if progress:
        # 数えるところから時間がかかる（保存先の中身を全部たどる）。
        progress.begin("scan")
    plan = plan_move(source, target)
    if mode not in plan.available_modes:
        reason = plan.blocked.get(mode, "この選び方はできません")
        raise DataLocationError(reason)
    # 選んだフォルダの中に ChottoSashie を作るときは、そちらへ移す。
    target = Path(plan.target)

    if mode == MODE_ADOPT:
        return {"mode": mode, "moved_files": 0, "rewritten_rows": 0}
    if mode == MODE_FRESH:
        target.expanduser().mkdir(parents=True, exist_ok=True)
        return {"mode": mode, "moved_files": 0, "rewritten_rows": 0}

    moved = plan.source_facts.file_count
    move_data_directory(source, target, plan.source_facts, progress)
    if progress:
        progress.begin("rewrite")
    database = target.expanduser() / DATABASE_RELATIVE
    rewritten = (
        rewrite_database_paths(database, source.expanduser().resolve(), target.expanduser().resolve())
        if database.is_file()
        else 0
    )
    if progress:
        progress.begin("done")
    return {"mode": mode, "moved_files": moved, "rewritten_rows": rewritten}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="保存先を変える（APIを止めた状態で呼ぶ）")
    parser.add_argument("--from", dest="source", required=True)
    parser.add_argument("--to", dest="target", required=True)
    parser.add_argument("--mode", choices=[MODE_MIGRATE, MODE_FRESH, MODE_ADOPT], required=True)
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="下見だけして、できることをJSONで返す（何も変えない）",
    )
    arguments = parser.parse_args(argv)
    source = Path(arguments.source)
    target = Path(arguments.target)

    try:
        if arguments.plan_only:
            payload = plan_move(source, target).as_dict()
        else:
            # 途中経過は1行ずつ先に流れ、結果はこのあとの最後の1行になる。
            payload = apply_change(source, target, arguments.mode, ProgressReporter())
    except (DataLocationError, OSError, sqlite3.Error) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
