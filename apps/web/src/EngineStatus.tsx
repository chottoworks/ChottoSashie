import { useCallback, useEffect, useRef, useState } from "react";
import * as api from "./api";
import {
  isLongRunningKind,
  notifyFinished,
  requestNotificationPermission,
  setTitleProgress,
} from "./notify";

// 専用ComfyUIはtorchの読み込みなどで数十秒かかる。応答するまでこの時間まで待つ。
// モデルフォルダが大きいと読み込みだけで数分かかることがある。
const ENGINE_READY_TIMEOUT_MS = 360_000;
// 起動要求が弾かれたときに入れ直す間隔（前のプロセスがポートを手放すのを待つ）。
const START_RETRY_INTERVAL_MS = 15_000;
const PROBE_INTERVAL_MS = 20_000;
const ACTIVE_JOB_POLL_MS = 3000;

const isActiveJob = (job: api.Job): boolean =>
  !["completed", "failed", "cancelled", "interrupted"].includes(job.state);

/** 順番待ち（まだ実行が始まっていない）か。 */
const isWaitingJob = (job: api.Job): boolean => job.state === "queued";

/** ヘッダーに出す「いまどの環境につながっているか」の短い要約。 */
function engineSummary(status: api.SetupStatus | null): string {
  if (!status?.configured) return "未設定";
  if (status.mode === "existing_comfyui") return `既存ComfyUI: ${status.resolved_base_url}`;
  return `管理モード: ${status.resolved_base_url}`;
}

/**
 * 生成エンジンの接続状態と復旧。
 *
 * 生成が始められるかどうかは作品づくりの前提なので、制作画面に常時出す。
 * 管理モードなら、画面を開いた時点でこのアプリが専用ComfyUIを起動する。
 */
