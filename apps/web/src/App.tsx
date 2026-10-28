import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import * as api from "./api";
import type { AssetKind, Project, StillImage } from "./api";
import { CharacterStudio } from "./CharacterStudio";
import { EngineStatus } from "./EngineStatus";
import { HardwarePanel } from "./HardwarePanel";
import { LocationSettings } from "./LocationSettings";
import { SettingsDialog } from "./SettingsDialog";
import { EditableName } from "./EditableName";
import { useLightbox } from "./Lightbox";
import { PortraitCropper } from "./PortraitCropper";
import { formatBytes, safeFileName } from "./format";
import { chosenFigureId, withChosenFigure } from "./stillImageFigure";
import { useRememberedSetting } from "./preferences";
import { stillImageUsageItems, useDeletion } from "./useDeletion";

function useJobRunner() {
  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState(0);
  const [message, setMessage] = useState("");
  // 中止できるように、実行中のジョブIDを持つ。
  const [jobId, setJobId] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);
  // どのカード（素材・カット）の処理かを覚えて、進捗と中止をその場に出す。
  const [activeKey, setActiveKey] = useState<string | null>(null);

  const run = useCallback(
    async (
      start: () => Promise<{ job_id: string }>,
      onDone: () => void | Promise<void>,
      setError: (value: string | null) => void,
      key: string | null = null,
    ) => {
      setActiveKey(key);
      setRunning(true);
      setProgress(0);
      setMessage("開始しています");
      setError(null);
      try {
        const { job_id } = await start();
        setJobId(job_id);
        const job = await api.pollJob(job_id, (j) => {
          setProgress(j.progress);
          setMessage(j.message ?? "");
        });
        // 中止は利用者の操作なので、失敗としては扱わない。
        if (job.state === "cancelled") {
          await onDone();
          return;
        }
        if (job.state !== "completed") {
          throw new Error(job.message ?? "処理に失敗しました");
        }
        await onDone();
      } catch (error) {
        setError(error instanceof Error ? error.message : String(error));
      } finally {
        setRunning(false);
        setCancelling(false);
        setJobId(null);
        setActiveKey(null);
      }
    },
    [],
  );

  const cancel = useCallback(
    async (setError: (value: string | null) => void) => {
      if (!jobId || cancelling) return;
      setCancelling(true);
      try {
        await api.cancelJob(jobId);
      } catch (error) {
        setError(error instanceof Error ? error.message : String(error));
        setCancelling(false);
      }
    },
    [cancelling, jobId],
  );

  return { running, progress, message, run, cancel, cancelling, jobId, activeKey };
}

/**
 * 実行中の処理の進捗と「中止」。
 *
 * GPUを使う処理は一度に1件しか走らないため、止められないと次の操作に進めない。
 * 進捗を出すところには必ず中止も置く。
 */
function Progress({
  value,
  label,
  runner,
  setError,
}: {
  value: number;
  label: string;
  runner?: { cancel: (setError: (value: string | null) => void) => Promise<void>; cancelling: boolean; jobId: string | null };
  setError?: (value: string | null) => void;
}) {
  return (
    <div className="vs-progress-block">
      <div className="vs-muted">{label}</div>
      <div className="vs-progress">
        <div style={{ width: `${Math.round(value * 100)}%` }} />
      </div>
      {runner?.jobId && setError && (
        <button
          className="vs-btn ghost vs-cancel"
          disabled={runner.cancelling}
          onClick={() => void runner.cancel(setError)}
        >
          {runner.cancelling ? "中止しています…" : "中止する"}
        </button>
      )}
    </div>
  );
}

// ============================ セットアップ ============================

/** 状態を一目で示す短いラベル。 */
function StatusPill({ tone, children }: { tone: "ok" | "warn" | "idle"; children: ReactNode }) {
  return <span className={`vs-status ${tone}`}>{children}</span>;
}

/**
 * 「不足への対処」の1手順。済んだ手順は自動で畳み、残りの作業だけが開いた状態になる。
 */
function SubStep({
  index,
  title,
  done,
  doneLabel = "完了",
  todoLabel = "要対応",
  keepOpen = false,
  children,
}: {
  index: number;
  title: string;
  done: boolean;
  doneLabel?: string;
  todoLabel?: string;
  /** 済んでいても開いておく（任意で足せるものが残っているときなど）。 */
  keepOpen?: boolean;
  children: ReactNode;
}) {
  return (
    <details className={`vs-substep ${done ? "done" : ""}`} open={!done || keepOpen}>
      <summary>
        <span className="vs-substep-index">{index}</span>
        <strong>{title}</strong>
        <StatusPill tone={done ? "ok" : "warn"}>{done ? doneLabel : todoLabel}</StatusPill>
      </summary>
      <div className="vs-substep-body">{children}</div>
    </details>
  );
}

