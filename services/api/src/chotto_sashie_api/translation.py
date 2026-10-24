"""指示文の日本語を英語へ直す（ComfyUI の TextGenerate を使う）。

MiniMax H3 は1本のテキストで映像と音声の両方を条件付ける。動きの指示とセリフの両方が
日本語だと、モデルは両者を区別できず、日本語の断片を順に読み上げてしまう（実機で確認）。
公式テンプレートのプロンプトが全編英語でCJK文字を含まないのは、そこに理由がある。

そこで、**セリフ以外の指示だけ**を英語へ直してから組み立てる。翻訳には ComfyUI 標準の
`TextGenerate`（`clip.generate()`）を使う。生成できるテキストエンコーダ（Gemma 4 など）を
`CLIPLoader` で読み、同じ接続先・同じ順番待ちの中で完結させる。外部サービスは使わない。

セリフは訳さない。読み上げてほしい言葉そのものなので、日本語のまま引用符の中へ入れる。

訳すものは2種類ある。動きなどの**指示**は1行の英文に、セリフの**話す人**の記述
（配置・年齢・髪）は "the young woman on the left" のような名指しの句にする。句にしないと
「〜が言う」の主語として文へ組み込めない。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from chotto_sashie_shared.comfy_client import ComfyClient, ComfyConnectionError, bind_workflow

# 生成できるテキストエンコーダの既定。手元にある Gemma 4 E4B（指示調整済み）を使う。
# `CLIPLoader` は単一ファイルなら検出したモデルで読み込み先を決めるため、type は
# Gemma 系の recipe である ltxv を指定しておけば足りる。
DEFAULT_TRANSLATION_CLIP = "gemma4_e4b_it_fp8_scaled.safetensors"
DEFAULT_TRANSLATION_CLIP_TYPE = "ltxv"
# 指示文は短い。長く続けさせると余計な説明が付くので、頭打ちを低くしておく。
TRANSLATION_MAX_LENGTH = 96

_JAPANESE = re.compile(r"[぀-ゟ゠-ヿ㐀-䶿一-鿿]")

# 出力に混じりやすい前置き・囲みを落とすための目印。
_PREFIXES = ("english:", "translation:", "output:", "answer:")

# 何として訳すか。direction=指示の1行 / person=話す人を名指しする句。
TranslationKind = Literal["direction", "person"]


def contains_japanese(text: str) -> bool:
    """かな・漢字が含まれているか（英語で書かれていれば訳す必要がない）。"""
    return bool(_JAPANESE.search(text))


def translation_key(text: str, kind: TranslationKind = "direction") -> str:
    """同じ文を何度も訳し直さないための鍵。訳し方が違えば、同じ文でも別に覚える。"""
    source = text.strip() if kind == "direction" else f"{kind}\n{text.strip()}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TranslationSettings:
    clip_name: str = DEFAULT_TRANSLATION_CLIP
    clip_type: str = DEFAULT_TRANSLATION_CLIP_TYPE
    max_length: int = TRANSLATION_MAX_LENGTH


def build_instruction(text: str, kind: TranslationKind = "direction") -> str:
    """1文を訳させる指示。余計な説明を返させないところまで含めて書く。"""
    if kind == "person":
        return (
            "Turn the Japanese description of a person below into one short English noun "
            "phrase that picks out that person on screen, starting with \"the\". "
            "Use only the details given. "
            "Output the noun phrase and nothing else: no sentence, no quotes, no name, "
            "no Japanese.\n\n"
            f"Japanese: {text.strip()}\n"
            "English:"
        )
    return (
        "Translate the Japanese direction below into one short English line for a "
        "video generation prompt. Describe only what is visible on screen. "
        "Output the English line and nothing else: no quotes, no notes, no Japanese.\n\n"
        f"Japanese: {text.strip()}\n"
        "English:"
    )


def clean_output(generated: str) -> str:
    """生成された文から、前置き・引用符・改行以降の余談を落とす。

    指示で禁じても前置きが付くことがあるため、受け取る側でも整える。訳せなかった
    （日本語が残っている・空）ときは空文字を返し、呼び出し側が元の文へ戻せるようにする。
    """
    text = generated.strip()
    # 思考の痕跡が混ざる場合に備えて落とす。
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    # 最初の意味のある行だけを採る。
    for line in (segment.strip() for segment in text.splitlines()):
        if not line:
            continue
        lowered = line.lower()
        for prefix in _PREFIXES:
            if lowered.startswith(prefix):
                line = line[len(prefix):].strip()
                break
        line = line.strip().strip('"“”').strip()
        if not line or contains_japanese(line):
            return ""
        return line
    return ""


def build_translation_workflow(
    template: dict[str, Any],
    text: str,
    settings: TranslationSettings,
    kind: TranslationKind = "direction",
) -> dict[str, Any]:
    return bind_workflow(
        template,
        {
            "prompt": build_instruction(text, kind),
            "max_length": settings.max_length,
            "clip_name": settings.clip_name,
            "clip_type": settings.clip_type,
        },
    )


def extract_generated_text(history_entry: dict[str, Any]) -> str:
    """`PreviewAny` の出力（履歴に載る文字列）を取り出す。

    ファイルを書かないので、既存ComfyUIの出力フォルダを汚さない。その代わり画像用の
    取り出し（`output_files`）では拾えないため、ここで履歴を直接読む。
    """
    outputs = history_entry.get("outputs", {})
    if not isinstance(outputs, dict):
        return ""
    for node_output in outputs.values():
        if not isinstance(node_output, dict):
            continue
        for value in node_output.values():
            if isinstance(value, list) and value and isinstance(value[0], str):
                return value[0]
    return ""


class TranslationUnavailableError(RuntimeError):
    """翻訳できない（モデルが無い・接続できない等）。元の文のまま進める合図。"""


async def translate_once(
    client: ComfyClient,
    template: dict[str, Any],
    text: str,
    settings: TranslationSettings,
    *,
    wait_for: Any,
    kind: TranslationKind = "direction",
) -> str:
    """1文を英語へ直す。訳せなければ `TranslationUnavailableError`。

    `wait_for` は「プロンプトIDを渡すと履歴を返す」待ち関数。監視の作法は呼び出し側の
    事情（ジョブの進捗や中止）で変わるため、ここには持ち込まない。
    """
    workflow = build_translation_workflow(template, text, settings, kind)
    try:
        prompt_id = await client.submit(workflow, translation_key(text, kind)[:32])
    except ComfyConnectionError as error:
        raise TranslationUnavailableError(str(error)) from error
    entry = await wait_for(prompt_id)
    if entry is None:
        raise TranslationUnavailableError("翻訳の待機がタイムアウトしました")
    status = entry.get("status", {})
    if isinstance(status, dict) and status.get("status_str") != "success":
        raise TranslationUnavailableError("ComfyUIで翻訳できませんでした")
    cleaned = clean_output(extract_generated_text(entry))
    if not cleaned:
        raise TranslationUnavailableError("翻訳結果を読み取れませんでした")
    return cleaned


TRANSLATION_TABLE_DDL = """
-- 指示文の翻訳結果。同じ文を何度も訳し直さないために覚えておく
-- （送信内容の下見は画面を開くたびに走るので、都度生成させない）。
CREATE TABLE IF NOT EXISTS prompt_translations (
    source_hash TEXT PRIMARY KEY,
    source_text TEXT NOT NULL,
    translated_text TEXT NOT NULL,
    clip_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""


class TranslationCache:
    """訳した文を覚えておく。翻訳エンジンが使えないときは、覚えた分だけでも使う。"""

    def __init__(self, database):  # noqa: ANN001 - Database
        self.database = database

    def get(self, text: str, kind: TranslationKind = "direction") -> str:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT translated_text FROM prompt_translations WHERE source_hash = ?",
                (translation_key(text, kind),),
            ).fetchone()
        return row["translated_text"] if row else ""

    def put(
        self,
        text: str,
        translated: str,
        clip_name: str,
        timestamp: str,
        kind: TranslationKind = "direction",
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO prompt_translations(
                    source_hash, source_text, translated_text, clip_name, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_hash) DO UPDATE SET
                    translated_text = excluded.translated_text,
                    clip_name = excluded.clip_name,
                    created_at = excluded.created_at
                """,
                (translation_key(text, kind), text.strip(), translated, clip_name, timestamp),
            )


def translation_workflow_path(resources_root: Path) -> Path:
    return resources_root / "workflows" / "text" / "translate_to_english.json"
