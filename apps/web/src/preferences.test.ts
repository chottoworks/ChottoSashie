import { describe, expect, it } from "vitest";
import { missingModelNote } from "./preferences";

describe("missingModelNote", () => {
  it("モデルが揃っていれば何も出さない", () => {
    expect(missingModelNote({ available: true, can_download: true }, "int8")).toBeNull();
    expect(missingModelNote({ available: true, can_download: false }, "int8")).toBeNull();
  });

  it("取得できる環境では、取得する場所を案内する", () => {
    const note = missingModelNote({ available: false, can_download: true }, "int8");
    expect(note).toContain("「int8」");
    expect(note).toContain("モデルの取得");
  });

  it("既存ComfyUIをそのまま使う環境では、配置を案内する", () => {
    const note = missingModelNote({ available: false, can_download: false }, "int8");
    expect(note).toContain("「int8」");
    expect(note).toContain("modelsフォルダ");
    expect(note).not.toContain("モデルの取得");
  });
});
