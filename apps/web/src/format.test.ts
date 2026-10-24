import { describe, expect, it } from "vitest";
import { formatBytes, safeFileName } from "./format";

describe("容量表示", () => {
  it("バイトを読みやすく表示する", () => {
    expect(formatBytes(32 * 1024 ** 3)).toBe("32.0 GB");
    expect(formatBytes(512 * 1024 ** 2)).toBe("512 MB");
    expect(formatBytes(null)).toBe("未確認");
  });
});

describe("保存するファイル名", () => {
  it("作品名とシーン名をつないで、何のファイルか分かる名前にする", () => {
    expect(safeFileName(["海辺の町の物語", "夕暮れの屋上", "カット01"], "mp4"))
      .toBe("海辺の町の物語_夕暮れの屋上_カット01.mp4");
  });

  it("Windowsが使えない文字を落とす（保存の段階で弾かれないように）", () => {
    expect(safeFileName(['第1話: 出会い/再会', "カット01"], "webp"))
      .toBe("第1話 出会い再会_カット01.webp");
  });

  it("空の名前は飛ばし、全部空なら無題にする", () => {
    expect(safeFileName(["作品", "", null, "カット01"], "mp4")).toBe("作品_カット01.mp4");
    expect(safeFileName(["", null], "mp4")).toBe("無題.mp4");
  });

  it("先頭と末尾の点は外す（Windowsが黙って落とすため）", () => {
    expect(safeFileName(["...作品..."], "mp4")).toBe("作品.mp4");
  });
});
