import { useEffect, useState } from "react";
import * as api from "./api";
import { HardwarePanel } from "./HardwarePanel";
import { LocationSettings } from "./LocationSettings";
import { useRememberedSetting } from "./preferences";

const jobStateLabels: Record<string, string> = {
  queued: "待機中",
  preparing: "準備中",
  running: "実行中",
  validating: "確認中",
  completed: "完了",
  failed: "失敗",
  cancelled: "中止",
  interrupted: "中断",
};

/**
 * 設定の確認・変更と、これまでの処理の履歴。
 *
 * 再セットアップは戻れない操作ではないが、押し間違いで制作画面から離れるため確認を挟む。
 */
export function SettingsDialog({
  onClose,
  onReconfigure,
}: {
  onClose: () => void;
  onReconfigure: () => void | Promise<void>;
}) {
  const [config, setConfig] = useState<api.SetupConfig | null>(null);
  const [status, setStatus] = useState<api.SetupStatus | null>(null);
  const [jobs, setJobs] = useState<api.Job[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // 人物候補を作り直したときの前の回。作り直しの確認では、ここで選んだ方が初めに選ばれる。
  const [candidateHistory, setCandidateHistory] = useRememberedSetting("candidate_history");

  useEffect(() => {
    void (async () => {
      try {
        setConfig(await api.getSetupConfig());
        setStatus(await api.getSetupStatus());
        setJobs(await api.listJobs());
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    })();
  }, []);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [busy, onClose]);

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const recent = (jobs ?? []).slice(0, 12);

  return (
    <div className="confirm-overlay" role="presentation" onMouseDown={() => { if (!busy) onClose(); }}>
      <div
        className="confirm-dialog vs-settings-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="vs-settings-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="vs-step-head">
          <h2 id="vs-settings-title">設定の確認・変更</h2>
          <button className="vs-btn ghost" onClick={onClose}>閉じる</button>
        </div>
        {error && <div className="vs-error">{error}</div>}

        <dl className="vs-settings-list">
          <div><dt>現在のモード</dt><dd>{api.setupModeLabel(config?.mode ?? "")}</dd></div>
          <div><dt>接続先</dt><dd>{config?.resolved_base_url || "（未設定）"}</dd></div>
          <div>
            <dt>参照中のモデル</dt>
            <dd>
              {config && config.external_model_roots.length > 0
                ? config.external_model_roots.join(" / ")
                : "アプリ管理領域のモデルのみ"}
            </dd>
          </div>
          <div><dt>接続状態</dt><dd>{status?.message ?? "確認中…"}</dd></div>
        </dl>
        <p className="vs-muted">
          参照先のモデルフォルダと既存ComfyUIの設定・ノード・モデルは変更しません。
          既存のComfyUIをそのまま使うときは、生成した画像・動画がそのComfyUIの出力フォルダにも保存されます（アプリはそこを片付けません）。
        </p>

        <h3 className="vs-subhead">人物候補の作り直し</h3>
        <div className="vs-field">
          <div className="vs-row">
            {(["discard", "keep"] as const).map((value) => (
              <label className="vs-choice" key={value}>
                <input
                  type="radio"
                  name="candidate-history"
                  checked={candidateHistory === value}
                  onChange={() => setCandidateHistory(value)}
                />
                <span>
                  {value === "discard"
                    ? "前の回を上書きする（基準画像だけ残して削除）"
                    : "前の回を履歴として残す"}
                </span>
              </label>
            ))}
          </div>
          <p className="vs-muted">
            作り直すたびに確認画面でも選び直せます。残した回は、人物の画面の「この回を削除」や
            「選ばなかった候補を削除」で、あとからまとめて消せます。
          </p>
        </div>

        <h3 className="vs-subhead">置き場所</h3>
        <LocationSettings />

        <div className="vs-row">
          <button className="vs-btn" disabled={busy} onClick={() => void run(async () => {
            await api.resetSetup();
            await onReconfigure();
          })}>
            ✏️ 設定を変更する
          </button>
          <button className="vs-btn danger" disabled={busy} onClick={() => void run(async () => {
            await api.clearSetupConfig();
            await onReconfigure();
          })}>
            🔄 設定を初期化して最初から
          </button>
        </div>
        <p className="vs-muted">
          「変更」は今の設定を保ったままセットアップを開きます。「初期化」は保存済みの設定を消してやり直します
          （どちらも既存のモデルやComfyUIは消しません）。
        </p>

        {/* 構成が変わった（GPUを載せ替えた等）ときに、ここから見直せるようにする。 */}
        <details className="vs-collapse">
          <summary>この端末に合う設定を見る</summary>
          <p className="vs-muted">
            GPU・メモリを調べ、この構成で勧められる生成方法を出します。適用すると各画面の
            初期表示が変わりますが、画面ごとに選び直せます。
          </p>
          <HardwarePanel videoEnabled={config?.video_enabled ?? true} />
        </details>

        <details className="vs-collapse">
          <summary>これまでの処理（最新{recent.length}件）</summary>
          {recent.length === 0 ? (
            <p className="vs-muted">まだ実行した処理はありません。</p>
          ) : (
            <div className="vs-job-history">
              {recent.map((job) => (
                <div key={job.id} className="vs-job-row">
                  <span className={`vs-status ${job.state === "completed" ? "ok" : job.state === "failed" ? "warn" : "idle"}`}>
                    {jobStateLabels[job.state] ?? job.state}
                  </span>
                  <strong>{api.jobKindLabel(job.kind)}</strong>
                  <span className="vs-muted">{job.message ?? ""}</span>
                </div>
              ))}
            </div>
          )}
        </details>
      </div>
    </div>
  );
}
