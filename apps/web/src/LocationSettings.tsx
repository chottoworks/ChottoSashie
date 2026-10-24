import { useCallback, useEffect, useState } from "react";
import * as api from "./api";
import { formatBytes } from "./format";

/**
 * 保存先（作品と生成物）と、共有フォルダ（ComfyUI本体とモデル）の置き場所。
 *
 * 保存先を変えるときは、**今のデータをどうするか**を必ず選ばせる。黙って新しい場所へ
 * 切り替えると、作品が消えたように見える（実際には元の場所に残っている）。
 * 選べる道は移し先の状態で変わるので、押す前に下見して、できることだけを出す。
 *
 * 共有フォルダは数十GBあるため、変えても中身は移さない。新しい場所に一式があれば
 * それを使い、無ければ取得し直すことになる——それを押す前に伝える。
 */
export function LocationSettings({
  compact = false,
  section = "both",
}: {
  compact?: boolean;
  /**
   * 出す項目。ComfyUIとモデルの置き場所は、ComfyUIの使い方を決めたあとでないと
   * 意味を持たない（既存のものを使うなら関係が無い）。段を分けて出せるようにする。
   */
  section?: "data" | "shared" | "both";
}) {
  const [locations, setLocations] = useState<api.Locations | null>(null);
  const [plan, setPlan] = useState<api.DataDirPlan | null>(null);
  const [mode, setMode] = useState<api.DataDirMode | null>(null);
  const [sharedTarget, setSharedTarget] = useState<string | null>(null);
  const [progress, setProgress] = useState<api.DataDirProgress | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setLocations(await api.getLocations());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // 移している間の途中経過を受け取る。押したまま黙ると、数十GBの複製が止まったのか
  // 進んでいるのか分からない。
  useEffect(() => {
    let stopListening: (() => void) | null = null;
    let gone = false;
    void api.onDataDirProgress(setProgress).then((stop) => {
      if (gone) stop();
      else stopListening = stop;
    });
    return () => {
      gone = true;
      stopListening?.();
    };
  }, []);

  if (!api.isDesktop()) {
    return (
      <p className="vs-muted">
        保存先の変更はデスクトップ版だけで行えます。ブラウザで開いているときは表示のみです。
      </p>
    );
  }
  if (!locations) return <p className="vs-muted">確認中…</p>;

  const fixedByEnvironment = locations.data_dir_source === "environment";
  const sharedFixed = locations.shared_dir_source === "environment";

  const run = (action: () => Promise<void>) =>
    void (async () => {
      setBusy(true);
      setError(null);
      try {
        await action();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(false);
      }
    })();

  const pickDataDir = () =>
    run(async () => {
      const chosen = await api.chooseDirectory("作品の保存先を選ぶ");
      if (!chosen) return;
      const next = await api.planDataDirChange(chosen);
      setPlan(next);
      // 選べるなら「今のデータを移す」を初めに選んでおく。保存先を変えたい人の多くは
      // 今の作品ごと移したいので、ほかの道は自分で選び直してもらう。
      const preferred = next.available_modes.includes("migrate")
        ? "migrate"
        : next.available_modes[0];
      setMode((preferred as api.DataDirMode) ?? null);
    });

  const applyDataDir = () =>
    run(async () => {
      if (!plan || !mode) return;
      setProgress(null);
      // 成功するとアプリが再起動するので、ここから先は戻ってこない。
      await api.applyDataDirChange(plan.target, mode);
    });

  const pickSharedDir = () =>
    run(async () => {
      const chosen = await api.chooseDirectory("ComfyUIとモデルの置き場所を選ぶ");
      if (chosen) setSharedTarget(chosen);
    });

  // 選べない理由は、選べないやり方ごとに同じ文が入っていることがある。1回ずつ出す。
  const blockedReasons = plan ? [...new Set(Object.values(plan.blocked))] : [];

  const showData = section !== "shared";
  const showShared = section !== "data";

  return (
    <div className="vs-locations">
      {error && <div className="vs-error">{error}</div>}

      {showData && (
      <div className="vs-field vs-list-item">
        <label>作品の保存先</label>
        <div className="vs-detail-path" title={locations.data_dir}>{locations.data_dir}</div>
        {!compact && (
          <p className="vs-muted">
            作品・登場人物・生成した画像と動画が入ります。インストール先を分ければ、
            ここも別にできます。
          </p>
        )}
        {fixedByEnvironment ? (
          <p className="vs-muted">
            環境変数 CHOTTO_SASHIE_DATA_DIR で指定されているため、画面からは変えられません。
          </p>
        ) : (
          <div className="vs-row">
            <button className="vs-btn ghost" disabled={busy} onClick={pickDataDir}>
              保存先を変える…
            </button>
          </div>
        )}
      </div>
      )}

      {showData && plan && (
        <div className="vs-note warn">
          <strong>保存先を「{plan.target}」に変えます</strong>
          {plan.requested !== plan.target && (
            <div>
              選んだフォルダにはほかのファイルがあるため、その中に「ChottoSashie」フォルダを
              作って保存先にします。
            </div>
          )}
          <div className="vs-muted">
            いまの保存先: {plan.source}
            {plan.source_facts.has_data
              && `（${plan.source_facts.file_count}件・${formatBytes(plan.source_facts.total_bytes)}）`}
          </div>

          {plan.available_modes.length === 0 ? (
            <div>{blockedReasons[0] ?? "この場所は選べません"}</div>
          ) : (
            <div className="vs-mode-list">
              {plan.available_modes.includes("migrate") && (
                <label className="vs-accept">
                  <input
                    type="radio"
                    checked={mode === "migrate"}
                    onChange={() => setMode("migrate")}
                  />
                  今のデータを移す
                  {!plan.same_drive && "（別のドライブなので、複製してから元を消します）"}
                </label>
              )}
              {plan.available_modes.includes("fresh") && (
                <label className="vs-accept">
                  <input type="radio" checked={mode === "fresh"} onChange={() => setMode("fresh")} />
                  新しく始める（いまのデータは元の場所に残します）
                </label>
              )}
              {plan.available_modes.includes("adopt") && (
                <label className="vs-accept">
                  <input type="radio" checked={mode === "adopt"} onChange={() => setMode("adopt")} />
                  そこにある作品をそのまま使う（どちらのデータも移しません）
                </label>
              )}
            </div>
          )}

          {/* 選べるやり方があるときだけ、選べないものの理由を添える（無いときは上に出した）。 */}
          {plan.available_modes.length > 0
            && blockedReasons.map((reason) => (
              <div key={reason} className="vs-muted">{reason}</div>
            ))}

          <div className="vs-row" style={{ marginTop: 10 }}>
            <button
              className="vs-btn"
              disabled={busy || !mode}
              onClick={applyDataDir}
            >
              {busy ? "処理しています…" : "この内容で変える（アプリを再起動します）"}
            </button>
            <button className="vs-btn ghost" disabled={busy} onClick={() => setPlan(null)}>
              やめる
            </button>
          </div>

          {busy && <MoveProgress progress={progress} />}
        </div>
      )}

      {showShared && (
      <div className="vs-field vs-list-item" style={{ marginTop: 14 }}>
        <label>ComfyUIとモデルの置き場所</label>
        <div className="vs-detail-path" title={locations.shared_dir}>{locations.shared_dir}</div>
        {!compact && (
          <p className="vs-muted">
            数十GBあるため、インストールを分けても<strong>ここは同じ場所を使うのがおすすめ</strong>です。
            分けると、その数だけ取得し直すことになります。
          </p>
        )}
        {sharedFixed ? (
          <p className="vs-muted">
            環境変数 CHOTTO_SASHIE_SHARED_DIR で指定されているため、画面からは変えられません。
          </p>
        ) : (
          <div className="vs-row">
            <button className="vs-btn ghost" disabled={busy} onClick={pickSharedDir}>
              置き場所を変える…
            </button>
          </div>
        )}
      </div>
      )}

      {showShared && sharedTarget && (
        <div className="vs-note warn">
          <strong>置き場所を「{sharedTarget}」に変えます</strong>
          <div>
            中身は移しません。新しい場所にComfyUIとモデルがすでにあればそれを使い、
            無ければ「ComfyUI環境を用意」から取得し直すことになります（数十GB）。
          </div>
          <div className="vs-row" style={{ marginTop: 10 }}>
            <button
              className="vs-btn"
              disabled={busy}
              onClick={() => run(async () => await api.setSharedDir(sharedTarget))}
            >
              {busy ? "処理しています…" : "変える（アプリを再起動します）"}
            </button>
            <button className="vs-btn ghost" disabled={busy} onClick={() => setSharedTarget(null)}>
              やめる
            </button>
          </div>
        </div>
      )}

      {showData && !compact && locations.config_path && (
        <p className="vs-muted" style={{ marginTop: 10 }}>
          この設定はインストール先の {locations.config_path} に保存します。
        </p>
      )}
    </div>
  );
}

