// 代表写真のトリミング枠の計算。
//
// 枠は画像の幅・高さに対する割合（0〜1）で持つ（APIと同じ形）。正方形は画素で
// 正方形なので、割合のままでは比べられない。画像の縦横の画素数（aspect = 幅 / 高さ）を
// 受け取り、画素に直して考える。

import type { PortraitCrop } from "./api";

/** 枠の一辺の下限（割合）。これより小さくすると、つまめなくなる。 */
export const MIN_CROP_SIZE = 0.05;

/** つまむ角。n = 上、s = 下、w = 左、e = 右。 */
export type CropCorner = "nw" | "ne" | "sw" | "se";

export const WHOLE_IMAGE: PortraitCrop = { x: 0, y: 0, width: 1, height: 1 };

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}

/** 画像全体と同じ範囲か（それなら範囲を記録せず、全体を出すのと同じ扱いにする）。 */
export function isWholeImage(crop: PortraitCrop): boolean {
  const epsilon = 1e-3;
  return crop.x < epsilon && crop.y < epsilon && crop.width > 1 - epsilon && crop.height > 1 - epsilon;
}

/** 枠を、中心をできるだけ保ったまま、画像に収まる最大の正方形にする。 */
export function toSquare(crop: PortraitCrop, aspect: number): PortraitCrop {
  // 画素で見た一辺。幅の割合×aspect が、高さを1としたときの幅になる。
  const side = Math.min(crop.width * aspect, crop.height);
  const width = side / aspect;
  const height = side;
  const centerX = crop.x + crop.width / 2;
  const centerY = crop.y + crop.height / 2;
  return {
    x: clamp(centerX - width / 2, 0, 1 - width),
    y: clamp(centerY - height / 2, 0, 1 - height),
    width,
    height,
  };
}

/** 枠を動かす。画像の外へは出さない（端で止める）。 */
export function moveCrop(start: PortraitCrop, dx: number, dy: number): PortraitCrop {
  return {
    ...start,
    x: clamp(start.x + dx, 0, 1 - start.width),
    y: clamp(start.y + dy, 0, 1 - start.height),
  };
}

/**
 * 角をつまんで大きさを変える。つまんでいない側の角（対角）は動かさない。
 *
 * `pointX` / `pointY` はつまんでいる角の行き先（割合）。対角を越えて裏返すことはせず、
 * 下限の大きさで止める。正方形にそろえているときは、縦横のうち大きく動かした方に
 * 合わせ、画像からはみ出す分は縮める。
 */
export function resizeCrop(
  start: PortraitCrop,
  corner: CropCorner,
  pointX: number,
  pointY: number,
  square: boolean,
  aspect: number,
): PortraitCrop {
  const west = corner === "nw" || corner === "sw";
  const north = corner === "nw" || corner === "ne";
  const anchorX = west ? start.x + start.width : start.x;
  const anchorY = north ? start.y + start.height : start.y;
  // 対角から画像の端までの余地（割合）。
  const roomX = west ? anchorX : 1 - anchorX;
  const roomY = north ? anchorY : 1 - anchorY;

  let width = clamp(west ? anchorX - pointX : pointX - anchorX, MIN_CROP_SIZE, roomX);
  let height = clamp(north ? anchorY - pointY : pointY - anchorY, MIN_CROP_SIZE, roomY);
  if (square) {
    // 画素で比べる。高さを1としたとき、幅の割合×aspect が画素での幅。
    const minSide = Math.max(MIN_CROP_SIZE * aspect, MIN_CROP_SIZE);
    const side = clamp(Math.max(width * aspect, height), minSide, Math.min(roomX * aspect, roomY));
    width = side / aspect;
    height = side;
  }
  return {
    x: west ? anchorX - width : anchorX,
    y: north ? anchorY - height : anchorY,
    width,
    height,
  };
}
