from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from chotto_sashie_shared.character_studio import CHARACTER_CANDIDATES_TABLE_DDL
from chotto_sashie_shared.jobs import JOBS_TABLE_DDL
from chotto_sashie_shared.sqlite import SqliteDatabase
from chotto_sashie_shared.setup_config import SETUP_CONFIG_TABLE_DDL
from .character_figure_edits import CHARACTER_FIGURE_EDITS_TABLE_DDL
from .character_figures import CHARACTER_FIGURES_TABLE_DDL
from .preferences import PREFERENCES_TABLE_DDL
from .still_image_characters import STILL_IMAGE_CHARACTERS_TABLE_DDL
from .translation import TRANSLATION_TABLE_DDL


# DBの形の版。下の SCHEMA は常に最新の形を書き、新しく作るDBはこの版から始まる。
# 形を変えるときは版を1つ上げ、前の版のDBを追いつかせる手順を `_MIGRATIONS` に足す。
SCHEMA_VERSION = 4

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('schema_version', '{SCHEMA_VERSION}');

{JOBS_TABLE_DDL}

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    -- 作品全体の画風。人物候補・立ち絵・背景・小物・静止画の生成に効かせる。
    illustration_style TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- セットアップモード（単一行、3方式）。
{SETUP_CONFIG_TABLE_DDL}

