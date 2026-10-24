import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import * as api from "./api";
import { ConfirmDialog } from "./ConfirmDialog";
import { EditableName } from "./EditableName";
import { CompareLightbox, useLightbox } from "./Lightbox";
import { useRememberedSetting } from "./preferences";

/** これ以上進まないジョブの状態。ここに入ったら追いかけるのをやめる。 */
const TERMINAL_JOB_STATES = new Set(["completed", "failed", "cancelled", "interrupted"]);

function isActive(job: api.Job | null): boolean {
  return job !== null && !TERMINAL_JOB_STATES.has(job.state);
}

/** 立ち絵の変更がまだ終わっていないか（順番待ち・変更中）。 */
function isEditActive(edit: api.CharacterFigureEdit): boolean {
  return edit.state === "queued" || edit.state === "running";
}

/** 生成の回を選ぶときの見出し（作った日時）。 */
function batchLabel(batch: api.BatchSummary): string {
  return new Intl.DateTimeFormat("ja-JP", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(batch.created_at));
}

/** 人物プロフィールの入力欄。 */
const PROFILE_FIELDS: Array<{ key: keyof api.CharacterProfile; label: string; hint?: string }> = [
  { key: "gender_age", label: "性別・年齢帯", hint: "性別と、おおよその年齢の幅" },
  { key: "hair_style", label: "髪型", hint: "長さ、前髪、結び方など" },
  { key: "hair_color", label: "髪色", hint: "例：青みのある黒" },
  { key: "eyes", label: "目の特徴", hint: "形、大きさ、まつ毛など" },
  { key: "eye_color", label: "目の色", hint: "例：琥珀色" },
  { key: "face", label: "顔立ち", hint: "輪郭・眉・鼻・口・年齢感を具体的に" },
  { key: "body_type", label: "体格" },
  { key: "base_outfit", label: "基本衣装" },
  { key: "accessories", label: "装飾品", hint: "髪留め、眼鏡、耳飾りなど" },
  { key: "personality", label: "性格" },
  { key: "art_style", label: "この人物だけの雰囲気補正", hint: "任意" },
];

/**
 * 登場人物を作る画面。プロフィールを決め、人物候補から顔（基準画像）を選び、
 * そこから立ち絵を作る。立ち絵がこの人物の実体で、静止画の合成はこれを使う。
 */
