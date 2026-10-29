"""「いまこの操作を実行できるか」と、できない場合の理由。

押せるのに必ず失敗するボタンを無くすため、判定は生成側と同じ根拠から作る。
合成は `resolve_still_image_sources`、カットの動画生成は起点フレームの有無で、
実行時と同じ条件を先に評価して理由を返す。UIはこれを表示するだけにする。
置いて合成する静止画は `resolve_placed_sources` を通す。
"""

from __future__ import annotations

from .composition import CompositionError, resolve_still_image_sources


def still_image_compose_block_reason(database, still_image) -> str:  # noqa: ANN001 - Database / StillImageRead
    """静止画の合成ができない理由。実行できるなら空文字。"""
    # 遅延インポートで循環参照を避ける（他の資産リポジトリも同じ扱い）。
    from .assets import AssetNotFoundError
    from .characters import CharacterNotFoundError

    from .placed_composition import resolve_placed_sources
    from .schemas import CompositionMethod

    try:
        if still_image.composition_method == CompositionMethod.PLACED:
            resolve_placed_sources(database, still_image)
        else:
            resolve_still_image_sources(database, still_image)
    except CompositionError as error:
        return str(error)
    except (CharacterNotFoundError, AssetNotFoundError):
        return "この静止画が参照している人物か素材が見つかりません。選び直してください"
    return ""


def resolve_shot_start_frame(database, shot, *, continue_from_previous: bool = True):  # noqa: ANN001, ANN201
    """このカットの起点を「出どころ」と「実体」の組で返す。"""
    source, _owner_id, image_path = _resolve_start_frame_owner(
        database, shot, continue_from_previous=continue_from_previous
    )
    return source, image_path


def resolve_shot_start_frame_version(database, shot) -> tuple[str, str]:  # noqa: ANN001
    """このカットの起点の「出どころ」と、その画像URLに付ける版を返す。

    起点は他の記録（前のカット・静止画）の絵を指すことがあり、その絵が書き直されても
    このカットの記録は変わらない。版は、絵を持つ記録とその記録の画像の版から作る。
    """
    from .shots import ShotRepository
    from .still_images import StillImageRepository

    source, owner_id, _image_path = _resolve_start_frame_owner(database, shot)
    if source in ("custom", "previous"):
        revision = str(ShotRepository(database).frame_revision(owner_id))
    elif source == "still_image":
        revision = StillImageRepository(database).get(owner_id).image_version
    else:
        return source, ""
    return source, f"{source}:{owner_id}:{revision}"


def _resolve_start_frame_owner(database, shot, *, continue_from_previous: bool = True):  # noqa: ANN001, ANN202
    """起点の「出どころ」「その絵を持つ記録のID」「実体」を返す。

    優先順位:
      1. 構図変更で作った起点フレーム
      2. 連続性オンなら、前カットの最終フレーム
      3. 元になった静止画

    **実体が無いものは数えない。**DBに道が残っていてもファイルが消えていることがある
    （利用者が保存先の中身を手で消した、など）。判定・表示・
    生成がここを共有することで、画面の表示と実際に使う1枚が食い違わないようにする。
    """
    from .still_images import StillImageNotFoundError, StillImageRepository
    from .shots import ShotRepository

    shots = ShotRepository(database)
    custom = shots.custom_start_frame_path(shot.id)
    if custom is not None and custom.is_file():
        return "custom", shot.id, custom
    if continue_from_previous:
        previous = shots.previous_shot(shot.still_image_id, shot.ordinal)
        if previous is not None:
            last_frame = shots.last_frame_path(previous.id)
            if last_frame is not None and last_frame.is_file():
                return "previous", previous.id, last_frame
    try:
        still_image = StillImageRepository(database).image_path(shot.still_image_id)
    except StillImageNotFoundError:
        return "none", "", None
    if still_image is not None and still_image.is_file():
        return "still_image", shot.still_image_id, still_image
    return "none", "", None


def shot_start_frame_source(database, shot) -> str:  # noqa: ANN001 - Database / ShotRead
    """このカットの起点が、実際にどこから来るか。"""
    return resolve_shot_start_frame(database, shot)[0]


def shot_video_block_reason(database, shot) -> str:  # noqa: ANN001 - Database / ShotRead
    """カットの動画生成ができない理由。実行できるなら空文字。"""
    from .still_images import StillImageNotFoundError, StillImageRepository

    if shot_start_frame_source(database, shot) != "none":
        return ""
    try:
        StillImageRepository(database).get(shot.still_image_id)
    except StillImageNotFoundError:
        return "このカットの静止画が見つかりません"
    return "起点となる画像がありません。先に静止画を合成してください"
