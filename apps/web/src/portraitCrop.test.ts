import { describe, expect, it } from "vitest";
import { isWholeImage, moveCrop, resizeCrop, toSquare, WHOLE_IMAGE } from "./portraitCrop";

// 縦長の画像（幅:高さ = 1:2）。人物候補・立ち絵はおおむねこの向き。
const TALL = 0.5;

function pixelRatio(crop: { width: number; height: number }, aspect: number): number {
  return (crop.width * aspect) / crop.height;
}

describe("トリミング枠を動かす", () => {
  it("画像の外へは出さず、端で止める", () => {
    const crop = { x: 0.2, y: 0.2, width: 0.5, height: 0.5 };
    expect(moveCrop(crop, 0.9, -0.9)).toEqual({ x: 0.5, y: 0, width: 0.5, height: 0.5 });
  });
});

describe("トリミング枠の大きさを変える", () => {
  it("つまんだ角だけが動き、対角は動かない", () => {
    const crop = { x: 0.2, y: 0.2, width: 0.4, height: 0.4 };
    const resized = resizeCrop(crop, "se", 0.9, 0.7, false, TALL);
    expect(resized.x).toBeCloseTo(0.2);
    expect(resized.y).toBeCloseTo(0.2);
    expect(resized.width).toBeCloseTo(0.7);
    expect(resized.height).toBeCloseTo(0.5);
  });

  it("左上をつまんだときも、右下の角は動かない", () => {
    const crop = { x: 0.2, y: 0.2, width: 0.4, height: 0.4 };
    const resized = resizeCrop(crop, "nw", 0.1, 0.05, false, TALL);
    expect(resized.x + resized.width).toBeCloseTo(0.6);
    expect(resized.y + resized.height).toBeCloseTo(0.6);
  });

  it("画像の外までつまんでも、画像の端で止める", () => {
    const resized = resizeCrop({ x: 0.5, y: 0.5, width: 0.2, height: 0.2 }, "se", 1.5, 1.5, false, TALL);
    expect(resized.x + resized.width).toBeCloseTo(1);
    expect(resized.y + resized.height).toBeCloseTo(1);
  });

  it("対角を越えても裏返らず、下限の大きさで止める", () => {
    const resized = resizeCrop({ x: 0.5, y: 0.5, width: 0.2, height: 0.2 }, "se", 0.1, 0.1, false, TALL);
    expect(resized.x).toBeCloseTo(0.5);
    expect(resized.width).toBeGreaterThan(0);
    expect(resized.height).toBeGreaterThan(0);
  });

  it("正方形にそろえているときは、画素で正方形のまま画像に収まる", () => {
    const crop = toSquare(WHOLE_IMAGE, TALL);
    const resized = resizeCrop(crop, "se", 1.2, 0.95, true, TALL);
    expect(pixelRatio(resized, TALL)).toBeCloseTo(1);
    expect(resized.x + resized.width).toBeLessThanOrEqual(1 + 1e-9);
    expect(resized.y + resized.height).toBeLessThanOrEqual(1 + 1e-9);
  });
});

describe("正方形にそろえる", () => {
  it("縦長の画像全体からは、幅いっぱいの正方形を中央に取る", () => {
    const square = toSquare(WHOLE_IMAGE, TALL);
    expect(square.width).toBeCloseTo(1);
    expect(square.height).toBeCloseTo(0.5);
    expect(square.y).toBeCloseTo(0.25);
    expect(pixelRatio(square, TALL)).toBeCloseTo(1);
  });

  it("中心が端に寄っていても、画像からはみ出さない", () => {
    const square = toSquare({ x: 0, y: 0.9, width: 1, height: 0.1 }, 2);
    expect(square.y + square.height).toBeLessThanOrEqual(1 + 1e-9);
    expect(pixelRatio(square, 2)).toBeCloseTo(1);
  });
});

describe("画像全体かどうか", () => {
  it("全体と同じ範囲なら、範囲を決めていないのと同じに扱う", () => {
    expect(isWholeImage(WHOLE_IMAGE)).toBe(true);
    expect(isWholeImage({ x: 0, y: 0.1, width: 1, height: 0.9 })).toBe(false);
  });
});
