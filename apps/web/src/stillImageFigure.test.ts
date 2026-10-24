import { describe, expect, it } from "vitest";
import type { CharacterFigure, StillImageCharacter } from "./api";
import { chosenFigureId, withChosenFigure } from "./stillImageFigure";

function figure(id: string, overrides: Partial<CharacterFigure> = {}): CharacterFigure {
  return {
    id,
    character_id: "c1",
    label: `ラベル${id}`,
    origin: "base",
    state: "completed",
    seed: 0,
    is_default: false,
    has_image: true,
    url: `/api/v1/character-figures/${id}/image`,
    instruction: "",
    prop_asset_ids: [],
    source_figure_id: null,
    job_id: "",
    ordinal: 0,
    error_message: "",
    edit: null,
    created_at: "2026-10-01T00:00:00+00:00",
    updated_at: "2026-10-02T03:04:05+00:00",
    ...overrides,
  };
}

const info: StillImageCharacter = {
  character_id: "c1",
  character_name: "人物",
  spec: { pose: "座る", expression: "", direction: "", placement: "", notes: "" },
  figure_id: "f1",
  figure_is_default: true,
  figure_label: "ラベルf1",
  figure_url: "/api/v1/character-figures/f1/image?v=x",
  has_figure: true,
};

describe("静止画で使う立ち絵", () => {
  const figures = [figure("f1", { is_default: true }), figure("f2")];

  it("選んだ立ち絵に、保存を待たずに切り替える", () => {
    const shown = withChosenFigure(info, figures, "f2");
    expect(shown.figure_id).toBe("f2");
    expect(shown.figure_is_default).toBe(false);
    expect(shown.figure_label).toBe("ラベルf2");
    expect(shown.figure_url).toBe(
      "/api/v1/character-figures/f2/image?v=2026-10-02T03%3A04%3A05%2B00%3A00",
    );
    // 指定の中身には触れない。
    expect(shown.spec).toEqual(info.spec);
  });

  it("選ばなければ既定の立ち絵を使う", () => {
    const shown = withChosenFigure({ ...info, figure_id: "f2", figure_is_default: false }, figures, "");
    expect(shown.figure_id).toBe("f1");
    expect(shown.figure_is_default).toBe(true);
  });

  it("選んだ立ち絵が使えなければ既定の立ち絵に戻る（合成と同じ決め方）", () => {
    const unfinished = [figure("f1", { is_default: true }), figure("f2", { state: "running", has_image: false })];
    const shown = withChosenFigure(info, unfinished, "f2");
    expect(shown.figure_id).toBe("f1");
    expect(shown.figure_is_default).toBe(false);
  });

  it("使える立ち絵が無ければ、立ち絵なしとして出す", () => {
    const shown = withChosenFigure(info, [], "");
    expect(shown.has_figure).toBe(false);
    expect(shown.figure_url).toBeNull();
  });

  it("自分で選んだ立ち絵だけを選択として扱う", () => {
    expect(chosenFigureId(info)).toBe("");
    expect(chosenFigureId({ ...info, figure_is_default: false })).toBe("f1");
    expect(chosenFigureId(undefined)).toBe("");
  });
});
