from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

from .model_layout import models_relative_path


CHUNK_SIZE = 8 * 1024 * 1024


class DownloadError(RuntimeError):
    """安全な取得または検証を完了できなかった場合のエラー。"""


@dataclass(frozen=True)
class DownloadSpec:
    id: str
    display_name_ja: str
    download_url: str
    destination: str
    size_bytes: int
    sha256: str
    license: str
    source: str
    external_read_only_reuse: bool = False
    # 既定では取得しない任意のモデル（特定の機能を使う人だけが取る）。
    optional: bool = False
    # 一括で選べるようにするためのまとまり（例: minimax_h3）。既定は本体一式。
    group: str = "core"
    # 動画を作るときだけ必要か。挿絵だけの設定では取得対象から外す。
    video_only: bool = False


@dataclass(frozen=True)
class FileStatus:
    state: str
    path: str
    current_bytes: int
    expected_bytes: int
    message: str


def load_download_specs(manifest_path: Path) -> list[DownloadSpec]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    specs: list[DownloadSpec] = []
    for component in manifest.get("components", []):
        required = {
            "id",
            "display_name_ja",
            "download_url",
            "destination",
            "size_bytes",
            "sha256",
            "license",
            "source",
        }
        if not required.issubset(component):
            continue
        specs.append(
            DownloadSpec(
                id=component["id"],
                display_name_ja=component["display_name_ja"],
                download_url=component["download_url"],
                destination=component["destination"],
                size_bytes=component["size_bytes"],
                sha256=component["sha256"].lower(),
                license=component["license"],
                source=component["source"],
                external_read_only_reuse=component.get("external_read_only_reuse", False),
                optional=bool(component.get("optional", False)),
                group=str(component.get("group", "core")),
                video_only=bool(component.get("video_only", False)),
            )
        )
    return specs


def safe_destination(root: Path, relative_path: str) -> Path:
    """保存先が必ずアプリ管理ルート内になることを保証する。"""
    managed_root = root.expanduser().resolve()
    destination = (managed_root / relative_path).resolve()
    try:
        destination.relative_to(managed_root)
    except ValueError as error:
        raise DownloadError("保存先がアプリ管理領域の外を指しています") from error
    return destination


