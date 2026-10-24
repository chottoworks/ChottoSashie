import { afterEach, describe, expect, it, vi } from "vitest";
import { pollJob } from "./api";

/** ジョブ取得の応答を1回ぶん作る。 */
function jobResponse(state: string, progress: number): Response {
  return {
    ok: true,
    json: async () => ({
      id: "job-1",
      kind: "character.figures",
      state,
      progress,
      payload: {},
      message: null,
      external_id: null,
      error_code: null,
      result: null,
      created_at: "2026-09-06T08:55:06Z",
      updated_at: "2026-09-06T08:55:06Z",
    }),
  } as unknown as Response;
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

/** 待ち時間を飛ばしながら、pollJob が返るまで進める。 */
async function runWithFakeTimers<T>(work: Promise<T>): Promise<T> {
  let settled = false;
  // 先に結果の受け手を付けておく（付け忘れると、失敗が宙に浮いたまま報告される）。
  void work.then(
    () => undefined,
    () => undefined,
  ).finally(() => {
    settled = true;
  });
  while (!settled) {
    await vi.advanceTimersByTimeAsync(1200);
  }
  return work;
}

describe("処理の進み具合を追う", () => {
  it("途中で状態を取れなくても、取れるようになれば追い続ける", async () => {
    vi.useFakeTimers();
    const responses = [
      () => Promise.resolve(jobResponse("running", 0.31)),
      () => Promise.reject(new TypeError("Failed to fetch")),
      () => Promise.reject(new TypeError("Failed to fetch")),
      () => Promise.resolve(jobResponse("running", 0.6)),
      () => Promise.resolve(jobResponse("completed", 1)),
    ];
    vi.stubGlobal("fetch", vi.fn(() => (responses.shift() ?? (() => Promise.reject(new Error("呼びすぎ"))))()));

    const seen: number[] = [];
    const job = await runWithFakeTimers(pollJob("job-1", (update) => seen.push(update.progress)));

    expect(job.state).toBe("completed");
    expect(seen).toEqual([0.31, 0.6, 1]);
  });

  it("取れない状態が続いたときは、確認できなくなったことを伝える", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))));

    await expect(runWithFakeTimers(pollJob("job-1", () => undefined))).rejects.toThrow(
      "処理の状態を確認できません",
    );
  });
});
