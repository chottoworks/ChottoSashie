"""人物と人物候補（顔を決めるための比較用画像）のテーブル定義（共通の土台）。

`characters` は各アプリの `projects` を参照する。プロジェクトの列はアプリごとに
違うため、そちらは共有しない。
"""

from __future__ import annotations

# 人物1件（プロフィール）。アプリ側は代表写真などを足した拡張版を持つため、
# 列を足す場合は CHARACTER_PROFILE_COLUMNS も更新して形を揃える。
CHARACTER_TABLE_DDL = """
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
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_characters_project_updated
ON characters(project_id, updated_at DESC);
"""

# `characters` が持つプロフィール列。アプリ側の拡張版と形が揃っているかを
# 試験で確かめるときに使う。
CHARACTER_PROFILE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("gender_age", "TEXT NOT NULL DEFAULT ''"),
    ("face", "TEXT NOT NULL DEFAULT ''"),
    ("hair_style", "TEXT NOT NULL DEFAULT ''"),
    ("hair_color", "TEXT NOT NULL DEFAULT ''"),
    ("eyes", "TEXT NOT NULL DEFAULT ''"),
    ("eye_color", "TEXT NOT NULL DEFAULT ''"),
    ("body_type", "TEXT NOT NULL DEFAULT ''"),
    ("base_outfit", "TEXT NOT NULL DEFAULT ''"),
    ("accessories", "TEXT NOT NULL DEFAULT ''"),
    ("personality", "TEXT NOT NULL DEFAULT ''"),
    ("immutable_features_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("art_style", "TEXT NOT NULL DEFAULT ''"),
    ("notes", "TEXT NOT NULL DEFAULT ''"),
)

# 人物候補。顔を決めるための比較用画像で、ここから基準画像を1枚選ぶ。
CHARACTER_CANDIDATES_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS character_candidates (
    id TEXT PRIMARY KEY,
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    batch_job_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'completed', 'failed')),
    prompt TEXT NOT NULL,
    negative_prompt TEXT NOT NULL,
    seed INTEGER NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    base_url TEXT NOT NULL,
    prompt_id TEXT,
    image_json TEXT,
    is_base INTEGER NOT NULL DEFAULT 0 CHECK (is_base IN (0, 1)),
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(batch_job_id, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_character_candidates_character_created
ON character_candidates(character_id, created_at DESC, ordinal);
"""
