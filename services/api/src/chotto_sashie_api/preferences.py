"""前回選んだ生成方法・枚数を覚えておく（次に開いたときの既定値）。

画面で選ぶ「動画の作り方」「向きの作り方」「枚数」は、一度決めたら作品をまたいで
同じものを使い続けることがほとんどで、毎回選び直させる理由が無い。作品ごとの設定では
なく**この端末の使い方**なので、作品ではなくアプリの設定として1行ずつ持つ。

保存は鍵と値の表にする。項目が増えても列の追加（＝既存DBの移行）が要らず、知らない鍵が
残っていても読み飛ばせるため。値は文字列で持ち、読むときに項目ごとの型で検証する。
壊れた値が1つ入っていても、その項目だけ既定へ戻し、他の項目は生かす。

セットアップの「この環境のおすすめ」もここへ書き込む。おすすめは**適用したときだけ**
保存され、利用者があとから選び直した値を勝手に戻すことはしない。

使うモデルが変わる項目（動画の作り方・人物候補の生成モデル）は、まだ選んでいなければ
この端末へのおすすめを既定にする（`with_recommended`）。モデルの取得と確認もおすすめを
既定にしているので、画面の既定だけが別のものを指して「取得していないモデル」を使おうと
することのないようにする。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

CompositionQualityPreference = Literal["fast", "careful"]
VideoEnginePreference = Literal["wan22", "minimax_h3"]
CandidateCount = Annotated[int, Field(ge=1, le=24)]
# 人物候補の生成モデルの重みの形式（bf16 か、速くて軽い int8 か）。
CandidatePrecisionPreference = Literal["bf16", "int8"]
# 人物候補の振れ幅。画面のつまみと同じ範囲。
VariationStrength = Annotated[float, Field(ge=1.0, le=5.0)]
# 人物候補を作り直したときの前の回。discard は上書き（基準画像だけ残して消す）、
# keep は履歴として残す。
CandidateHistoryPreference = Literal["discard", "keep"]


class PreferencesRead(BaseModel):
    """いまの既定値。保存が無い項目は、このモデルの既定がそのまま出る。"""

    composition_quality: CompositionQualityPreference = "fast"
    video_engine: VideoEnginePreference = "wan22"
    candidate_count: CandidateCount = 8
    candidate_precision: CandidatePrecisionPreference = "bf16"
    variation_strength: VariationStrength = 3.0
    # 使わなかった画像を溜めない方を既定にする。残したい人だけが切り替える。
    candidate_history: CandidateHistoryPreference = "discard"
    # 最後に適用したおすすめの段（high/standard/light/minimum）。空なら未適用。
    # 画面で「おすすめのまま」かどうかを示すためだけに持つ。
    recommendation_tier: str = ""
    # この端末へのおすすめ（使うモデルが変わる項目だけ）。保存はせず、読むたびに添える。
    # 画面で、おすすめ以外を選んだときの注意を出すのに使う。
    recommended: dict[str, str] = Field(default_factory=dict)
    updated_at: datetime | None = None


class PreferencesUpdate(BaseModel):
    """変更したい項目だけを送る。None は「今の値を変えない」。"""

    composition_quality: CompositionQualityPreference | None = None
    video_engine: VideoEnginePreference | None = None
    candidate_count: CandidateCount | None = None
    candidate_precision: CandidatePrecisionPreference | None = None
    variation_strength: VariationStrength | None = None
    candidate_history: CandidateHistoryPreference | None = None


# 保存する鍵と、その検証器。PreferencesRead の宣言をそのまま使う
# （型と範囲の定義を二重に書かないため）。範囲の指定は annotation ではなく metadata
# 側に入るので、Annotated へ組み直してから渡す（でないと 999 のような値が通る）。
_ADAPTERS: dict[str, TypeAdapter[Any]] = {
    name: TypeAdapter(
        Annotated[tuple([field.annotation, *field.metadata])]
        if field.metadata
        else field.annotation
    )
    for name, field in PreferencesRead.model_fields.items()
    if name not in ("updated_at", "recommended")
}

PREFERENCES_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS preferences (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class _SupportsConnect(Protocol):
    def connect(self): ...  # noqa: ANN201 - sqlite3.Connection context manager


def _now() -> str:
    return datetime.now(UTC).isoformat()


class PreferenceRepository:
    def __init__(self, database: _SupportsConnect):
        self.database = database

    def get(self) -> PreferencesRead:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT key, value, updated_at FROM preferences").fetchall()
        values: dict[str, Any] = {}
        latest = ""
        for row in rows:
            key = row["key"]
            adapter = _ADAPTERS.get(key)
            if adapter is None:
                # 知らない鍵（古い版が書いたものなど）は読み飛ばす。
                continue
            try:
                values[key] = adapter.validate_python(row["value"])
            except ValidationError:
                # 1項目が壊れていても、他の項目まで既定へ戻さない。
                continue
            latest = max(latest, row["updated_at"])
        return PreferencesRead(**values, updated_at=latest or None)

    def stored_keys(self) -> set[str]:
        """利用者が選んだ（＝使える値で保存されている）項目。"""
        with self.database.connect() as connection:
            rows = connection.execute("SELECT key, value FROM preferences").fetchall()
        keys: set[str] = set()
        for row in rows:
            adapter = _ADAPTERS.get(row["key"])
            if adapter is None:
                continue
            try:
                adapter.validate_python(row["value"])
            except ValidationError:
                # 壊れた値は、読むときに既定へ戻る。選んでいないものとして扱う。
                continue
            keys.add(row["key"])
        return keys

    def with_recommended(self, recommended: dict[str, str]) -> PreferencesRead:
        """まだ選んでいない項目を、渡したおすすめで埋めて返す（保存はしない）。

        おすすめの値が検証を通らなければ、その項目は既定のまま。
        """
        current = self.get()
        chosen = self.stored_keys()
        values: dict[str, Any] = {}
        for key, raw in recommended.items():
            adapter = _ADAPTERS.get(key)
            if adapter is None:
                continue
            try:
                value = adapter.validate_python(raw)
            except ValidationError:
                continue
            if key not in chosen:
                values[key] = value
        return current.model_copy(update={**values, "recommended": dict(recommended)})

    def set(self, request: PreferencesUpdate) -> PreferencesRead:
        updates = request.model_dump(exclude_none=True)
        return self._write(updates)

    def apply(self, values: dict[str, str]) -> PreferencesRead:
        """おすすめなど、文字列で来た値をまとめて書き込む。

        検証を通らない値は書かずに飛ばす。マニフェスト側の書き間違いで、覚えている
        設定が壊れることのないようにする。
        """
        accepted: dict[str, Any] = {}
        for key, raw in values.items():
            adapter = _ADAPTERS.get(key)
            if adapter is None:
                continue
            try:
                accepted[key] = adapter.validate_python(raw)
            except ValidationError:
                continue
        return self._write(accepted)

    def _write(self, values: dict[str, Any]) -> PreferencesRead:
        if not values:
            return self.get()
        timestamp = _now()
        with self.database.connect() as connection:
            connection.executemany(
                """
                INSERT INTO preferences(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                [(key, str(value), timestamp) for key, value in values.items()],
            )
        return self.get()

    def clear(self) -> PreferencesRead:
        """覚えている既定値を消す（設定の初期化から呼ぶ）。"""
        with self.database.connect() as connection:
            connection.execute("DELETE FROM preferences")
        return self.get()