export function CharacterStudio({
  character,
  onChanged,
  onClose,
  overviewExtras,
  propsRevision,
}: {
  character: api.Character;
  onChanged: () => void | Promise<void>;
  /** この人物の画面を閉じて、登場人物の一覧へ戻る。 */
  onClose: () => void;
  /** 人物の画面の下に添えるもの（代表写真・削除など、一覧側が持つ操作）。 */
  overviewExtras?: ReactNode;
  /** 小物の一覧が変わるたびに増える。素材作成の別タブで小物を足したときに読み直す。 */
  propsRevision: number;
}) {
  const [profile, setProfile] = useState<api.CharacterProfileRead | null>(null);
  const [candidates, setCandidates] = useState<api.Candidate[]>([]);
  // 立ち絵（この人物の実体）。合成はこの中の1枚を選んで使う。
  const [figures, setFigures] = useState<api.CharacterFigure[]>([]);
  // 枚数・強度は、前回選んだ値から始める（毎回選び直させない）。
  const [candidateCount, setCandidateCount] = useRememberedSetting("candidate_count");
  const [variationStrength, setVariationStrength] = useRememberedSetting("variation_strength");
  // 作り直したときに前の回をどうするか（設定で選ぶ。既定は上書き）。
  const [candidateHistory] = useRememberedSetting("candidate_history");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // 生成の回（バッチ）。作り直しても前の回を選び直せる。null は最新。
  const [candidateBatches, setCandidateBatches] = useState<api.BatchSummary[]>([]);
  const [candidateBatchId, setCandidateBatchId] = useState<string | null>(null);
  // 作り直しの確認（前の回が見えなくなることを伝えてから実行する）。
  const [regenerateCandidates, setRegenerateCandidates] = useState(false);
  // 作り直したときに前の回を残すか（false は上書き＝基準画像以外を消してから作る）。
  const [keepHistory, setKeepHistory] = useState(false);
  // 基準画像に選ばなかった候補の削除確認。
  const [unusedDeletion, setUnusedDeletion] = useState(false);
  // いま開いている画面。人物の画面（手順の一覧）から、手順ごとの画面へ移って作業し、
  // 閉じると人物の画面へ戻る。1つの画面に全部を広げると、どこで終わったのか分からない。
  const [view, setView] = useState<StudioView>("overview");
  // 画面を移ったら先頭から見せる（前の画面の位置のままだと、下の方から始まる）。
  const screenRef = useRef<HTMLDivElement>(null);
  // 保存済みのプロフィール。保存せずに閉じたときは、ここへ戻す。
  const [savedProfile, setSavedProfile] = useState<api.CharacterProfileRead | null>(null);
  // 生成中のジョブ。完了まで追いかけて、終わったら一覧を取り直す。
  const [candidateJob, setCandidateJob] = useState<api.Job | null>(null);
  const [figureJob, setFigureJob] = useState<api.Job | null>(null);
  // 見出しを書き換えている立ち絵（入力中は一覧の取り直しで消さない）。
  const [figureLabelEdit, setFigureLabelEdit] = useState<{ id: string; label: string } | null>(
    null,
  );
  // 削除の確認（実体ごと消えるので、押しただけでは消さない）。
  const [figureDeletion, setFigureDeletion] = useState<api.CharacterFigure | null>(null);
  // 立ち絵の変更: どの立ち絵の変更欄を開いているか（null なら閉じている）と、その指定。
  const [editTargetId, setEditTargetId] = useState<string | null>(null);
  const [editInstruction, setEditInstruction] = useState("");
  const [editPropIds, setEditPropIds] = useState<string[]>([]);
  // 変更の結果を、元の立ち絵と並べて拡大している対象。保存する前に細部まで確かめるため。
  const [compareTarget, setCompareTarget] = useState<{
    figure: api.CharacterFigure;
    label: string;
    edit: api.CharacterFigureEdit;
  } | null>(null);
  // この作品の小物（立ち絵の変更で合わせる）。開いたときと、小物が変わったときに読む。
  const [propAssets, setPropAssets] = useState<api.Asset[]>([]);
  // 履歴（回）の削除確認。
  const [batchDeletion, setBatchDeletion] = useState<api.BatchSummary | null>(null);
  const [deletingBatch, setDeletingBatch] = useState(false);
  const lightbox = useLightbox();
  const candidateActive = isActive(candidateJob);
  const figureActive = isActive(figureJob);

  /** 画像まわりだけを取り直す。生成中の追従で毎回呼ぶので、入力中の欄には触れない。 */
  // 立ち絵の一覧を一度でも読めたか（読む前の空の一覧を「無くなった」と取り違えない）。
  const [figuresLoaded, setFiguresLoaded] = useState(false);

  const reloadImages = useCallback(
    async (candidateBatch: string | null = null) => {
      setError(null);
      try {
        setCandidates(await api.listCandidates(character.id, candidateBatch));
        setFigures(await api.listCharacterFigures(character.id));
        setFiguresLoaded(true);
        setCandidateBatches(await api.listCandidateBatches(character.id));
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [character.id],
  );

  const reload = useCallback(
    async (candidateBatch: string | null = null) => {
      setError(null);
      try {
        // プロフィール欄は編集中のことがあるため、取り直すのは人物を開いたときだけ。
        const loaded = await api.getCharacterProfile(character.id);
        setProfile(loaded);
        setSavedProfile(loaded);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
      await reloadImages(candidateBatch);
    },
    [character.id, reloadImages],
  );

  useEffect(() => {
    void reload();
  }, [reload]);

  // 立ち絵の変更で合わせる小物。人物を開いた時点で読み、
  // 開いたまま「小物」のタブで足したり消したりしたときも読み直す。
  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const assets = await api.listAssets(character.project_id, "prop-assets");
        if (alive) setPropAssets(assets);
      } catch {
        // 読めなくても、指示だけで立ち絵は作れる。ここで画面を止めない。
        if (alive) setPropAssets([]);
      }
    })();
    return () => {
      alive = false;
    };
  }, [character.project_id, propsRevision]);

  // 描き終わってから送る（送り先の高さが決まってから）。
  useEffect(() => {
    screenRef.current?.scrollIntoView({ block: "start" });
  }, [view]);

  // 生成の待ちがある間だけ、ジョブと一覧を取り直し続ける。
  // ジョブを追うだけだと、人物を切り替えて戻ったときに「生成中...」のまま止まるため、
  // 未完成の画像が残っているかどうかも合わせて見る。
  const figuresWaiting =
    figures.some((item) => item.state === "queued" || item.state === "running")
    || figures.some((item) => item.edit !== null && isEditActive(item.edit));
  const waitingImages =
    candidates.some((item) => item.state === "queued" || item.state === "running")
    || figuresWaiting;

  // 出来上がった立ち絵が変わったら、呼び出し元（登場人物の一覧）にも読み直してもらう。
  // 生成は裏で終わるので、ここで追いかけている画像だけを新しくすると、一覧の
  // 「立ち絵の枚数」と代表写真が作る前のまま残り、戻ると「立ち絵がまだありません」と出る。
  // 一覧が数えているのと同じもの（出来上がっていて、画像がある立ち絵）。
  const listedFigures = figures.filter((item) => item.state === "completed" && item.url);
  const readyFigureSignature = listedFigures
    .map((item) => `${item.id}:${item.updated_at}`)
    .join("|");
  const notifiedFigureSignature = useRef<string | null>(null);
  useEffect(() => {
    if (!figuresLoaded) return;
    if (notifiedFigureSignature.current === null) {
      notifiedFigureSignature.current = readyFigureSignature;
      // 開く前に裏で出来上がっていたときは、呼び出し元の枚数がすでに古い。
      if (listedFigures.length !== character.figure_count) void onChanged();
      return;
    }
    if (notifiedFigureSignature.current === readyFigureSignature) return;
    notifiedFigureSignature.current = readyFigureSignature;
    void onChanged();
    // 枚数は署名の変化と一緒に動くので、署名だけを見ればよい。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [figuresLoaded, onChanged, readyFigureSignature]);

  useEffect(() => {
    if (!candidateActive && !figureActive && !waitingImages) return;
    let alive = true;
    const timer = window.setInterval(() => {
      void (async () => {
        try {
          if (candidateJob && candidateActive) {
            const latest = await api.getJob(candidateJob.id);
            if (!alive) return;
            setCandidateJob(latest);
          }
          if (figureJob && figureActive) {
            const latest = await api.getJob(figureJob.id);
            if (!alive) return;
            setFigureJob(latest);
          }
          if (!alive) return;
          await reloadImages(candidateBatchId);
        } catch {
          // 一時的に取得できなくても次の周期で取り直す（画面は前の状態のまま）。
        }
      })();
    }, 1500);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [
    candidateActive,
    candidateBatchId,
    candidateJob,
    figureActive,
    figureJob,
    reloadImages,
    waitingImages,
  ]);

  async function guarded(label: string, action: () => Promise<void>) {
    setBusy(label);
    setError(null);
    setNotice(null);
    try {
      await action();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  function updateProfileField(key: keyof api.CharacterProfile, value: string) {
    setProfile((current) => (current ? { ...current, [key]: value } : current));
  }

  // 基準画像は、いま表示している回に無くても決まっていることがある（前の回から
  // 選んだまま作り直した、など）。有無は回の一覧から読む。
  const hasBase = candidateBatches.some((batch) => batch.has_base);
  const totalCandidates = candidateBatches.reduce((total, batch) => total + batch.image_count, 0);
  // 基準画像に選ばなかった候補。基準を決めた時点で使い道が無くなる。
  const unusedCandidates = hasBase ? totalCandidates - 1 : 0;
  // 合成に使える立ち絵（生成中・失敗は数えない）。
  const readyFigures = figures.filter((figure) => figure.state === "completed");

  /** 表示する生成の回を切り替える（記録は消えないので何度でも往復できる）。 */
  async function switchCandidateBatch(batchJobId: string | null) {
    setCandidateBatchId(batchJobId);
    await reloadImages(batchJobId);
  }

  /** 作り直しの前に、いま表示している回がどうなるかを伝える。 */
  function requestCandidateRegeneration() {
    if (candidates.length === 0) {
      void runCandidateGeneration(true);
      return;
    }
    // 作り直しのたびに選び直せる。初めに選ばれているのは設定の値。
    setKeepHistory(candidateHistory === "keep");
    setRegenerateCandidates(true);
  }

  async function runCandidateGeneration(keep: boolean) {
    await guarded("candidates", async () => {
      const result = await api.generateCandidates(
        character.id,
        candidateCount,
        variationStrength,
        keep,
      );
      setCandidateJob(result.job);
      setNotice(
        keep
          ? "人物候補の生成を開始しました。完了までしばらくかかります"
          : "前の回の候補を削除し、人物候補の生成を開始しました",
      );
      setCandidateBatchId(null);
      await reloadImages(null);
    });
  }

  /** 基準画像から立ち絵を1枚作る。衣装や小物は、出来た1枚を「変更」で変える。 */
  async function generateFigure() {
    await guarded("figures", async () => {
      const result = await api.generateCharacterFigure(character.id);
      setFigureJob(result.job);
      setNotice("立ち絵の生成を開始しました");
      await reloadImages(candidateBatchId);
    });
  }

  async function uploadFigure(file: File) {
    await guarded("figure-upload", async () => {
      await api.uploadCharacterFigure(character.id, file);
      setNotice("立ち絵を登録しました");
      await reloadImages(candidateBatchId);
      await onChanged();
    });
  }

  async function saveFigureLabel(figureId: string, label: string) {
    await guarded("figure-label", async () => {
      await api.updateCharacterFigure(figureId, { label });
      setFigureLabelEdit(null);
      await reloadImages(candidateBatchId);
    });
  }

  async function chooseDefaultFigure(figureId: string) {
    await guarded("figure-default", async () => {
      await api.updateCharacterFigure(figureId, { is_default: true });
      await reloadImages(candidateBatchId);
      await onChanged();
    });
  }

  /** 押した立ち絵の変更欄を開く。前の指定があれば、それを直して頼み直せるよう入れておく。 */
  function openEdit(figure: api.CharacterFigure) {
    setEditTargetId(figure.id);
    setEditInstruction(figure.edit?.instruction ?? "");
    setEditPropIds(figure.edit?.prop_asset_ids ?? []);
  }

  /** 立ち絵の衣装・小物を変える。出来上がりはその立ち絵に預けられ、保存するまで反映しない。 */
  async function startEdit(figure: api.CharacterFigure) {
    await guarded("figure-edit", async () => {
      await api.startCharacterFigureEdit(figure.id, {
        instruction: editInstruction,
        prop_asset_ids: editPropIds,
      });
      setEditTargetId(null);
      setEditInstruction("");
      setEditPropIds([]);
      setNotice("立ち絵の変更を開始しました。出来上がったら、その立ち絵のところで確かめて採用します");
      await reloadImages(candidateBatchId);
    });
  }

  /** 変更の結果を採用し、元の立ち絵を置き換える。 */
  async function adoptEdit(edit: api.CharacterFigureEdit) {
    await guarded("figure-edit-save", async () => {
      await api.saveCharacterFigureEdit(edit.id);
      setCompareTarget(null);
      setNotice("立ち絵を変更後の画像に置き換えました");
      await reloadImages(candidateBatchId);
      await onChanged();
    });
  }

  /** 同じ指示でもう一度作る。いまの結果は新しい結果に入れ替わる。 */
  async function regenerateEdit(figure: api.CharacterFigure, edit: api.CharacterFigureEdit) {
    await guarded("figure-edit", async () => {
      await api.startCharacterFigureEdit(figure.id, {
        instruction: edit.instruction,
        prop_asset_ids: edit.prop_asset_ids,
      });
      setCompareTarget(null);
      setNotice("同じ指示で作り直しています。出来上がったら、その立ち絵のところで確かめます");
      await reloadImages(candidateBatchId);
    });
  }

  /** 立ち絵をそのまま複製する。元を残したまま変えたいときは、複製した方を変更する。 */
  async function duplicateFigure(figure: api.CharacterFigure) {
    await guarded(`figure-duplicate:${figure.id}`, async () => {
      await api.duplicateCharacterFigure(figure.id);
      setNotice("立ち絵を複製しました。元を残したまま変えたいときは、複製した方を「変更」します");
      await reloadImages(candidateBatchId);
      await onChanged();
    });
  }

  /** 立ち絵を左右の隣と入れ替える。誰も既定を選んでいなければ、先頭の1枚が既定になる。 */
  async function moveFigure(figure: api.CharacterFigure, offset: -1 | 1) {
    const index = figures.findIndex((item) => item.id === figure.id);
    const target = index + offset;
    if (index < 0 || target < 0 || target >= figures.length) return;
    const order = figures.map((item) => item.id);
    [order[index], order[target]] = [order[target], order[index]];
    await guarded("figure-move", async () => {
      try {
        setFigures(await api.reorderCharacterFigures(character.id, order));
      } catch (e) {
        // 見ている一覧が古かったときは、読み直してから並べ替えてもらう。
        setFigures(await api.listCharacterFigures(character.id));
        throw e;
      }
    });
  }

  async function discardEdit(edit: api.CharacterFigureEdit) {
    await guarded("figure-edit-discard", async () => {
      await api.discardCharacterFigureEdit(edit.id);
      setNotice("変更した立ち絵を破棄しました（元の立ち絵はそのままです）");
      await reloadImages(candidateBatchId);
    });
  }

  async function cancelEdit(edit: api.CharacterFigureEdit) {
    await guarded("figure-edit-cancel", async () => {
      await api.cancelJob(edit.job_id);
      setNotice("立ち絵の変更を中止しました");
      await reloadImages(candidateBatchId);
    });
  }

  function toggleEditProp(propId: string) {
    setEditPropIds((current) =>
      current.includes(propId)
        ? current.filter((value) => value !== propId)
        : [...current, propId],
    );
  }

  async function removeFigure(figure: api.CharacterFigure) {
    await guarded("figure-delete", async () => {
      await api.deleteCharacterFigure(figure.id);
      setFigureDeletion(null);
      setNotice("立ち絵を削除しました");
      await reloadImages(candidateBatchId);
      await onChanged();
    });
  }

  /** 基準画像に選ばなかった候補を消す。基準を決めた時点で、ほかの候補は使い道が無い。 */
  async function removeUnusedCandidates() {
    await guarded("unused-candidates", async () => {
      const result = await api.deleteUnusedCandidates(character.id);
      setUnusedDeletion(false);
      setCandidateBatchId(null);
      setNotice(`選ばなかった人物候補 ${result.deleted_count}枚を削除しました`);
      await reloadImages(null);
      await onChanged();
    });
  }

  /** いま表示している回（未選択なら最新）。履歴の削除対象になる。 */
  function shownBatch(): api.BatchSummary | null {
    if (!candidateBatches.length) return null;
    return (
      candidateBatches.find((batch) => batch.batch_job_id === candidateBatchId)
      ?? candidateBatches[0]
    );
  }

  async function confirmBatchDeletion() {
    if (!batchDeletion) return;
    setDeletingBatch(true);
    setError(null);
    try {
      const result = await api.deleteCandidateBatch(character.id, batchDeletion.batch_job_id);
      setBatchDeletion(null);
      // 消した回を表示していたので、表示は最新へ戻す。
      setCandidateBatchId(null);
      setNotice(`人物候補の履歴 ${result.deleted_count}枚を削除しました`);
      await reloadImages(null);
      await onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setDeletingBatch(false);
    }
  }

  // 見出しの無い立ち絵は、出来上がっている分の並びで「立ち絵1」「立ち絵2」…と呼ぶ
  // （静止画の立ち絵の選択と同じ数え方。生成中・失敗の枠は数えない）。
  const figureLabelOf = (figure: api.CharacterFigure) => {
    const position = listedFigures.findIndex((item) => item.id === figure.id);
    return figure.label || (position >= 0 ? `立ち絵${position + 1}` : "立ち絵");
  };

  const figureSrc = (figure: api.CharacterFigure) =>
    `${api.characterFigureUrl(figure.id)}?v=${encodeURIComponent(figure.updated_at)}`;
  /** 出来上がった立ち絵を、一覧の並びのまま拡大する（前後へ送れる）。 */
  function openFigureGallery(figureId: string) {
    const shown = figures.filter((figure) => figure.state === "completed" && figure.url);
    lightbox.openGallery(
      shown.map((figure) => ({
        src: figureSrc(figure),
        caption: figureLabelOf(figure),
      })),
      shown.findIndex((figure) => figure.id === figureId),
    );
  }

  /** 人物候補を、いま表示している回の並びのまま拡大する（基準を選ぶ前に見比べる）。 */
  function openCandidateGallery(candidateId: string) {
    const shown = candidates.filter((candidate) => candidate.state === "completed");
    lightbox.openGallery(
      shown.map((candidate) => ({
        src: `${api.candidateImageUrl(candidate.id)}?t=${candidate.id}`,
        caption: `候補${candidate.ordinal}${candidate.is_base ? "（基準画像）" : ""}`,
      })),
      shown.findIndex((candidate) => candidate.id === candidateId),
    );
  }

  const editSrc = (edit: api.CharacterFigureEdit) =>
    `${api.characterFigureEditUrl(edit.id)}?v=${encodeURIComponent(edit.updated_at)}`;

  // プロフィールに何か書いてあるか（人物候補の土台になる）。
  const filledProfileFields = savedProfile
    ? PROFILE_FIELDS.filter((field) => String(savedProfile[field.key] ?? "").trim()).length
    : 0;
  // 次にやること。終わっている手順を飛ばして、残りの最初の1つを示す。
  // 手持ちの全身画像を登録するなら、人物候補を経ずに立ち絵へ進んでもよい。
  const nextStep: StudioView | null = readyFigures.length > 0
    ? null
    : hasBase
      ? "figures"
      : filledProfileFields > 0 || totalCandidates > 0
        ? "candidates"
        : "profile";
  const baseCandidate = candidates.find((candidate) => candidate.is_base) ?? null;

  /** 手順の画面を閉じて、人物の画面へ戻る。 */
  function backToOverview() {
    setNotice(null);
    setError(null);
    setView("overview");
  }

  /** プロフィールを保存して、人物の画面へ戻る（次の手順はそこで示す）。 */
  async function saveProfileAndClose() {
    if (!profile) return;
    await guarded("profile", async () => {
      const saved = await api.updateCharacterProfile(character.id, {
        ...profile,
        immutable_features: profile.immutable_features
          .map((value) => value.trim())
          .filter(Boolean),
      });
      setProfile(saved);
      setSavedProfile(saved);
      await onChanged();
      setView("overview");
      setNotice("プロフィールを保存しました");
    });
  }

  /** 保存せずに閉じる。書きかけの内容は捨て、保存済みの内容へ戻す。 */
  function discardProfileAndClose() {
    setProfile(savedProfile);
    backToOverview();
  }

  const stepRows: Array<{
    view: Exclude<StudioView, "overview">;
    index: number;
    title: string;
    status: ReactNode;
    summary: ReactNode;
  }> = [
    {
      view: "profile",
      index: 1,
      title: "プロフィール",
      status: (
        <span className={`vs-status ${filledProfileFields > 0 ? "ok" : "idle"}`}>
          {filledProfileFields > 0 ? `${filledProfileFields}項目を入力済み` : "未入力"}
        </span>
      ),
      summary: "見た目と性格を書きます。人物候補はここから作ります。",
    },
    {
      view: "candidates",
      index: 2,
      title: "人物候補から基準画像を選ぶ",
      status: (
        <span className={`vs-status ${hasBase ? "ok" : "warn"}`}>
          {hasBase ? "選択済み" : "未選択"}
        </span>
      ),
      summary: baseCandidate?.state === "completed" ? (
        <img
          className="vs-step-thumb vs-zoomable"
          onClick={() => openCandidateGallery(baseCandidate.id)}
          src={`${api.candidateImageUrl(baseCandidate.id)}?t=${baseCandidate.id}`}
          alt="基準画像"
        />
      ) : (
        "人物候補を何枚か作り、顔を決める1枚（基準画像）を選びます。"
      ),
    },
    {
      view: "figures",
      index: 3,
      title: "立ち絵を作る",
      status: (
        <span className={`vs-status ${readyFigures.length > 0 ? "ok" : "warn"}`}>
          {readyFigures.length > 0 ? `${readyFigures.length}枚` : "未作成"}
        </span>
      ),
      summary: readyFigures.length > 0 ? (
        <div className="vs-step-thumbs">
          {readyFigures.slice(0, 6).map((figure) => (
            <img
              key={figure.id}
              className="vs-step-thumb vs-zoomable"
              src={figureSrc(figure)}
              alt={figure.label || "立ち絵"}
              onClick={() => openFigureGallery(figure.id)}
            />
          ))}
        </div>
      ) : (
        "基準画像から全身の1枚（立ち絵）を作ります。手持ちの全身画像を登録するだけでも構いません。"
      ),
    },
  ];

  return (
    <div ref={screenRef} className="vs-character-screen">
      {view === "overview" && (
        <div className="vs-card">
          <div className="vs-screen-head">
            <button className="vs-btn ghost" onClick={onClose}>
              ← 登場人物の一覧へ
            </button>
            <EditableName
              as="h2"
              name={character.name}
              onSave={async (name) => {
                await api.updateCharacter(character.id, { name });
                await onChanged();
              }}
            />
            <span className={`vs-status ${readyFigures.length > 0 ? "ok" : "warn"}`}>
              {readyFigures.length > 0 ? "静止画に出せます" : "立ち絵がまだありません"}
            </span>
          </div>
          {error && <div className="vs-error">{error}</div>}
          {notice && <div className="vs-note">{notice}</div>}
          <p className="vs-muted">
            {nextStep === null
              ? "立ち絵ができています。静止画でこの人物を使えます。衣装や小物を変えるときは「立ち絵を作る」から変更します。"
              : `次は「${stepRows.find((row) => row.view === nextStep)?.title}」です。`}
          </p>
          <div className="vs-step-rows">
            {stepRows.map((row) => (
              <div key={row.view} className={`vs-step-row ${nextStep === row.view ? "next" : ""}`}>
                <span className="vs-substep-index">{row.index}</span>
                <div className="vs-step-row-body">
                  <div className="vs-row">
                    <strong>{row.title}</strong>
                    {row.status}
                    {/* 生成中の印。画面を閉じていても、ここで進み具合が分かる。 */}
                    {row.view === "candidates" && candidateActive && (
                      <span className="vs-status warn">生成中</span>
                    )}
                    {row.view === "figures" && (figureActive || figuresWaiting) && (
                      <span className="vs-status warn">生成中</span>
                    )}
                  </div>
                  <div className="vs-muted">{row.summary}</div>
                </div>
                <button
                  className={`vs-btn ${nextStep === row.view ? "" : "ghost"}`}
                  onClick={() => {
                    setNotice(null);
                    setError(null);
                    setView(row.view);
                  }}
                >
                  {row.view === "profile" ? "編集する" : "開く"}
                </button>
              </div>
            ))}
          </div>
          {overviewExtras}
        </div>
      )}

      {view === "profile" && profile && (
        <div className="vs-card">
          <div className="vs-screen-head">
            <button className="vs-btn ghost" disabled={busy !== null} onClick={discardProfileAndClose}>
              ← {character.name}
            </button>
            <h2>プロフィール</h2>
          </div>
          <p className="vs-muted">
            見た目と性格を書きます。人物候補はこの内容から作ります。書けるところだけで構いません。
          </p>
          {error && <div className="vs-error">{error}</div>}
          <div className="vs-profile-grid">
            {PROFILE_FIELDS.map((field) => (
              <div className="vs-field" key={field.key}>
                <label>
                  {field.label}
                  {field.hint && <span className="vs-muted"> — {field.hint}</span>}
                </label>
                <input
                  className="vs-input"
                  value={String(profile[field.key] ?? "")}
                  onChange={(e) => updateProfileField(field.key, e.target.value)}
                />
              </div>
            ))}
          </div>
          <div className="vs-field">
            <label>絶対に変えない特徴（1行に1つ）</label>
            <textarea
              className="vs-textarea"
              value={profile.immutable_features.join("\n")}
              onChange={(e) =>
                setProfile({
                  ...profile,
                  immutable_features: e.target.value.split("\n"),
                })
              }
              placeholder={"左目の下の小さなほくろ\n右手首の細い傷あと"}
            />
          </div>
          <div className="vs-screen-footer">
            <button className="vs-btn" disabled={busy !== null} onClick={() => void saveProfileAndClose()}>
              {busy === "profile" ? "保存中..." : "保存して閉じる"}
            </button>
            <button className="vs-btn ghost" disabled={busy !== null} onClick={discardProfileAndClose}>
              保存せずに閉じる
            </button>
          </div>
        </div>
      )}

      {view === "candidates" && (
        <div className="vs-card">
          <div className="vs-screen-head">
            <button className="vs-btn ghost" onClick={backToOverview}>
              ← {character.name}
            </button>
            <h2>人物候補から基準画像を選ぶ</h2>
            <span className={`vs-status ${hasBase ? "ok" : "warn"}`}>
              {hasBase ? "選択済み" : "未選択"}
            </span>
          </div>
          <p className="vs-muted">
            プロフィールから人物候補を作り、顔を決める1枚を「これを基準にする」で選びます。
            立ち絵はこの基準画像から作ります。
          </p>
          {error && <div className="vs-error">{error}</div>}
          {notice && <div className="vs-note">{notice}</div>}
          <div className="vs-row">
            <label className="vs-muted">枚数</label>
            <select
              className="vs-select"
              style={{ maxWidth: 90 }}
              value={candidateCount}
              onChange={(e) => setCandidateCount(Number(e.target.value))}
            >
              {[8, 10, 12].map((value) => (
                <option key={value} value={value}>{value}</option>
              ))}
            </select>
            <label className="vs-muted" title="低いと画風に忠実で穏やか、高いと顔立ちと候補ごとの差が強く出ます。">
              変化強度 {variationStrength.toFixed(1)}
            </label>
            <input
              type="range"
              min={2}
              max={3.5}
              step={0.1}
              value={variationStrength}
              onChange={(e) => setVariationStrength(Number(e.target.value))}
            />
            <button
              className="vs-btn"
              disabled={busy !== null || candidateActive}
              onClick={() => requestCandidateRegeneration()}
            >
              {busy === "candidates" || candidateActive
                ? "生成中..."
                : candidates.length
                  ? "候補を作り直す"
                  : "人物候補を生成"}
            </button>
          </div>
          {candidateJob && (
            <div className="vs-banner warn">
              <strong>{candidateJob.message ?? "人物候補を生成しています"}</strong>
              <span>{Math.round(candidateJob.progress * 100)}%</span>
              {candidateActive && (
                <button
                  className="vs-btn ghost vs-cancel"
                  disabled={busy !== null}
                  onClick={() => void guarded("cancel-candidates", async () => {
                    setCandidateJob(await api.cancelJob(candidateJob.id));
                    setNotice("人物候補の生成を中止しました");
                  })}
                >
                  {busy === "cancel-candidates" ? "中止しています…" : "中止する"}
                </button>
              )}
            </div>
          )}
          {candidateBatches.length > 0 && (
            <div className="vs-field">
              <label>表示する回</label>
              <div className="vs-row">
                <select
                  className="vs-select"
                  value={candidateBatchId ?? ""}
                  onChange={(e) => void switchCandidateBatch(e.target.value || null)}
                >
                  <option value="">最新（{candidateBatches[0].image_count}枚）</option>
                  {candidateBatches.slice(1).map((batch) => (
                    <option key={batch.batch_job_id} value={batch.batch_job_id}>
                      {batchLabel(batch)}（{batch.image_count}枚{batch.has_base ? "・基準画像あり" : ""}）
                    </option>
                  ))}
                </select>
                <button
                  className="vs-btn ghost"
                  disabled={busy !== null || candidateActive || !shownBatch()}
                  onClick={() => {
                    const batch = shownBatch();
                    if (batch) setBatchDeletion(batch);
                  }}
                >
                  この回を削除
                </button>
              </div>
              <span className="vs-muted">
                履歴として残した回は、ここから見返して基準画像を選び直せます。
                いらない回は「この回を削除」で画像ごと消せます。
              </span>
            </div>
          )}
          {/* 基準を決めたら、ほかの候補は使い道が無い。その場で片付けられるようにする。 */}
          {unusedCandidates > 0 && !candidateActive && (
            <div className="vs-note">
              基準画像に選ばなかった候補が{unusedCandidates}枚あります。
              <button
                className="vs-btn ghost"
                style={{ marginLeft: 8 }}
                disabled={busy !== null}
                onClick={() => setUnusedDeletion(true)}
              >
                選ばなかった候補を削除
              </button>
            </div>
          )}
          {candidates.length > 0 && (
            <div className="vs-candidate-grid">
              {candidates.map((candidate) => (
                <div
                  key={candidate.id}
                  className={`vs-candidate ${candidate.is_base ? "selected" : ""}`}
                >
                  {candidate.state === "completed" ? (
                    <img
                      className="vs-zoomable"
                      src={`${api.candidateImageUrl(candidate.id)}?t=${candidate.id}`}
                      alt={`候補${candidate.ordinal}`}
                      onClick={() => openCandidateGallery(candidate.id)}
                    />
                  ) : (
                    <div className="vs-candidate-placeholder">
                      {candidate.state === "failed" ? "失敗" : "生成中..."}
                    </div>
                  )}
                  <button
                    className={`vs-btn ${candidate.is_base ? "" : "ghost"}`}
                    disabled={busy !== null || candidate.state !== "completed" || candidate.is_base}
                    onClick={() => void guarded("base", async () => {
                      await api.selectBaseCandidate(candidate.id);
                      await reloadImages(candidateBatchId);
                      setNotice("基準画像を決めました。次は、この基準画像から立ち絵を作ります");
                    })}
                  >
                    {candidate.is_base ? "基準画像" : "これを基準にする"}
                  </button>
                </div>
              ))}
            </div>
          )}
          <div className="vs-screen-footer">
            {hasBase ? (
              <>
                <button className="vs-btn" onClick={() => { setNotice(null); setView("figures"); }}>
                  立ち絵を作るへ進む
                </button>
                <button className="vs-btn ghost" onClick={backToOverview}>
                  閉じる
                </button>
              </>
            ) : (
              <button className="vs-btn" onClick={backToOverview}>
                閉じる
              </button>
            )}
          </div>
        </div>
      )}

      {view === "figures" && (
        <div className="vs-card">
          <div className="vs-screen-head">
            <button className="vs-btn ghost" onClick={backToOverview}>
              ← {character.name}
            </button>
            <h2>立ち絵を作る</h2>
            <span className={`vs-status ${readyFigures.length > 0 ? "ok" : "warn"}`}>
              {readyFigures.length > 0 ? `${readyFigures.length}枚` : "未作成"}
            </span>
          </div>
          {error && <div className="vs-error">{error}</div>}
          {notice && <div className="vs-note">{notice}</div>}
          <p className="vs-muted">
            基準画像から、背景の無い全身の1枚（立ち絵）を作ります。静止画は、ここで作った
            立ち絵と背景を合成して作り、姿勢や表情はそのときに指定します。
            衣装や小物は、出来た立ち絵の<strong>「変更」</strong>で変えられます。出来上がりを
            見て採用すると、その立ち絵が置き換わります。元の立ち絵も残したいときは、先に
            <strong>「複製」</strong>してから、複製した方を変更します。
            立ち絵が複数あるときは、静止画ごとにどれを使うかを選べます。
          </p>
          <div className="vs-row">
            <button
              className="vs-btn"
              disabled={busy !== null || figureActive || !hasBase}
              onClick={() => void generateFigure()}
              title={hasBase ? undefined : "先に基準画像を選んでください"}
            >
              {busy === "figures" || figureActive ? "生成中..." : "立ち絵を作る"}
            </button>
            <label className="vs-btn ghost">
              手持ちの画像を登録
              <input
                type="file"
                accept="image/png,image/jpeg,image/webp"
                style={{ display: "none" }}
                disabled={busy !== null}
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  e.target.value = "";
                  if (file) void uploadFigure(file);
                }}
              />
            </label>
          </div>
          {figureJob && (
            <div className="vs-banner warn">
              <strong>{figureJob.message ?? "立ち絵を作っています"}</strong>
              <span>{Math.round(figureJob.progress * 100)}%</span>
              {figureActive && (
                <button
                  className="vs-btn ghost vs-cancel"
                  disabled={busy !== null}
                  onClick={() => void guarded("cancel-figures", async () => {
                    setFigureJob(await api.cancelJob(figureJob.id));
                    setNotice("立ち絵の生成を中止しました");
                  })}
                >
                  {busy === "cancel-figures" ? "中止しています…" : "中止する"}
                </button>
              )}
            </div>
          )}
          {figures.length > 0 && (
            <div className="vs-candidate-grid">
              {figures.map((figure, index) => {
                const edit = figure.edit;
                const editing = editTargetId === figure.id;
                const editBusy = edit !== null && isEditActive(edit);
                // 変更欄や変更の結果があるときは、その立ち絵を横幅いっぱいに広げて隣に出す。
                // どの立ち絵への変更なのかを、離れた場所の枠で探させないため。
                const wide = editing || edit !== null;
                return (
                  <div
                    key={figure.id}
                    className={`vs-candidate ${figure.is_default ? "selected" : ""} ${wide ? "vs-figure-wide" : ""}`}
                  >
                    <div className="vs-figure-main">
                      {figure.state === "completed" && figure.url ? (
                        <img
                          className="vs-zoomable"
                          src={figureSrc(figure)}
                          alt={figureLabelOf(figure)}
                          onClick={() => openFigureGallery(figure.id)}
                        />
                      ) : (
                        <div className="vs-candidate-placeholder">
                          {figure.state === "failed"
                            ? (figure.error_message || "失敗")
                            : "生成中..."}
                        </div>
                      )}
                      <div className="vs-figure-order">
                        <button
                          className="vs-btn ghost"
                          disabled={busy !== null || index === 0}
                          onClick={() => void moveFigure(figure, -1)}
                          title="左へ移す"
                          aria-label={`${figureLabelOf(figure)}を左へ移す`}
                        >
                          ←
                        </button>
                        <button
                          className="vs-btn ghost"
                          disabled={busy !== null || index === figures.length - 1}
                          onClick={() => void moveFigure(figure, 1)}
                          title="右へ移す"
                          aria-label={`${figureLabelOf(figure)}を右へ移す`}
                        >
                          →
                        </button>
                      </div>
                      {figureLabelEdit?.id === figure.id ? (
                        <div className="vs-row">
                          <input
                            className="vs-input"
                            value={figureLabelEdit.label}
                            placeholder="例: 制服"
                            autoFocus
                            onChange={(e) => setFigureLabelEdit({ id: figure.id, label: e.target.value })}
                            onKeyDown={(e) => {
                              if (e.key === "Enter") void saveFigureLabel(figure.id, figureLabelEdit.label);
                              if (e.key === "Escape") setFigureLabelEdit(null);
                            }}
                          />
                          <button
                            className="vs-btn"
                            disabled={busy !== null}
                            onClick={() => void saveFigureLabel(figure.id, figureLabelEdit.label)}
                          >
                            保存
                          </button>
                        </div>
                      ) : (
                        <button
                          className="vs-btn ghost"
                          disabled={busy !== null}
                          onClick={() => setFigureLabelEdit({ id: figure.id, label: figure.label })}
                        >
                          {figure.label || "見出しを付ける"}
                        </button>
                      )}
                      <div className="vs-figure-actions">
                        <button
                          className={`vs-btn ${figure.is_default ? "" : "ghost"}`}
                          disabled={busy !== null || figure.state !== "completed" || figure.is_default}
                          aria-pressed={figure.is_default}
                          onClick={() => void chooseDefaultFigure(figure.id)}
                          title={figure.is_default
                            ? "静止画で立ち絵を選ばなかったときは、この立ち絵を使います"
                            : "静止画で立ち絵を選ばなかったときに、この立ち絵を使うようにします"}
                        >
                          既定
                        </button>
                        <button
                          className={`vs-btn ${editing ? "" : "ghost"}`}
                          disabled={busy !== null || figure.state !== "completed" || editBusy}
                          onClick={() => (editing ? setEditTargetId(null) : openEdit(figure))}
                          title="この立ち絵の衣装や持ち物を変えます"
                        >
                          変更
                        </button>
                        <button
                          className="vs-btn ghost"
                          disabled={busy !== null || figure.state !== "completed"}
                          onClick={() => void duplicateFigure(figure)}
                          title="この立ち絵をそのまま複製して、1枚増やします"
                        >
                          {busy === `figure-duplicate:${figure.id}` ? "複製しています…" : "複製"}
                        </button>
                        <button
                          className="vs-btn ghost"
                          disabled={
                            busy !== null
                            || figure.state === "queued"
                            || figure.state === "running"
                            || editBusy
                          }
                          onClick={() => setFigureDeletion(figure)}
                        >
                          削除
                        </button>
                      </div>
                    </div>

                    {editing && (
                      <div className="vs-figure-edit">
                        <div className="vs-state-title">
                          {figureLabelOf(figure)}の衣装・小物を変更
                        </div>
                        <p className="vs-muted">
                          顔・髪・体格・画風はこの立ち絵のまま、衣装と持ち物だけを変えます。姿勢は
                          変えません（姿勢は静止画を作るときに指定します）。出来上がりを見て採用すると、
                          この立ち絵が置き換わります。元も残したいときは、先に「複製」してください。
                        </p>
                        <div className="vs-field">
                          <label>変えたいこと</label>
                          <input
                            className="vs-input"
                            value={editInstruction}
                            placeholder="例: 制服をパーカーとジーンズに変える"
                            onChange={(e) => setEditInstruction(e.target.value)}
                          />
                        </div>
                        <div className="vs-field">
                          <label>合わせる小物（任意・2つまで）</label>
                          <div className="vs-row">
                            {propAssets.length === 0 && (
                              <span className="vs-muted">
                                まだ小物がありません。「小物」タブで登録すると、ここで選べます。
                              </span>
                            )}
                            {propAssets.map((prop) => {
                              const picked = editPropIds.includes(prop.id);
                              const full = !picked && editPropIds.length >= 2;
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
                                    onChange={() => toggleEditProp(prop.id)}
                                  /> {prop.name}
                                </label>
                              );
                            })}
                          </div>
                        </div>
                        {edit !== null && (
                          <p className="vs-muted">
                            まだ採用していない変更の結果は、変更し直すと破棄されます。
                          </p>
                        )}
                        <div className="vs-state-actions">
                          <button
                            className="vs-btn"
                            disabled={
                              busy !== null
                              || (!editInstruction.trim() && editPropIds.length === 0)
                            }
                            onClick={() => void startEdit(figure)}
                          >
                            {busy === "figure-edit" ? "開始しています…" : "変更する"}
                          </button>
                          <button
                            className="vs-btn ghost"
                            disabled={busy !== null}
                            onClick={() => setEditTargetId(null)}
                          >
                            やめる
                          </button>
                          <span className="vs-muted">
                            指示の反映を優先して作るので、立ち絵を作るときより時間がかかります。
                          </span>
                        </div>
                      </div>
                    )}

                    {!editing && edit !== null && (
                      <div className="vs-figure-edit">
                        <div className="vs-state-title">変更した立ち絵</div>
                        {edit.instruction && (
                          <p className="vs-muted">変えたこと: {edit.instruction}</p>
                        )}
                        {editBusy && (
                          <>
                            <div className="vs-candidate-placeholder vs-figure-edit-image">
                              {edit.state === "queued" ? "順番待ち..." : "変更しています..."}
                            </div>
                            <div className="vs-state-actions">
                              <button
                                className="vs-btn ghost vs-cancel"
                                disabled={busy !== null || !edit.job_id}
                                onClick={() => void cancelEdit(edit)}
                              >
                                {busy === "figure-edit-cancel" ? "中止しています…" : "中止する"}
                              </button>
                            </div>
                          </>
                        )}
                        {edit.state === "failed" && (
                          <>
                            <div className="vs-error">
                              {edit.error_message || "立ち絵を変更できませんでした"}
                            </div>
                            <div className="vs-state-actions">
                              <button
                                className="vs-btn"
                                disabled={busy !== null}
                                onClick={() => openEdit(figure)}
                              >
                                やり直す
                              </button>
                              <button
                                className="vs-btn ghost"
                                disabled={busy !== null}
                                onClick={() => void discardEdit(edit)}
                              >
                                破棄
                              </button>
                            </div>
                          </>
                        )}
                        {edit.state === "completed" && edit.url && (
                          <>
                            <img
                              className="vs-figure-edit-image vs-zoomable"
                              src={editSrc(edit)}
                              alt="変更した立ち絵"
                              title="元の立ち絵と並べて拡大します"
                              onClick={() => setCompareTarget({ figure, label: figureLabelOf(figure), edit })}
                            />
                            <button
                              className="vs-btn ghost"
                              onClick={() => setCompareTarget({ figure, label: figureLabelOf(figure), edit })}
                            >
                              拡大して比べる
                            </button>
                            <span className="vs-muted">
                              採用すると、この立ち絵が変更後の画像に置き換わります（元の画像は残りません）。
                              この立ち絵を使っている静止画は、次に合成し直したときから変更後の姿になります。
                            </span>
                            <div className="vs-state-actions">
                              <button
                                className="vs-btn"
                                disabled={busy !== null}
                                onClick={() => void adoptEdit(edit)}
                              >
                                {busy === "figure-edit-save" ? "採用しています…" : "採用する"}
                              </button>
                              <button
                                className="vs-btn ghost"
                                disabled={busy !== null}
                                onClick={() => void regenerateEdit(figure, edit)}
                                title="同じ指示でもう一度作ります"
                              >
                                {busy === "figure-edit" ? "開始しています…" : "再生成する"}
                              </button>
                              <button
                                className="vs-btn ghost"
                                disabled={busy !== null}
                                onClick={() => void discardEdit(edit)}
                                title="変更の結果を捨てます（元の立ち絵はそのまま）"
                              >
                                やめる
                              </button>
                            </div>
                          </>
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
          <div className="vs-screen-footer">
            <button className="vs-btn" onClick={backToOverview}>
              閉じる
            </button>
          </div>
        </div>
      )}

      {figureDeletion && (
        <ConfirmDialog
          title="この立ち絵を削除しますか？"
          lead={`${figureDeletion.label || "立ち絵"}を、画像ごと削除します。元には戻せません。`}
          items={[
            "この立ち絵の画像",
            ...(figureDeletion.is_default
              ? ["既定の立ち絵の指定（残りの先頭の1枚が既定になります）"]
              : []),
          ]}
          note="この立ち絵を選んでいる静止画は消えません。合成し直すときに選び直してください。"
          confirmLabel="削除する"
          busy={busy === "figure-delete"}
          onConfirm={() => void removeFigure(figureDeletion)}
          onCancel={() => setFigureDeletion(null)}
        />
      )}
      {regenerateCandidates && (
        <ConfirmDialog
          title="人物候補を作り直しますか？"
          lead={`${candidateCount}枚を作り直します。画面の表示は新しい回に切り替わります。`}
          items={keepHistory
            ? [
                `いま表示中の候補 ${candidates.length}枚は「表示する回」から見返せます`,
                ...(hasBase ? ["基準画像の選択は、新しい回から選び直すまでそのまま残ります"] : []),
              ]
            : [
                `これまでの人物候補${hasBase ? "のうち、基準画像以外の" : " "}${unusedCandidates || totalCandidates}枚と、その画像`,
                ...(hasBase ? ["基準画像は、新しい回から選び直すまで残します"] : []),
              ]}
          note={keepHistory
            ? "前の回は消えません。生成にはGPUを使うため、完了まで時間がかかります。"
            : "上書きは元に戻せません。生成を始める前に前の回を削除します。どちらにするかの初めの選択は「設定の確認・変更」で変えられます。"}
          extra={(
            <div className="vs-field">
              <label>前の回の扱い</label>
              <div className="vs-row">
                {([false, true] as const).map((value) => (
                  <label className="vs-choice" key={String(value)}>
                    <input
                      type="radio"
                      name="keep-history"
                      checked={keepHistory === value}
                      onChange={() => setKeepHistory(value)}
                    />
                    <span>{value ? "履歴として残す" : "上書きする（前の回の候補を削除）"}</span>
                  </label>
                ))}
              </div>
            </div>
          )}
          confirmLabel={keepHistory ? "作り直す" : "削除して作り直す"}
          onConfirm={() => {
            const keep = keepHistory;
            setRegenerateCandidates(false);
            void runCandidateGeneration(keep);
          }}
          onCancel={() => setRegenerateCandidates(false)}
        />
      )}
      {unusedDeletion && (
        <ConfirmDialog
          title="選ばなかった人物候補を削除しますか？"
          lead="基準画像だけを残し、ほかの人物候補を画像ごと削除します。元には戻せません。"
          items={[`人物候補 ${unusedCandidates}枚と、その画像（履歴として残した回も含みます）`]}
          note="基準画像と、作った立ち絵はそのまま残ります。"
          confirmLabel="削除する"
          busy={busy === "unused-candidates"}
          onConfirm={() => void removeUnusedCandidates()}
          onCancel={() => setUnusedDeletion(false)}
        />
      )}
      {batchDeletion && (
        <ConfirmDialog
          title="この回の人物候補を削除しますか？"
          lead={`${batchLabel(batchDeletion)}に作った回を、画像ごと完全に削除します。元には戻せません。`}
          items={[
            `人物候補 ${batchDeletion.image_count}枚と、その画像`,
            ...(batchDeletion.has_base
              ? ["この回から選んでいた基準画像（立ち絵の基準リンクが外れます）"]
              : []),
          ]}
          note="削除するのはこのアプリの保存領域だけです。既存のComfyUIや参照中のモデルフォルダには触れません。"
          confirmLabel="削除する"
          busy={deletingBatch}
          onConfirm={() => void confirmBatchDeletion()}
          onCancel={() => setBatchDeletion(null)}
        />
      )}
      {compareTarget && (
        <CompareLightbox
          title={`${compareTarget.label}の変更`}
          left={{ src: figureSrc(compareTarget.figure), caption: "いまの立ち絵" }}
          right={{ src: editSrc(compareTarget.edit), caption: "変更した立ち絵" }}
          actions={(
            <>
              <button
                className="vs-btn"
                disabled={busy !== null}
                onClick={() => void adoptEdit(compareTarget.edit)}
              >
                採用する
              </button>
              <button
                className="vs-btn ghost"
                disabled={busy !== null}
                onClick={() => void regenerateEdit(compareTarget.figure, compareTarget.edit)}
              >
                再生成する
              </button>
            </>
          )}
          onClose={() => setCompareTarget(null)}
        />
      )}
      {lightbox.node}
    </div>
  );
}

/** 人物の画面（overview）と、そこから移る手順ごとの画面。 */
type StudioView = "overview" | "profile" | "candidates" | "figures";
