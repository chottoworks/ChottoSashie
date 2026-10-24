"""資産・人物の参照静止画をディスクへ保存し、安全に配信するための共通処理。

背景・小物・人物の3種が同じ扱いをするため、検証・保存・パス封じ込めの判定を
ここへ一本化する。パス封じ込めの緩みは情報漏えいに直結するため、判定は必ず
この一箇所を通す。
"""

from __future__ import annotations

from pathlib import Path

# 拡張子はサーバー側で決めた既知の値だけを使う。アップロードされたファイル名は使わない。
ALLOWED_IMAGE_CONTENT_TYPES: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}

_EXTENSION_MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

# 生成したカット動画の入れ物。Wan 2.2 はアニメーションwebp、MiniMax H3 は音声付きmp4。
_VIDEO_EXTENSION_MEDIA_TYPES: dict[str, str] = {
    ".webp": "image/webp",
    ".mp4": "video/mp4",
}

MAX_IMAGE_BYTES = 15 * 1024 * 1024


class UnsupportedImageError(ValueError):
    pass


class ImageNotAvailableError(FileNotFoundError):
    pass


def validate_image_upload(content_type: str | None, data: bytes) -> str:
    """アップロードされた画像の形式・サイズを検証し、保存先の拡張子を返す。"""
    if content_type not in ALLOWED_IMAGE_CONTENT_TYPES:
        raise UnsupportedImageError("対応していない画像形式です（PNG・JPEG・WebPが使えます）")
    if not data:
        raise UnsupportedImageError("画像データが空です")
    if len(data) > MAX_IMAGE_BYTES:
        raise UnsupportedImageError("画像サイズが上限（15MB）を超えています")
    return ALLOWED_IMAGE_CONTENT_TYPES[content_type]


def save_uploaded_image(image_root: Path, entity_id: str, data: bytes, extension: str) -> Path:
    """検証済みの画像データを ``image_root/{entity_id}{extension}`` へ保存する。"""
    image_root.mkdir(parents=True, exist_ok=True)
    destination = image_root / f"{entity_id}{extension}"
    destination.write_bytes(data)
    return destination


def guess_content_type(path: Path) -> str:
    """ローカル画像の拡張子から、ComfyUIへ送るときのcontent-typeを推定する。"""
    return _EXTENSION_MEDIA_TYPES.get(path.suffix.lower(), "image/png")


def _read_contained_file(
    root: Path, path: Path, media_types: dict[str, str], missing_message: str
) -> tuple[bytes, str]:
    """保存領域内に収まっていることを確認したうえでファイルを読み出す。

    resolve 後の親ディレクトリが想定領域と一致し、拡張子も許可済みのものだけを
    通す。ディレクトリトラバーサルで領域外を読ませない。
    """
    try:
        resolved = path.resolve(strict=True)
        expected_root = root.resolve(strict=True)
    except (FileNotFoundError, OSError) as error:
        raise ImageNotAvailableError(missing_message) from error
    suffix = resolved.suffix.lower()
    if resolved.parent != expected_root or suffix not in media_types:
        raise ImageNotAvailableError(missing_message)
    return resolved.read_bytes(), media_types[suffix]


def read_served_image(image_root: Path, image_path: Path) -> tuple[bytes, str]:
    """配信対象の画像を、保存領域内に収まっていることを確認したうえで読み出す。"""
    return _read_contained_file(
        image_root, image_path, _EXTENSION_MEDIA_TYPES, "画像ファイルが見つかりません"
    )


def read_served_video(video_root: Path, video_path: Path) -> tuple[bytes, str]:
    """配信対象のカット動画を、保存領域内に収まっていることを確認したうえで読み出す。"""
    return _read_contained_file(
        video_root, video_path, _VIDEO_EXTENSION_MEDIA_TYPES, "動画ファイルが見つかりません"
    )
