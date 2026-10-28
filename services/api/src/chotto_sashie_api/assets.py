"""背景資産・小物資産の共通CRUD。

両テーブル(`background_assets`, `prop_assets`)は列構成が同一のため、テーブル名
だけをパラメータ化した1つのリポジトリで扱う。`table` は呼び出し側のハード
コードされた値のみを受け取り、利用者入力からは決して渡らないため、
f-string によるテーブル名の埋め込みはSQLインジェクションの経路にならない。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from sqlite3 import Row
from uuid import uuid4

from .database import Database
from .projects import ProjectRepository
from .schemas import AssetCreate, AssetRead, AssetUpdate

_ALLOWED_TABLES = {"background_assets", "prop_assets"}


class AssetNotFoundError(KeyError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _to_asset(row: Row) -> AssetRead:
    columns = set(row.keys())
    return AssetRead(
        id=row["id"],
        project_id=row["project_id"],
        name=row["name"],
        category=row["category"],
        description=row["description"],
        has_image=bool(row["image_path"]),
        image_version=str(row["image_revision"]),
        source_prompt=row["source_prompt"],
        input_prompt=row["input_prompt"],
        # 背景だけが持つ列。小物のテーブルには無いので、あるときだけ読む。
        base_asset_id=row["base_asset_id"] if "base_asset_id" in columns else None,
        variant_label=row["variant_label"] if "variant_label" in columns else "",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def build_asset_prompt(prompt: str, illustration_style: str) -> str:
    """背景・小物の指示文へ、作品の画風を足す。

    合成・人物の状態画像・カットの構図変更と同じく、素材の生成にも作品の画風を渡す。
    渡さないと素材だけが作品と違う描かれ方になり、合成で人物と食い違う（アニメ調の
    人物を置く作品で、背景だけが写真になる）。

    足すだけでは足りない場合もある。素材の生成は
    `zimage_basic_core.json` にステップ9・cfg 1.0が焼き込まれており、cfg 1.0 では
    CFGが働かないので**負例プロンプトが一切効かない**。また画風の指定が
    「落ち着いた色合い、やわらかな光、繊細な描き込み」のように**画材や描法を名指し
    していない**と、写真もその条件を満たしてしまう。画風に「イラスト」「水彩」などの
    描かれ方が書かれているほど効く。
    """
    style = illustration_style.strip()
    if not style:
        return prompt
    return f"{prompt.rstrip()} 作品全体の画風: {style}"


def variation_name(base_name: str, label: str) -> str:
    """バリエーションの名前。グループの元の名前に、元との違いを括弧で添える（「教室（夕方）」）。

    静止画で背景を選ぶ一覧には名前しか出ないので、同じ場所の背景を名前で見分けられる
    ようにする。
    """
    return f"{base_name}（{label}）"[:200] if label else base_name[:200]


def _follow_group_name(connection, base_id: str, old_name: str, new_name: str, timestamp: str) -> None:  # noqa: ANN001
    """元の背景の名前を変えたら、そのグループのバリエーションの名前も追従させる。

    追従させるのは「元の名前（違い）」のまま使っているものだけ。利用者が個別に付け直した
    名前は、その人の意図なので書き換えない。
    """
    rows = connection.execute(
        "SELECT id, name, variant_label FROM background_assets WHERE base_asset_id = ?",
        (base_id,),
    ).fetchall()
    for row in rows:
        if row["name"] != variation_name(old_name, row["variant_label"]):
            continue
        connection.execute(
            "UPDATE background_assets SET name = ?, updated_at = ? WHERE id = ?",
            (variation_name(new_name, row["variant_label"]), timestamp, row["id"]),
        )


class AssetRepository:
    def __init__(self, database: Database, table: str):
        if table not in _ALLOWED_TABLES:
            raise ValueError(f"未対応のテーブルです: {table}")
        self.database = database
        self.table = table

    def create(
        self,
        project_id: str,
        request: AssetCreate,
        *,
        base_asset_id: str | None = None,
        variant_label: str = "",
    ) -> AssetRead:
        """素材を1件作る。`base_asset_id` を渡すと、その背景のバリエーションになる。

        `base_asset_id` は常にグループの元を指す。派生の派生を作るときも、呼び出し側が
        元をたどってから渡す（`resolve_group_base`）。
        """
        ProjectRepository(self.database).get(project_id)
        asset_id = str(uuid4())
        timestamp = _now()
        extra_columns = ", base_asset_id, variant_label" if base_asset_id or variant_label else ""
        extra_values = ", ?, ?" if extra_columns else ""
        parameters: list[object] = [
            asset_id,
            project_id,
            request.name,
            request.category,
            request.description,
        ]
        if extra_columns:
            parameters.extend([base_asset_id, variant_label])
        parameters.extend([timestamp, timestamp])
        with self.database.connect() as connection:
            connection.execute(
                f"""
                INSERT INTO {self.table}(
                    id, project_id, name, category, description{extra_columns},
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?{extra_values}, ?, ?)
                """,
                tuple(parameters),
            )
        return self.get(asset_id)

    def resolve_group_base(self, asset_id: str) -> str:
        """この素材が属するグループの元。自分が元ならそのID。"""
        asset = self.get(asset_id)
        return asset.base_asset_id or asset.id

    def get(self, asset_id: str) -> AssetRead:
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT * FROM {self.table} WHERE id = ?", (asset_id,)
            ).fetchone()
        if row is None:
            raise AssetNotFoundError(asset_id)
        return _to_asset(row)

    def list(self, project_id: str) -> list[AssetRead]:
        ProjectRepository(self.database).get(project_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM {self.table}
                WHERE project_id = ?
                ORDER BY updated_at DESC, created_at DESC
                """,
                (project_id,),
            ).fetchall()
        return [_to_asset(row) for row in rows]

    def update(self, asset_id: str, request: AssetUpdate) -> AssetRead:
        current = self.get(asset_id)
        updates = request.model_dump(exclude_unset=True)
        if not updates:
            return current
        allowed = {"name", "category", "description"}
        if not set(updates).issubset(allowed):
            raise ValueError("更新対象に許可されていない項目があります")
        timestamp = _now()
        assignments = ", ".join(f"{name} = ?" for name in updates)
        with self.database.connect() as connection:
            cursor = connection.execute(
                f"UPDATE {self.table} SET {assignments}, updated_at = ? WHERE id = ?",
                (*updates.values(), timestamp, asset_id),
            )
            if cursor.rowcount != 1:
                raise AssetNotFoundError(asset_id)
            if self.table == "background_assets" and "name" in updates:
                _follow_group_name(connection, asset_id, current.name, updates["name"], timestamp)
        return self.get(asset_id)

    def image_path(self, asset_id: str) -> Path | None:
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT image_path FROM {self.table} WHERE id = ?", (asset_id,)
            ).fetchone()
        if row is None:
            raise AssetNotFoundError(asset_id)
        return Path(row["image_path"]) if row["image_path"] else None

    def set_image(
        self,
        asset_id: str,
        image_path: Path,
        source_prompt: str = "",
        input_prompt: str = "",
    ) -> AssetRead:
        """画像を差し替える。指示文はその画像を作ったときのもので、アップロードなら空。"""
        self.get(asset_id)
        timestamp = _now()
        with self.database.connect() as connection:
            connection.execute(
                f"""
                UPDATE {self.table}
                SET image_path = ?, source_prompt = ?, input_prompt = ?,
                    image_revision = image_revision + 1, updated_at = ?
                WHERE id = ?
                """,
                (str(image_path), source_prompt, input_prompt, timestamp, asset_id),
            )
        return self.get(asset_id)

    def delete(self, asset_id: str) -> Path | None:
        """削除した行のimage_pathを返す（存在すれば呼び出し側でファイルも削除する）。"""
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT image_path FROM {self.table} WHERE id = ?", (asset_id,)
            ).fetchone()
            if row is None:
                raise AssetNotFoundError(asset_id)
            image_path = row["image_path"]
            connection.execute(f"DELETE FROM {self.table} WHERE id = ?", (asset_id,))
        return Path(image_path) if image_path else None