export function EngineStatus({ onReadyChanged }: { onReadyChanged?: (ready: boolean) => void }) {
  const [status, setStatus] = useState<api.SetupStatus | null>(null);
  const [ready, setReady] = useState(false);
  const [preparing, setPreparing] = useState(true);
  // 一度つながった接続が切れたか（復旧の案内を出す条件）。
  const [lost, setLost] = useState(false);
  const [recovering, setRecovering] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  // 準備の多重実行を防ぐ（画面の再描画で何度も起動しない）。
  const preparingRef = useRef(false);
  // いま動いている処理（GPUは1件ずつなので1件だけ見張れば足りる）。
  const [activeJob, setActiveJob] = useState<api.Job | null>(null);
  // 順番待ちの件数。GPUは1件ずつなので、待たされていることを見えるようにする。
  const [waitingCount, setWaitingCount] = useState(0);
  const [cancellingJob, setCancellingJob] = useState(false);
  // 直前まで動いていた長い処理。終わったことに気づけるよう、完了を知らせるために覚える。
  const watchedJob = useRef<api.Job | null>(null);

  const applyReady = useCallback(
    (value: boolean) => {
      setReady(value);
      onReadyChanged?.(value);
    },
    [onReadyChanged],
  );

  /** 接続を確かめ、管理モードなら黙って起動して応答を待つ。 */
  const ensureReady = useCallback(async (): Promise<boolean> => {
    if (preparingRef.current) return ready;
    preparingRef.current = true;
    setPreparing(true);
    try {
      let current = await api.getSetupStatus();
      setStatus(current);
      setMessage(current.message);
      if (current.reachable) {
        applyReady(true);
        return true;
      }
      if (!current.configured || current.mode === "existing_comfyui") {
        applyReady(false);
        return false;
      }
      let startFailed = false;
      try {
        // 起動が進行中なら、その状態がそのまま返る（重複起動はしない）。
        setMessage((await api.startRuntime()).message);
      } catch (error) {
        // 起動要求が通らなくても、読み込み中の可能性があるので待って確かめる。
        startFailed = true;
        setMessage(error instanceof Error ? error.message : "生成エンジンを起動できませんでした");
      }
      const deadline = Date.now() + ENGINE_READY_TIMEOUT_MS;
      let nextRetry = Date.now() + START_RETRY_INTERVAL_MS;
      while (Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 3000));
        current = await api.getSetupStatus();
        setStatus(current);
        if (current.reachable) {
          setMessage(current.message);
          applyReady(true);
          return true;
        }
        // 直前のプロセスがポートを握っていて起動要求が弾かれることがある。
        // 空くのを待って自分で入れ直す（待つだけだと誰も起動しないまま終わる）。
        if (startFailed && Date.now() >= nextRetry) {
          nextRetry = Date.now() + START_RETRY_INTERVAL_MS;
          try {
            setMessage((await api.startRuntime()).message);
            startFailed = false;
          } catch (error) {
            setMessage(error instanceof Error ? error.message : "生成エンジンを起動できませんでした");
          }
        }
      }
      setMessage("生成エンジンの起動を確認できませんでした。設定を確認してください。");
      applyReady(false);
      return false;
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "接続状態を確認できませんでした");
      applyReady(false);
      return false;
    } finally {
      preparingRef.current = false;
      setPreparing(false);
    }
  }, [applyReady, ready]);

  useEffect(() => {
    void ensureReady();
    // ensureReady は状態を見て多重実行を防ぐため、初回だけ走らせれば足りる。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // つながっている間だけ見張り、切れたら案内を出す（切れたら見張りは止める）。
  useEffect(() => {
    if (!ready) return;
    const timer = window.setInterval(() => {
      void api
        .getSetupStatus()
        .then((next) => {
          setStatus(next);
          if (!next.reachable) {
            applyReady(false);
            setLost(true);
            setMessage(next.message);
          }
        })
        .catch(() => undefined);
    }, PROBE_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [applyReady, ready]);

  // 実行中の処理を見張る。作品づくりの操作領域ではなく、この状態表示にまとめる。
  // 長い処理は完了をデスクトップ通知とタイトルバーでも知らせる。
  useEffect(() => {
    let alive = true;
    const check = () => {
      api
        .listJobs()
        .then((jobs) => {
          if (!alive) return;
          const active = jobs.filter(isActiveJob);
          // 実行中の1件を優先して出す。無ければ先頭の順番待ちを出す。
          const running = active.find((job) => !isWaitingJob(job)) ?? active[0] ?? null;
          setActiveJob(running);
          setWaitingCount(active.filter((job) => job.id !== running?.id).length);
          const watched = watchedJob.current;
          if (running && isLongRunningKind(running.kind)) {
            if (!watched) void requestNotificationPermission();
            watchedJob.current = running;
            setTitleProgress(`(${Math.round(running.progress * 100)}%)`);
            return;
          }
          if (watched) {
            const finished = jobs.find((job) => job.id === watched.id);
            const label = api.jobKindLabel(watched.kind);
            notifyFinished(
              finished?.state === "completed" ? `${label}が完了しました` : `${label}が終了しました`,
              finished?.message ?? "",
            );
            watchedJob.current = null;
          }
          setTitleProgress(null);
        })
        .catch(() => undefined);
    };
    check();
    const timer = window.setInterval(check, ACTIVE_JOB_POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
      setTitleProgress(null);
    };
  }, []);

  async function handleRecover() {
    if (recovering) return;
    setRecovering(true);
    try {
      if (await ensureReady()) setLost(false);
    } finally {
      setRecovering(false);
    }
  }

  async function handleCancelActive() {
    if (!activeJob || cancellingJob) return;
    setCancellingJob(true);
    try {
      await api.cancelJob(activeJob.id);
      setActiveJob(null);
    } catch {
      // 中止できなくても状態表示は続ける（次の確認で実際の状態に追いつく）。
    } finally {
      setCancellingJob(false);
    }
  }

  return (
    <>
      {lost && (
        <div className="vs-engine-toast" role="status">
          <span>生成エンジンとの接続が切れました。</span>
          <button className="vs-btn ghost" disabled={recovering} onClick={() => void handleRecover()}>
            {recovering ? "復旧しています…" : "再接続・再起動"}
          </button>
        </div>
      )}
      <div className="vs-engine-bar">
        <span className={ready ? "vs-live-dot" : "vs-offline-dot"}>
          {ready
            ? `生成準備完了（${engineSummary(status)}）`
            : preparing
              ? `生成エンジンを準備しています…（${engineSummary(status)}）`
              : `生成エンジンに接続できません（${engineSummary(status)}）`}
        </span>
        {!ready && !preparing && (
          <button className="vs-btn ghost" disabled={recovering} onClick={() => void handleRecover()}>
            {recovering ? "接続しています…" : "再接続・再起動"}
          </button>
        )}
        {message && !ready && <span className="vs-muted">{message}</span>}
        <span className="vs-engine-spacer" />
        {activeJob ? (
          <span className="vs-engine-job">
            {api.jobKindLabel(activeJob.kind)}
            {" "}
            {isWaitingJob(activeJob)
              ? "順番待ち"
              : `${Math.round(activeJob.progress * 100)}%`}
            {waitingCount > 0 && <span className="vs-muted">ほか{waitingCount}件が順番待ち</span>}
            <button className="vs-btn ghost" disabled={cancellingJob} onClick={() => void handleCancelActive()}>
              {cancellingJob ? "中止しています…" : "中止する"}
            </button>
          </span>
        ) : (
          <span className="vs-muted">実行中の処理はありません</span>
        )}
        {status?.data_dir && <span className="vs-muted">保存先: {status.data_dir}</span>}
      </div>
    </>
  );
}