def calculate_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(CHUNK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


def inspect_managed_file(
    root: Path,
    spec: DownloadSpec,
    *,
    verify_hash: bool = False,
    relative_path: str | None = None,
) -> FileStatus:
    """保存先ルートの中に、そのモデルが正しく揃っているかを調べる。

    `relative_path` を渡すと、マニフェストの保存先ではなくその相対パスを見る
    （既存モデルフォルダのように、種別のフォルダ名が違う置き場所のため）。
    """
    destination = safe_destination(root, relative_path or spec.destination)
    partial = destination.with_name(f"{destination.name}.part")
    if destination.is_file():
        size = destination.stat().st_size
        if size != spec.size_bytes:
            return FileStatus("invalid", str(destination), size, spec.size_bytes, "ファイルサイズが一致しません")
        if verify_hash and calculate_sha256(destination) != spec.sha256:
            return FileStatus("invalid", str(destination), size, spec.size_bytes, "SHA-256が一致しません")
        return FileStatus("ready", str(destination), size, spec.size_bytes, "利用できます")
    if partial.is_file():
        size = partial.stat().st_size
        state = "partial" if size < spec.size_bytes else "invalid"
        message = "途中から再開できます" if state == "partial" else "一時ファイルのサイズが不正です"
        return FileStatus(state, str(partial), size, spec.size_bytes, message)
    return FileStatus("missing", str(destination), 0, spec.size_bytes, "ダウンロードが必要です")


def find_matching_external_file(
    paths: list[Path], spec: DownloadSpec, *, verify_hash: bool = True
) -> Path | None:
    """外部ファイルを読み取って照合するだけで、変更は一切行わない。

    verify_hash=False はサイズ一致までの高速判定。数十GBのモデルのSHA-256計算は
    数分かかるため、画面から即座に返したい取得計画ではこちらを使い、実際に取得を
    省く判断（取得ジョブ）では必ずハッシュまで照合する。
    """
    for candidate in paths:
        try:
            resolved = candidate.expanduser().resolve(strict=True)
            if not resolved.is_file() or resolved.stat().st_size != spec.size_bytes:
                continue
            if not verify_hash or calculate_sha256(resolved) == spec.sha256:
                return resolved
        except OSError:
            continue
    return None


def required_download_bytes(root: Path, specs: list[DownloadSpec]) -> int:
    total = 0
    for spec in specs:
        status = inspect_managed_file(
            root, spec, relative_path=models_relative_path(root, spec.destination)
        )
        if status.state == "ready":
            continue
        current = status.current_bytes if status.state == "partial" else 0
        total += spec.size_bytes - current
    return total


def find_existing_model(
    roots: list[Path], spec: DownloadSpec, *, verify_hash: bool = False
) -> Path | None:
    """既知のモデルフォルダに、そのモデルが正しい名前で置かれていれば返す。

    共有フォルダ・既存モデルフォルダのどちらへ入れていても取得を省けるよう、
    保存先ルートを順に見る。名前と大きさが合えば同じものとして扱う（既定）。
    """
    for root in roots:
        try:
            status = inspect_managed_file(
                root,
                spec,
                verify_hash=verify_hash,
                relative_path=models_relative_path(root, spec.destination),
            )
        except DownloadError:
            continue
        if status.state == "ready":
            return Path(status.path)
    return None


def build_setup_plan(
    root: Path,
    specs: list[DownloadSpec],
    *,
    external_files: list[Path] | None = None,
    extra_roots: list[Path] | None = None,
    verify_managed_hashes: bool = False,
    verify_external_hashes: bool = True,
    reserve_bytes: int = 2 * 1024**3,
) -> dict[str, object]:
    """取得計画を作る。

    `root` は取得先のモデルフォルダ、`extra_roots` はすでに置いてあるかもしれない
    別のモデルフォルダ（共有フォルダ・既存フォルダ）。`external_files` は
    フォルダ構成に関係なく走査した手持ちファイルで、名前が違っても内容が同じなら
    再利用する。
    """
    managed_root = root.expanduser().resolve()
    external_candidates = external_files or []
    other_roots = extra_roots or []
    components: list[dict[str, object]] = []
    required_bytes = 0

    for spec in specs:
        status = inspect_managed_file(
            managed_root,
            spec,
            verify_hash=verify_managed_hashes,
            relative_path=models_relative_path(managed_root, spec.destination),
        )
        external = None
        elsewhere = None
        if status.state != "ready":
            # 別のモデルフォルダに同名で入っているものは、そのまま使える。
            elsewhere = find_existing_model(other_roots, spec)
            if elsewhere is None and spec.external_read_only_reuse:
                external = find_matching_external_file(
                    external_candidates, spec, verify_hash=verify_external_hashes
                )
        if external or elsewhere:
            state = "external_ready"
            path = str(elsewhere or external)
            current_bytes = spec.size_bytes
            download_bytes = 0
            if elsewhere is not None:
                message = "別のモデルフォルダにあるものを使います"
            else:
                message = (
                    "外部ファイルを読み取り専用で再利用できます"
                    if verify_external_hashes
                    else "手持ちのファイルを再利用できます（内容は取得時に照合します）"
                )
        else:
            state = status.state
            path = status.path
            current_bytes = status.current_bytes
            resumable_bytes = current_bytes if state == "partial" else 0
            download_bytes = 0 if state == "ready" else spec.size_bytes - resumable_bytes
            message = status.message
        # 任意のモデルは、選ばれるまで「必要な容量」に数えない。
        if not spec.optional:
            required_bytes += download_bytes
        components.append(
            {
                "id": spec.id,
                "display_name": spec.display_name_ja,
                "optional": spec.optional,
                "group": spec.group,
                "state": state,
                "path": path,
                "current_bytes": current_bytes,
                "expected_bytes": spec.size_bytes,
                "download_bytes": download_bytes,
                "source": spec.source,
                "license": spec.license,
                "message": message,
            }
        )

    try:
        existing = managed_root
        while not existing.exists() and existing.parent != existing:
            existing = existing.parent
        free_bytes: int | None = shutil.disk_usage(existing).free
    except OSError:
        free_bytes = None
    enough_space = free_bytes is not None and free_bytes >= required_bytes + reserve_bytes
    return {
        "managed_root": str(managed_root),
        "components": components,
        "required_download_bytes": required_bytes,
        "free_bytes": free_bytes,
        "enough_space": enough_space,
        "reserve_bytes": reserve_bytes,
        "message": "準備を開始できます" if enough_space else "保存先の空き容量を確認してください",
    }


def ensure_free_space(root: Path, required_bytes: int, *, reserve_bytes: int = 2 * 1024**3) -> None:
    root.mkdir(parents=True, exist_ok=True)
    free_bytes = shutil.disk_usage(root).free
    if free_bytes < required_bytes + reserve_bytes:
        raise DownloadError(
            f"空き容量が不足しています（必要: {required_bytes + reserve_bytes:,}バイト、空き: {free_bytes:,}バイト）"
        )


class DownloadCancelled(DownloadError):
    """利用者が中止した。`.part` は残すので、後から途中で再開できる。"""


def download_with_resume(
    root: Path,
    spec: DownloadSpec,
    *,
    progress: Callable[[int, int], None] | None = None,
    client: httpx.Client | None = None,
    should_cancel: Callable[[], bool] | None = None,
    relative_path: str | None = None,
) -> Path:
    """`.part`へRange取得し、サイズとSHA-256検証後にだけ正式名へ切り替える。

    `should_cancel` が真を返した時点で DownloadCancelled を投げて降りる。
    受信済みの `.part` は消さないので、もう一度実行すれば続きから取得できる。
    """
    destination = safe_destination(root, relative_path or spec.destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f"{destination.name}.part")

    if destination.is_file() and destination.stat().st_size == spec.size_bytes:
        if calculate_sha256(destination) == spec.sha256:
            return destination
        raise DownloadError(
            f"同じ名前で内容の違うファイルがあります。自動では上書きしません: {destination}"
        )

    offset = partial.stat().st_size if partial.is_file() else 0
    if offset > spec.size_bytes:
        raise DownloadError("一時ファイルが想定サイズを超えています。自動では削除しません")

    owns_client = client is None
    active_client = client or httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60, read=120))
    try:
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        with active_client.stream("GET", spec.download_url, headers=headers) as response:
            if offset and response.status_code == 206:
                mode = "ab"
            elif response.status_code == 200:
                mode = "wb"
                offset = 0
            else:
                raise DownloadError(
                    "取得先から受け取れませんでした（応答 "
                    f"{response.status_code}）。時間をおいて、もう一度お試しください"
                )
            with partial.open(mode) as stream:
                downloaded = offset
                for chunk in response.iter_bytes(CHUNK_SIZE):
                    if should_cancel is not None and should_cancel():
                        raise DownloadCancelled("利用者が中止しました")
                    stream.write(chunk)
                    downloaded += len(chunk)
                    if downloaded > spec.size_bytes:
                        raise DownloadError("受信データがマニフェストのサイズを超えました")
                    if progress:
                        progress(downloaded, spec.size_bytes)
    except httpx.HTTPError as error:
        raise DownloadError(f"通信に失敗しました。次回は途中から再開できます: {error}") from error
    finally:
        if owns_client:
            active_client.close()

    if partial.stat().st_size != spec.size_bytes:
        raise DownloadError("受信サイズがマニフェストと一致しません。次回は途中から再開します")
    if calculate_sha256(partial) != spec.sha256:
        raise DownloadError("SHA-256検証に失敗しました。一時ファイルを保持します")
    partial.replace(destination)
    return destination
