import { useCallback, useEffect, useState } from "react";
import * as api from "./api";
import { formatBytes } from "./format";
import { usePreferences } from "./preferences";

/**
 * この端末の構成と、それに合わせた既定値の提案。
 *
 * 「どのモデルで作るか」は、GPUのVRAMと世代で現実的な選択肢が変わる。利用者に
 * 見比べさせる代わりに、調べた構成と**その理由**を並べて出し、適用するかどうかだけを
 * 選んでもらう。適用しなければ何も変わらない（提案は保存済みの選択を上書きしない）。
 *
 * 調べられなかったときは、何も勧めずにその旨だけを出す。当てずっぽうの既定を
 * 押し付けると、動かない組み合わせを既定にしてしまう。
 */
export function HardwarePanel({
  videoEnabled = true,
  onReport,
}: {
  videoEnabled?: boolean;
  /** 調べた結果を親へも渡す。同じ調べ（nvidia-smi）を二度走らせないため。 */
  onReport?: (report: api.HardwareReport) => void;
}) {
  const [report, setReport] = useState<api.HardwareReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { reload: reloadPreferences } = usePreferences();

  const load = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const next = await api.getHardware();
      setReport(next);
      onReport?.(next);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [onReport]);

  useEffect(() => {
    void load();
    // 「動画も作るか」を切り替えると、出すべき注意も変わるので調べ直す。
  }, [load, videoEnabled]);

  const apply = () =>
    void (async () => {
      setBusy(true);
      setError(null);
      try {
        await api.applyHardwareRecommendation();
        await reloadPreferences();
        const next = await api.getHardware();
        setReport(next);
        onReport?.(next);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(false);
      }
    })();

  const recommendation = report?.recommendation;
  const hasSettings = (recommendation?.settings.length ?? 0) > 0;
  const requirements = recommendation?.requirements;

  return (
    <div>
      {error && <div className="vs-error">{error}</div>}

      <div className="vs-shared-summary">
        <div>
          <b>GPU</b>
          {report ? report.gpu_name || "確認できません" : "確認中…"}
        </div>
        <div>
          <b>VRAM</b>
          {report && report.vram_total_bytes > 0
            ? `${formatBytes(report.vram_total_bytes)}${
                report.vram_free_bytes !== null
                  ? `（いまの空き ${formatBytes(report.vram_free_bytes)}）`
                  : ""
              }`
            : "確認できません"}
        </div>
        <div>
          <b>メモリ</b>
          {report && report.ram_total_bytes > 0 ? formatBytes(report.ram_total_bytes) : "確認できません"}
        </div>
        {report?.compute_capability && (
          <div>
            <b>GPUの世代</b>
            compute capability {report.compute_capability}
            {report.driver_version && `（ドライバ ${report.driver_version}）`}
          </div>
        )}
        <div>
          <b>調べ方</b>
          {report?.source === "comfyui"
            ? "起動中のComfyUIに聞きました"
            : report?.source === "nvidia-smi"
              ? "nvidia-smi で調べました"
              : "確認できません"}
        </div>
      </div>

      {report && !report.detected && (
        <div className="vs-note warn">
          {report.message || "この端末の構成を確認できませんでした。"}
          設定はすべて手動で選べます。
        </div>
      )}

      {/* 動作要件は、提案より先に出す。数十GBを取得したあとで気づくのは遅い。 */}
      {requirements?.minimum_label && (
        <div
          className={
            !requirements.known
              ? "vs-note"
              : requirements.meets_minimum
                ? requirements.meets_recommended
                  ? "vs-note info"
                  : "vs-note"
                : "vs-note warn"
          }
        >
          <strong>
            動作要件: {requirements.minimum_label}
            {requirements.recommended_label && `／推奨: ${requirements.recommended_label}`}
          </strong>
          {requirements.known && requirements.meets_recommended && "この端末は推奨を満たしています。"}
          {requirements.message}
        </div>
      )}

      {recommendation && report?.detected && (
        <>
          <div className="vs-note info">
            <strong>{recommendation.tier_label}</strong>
            {recommendation.summary}
          </div>

          {hasSettings && (
            <div className="vs-recommend-list">
              {recommendation.settings.map((setting) => (
                <div key={setting.key} className="vs-recommend-row">
                  <span className="vs-recommend-name">{setting.display_name}</span>
                  <div>
                    <strong>{setting.value_label}</strong>
                    <span className="vs-muted">{setting.reason}</span>
                    {setting.notes.map((note) => (
                      <span key={note} className="vs-recommend-note">
                        {note}
                      </span>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}

          {recommendation.warnings.map((warning) => (
            <div key={warning} className="vs-note warn">
              {warning}
            </div>
          ))}
        </>
      )}

      <div className="vs-row" style={{ marginTop: 12 }}>
        <button className="vs-btn" disabled={busy || !hasSettings || report?.applied} onClick={apply}>
          {report?.applied ? "適用済み" : "おすすめ設定を使う"}
        </button>
        <button className="vs-btn ghost" disabled={busy} onClick={() => void load()}>
          {busy ? "調べています…" : "調べ直す"}
        </button>
      </div>
      {hasSettings && (
        <p className="vs-muted" style={{ marginBottom: 0 }}>
          適用すると、各画面の初期表示がこの値になります。あとから画面ごとに選び直せて、
          選び直した値がそのまま次回の既定になります。
        </p>
      )}
    </div>
  );
}
