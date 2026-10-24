import type { CharacterFigure, StillImageCharacter } from "./api";

/** 自分で選んだ立ち絵の id。既定のまま使っているなら空文字。 */
export function chosenFigureId(info: StillImageCharacter | undefined): string {
  return info && !info.figure_is_default && info.figure_id ? info.figure_id : "";
}

/**
 * 立ち絵を選び直した直後に出す1人ぶん。保存の返事を待たずに画面を切り替えるために使う。
 *
 * 決め方はAPIの `resolve_still_image_figure` と同じ：選んだ立ち絵が出来上がっていれば
 * それ、そうでなければ既定の1枚。画像ファイルが実際にあるかはAPIにしか確かめられないので、
 * 保存の返事が届いたらAPIの答えで置き換える。
 */
export function withChosenFigure(
  info: StillImageCharacter,
  figures: CharacterFigure[],
  figureId: string,
): StillImageCharacter {
  const usable = figures.filter(
    (figure) =>
      figure.character_id === info.character_id
      && figure.state === "completed"
      && figure.has_image
      && figure.url,
  );
  const figure =
    (figureId ? usable.find((item) => item.id === figureId) : undefined)
    ?? usable.find((item) => item.is_default)
    ?? null;
  return {
    ...info,
    figure_id: figure?.id ?? null,
    figure_is_default: !figureId,
    figure_label: figure?.label ?? "",
    // 立ち絵の画面と同じく、描き直すたびに変わる版をURLに付けて古い画像を出さないようにする。
    figure_url: figure ? `${figure.url}?v=${encodeURIComponent(figure.updated_at)}` : null,
    has_figure: figure !== null,
  };
}