/** いまどの段階か（数が分かるのは複製のときだけ）。 */
const PHASE_LABELS: Record<api.DataDirProgress["phase"], string> = {
  scan: "中身を数えています…",
  relink: "保存先を付け替えています…",
  copy: "複製しています…",
  verify: "写したものが元と合っているか確かめています…",
  cleanup: "元の場所を片付けています…",
  rewrite: "記録の中の場所を直しています…",
  done: "まもなくアプリを再起動します…",
};

/**
 * 移している最中の様子。
 *
 * 複製は元の量だけ時間がかかる（数十GBなら何分も）。どこまで来たかと、閉じては
 * いけないことを出す。途中で閉じると、複製し終えていない中途半端な場所が残る。
 */
function MoveProgress({ progress }: { progress: api.DataDirProgress | null }) {
  if (!progress) return <div className="vs-progress-block vs-muted">準備しています…</div>;

  const copying = progress.phase === "copy" && progress.total_bytes > 0;
  const ratio = copying ? Math.min(1, progress.copied_bytes / progress.total_bytes) : 0;

  return (
    <div className="vs-progress-block">
      <div className="vs-muted">
        {PHASE_LABELS[progress.phase]}
        {copying && (
          <>
            {" "}
            {formatBytes(progress.copied_bytes)} / {formatBytes(progress.total_bytes)}
            （{progress.copied_files} / {progress.total_files}件・
            {Math.floor(ratio * 100)}%）
          </>
        )}
      </div>
      {copying && (
        <div className="vs-progress">
          <div style={{ width: `${Math.round(ratio * 100)}%` }} />
        </div>
      )}
      {copying && progress.current && (
        <div className="vs-detail-path vs-muted" title={progress.current}>
          {progress.current}
        </div>
      )}
      <div className="vs-warn">終わるとひとりでに再起動します。それまで閉じないでください。</div>
    </div>
  );
}