-- 人物1件。共有のプロフィール列に、このアプリ固有の代表写真を足した拡張版。
-- プロフィール列は chotto_sashie_shared.character_studio と揃える（試験で確認する）。
CREATE TABLE IF NOT EXISTS characters (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    gender_age TEXT NOT NULL DEFAULT '',
    face TEXT NOT NULL DEFAULT '',
    hair_style TEXT NOT NULL DEFAULT '',
    hair_color TEXT NOT NULL DEFAULT '',
    eyes TEXT NOT NULL DEFAULT '',
    eye_color TEXT NOT NULL DEFAULT '',
    body_type TEXT NOT NULL DEFAULT '',
    base_outfit TEXT NOT NULL DEFAULT '',
    accessories TEXT NOT NULL DEFAULT '',
    personality TEXT NOT NULL DEFAULT '',
    immutable_features_json TEXT NOT NULL DEFAULT '[]',
    art_style TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    -- 一覧に出す代表写真（顔が見える1枚）。合成には使わない。
    -- 空なら既定の立ち絵を出す。
    portrait_image_path TEXT NOT NULL DEFAULT '',
    -- どの候補を代表写真に選んだか（選択中の印を出すためだけに持つ）。
    portrait_source TEXT NOT NULL DEFAULT '',
    -- 代表写真のうち一覧に出す範囲（JSON。画像の幅・高さに対する割合）。空なら全体を出す。
    portrait_crop TEXT NOT NULL DEFAULT '',
    -- 代表写真か範囲を変えるたびに1つ増やす。一覧の画像URLの版にする（時刻は続けて
    -- 変えると同じ値になりうるので、版には使わない）。
    portrait_revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_characters_project_updated
ON characters(project_id, updated_at DESC);

-- 人物候補（顔を決めるための比較用画像）。
{CHARACTER_CANDIDATES_TABLE_DDL}

-- 立ち絵: その人物の全身・素背景の1枚。合成はここから選んだ1枚を使う。
{CHARACTER_FIGURES_TABLE_DDL}

-- 立ち絵の変更の結果（保存するまで預かる1枚）。保存で立ち絵へ移すか、捨てる。
{CHARACTER_FIGURE_EDITS_TABLE_DDL}

-- 背景・小物: 固定の静止画を資産として登録する（生成・アップロードいずれも可）。
--
-- 背景は、元の1枚から「小物を置いた」「時間帯を変えた」バリエーションを派生させられる。
-- `base_asset_id` は**常にグループの元**を指す（派生の派生でも最初の背景を指す）。親を
-- たどる形にすると、一覧を出すたびに木をたどることになり、途中を消したときに群れが割れる。
CREATE TABLE IF NOT EXISTS background_assets (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    image_path TEXT NOT NULL DEFAULT '',
    -- 今の画像を作るときに ComfyUI へ渡した指示文（作品の画風を足したもの）。
    source_prompt TEXT NOT NULL DEFAULT '',
    -- 今の画像を作るときに、利用者が「生成プロンプト」欄へ入れた文。欄の初期値に出す。
    -- 画像をアップロードしたときや、背景のバリエーション（欄から作らない）では空。
    input_prompt TEXT NOT NULL DEFAULT '',
    -- NULL なら自分が元。元を消してもバリエーションは残す（それぞれが元になる）。
    base_asset_id TEXT REFERENCES background_assets(id) ON DELETE SET NULL,
    -- 元と何が違うのか（「夕方」「机に本」）。名前の括弧書きの元で、元の名前を変えたときに
    -- バリエーションの名前を付け直すのに使う（画面へは名前として出す）。
    variant_label TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_background_assets_project_updated
ON background_assets(project_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS prop_assets (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    image_path TEXT NOT NULL DEFAULT '',
    -- 今の画像を作るときに ComfyUI へ渡した指示文（作品の画風を足したもの）。
    source_prompt TEXT NOT NULL DEFAULT '',
    -- 今の画像を作るときに、利用者が「生成プロンプト」欄へ入れた文。欄の初期値に出す。
    -- 画像をアップロードしたときや、背景のバリエーション（欄から作らない）では空。
    input_prompt TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_prop_assets_project_updated
ON prop_assets(project_id, updated_at DESC);

-- 静止画: 背景1枚と人物の立ち絵を1枚へ合成したもの。
-- 挿絵としてはここが完成品で、動画を作る場合はこの1枚がシーンの起点になる。
-- 小物は合成へ渡さない（人物が持つ小物は立ち絵へ、場に置く小物は背景へ写し込む）。
CREATE TABLE IF NOT EXISTS still_images (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    name TEXT NOT NULL DEFAULT '',
    background_asset_id TEXT REFERENCES background_assets(id) ON DELETE SET NULL,
    character_ids_json TEXT NOT NULL DEFAULT '[]',
    composition_job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
    image_path TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_still_images_project_ordinal
ON still_images(project_id, ordinal);

-- 静止画ごとの人物: どの立ち絵を使うかと、姿勢・表情・向き・配置の指定。
{STILL_IMAGE_CHARACTERS_TABLE_DDL}

-- カット: 1枚の静止画から始まるシーンの、連続する動画区間。前カットの最終フレームを次カットの
-- 起点フレームへ引き継ぐことで連続性を確保する（起点画像は都度コピーして持つ）。
CREATE TABLE IF NOT EXISTS shots (
    id TEXT PRIMARY KEY,
    still_image_id TEXT NOT NULL REFERENCES still_images(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    motion_prompt TEXT NOT NULL DEFAULT '',
    -- セリフ・ナレーションは shot_speech_lines に行で持つ。
    -- 環境音・効果音の指定（音を作るのはMiniMax H3だけなので、Wanでは使わない）。
    -- H3の overall_soundscape に対応する。
    ambience_prompt TEXT NOT NULL DEFAULT '',
    -- 劇伴（画面の外で鳴る音楽）。H3の non_diegetic_music に対応し、空なら N/A を送る。
    music_prompt TEXT NOT NULL DEFAULT '',
    -- 構図・カメラの変更。前カットの最終フレームをこの指示で編集し、起点フレームを作る。
    composition_prompt TEXT NOT NULL DEFAULT '',
    -- 利用者の覚え書き。生成には一切渡さない（渡すと、書いた覚えのない指示として効く）。
    note TEXT NOT NULL DEFAULT '',
    -- 構図変更で作った起点フレーム。あれば連続性より優先して起点に使う。
    custom_start_frame_path TEXT NOT NULL DEFAULT '',
    -- 終端の絵（MiniMax H3 の last_frame へ渡す）。着地の構図を固定したいときに使う。
    custom_end_frame_path TEXT NOT NULL DEFAULT '',
    -- ビートの実現方法。single=1回で生成 / split=ビートごとに区間を生成して繋ぐ。
    timeline_mode TEXT NOT NULL DEFAULT 'single',
    -- 用意できた「生成に使う文」（区間ごとの配列をJSONで持つ）。動画生成はこれを送る。
    -- 途中の姿を画面へ出さないために、用意できたものだけをここへ書く。
    resolved_prompt_json TEXT NOT NULL DEFAULT '',
    -- その文を、どの作り方・どの指示から用意したか。ここが今と違えば作り直しが要る。
    resolved_prompt_engine TEXT NOT NULL DEFAULT '',
    resolved_prompt_signature TEXT NOT NULL DEFAULT '',
    -- 直接書き直した文（区間ごとの配列をJSON）。書き直しモードを切ると捨てる。
    edited_prompt_json TEXT NOT NULL DEFAULT '',
    -- 直接書き直しているか。1の間は、文のもとになる指示を変えさせない。
    prompt_edit_mode INTEGER NOT NULL DEFAULT 0,
    duration_seconds REAL NOT NULL DEFAULT 4.0 CHECK (duration_seconds > 0),
    start_frame_image_path TEXT NOT NULL DEFAULT '',
    video_job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
    output_video_path TEXT NOT NULL DEFAULT '',
    -- 生成した動画の最終フレーム。次カットの起点フレームに使う（連続性）。
    last_frame_image_path TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(still_image_id, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_shots_still_image_ordinal
ON shots(still_image_id, ordinal);

-- ビート: 1カットの中の時間区間。MiniMax H3 は中間キーフレームを受け付けない
-- （土台が先頭と末尾のみを許す）ため、区間ごとの指示はプロンプトの時刻ラベルとして
-- 渡すか、区間ごとに生成を分けて最終フレームで繋ぐ（timeline_mode で選ぶ）。
CREATE TABLE IF NOT EXISTS shot_beats (
    id TEXT PRIMARY KEY,
    shot_id TEXT NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    duration_seconds REAL NOT NULL DEFAULT 2.0 CHECK (duration_seconds > 0),
    motion_prompt TEXT NOT NULL DEFAULT '',
    -- 分割モードで作った、この区間の動画と最終フレーム。
    segment_video_path TEXT NOT NULL DEFAULT '',
    last_frame_image_path TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(shot_id, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_shot_beats_shot_ordinal
ON shot_beats(shot_id, ordinal);

-- セリフ・ナレーションの行。動きの指示と混ぜると読み上げられてしまうため別に持ち、
-- 行ごとに誰が話すかを持つ（話す人を決めないと、二人いる場面でどちらが話すかがモデル任せになる）。
CREATE TABLE IF NOT EXISTS shot_speech_lines (
    id TEXT PRIMARY KEY,
    shot_id TEXT NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
    -- NULL ならカット全体の行（ビートが無いときに使う）。
    beat_id TEXT REFERENCES shot_beats(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    -- 話す人。人物を消しても行は残し、話す人だけ外す（画面で選び直してもらう）。
    speaker_character_id TEXT REFERENCES characters(id) ON DELETE SET NULL,
    -- 画面の外の声。1のときは話す人を持たない。
    narration INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_shot_speech_lines_owner
ON shot_speech_lines(shot_id, beat_id, ordinal);

{TRANSLATION_TABLE_DDL}

-- 前回選んだ生成方法・枚数（この端末の使い方。作品には紐づかない）。
{PREFERENCES_TABLE_DDL}
"""


def _add_portrait_crop(connection: sqlite3.Connection) -> None:
    connection.execute(
        "ALTER TABLE characters ADD COLUMN portrait_crop TEXT NOT NULL DEFAULT ''"
    )


# 版2までのアプリが、背景・小物の指示文へ作品の画風を足すときに挟んだ区切り。
_V2_STYLE_MARKER = " 作品全体の画風: "
# 版2までのアプリが、背景のバリエーションの指示文へ必ず入れた一文。
_V2_VARIATION_SENTENCE = (
    "Keep the same place, the same layout, the same camera position and the same "
    "perspective as image 1."
)


def _add_asset_input_prompt(connection: sqlite3.Connection) -> None:
    """背景・小物に、生成プロンプト欄へ入れた文の列を足し、記録済みの指示文から埋める。

    版2のDBは ComfyUI へ渡した指示文（`source_prompt`）だけを持つ。欄から生成したものは
    「欄の文 + 区切り + 画風」か「欄の文」そのものなので、区切りより前を戻す。
    バリエーションの指示文は欄から作っていないので埋めない。
    """
    for table in ("background_assets", "prop_assets"):
        connection.execute(
            f"ALTER TABLE {table} ADD COLUMN input_prompt TEXT NOT NULL DEFAULT ''"
        )
        rows = connection.execute(
            f"SELECT id, source_prompt FROM {table} WHERE source_prompt != ''"
        ).fetchall()
        for row in rows:
            source = row["source_prompt"]
            if _V2_VARIATION_SENTENCE in source:
                continue
            cut = source.rfind(_V2_STYLE_MARKER)
            connection.execute(
                f"UPDATE {table} SET input_prompt = ? WHERE id = ?",
                (source[:cut] if cut >= 0 else source, row["id"]),
            )


def _add_portrait_revision(connection: sqlite3.Connection) -> None:
    connection.execute(
        "ALTER TABLE characters ADD COLUMN portrait_revision INTEGER NOT NULL DEFAULT 0"
    )


# 版ごとの移行手順。キーの版へ上げるために、1つ前の版のDBへ行う変更。
_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    2: _add_portrait_crop,
    3: _add_asset_input_prompt,
    4: _add_portrait_revision,
}


class Database(SqliteDatabase):
    """このアプリのDB。接続設定は共有のSqliteDatabaseに従う。"""

    def __init__(self, path: Path):
        super().__init__(path, SCHEMA)

    def initialize(self) -> None:
        super().initialize()
        self._migrate()

    def _migrate(self) -> None:
        """前の版で作られたDBを、1版ずつ今の形へ追いつかせる。

        版を上げる記録と形の変更は同じトランザクションで行う。途中で落ちても
        「形は変わったのに版は古い」状態が残らず、次の起動でその版からやり直せる。
        """
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            current = int(row["value"])
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"このDBは新しい版のアプリで作られています（DBの版 {current}、"
                f"このアプリが扱える版 {SCHEMA_VERSION}）"
            )
        for version in range(current + 1, SCHEMA_VERSION + 1):
            with self.connect() as connection:
                connection.execute("BEGIN")
                _MIGRATIONS[version](connection)
                connection.execute(
                    "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                    (str(version),),
                )