function SetupPanel({
  wizard = false,
  onCompleted,
}: {
  wizard?: boolean;
  onCompleted?: () => void | Promise<void>;
}) {
  const [config, setConfig] = useState<api.SetupConfig | null>(null);
  const [status, setStatus] = useState<api.SetupStatus | null>(null);
  const [requirements, setRequirements] = useState<api.RequirementsReport | null>(null);
  const [runtime, setRuntime] = useState<api.ManagedRuntimeStatus | null>(null);
  const [plan, setPlan] = useState<api.ModelPlan | null>(null);
  const [error, setError] = useState<string | null>(null);
  // 必要環境の確認だけは、押した場所のすぐ下に結果と対処を出したいので分けて持つ。
  const [checking, setChecking] = useState(false);
  const [checkError, setCheckError] = useState<string | null>(null);
  const [planBusy, setPlanBusy] = useState(false);

  const [comfyui, setComfyui] = useState<api.ComfyUIStatus | null>(null);
  // 動作要件の判定は、この端末を調べた結果から取る（HardwarePanel が渡してくる）。
  const [hardware, setHardware] = useState<api.HardwareReport | null>(null);
  const [acceptBelowMinimum, setAcceptBelowMinimum] = useState(false);
  const [acceptH3License, setAcceptH3License] = useState(false);
  // 何も無い環境から始める人が多いので、既定はアプリが一式を用意する方にする。
  const [mode, setMode] = useState<api.SetupMode>("managed_dedicated");
  // 既存のComfyUIを選んだときの初めの値は、ComfyUIの既定の番号。
  const [baseUrl, setBaseUrl] = useState("http://127.0.0.1:8188");
  const [port, setPort] = useState(8199);
  const [externalRoots, setExternalRoots] = useState("");
  const [comfyPath, setComfyPath] = useState("");
  const [pythonPath, setPythonPath] = useState("");
  // 不足モデルの取得先。空文字は共有フォルダ。
  const [downloadRoot, setDownloadRoot] = useState("");
  // 既定は共有フォルダのComfyUIを使う。既存インストールの流用は明示的な選択にする。
  const [reuseExisting, setReuseExisting] = useState(false);
  // 動画も作るか。切ると、シーン・カット・書き出しの画面と、動画用モデルの取得が省かれる。
  const [videoEnabled, setVideoEnabled] = useState(true);

  const download = useJobRunner();
  const provision = useJobRunner();
  // 足りない道具（書き出しに使うしくみなど）をアプリが用意する処理。
  const tooling = useJobRunner();
  // 用意の失敗は、押したボタンの隣に出す。画面上端のエラー欄だけだと、
  // ここから300行以上離れていて見えず、「押しても何も起きない」に見える。
  const [toolError, setToolError] = useState<{ id: string; message: string } | null>(null);

  const reload = useCallback(async () => {
    try {
      const c = await api.getSetupConfig();
      setConfig(c);
      // まだ接続方式を保存していないうちは、記録上のモードは仮の値なので出さない。
      const effectiveMode = c.configured ? c.mode : "managed_dedicated";
      setMode(effectiveMode);
      if (c.base_url) setBaseUrl(c.base_url);
      setPort(c.port);
      setExternalRoots(c.external_model_roots.join("\n"));
      setDownloadRoot(c.model_download_root ?? "");
      setVideoEnabled(c.video_enabled ?? true);
      setStatus(await api.getSetupStatus());
      const comfy = await api.getComfyuiStatus();
      setComfyui(comfy);
      // 共有フォルダの本体は設定に書かず、アプリが自分で見つける。ここで扱うのは
      // 利用者が指定した「既存インストールの流用」だけ。
      const borrowed = Boolean(c.comfyui_path) && comfy.source === "existing";
      setReuseExisting(borrowed);
      setComfyPath(borrowed ? c.comfyui_path : "");
      setPythonPath(borrowed ? c.python_path : "");
      // すでに動いていることもあるので、起動状態は画面を開いた時点で読んでおく
      // （押す必要のない「起動」を残さない）。
      setRuntime(await api.getRuntimeStatus());
      // 専用環境モードのときは、確認前でも不足量が分かるように取得計画を読んでおく。
      setPlan(effectiveMode === "existing_comfyui" ? null : await api.getModelPlan());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  async function guarded(fn: () => Promise<void>) {
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function save() {
    setError(null);
    try {
      await api.putSetupConfig({
        mode,
        base_url: baseUrl,
        port,
        // 流用を選んでいないときは空にする。共有フォルダの本体はアプリが自分で
        // 見つけるため、場所を書き写しておく必要はない。
        comfyui_path: reuseExisting ? comfyPath.trim() : "",
        python_path: reuseExisting ? pythonPath.trim() : "",
        external_model_roots: externalRoots
          .split("\n")
          .map((s) => s.trim())
          .filter(Boolean),
        // 既存フォルダへ書き足すのは、この画面で明示的に選んだときだけ。
        model_download_root: mode === "managed_external_models" ? downloadRoot.trim() : "",
        video_enabled: videoEnabled,
      });
      // 設定が変わると充足状況も変わるため、確認結果はいったん捨てる。
      setRequirements(null);
      setCheckError(null);
      await reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  /** 必要環境の確認。接続できないときは対処へ誘導したいので、結果をこの場に出す。 */
  const checkRequirements = useCallback(async () => {
    setChecking(true);
    setCheckError(null);
    try {
      setRequirements(await api.getRequirements());
    } catch (e) {
      setRequirements(null);
      setCheckError(e instanceof Error ? e.message : String(e));
    } finally {
      setChecking(false);
    }
  }, []);

  // 確認はComfyUIに聞いて行うので、聞ける状態になったら自動で始める（押させない）。
  // 既存のComfyUIをそのまま使う設定では、設定を保存した（読み込んだ）時点で聞く。
  // 専用ComfyUIでは、起動して応答するようになった時点で聞く——止まっている間に聞いても
  // 「接続できません」になるだけで、何が足りないのかは分からない。
  // 設定を保存し直したとき（接続先や「動画も作るか」が変わる）も、聞き直す。
  const engineReachable = runtime?.reachable === true;
  const savedMode = config?.configured ? config.mode : null;
  const canCheckNow =
    savedMode === "existing_comfyui" || (savedMode !== null && engineReachable);
  const checkKey = canCheckNow
    ? `${config?.resolved_base_url}|${config?.updated_at ?? ""}`
    : null;
  useEffect(() => {
    if (checkKey !== null) void checkRequirements();
  }, [checkKey, checkRequirements]);

  /** モデルを取り終えたら、結果を新しくする（専用ComfyUIが動いているときだけ聞ける）。 */
  const recheckIfReachable = useCallback(async () => {
    const current = await api.getRuntimeStatus().catch(() => null);
    if (current) setRuntime(current);
    if (current?.reachable) await checkRequirements();
  }, [checkRequirements]);

  // 起動を押しても、応答するまでは state が "starting" のまま。誰も見に行かないと
  // 進捗が出ないまま止まったように見える。起動が済んだあとも、アプリの外で止められた
  // ことに気づけるよう追い続ける（読み込み中は短く、それ以外は控えめに）。
  // この画面を閉じれば止まるので、制作中は動かない。
  const runtimeStarting = runtime?.state === "starting";
  // 押した瞬間に表示が変わらないと、効いたのかどうか分からない。応答を待たずに
  // 切り替えるため、要求を出している間だけ覚えておく。
  const [runtimeAction, setRuntimeAction] = useState<"start" | "stop" | null>(null);
  useEffect(() => {
    if (mode === "existing_comfyui") return;
    const timer = window.setInterval(
      () => {
        void api.getRuntimeStatus().then(setRuntime).catch(() => undefined);
      },
      runtimeStarting ? 2000 : 10000,
    );
    return () => window.clearInterval(timer);
  }, [mode, runtimeStarting]);

  const refreshPlan = useCallback(async () => {
    setPlanBusy(true);
    try {
      await guarded(async () => setPlan(await api.getModelPlan()));
    } finally {
      setPlanBusy(false);
    }
  }, []);

  // ComfyUIの取得・展開。不足ノードの説明のそばと、対処手順の両方から呼ぶ。
  // 入れ替えの前に生成エンジンは停止されるので、起動状態も読み直す
  // （「起動中」の表示が残ったままにならないように）。
  const startProvision = () =>
    void provision.run(
      async () => {
        const job = await api.provisionComfyui();
        return { job_id: job.id };
      },
      async () => {
        setComfyui(await api.getComfyuiStatus());
        setRuntime(await api.getRuntimeStatus());
      },
      setError,
    );

  // ComfyUIの外で動く道具を用意する。終わったら確認をやり直し、揃ったことを画面へ出す。
  const startToolProvision = (toolId: string) =>
    void tooling.run(
      async () => {
        const job = await api.provisionTool(toolId);
        return { job_id: job.id };
      },
      checkRequirements,
      (value) => setToolError(value === null ? null : { id: toolId, message: value }),
      toolId,
    );

  const isManaged = mode !== "existing_comfyui";

  const comfyReady = !isManaged || comfyui?.provisioned === true;
  // 入っているComfyUIが古いと、新しいモデルのノードが無く「不足ノード」になる。
  const comfyOutdated = comfyui?.update_available === true;
  // ComfyUI本体はアプリの持ち物。借用中でなければ準備・更新できる。
  const comfySource = comfyui?.source ?? "";
  const canProvision = comfyui?.provisioned !== true || comfyui?.can_update === true;
  // 任意の機能（MiniMax H3）の取得は、必須が揃ったあとでも辿り着けるようにする。
  const optionalPending =
    requirements?.capabilities.some((cap) => cap.optional && !cap.ready) === true;
  // 任意のモデル（MiniMax H3）は、必須の準備状況と混ぜない。
  const requiredComponents = plan?.components.filter((c) => !c.optional) ?? [];
  const missingModels = requiredComponents.filter((c) => !api.isModelReady(c.state));
  const readyModels = requiredComponents.filter((c) => api.isModelReady(c.state));
  const h3Components = plan?.components.filter((c) => c.group === "minimax_h3") ?? [];
  const h3Ready = h3Components.length > 0 && h3Components.every((c) => api.isModelReady(c.state));
  const h3DownloadBytes = h3Components.reduce(
    (sum, c) => sum + (api.isModelReady(c.state) ? 0 : c.download_bytes),
    0,
  );
  const modelsReady = plan !== null && plan.required_download_bytes === 0;
  const requirementsReady = requirements?.ready === true;
  const canComplete = Boolean(config?.configured) && requirementsReady;

  // 手順の番号は、その手順に固定で持たせる。描画のたびに並び順で振り直すと、
  // 判定材料が遅れて届いた手順が現れた瞬間に、同じ番号が別の作業を指してしまう。
  const showComfyStep = isManaged;
  // 判定できなかったときは満たしていない扱いにしない（当てずっぽうで止めない）。
  const h3Notice = plan?.license_notices?.minimax_h3;
  const belowMinimum =
    hardware?.recommendation.requirements.known === true
    && !hardware.recommendation.requirements.meets_minimum;
  const showModelStep = isManaged;
  // 用意することが残っているか。この段の見出しと、対処一覧の表示条件に同じものを使う。
  const preparationPending = !requirementsReady || comfyOutdated || optionalPending;

  let stepNumber = 1;
  // 保存先はいちばん最初に決める。あとから変えられるが、作品を作り始めてからだと
  // 移す手間がかかる。デスクトップ版でだけ選べる（ブラウザでは変えられない）。
  const locationStep = api.isDesktop() ? stepNumber++ : 0;
  const purposeStep = stepNumber++;
  // この端末に何ができるかは、モデルの取得や生成方法を決める前に知っておきたい。
  const hardwareStep = stepNumber++;
  const configStep = stepNumber++;
  // 環境を作る段と、作った環境を確かめる段は分ける。同じ段に混ぜると、まだ本体が
  // 無い時点の「確認する」が何を意味するのか分からなくなる。
  // 確かめるにはComfyUIが動いている必要があるので、用意の最後の手順として同じ段に置く。
  const prepareStep = stepNumber++;
  const doneStep = stepNumber++;

  /** 「動画も作る」の切り替え。設定を保存する前でも選べるよう、その場で記録する。 */
  const changeVideoEnabled = async (next: boolean) => {
    setVideoEnabled(next);
    setError(null);
    try {
      // 接続方式をまだ保存していない段階では書き込む行が無いので何も起きない。
      // そのときは接続方式の保存時に、この選択が一緒に書き込まれる。
      await api.putVideoEnabled(next);
      await reload();
      if (onCompleted) await onCompleted();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div>
      {error && <div className="vs-error">{error}</div>}

      {locationStep > 0 && (
        <div className="vs-card">
          <div className="vs-step-head">
            <h2>
              <span className="vs-step-no">STEP {locationStep}</span>
              作品の保存先を決める
            </h2>
          </div>
          <p className="vs-muted">
            このままで構いません。別のドライブへ置きたいときに変えます。
          </p>
          {/* ComfyUIとモデルの置き場所はここでは決めない。ComfyUIをどう使うかを
              決める前に置き場所だけ先に聞いても答えようがない（既存のものを使うなら
              関係が無い）。専用ComfyUIを選んだときの詳細設定で変えられる。 */}
          <LocationSettings section="data" />
        </div>
      )}

      <div className="vs-card">
        <div className="vs-step-head">
          <h2>
            <span className="vs-step-no">STEP {purposeStep}</span>
            何を作りますか
          </h2>
          <StatusPill tone="ok">{videoEnabled ? "挿絵と動画" : "挿絵だけ"}</StatusPill>
        </div>
        <p className="vs-muted">
          どちらでも、作品・登場人物・背景・小物から静止画を作るところまでは同じです。
          動画を作る場合だけ、その静止画を動かす「シーン・カット」と「書き出し」が増えます。
          あとから変えられます。
        </p>
        <div className="vs-mode-cards">
          <button
            type="button"
            className={`vs-mode-card ${videoEnabled ? "" : "active"}`}
            onClick={() => void changeVideoEnabled(false)}
          >
            <strong>挿絵だけ作る</strong>
            <span className="vs-muted">
              静止画までで完成します。動画用のモデル（数十GB）は取得しません。
            </span>
          </button>
          <button
            type="button"
            className={`vs-mode-card ${videoEnabled ? "active" : ""}`}
            onClick={() => void changeVideoEnabled(true)}
          >
            <strong>挿絵と動画を作る</strong>
            <span className="vs-muted">
              作った静止画を動かしてカットにし、1本の動画に書き出せます。
            </span>
          </button>
        </div>
      </div>

      <div className="vs-card">
        <div className="vs-step-head">
          <h2>
            <span className="vs-step-no">STEP {hardwareStep}</span>
            この端末に合う設定を見る
          </h2>
        </div>
        <p className="vs-muted">
          GPUのVRAMと世代、メモリの量で、動かせるモデルと生成方法、1枚あたりの待ち時間が変わります。
          調べた構成と、その構成で勧められる設定を出します。適用しなければ何も変わりません。
        </p>
        <HardwarePanel videoEnabled={videoEnabled} onReport={setHardware} />
      </div>

      <div className="vs-card">
          <div className="vs-step-head">
            <h2>
              <span className="vs-step-no">STEP {configStep}</span>
              ComfyUIの使い方を決める
            </h2>
            <StatusPill tone={config?.configured ? "ok" : "warn"}>
              {config?.configured ? "保存済み" : "未保存"}
            </StatusPill>
          </div>
          <p className="vs-muted">ComfyUIをどう使うかを選びます。設定後は各生成が自動的にこの接続先を使います。</p>
          <div className="vs-field">
            <label>モード</label>
            <select className="vs-select" value={mode} onChange={(e) => setMode(e.target.value as api.SetupMode)}>
              <option value="managed_dedicated">完全に専用の環境（モデルもアプリ管理）</option>
              <option value="managed_external_models">専用ComfyUIを起動し、既存モデルを参照する</option>
              <option value="existing_comfyui">既存のComfyUIをそのまま使う</option>
            </select>
          </div>
          {!isManaged && (
            <div className="vs-field">
              <label>接続先のアドレス</label>
              <input className="vs-input" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} />
              <span className="vs-muted">
                ComfyUIの既定は 8188 番です（ComfyUI Desktop は 8000 番）。起動時に表示されるアドレスを入れてください。
              </span>
            </div>
          )}
          {isManaged && mode === "managed_external_models" && (
            <>
              <div className="vs-field">
                <label>既存モデルフォルダ（1行に1つ・読み取り専用で参照します）</label>
                <textarea
                  className="vs-textarea"
                  value={externalRoots}
                  onChange={(e) => setExternalRoots(e.target.value)}
                  placeholder={"例: C:\\ComfyUI\\models"}
                />
              </div>
              <div className="vs-field">
                <label>不足モデルの保存先</label>
                <select
                  className="vs-select"
                  value={downloadRoot}
                  onChange={(e) => setDownloadRoot(e.target.value)}
                >
                  <option value="">アプリの共有フォルダ（既存フォルダは変更しません）</option>
                  {externalRoots
                    .split("\n")
                    .map((s) => s.trim())
                    .filter(Boolean)
                    .map((root) => (
                      <option key={root} value={root}>
                        {root} に追加する
                      </option>
                    ))}
                </select>
                <p className="vs-muted">
                  {downloadRoot
                    ? "選んだ既存フォルダへ不足分を書き足します（種別のフォルダ名は既存の並びに合わせます）。同名のファイルがある場合は上書きしません。"
                    : "既存フォルダは読み取りのままにし、不足分は共有フォルダへ取得します。"}
                </p>
              </div>
            </>
          )}
          {isManaged && !reuseExisting && (
            <p className="vs-muted">
              アプリ専用のComfyUI（Python同梱）を用意して使います。既存のComfyUIには一切触れません。
            </p>
          )}
          {isManaged && (
            <details className="vs-collapse" open={reuseExisting}>
              <summary>詳細設定（上級者向け）</summary>
              <div className="vs-field" style={{ marginTop: 10 }}>
                <label>起動ポート</label>
                <input
                  className="vs-input"
                  type="number"
                  value={port}
                  onChange={(e) => setPort(Number(e.target.value))}
                />
              </div>
              <label style={{ cursor: "pointer" }}>
                <input
                  type="checkbox"
                  checked={reuseExisting}
                  onChange={(e) => setReuseExisting(e.target.checked)}
                />{" "}
                既存のComfyUI本体を流用する
              </label>
              {reuseExisting && (
                <>
                  <p className="vs-muted">
                    既存インストールは<strong>読み取り専用で借用</strong>します。入出力・一時ファイル・キャッシュ・
                    モデル参照設定はすべてアプリ専用領域へ隔離して起動します。
                  </p>
                  <div className="vs-field">
                    <label>既存ComfyUIフォルダ（main.pyのある場所）</label>
                    <input
                      className="vs-input"
                      placeholder={"例: C:\\AI\\ComfyUI"}
                      value={comfyPath}
                      onChange={(e) => setComfyPath(e.target.value)}
                    />
                  </div>
                  <div className="vs-field">
                    <label>既存ComfyUI用のPython</label>
                    <input
                      className="vs-input"
                      placeholder={"例: C:\\AI\\PythonEnv\\Scripts\\python.exe"}
                      value={pythonPath}
                      onChange={(e) => setPythonPath(e.target.value)}
                    />
                  </div>
                </>
              )}
              {/* 既定はアプリの共有置き場。分けたい人だけがここで変える。
                  置き場所を変えられるのはデスクトップ版だけ。 */}
              {api.isDesktop() && <LocationSettings section="shared" />}
            </details>
          )}
          <div className="vs-row">
            <button className="vs-btn" onClick={() => void save()}>
              この内容で保存
            </button>
            {status && <span className="vs-muted">{status.message}</span>}
          </div>
      </div>

      <div className="vs-card">
        <div className="vs-step-head">
          <h2>
            <span className="vs-step-no">STEP {prepareStep}</span>
            ComfyUI環境を用意
          </h2>
          {!config?.configured ? (
            <StatusPill tone="idle">設定待ち</StatusPill>
          ) : (
            <StatusPill tone={preparationPending ? "warn" : "ok"}>
              {preparationPending ? "用意することがあります" : "用意できています"}
            </StatusPill>
          )}
        </div>
        <p className="vs-muted">
          生成に使うComfyUI本体とモデルを用意し、必要なノードとモデルが揃っているかを確かめます。
        </p>
        {!config?.configured && (
          <span className="vs-warn">先に接続設定を保存してください</span>
        )}
        {/* 保存前は対処のしようがないので出さない（まず設定を保存してもらう）。
            揃ったあとも手順は消さない。消すとこの段が見出しだけになり、何が済んだのか
            も、入れ替えややり直しをどこからするのかも分からなくなる。済んだ手順は
            SubStep側が畳む。 */}
        {config?.configured && (
          <div className="vs-remedy">
            <div className="vs-remedy-head">
              <strong>{requirementsReady ? "追加でできること" : "不足への対処"}</strong>
              <span className="vs-muted">
                {requirementsReady
                  ? "制作は始められます。ここは使いたい機能を足すための場所です。"
                  : "上から順に進めると、この画面のまま環境が揃います。"}
              </span>
            </div>
            <div className="vs-substeps">
              {isManaged ? (
                <>
                  {showComfyStep && (
                  <SubStep
                    index={1}
                    title="ComfyUI本体の準備"
                    done={comfyReady && !comfyOutdated}
                    doneLabel={
                      comfyui?.source === "shared"
                        ? "準備済み（共有フォルダ）"
                        : comfyui?.source === "existing"
                          ? "準備済み（指定した既存ComfyUI）"
                          : "準備済み"
                    }
                  >
                    <p className="vs-muted">
                      ComfyUI本体(Python同梱)をアプリが取得・展開します。利用者側の準備は不要です。
                      置き場所はアプリの共有フォルダです。
                    </p>
                    {comfyui?.shared_root && (
                      <p className="vs-muted">共有フォルダ: {comfyui.shared_root}</p>
                    )}
                    {comfyOutdated && (
                      <div className="vs-note warn">
                        入っているComfyUIが古いため（{comfyui?.installed_version} →{" "}
                        {comfyui?.target_version}）、新しいモデルのノードがありません。
                        入れ替えると解消します。作った作品や取得済みのモデルはそのまま残ります。
                      </div>
                    )}
                    <div className="vs-row">
                      <button
                        className="vs-btn"
                        disabled={provision.running || !canProvision}
                        onClick={startProvision}
                      >
                        {provision.running
                          ? "準備中..."
                          : comfyui?.provisioned !== true
                            ? "ComfyUI環境を準備"
                            : comfyOutdated
                              ? "最新版に入れ替える"
                              : "準備し直す"}
                      </button>
                      {comfyui && canProvision && (
                        <span className="vs-muted">{comfyui.target_version} を取得します</span>
                      )}
                      {comfyui && !canProvision && (
                        <span className="vs-muted">
                          指定した既存のComfyUIを使っています（アプリは書き換えません）
                        </span>
                      )}
                    </div>
                    {provision.running && <Progress value={provision.progress} label={provision.message} runner={provision} setError={setError} />}
                  </SubStep>
                  )}

                  {showModelStep && (
                  <SubStep
                    index={2}
                    title="モデルの取得"
                    done={modelsReady}
                    doneLabel={h3Ready ? "すべて取得済み" : "必須はすべて取得済み"}
                  >
                    {/* 数十GBを取ったあとで「実用にならない」と気づくのは遅い。
                        止めはしないが、承知したことを一度確かめる。 */}
                    {belowMinimum && !modelsReady && (
                      <div className="vs-note warn">
                        <strong>この端末は動作要件を満たしていません</strong>
                        <div>{hardware?.recommendation.requirements.message}</div>
                        <label className="vs-accept">
                          <input
                            type="checkbox"
                            checked={acceptBelowMinimum}
                            onChange={(e) => setAcceptBelowMinimum(e.target.checked)}
                          />
                          待ち時間が長くなることを承知のうえで進める
                        </label>
                      </div>
                    )}
                    <div className="vs-row">
                      <button
                        className="vs-btn"
                        disabled={
                          download.running
                          || modelsReady
                          || plan === null
                          || (belowMinimum && !acceptBelowMinimum)
                        }
                        onClick={() =>
                          void download.run(
                            async () => {
                              const job = await api.startModelDownload();
                              return { job_id: job.id };
                            },
                            async () => {
                              setPlan(await api.getModelPlan());
                              await recheckIfReachable();
                            },
                            setError,
                          )
                        }
                      >
                        {modelsReady
                          ? "取得の必要はありません"
                          : download.running
                            ? "取得中..."
                            : plan
                              ? `不足分を取得（${formatBytes(plan.required_download_bytes)}）`
                              : "不足分を取得"}
                      </button>
                      <button className="vs-btn ghost" disabled={planBusy} onClick={() => void refreshPlan()}>
                        {planBusy ? "確認中..." : "取得状況を再確認"}
                      </button>
                    </div>
                    {download.running && <Progress value={download.progress} label={download.message} runner={download} setError={setError} />}

                    {plan && (
                      <>
                        <div className={`vs-banner ${modelsReady ? "ok" : "warn"}`}>
                          {modelsReady ? (
                            <>
                              <strong>✓ 必要なモデルはすべて揃っています</strong>
                              <span>
                                {plan.components.length}件・合計{" "}
                                {formatBytes(plan.components.reduce((sum, c) => sum + c.expected_bytes, 0))}
                                。追加のダウンロードはありません。
                              </span>
                            </>
                          ) : (
                            <>
                              <strong>{missingModels.length}件が不足しています</strong>
                              <span>
                                必要ダウンロード {formatBytes(plan.required_download_bytes)}
                                {plan.free_bytes !== null && ` ／ 保存先の空き ${formatBytes(plan.free_bytes)}`}
                              </span>
                            </>
                          )}
                        </div>
                        {!plan.enough_space && (
                          <div className="vs-note warn">保存先の空き容量が足りません。{plan.message}</div>
                        )}
                        <p className="vs-muted">保存先: {plan.download_label}</p>
                        {mode === "managed_external_models" && (
                          <p className="vs-muted">
                            参照フォルダで見つかったモデルファイル: {plan.external_candidate_files} 件
                            {plan.external_candidate_files === 0
                              && "（modelsフォルダそのものを指定しているか確認してください）"}
                          </p>
                        )}
                        {missingModels.length > 0 && (
                          <details className="vs-collapse" open>
                            <summary>
                              不足しているモデル（{missingModels.length}件・
                              {formatBytes(missingModels.reduce((sum, c) => sum + c.download_bytes, 0))}）
                            </summary>
                            <div className="vs-model-list">
                              {missingModels.map((c) => (
                                <div key={c.id} className="vs-model-row">
                                  <span>{c.display_name}</span>
                                  <b>
                                    {formatBytes(c.expected_bytes)}・{api.modelStateLabel(c.state)}
                                    {c.license && `・${c.license}`}
                                  </b>
                                </div>
                              ))}
                            </div>
                          </details>
                        )}
                        {readyModels.length > 0 && (
                          <details className="vs-collapse">
                            <summary>準備済みのモデル（{readyModels.length}件）</summary>
                            <div className="vs-model-list">
                              {readyModels.map((c) => (
                                <div key={c.id} className="vs-model-row done">
                                  <span>✓ {c.display_name}</span>
                                  <b>
                                    {formatBytes(c.expected_bytes)}・{api.modelStateLabel(c.state)}
                                    {c.license && `・${c.license}`}
                                  </b>
                                </div>
                              ))}
                            </div>
                          </details>
                        )}
                        {/* 任意のモデルは、必須の準備と分けて「使いたい人だけ」取得できるようにする。 */}
                        {h3Components.length > 0 && (
                          <details className="vs-collapse" open={h3Ready}>
                            <summary>
                              音声付きの動画を作る（MiniMax H3・任意）
                              {h3Ready ? "：取得済み" : `：未取得・${formatBytes(h3DownloadBytes)}`}
                            </summary>
                            <p className="vs-muted">
                              カットの動画を、音（声・効果音・音楽）まで一緒に生成できるモデルです。
                              使わないなら取得しなくて構いません。容量が大きいので時間がかかります。
                            </p>
                            {/* 利用条件に制限があるモデルは、取得を押す前に条件を読ませる。
                                ffmpegと同じ考え方で、何を入れるのかを隠さない。 */}
                            {h3Notice && !h3Ready && (
                              <div className="vs-note warn">
                                <strong>{h3Notice.title_ja}</strong>
                                <ul className="vs-license-points">
                                  {h3Notice.points_ja.map((point) => (
                                    <li key={point}>{point}</li>
                                  ))}
                                </ul>
                                <div className="vs-muted">{h3Notice.note_ja}</div>
                                <div>
                                  <a href={h3Notice.url} target="_blank" rel="noreferrer">
                                    {h3Notice.license_name}（原文）
                                  </a>
                                </div>
                                <label className="vs-accept">
                                  <input
                                    type="checkbox"
                                    checked={acceptH3License}
                                    onChange={(e) => setAcceptH3License(e.target.checked)}
                                  />
                                  {h3Notice.consent_ja}
                                </label>
                              </div>
                            )}
                            <div className="vs-row">
                              <button
                                className="vs-btn ghost"
                                disabled={
                                  download.running
                                  || h3Ready
                                  || plan === null
                                  || (h3Notice !== undefined && !acceptH3License)
                                }
                                onClick={() =>
                                  void download.run(
                                    async () => {
                                      const job = await api.startModelDownload(["minimax_h3"]);
                                      return { job_id: job.id };
                                    },
                                    async () => {
                                      setPlan(await api.getModelPlan());
                                      await recheckIfReachable();
                                    },
                                    setError,
                                  )
                                }
                              >
                                {h3Ready
                                  ? "取得済み"
                                  : download.running
                                    ? "取得中..."
                                    : `MiniMax H3 を取得（${formatBytes(h3DownloadBytes)}）`}
                              </button>
                            </div>
                            <div className="vs-model-list">
                              {h3Components.map((c) => (
                                <div
                                  key={c.id}
                                  className={`vs-model-row ${api.isModelReady(c.state) ? "done" : ""}`}
                                >
                                  <span>
                                    {api.isModelReady(c.state) ? "✓ " : ""}
                                    {c.display_name}
                                  </span>
                                  <b>
                                    {formatBytes(c.expected_bytes)}・{api.modelStateLabel(c.state)}
                                    {c.license && `・${c.license}`}
                                  </b>
                                </div>
                              ))}
                            </div>
                          </details>
                        )}
                      </>
                    )}
                  </SubStep>
                  )}

                  <SubStep
                    index={3}
                    title="専用ComfyUIの起動"
                    done={runtime?.reachable === true}
                    doneLabel="起動済み"
                    todoLabel={runtimeStarting ? "起動しています…" : "要対応"}
                  >
                    <p className="vs-muted">
                      生成のために、アプリが専用ComfyUIを起動します。初回は読み込みに1分ほどかかることがあります。
                      起動できたら、続けて下の「ノードとモデルの確認」が自動で始まります。
                    </p>
                    <div className="vs-row">
                      <button
                        className="vs-btn"
                        disabled={!comfyReady || runtimeStarting || runtimeAction !== null}
                        onClick={() => {
                          setRuntimeAction("start");
                          void guarded(async () => {
                            setRuntime(await api.startRuntime());
                          }).finally(() => setRuntimeAction(null));
                        }}
                      >
                        {runtimeAction === "start" || runtimeStarting ? "起動しています…" : "起動"}
                      </button>
                      <button
                        className="vs-btn ghost"
                        disabled={runtimeAction !== null}
                        onClick={() => {
                          setRuntimeAction("stop");
                          void guarded(async () => setRuntime(await api.stopRuntime()))
                            .finally(() => setRuntimeAction(null));
                        }}
                      >
                        {runtimeAction === "stop" ? "停止しています…" : "停止"}
                      </button>
                      {runtime && <span className="vs-muted">{runtime.message}</span>}
                    </div>
                  </SubStep>

                  <SubStep
                    index={4}
                    title="ノードとモデルの確認"
                    done={requirementsReady}
                    doneLabel={optionalPending ? "必須は揃っています" : "すべて揃っています"}
                    todoLabel={
                      checking
                        ? "確認しています…"
                        : requirements
                          ? "不足があります"
                          : checkError
                            ? "接続できません"
                            : "未確認"
                    }
                    keepOpen={optionalPending}
                  >
                    <p className="vs-muted">
                      接続先のComfyUI（{config?.resolved_base_url || "未設定"}）に、使う機能のノードとモデルが
                      揃っているかを確かめます。
                    </p>
                    {/* 確かめる前は、いつ始まるのかを書く（押すものは無い）。 */}
                    {!requirements && !checkError && !checking && isManaged && (
                      <div className="vs-muted">専用ComfyUIを起動すると、自動で確かめます。</div>
                    )}
                    {checking && <div className="vs-muted">確かめています…</div>}
                    {checkError && (
                      <div className="vs-note warn">
                        <strong>ComfyUIへ接続できませんでした。</strong>
                        <div>{checkError}</div>
                        <div>
                          {isManaged
                            ? "上の「専用ComfyUIの起動」で起動してください。起動できたら、自動で確かめ直します。"
                            : "既存のComfyUIを起動し、上の接続先が正しいか確かめてから「もう一度確認する」を押してください。"}
                        </div>
                      </div>
                    )}

                    {requirements && (
                      <div className="vs-capabilities">
                        {requirements.capabilities.map((cap) => {
                          const lacking = cap.models.filter((m) => !m.present);
                          return (
                            <details key={cap.id} className={`vs-substep ${cap.ready ? "done" : ""}`} open={!cap.ready}>
                              <summary>
                                <span className="vs-substep-index">{cap.ready ? "✓" : "!"}</span>
                                <strong>{cap.display_name}</strong>
                                <StatusPill tone={cap.ready ? "ok" : cap.optional ? "idle" : "warn"}>
                                  {cap.ready ? "OK" : cap.optional ? "任意（未導入）" : "要対応"}
                                </StatusPill>
                              </summary>
                              <div className="vs-substep-body">
                                {cap.ready && (
                                  <div className="vs-ok">
                                    {cap.tools.length > 0 && cap.models.length === 0 && cap.missing_nodes.length === 0
                                      ? "必要なものは揃っています。"
                                      : "必要なノードとモデルは揃っています。"}
                                  </div>
                                )}
                                {!cap.ready && cap.optional && (
                                  <div className="vs-muted">
                                    この機能を使わないなら、揃っていなくても構いません（セットアップは完了できます）。
                                  </div>
                                )}
                                {cap.missing_nodes.length > 0 && (
                                  <div className="vs-lack">
                                    <strong>不足ノード（{cap.missing_nodes.length}件）</strong>
                                    <div className="vs-muted">{cap.missing_nodes.join(", ")}</div>
                                    {/* 直し方を書くだけにせず、その場で直せるようにする。
                                        共有フォルダの本体はここから入れ替えられる。 */}
                                    {canProvision ? (
                                      <>
                                        <div className="vs-muted">
                                          ComfyUI本体が古いか、必要な機能が入っていません。
                                          {comfyui?.installed_version
                                            && `（いま ${comfyui.installed_version} ／ 必要 ${comfyui.target_version}）`}
                                        </div>
                                        <div className="vs-row" style={{ marginTop: 8 }}>
                                          <button className="vs-btn" disabled={provision.running} onClick={startProvision}>
                                            {provision.running
                                              ? "準備中..."
                                              : comfyOutdated
                                                ? `最新版（${comfyui?.target_version}）に入れ替える`
                                                : "ComfyUI環境を準備し直す"}
                                          </button>
                                          <span className="vs-muted">
                                            アプリが用意したComfyUIなので、ここから入れ替えられます。
                                            作った作品も、取得済みのモデルもそのまま残ります。
                                          </span>
                                        </div>
                                        {provision.running && (
                                          <Progress
                                            value={provision.progress}
                                            label={provision.message}
                                            runner={provision}
                                            setError={setError}
                                          />
                                        )}
                                      </>
                                    ) : comfySource === "existing" ? (
                                      <div className="vs-muted">
                                        ComfyUI本体が古いか、必要な機能が入っていません。
                                        {comfyui?.installed_version
                                          && `（いま ${comfyui.installed_version} ／ 必要 ${comfyui.target_version}）`}
                                        指定した既存のComfyUIはアプリが書き換えないため、更新はご自身で行ってください。
                                      </div>
                                    ) : (
                                      <div className="vs-muted">
                                        ComfyUI本体が古いか、必要な機能が入っていません。
                                        上の「ComfyUI本体の準備」から用意すると解消できます。
                                      </div>
                                    )}
                                  </div>
                                )}
                                {/* ComfyUIの外で動くものも、同じ場所で揃えられるようにする。 */}
                                {cap.tools.filter((tool) => !tool.present).map((tool) => (
                                  <div key={tool.id} className="vs-lack">
                                    <strong>足りないもの</strong>
                                    <div className="vs-model-list">
                                      <div className="vs-model-row">
                                        <span>{tool.display_name}</span>
                                        <b>未導入</b>
                                      </div>
                                    </div>
                                    {tool.hint && <div className="vs-muted">{tool.hint}</div>}
                                    {tool.can_install && (
                                      <>
                                        <div className="vs-row" style={{ marginTop: 8 }}>
                                          <button
                                            className="vs-btn"
                                            disabled={tooling.running}
                                            onClick={() => startToolProvision(tool.id)}
                                          >
                                            {tooling.activeKey === tool.id ? "用意しています..." : "用意する"}
                                          </button>
                                          <span className="vs-muted">
                                            約{formatBytes(tool.install_size_bytes)}を取得します。1回で済みます。
                                          </span>
                                        </div>
                                        {/* 外部の配布物を入れるので、何をどこから取るのかを押す前に出す。 */}
                                        {tool.source && (
                                          <div className="vs-muted" style={{ marginTop: 4 }}>
                                            取得元: {tool.source}
                                            {tool.license && `（${tool.license}）`}
                                          </div>
                                        )}
                                        {tooling.activeKey === tool.id && (
                                          <Progress
                                            value={tooling.progress}
                                            label={tooling.message}
                                            runner={tooling}
                                            setError={setError}
                                          />
                                        )}
                                        {toolError?.id === tool.id && (
                                          <div className="vs-error" style={{ marginTop: 8 }}>
                                            {toolError.message}
                                          </div>
                                        )}
                                      </>
                                    )}
                                  </div>
                                ))}
                                {lacking.length > 0 && (
                                  <div className="vs-lack">
                                    <strong>不足モデル（{lacking.length}件）</strong>
                                    <div className="vs-model-list">
                                      {lacking.map((m) => (
                                        <div key={m.file} className="vs-model-row">
                                          <span>{m.display_name}</span>
                                          <b>{m.reason === "loader_missing" ? "ローダー未導入で確認不可" : "未導入"}</b>
                                        </div>
                                      ))}
                                    </div>
                                    <div className="vs-muted">
                                      {isManaged
                                        ? "上の「モデルの取得」で入手できます。"
                                        : "既存ComfyUIのmodelsフォルダへ配置してください。"}
                                    </div>
                                  </div>
                                )}
                              </div>
                            </details>
                          );
                        })}
                      </div>
                    )}
                    {/* 自動では気づけない変化（手でモデルを置いた、ComfyUIを起動し直した、など）の
                        あとに使う。一度確かめたあとだけ出す。 */}
                    {(requirements || checkError) && (
                      <div className="vs-row" style={{ marginTop: 10 }}>
                        <button
                          className="vs-btn ghost"
                          disabled={checking}
                          onClick={() => void checkRequirements()}
                        >
                          {checking ? "確認中..." : "もう一度確認する"}
                        </button>
                        <span className="vs-muted">
                          {isManaged
                            ? "モデルやノードを手で足したあとなどに押すと、結果を新しくします。"
                            : "モデルを置いた、ComfyUIを起動し直した、などのあとに押すと、結果を新しくします。"}
                        </span>
                      </div>
                    )}
                  </SubStep>
                </>
              ) : (
                <>
                <SubStep
                  index={1}
                  title="既存ComfyUIへモデルとノードを導入"
                  done={false}
                  todoLabel="手動"
                >
                  <p className="vs-muted">
                    「既存のComfyUIをそのまま使う」モードでは、アプリは既存のComfyUIの設定・ノード・モデルを変更しません
                    （生成した画像・動画は、そのComfyUIの出力フォルダにも保存されます。アプリはそこを片付けません）。
                    不足しているモデルは既存ComfyUIのmodelsフォルダへ配置し、不足ノードはComfyUIの更新や
                    カスタムノードの導入で解消してください。アプリにモデルを管理させたい場合は、上の
                    モードを「専用ComfyUIを起動し、既存モデルを参照する」に変更すると自動取得できます。
                  </p>
                </SubStep>

                  <SubStep
                    index={2}
                    title="ノードとモデルの確認"
                    done={requirementsReady}
                    doneLabel={optionalPending ? "必須は揃っています" : "すべて揃っています"}
                    todoLabel={
                      checking
                        ? "確認しています…"
                        : requirements
                          ? "不足があります"
                          : checkError
                            ? "接続できません"
                            : "未確認"
                    }
                    keepOpen={optionalPending}
                  >
                    <p className="vs-muted">
                      接続先のComfyUI（{config?.resolved_base_url || "未設定"}）に、使う機能のノードとモデルが
                      揃っているかを確かめます。
                    </p>
                    {/* 確かめる前は、いつ始まるのかを書く（押すものは無い）。 */}
                    {!requirements && !checkError && !checking && isManaged && (
                      <div className="vs-muted">専用ComfyUIを起動すると、自動で確かめます。</div>
                    )}
                    {checking && <div className="vs-muted">確かめています…</div>}
                    {checkError && (
                      <div className="vs-note warn">
                        <strong>ComfyUIへ接続できませんでした。</strong>
                        <div>{checkError}</div>
                        <div>
                          {isManaged
                            ? "上の「専用ComfyUIの起動」で起動してください。起動できたら、自動で確かめ直します。"
                            : "既存のComfyUIを起動し、上の接続先が正しいか確かめてから「もう一度確認する」を押してください。"}
                        </div>
                      </div>
                    )}

                    {requirements && (
                      <div className="vs-capabilities">
                        {requirements.capabilities.map((cap) => {
                          const lacking = cap.models.filter((m) => !m.present);
                          return (
                            <details key={cap.id} className={`vs-substep ${cap.ready ? "done" : ""}`} open={!cap.ready}>
                              <summary>
                                <span className="vs-substep-index">{cap.ready ? "✓" : "!"}</span>
                                <strong>{cap.display_name}</strong>
                                <StatusPill tone={cap.ready ? "ok" : cap.optional ? "idle" : "warn"}>
                                  {cap.ready ? "OK" : cap.optional ? "任意（未導入）" : "要対応"}
                                </StatusPill>
                              </summary>
                              <div className="vs-substep-body">
                                {cap.ready && (
                                  <div className="vs-ok">
                                    {cap.tools.length > 0 && cap.models.length === 0 && cap.missing_nodes.length === 0
                                      ? "必要なものは揃っています。"
                                      : "必要なノードとモデルは揃っています。"}
                                  </div>
                                )}
                                {!cap.ready && cap.optional && (
                                  <div className="vs-muted">
                                    この機能を使わないなら、揃っていなくても構いません（セットアップは完了できます）。
                                  </div>
                                )}
                                {cap.missing_nodes.length > 0 && (
                                  <div className="vs-lack">
                                    <strong>不足ノード（{cap.missing_nodes.length}件）</strong>
                                    <div className="vs-muted">{cap.missing_nodes.join(", ")}</div>
                                    {/* 直し方を書くだけにせず、その場で直せるようにする。
                                        共有フォルダの本体はここから入れ替えられる。 */}
                                    {canProvision ? (
                                      <>
                                        <div className="vs-muted">
                                          ComfyUI本体が古いか、必要な機能が入っていません。
                                          {comfyui?.installed_version
                                            && `（いま ${comfyui.installed_version} ／ 必要 ${comfyui.target_version}）`}
                                        </div>
                                        <div className="vs-row" style={{ marginTop: 8 }}>
                                          <button className="vs-btn" disabled={provision.running} onClick={startProvision}>
                                            {provision.running
                                              ? "準備中..."
                                              : comfyOutdated
                                                ? `最新版（${comfyui?.target_version}）に入れ替える`
                                                : "ComfyUI環境を準備し直す"}
                                          </button>
                                          <span className="vs-muted">
                                            アプリが用意したComfyUIなので、ここから入れ替えられます。
                                            作った作品も、取得済みのモデルもそのまま残ります。
                                          </span>
                                        </div>
                                        {provision.running && (
                                          <Progress
                                            value={provision.progress}
                                            label={provision.message}
                                            runner={provision}
                                            setError={setError}
                                          />
                                        )}
                                      </>
                                    ) : comfySource === "existing" ? (
                                      <div className="vs-muted">
                                        ComfyUI本体が古いか、必要な機能が入っていません。
                                        {comfyui?.installed_version
                                          && `（いま ${comfyui.installed_version} ／ 必要 ${comfyui.target_version}）`}
                                        指定した既存のComfyUIはアプリが書き換えないため、更新はご自身で行ってください。
                                      </div>
                                    ) : (
                                      <div className="vs-muted">
                                        ComfyUI本体が古いか、必要な機能が入っていません。
                                        上の「ComfyUI本体の準備」から用意すると解消できます。
                                      </div>
                                    )}
                                  </div>
                                )}
                                {/* ComfyUIの外で動くものも、同じ場所で揃えられるようにする。 */}
                                {cap.tools.filter((tool) => !tool.present).map((tool) => (
                                  <div key={tool.id} className="vs-lack">
                                    <strong>足りないもの</strong>
                                    <div className="vs-model-list">
                                      <div className="vs-model-row">
                                        <span>{tool.display_name}</span>
                                        <b>未導入</b>
                                      </div>
                                    </div>
                                    {tool.hint && <div className="vs-muted">{tool.hint}</div>}
                                    {tool.can_install && (
                                      <>
                                        <div className="vs-row" style={{ marginTop: 8 }}>
                                          <button
                                            className="vs-btn"
                                            disabled={tooling.running}
                                            onClick={() => startToolProvision(tool.id)}
                                          >
                                            {tooling.activeKey === tool.id ? "用意しています..." : "用意する"}
                                          </button>
                                          <span className="vs-muted">
                                            約{formatBytes(tool.install_size_bytes)}を取得します。1回で済みます。
                                          </span>
                                        </div>
                                        {/* 外部の配布物を入れるので、何をどこから取るのかを押す前に出す。 */}
                                        {tool.source && (
                                          <div className="vs-muted" style={{ marginTop: 4 }}>
                                            取得元: {tool.source}
                                            {tool.license && `（${tool.license}）`}
                                          </div>
                                        )}
                                        {tooling.activeKey === tool.id && (
                                          <Progress
                                            value={tooling.progress}
                                            label={tooling.message}
                                            runner={tooling}
                                            setError={setError}
                                          />
                                        )}
                                        {toolError?.id === tool.id && (
                                          <div className="vs-error" style={{ marginTop: 8 }}>
                                            {toolError.message}
                                          </div>
                                        )}
                                      </>
                                    )}
                                  </div>
                                ))}
                                {lacking.length > 0 && (
                                  <div className="vs-lack">
                                    <strong>不足モデル（{lacking.length}件）</strong>
                                    <div className="vs-model-list">
                                      {lacking.map((m) => (
                                        <div key={m.file} className="vs-model-row">
                                          <span>{m.display_name}</span>
                                          <b>{m.reason === "loader_missing" ? "ローダー未導入で確認不可" : "未導入"}</b>
                                        </div>
                                      ))}
                                    </div>
                                    <div className="vs-muted">
                                      {isManaged
                                        ? "上の「モデルの取得」で入手できます。"
                                        : "既存ComfyUIのmodelsフォルダへ配置してください。"}
                                    </div>
                                  </div>
                                )}
                              </div>
                            </details>
                          );
                        })}
                      </div>
                    )}
                    {/* 自動では気づけない変化（手でモデルを置いた、ComfyUIを起動し直した、など）の
                        あとに使う。一度確かめたあとだけ出す。 */}
                    {(requirements || checkError) && (
                      <div className="vs-row" style={{ marginTop: 10 }}>
                        <button
                          className="vs-btn ghost"
                          disabled={checking}
                          onClick={() => void checkRequirements()}
                        >
                          {checking ? "確認中..." : "もう一度確認する"}
                        </button>
                        <span className="vs-muted">
                          {isManaged
                            ? "モデルやノードを手で足したあとなどに押すと、結果を新しくします。"
                            : "モデルを置いた、ComfyUIを起動し直した、などのあとに押すと、結果を新しくします。"}
                        </span>
                      </div>
                    )}
                  </SubStep>
                </>
              )}
            </div>
          </div>
        )}
      </div>

      {wizard && (
        <div className="vs-card">
          <div className="vs-step-head">
            <h2>
              <span className="vs-step-no">STEP {doneStep}</span>
              セットアップの完了
            </h2>
            <StatusPill tone={canComplete ? "ok" : "idle"}>
              {canComplete ? "完了できます" : "まだ完了できません"}
            </StatusPill>
          </div>
          <p className="vs-muted">
            完了するとこの画面は表示されなくなり、制作画面から使えます。設定はあとから「設定を確認・変更」で見直せます。
          </p>
          <ul className="vs-check">
            <li className={config?.configured ? "done" : ""}>
              {config?.configured ? "✓" : "—"} 接続設定を保存する
            </li>
            <li className={requirementsReady ? "done" : ""}>
              {requirementsReady ? "✓" : "—"} 「ノードとモデルの確認」で必要なものが揃っている
            </li>
          </ul>
          <div className="vs-row" style={{ marginTop: 12 }}>
            <button
              className="vs-btn"
              disabled={!canComplete}
              onClick={() => void guarded(async () => {
                await api.completeSetup();
                await onCompleted?.();
              })}
            >
              セットアップを完了して制作を始める
            </button>
          </div>
          {!canComplete && (
            <p className="vs-muted" style={{ marginTop: 8 }}>
              必要環境が揃うまで完了できません（上のチェックが両方✓になると押せます）。
            </p>
          )}
          {config?.configured && !requirementsReady && (
            <button
              className="vs-btn link"
              onClick={() => void guarded(async () => {
                await api.completeSetup();
                await onCompleted?.();
              })}
            >
              確認せずに完了する（上級者向け・生成時に失敗することがあります）
            </button>
          )}
        </div>
      )}
    </div>
  );
}

// ============================ 素材（背景・小物） ============================

function AssetsTab({
  projectId,
  kind,
  title,
  propsRevision = 0,
  onChanged,
}: {
  projectId: string;
  kind: AssetKind;
  title: string;
  /** 背景だけ: 小物の一覧が変わるたびに増える。別タブで小物を足したときに読み直す。 */
  propsRevision?: number;
  /** 一覧を読み直したとき（作成・生成・削除のあと）に知らせる。 */
  onChanged?: () => void;
}) {
  const [assets, setAssets] = useState<api.Asset[]>([]);
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [prompts, setPrompts] = useState<Record<string, string>>({});
  // 背景だけ: バリエーションを作る欄（どの背景から作るか。null なら閉じている）。
  const [variationSource, setVariationSource] = useState<api.Asset | null>(null);
  const [variationKind, setVariationKind] = useState<api.BackgroundVariationKind>("prop");
  const [variationLabel, setVariationLabel] = useState("");
  const [variationInstruction, setVariationInstruction] = useState("");
  const [variationPropIds, setVariationPropIds] = useState<string[]>([]);
  const [propAssets, setPropAssets] = useState<api.Asset[]>([]);
  // 背景だけ: 開いているグループ（元の背景のID）。一覧には元の背景だけを並べ、
  // バリエーションはグループの画面で扱う。一覧に全部を広げると、グループが増えるほど
  // 縦に伸びて目当ての背景を探しにくい。
  const [openGroupId, setOpenGroupId] = useState<string | null>(null);
  // 画面を移ったら先頭から見せる。
  const screenRef = useRef<HTMLDivElement>(null);
  const gen = useJobRunner();
  const deletion = useDeletion(setError);
  const lightbox = useLightbox();

  // 例示は、どんな作品でも当てはまるものにする。
  // 背景と小物で同じ画面を使うので、その種類に合った例を出す。
  const isProp = kind === "prop-assets";
  const nameExample = isProp ? "例: 古い懐中時計" : "例: 夜の街角";
  const promptHint = isProp
    ? "形・素材・色・大きさが分かるように書きます"
    : "どんな場所か。時間帯や天気まで書くと安定します";

  const reload = useCallback(async () => {
    try {
      setAssets(await api.listAssets(projectId, kind));
      onChanged?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [projectId, kind, onChanged]);

  useEffect(() => {
    void reload();
  }, [reload]);

  // 背景へ置く小物。背景の画面でだけ使う。「小物」のタブで変わったら読み直す。
  useEffect(() => {
    if (isProp) return;
    let alive = true;
    void (async () => {
      try {
        const found = await api.listAssets(projectId, "prop-assets");
        if (alive) setPropAssets(found);
      } catch {
        // 読めなくても、時間帯を変えるバリエーションは作れる。
        if (alive) setPropAssets([]);
      }
    })();
    return () => {
      alive = false;
    };
  }, [projectId, isProp, propsRevision]);

  async function guarded(fn: () => Promise<void>) {
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  const screenShown = useRef(false);
  useEffect(() => {
    // 最初に描いたときは送らない（タブを開いただけで画面が動かないように）。
    if (!screenShown.current) {
      screenShown.current = true;
      return;
    }
    screenRef.current?.scrollIntoView({ block: "start" });
  }, [openGroupId]);

  /** グループの画面を閉じて一覧へ戻る。作りかけのバリエーションの欄も閉じる。 */
  function closeGroup() {
    setVariationSource(null);
    setOpenGroupId(null);
  }

  function openVariation(asset: api.Asset) {
    setVariationSource(asset);
    setVariationKind("prop");
    setVariationLabel("");
    setVariationInstruction("");
    setVariationPropIds([]);
  }

  /** 背景をグループにまとめる。元の背景と、そこから作ったバリエーション。 */
  function assetGroups(): Array<{ base: api.Asset; variations: api.Asset[] }> {
    if (isProp) return assets.map((asset) => ({ base: asset, variations: [] }));
    const groups = assets
      .filter((asset) => !asset.base_asset_id)
      .map((base) => ({
        base,
        variations: assets
          .filter((asset) => asset.base_asset_id === base.id)
          // 作った順に並べる（名前や画像を変えても並びが入れ替わらないように）。
          .sort((left, right) => left.created_at.localeCompare(right.created_at)),
      }));
    // 元が見つからない派生も落とさない（それ自体を1つのグループとして出す）。
    const grouped = new Set(groups.flatMap((group) => [group.base, ...group.variations]));
    for (const asset of assets) {
      if (!grouped.has(asset)) groups.push({ base: asset, variations: [] });
    }
    return groups;
  }

  /** 画像のある背景・小物を、見えている並びのまま拡大する（前後へ送れる）。
   *  グループの画面ではそのグループの中だけ、小物は一覧全体を送る。 */
  function openAssetGallery(assetId: string) {
    const groups = assetGroups();
    const opened = groups.find((group) => group.base.id === openGroupId);
    const shown = (opened ? [opened] : groups)
      .flatMap((group) => [group.base, ...group.variations])
      .filter((asset) => asset.has_image);
    lightbox.openGallery(
      shown.map((asset) => ({
        src: api.assetImageSrc(kind, asset),
        caption: asset.name,
      })),
      shown.findIndex((asset) => asset.id === assetId),
    );
  }

  /** 背景・小物1件のカード。グループの画面でも小物の一覧でも同じものを使う。 */
  function renderAssetCard(asset: api.Asset) {
    // 欄に手を入れるまでは、今の画像を作ったときの文を出す（そのまま作り直したり、
    // 少し直して作り直したりできる）。
    const prompt = prompts[asset.id] ?? asset.input_prompt;
    // 生成するまでは、書き換えた文を捨てて今の画像を作ったときの文へ戻せる。
    const promptEdited = prompts[asset.id] !== undefined && prompts[asset.id] !== asset.input_prompt;
    return (
            <div key={asset.id} className="vs-card">
              <EditableName
                name={asset.name}
                onSave={async (newName) => {
                  await api.updateAsset(kind, asset.id, { name: newName });
                  await reload();
                }}
              />
              {asset.has_image ? (
                // 画像は長期キャッシュで配られるので、作り直したら別のURLにする
                // （idだけのURLでは、最初に作った1枚が出続ける）。
                <img
                  className="vs-thumb vs-zoomable"
                  src={api.assetImageSrc(kind, asset)}
                  alt={asset.name}
                  style={{ marginTop: 8 }}
                  onClick={() => openAssetGallery(asset.id)}
                />
              ) : (
                <div className="vs-thumb" style={{ display: "grid", placeItems: "center", marginTop: 8 }}>
                  <span className="vs-muted">画像なし</span>
                </div>
              )}
              <div className="vs-field" style={{ marginTop: 10 }}>
                <div className="vs-field-head">
                  <label htmlFor={`asset-prompt-${asset.id}`}>生成プロンプト</label>
                  <button
                    type="button"
                    className="vs-btn ghost vs-field-revert"
                    disabled={!promptEdited}
                    onClick={() =>
                      setPrompts((current) => {
                        const next = { ...current };
                        delete next[asset.id];
                        return next;
                      })
                    }
                  >
                    元に戻す
                  </button>
                </div>
                <textarea
                  id={`asset-prompt-${asset.id}`}
                  className="vs-textarea"
                  placeholder={promptHint}
                  value={prompt}
                  onChange={(e) => setPrompts((p) => ({ ...p, [asset.id]: e.target.value }))}
                />
              </div>
              <div className="vs-row">
                <button
                  className="vs-btn"
                  disabled={gen.running || !prompt.trim()}
                  onClick={() =>
                    void gen.run(
                      async () => {
                        const r = await api.generateAsset(kind, asset.id, { prompt: prompt.trim() });
                        return { job_id: r.job_id };
                      },
                      reload,
                      setError,
                      asset.id,
                    )
                  }
                >
                  {gen.activeKey === asset.id ? "生成中..." : "生成"}
                </button>
                <label className="vs-btn ghost">
                  画像を選択
                  <input
                    type="file"
                    accept="image/*"
                    hidden
                    onChange={(e) => {
                      const file = e.target.files?.[0];
                      if (file) void guarded(async () => {
                        await api.uploadAssetImage(kind, asset.id, file);
                        await reload();
                      });
                    }}
                  />
                </label>
                {!isProp && (
                  <button
                    className="vs-btn ghost"
                    disabled={gen.running || !asset.has_image}
                    onClick={() => openVariation(asset)}
                    title={
                      asset.has_image
                        ? "この背景に小物を置いた／時間帯を変えた背景を作ります"
                        : "先に画像を作るか選んでください"
                    }
                  >
                    ここから背景を作る
                  </button>
                )}
                <button
                  className="vs-btn danger"
                  onClick={() => void deletion.ask(async () => {
                    const preview = await api.getAssetDeletionPreview(kind, asset.id);
                    const followers = assets.filter((item) => item.base_asset_id === asset.id).length;
                    return {
                      title: `${title}「${preview.name}」を削除しますか？`,
                      lead: `${title}「${preview.name}」と、その画像を削除します。元には戻せません。`,
                      items: stillImageUsageItems(
                        preview.usage,
                        kind === "prop-assets" ? "この小物が外れます" : "この背景が外れます",
                      ),
                      note: followers
                        ? `この背景から作ったバリエーション${followers}件は消えずに残り、それぞれ1つの背景として一覧に並びます。`
                        : undefined,
                      perform: async () => {
                        await api.deleteAsset(kind, asset.id);
                        await reload();
                      },
                    };
                  })}
                >
                  削除
                </button>
              </div>
              {/* 進捗と中止は、その処理を始めたカードの中に出す。 */}
              {gen.activeKey === asset.id && (
                <Progress value={gen.progress} label={gen.message} runner={gen} setError={setError} />
              )}
              {gen.running && gen.activeKey !== asset.id && (
                <div className="vs-muted vs-block-reason">
                  他の生成が実行中です（GPUは1件ずつ使います）。終わるまでお待ちください。
                </div>
              )}
            </div>
    );
  }

  const groups = assetGroups();
  const openGroup = isProp ? null : groups.find((group) => group.base.id === openGroupId) ?? null;

  // 背景のグループの画面。元の背景と、そこから作ったバリエーションをまとめて扱う。
  if (openGroup) {
    const { base, variations } = openGroup;
    return (
      <div ref={screenRef} className="vs-asset-screen">
        {error && <div className="vs-error">{error}</div>}
        <div className="vs-card">
          <div className="vs-screen-head">
            <button className="vs-btn ghost" onClick={closeGroup}>
              ← 背景の一覧へ
            </button>
            {/* 名前の変更は各カードで行う（見出しと二重に置かない）。 */}
            <h2>{base.name}</h2>
            <span className="vs-pill">バリエーション{variations.length}枚</span>
          </div>
          <p className="vs-muted">
            元の背景から「ここから背景を作る」で、小物を置いた背景や時間帯・天気を変えた背景
            （バリエーション）を作れます。静止画では、このグループのどれか1枚を選びます。
            元の背景の名前を変えると、バリエーションの名前も合わせて変わります。
          </p>
          {variationSource && (
            <div className="vs-card">
              <h2>「{variationSource.name}」から背景を作る</h2>
              <p className="vs-muted">
                場所・構図・カメラの位置は元のまま、小物を置くか、時間帯・天気だけを変えます。
                出来上がりは同じグループの背景として並び、静止画ではどれか1枚を選びます。
              </p>
              <div className="vs-field">
                <label>何を変えるか</label>
                <div className="vs-row">
                  {(["prop", "time"] as const).map((value) => (
                    <label className="vs-choice" key={value}>
                      <input
                        type="radio"
                        name="background-variation-kind"
                        checked={variationKind === value}
                        onChange={() => {
                          setVariationKind(value);
                          if (value === "time") setVariationPropIds([]);
                        }}
                      />
                      <span>{value === "prop" ? "小物を置く" : "時間帯・天気を変える"}</span>
                    </label>
                  ))}
                </div>
                {variationKind === "time" && (
                  <span className="vs-muted">
                    時間帯を変えるときは、元の背景1枚だけを渡します（小物を一緒に渡すと
                    時間帯が変わりません）。
                  </span>
                )}
              </div>
              {variationKind === "prop" && (
                <div className="vs-field">
                  <label>置く小物（2つまで）</label>
                  <div className="vs-row">
                    {propAssets.length === 0 && (
                      <span className="vs-muted">
                        まだ小物がありません。「小物」タブで登録すると、ここで選べます。
                      </span>
                    )}
                    {propAssets.map((prop) => {
                      const picked = variationPropIds.includes(prop.id);
                      const full = !picked && variationPropIds.length >= 2;
                      return (
                        <label
                          key={prop.id}
                          className="vs-pill"
                          style={{ cursor: full ? "not-allowed" : "pointer", opacity: full ? 0.5 : undefined }}
                          title={full ? "小物は2つまでです" : undefined}
                        >
                          <input
                            type="checkbox"
                            checked={picked}
                            disabled={full}
                            onChange={() =>
                              setVariationPropIds((current) =>
                                current.includes(prop.id)
                                  ? current.filter((value) => value !== prop.id)
                                  : [...current, prop.id],
                              )
                            }
                          /> {prop.name}
                        </label>
                      );
                    })}
                  </div>
                </div>
              )}
              <div className="vs-field">
                <label>{variationKind === "time" ? "どう変えるか" : "どこへ置くか（任意）"}</label>
                <input
                  className="vs-input"
                  value={variationInstruction}
                  placeholder={variationKind === "time" ? "例: 夕方にする" : "例: 手前の机の上に置く"}
                  onChange={(e) => setVariationInstruction(e.target.value)}
                />
              </div>
              <div className="vs-field">
                <label>元との違い（任意）</label>
                <input
                  className="vs-input"
                  style={{ maxWidth: 280 }}
                  value={variationLabel}
                  placeholder={variationKind === "time" ? "例: 夕方" : "例: 机に本"}
                  onChange={(e) => setVariationLabel(e.target.value)}
                />
                <span className="vs-muted">
                  名前の括弧書きになり、静止画で背景を選ぶときに見分けられます。空なら指示の先頭を使います。
                  名前は「{(openGroup?.base ?? variationSource).name}（{(variationLabel.trim() || variationInstruction.trim().slice(0, 30)) || "…"}）」になります。
                </span>
              </div>
              <div className="vs-row">
                <button
                  className="vs-btn"
                  disabled={
                    gen.running
                    || (variationKind === "time"
                      ? !variationInstruction.trim()
                      : variationPropIds.length === 0)
                  }
                  onClick={() =>
                    void gen.run(
                      async () => {
                        const result = await api.createBackgroundVariation(variationSource.id, {
                          kind: variationKind,
                          instruction: variationInstruction.trim(),
                          variant_label: variationLabel.trim(),
                          prop_asset_ids: variationPropIds,
                        });
                        setVariationSource(null);
                        return { job_id: result.job_id };
                      },
                      reload,
                      setError,
                      variationSource.id,
                    )
                  }
                >
                  背景を作る
                </button>
                <button className="vs-btn ghost" disabled={gen.running} onClick={() => setVariationSource(null)}>
                  やめる
                </button>
                <span className="vs-muted">
                  指示の反映を優先して作るので、1枚あたりの時間は最初の背景を作るときより
                  長くなります。
                </span>
              </div>
            </div>
          )}

          {/* 元の背景とバリエーションは横に並べて見比べる。元は先頭に置く。 */}
          <div className="vs-grid">
            {renderAssetCard(base)}
            {variations.map((variation) => renderAssetCard(variation))}
          </div>
          {variations.length === 0 && (
            <p className="vs-muted">バリエーションはまだありません。元の背景の「ここから背景を作る」で作れます。</p>
          )}
          <div className="vs-screen-footer">
            <button className="vs-btn" onClick={closeGroup}>
              閉じる
            </button>
          </div>
        </div>
        {deletion.dialog}
        {lightbox.node}
      </div>
    );
  }

  return (
    <div ref={screenRef} className="vs-asset-screen">
      {error && <div className="vs-error">{error}</div>}
      <div className="vs-card">
        <h2>{title}を追加</h2>
        <div className="vs-row">
          <input className="vs-input" style={{ maxWidth: 280 }} placeholder={`名前（${nameExample}）`} value={name} onChange={(e) => setName(e.target.value)} />
          <button
            className="vs-btn"
            disabled={!name.trim()}
            onClick={() =>
              void guarded(async () => {
                const created = await api.createAsset(projectId, kind, { name: name.trim() });
                setName("");
                await reload();
                // 背景は作ったらそのまま、その背景の画面で画像を作るところから始める。
                if (!isProp) setOpenGroupId(created.id);
              })
            }
          >
            追加
          </button>
        </div>
      </div>

      {isProp ? (
        <div className="vs-grid">{groups.map(({ base }) => renderAssetCard(base))}</div>
      ) : (
        <>
          {groups.length > 0 && (
            <p className="vs-muted vs-grid-lead">
              背景を選ぶと、その背景の画面で画像を作ったり、小物を置いた／時間帯を変えた
              バリエーションを足したりできます。
            </p>
          )}
          <div className="vs-grid">
            {groups.map(({ base, variations }) => {
              const members = [base, ...variations];
              const working = gen.activeKey !== null && members.some((asset) => asset.id === gen.activeKey);
              return (
                <button
                  key={base.id}
                  type="button"
                  className="vs-card vs-character-card"
                  onClick={() => setOpenGroupId(base.id)}
                >
                  <strong>{base.name}</strong>
                  {base.has_image ? (
                    <img
                      className="vs-thumb"
                      src={api.assetImageSrc(kind, base)}
                      alt={base.name}
                    />
                  ) : (
                    <div className="vs-thumb" style={{ display: "grid", placeItems: "center" }}>
                      <span className="vs-muted">画像なし</span>
                    </div>
                  )}
                  <span className="vs-row">
                    <span className={`vs-status ${variations.length ? "ok" : "idle"}`}>
                      バリエーション{variations.length}枚
                    </span>
                    {working && <span className="vs-status warn">生成中</span>}
                  </span>
                  <span className="vs-character-card-open">開く →</span>
                </button>
              );
            })}
          </div>
        </>
      )}
      {deletion.dialog}
      {lightbox.node}
    </div>
  );
}

// ============================ 登場人物 ============================

/** 一覧に出す代表写真を選び直し、一覧に出す範囲を決める。

    立ち絵には触れないので、何度変えても生成結果は変わらない。一覧には既定で
    写真の全体を出し、範囲を決めたときだけその範囲を出す。 */
function PortraitPicker({
  character,
  onChanged,
}: {
  character: api.Character;
  onChanged: () => Promise<void> | void;
}) {
  const [options, setOptions] = useState<api.PortraitOption[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(false);
  const [cropping, setCropping] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const portraitVersion = encodeURIComponent(character.portrait_version ?? character.id);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      // 取り直している間も前の候補は出したままにする（開くたびに空へ戻さない）。
      setOptions(await api.listPortraitOptions(character.id));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [character.id]);

  // 開いている間は、この人物の状態が変わるたびに候補を取り直す。
  // 選べる写真は作業中に増える（人物候補や立ち絵を作る）ため、一度読んだきりに
  // すると、増えたはずの候補がいつまでも出てこない。
  useEffect(() => {
    if (!open) return;
    void load();
  }, [open, load, character.updated_at, character.figure_count, character.portrait_version]);

  async function apply(action: () => Promise<unknown>) {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await action();
      await load();
      await onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <details
      className="vs-collapse"
      onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}
    >
      <summary>代表写真を変える</summary>
      <div className="vs-detail-body">
        <p className="vs-muted">
          一覧に出す写真だけを変えます。合成に使う立ち絵はそのままです。
        </p>
        {error && <div className="vs-note warn">{error}</div>}
        {/* 読み込み中と「1枚も無い」を区別する。どちらも無表示だと、
            出てこないのが待ちなのか無いのか分からない。 */}
        {loading && options === null && (
          <div className="vs-muted">選べる写真を読み込んでいます…</div>
        )}
        {!loading && options !== null && options.length === 0 && (
          <div className="vs-muted">選べる写真がまだありません。手持ちの写真を使ってください。</div>
        )}
        {options !== null && options.length > 0 && (
          <div className="vs-portrait-options">
            {options.map((option) => (
              <button
                key={option.id}
                type="button"
                className={`vs-portrait-option${option.selected ? " selected" : ""}`}
                disabled={busy}
                title={option.label}
                onClick={() => void apply(async () => {
                  await api.selectPortrait(character.id, option.id);
                  // 範囲は前の写真に合わせたもの。写真を変えたら決め直してもらう。
                  setCropping(false);
                })}
              >
                <img src={api.apiUrl(option.url)} alt={option.label} />
                <span>{option.label}</span>
              </button>
            ))}
          </div>
        )}
        <label className="vs-btn ghost" style={{ marginTop: 10 }}>
          手持ちの写真を使う
          <input
            type="file"
            accept="image/*"
            hidden
            disabled={busy}
            onChange={(e) => {
              const file = e.target.files?.[0];
              e.currentTarget.value = "";
              if (file) void apply(async () => {
                await api.uploadCharacterPortrait(character.id, file);
                setCropping(false);
              });
            }}
          />
        </label>

        <div className="vs-portrait-trim">
          {cropping ? (
            <>
              {!character.has_portrait_image && (
                <p className="vs-muted">
                  いま一覧に出ている立ち絵を代表写真にしてトリミングします。
                </p>
              )}
              <PortraitCropper
                src={`${api.characterPortraitSourceUrl(character.id)}?v=${portraitVersion}`}
                initial={character.portrait_crop ?? null}
                busy={busy}
                onSave={(crop) => void apply(async () => {
                  await api.setPortraitCrop(character.id, crop);
                  setCropping(false);
                })}
                onCancel={() => setCropping(false)}
              />
            </>
          ) : (
            <>
              {/* 選んだ写真が一覧でどう見えるかを、その場で出す（一覧と同じ枠・同じ画像）。 */}
              <span className="vs-muted">一覧での見え方</span>
              <img
                className={`vs-thumb face vs-portrait-preview${busy ? " busy" : ""}`}
                src={`${api.characterPortraitUrl(character.id)}?v=${portraitVersion}`}
                alt={`${character.name}の代表写真`}
              />
              <div className="vs-row">
                <button
                  type="button"
                  className="vs-btn ghost"
                  disabled={busy}
                  onClick={() => setCropping(true)}
                >
                  トリミングする
                </button>
                <button
                  type="button"
                  className="vs-btn ghost"
                  disabled={busy || !character.portrait_crop}
                  onClick={() => void apply(() => api.setPortraitCrop(character.id, null))}
                >
                  全体を表示
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </details>
  );
}

function CharactersTab({ projectId, propsRevision }: { projectId: string; propsRevision: number }) {
  const [characters, setCharacters] = useState<api.Character[]>([]);
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  // 開いている人物。開いている間はその人物の画面だけを出し、一覧は出さない。
  // 一覧の中で枠を広げると、人数が増えるほど縦に伸び、どこで作業が終わったのか分からない。
  const [openCharacterId, setOpenCharacterId] = useState<string | null>(null);
  // 一覧へ戻ったら一覧の先頭から見せる（人物の画面の下の方の位置のままにしない）。
  const listRef = useRef<HTMLDivElement>(null);
  const returnedToList = useRef(false);
  const deletion = useDeletion(setError);

  const reload = useCallback(async () => {
    try {
      setCharacters(await api.listCharacters(projectId));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [projectId]);

  useEffect(() => {
    void reload();
  }, [reload]);

  useEffect(() => {
    if (openCharacterId !== null) {
      returnedToList.current = true;
      return;
    }
    if (!returnedToList.current) return;
    returnedToList.current = false;
    listRef.current?.scrollIntoView({ block: "start" });
  }, [openCharacterId]);

  async function guarded(fn: () => Promise<void>) {
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  const openCharacter = characters.find((character) => character.id === openCharacterId) ?? null;

  if (openCharacter) {
    return (
      <div>
        {error && <div className="vs-error">{error}</div>}
        <CharacterStudio
          key={openCharacter.id}
          character={openCharacter}
          onChanged={reload}
          onClose={() => setOpenCharacterId(null)}
          propsRevision={propsRevision}
          overviewExtras={(
            <>
              {Boolean(openCharacter.figure_count) && (
                <PortraitPicker character={openCharacter} onChanged={reload} />
              )}
              <div className="vs-row" style={{ marginTop: 14 }}>
                <button
                  className="vs-btn danger"
                  onClick={() => void deletion.ask(async () => {
                    const preview = await api.getCharacterDeletionPreview(openCharacter.id);
                    const items = stillImageUsageItems(preview.usage, "この人物が外れます");
                    if (preview.candidate_count) items.push(`人物候補 ${preview.candidate_count}枚`);
                    if (preview.figure_count) items.push(`立ち絵 ${preview.figure_count}枚`);
                    return {
                      title: `「${preview.name}」を削除しますか？`,
                      lead: `登場人物「${preview.name}」と、この人物のために作ったものをすべて削除します。元には戻せません。`,
                      items,
                      perform: async () => {
                        await api.deleteCharacter(openCharacter.id);
                        setOpenCharacterId(null);
                        await reload();
                      },
                    };
                  })}
                >
                  この人物を削除
                </button>
              </div>
            </>
          )}
        />
        {deletion.dialog}
      </div>
    );
  }

  return (
    <div ref={listRef} className="vs-character-list">
      {error && <div className="vs-error">{error}</div>}
      <div className="vs-card">
        <h2>登場人物を追加</h2>
        <div className="vs-row" style={{ marginBottom: 12 }}>
          <input className="vs-input" style={{ maxWidth: 240 }} placeholder="名前" value={name} onChange={(e) => setName(e.target.value)} />
          <button
            className="vs-btn"
            disabled={!name.trim()}
            onClick={() => void guarded(async () => {
              const created = await api.createCharacter(projectId, { name: name.trim() });
              setName("");
              await reload();
              // 作ったらそのまま、その人物の画面でプロフィールから始める。
              setOpenCharacterId(created.id);
            })}
          >
            新規作成
          </button>
        </div>
      </div>

      {characters.length > 0 && (
        <p className="vs-muted vs-grid-lead">
          人物を選ぶと、その人物の画面で<strong>プロフィール → 人物候補 → 立ち絵</strong>の順に
          進めます。静止画の合成では、人物ごとに<strong>立ち絵</strong>を1枚使います。手持ちの
          全身画像があるなら、立ち絵の画面で登録するだけでも構いません。
        </p>
      )}
      <div className="vs-grid">
        {characters.map((character) => (
          <button
            key={character.id}
            type="button"
            className="vs-card vs-character-card"
            onClick={() => setOpenCharacterId(character.id)}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <strong>{character.name}</strong>
              {Boolean(character.figure_count) && (
                <span className="vs-pill model">立ち絵{character.figure_count}枚</span>
              )}
            </div>
            {character.figure_count ? (
              // 一覧は顔で見分ける。代表写真（顔が見える1枚）を出し、決めていなければ
              // 既定の立ち絵に落ちる。範囲を決めていればその範囲だけが返ってくる。
              <img
                className="vs-thumb face"
                src={`${api.characterPortraitUrl(character.id)}?v=${encodeURIComponent(character.portrait_version ?? character.id)}`}
                alt={character.name}
              />
            ) : (
              <div className="vs-thumb face" style={{ display: "grid", placeItems: "center" }}>
                <span className="vs-muted">立ち絵なし</span>
              </div>
            )}
            {/* この人物が静止画に出せる状態かどうかを、まず一言で出す。 */}
            <span className={`vs-status ${character.figure_count ? "ok" : "warn"}`}>
              {character.figure_count ? "静止画に出せます" : "立ち絵がまだありません"}
            </span>
            <span className="vs-character-card-open">開く →</span>
          </button>
        ))}
      </div>

      {deletion.dialog}
    </div>
  );
}

// ============================ 静止画 ============================

const EMPTY_STILL_IMAGE_CHARACTER_SPEC: api.StillImageCharacterSpec = {
  pose: "",
  expression: "",
  direction: "",
  placement: "",
  notes: "",
};

function StillImageCharacterPanel({
  character,
  info,
  figures,
  spec,
  onSpecChange,
  onChooseFigure,
}: {
  character: api.Character;
  info?: api.StillImageCharacter;
  /** この人物の立ち絵（出来上がっているものだけ）。 */
  figures: api.CharacterFigure[];
  /** 画面に出ている指定。保存は合成のときにまとめて行う。 */
  spec: api.StillImageCharacterSpec;
  onSpecChange: (spec: api.StillImageCharacterSpec) => void;
  /** 使う立ち絵を選び直す。空文字は「既定の立ち絵を使う」。 */
  onChooseFigure: (figureId: string) => Promise<void>;
}) {
  // 空文字は「既定の立ち絵を使う」。選び直すとすぐ info に反映されるので、ここでは持たない。
  const figureId = chosenFigureId(info);
  const [error, setError] = useState<string | null>(null);
  const lightbox = useLightbox();

  const chooseFigure = async (next: string) => {
    setError(null);
    try {
      await onChooseFigure(next);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const figureLabel = (figure: api.CharacterFigure, index: number) =>
    figure.label || `立ち絵${index + 1}`;

  return (
    <div className="vs-state-box">
      <div className="vs-state-title">{character.name} の指定</div>
      {error && <div className="vs-error">{error}</div>}
      {!info?.has_figure && (
        <div className="vs-warn vs-block-reason">
          この人物の立ち絵がありません。素材作成の「登場人物」タブで立ち絵を作ってください。
        </div>
      )}
      {info?.figure_url && (
        <div className="vs-state-preview">
          <img
            className="vs-thumb vs-state-thumb"
            src={api.apiUrl(info.figure_url)}
            alt={`${character.name} の立ち絵`}
            onClick={() => lightbox.open(api.apiUrl(info.figure_url!), `${character.name} の立ち絵`)}
          />
          <span className="vs-muted">
            {info.figure_is_default
              ? `既定の立ち絵を使います${info.figure_label ? `（${info.figure_label}）` : ""}`
              : `この静止画では「${info.figure_label || "選んだ立ち絵"}」を使います`}
          </span>
        </div>
      )}
      {figures.length > 1 && (
        <div className="vs-field">
          <label>使う立ち絵</label>
          <select
            className="vs-select"
            value={figureId}
            onChange={(e) => void chooseFigure(e.target.value)}
          >
            <option value="">既定の立ち絵を使う</option>
            {figures.map((figure, index) => (
              <option key={figure.id} value={figure.id}>
                {figureLabel(figure, index)}
              </option>
            ))}
          </select>
        </div>
      )}
      <div className="vs-state-grid">
        <div>
          <label>ポーズ</label>
          <input className="vs-input" value={spec.pose} onChange={(e) => onSpecChange({ ...spec, pose: e.target.value })} placeholder="例: 椅子に座って右手をあげる" />
        </div>
        <div>
          <label>表情</label>
          <input className="vs-input" value={spec.expression} onChange={(e) => onSpecChange({ ...spec, expression: e.target.value })} placeholder="例: 笑顔" />
        </div>
        <div>
          <label>向き</label>
          <input className="vs-input" value={spec.direction} onChange={(e) => onSpecChange({ ...spec, direction: e.target.value })} placeholder="例: 右向き" />
        </div>
        <div>
          <label>配置・位置</label>
          <input className="vs-input" value={spec.placement} onChange={(e) => onSpecChange({ ...spec, placement: e.target.value })} placeholder="例: 画面の右寄り" />
        </div>
      </div>
      <div className="vs-state-field">
        <label>補足</label>
        <input className="vs-input" value={spec.notes} onChange={(e) => onSpecChange({ ...spec, notes: e.target.value })} placeholder="上の項目に収まらない指定" />
      </div>
      <div className="vs-state-actions">
        <span className="vs-muted">
          空のままにすると、立ち絵の姿勢・表情・向きのまま合成します。
        </span>
      </div>
      {lightbox.node}
    </div>
  );
}

/** この組み合わせが使う参照画像の枚数。数え方はAPI側の合成と同じ。 */
function referenceImageCount(backgroundId: string, characterIds: string[]): number {
  return characterIds.length + (backgroundId ? 1 : 0);
}

/** 静止画に使う素材の選択。新しく作るときと、あとから入れ替えるときで同じものを使う。
 *
 * 合成が受け取れる参照画像には上限があるため、選んでいる最中に枚数を出す。
 * 上限は合成のときに分かっても直しようがないので、選ぶこの場で知らせる。
 */
function CompositionPicker({
  characters,
  backgrounds,
  backgroundId,
  characterIds,
  onChange,
  maxCharacters,
}: {
  characters: api.Character[];
  backgrounds: api.Asset[];
  backgroundId: string;
  characterIds: string[];
  onChange: (next: { backgroundId: string; characterIds: string[] }) => void;
  /** APIから受け取る人数の上限。まだ読めていない間は null。 */
  maxCharacters: number | null;
}) {
  const over = maxCharacters !== null && characterIds.length > maxCharacters;
  const full = maxCharacters !== null && characterIds.length >= maxCharacters;

  function toggled(list: string[], id: string): string[] {
    return list.includes(id) ? list.filter((x) => x !== id) : [...list, id];
  }

  const background = backgrounds.find((b) => b.id === backgroundId);

  return (
    <>
      <div className="vs-field">
        <label>背景</label>
        <select
          className="vs-select"
          value={backgroundId}
          onChange={(e) => onChange({ backgroundId: e.target.value, characterIds })}
        >
          <option value="">（なし）</option>
          {backgrounds.map((b) => (
            <option key={b.id} value={b.id}>{b.name}</option>
          ))}
        </select>
        {/* 名前だけでは似た背景（時間帯違いなど）を見分けにくいので、選んだ1枚を出す。 */}
        {background && (
          background.has_image ? (
            <img
              className="vs-thumb vs-picked-background"
              src={api.assetImageSrc("background-assets", background)}
              alt={`背景「${background.name}」`}
            />
          ) : (
            <div className="vs-muted">この背景にはまだ画像がありません</div>
          )
        )}
      </div>
      <div className="vs-field">
        <label>登場人物</label>
        <div className="vs-row">
          {characters.length === 0 && <span className="vs-muted">まだ登場人物がありません</span>}
          {characters.map((c) => {
            const picked = characterIds.includes(c.id);
            return (
              <label
                key={c.id}
                className="vs-pill"
                style={{
                  cursor: !picked && full ? "not-allowed" : "pointer",
                  borderColor: picked ? "#2e3934" : undefined,
                  opacity: !picked && full ? 0.5 : undefined,
                }}
                title={!picked && full ? `人物は${maxCharacters}人までです` : undefined}
              >
                <input
                  type="checkbox"
                  checked={picked}
                  disabled={!picked && full}
                  onChange={() => onChange({ backgroundId, characterIds: toggled(characterIds, c.id) })}
                /> {c.name}
                {c.figure_count === 0 && <span className="vs-muted">（立ち絵なし）</span>}
              </label>
            );
          })}
        </div>
      </div>
      {maxCharacters !== null && (
        <div className={over ? "vs-warn vs-block-reason" : "vs-muted"}>
          人物 {characterIds.length} / {maxCharacters} 人
          {over && ` — ${maxCharacters}人以内にしてください`}
          {!over && full && " — 3人以上を出したいときは、人物を入れた1枚を背景として登録し直してください"}
        </div>
      )}
    </>
  );
}

function StillImageCard({
  stillImage,
  projectName,
  characters,
  backgrounds,
  onChanged,
  onEnlarge,
  maxCharacters,
}: {
  stillImage: StillImage;
  projectName: string;
  characters: api.Character[];
  backgrounds: api.Asset[];
  onChanged: () => Promise<void>;
  /** 出来上がった静止画を拡大する。一覧の側で開き、前後の静止画へ送れるようにする。 */
  onEnlarge: () => void;
  maxCharacters: number | null;
}) {
  const [charInfos, setCharInfos] = useState<api.StillImageCharacter[]>([]);
  // 画面に出ている人物ごとの指定。合成するときにこの内容を保存し、その内容で合成する。
  // 合成しなければ保存しない。
  const [specDrafts, setSpecDrafts] = useState<Record<string, api.StillImageCharacterSpec>>({});
  // 人物ごとの立ち絵。選び直せるように、この静止画に出ている人物の分だけ読む。
  const [figuresByCharacter, setFiguresByCharacter] = useState<Record<string, api.CharacterFigure[]>>({});
  const [prompt, setPrompt] = useState("");
  // 選んだ丁寧さは覚えておき、次に開いたときもそのまま出す。
  const [quality, setQuality] = useRememberedSetting("composition_quality");
  const [error, setError] = useState<string | null>(null);
  const compose = useJobRunner();
  const deletion = useDeletion(setError);
  // 保存は中身を受け取ってから行うので、その間を出す。
  const [saving, setSaving] = useState(false);
  // 保存し終えたことは画面で伝える。黙って終わると、保存できたのか分からない。
  const [saved, setSaved] = useState<string | null>(null);

  /** 合成した静止画を挿絵として保存する。名前は後から見て分かる形にし、保存先は選べるようにする。 */
  const saveImage = () =>
    void (async () => {
      setSaving(true);
      setError(null);
      setSaved(null);
      const name = safeFileName(
        [projectName, stillImage.name || `静止画${stillImage.ordinal}`],
        "png",
      );
      try {
        const outcome = await api.saveFile(
          api.stillImageSrc(stillImage),
          name,
          api.stillImageSaveAsPath(stillImage.id),
        );
        if (outcome === "saved") setSaved(name);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setSaving(false);
      }
    })();

  // 使う素材の入れ替え。保存するまではここだけで持つ。
  const savedComposition = {
    backgroundId: stillImage.background_asset_id ?? "",
    characterIds: stillImage.character_ids,
  };
  // 一覧を取り直すたびに配列は作り直されるので、中身を文字列にして比べる。
  // そうしないと、別の操作で一覧が更新されただけで編集中の内容が消える。
  const savedKey = [savedComposition.backgroundId, savedComposition.characterIds.join(",")].join("|");
  const [draft, setDraft] = useState(savedComposition);
  const [savingComposition, setSavingComposition] = useState(false);
  useEffect(() => {
    setDraft({
      backgroundId: stillImage.background_asset_id ?? "",
      characterIds: stillImage.character_ids,
    });
    // savedKey が変わったときだけ、保存済みの内容へ戻す。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [savedKey]);
  const draftKey = [draft.backgroundId, draft.characterIds.join(",")].join("|");
  const draftOverLimit = maxCharacters !== null && draft.characterIds.length > maxCharacters;
  const savedOverLimit =
    maxCharacters !== null && savedComposition.characterIds.length > maxCharacters;

  // 保存の列が読む最新の内容。setState の反映を待つと、選び直した直後の保存が古い内容を読む。
  const charInfosRef = useRef<api.StillImageCharacter[]>([]);
  const updateCharInfos = useCallback(
    (next: (current: api.StillImageCharacter[]) => api.StillImageCharacter[]) => {
      charInfosRef.current = next(charInfosRef.current);
      setCharInfos(charInfosRef.current);
    },
    [],
  );

  const reloadCharacters = useCallback(async () => {
    try {
      const infos = await api.listStillImageCharacters(stillImage.id);
      updateCharInfos(() => infos);
      const entries = await Promise.all(
        infos.map(async (info) => {
          const figures = await api.listCharacterFigures(info.character_id);
          return [info.character_id, figures.filter((f) => f.state === "completed")] as const;
        }),
      );
      setFiguresByCharacter(Object.fromEntries(entries));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [stillImage.id, updateCharInfos]);

  useEffect(() => {
    void reloadCharacters();
  }, [reloadCharacters]);

  // 保存済みの指定が変わったときだけ、画面の指定を置き換える。
  // 立ち絵を選び直しただけで charInfos は作り直されるので、そのたびに戻すと書きかけが消える。
  const savedSpecsKey = JSON.stringify(charInfos.map((info) => [info.character_id, info.spec]));
  useEffect(() => {
    setSpecDrafts(Object.fromEntries(charInfos.map((info) => [info.character_id, info.spec])));
    // savedSpecsKey が変わったときだけ戻す。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [savedSpecsKey]);

  // 立ち絵の選び直しの保存は、1本の列で順に送る。追い越しがあると、後から届いた古い選択で
  // 上書きされる。合成もこの列が空くのを待つ。
  const saveQueue = useRef<Promise<unknown>>(Promise.resolve());
  // 人物ごとに、立ち絵を何回選び直したか。古い選択への返事で画面を戻さないために使う。
  const figureChoiceCount = useRef<Record<string, number>>({});

  /** 使う立ち絵を選び直す。画面はすぐ切り替え、保存は後ろで送る。 */
  const chooseFigure = (characterId: string, figureId: string): Promise<void> => {
    const count = (figureChoiceCount.current[characterId] ?? 0) + 1;
    figureChoiceCount.current[characterId] = count;
    const figures = figuresByCharacter[characterId] ?? [];
    updateCharInfos((infos) =>
      infos.map((info) =>
        info.character_id === characterId ? withChosenFigure(info, figures, figureId) : info,
      ),
    );
    const task = saveQueue.current.then(async () => {
      try {
        const saved = await api.chooseStillImageFigure(stillImage.id, characterId, figureId || null);
        // あとから選び直していれば、画面はその選択のまま。
        if ((figureChoiceCount.current[characterId] ?? 0) !== count) return;
        updateCharInfos((infos) =>
          infos.map((info) => (info.character_id === characterId ? saved : info)),
        );
      } catch (e) {
        // 先に切り替えた画面を、保存されている内容に戻す。
        await reloadCharacters();
        throw e;
      }
    });
    saveQueue.current = task.catch(() => undefined);
    return task;
  };

  const background = backgrounds.find((b) => b.id === stillImage.background_asset_id);
  const stillImageChars = stillImage.character_ids
    .map((id) => characters.find((c) => c.id === id))
    .filter((c): c is api.Character => Boolean(c));
  // 立ち絵が無い人物がいると合成できない。指定の欄を開いた状態で出す。
  const missingFigure = charInfos.some((info) => !info.has_figure);
  const sourceLightbox = useLightbox();

  return (
    <div className="vs-card">
      <div className="vs-head" style={{ marginBottom: 12 }}>
        <h2 style={{ fontSize: 19 }}>#{stillImage.ordinal} {stillImage.name || "（無題）"}</h2>
        <div className="vs-row">
          <span className="vs-pill">{stillImage.has_image ? "合成済み" : "未合成"}</span>
          <button
            className="vs-btn danger"
            onClick={() => void deletion.ask(async () => {
              const preview = await api.getStillImageDeletionPreview(stillImage.id);
              const items: string[] = [];
              if (preview.shot_count) items.push(`カット ${preview.shot_count}件`);
              if (preview.video_count) items.push(`生成済みの動画 ${preview.video_count}件`);
              if (preview.has_image) items.push("合成した静止画");
              return {
                title: `静止画「${preview.name || `#${preview.ordinal}`}」を削除しますか？`,
                lead: "この静止画と、そこから作ったものを削除します。元には戻せません。",
                items,
                perform: async () => {
                  await api.deleteStillImage(stillImage.id);
                  await onChanged();
                },
              };
            })}
          >
            静止画を削除
          </button>
        </div>
      </div>
      {error && <div className="vs-error">{error}</div>}
      {/* 合成に渡る素材をそのまま並べる。人物は、この静止画で実際に使う立ち絵を出す。 */}
      <div className="vs-still-sources">
        <figure className="vs-still-source">
          {background?.has_image ? (
            <img
              className="vs-still-source-image"
              src={api.assetImageSrc("background-assets", background)}
              alt={`背景「${background.name}」`}
              onClick={() =>
                sourceLightbox.open(
                  api.assetImageSrc("background-assets", background),
                  `背景「${background.name}」`,
                )
              }
            />
          ) : (
            <div className="vs-still-source-image empty">
              <span className="vs-muted">{background ? "画像なし" : "背景なし"}</span>
            </div>
          )}
          <figcaption className="vs-muted">背景: {background?.name ?? "なし"}</figcaption>
        </figure>
        {stillImageChars.map((char) => {
          const info = charInfos.find((item) => item.character_id === char.id);
          const figureUrl = info?.figure_url ? api.apiUrl(info.figure_url) : null;
          return (
            <figure key={char.id} className="vs-still-source">
              {figureUrl ? (
                <img
                  className="vs-still-source-image figure"
                  src={figureUrl}
                  alt={`${char.name} の立ち絵`}
                  onClick={() => sourceLightbox.open(figureUrl, `${char.name} の立ち絵`)}
                />
              ) : (
                <div className="vs-still-source-image figure empty">
                  <span className="vs-muted">{info ? "立ち絵なし" : "読み込み中"}</span>
                </div>
              )}
              <figcaption className="vs-muted">{char.name}</figcaption>
            </figure>
          );
        })}
      </div>
      {/* 合成できない組み合わせになっていることは、ここでしか直せない。
          人数が上限を超えているときは開いた状態で出す。 */}
      <details className="vs-collapse" open={savedOverLimit}>
        <summary>使う素材を変える</summary>
        <div className="vs-detail-body">
          <p className="vs-muted">
            選び直して保存すると、次の合成からこの組み合わせを使います。合成済みの画像はそのまま残ります。
          </p>
          <CompositionPicker
            characters={characters}
            backgrounds={backgrounds}
            backgroundId={draft.backgroundId}
            characterIds={draft.characterIds}
            onChange={setDraft}
            maxCharacters={maxCharacters}
          />
          <div className="vs-row">
            <button
              className="vs-btn"
              disabled={savingComposition || draftOverLimit || draftKey === savedKey}
              onClick={() => void (async () => {
                setSavingComposition(true);
                setError(null);
                try {
                  await api.updateStillImage(stillImage.id, {
                    background_asset_id: draft.backgroundId || null,
                    character_ids: draft.characterIds,
                  });
                  await onChanged();
                } catch (e) {
                  setError(e instanceof Error ? e.message : String(e));
                } finally {
                  setSavingComposition(false);
                }
              })()}
            >
              {savingComposition ? "保存中..." : "この組み合わせで保存"}
            </button>
            <button
              className="vs-btn ghost"
              disabled={savingComposition || draftKey === savedKey}
              onClick={() => setDraft(savedComposition)}
            >
              変更を取り消す
            </button>
          </div>
        </div>
      </details>

      <div className="vs-still-layout">
        {/* 主役は出来上がった静止画。押すと拡大できる。 */}
        <div className="vs-still-preview">
          {stillImage.has_image ? (
            <button className="vs-still-image" onClick={onEnlarge}>
              <img src={api.stillImageSrc(stillImage)} alt="合成した静止画" />
              <span className="vs-muted">クリックで拡大</span>
            </button>
          ) : (
            <div className="vs-still-image empty">
              <span className="vs-muted">まだ静止画がありません</span>
            </div>
          )}
          {/* 挿絵として使うならこの1枚が完成品。アプリの外へ取り出せるようにする。 */}
          {stillImage.has_image && (
            <div className="vs-row" style={{ marginTop: 8 }}>
              <button className="vs-btn ghost" disabled={saving} onClick={saveImage}>
                {saving ? "保存しています..." : "名前を付けて保存"}
              </button>
            </div>
          )}
          {saved && <p className="vs-ok" style={{ marginTop: 8 }}>「{saved}」を保存しました。</p>}
        </div>

        <div className="vs-still-controls">
          <div className="vs-field">
            <label>合成の状況説明（任意）</label>
            <textarea
              className="vs-textarea"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder={
                stillImage.character_ids.length > 1
                  ? "例: 二人で話している"
                  : "例: 本を読んでいる"
              }
            />
            {/* 姿勢・表情・向きは人物ごとの欄で指定する。ここは出来事や人物どうしの関わりを書く場所。
                人物ごとの欄が空なら立ち絵の向きのままと指示されるため、「向かい合う」をここに
                書くだけでは、正面を向いたままという指示とぶつかる。 */}
            <div className="vs-muted">
              その場で起きていることや、人物どうしの関わりを書きます。姿勢・表情・向き・立ち位置は、
              下の「人物ごとの指定」で人物ごとに書きます。
              {stillImage.character_ids.length > 1
                && "状況説明に名前を書くと、その人物だけに効きます。向かい合わせたいときは、それぞれの向きも人物ごとに書きます。"}
            </div>
          </div>
          {/* 速さと出来のどちらを取るかは作品づくりの判断なので、その場で選べるようにする。 */}
          <div className="vs-field vs-quality-field">
            <label>合成の丁寧さ</label>
            <select
              className="vs-input"
              value={quality}
              onChange={(e) => setQuality(e.target.value as "fast" | "careful")}
            >
              <option value="fast">速さ優先（数秒）</option>
              <option value="careful">丁寧に合成（数分・指定の反映が強い）</option>
            </select>
          </div>
          <button
            className="vs-btn"
            disabled={compose.running || !stillImage.can_compose}
            onClick={() =>
              void compose.run(
                async () => {
                  // 選び直した立ち絵の保存が届いてから合成する。届く前だと前の立ち絵で合成される。
                  await saveQueue.current;
                  // 人物ごとの指定は、画面に出ている内容を合成と一緒に保存する。
                  const characterSpecs = Object.fromEntries(
                    Object.entries(specDrafts).filter(([id]) => stillImage.character_ids.includes(id)),
                  );
                  const r = await api.composeStillImage(stillImage.id, {
                    prompt,
                    quality,
                    character_specs: characterSpecs,
                  });
                  // 保存された指定を読み直し、画面と保存済みの内容をそろえる。
                  void reloadCharacters();
                  return { job_id: r.job_id };
                },
                onChanged,
                setError,
              )
            }
          >
            {compose.running ? "合成中..." : "静止画を合成"}
          </button>
          {/* 押せない理由は必ずボタンのそばに出す（押して409で気づく形にしない）。 */}
          {!stillImage.can_compose && (
            <div className="vs-warn vs-block-reason">{stillImage.compose_blocked_reason}</div>
          )}
          {compose.running && (
            <Progress value={compose.progress} label={compose.message} runner={compose} setError={setError} />
          )}

          {/* 人物ごとの指定は、必要なときだけ開く。 */}
          {stillImageChars.length > 0 && (
            <details className="vs-collapse" open={missingFigure}>
              <summary>人物ごとの指定（{stillImageChars.length}人）</summary>
              <p className="vs-muted vs-state-lead">
                どの立ち絵を使うかと、姿勢・表情・向き・立ち位置をここで決めます。書かなければ
                立ち絵のままの姿勢で合成します。ここに書いた内容は、静止画を合成したときに保存されます。
                衣装を変えたいときは素材作成の「登場人物」タブで別の立ち絵を作ってください。
              </p>
              {stillImageChars.map((char) => {
                const info = charInfos.find((item) => item.character_id === char.id);
                return (
                  <StillImageCharacterPanel
                    key={char.id}
                    character={char}
                    info={info}
                    figures={figuresByCharacter[char.id] ?? []}
                    spec={specDrafts[char.id] ?? info?.spec ?? EMPTY_STILL_IMAGE_CHARACTER_SPEC}
                    onSpecChange={(spec) =>
                      setSpecDrafts((drafts) => ({ ...drafts, [char.id]: spec }))
                    }
                    onChooseFigure={(figureId) => chooseFigure(char.id, figureId)}
                  />
                );
              })}
            </details>
          )}
        </div>
      </div>
      {sourceLightbox.node}
      {deletion.dialog}
    </div>
  );
}

/**
 * 静止画の一覧と、新しい静止画を作るところ。
 *
 * 挿絵として使うならここが完成地点で、動画を作る場合はこの1枚が「シーン」の起点になる。
 */
function StillImagesTab({ projectId, projectName }: { projectId: string; projectName: string }) {
  const [stillImages, setStillImages] = useState<StillImage[]>([]);
  const [characters, setCharacters] = useState<api.Character[]>([]);
  const [backgrounds, setBackgrounds] = useState<api.Asset[]>([]);
  const [error, setError] = useState<string | null>(null);

  const [name, setName] = useState("");
  const [bg, setBg] = useState("");
  const [charIds, setCharIds] = useState<string[]>([]);
  // 1枚に入れられる人数。作品によらないのでここで一度だけ読む。
  const [maxCharacters, setMaxCharacters] = useState<number | null>(null);
  // 出来上がった静止画の拡大。前後の静止画へ送れるよう、一覧の側で持つ。
  const stillImageLightbox = useLightbox();

  useEffect(() => {
    void (async () => {
      try {
        const limits = await api.getCompositionLimits();
        setMaxCharacters(limits.max_characters);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    })();
  }, []);

  const reload = useCallback(async () => {
    try {
      setStillImages(await api.listStillImages(projectId));
      setCharacters(await api.listCharacters(projectId));
      setBackgrounds(await api.listAssets(projectId, "background-assets"));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [projectId]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const newOverLimit = maxCharacters !== null && charIds.length > maxCharacters;

  return (
    <div>
      {error && <div className="vs-error">{error}</div>}
      <div className="vs-card">
        <h2>静止画を作成</h2>
        <p className="vs-muted">
          背景と、登場人物の立ち絵を1枚に合成します。ここで出来上がった1枚が挿絵になります。
        </p>
        <div className="vs-field">
          <label>名前</label>
          <input className="vs-input" value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        <CompositionPicker
          characters={characters}
          backgrounds={backgrounds}
          backgroundId={bg}
          characterIds={charIds}
          onChange={(next) => {
            setBg(next.backgroundId);
            setCharIds(next.characterIds);
          }}
          maxCharacters={maxCharacters}
        />
        <button
          className="vs-btn"
          disabled={newOverLimit}
          onClick={() => void (async () => {
            try {
              await api.createStillImage(projectId, {
                name,
                background_asset_id: bg || null,
                character_ids: charIds,
              });
              setName("");
              setBg("");
              setCharIds([]);
              await reload();
            } catch (e) {
              setError(e instanceof Error ? e.message : String(e));
            }
          })()}
        >
          静止画を追加
        </button>
      </div>

      {stillImages.map((stillImage) => (
        <StillImageCard
          key={stillImage.id}
          stillImage={stillImage}
          projectName={projectName}
          characters={characters}
          backgrounds={backgrounds}
          onChanged={reload}
          onEnlarge={() => {
            const shown = stillImages.filter((item) => item.has_image);
            stillImageLightbox.openGallery(
              shown.map((item) => ({
                src: api.stillImageSrc(item),
                caption: `静止画 #${item.ordinal} ${item.name || "（無題）"}`,
              })),
              shown.findIndex((item) => item.id === stillImage.id),
            );
          }}
          maxCharacters={maxCharacters}
        />
      ))}
      {stillImageLightbox.node}
    </div>
  );
}

// ============================ シーン（静止画から作るカット） ============================

/** 尺の表示（小数点以下1桁まで、末尾の0は落とす）。 */
function formatSeconds(seconds: number): string {
  return `${Number(seconds.toFixed(1))}秒`;
}

/** この静止画に写っている人物（セリフの話す人の候補）。 */
type ScenePerson = { id: string; name: string };

// 話す人の選択肢で、ナレーションを表す値（人物のidと重ならない）。
const NARRATION_SPEAKER = "__narration__";

/** セリフ・ナレーションの行。行ごとに話す人を選ぶ。
 *
 * 話す人を決めずに1つの欄へ書く形だと、二人いる場面でどちらが話すかがモデル任せになる。
 * 行の並びはまるごと保存し直す（並びの順がそのまま話す順になる）。
 */
function SpeechLinesEditor({
  lines,
  people,
  disabled,
  onSave,
}: {
  lines: api.ShotSpeechLineRead[];
  people: ScenePerson[];
  disabled: boolean;
  onSave: (lines: api.ShotSpeechLine[]) => Promise<void>;
}) {
  // 言葉は手元で書き、フォーカスが外れたときだけ保存する。
  const [texts, setTexts] = useState<string[]>(lines.map((line) => line.text));
  const saved = JSON.stringify(lines.map((line) => line.text));
  useEffect(() => {
    setTexts(JSON.parse(saved) as string[]);
  }, [saved]);

  // 書きかけの言葉を失わないよう、保存は常に手元の言葉で組み立てる。
  const current = (): api.ShotSpeechLine[] =>
    lines.map((line, index) => ({
      speaker_character_id: line.speaker_character_id,
      narration: line.narration,
      text: texts[index] ?? line.text,
    }));
  const speakerOf = (value: string): Pick<api.ShotSpeechLine, "speaker_character_id" | "narration"> =>
    value === NARRATION_SPEAKER
      ? { speaker_character_id: null, narration: true }
      : { speaker_character_id: value, narration: false };

  return (
    <div className="vs-speech-lines">
      {lines.map((line, index) => {
        const missing = !line.narration && !line.speaker_present;
        return (
          <div key={index} className="vs-speech-line">
            <div className="vs-row" style={{ gap: 6, alignItems: "flex-start" }}>
              <select
                className="vs-select"
                style={{ width: 150 }}
                disabled={disabled}
                aria-label="話す人"
                value={line.narration ? NARRATION_SPEAKER : missing ? "" : line.speaker_character_id ?? ""}
                onChange={(e) => {
                  const next = current();
                  next[index] = { ...next[index], ...speakerOf(e.target.value) };
                  void onSave(next);
                }}
              >
                {missing && (
                  <option value="" disabled>
                    話す人を選ぶ
                  </option>
                )}
                {people.map((person) => (
                  <option key={person.id} value={person.id}>
                    {person.name}
                  </option>
                ))}
                <option value={NARRATION_SPEAKER}>ナレーション</option>
              </select>
              <input
                className="vs-input"
                style={{ flex: 1 }}
                disabled={disabled}
                aria-label="セリフ"
                value={texts[index] ?? ""}
                placeholder={line.narration ? "例: その日は朝から雨だった" : "例: 行こう"}
                onChange={(e) => {
                  const next = [...texts];
                  next[index] = e.target.value;
                  setTexts(next);
                }}
                onBlur={() => {
                  if ((texts[index] ?? "") === line.text) return;
                  void onSave(current());
                }}
              />
              <button
                className="vs-btn ghost"
                disabled={disabled}
                onClick={() => void onSave(current().filter((_, i) => i !== index))}
              >
                削除
              </button>
            </div>
            {missing && (
              <div className="vs-warn vs-block-reason">
                話す人がこの静止画にいません。話す人を選び直してください。
              </div>
            )}
            {!line.narration && line.speaker_present && !line.speaker_description && (
              <div className="vs-muted">
                この人物を見分ける手がかりがありません。静止画の画面で配置（左・右など）を書くと、生成に使う文でこの人を名指しできます。
              </div>
            )}
          </div>
        );
      })}
      <button
        className="vs-btn ghost"
        disabled={disabled}
        onClick={() => {
          // 前の行と違う人を既定にする（掛け合いは交互に話すことが多い）。
          const previous = lines[lines.length - 1]?.speaker_character_id;
          const speaker = people.find((person) => person.id !== previous) ?? people[0];
          void onSave([
            ...current(),
            speaker
              ? { speaker_character_id: speaker.id, narration: false, text: "" }
              : { speaker_character_id: null, narration: true, text: "" },
          ]);
        }}
      >
        セリフを足す
      </button>
    </div>
  );
}

/** カット1件の編集と生成。起点・構図・動き・音・ビートをこの中に集める。
 *
 * 出来上がり（起点の1枚と動画）を上に横並びで置き、設定は折りたたむ。カットは何本も
 * 並ぶので、開いたままだと隣のカットと見比べられない。
 */
function ShotCard({
  shot,
  engine,
  stillImage,
  projectName,
  reload,
  runner,
  setError,
  lightbox,
  deletion,
  justAdded,
  people,
}: {
  shot: api.Shot;
  engine: api.VideoEngine;
  stillImage: StillImage;
  projectName: string;
  reload: () => Promise<void>;
  runner: ReturnType<typeof useJobRunner>;
  setError: (value: string | null) => void;
  lightbox: ReturnType<typeof useLightbox>;
  deletion: ReturnType<typeof useDeletion>;
  /** 追加したばかりのカット。動きの指示の欄へ移って、そのまま書き始められるようにする。 */
  justAdded: boolean;
  /** この静止画に写っている人物（セリフの話す人の候補）。 */
  people: ScenePerson[];
}) {
  // 入力中の値は手元に持ち、フォーカスが外れたときだけ保存する。
  const [fields, setFields] = useState<api.ShotFields>({
    motion_prompt: shot.motion_prompt,
    ambience_prompt: shot.ambience_prompt,
    music_prompt: shot.music_prompt,
    composition_prompt: shot.composition_prompt,
    note: shot.note,
  });
  const [beatDraft, setBeatDraft] = useState({ duration_seconds: 3, motion_prompt: "" });
  // 設定の開閉。出来上がったカットは畳んでおき、まだのカットは開いておく。
  // 開閉を手元で持つのは、作り直しの最中に勝手に閉じてしまわないようにするため。
  const [settingsOpen, setSettingsOpen] = useState(!shot.has_video);
  // 保存は中身を受け取ってから行うので、その間を出す。
  const [saving, setSaving] = useState(false);
  // 保存し終えたことは画面で伝える。黙って終わると、保存できたのか分からない。
  const [saved, setSaved] = useState<string | null>(null);
  // 用意できた「生成に使う文」。用意できていないものは出さない。
  const [prompt, setPrompt] = useState<api.ShotPromptState | null>(null);
  // 直接書き直している最中の文（区間ごと）。
  const [drafts, setDrafts] = useState<string[]>([]);
  const audio = engine === "minimax_h3";
  const busy = runner.running;
  const mine = runner.activeKey === shot.id;
  // 文を直接書き直している間は、その文のもとになる指示を変えさせない。
  const locked = shot.prompt_edit_mode;
  const motionRef = useRef<HTMLTextAreaElement>(null);
  // カットの指示の保存は順に送る。ビートを足すとカット全体の動きはビートへ移るので、
  // 書いた直後に足すときは、その保存を済ませてから足す（追い越すと、移した後に書き戻される）。
  const pendingSave = useRef<Promise<void>>(Promise.resolve());

  // カット全体の動きは、ビートを足すとサーバー側でビートへ移る。手元の値も合わせる。
  useEffect(() => {
    setFields((current) => ({ ...current, motion_prompt: shot.motion_prompt }));
  }, [shot.motion_prompt]);

  // 追加した直後だけ。読み込み直しでは移さない（書いている途中の欄から飛ばさない）。
  useEffect(() => {
    if (!justAdded) return;
    motionRef.current?.scrollIntoView({ block: "center" });
    motionRef.current?.focus({ preventScroll: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const result = await api.getShotPrompt(shot.id, engine);
        if (cancelled) return;
        setPrompt(result);
        setDrafts(result.segments.map((segment) => segment.prompt));
      } catch {
        // 文の表示は補助なので、取れなくても操作は止めない。
        if (!cancelled) setPrompt(null);
      }
    })();
    return () => {
      cancelled = true;
    };
    // ビートや話す人を変えても、カット自体の更新時刻は変わらない。文が古くなったかで読み直す。
  }, [shot.id, shot.updated_at, shot.prompt_is_stale, engine]);

  const save = (patch: api.ShotFields) => {
    pendingSave.current = pendingSave.current.then(async () => {
      try {
        await api.updateShot(shot.id, patch);
        await reload();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    });
  };

  const saveBeatLines = async (beatId: string, lines: api.ShotSpeechLine[]) => {
    try {
      await api.updateShotBeat(beatId, { speech_lines: lines });
      await reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  /** 生成に使う文だけを先に用意する（動画は作らない）。 */
  const preparePrompt = () =>
    void runner.run(
      async () => {
        await pendingSave.current;
        const r = await api.prepareShotPrompt(shot.id, engine);
        return { job_id: r.job_id };
      },
      reload,
      setError,
      shot.id,
    );

  const setEditMode = (enabled: boolean) =>
    void (async () => {
      try {
        const state = await api.setShotPromptEditMode(shot.id, engine, enabled);
        setPrompt(state);
        setDrafts(state.segments.map((segment) => segment.prompt));
        await reload();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    })();

  const saveDrafts = (next: string[]) =>
    void (async () => {
      try {
        const state = await api.writeShotPrompt(shot.id, engine, next);
        setPrompt(state);
        await reload();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    })();

  // 起点がどこから来るかは、押す前に分かるようにしておく（黙って静止画へ戻さない）。
  const START_FRAME_LABELS: Record<api.StartFrameSource, string> = {
    custom: "構図を変えて作った1枚",
    previous: "前のカットの最終フレーム",
    still_image: "このシーンの静止画",
    none: "まだありません",
  };
  const startFrameLabel = START_FRAME_LABELS[shot.start_frame_source];
  const startFrameUrl = api.shotStartFrameSrc(shot);
  const videoUrl = api.shotVideoSrc(shot);

  return (
    <div className="vs-list-item" style={{ marginTop: 10 }}>
      <div className="vs-row" style={{ justifyContent: "space-between" }}>
        <strong>カット #{shot.ordinal}</strong>
        <div className="vs-row">
          <span className="vs-pill">{formatSeconds(shot.total_seconds)}</span>
          <span className="vs-pill">{shot.has_video ? "動画あり" : "未生成"}</span>
          {/* このカット1本だけを使いたいこともある。作ったままの形で保存できるようにする。 */}
          {shot.has_video && (
            <button
              className="vs-btn ghost"
              disabled={saving}
              onClick={() => void (async () => {
                setSaving(true);
                setError(null);
                setSaved(null);
                const name = safeFileName(
                  [
                    projectName,
                    stillImage.name || `静止画${stillImage.ordinal}`,
                    `カット${shot.ordinal.toString().padStart(2, "0")}`,
                  ],
                  shot.video_format,
                );
                try {
                  const outcome = await api.saveFile(
                    videoUrl,
                    name,
                    api.shotVideoSaveAsPath(shot.id),
                  );
                  if (outcome === "saved") setSaved(name);
                } catch (e) {
                  setError(e instanceof Error ? e.message : String(e));
                } finally {
                  setSaving(false);
                }
              })()}
            >
              {saving ? "保存しています..." : "保存"}
            </button>
          )}
          <button
            className="vs-btn danger"
            onClick={() => void deletion.ask(async () => {
              const preview = await api.getShotDeletionPreview(shot.id);
              const items: string[] = [];
              if (preview.has_video) items.push("生成済みの動画");
              if (preview.beat_video_count) items.push(`ビートの区間動画 ${preview.beat_video_count}件`);
              return {
                title: `カット #${preview.ordinal} を削除しますか？`,
                lead: "このカットを削除します。元には戻せません。",
                items,
                perform: async () => {
                  await api.deleteShot(shot.id);
                  await reload();
                },
              };
            })}
          >
            削除
          </button>
        </div>
      </div>

      {/* --- 出来上がり: 起点の1枚と、そこから作った動画を並べて見比べる --- */}
      <div className="vs-shot-frames">
        {shot.start_frame_source === "none" ? (
          <div className="vs-still-image empty">
            <span className="vs-muted">起点になる画像がまだありません</span>
          </div>
        ) : (
          <button
            className="vs-still-image"
            onClick={() => lightbox.open(
              startFrameUrl,
              `カット #${shot.ordinal} の起点（${startFrameLabel}）`,
            )}
          >
            <img src={startFrameUrl} alt="このカットの起点" />
            <span className="vs-muted">起点：{startFrameLabel}</span>
          </button>
        )}
        {shot.has_video ? (
          <button
            className="vs-still-image"
            onClick={() => lightbox.open(
              videoUrl,
              `カット #${shot.ordinal} の動画`,
              shot.video_format === "mp4" ? "video" : "image",
            )}
          >
            {shot.video_format === "mp4" ? (
              <video src={videoUrl} muted loop autoPlay playsInline />
            ) : (
              <img src={videoUrl} alt="生成した動画" />
            )}
            <span className="vs-muted">
              {shot.video_format === "mp4" ? "このカットの動画（音あり）" : "このカットの動画"}
            </span>
          </button>
        ) : (
          <div className="vs-still-image empty">
            <span className="vs-muted">まだ動画はありません</span>
          </div>
        )}
      </div>
      {saved && <p className="vs-ok vs-block-reason">「{saved}」を保存しました。</p>}
      {/* 覚え書きは、設定を畳んだままでも読めるようにここへ出す（カットを見比べるときの手がかり）。 */}
      {shot.note.trim() && <p className="vs-shot-note">{shot.note}</p>}
      {shot.ordinal > 1 && shot.start_frame_source === "still_image" && (
        <div className="vs-warn vs-block-reason">
          前のカットがまだ生成されていないため、このシーンの静止画から始まります。
          続きから始めたい場合は、先に前のカットを生成してください。
        </div>
      )}
      {/* 次のカットへ繋げられるかは、作った直後に分かるようにしておく。 */}
      {shot.has_video && !shot.has_last_frame && (
        <div className="vs-warn vs-block-reason">
          最終フレームを取り出せませんでした。次のカットはこの続きから始められません。
        </div>
      )}

      {/* --- 設定: 作り終わったカットは畳んでおき、複数カットを並べて見られるようにする --- */}
      <details
        className="vs-collapse"
        open={settingsOpen}
        onToggle={(e) => setSettingsOpen(e.currentTarget.open)}
      >
        <summary>
          このカットの設定
          {locked && <span className="vs-pill">文を書き直し中</span>}
        </summary>

        {/* --- 起点と構図 --- */}
        <div className="vs-field">
          <label>起点の構図</label>
          <input
            className="vs-input"
            value={fields.composition_prompt ?? ""}
            placeholder="例: バストアップにする、背後から見た構図にする"
            onChange={(e) => setFields({ ...fields, composition_prompt: e.target.value })}
            onBlur={() => save({ composition_prompt: fields.composition_prompt ?? "" })}
          />
          <div className="vs-muted">
            このカットの最初の1枚を、構図を変えて作り直すための指示です。書いただけでは使われず、「起点を作り直す」を押したときに作り直します。カットの中でカメラを動かしたいときは、動き・カメラワークの指示（ビートがあるときは各ビートの欄）に書いてください。
          </div>
          <div className="vs-row">
            <button
              className="vs-btn ghost"
              disabled={busy || !(fields.composition_prompt ?? "").trim()}
              onClick={() =>
                void runner.run(
                  async () => {
                    await api.updateShot(shot.id, { composition_prompt: fields.composition_prompt ?? "" });
                    const r = await api.composeShotFrame(shot.id, { target: "start" });
                    return { job_id: r.job_id };
                  },
                  reload,
                  setError,
                  shot.id,
                )
              }
            >
              起点を作り直す
            </button>
            {shot.has_custom_start_frame && (
              <button
                className="vs-btn ghost"
                disabled={busy}
                onClick={() => void (async () => {
                  try {
                    await api.clearShotFrame(shot.id, "start");
                    await reload();
                  } catch (e) {
                    setError(e instanceof Error ? e.message : String(e));
                  }
                })()}
              >
                外して続きに戻す
              </button>
            )}
            {audio && (
              <button
                className="vs-btn ghost"
                disabled={busy || !(fields.composition_prompt ?? "").trim()}
                onClick={() =>
                  void runner.run(
                    async () => {
                      const r = await api.composeShotFrame(shot.id, {
                        target: "end",
                        composition_prompt: fields.composition_prompt ?? "",
                      });
                      return { job_id: r.job_id };
                    },
                    reload,
                    setError,
                    shot.id,
                  )
                }
              >
                {shot.has_custom_end_frame ? "着地の絵を作り直す" : "着地の絵にする"}
              </button>
            )}
          </div>
          {shot.has_custom_end_frame && audio && (
            <div className="vs-row">
              <span className="vs-muted">終わりの構図を固定しています。</span>
              <button
                className="vs-btn ghost"
                disabled={busy}
                onClick={() => void (async () => {
                  try {
                    await api.clearShotFrame(shot.id, "end");
                    await reload();
                  } catch (e) {
                    setError(e instanceof Error ? e.message : String(e));
                  }
                })()}
              >
                着地の絵を外す
              </button>
            </div>
          )}
        </div>

        <div className="vs-field">
          <label>コメント</label>
          <textarea
            className="vs-textarea"
            value={fields.note ?? ""}
            placeholder="例: あとで撮り直す"
            onChange={(e) => setFields({ ...fields, note: e.target.value })}
            onBlur={() => save({ note: fields.note ?? "" })}
          />
          <div className="vs-muted">
            自分用の覚え書きです。カットの一覧にそのまま出ます。生成の内容には影響しません。
          </div>
        </div>

        {locked && (
          <div className="vs-note warn">
            <strong>生成に使う文を直接書き直しています</strong>
            この間、下の指示は変えられません。指示から作り直したいときは、書き直しをやめてください。
          </div>
        )}

        {/* --- 指示（ビートが無いときはカット全体の指示を使う） --- */}
        {shot.beats.length === 0 && (
          <>
            <div className="vs-field">
              <label>動き・カメラワークの指示</label>
              <textarea
                ref={motionRef}
                className="vs-textarea"
                disabled={locked}
                value={fields.motion_prompt ?? ""}
                placeholder="例: ゆっくり振り返る。カメラは顔へゆっくり寄る"
                onChange={(e) => setFields({ ...fields, motion_prompt: e.target.value })}
                onBlur={() => save({ motion_prompt: fields.motion_prompt ?? "" })}
              />
            </div>
            {audio && (
              <div className="vs-field">
                <label>セリフ・ナレーション</label>
                <SpeechLinesEditor
                  lines={shot.speech_lines}
                  people={people}
                  disabled={locked}
                  onSave={async (lines) => save({ speech_lines: lines })}
                />
                <div className="vs-muted">
                  ここに書いた言葉だけが、上から順に読み上げられます。行が無ければ声は入りません。
                </div>
              </div>
            )}
          </>
        )}
        {audio && (
          <div className="vs-field">
            <label>環境音・効果音</label>
            <input
              className="vs-input"
              disabled={locked}
              value={fields.ambience_prompt ?? ""}
              placeholder="例: 雨音と遠くの雷"
              onChange={(e) => setFields({ ...fields, ambience_prompt: e.target.value })}
              onBlur={() => save({ ambience_prompt: fields.ambience_prompt ?? "" })}
            />
            <label>音楽（画面の外で鳴るBGM）</label>
            <input
              className="vs-input"
              disabled={locked}
              value={fields.music_prompt ?? ""}
              placeholder="例: 静かなピアノ（空なら音楽なし）"
              onChange={(e) => setFields({ ...fields, music_prompt: e.target.value })}
              onBlur={() => save({ music_prompt: fields.music_prompt ?? "" })}
            />
            {!shot.speech_lines.some((line) => line.text.trim()) && shot.beats.length === 0 && (
              <div className="vs-muted">
                セリフが空なので、このカットでは人物が声を出しません。
              </div>
            )}
          </div>
        )}

        {/* --- ビート（カット内の時間割り） --- */}
        <div className="vs-field">
          <label>カット内の時間割り（ビート）</label>
          {shot.beats.length === 0 ? (
            <div className="vs-muted">
              ビートを足すと、1つのカットの中で動きとセリフを時間ごとに分けられます。
            </div>
          ) : (
            shot.beats.map((beat) => (
              <div key={beat.id} className="vs-row" style={{ alignItems: "flex-start", gap: 8, marginTop: 6 }}>
                <span className="vs-pill">{formatSeconds(beat.start_seconds)}〜</span>
                <input
                  className="vs-input"
                  style={{ width: 72 }}
                  type="number"
                  min={0.5}
                  step={0.5}
                  disabled={locked}
                  defaultValue={beat.duration_seconds}
                  onBlur={(e) => void (async () => {
                    const value = Number(e.target.value);
                    if (!Number.isFinite(value) || value <= 0 || value === beat.duration_seconds) return;
                    try {
                      await api.updateShotBeat(beat.id, { duration_seconds: value });
                      await reload();
                    } catch (err) {
                      setError(err instanceof Error ? err.message : String(err));
                    }
                  })()}
                />
                <div style={{ flex: 1 }}>
                  <input
                    className="vs-input"
                    disabled={locked}
                    defaultValue={beat.motion_prompt}
                    placeholder="この区間の動き・カメラワーク"
                    onBlur={(e) => void (async () => {
                      if (e.target.value === beat.motion_prompt) return;
                      try {
                        await api.updateShotBeat(beat.id, { motion_prompt: e.target.value });
                        await reload();
                      } catch (err) {
                        setError(err instanceof Error ? err.message : String(err));
                      }
                    })()}
                  />
                  {audio && (
                    <div style={{ marginTop: 4 }}>
                      <SpeechLinesEditor
                        lines={beat.speech_lines}
                        people={people}
                        disabled={locked}
                        onSave={(lines) => saveBeatLines(beat.id, lines)}
                      />
                    </div>
                  )}
                  {beat.has_segment_video && (
                    <button
                      className="vs-shot-video"
                      onClick={() => lightbox.open(
                        api.shotBeatVideoSrc(shot, beat.id),
                        `カット #${shot.ordinal} のビート #${beat.ordinal}`,
                        beat.segment_video_format === "mp4" ? "video" : "image",
                      )}
                    >
                      {beat.segment_video_format === "mp4" ? (
                        <video src={api.shotBeatVideoSrc(shot, beat.id)} muted loop autoPlay playsInline />
                      ) : (
                        <img src={api.shotBeatVideoSrc(shot, beat.id)} alt="この区間の動画" />
                      )}
                      <span className="vs-muted">この区間（クリックで拡大）</span>
                    </button>
                  )}
                </div>
                <button
                  className="vs-btn danger"
                  disabled={busy || locked}
                  onClick={() => void (async () => {
                    try {
                      await api.deleteShotBeat(beat.id);
                      await reload();
                    } catch (err) {
                      setError(err instanceof Error ? err.message : String(err));
                    }
                  })()}
                >
                  削除
                </button>
              </div>
            ))
          )}
          <div className="vs-row" style={{ marginTop: 8, alignItems: "flex-start", gap: 8 }}>
            <input
              className="vs-input"
              style={{ width: 72 }}
              type="number"
              min={0.5}
              step={0.5}
              disabled={locked}
              value={beatDraft.duration_seconds}
              onChange={(e) => setBeatDraft({ ...beatDraft, duration_seconds: Number(e.target.value) })}
            />
            <input
              className="vs-input"
              style={{ flex: 1 }}
              disabled={locked}
              value={beatDraft.motion_prompt}
              placeholder="足すビートの動き・カメラワーク"
              onChange={(e) => setBeatDraft({ ...beatDraft, motion_prompt: e.target.value })}
            />
            <button
              className="vs-btn ghost"
              disabled={busy || locked}
              onClick={() => void (async () => {
                try {
                  await pendingSave.current;
                  await api.createShotBeat(shot.id, beatDraft);
                  setBeatDraft({ duration_seconds: 3, motion_prompt: "" });
                  await reload();
                } catch (err) {
                  setError(err instanceof Error ? err.message : String(err));
                }
              })()}
            >
              ビートを足す
            </button>
          </div>
        </div>

        {shot.beats.length > 1 && (
          <div className="vs-field">
            <label>ビートの作り方</label>
            <select
              className="vs-select"
              disabled={locked}
              value={shot.timeline_mode}
              onChange={(e) => save({ timeline_mode: e.target.value as api.TimelineMode })}
            >
              <option value="single">まとめて1回で作る（速い・時間の割り振りはモデル任せ）</option>
              <option value="split">ビートごとに分けて作る（時間どおり・ビートの数だけ時間がかかる）</option>
            </select>
          </div>
        )}

        {/* --- 生成に使う文（用意できたものだけを出す） --- */}
        <ShotPromptPanel
          shot={shot}
          prompt={prompt}
          drafts={drafts}
          setDrafts={setDrafts}
          busy={busy}
          onPrepare={preparePrompt}
          onSetEditMode={setEditMode}
          onSaveDrafts={saveDrafts}
        />
        {/* 作る・作り直すは設定の一番下に置く。指示を確かめてから押す流れにするため
            （出来上がったカットでは畳まれているので、開かないと押せない）。 */}
        <button
          className="vs-btn"
          style={{ marginTop: 8 }}
          disabled={busy || !shot.can_generate_video}
          onClick={() =>
            void runner.run(
              async () => {
                await pendingSave.current;
                const r = await api.generateShotVideo(shot.id, {
                  continue_from_previous: true,
                  engine,
                });
                return { job_id: r.job_id };
              },
              reload,
              setError,
              shot.id,
            )
          }
        >
          {mine ? "生成中..." : shot.has_video ? "動画を作り直す" : "動画を生成"}
        </button>
        {/* 押せない理由は必ずボタンのそばに出す（押して409で気づく形にしない）。 */}
        {!shot.can_generate_video && (
          <div className="vs-warn vs-block-reason">{shot.video_blocked_reason}</div>
        )}
      </details>

      {/* 進捗と中止は、畳んでも見えるように折りたたみの外へ出す。 */}
      {mine && <Progress value={runner.progress} label={runner.message} runner={runner} setError={setError} />}
      {busy && !mine && (
        <div className="vs-muted vs-block-reason">
          他の生成が実行中です（GPUは1件ずつ使います）。終わるまでお待ちください。
        </div>
      )}
    </div>
  );
}

/**
 * このカットの生成に使う文。
 *
 * 用意できた文だけを出す。作りかけの文を見せると、画面に出ている文と実際に送られる文が
 * 食い違い、直したつもりの内容が結果に出ない。文は「文を作る」か「動画を生成」で作る。
 */
function ShotPromptPanel({
  shot,
  prompt,
  drafts,
  setDrafts,
  busy,
  onPrepare,
  onSetEditMode,
  onSaveDrafts,
}: {
  shot: api.Shot;
  prompt: api.ShotPromptState | null;
  drafts: string[];
  setDrafts: (value: string[]) => void;
  busy: boolean;
  onPrepare: () => void;
  onSetEditMode: (enabled: boolean) => void;
  onSaveDrafts: (segments: string[]) => void;
}) {
  const multiple = (prompt?.segments.length ?? 0) > 1;

  if (prompt?.edit_mode) {
    return (
      <div className="vs-field">
        <label>生成に使う文（直接書き直し中）</label>
        <div className="vs-muted">
          ここに書いた文がそのまま使われます。上の指示から作り直したいときは、書き直しをやめてください。
        </div>
        {prompt.segments.map((segment, index) => (
          <div key={segment.label} style={{ marginTop: 8 }}>
            {multiple && (
              <div className="vs-muted">
                {segment.label}（{formatSeconds(segment.seconds)}）
              </div>
            )}
            <textarea
              className="vs-textarea"
              rows={8}
              value={drafts[index] ?? ""}
              onChange={(e) => setDrafts(drafts.map((text, i) => (i === index ? e.target.value : text)))}
              onBlur={() => {
                if (drafts[index] !== segment.prompt) onSaveDrafts(drafts);
              }}
            />
          </div>
        ))}
        <div className="vs-row" style={{ marginTop: 8 }}>
          <button className="vs-btn ghost" disabled={busy} onClick={() => onSetEditMode(false)}>
            書き直しをやめる（指示から作った文に戻す）
          </button>
        </div>
      </div>
    );
  }

  if (prompt?.ready) {
    return (
      <div className="vs-field">
        <label>生成に使う文</label>
        <div className="vs-muted">
          {shot.has_video
            ? "この動画を作ったときに使った文です。"
            : "動画を生成すると、この文が使われます。"}
        </div>
        {prompt.segments.map((segment) => (
          <div key={segment.label} style={{ marginTop: 8 }}>
            {multiple && (
              <div className="vs-muted">
                {segment.label}（{formatSeconds(segment.seconds)}）
              </div>
            )}
            <pre className="vs-prompt-preview">{segment.prompt}</pre>
          </div>
        ))}
        <div className="vs-row" style={{ marginTop: 8 }}>
          <button className="vs-btn ghost" disabled={busy} onClick={onPrepare}>
            指示から作り直す
          </button>
          <button className="vs-btn ghost" disabled={busy} onClick={() => onSetEditMode(true)}>
            この文を直接書き直す
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="vs-field">
      <label>生成に使う文</label>
      <div className="vs-muted">
        {prompt?.stale
          ? "指示を変えたので、この文は作り直します。動画を生成すれば自動で作り直しますが、先に作って中身を確かめることもできます。"
          : "動画を生成すると、上の指示から文を作って使います。先に作って、中身を確かめておくこともできます。"}
      </div>
      <div className="vs-row" style={{ marginTop: 8 }}>
        <button className="vs-btn ghost" disabled={busy} onClick={onPrepare}>
          生成に使う文を作る
        </button>
      </div>
    </div>
  );
}

/** 1枚の静止画から作るカットの一覧。動画を作る作品だけで使う。 */
function SceneCutsCard({
  stillImage,
  projectName,
  people,
}: {
  stillImage: StillImage;
  projectName: string;
  /** この静止画に写っている人物（セリフの話す人の候補）。 */
  people: ScenePerson[];
}) {
  const [shots, setShots] = useState<api.Shot[]>([]);
  // 追加したばかりのカット。その動きの指示の欄へ移って、続けて書けるようにする。
  const [addedShotId, setAddedShotId] = useState<string | null>(null);
  // 動画生成に使うモデル。選び直すとそれが次回からの既定になる
  // （H3で作ると決めた人に、毎回選び直させない）。
  const [engine, setEngine] = useRememberedSetting("video_engine");
  const [error, setError] = useState<string | null>(null);
  const video = useJobRunner();
  const deletion = useDeletion(setError);
  const lightbox = useLightbox();

  const reloadShots = useCallback(async () => {
    try {
      setShots(await api.listShots(stillImage.id));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [stillImage.id]);

  useEffect(() => {
    void reloadShots();
  }, [reloadShots]);

  return (
    <div className="vs-card">
      <div className="vs-head" style={{ marginBottom: 12 }}>
        <h2 style={{ fontSize: 19 }}>#{stillImage.ordinal} {stillImage.name || "（無題）"}</h2>
        <span className="vs-pill">{shots.length ? `カット ${shots.length}件` : "カットなし"}</span>
      </div>
      {error && <div className="vs-error">{error}</div>}

      {/* このシーンの起点になる1枚を、カットのそばに出しておく。 */}
      {stillImage.has_image ? (
        <button
          className="vs-still-image vs-scene-start"
          onClick={() => lightbox.open(
            api.stillImageSrc(stillImage),
            `静止画 #${stillImage.ordinal} ${stillImage.name || "（無題）"}`,
          )}
        >
          <img src={api.stillImageSrc(stillImage)} alt="このシーンの起点になる静止画" />
          <span className="vs-muted">このシーンの起点（クリックで拡大）</span>
        </button>
      ) : (
        <div className="vs-warn vs-block-reason">
          まだ静止画が合成されていません。「静止画」の画面で合成すると、ここから動かせます。
        </div>
      )}

      <div className="vs-field" style={{ marginTop: 12 }}>
        <label>動画の作り方</label>
        <select
          className="vs-select"
          value={engine}
          onChange={(e) => setEngine(e.target.value as api.VideoEngine)}
        >
          {api.VIDEO_ENGINES.map((option) => (
            <option key={option.id} value={option.id}>
              {option.label}
            </option>
          ))}
        </select>
        <div className="vs-muted">
          {api.VIDEO_ENGINES.find((option) => option.id === engine)?.note}
        </div>
      </div>
      {/* 指示はカットの設定にまとめて書く。追加の前に書く欄を別に持つと、同じ指示の書き場所が2つになる。 */}
      <button
        className="vs-btn ghost"
        onClick={() => void (async () => {
          try {
            const created = await api.createShot(stillImage.id, {});
            setAddedShotId(created.id);
            await reloadShots();
          } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
          }
        })()}
      >
        カットを追加
      </button>

      {shots.map((shot) => (
        <ShotCard
          key={shot.id}
          shot={shot}
          engine={engine}
          stillImage={stillImage}
          projectName={projectName}
          reload={reloadShots}
          runner={video}
          setError={setError}
          lightbox={lightbox}
          deletion={deletion}
          justAdded={shot.id === addedShotId}
          people={people}
        />
      ))}
      {deletion.dialog}
      {lightbox.node}
    </div>
  );
}

/** 作った静止画を1つずつシーンにして、カットを足していく画面。 */
function ScenesTab({ projectId, projectName }: { projectId: string; projectName: string }) {
  const [stillImages, setStillImages] = useState<StillImage[]>([]);
  // セリフの話す人を名前で選べるようにする。
  const [characters, setCharacters] = useState<api.Character[]>([]);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      const [images, people] = await Promise.all([
        api.listStillImages(projectId),
        api.listCharacters(projectId),
      ]);
      setStillImages(images);
      setCharacters(people);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [projectId]);

  useEffect(() => {
    void reload();
  }, [reload]);

  return (
    <div>
      {error && <div className="vs-error">{error}</div>}
      <div className="vs-card">
        <h2>シーン</h2>
        <p className="vs-muted">
          静止画1枚が1つのシーンになります。動かしたい静止画にカットを足して、順番に動画を作ります。
          シーンを増やしたいときは「静止画」の画面で静止画を作ってください。
        </p>
      </div>

      {stillImages.length === 0 && (
        <div className="vs-card">
          <div className="vs-muted">
            まだ静止画がありません。「静止画」の画面で1枚作るところから始めてください。
          </div>
        </div>
      )}

      {stillImages.map((stillImage) => (
        <SceneCutsCard
          key={stillImage.id}
          stillImage={stillImage}
          projectName={projectName}
          people={stillImage.character_ids.flatMap((id) => {
            const person = characters.find((character) => character.id === id);
            return person ? [{ id: person.id, name: person.name }] : [];
          })}
        />
      ))}
    </div>
  );
}


// ============================ 書き出し ============================

function ExportTab({ projectId, projectName }: { projectId: string; projectName: string }) {
  const [status, setStatus] = useState<api.ExportStatus | null>(null);
  const [result, setResult] = useState<{ shot_count: number; audio_included: boolean; fps: number } | null>(null);
  const [busy, setBusy] = useState(false);
  // 保存は中身を受け取ってから行うので、その間を出す。
  const [saving, setSaving] = useState(false);
  // 保存し終えたことは画面で伝える。黙って終わると、保存できたのか分からない。
  const [saved, setSaved] = useState<string | null>(null);
  // 書き出し済みの動画。作り直すまで残るので、開き直しても取り出せる。
  const [exported, setExported] = useState<api.ProjectExportStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  // 既定はカット動画のfpsに合わせる（null）。前回の値は覚えない——覚えると、作り方の
  // 違う作品へ前の作品の値が持ち越され、コマの間引き・水増しが起きる（H3で作ったカットを、
  // 別の作品で選んだ16fpsのまま書き出すなど）。
  const [fps, setFps] = useState<number | null>(null);
  const cutFps = exported?.cut_fps ?? [];

  const reloadStatus = useCallback(async () => {
    try {
      setStatus(await api.getExportStatus());
      setExported(await api.getProjectExport(projectId));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [projectId]);

  useEffect(() => {
    void reloadStatus();
  }, [reloadStatus]);

  /** 書き出した動画を保存する。名前は後から見て分かる形にし、保存先は選べるようにする。 */
  const saveExported = () =>
    void (async () => {
      setSaving(true);
      setError(null);
      setSaved(null);
      const name = safeFileName([projectName, "全カット"], "mp4");
      try {
        const outcome = await api.saveFile(
          api.exportDownloadUrl(projectId),
          name,
          api.exportSaveAsPath(projectId),
        );
        if (outcome === "saved") setSaved(name);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setSaving(false);
      }
    })();

  return (
    <div>
      {error && <div className="vs-error">{error}</div>}
      <div className="vs-card">
        <h2>1本の動画に書き出し</h2>
        <p className="vs-muted">全カットを物語順につなぎ、1本のmp4として書き出します。</p>
        {/* 作り方を混ぜると大きさが違う（Wan 2.2 は1280x720、MiniMax H3 は1344x768）。
            絵を切り落とさず、余白で揃えることを先に伝える。 */}
        <p className="vs-muted">
          大きさの違うカットが混ざるとき（Wan 2.2 と MiniMax H3 を混ぜたときなど）は、
          いちばん大きいカットに合わせて拡大し、縦横比の違う分は上下か左右に黒い帯を付けます。絵は切れません。
        </p>
        {status?.ffmpeg_available && <div className="vs-ok">書き出せます</div>}
        {/* 押せないままにせず、どこへ行けば揃うかを出す（用意はセットアップで行う）。 */}
        {status && !status.ffmpeg_available && (
          <div className="vs-note warn">
            <strong>1本につなぐには準備が要ります</strong>
            <div>{status.hint}</div>
            <div className="vs-muted" style={{ marginTop: 4 }}>
              つながなくても、カット1本ずつなら「シーン・カット」の画面から保存できます。
            </div>
            <button
              className="vs-btn ghost"
              style={{ marginTop: 8 }}
              onClick={() => void reloadStatus()}
            >
              もう一度確認する
            </button>
          </div>
        )}
        <div className="vs-field" style={{ marginTop: 12, maxWidth: 220 }}>
          <label>フレームレート（fps）</label>
          <select
            className="vs-select"
            value={fps ?? ""}
            onChange={(e) => setFps(e.target.value ? Number(e.target.value) : null)}
          >
            <option value="">
              {cutFps.length
                ? `カットに合わせる（${cutFps[cutFps.length - 1]} fps）`
                : "カットに合わせる"}
            </option>
            {[12, 16, 24, 30].map((value) => (
              <option key={value} value={value}>{value} fps</option>
            ))}
          </select>
          {/* 元のカットと違うfpsで書き出すと、コマの間引き・水増しが起きる。 */}
          {cutFps.length > 1 ? (
            <div className="vs-muted">
              カットによってfpsが違います（{cutFps.join("・")} fps）。合わせるときは高いほうに揃えます。
            </div>
          ) : (
            <div className="vs-muted">
              カットと違う値にすると、コマが間引かれたり水増しされたりします。
            </div>
          )}
        </div>
        <div className="vs-row" style={{ marginTop: 12 }}>
          <button
            className="vs-btn"
            disabled={busy || !status?.ffmpeg_available}
            onClick={() => void (async () => {
              setBusy(true);
              setError(null);
              setSaved(null);
              try {
                const r = await api.exportProject(projectId, fps === null ? {} : { fps });
                setResult({ shot_count: r.shot_count, audio_included: r.audio_included, fps: r.fps });
                setExported(await api.getProjectExport(projectId));
              } catch (e) {
                setError(e instanceof Error ? e.message : String(e));
              } finally {
                setBusy(false);
              }
            })()}
          >
            {busy ? "書き出し中..." : exported?.exists ? "書き出し直す" : "書き出す"}
          </button>
          {/* 書き出した動画はアプリの中に残るので、画面を開き直しても取り出せる
              （「さっき押したかどうか」で消さない）。 */}
          {exported?.exists && (
            <button className="vs-btn ghost" disabled={saving} onClick={saveExported}>
              {saving ? "保存しています..." : "名前を付けて保存"}
            </button>
          )}
        </div>
        {exported?.exists && !result && (
          <div className="vs-muted" style={{ marginTop: 8 }}>
            書き出した動画があります（{formatBytes(exported.size_bytes)}）。
            カットを作り直したときは、書き出し直してください。
          </div>
        )}
        {/* 音が入るのは全カットが音声付き（MiniMax H3）のときだけ。 */}
        {result && (
          <div className="vs-muted" style={{ marginTop: 8 }}>
            {result.shot_count}カットを{result.fps} fps・
            {result.audio_included
              ? "音声付きで書き出しました。"
              : "音声なしで書き出しました（音を入れるには全カットをMiniMax H3で作ります）。"}
          </div>
        )}
        {saved && <p className="vs-ok" style={{ marginTop: 8 }}>「{saved}」を保存しました。</p>}
      </div>

    </div>
  );
}

// ============================ 作品 ============================

// 最後に開いていた作品。次に起動したときも同じ作品から続けられるようにする。
const LAST_PROJECT_KEY = "chotto-sashie.last-project";

type Tab = "project" | "materials" | "stillImages" | "scenes" | "export";

// 作業の並び。サイドバーのメニューと本文はこの一覧を共有する。
const WORK_SECTIONS: { key: Tab; label: string; videoOnly?: boolean }[] = [
  // 作品を選ぶ・作るところが出発点。ここで選んだ作品に対して以降の作業が続く。
  { key: "project", label: "作品" },
  // 登場人物・背景・小物は順番に作るものではないので1つにまとめ、中のタブで行き来する。
  { key: "materials", label: "素材作成" },
  // ここまでが挿絵づくりと動画づくりで共通。静止画が挿絵としての完成品になる。
  { key: "stillImages", label: "静止画" },
  // ここから先は動画を作る作品だけで使う。
  { key: "scenes", label: "シーン・カット", videoOnly: true },
  { key: "export", label: "書き出し", videoOnly: true },
];

type MaterialTab = "characters" | "backgrounds" | "props";

const MATERIAL_TABS: { key: MaterialTab; label: string }[] = [
  { key: "characters", label: "登場人物" },
  { key: "backgrounds", label: "背景" },
  { key: "props", label: "小物" },
];

/**
 * 素材作成: 登場人物・背景・小物をタブで行き来しながら作る。
 *
 * 3つとも常に描いたままにし、見えているタブだけを切り替える。描き直すと、入力中の欄と
 * 進み具合の表示が消え、背景の生成を始めてから人物へ移る、といった並行の作業ができない
 * （生成そのものはGPUの順番待ちに入り、1件ずつ処理される）。
 */
function MaterialsWorkspace({ projectId, hidden }: { projectId: string; hidden: boolean }) {
  const [materialTab, setMaterialTab] = useState<MaterialTab>("characters");
  // 小物は、背景のバリエーションと立ち絵の変更で選ぶ材料になる。
  // 別タブを描いたままにするので、小物が変わったことを知らせて読み直してもらう。
  const [propsRevision, setPropsRevision] = useState(0);
  const bumpPropsRevision = useCallback(() => setPropsRevision((value) => value + 1), []);

  return (
    <div hidden={hidden}>
      {/* 下へ送っても切り替えられるよう、タブは上に留めておく。 */}
      <div className="vs-tabs vs-sticky-tabs" role="tablist" aria-label="作る素材">
        {MATERIAL_TABS.map((item) => (
          <button
            key={item.key}
            type="button"
            role="tab"
            id={`material-tab-${item.key}`}
            aria-selected={materialTab === item.key}
            aria-controls={`material-panel-${item.key}`}
            className={`vs-tab ${materialTab === item.key ? "active" : ""}`}
            onClick={() => setMaterialTab(item.key)}
          >
            {item.label}
          </button>
        ))}
      </div>
      <div
        role="tabpanel"
        id="material-panel-characters"
        aria-labelledby="material-tab-characters"
        hidden={materialTab !== "characters"}
      >
        <CharactersTab projectId={projectId} propsRevision={propsRevision} />
      </div>
      <div
        role="tabpanel"
        id="material-panel-backgrounds"
        aria-labelledby="material-tab-backgrounds"
        hidden={materialTab !== "backgrounds"}
      >
        <AssetsTab
          projectId={projectId}
          kind="background-assets"
          title="背景"
          propsRevision={propsRevision}
        />
      </div>
      <div
        role="tabpanel"
        id="material-panel-props"
        aria-labelledby="material-tab-props"
        hidden={materialTab !== "props"}
      >
        <AssetsTab projectId={projectId} kind="prop-assets" title="小物" onChanged={bumpPropsRevision} />
      </div>
    </div>
  );
}

function ProjectWorkspace({ project, tab }: { project: Project; tab: Tab }) {
  return (
    <div>
      {/* 素材作成は、ほかの画面を開いている間も描いたままにする。静止画を見に行って
          戻っても、生成の進み具合と入力中の内容がそのまま残る。 */}
      <MaterialsWorkspace projectId={project.id} hidden={tab !== "materials"} />
      {tab === "stillImages" && <StillImagesTab projectId={project.id} projectName={project.name} />}
      {tab === "scenes" && <ScenesTab projectId={project.id} projectName={project.name} />}
      {tab === "export" && <ExportTab projectId={project.id} projectName={project.name} />}
    </div>
  );
}

// ============================ ルート ============================

/** デスクトップ版が起動時に知らせてきた事情（ポートの取り合いなど）。 */
const startupError =
  (window as unknown as { __CHOTTO_SASHIE_STARTUP_ERROR__?: string }).__CHOTTO_SASHIE_STARTUP_ERROR__ ??
  null;

export function App() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [selected, setSelected] = useState<Project | null>(null);
  const [error, setError] = useState<string | null>(null);
  // 初回セットアップが未完了ならウィザードを出す。完了後は制作画面のみ。
  const [setupCompleted, setSetupCompleted] = useState<boolean | null>(null);
  // 完了印はあるのに、この端末では設定どおりに動かせない事情。ウィザードへ戻す理由として出す。
  const [environmentGap, setEnvironmentGap] = useState<string | null>(null);
  // 動画も作る設定か。挿絵だけなら、シーン・カットと書き出しのメニューを出さない。
  const [videoEnabled, setVideoEnabled] = useState(true);
  const deletion = useDeletion(setError);
  // 下の欄で編集中の作品名と画風。選び直すたびにその作品の値へ戻す。
  // 新しい作品のときは空から始め、同じ欄をそのまま作成に使う。
  const [name, setName] = useState("");
  const [style, setStyle] = useState("");
  const [saving, setSaving] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  // どの作業を開いているか。サイドバーのメニューと本文で共有する。
  // どの作品を扱うかを決めてから作業へ入る流れなので、最初は「作品」を開く。
  const [tab, setTab] = useState<Tab>("project");

  const refreshSetupState = useCallback(async () => {
    try {
      const config = await api.getSetupConfig();
      setVideoEnabled(config.video_enabled ?? true);
      if (!config.setup_completed) {
        setSetupCompleted(false);
        setEnvironmentGap(null);
        return;
      }
      // 完了印は「どこかで一度ウィザードを終えた」という記録でしかない。保存先を
      // 別の環境から引き継ぐと、この端末に無いComfyUIを指したまま完了扱いになり、
      // 生成しようとして初めて壊れていることが分かる。専用ComfyUIを使う設定なら、
      // 実体がこの端末にあるかまで見てから本編へ入れる。
      if (config.mode === "existing_comfyui") {
        setSetupCompleted(true);
        setEnvironmentGap(null);
        return;
      }
      const comfyui = await api.getComfyuiStatus();
      setSetupCompleted(comfyui.provisioned);
      setEnvironmentGap(
        comfyui.provisioned
          ? null
          : "保存先にあった設定を読み込みましたが、この端末には専用ComfyUIがありません。" +
            "内容を確かめて、「ComfyUI本体の準備」から用意してください。",
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setSetupCompleted(false);
    }
  }, []);

  const reload = useCallback(async () => {
    try {
      const list = await api.listProjects();
      setProjects(list);
      // 前回開いていた作品があればそれを開く（毎回選び直させない）。
      setSelected((current) => {
        if (current) return list.find((item) => item.id === current.id) ?? list[0] ?? null;
        const remembered = window.localStorage.getItem(LAST_PROJECT_KEY);
        return list.find((item) => item.id === remembered) ?? list[0] ?? null;
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  /** 下の欄の内容を保存する。既存の作品なら更新、新しい作品なら作成して選ぶ。 */
  const saveProject = useCallback(async () => {
    const trimmedName = name.trim();
    if (!trimmedName) return;
    setSaving(true);
    try {
      if (selected) {
        const updated = await api.updateProject(selected.id, {
          name: trimmedName,
          illustration_style: style.trim(),
        });
        setProjects((list) => list.map((p) => (p.id === updated.id ? updated : p)));
        setSelected(updated);
      } else {
        const created = await api.createProject({
          name: trimmedName,
          illustration_style: style.trim(),
        });
        setProjects(await api.listProjects());
        setSelected(created);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }, [name, selected, style]);

  useEffect(() => {
    void refreshSetupState();
  }, [refreshSetupState]);

  useEffect(() => {
    if (setupCompleted) void reload();
  }, [setupCompleted, reload]);

  useEffect(() => {
    setName(selected?.name ?? "");
    setStyle(selected?.illustration_style ?? "");
    if (selected) window.localStorage.setItem(LAST_PROJECT_KEY, selected.id);
  }, [selected]);

  // 作品が無ければ、まず作品を作るところから始めてもらう。
  useEffect(() => {
    if (setupCompleted && selected === null) setTab("project");
  }, [setupCompleted, selected]);

  // 出さないことにした画面を開いたままにしない（設定を変えた直後など）。
  const sections = useMemo(
    () => WORK_SECTIONS.filter((section) => videoEnabled || !section.videoOnly),
    [videoEnabled],
  );
  useEffect(() => {
    if (!sections.some((section) => section.key === tab)) setTab("stillImages");
  }, [sections, tab]);

  if (setupCompleted === null) {
    return (
      <div className="vs-shell">
        <div className="vs-muted">読み込んでいます...</div>
      </div>
    );
  }

  if (!setupCompleted) {
    return (
      <div className="vs-shell">
        <div className="vs-head">
          <div>
            <h1>ちょっと挿絵のセットアップ</h1>
            <p className="vs-tagline">物語の挿絵と、その先の数秒を。ぜんぶ、このPCの中だけで。</p>
          </div>
        </div>
        <p className="vs-muted" style={{ marginBottom: 18 }}>
          上から順に進めれば準備が整います。
        </p>
        {environmentGap && (
          <div className="vs-note warn" style={{ marginBottom: 14 }}>
            <strong>この端末での準備が残っています</strong>
            <div>{environmentGap}</div>
          </div>
        )}
        {error && <div className="vs-error">{error}</div>}
        <SetupPanel wizard onCompleted={refreshSetupState} />
      </div>
    );
  }

  return (
    <div className="vs-app">
      {/* 作業の行き先と、いまの生成エンジンの状態を左端にまとめる。 */}
      <aside className="vs-sidebar">
        <div className="vs-brand">ちょっと挿絵</div>
        <nav aria-label="アプリ内メニュー">
          {sections.map((section, index) => (
            <button
              key={section.key}
              type="button"
              className={`vs-nav-item ${tab === section.key ? "active" : ""}`}
              // 作品が無いときでも「作品」へは行ける（そこで作るため）。
              disabled={!selected && section.key !== "project"}
              onClick={() => setTab(section.key)}
            >
              <span>{String(index + 1).padStart(2, "0")}</span>
              {section.label}
            </button>
          ))}
        </nav>
        <EngineStatus />
        <button className="vs-btn ghost vs-sidebar-settings" onClick={() => setSettingsOpen(true)}>
          ⚙️ 設定を確認・変更
        </button>
        <p className="vs-privacy-note">画像と設定は、このPCの中で処理されます。</p>
      </aside>

      <main className="vs-main">
      {/* アプリ名は左端に出ているので、ここでは繰り返さず作品名から始める。 */}

      {/* 起動時に別のAPIと取り違えた等、アプリ本体が知らせたい事情。 */}
      {startupError && <div className="vs-error">{startupError}</div>}
      {error && <div className="vs-error">{error}</div>}

      {(
        <div>
          {/* どの作品を扱っているかは常に見えるようにし、編集は「作品」画面へ集約する。 */}
          <div className="vs-project-context">
            <span className="vs-muted">作品</span>
            <strong>{selected?.name ?? "（新しい作品）"}</strong>
            {selected && (
              <span className="vs-muted">画風: {selected.illustration_style || "未設定"}</span>
            )}
            {tab !== "project" && (
              <button className="vs-btn ghost" onClick={() => setTab("project")}>
                作品を切り替える・設定
              </button>
            )}
          </div>

          {tab === "project" && (
          <div className="vs-card vs-project-bar">
            <h2>作品</h2>
            <p className="vs-muted">
              作品を選ぶと、その作品の登場人物・背景・小物を作り、組み合わせて静止画にできます。
              {videoEnabled && "作った静止画は、シーンとしてカットを足せば動画にできます。"}
            </p>

            {/* 作品を選ぶ。1件でも隠さない（切り替えられることが分かるようにする）。
                最後の1枠は必ず「＋ 新しい作品」で、既存の作品と見た目で区別する。 */}
            <div className="vs-project-list">
              {projects.map((p) => (
                <button
                  key={p.id}
                  type="button"
                  className={`vs-project-item ${selected?.id === p.id ? "selected" : ""}`}
                  onClick={() => setSelected(p)}
                >
                  <strong>{p.name}</strong>
                  <span className="vs-muted">{p.illustration_style || "画風は未設定"}</span>
                  {selected?.id === p.id && <span className="vs-status ok">選択中</span>}
                </button>
              ))}
              {/* 何も選んでいない状態＝新しい作品を入力中。別の印は持たない。 */}
              <button
                type="button"
                className={`vs-project-item add ${selected ? "" : "selected"}`}
                onClick={() => setSelected(null)}
              >
                <strong>＋ 新しい作品</strong>
                <span className="vs-muted">名前と画風を決めて作ります</span>
                {!selected && <span className="vs-status ok">入力中</span>}
              </button>
            </div>

            {/* 下の欄は、いま選んでいる枠のもの。既存なら更新、新しい作品なら作成。 */}
            <div className="vs-project-settings">
              <h3>{selected ? `選択中の作品「${selected.name}」` : "新しい作品"}</h3>
              <div className="vs-field">
                <label>作品名</label>
                <input
                  className="vs-input"
                  placeholder="作品名"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              </div>
              <div className="vs-field vs-project-style">
                <label>この作品全体の画風</label>
                <span className="vs-muted">
                  人物・背景・小物・静止画に共通する画風です。変えると、次に作るものから反映されます。
                </span>
                <input
                  className="vs-input"
                  value={style}
                  onChange={(e) => setStyle(e.target.value)}
                  placeholder="例: 落ち着いた色合い、やわらかな光、繊細な描き込み"
                />
              </div>
              <div className="vs-row vs-project-actions">
                <button
                  className="vs-btn"
                  disabled={saving || !name.trim()}
                  onClick={() => void saveProject()}
                >
                  {saving ? "保存中..." : selected ? "この内容で更新" : "この内容で作成"}
                </button>
                {selected && (
                  <button
                    className="vs-btn danger"
                    onClick={() => void deletion.ask(async () => {
                      const preview = await api.getProjectDeletionPreview(selected.id);
                      const items: string[] = [];
                      if (preview.character_count) items.push(`登場人物 ${preview.character_count}人`);
                      if (preview.background_count) items.push(`背景 ${preview.background_count}件`);
                      if (preview.prop_count) items.push(`小物 ${preview.prop_count}件`);
                      if (preview.still_image_count) items.push(`静止画 ${preview.still_image_count}件`);
                      if (preview.shot_count) items.push(`カット ${preview.shot_count}件`);
                      if (preview.video_count) {
                        items.push(`生成済みの動画 ${preview.video_count}件`);
                      }
                      return {
                        title: `作品「${preview.name}」を削除しますか？`,
                        lead: `「${preview.name}」と、その中で作ったものをすべて削除します。元には戻せません。`,
                        items,
                        // 何が残るのかを言っておく。成果物まで消えると思うと消せなくなる。
                        note:
                          "「名前を付けて保存」で取り出した画像・動画は残ります。"
                          + "削除するのはこのアプリの保存領域だけで、既存のComfyUIや参照中のモデルフォルダには触れません。",
                        perform: async () => {
                          await api.deleteProject(selected.id);
                          const list = await api.listProjects();
                          setProjects(list);
                          setSelected(list[0] ?? null);
                        },
                      };
                    })}
                  >
                    この作品を削除
                  </button>
                )}
              </div>
            </div>

            {/* 制作へ進む口。新しい作品は、作成するまで押せない。 */}
            <div className="vs-project-start">
              <button className="vs-btn" disabled={!selected} onClick={() => setTab("materials")}>
                この作品の制作を始める →
              </button>
              {!selected && (
                <span className="vs-muted">
                  先に「この内容で作成」を押すと、この作品の制作へ進めます。
                </span>
              )}
            </div>
          </div>
          )}


          {/* 作品を開いている間は、「作品」画面にいても作業の画面を描いたままにする
              （素材作成の進み具合を残すため）。作品を切り替えたら作り直す。 */}
          {selected ? (
            <ProjectWorkspace key={selected.id} project={selected} tab={tab} />
          ) : (
            tab !== "project" && (
              <div className="vs-card vs-muted">まず「作品」で作品を作成してください。</div>
            )
          )}
        </div>
      )}
      </main>
      {deletion.dialog}
      {settingsOpen && (
        <SettingsDialog
          onClose={() => setSettingsOpen(false)}
          onReconfigure={async () => {
            setSettingsOpen(false);
            await refreshSetupState();
          }}
        />
      )}
    </div>
  );
}
