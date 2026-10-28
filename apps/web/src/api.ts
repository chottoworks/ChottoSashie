// ちょっと挿絵 ローカルAPI (chotto_sashie_api) の薄いクライアント。

// 接続先はデスクトップ版が起動時に教える（番号が埋まっていればずれるため）。
// 開発時はViteが /api を中継するので、同じ生成元のまま空にする。
declare global {
  interface Window {
    __CHOTTO_SASHIE_API__?: string;
  }
}
const apiBaseUrl = import.meta.env.DEV
  ? ""
  : (window.__CHOTTO_SASHIE_API__ ?? "http://127.0.0.1:8766");

/** APIが返す相対URL（/api/v1/...）を、そのまま画像に使える形にする。 */
export function apiUrl(path: string): string {
  return `${apiBaseUrl}${path}`;
}
export function assetImageUrl(prefix: AssetKind, assetId: string): string {
  return `${apiBaseUrl}/api/v1/${prefix}/${assetId}/image`;
}
export function characterImageUrl(characterId: string): string {
  return `${apiBaseUrl}/api/v1/characters/${characterId}/image`;
}
/** 一覧に出す代表写真。決めていなければ合成に使う画像が返る。 */
export function characterPortraitUrl(characterId: string): string {
  return `${apiBaseUrl}/api/v1/characters/${characterId}/portrait`;
}
/** 範囲を切り出す前の代表写真（範囲を決める画面で全体を見せる）。 */
export function characterPortraitSourceUrl(characterId: string): string {
  return `${apiBaseUrl}/api/v1/characters/${characterId}/portrait/source`;
}
export function stillImageUrl(stillImageId: string): string {
  return `${apiBaseUrl}/api/v1/still-images/${stillImageId}/image`;
}
export function shotVideoUrl(shotId: string): string {
  return `${apiBaseUrl}/api/v1/shots/${shotId}/video`;
}
/** 分割モードで作った、ビート1件ぶんの区間動画。 */
export function shotBeatVideoUrl(beatId: string): string {
  return `${apiBaseUrl}/api/v1/shot-beats/${beatId}/video`;
}
/** このカットの起点として実際に使う1枚（前カットの続きか、静止画か、作り直した1枚か）。 */
export function shotStartFrameUrl(shotId: string): string {
  return `${apiBaseUrl}/api/v1/shots/${shotId}/start-frame`;
}
/** 構図を変えて作った起点／終端フレーム。 */
export function shotFrameUrl(shotId: string, target: ShotFrameTarget): string {
  return `${apiBaseUrl}/api/v1/shots/${shotId}/frame/${target}`;
}

// 画像・動画は記録ごとに決まった場所へ上書きされ、長くキャッシュされる。表示には
// APIが返す版を付けたURLを使い、書き直された後に前のものを出さないようにする。
function withVersion(url: string, version: string): string {
  return `${url}?v=${encodeURIComponent(version)}`;
}
export function assetImageSrc(kind: AssetKind, asset: Asset): string {
  return withVersion(assetImageUrl(kind, asset.id), asset.image_version);
}
export function stillImageSrc(stillImage: StillImage): string {
  return withVersion(stillImageUrl(stillImage.id), stillImage.image_version);
}
export function shotStartFrameSrc(shot: Shot): string {
  return withVersion(shotStartFrameUrl(shot.id), shot.start_frame_version);
}
export function shotVideoSrc(shot: Shot): string {
  return withVersion(shotVideoUrl(shot.id), shot.video_version);
}
/** ビートの区間動画。区間を作り直すとカットの動画の版が変わる。 */
export function shotBeatVideoSrc(shot: Shot, beatId: string): string {
  return withVersion(shotBeatVideoUrl(beatId), shot.video_version);
}
export function characterFigureSrc(figure: CharacterFigure): string {
  return withVersion(characterFigureUrl(figure.id), figure.image_version);
}

export function exportDownloadUrl(projectId: string): string {
  return `${apiBaseUrl}/api/v1/projects/${projectId}/export/download`;
}

export type AssetKind = "background-assets" | "prop-assets";
export type SetupMode = "existing_comfyui" | "managed_external_models" | "managed_dedicated";

export interface SetupConfig {
  mode: SetupMode;
  base_url: string;
  port: number;
  external_model_roots: string[];
  comfyui_path: string;
  python_path: string;
  /** 不足モデルの取得先。空なら共有フォルダ。 */
  model_download_root: string;
  /** 動画も作るか。false なら静止画（挿絵）までの画面だけを出す。 */
  video_enabled: boolean;
  resolved_base_url: string;
  configured: boolean;
  setup_completed: boolean;
  updated_at: string | null;
}
export interface ComfyUIStatus {
  provisioned: boolean;
  target_version: string;
  /** 実際に入っているComfyUIのバージョン（読めなければ空）。 */
  installed_version: string;
  /** 入っているものが古く、入れ替えれば解消できる状態か。 */
  update_available: boolean;
  comfyui_path: string | null;
  python_path: string | null;
  /** shared（共有フォルダ）/ existing（利用者指定の借用） */
  source: string;
  /** このアプリから準備・更新してよいか（借用中だけ false）。 */
  can_update: boolean;
  /** いま使っている本体の展開先。 */
  install_root: string;
  /** 共有フォルダ（ComfyUI本体の置き場）。 */
  shared_root: string;
}
/** 処理の種別を利用者の言葉にする（API側の describe_video_job_kind と同じ呼び方）。 */
const JOB_KIND_LABELS: Record<string, string> = {
  "composition.still_image": "静止画の合成",
  "composition.shot_frame": "構図の作り直し",
  "generation.asset": "背景・小物の生成",
  "generation.prompt.shot": "生成に使う文の用意",
  "generation.video.shot": "カットの動画生成",
  "character.candidates": "人物候補の生成",
  "character.figures": "立ち絵の生成",
  "setup.models.download": "モデルの取得",
  "setup.comfyui.provision": "ComfyUI環境の準備",
  "setup.ffmpeg.provision": "ffmpegの用意",
};

/** 知らない種別でも内部の名前は出さず、「処理」とだけ呼ぶ。 */
export const jobKindLabel = (kind: string): string => JOB_KIND_LABELS[kind] ?? "処理";

export const setupModeLabel = (mode: string): string =>
  ({
    existing_comfyui: "既存のComfyUIをそのまま使う",
    managed_external_models: "専用ComfyUIを起動し、既存モデルを参照する",
    managed_dedicated: "完全に専用の環境（モデルもアプリ管理）",
  })[mode] ?? "（未設定）";
/** この端末で覚えている既定値。画面の初期表示に使う。 */
export interface Preferences {
  composition_quality: "fast" | "careful";
  video_engine: VideoEngine;
  candidate_count: number;
  variation_strength: number;
  /** 人物候補を作り直したときの前の回。discard は上書き（基準画像だけ残す）、keep は履歴に残す。 */
  candidate_history: "discard" | "keep";
  /** 最後に適用したおすすめの段。空なら未適用。 */
  recommendation_tier: string;
  updated_at: string | null;
}

/** おすすめ1項目ぶん。value は Preferences の値そのもの（数値も文字列で来る）。 */
export interface RecommendedSetting {
  key: keyof Preferences & string;
  display_name: string;
  value: string;
  value_label: string;
  reason: string;
  /** 条件を満たさず下げたときの理由。 */
  notes: string[];
}

/** 動作要件に届いているか。届いていなくても止めはしないが、先に伝える。 */
export interface RequirementCheck {
  known: boolean;
  /** met | below_recommended | below_minimum | unknown */
  level: string;
  meets_minimum: boolean;
  meets_recommended: boolean;
  /** 満たしているときは空。 */
  message: string;
  minimum_label: string;
  recommended_label: string;
}

export interface HardwareRecommendation {
  tier: string;
  tier_label: string;
  summary: string;
  settings: RecommendedSetting[];
  warnings: string[];
  requirements: RequirementCheck;
}

/** 調べたGPU・メモリと、それに基づく提案。detected が false なら提案は空。 */
export interface HardwareReport {
  detected: boolean;
  /** comfyui | nvidia-smi | none */
  source: string;
  gpu_name: string;
  vram_total_bytes: number;
  vram_free_bytes: number | null;
  ram_total_bytes: number;
  compute_capability: string;
  driver_version: string;
  message: string;
  recommendation: HardwareRecommendation;
  /** いま覚えている既定値が、この提案と一致しているか。 */
  applied: boolean;
}

export interface SetupStatus {
  mode: SetupMode;
  configured: boolean;
  resolved_base_url: string;
  reachable: boolean | null;
  message: string;
  /** 生成物と設定の保存先。状態表示に出す。 */
  data_dir: string;
}
export interface RequirementModel {
  file: string;
  display_name: string;
  present: boolean;
  reason: string;
}
/** ComfyUIの外で動く道具（書き出しに使うしくみなど）の充足状態。 */
export interface RequirementTool {
  id: string;
  display_name: string;
  present: boolean;
  /** 使っているものの場所（分かるときだけ）。 */
  detail: string;
  /** アプリが用意できるか。できるなら、押す前に量を出す。 */
  can_install: boolean;
  install_size_bytes: number;
  /** 何をどこから取得するのか（押す前に見せる）。 */
  source: string;
  license: string;
  /** 用意できないときに、利用者が何をすればよいか。 */
  hint: string;
}
export interface CapabilityRequirement {
  id: string;
  display_name: string;
  ready: boolean;
  /** 任意の機能。揃っていなくても全体の判定は下げない。 */
  optional: boolean;
  present_nodes: string[];
  missing_nodes: string[];
  models: RequirementModel[];
  /** この端末に要る道具。 */
  tools: RequirementTool[];
}
export interface RequirementsReport {
  ready: boolean;
  capabilities: CapabilityRequirement[];
}
export interface ManagedRuntimeStatus {
  state: string;
  owned: boolean;
  reachable: boolean;
  pid: number | null;
  base_url: string;
  message: string;
}
export interface ModelPlanComponent {
  id: string;
  display_name: string;
  /** 既定では取得しない任意のモデル。 */
  optional: boolean;
  /** まとめて取得するときの単位（例: minimax_h3）。 */
  group: string;
  state: string;
  path: string;
  expected_bytes: number;
  download_bytes: number;
  message: string;
  /** 何をどこから取得するのか（押す前に見せる）。 */
  source: string;
  license: string;
}

/** 取得を押す前に見せる利用条件。制限のないものは載らない。 */
export interface ModelLicenseNotice {
  title_ja: string;
  license_name: string;
  url: string;
  points_ja: string[];
  consent_ja: string;
  note_ja: string;
}
export interface ModelPlan {
  components: ModelPlanComponent[];
  required_download_bytes: number;
  free_bytes: number | null;
  enough_space: boolean;
  message: string;
  /** 参照フォルダから見つかったモデルファイルの件数（参照モード以外は0）。 */
  external_candidate_files: number;
  /** 取得先の種類。shared＝共有フォルダ、external＝既存モデルフォルダへ追加。 */
  download_target: string;
  /** 取得先のフォルダ。 */
  download_root: string;
  /** 取得先の説明（画面表示用）。 */
  download_label: string;
  /** すでに持っているかを探すフォルダ一式。 */
  search_roots: string[];
  /** まとまりごとの利用条件（例: minimax_h3）。制限のあるものだけ載る。 */
  license_notices: Record<string, ModelLicenseNotice | undefined>;
}

/** 取得計画の状態を利用者向けの言葉にする。 */
export const modelStateLabel = (state: string): string =>
  ({
    ready: "準備済み",
    external_ready: "手持ちのモデルを利用",
    missing: "ダウンロードが必要",
    partial: "途中から再開できます",
    invalid: "要確認",
  })[state] ?? state;

/** 取得済み（ダウンロード不要）とみなせる状態か。 */
export const isModelReady = (state: string): boolean =>
  state === "ready" || state === "external_ready";
export interface Project {
  id: string;
  name: string;
  description: string;
  /** 作品全体の画風。人物候補・立ち絵・背景・小物・静止画の生成に反映される。 */
  illustration_style: string;
  created_at: string;
  updated_at: string;
}
export interface Character {
  id: string;
  project_id: string;
  name: string;
  has_reference_image: boolean;
  /** 合成に使う画像とは別に、一覧用の代表写真を決めているか。 */
  has_portrait_image?: boolean;
  portrait_source?: string;
  /** 代表写真のうち一覧に出す範囲。null なら全体を出す。 */
  portrait_crop?: PortraitCrop | null;
  has_character_model?: boolean;
  /** 出来上がっている立ち絵の枚数。0なら合成に使える人物画像がまだ無い。 */
  figure_count?: number;
  /** 一覧に出す写真の版。出ている画像が変わるたびに変わる。 */
  portrait_version?: string;
  updated_at?: string;
}
/** 代表写真のうち一覧に出す範囲。画像の幅・高さに対する割合（0〜1）。 */
export interface PortraitCrop {
  x: number;
  y: number;
  width: number;
  height: number;
}
/** 代表写真に選べる画像1件。 */
export interface PortraitOption {
  id: string;
  label: string;
  url: string;
  selected: boolean;
}
export type BackgroundVariationKind = "prop" | "time";

export interface Asset {
  id: string;
  project_id: string;
  name: string;
  category: string;
  description: string;
  has_image: boolean;
  /** 画像URLに付ける版。画像を書くたびに変わる。 */
  image_version: string;
  /** 今の画像を作るときに ComfyUI へ渡した指示文（作品の画風を足したもの）。 */
  source_prompt: string;
  /** 今の画像を作るときに「生成プロンプト」欄へ入れた文。アップロードやバリエーションでは空。 */
  input_prompt: string;
  /** 背景だけ: このバリエーションの元（自分が元なら null）。 */
  base_asset_id?: string | null;
  created_at: string;
  updated_at: string;
}
export interface StillImage {
  id: string;
  project_id: string;
  ordinal: number;
  name: string;
  background_asset_id: string | null;
  character_ids: string[];
  composition_job_id: string | null;
  has_image: boolean;
  /** 画像URLに付ける版。合成した画像を書くたびに変わる（合成を始めた時点では変わらない）。 */
  image_version: string;
  /** 合成できるか。できないときは理由を compose_blocked_reason に入れる。 */
  can_compose: boolean;
  compose_blocked_reason: string;
}
/** ビートの実現方法。single=1回で生成 / split=区間ごとに生成して繋ぐ。 */
export type TimelineMode = "single" | "split";
/** 構図を変えて作り直すフレームの位置。 */
export type ShotFrameTarget = "start" | "end";
/** 起点フレームが実際にどこから来るか。 */
export type StartFrameSource = "custom" | "previous" | "still_image" | "none";

/** セリフ・ナレーションの1行。話す人は行ごとに決める。 */
export interface ShotSpeechLine {
  /** この静止画にいる人物。ナレーションのときは null。 */
  speaker_character_id: string | null;
  /** 画面の外の声。画面の人物は口を動かさない。 */
  narration: boolean;
  text: string;
}

export interface ShotSpeechLineRead extends ShotSpeechLine {
  /** 話す人を見分ける手がかり（配置・年齢・髪）。空なら、生成に使う文で名指しできない。 */
  speaker_description: string;
  /** 話す人がこの静止画にいるか。人物を外した・消したあとは false。 */
  speaker_present: boolean;
}

/** カットの中の時間区間。動きとセリフを、この単位で割り振る。 */
export interface ShotBeat {
  id: string;
  shot_id: string;
  ordinal: number;
  /** 先行ビートの長さの合計（編集は長さで行い、開始時刻は表示だけ）。 */
  start_seconds: number;
  duration_seconds: number;
  motion_prompt: string;
  speech_lines: ShotSpeechLineRead[];
  has_segment_video: boolean;
  segment_video_format: "webp" | "mp4" | "";
  has_last_frame: boolean;
}

export interface Shot {
  id: string;
  still_image_id: string;
  ordinal: number;
  motion_prompt: string;
  /** ビートが無いときの、カット全体のセリフ・ナレーション（音を作るのはMiniMax H3だけ）。 */
  speech_lines: ShotSpeechLineRead[];
  /** 環境音・効果音（H3の overall_soundscape）。 */
  ambience_prompt: string;
  /** 劇伴（H3の non_diegetic_music）。空なら N/A を送る。 */
  music_prompt: string;
  /** 起点の構図の指示。起点・着地の絵を作り直すときだけ使い、動画の文には入らない。 */
  composition_prompt: string;
  timeline_mode: TimelineMode;
  /** 利用者の覚え書き。生成に使う文には入らない。 */
  note: string;
  duration_seconds: number;
  has_start_frame: boolean;
  /** 次の生成でどこから始まるか。前のカットが未生成なら静止画へ戻る。 */
  start_frame_source: StartFrameSource;
  /** 起点の画像URLに付ける版。起点の絵が書き直されるか、出どころが変わると変わる。 */
  start_frame_version: string;
  has_custom_start_frame: boolean;
  has_custom_end_frame: boolean;
  video_job_id: string | null;
  has_video: boolean;
  /** 動画URL（カットの動画とビートの区間動画）に付ける版。動画を書くたびに変わる。 */
  video_version: string;
  has_last_frame: boolean;
  beats: ShotBeat[];
  /** 実際に生成へ渡す尺（ビートの合計、無ければ duration_seconds）。 */
  total_seconds: number;
  /** 生成済み動画の入れ物。mp4（MiniMax H3・音声付き）は video 要素で再生する。 */
  video_format: "webp" | "mp4" | "";
  /** 動画を生成できるか。できないときは理由を video_blocked_reason に入れる。 */
  can_generate_video: boolean;
  video_blocked_reason: string;
  /** 生成に使う文が用意できているか。 */
  has_prompt: boolean;
  /** 用意したあとで指示が変わったか（＝その文はもう使われない）。 */
  prompt_is_stale: boolean;
  /** 生成に使う文を直接書き直しているか。この間は指示の欄を変えられない。 */
  prompt_edit_mode: boolean;
  created_at: string;
  updated_at: string;
}
/** 動画生成に使うモデル。 */
export type VideoEngine = "wan22" | "minimax_h3";

export const VIDEO_ENGINES: { id: VideoEngine; label: string; note: string }[] = [
  { id: "wan22", label: "Wan 2.2（標準）", note: "映像のみ・16fpsで約5秒" },
  { id: "minimax_h3", label: "MiniMax H3（音声付き）", note: "音声も一緒に生成・24fpsで約5秒。専用モデルの取得が要る" },
];

export interface Job {
  id: string;
  kind: string;
  state: string;
  progress: number;
  message: string | null;
  error_code: string | null;
  /** 要求した条件（立ち絵なら character_id など）。 */
  payload?: Record<string, unknown>;
  /** 成果物の場所など（立ち絵なら figure_id、変更なら edit_id）。 */
  result?: Record<string, unknown> | null;
  created_at?: string;
  updated_at?: string;
}
export interface ExportStatus {
  ffmpeg_available: boolean;
  ffmpeg_path: string | null;
  /** 使えないときに、どこへ行けばよいか（用意は必要環境の確認で行う）。 */
  hint: string;
}


/** 保存先と共有フォルダ。デスクトップ版だけで扱える（ブラウザでは変えられない）。 */
export interface Locations {
  data_dir: string;
  shared_dir: string;
  /** environment | file | default。environment のときは画面から変えられない。 */
  data_dir_source: string;
  shared_dir_source: string;
  config_path: string;
}

/** 移し先を見比べた結果。どの選び方ができるかまで返る。 */
export interface DataDirPlan {
  source: string;
  /** 実際に保存先にする場所。選んだフォルダにほかのファイルがあると、その中の ChottoSashie。 */
  target: string;
  /** 利用者が選んだフォルダ。 */
  requested: string;
  source_facts: { exists: boolean; has_data: boolean; file_count: number; total_bytes: number; is_empty: boolean };
  target_facts: { exists: boolean; has_data: boolean; file_count: number; total_bytes: number; is_empty: boolean };
  same_drive: boolean;
  target_free_bytes: number | null;
  /** migrate（今のデータを移す）/ fresh（新しく始める）/ adopt（そこにあるものを使う）。 */
  available_modes: string[];
  blocked: Record<string, string>;
}

export type DataDirMode = "migrate" | "fresh" | "adopt";

/**
 * 保存先を移している最中の途中経過。
 *
 * 段階（phase）は scan（数えている）/ relink（付け替え）/ copy（複製）/ verify（照合）/
 * cleanup（元を消している）/ rewrite（記録を直している）/ done。全体量が分かるのは
 * 複製のときだけで、それ以外は何をしているかだけを伝える。
 */
export interface DataDirProgress {
  phase: "scan" | "relink" | "copy" | "verify" | "cleanup" | "rewrite" | "done";
  /** いま写しているファイル（保存先からの相対）。 */
  current: string | null;
  total_files: number;
  total_bytes: number;
  copied_files: number;
  copied_bytes: number;
}

/** デスクトップ版か（ブラウザでは保存先を変えられない）。 */
export const isDesktop = (): boolean => desktopBridge() !== null;

export async function getLocations(): Promise<Locations | null> {
  const bridge = desktopBridge();
  return bridge ? bridge.core.invoke<Locations>("get_locations") : null;
}

export async function chooseDirectory(title: string): Promise<string | null> {
  const bridge = desktopBridge();
  if (!bridge) return null;
  return bridge.core.invoke<string | null>("choose_directory", { title });
}

export async function planDataDirChange(target: string): Promise<DataDirPlan> {
  const bridge = desktopBridge();
  if (!bridge) throw new Error("この操作はデスクトップ版だけで使えます");
  return bridge.core.invoke<DataDirPlan>("plan_data_dir_change", { target });
}

/** 保存先を変える。成功するとアプリが再起動するので、この呼び出しは戻らない。 */
export async function applyDataDirChange(target: string, mode: DataDirMode): Promise<void> {
  const bridge = desktopBridge();
  if (!bridge) throw new Error("この操作はデスクトップ版だけで使えます");
  await bridge.core.invoke<void>("apply_data_dir_change", { target, mode });
}

/**
 * 保存先を移している間の途中経過を受け取る。返る関数を呼ぶと受け取りをやめる。
 *
 * 別ドライブへの移動は中身を複製するため、数十GBあれば何分もかかる。押したまま黙ると
 * 止まったのか進んでいるのか分からないので、デスクトップ版から流れてくる経過を出す。
 */
export async function onDataDirProgress(
  handler: (progress: DataDirProgress) => void,
): Promise<() => void> {
  const bridge = desktopBridge();
  if (!bridge?.event) return () => {};
  return bridge.event.listen<DataDirProgress>("data-dir-progress", (message) =>
    handler(message.payload),
  );
}

/** 共有フォルダを変える。中身は移さない。成功するとアプリが再起動する。 */
export async function setSharedDir(target: string): Promise<void> {
  const bridge = desktopBridge();
  if (!bridge) throw new Error("この操作はデスクトップ版だけで使えます");
  await bridge.core.invoke<void>("set_shared_dir", { target });
}

/** 保存の結果。取り消しは失敗ではないので分けて返す。 */
export type SaveOutcome = "saved" | "cancelled";

/** デスクトップ版だけにある、このアプリのコマンドを呼ぶ入口。 */
interface TauriBridge {
  core: { invoke: <T>(command: string, args?: Record<string, unknown>) => Promise<T> };
  /** Rust側から流れてくる知らせ（途中経過など）。 */
  event?: {
    listen: <T>(
      name: string,
      handler: (message: { payload: T }) => void,
    ) => Promise<() => void>;
  };
}

function desktopBridge(): TauriBridge | null {
  return (window as unknown as { __TAURI__?: TauriBridge }).__TAURI__ ?? null;
}

/**
 * 生成物をファイルとして保存する。
 *
 * デスクトップ版では、OSの「名前を付けて保存」ダイアログで場所と名前を選ばせ、
 * 選ばれた場所へはローカルAPIが複製する（大きな動画を画面の層へ通さずに済む）。
 * ブラウザの保存先選択（`showSaveFilePicker`）は使わない——「このサイトにファイルの
 * 編集を許可するか」という警告が必ず付き、その文言は変えられないため、ローカルの
 * 保存なのに身に覚えのない警告が出る。
 *
 * ブラウザで開いているときは、既定の保存先へそのまま落とす。`<a href download>` を
 * 直接置かないのは、URLがページと別の出どころだと `download` が無視され、押しても
 * 何も起きないため。中身をこちらで受け取り、同じ出どころの `blob:` URLへ直す。
 */
export async function saveFile(
  url: string,
  suggestedName: string,
  saveAsPath: string,
): Promise<SaveOutcome> {
  const bridge = desktopBridge();
  if (bridge) {
    const chosen = await bridge.core.invoke<string | null>("choose_save_path", {
      suggestedName,
    });
    if (!chosen) return "cancelled";
    await apiSend<{ path: string; size_bytes: number }>(
      saveAsPath,
      "POST",
      { path: chosen },
      "保存できませんでした",
    );
    return "saved";
  }

  const response = await fetch(url);
  if (!response.ok) throw new Error(await failureMessage(response, "保存できませんでした"));
  const objectUrl = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = suggestedName;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  // 押した直後に取り消すと保存が始まらないことがあるので、しばらく残す。
  setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
  return "saved";
}

/** 保存先を選ばせて複製する入口（デスクトップ版で使う）。 */
export const exportSaveAsPath = (projectId: string) =>
  `/api/v1/projects/${projectId}/export/save-as`;
export const shotVideoSaveAsPath = (shotId: string) => `/api/v1/shots/${shotId}/video/save-as`;
export const stillImageSaveAsPath = (stillImageId: string) =>
  `/api/v1/still-images/${stillImageId}/image/save-as`;

/**
 * 失敗の理由を、画面に出せる文にする。APIが理由を返さなければ呼び出し側の文を使う。
 *
 * 状態番号（HTTP 500 など）は出さない。利用者には意味が無く、何をすればよいかも伝わらない。
 */
async function failureMessage(response: Response, fallback: string): Promise<string> {
  const body = (await response.json().catch(() => null)) as { detail?: unknown } | null;
  const detail = body?.detail;
  if (typeof detail === "string" && detail) return detail;
  // 入力の検証で弾かれたときは一覧で返る。こちらで書いた日本語の理由だけを出す
  // （検証の仕組みが付ける英語の説明は、利用者には読めない）。
  if (Array.isArray(detail)) {
    for (const item of detail) {
      const message = (item as { msg?: unknown } | null)?.msg;
      if (typeof message !== "string") continue;
      const reason = message.replace(/^Value error, /, "");
      if (/[\u3040-\u30ff\u4e00-\u9fff]/.test(reason)) return reason;
    }
  }
  return fallback;
}

async function readJson<T>(response: Response, fallback: string): Promise<T> {
  if (!response.ok) throw new Error(await failureMessage(response, fallback));
  return (await response.json()) as T;
}

async function apiGet<T>(path: string, fallback: string, init?: RequestInit): Promise<T> {
  return readJson<T>(await fetch(`${apiBaseUrl}${path}`, init), fallback);
}
async function apiSend<T>(
  path: string,
  method: string,
  body: unknown,
  fallback: string,
): Promise<T> {
  return readJson<T>(
    await fetch(`${apiBaseUrl}${path}`, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
    fallback,
  );
}
async function apiUpload<T>(path: string, file: File, fallback: string): Promise<T> {
  const form = new FormData();
  form.append("file", file);
  return readJson<T>(await fetch(`${apiBaseUrl}${path}`, { method: "POST", body: form }), fallback);
}

/** 204応答の削除。失敗の理由はAPIのdetailをそのまま伝える。 */
async function apiDelete(path: string, fallback: string): Promise<void> {
  const response = await fetch(`${apiBaseUrl}${path}`, { method: "DELETE" });
  if (response.ok) return;
  throw new Error(await failureMessage(response, fallback));
}

// --- 削除の事前確認（一緒に消えるものの内訳） ---

export interface StillImageUsageInfo {
  still_image_count: number;
  still_image_names: string[];
}
export interface CharacterDeletionPreview {
  character_id: string;
  name: string;
  usage: StillImageUsageInfo;
  candidate_count: number;
  /** 一緒に消える立ち絵の枚数。 */
  figure_count: number;
}
export interface AssetDeletionPreview {
  asset_id: string;
  name: string;
  usage: StillImageUsageInfo;
}
export interface StillImageDeletionPreview {
  still_image_id: string;
  name: string;
  ordinal: number;
  shot_count: number;
  video_count: number;
  has_image: boolean;
}
export interface ShotDeletionPreview {
  shot_id: string;
  ordinal: number;
  has_video: boolean;
  /** ビートと、そのうち区間動画を持つ数。 */
  beat_count: number;
  beat_video_count: number;
}
export interface ProjectDeletionPreview {
  project_id: string;
  name: string;
  character_count: number;
  background_count: number;
  prop_count: number;
  still_image_count: number;
  shot_count: number;
  video_count: number;
}

const previewFallback = "削除内容を確認できませんでした";

export const getCharacterDeletionPreview = (characterId: string) =>
  apiGet<CharacterDeletionPreview>(
    `/api/v1/characters/${characterId}/deletion-preview`,
    previewFallback,
  );
export const getAssetDeletionPreview = (kind: AssetKind, assetId: string) =>
  apiGet<AssetDeletionPreview>(`/api/v1/${kind}/${assetId}/deletion-preview`, previewFallback);
export const getStillImageDeletionPreview = (stillImageId: string) =>
  apiGet<StillImageDeletionPreview>(`/api/v1/still-images/${stillImageId}/deletion-preview`, previewFallback);
export const getShotDeletionPreview = (shotId: string) =>
  apiGet<ShotDeletionPreview>(`/api/v1/shots/${shotId}/deletion-preview`, previewFallback);
export const getProjectDeletionPreview = (projectId: string) =>
  apiGet<ProjectDeletionPreview>(
    `/api/v1/projects/${projectId}/deletion-preview`,
    previewFallback,
  );

// --- セットアップ ---
export const getSetupConfig = () => apiGet<SetupConfig>("/api/v1/setup/config", "設定を取得できませんでした");
export const putSetupConfig = (input: Partial<SetupConfig> & { mode: SetupMode }) =>
  apiSend<SetupConfig>("/api/v1/setup/config", "PUT", input, "設定を保存できませんでした");
/** 「動画も作るか」だけを切り替える（接続方式の設定には触れない）。 */
export const putVideoEnabled = (enabled: boolean) =>
  apiSend<SetupConfig>("/api/v1/setup/video-enabled", "PUT", { enabled }, "設定を保存できませんでした");
export const getSetupStatus = () => apiGet<SetupStatus>("/api/v1/setup/status", "状態を取得できませんでした");
export const completeSetup = () =>
  apiSend<SetupConfig>("/api/v1/setup/complete", "POST", {}, "セットアップを完了できませんでした");
export const resetSetup = () =>
  apiSend<SetupConfig>("/api/v1/setup/reset", "POST", {}, "再セットアップを開始できませんでした");
/** 保存済みの設定を消して最初からやり直す（外部フォルダには触れない）。 */
export const clearSetupConfig = () =>
  apiSend<SetupConfig>("/api/v1/setup/config/clear", "POST", {}, "設定を初期化できませんでした");
export const getRequirements = () =>
  apiGet<RequirementsReport>("/api/v1/setup/requirements", "必要環境を確認できませんでした");
/** この端末の構成を調べ、それに合う既定値の提案を受け取る（保存はしない）。 */
export const getHardware = () =>
  apiGet<HardwareReport>("/api/v1/setup/hardware", "この端末の構成を確認できませんでした");
/** 提案された既定値を、次回からの既定として保存する。 */
export const applyHardwareRecommendation = () =>
  apiSend<Preferences>(
    "/api/v1/setup/hardware/apply",
    "POST",
    {},
    "おすすめ設定を適用できませんでした",
  );
export const getPreferences = () =>
  apiGet<Preferences>("/api/v1/preferences", "前回の設定を取得できませんでした");
export const putPreferences = (input: Partial<Preferences>) =>
  apiSend<Preferences>("/api/v1/preferences", "PUT", input, "設定を保存できませんでした");
export const startRuntime = () =>
  apiSend<ManagedRuntimeStatus>("/api/v1/setup/runtime/start", "POST", {}, "ComfyUIを起動できませんでした");
export const stopRuntime = () =>
  apiSend<ManagedRuntimeStatus>("/api/v1/setup/runtime/stop", "POST", {}, "ComfyUIを停止できませんでした");
export const getRuntimeStatus = () =>
  apiGet<ManagedRuntimeStatus>("/api/v1/setup/runtime/status", "起動状態を取得できませんでした");
export const getModelPlan = () => apiGet<ModelPlan>("/api/v1/setup/models/plan", "モデル計画を取得できませんでした");
/** groups を渡すと、その任意モデル（例: minimax_h3）も併せて取得する。 */
export const startModelDownload = (groups: string[] = []) =>
  apiSend<Job>("/api/v1/setup/models/download", "POST", { groups }, "モデル取得を開始できませんでした");
export const getComfyuiStatus = () =>
  apiGet<ComfyUIStatus>("/api/v1/setup/comfyui/status", "ComfyUI準備状況を取得できませんでした");
/** 必要環境の確認で足りなかった道具を、アプリが用意する（共有フォルダへ置く）。 */
export const provisionTool = (toolId: string) =>
  apiSend<Job>(`/api/v1/setup/tools/${toolId}/provision`, "POST", {}, "準備を始められませんでした");
export const provisionComfyui = () =>
  apiSend<Job>("/api/v1/setup/comfyui/provision", "POST", {}, "ComfyUIの準備を開始できませんでした");
// --- プロジェクト ---
export const listProjects = () => apiGet<Project[]>("/api/v1/projects", "プロジェクトを取得できませんでした");
export const createProject = (input: {
  name: string;
  description?: string;
  illustration_style?: string;
}) =>
  apiSend<Project>("/api/v1/projects", "POST", input, "プロジェクトを作成できませんでした");
export const updateProject = (
  projectId: string,
  input: { name?: string; description?: string; illustration_style?: string },
) => apiSend<Project>(`/api/v1/projects/${projectId}`, "PATCH", input, "作品設定を更新できませんでした");
export const deleteProject = (projectId: string) =>
  apiDelete(`/api/v1/projects/${projectId}`, "作品を削除できませんでした");

// --- 登場人物 ---
export const listCharacters = (projectId: string) =>
  apiGet<Character[]>(`/api/v1/projects/${projectId}/characters`, "登場人物を取得できませんでした");
export const createCharacter = (projectId: string, input: { name: string }) =>
  apiSend<Character>(`/api/v1/projects/${projectId}/characters`, "POST", input, "登場人物を作成できませんでした");
/** 登場人物の名前を付け替える。立ち絵やプロフィールはそのまま。 */
export const updateCharacter = (characterId: string, input: { name: string }) =>
  apiSend<Character>(`/api/v1/characters/${characterId}`, "PATCH", input, "名前を変更できませんでした");
export const uploadCharacterImage = (characterId: string, file: File) =>
  apiUpload<Character>(`/api/v1/characters/${characterId}/image`, file, "画像を登録できませんでした");
/** 代表写真に選べる画像（人物候補・立ち絵）。 */
export const listPortraitOptions = (characterId: string) =>
  apiGet<PortraitOption[]>(
    `/api/v1/characters/${characterId}/portrait-options`,
    "代表写真の候補を取得できませんでした",
  );
export const selectPortrait = (characterId: string, optionId: string) =>
  apiSend<Character>(
    `/api/v1/characters/${characterId}/portrait/select`,
    "POST",
    { option_id: optionId },
    "代表写真を変更できませんでした",
  );
/** 一覧に出す範囲を決める。null で全体へ戻す（保存してある画像そのものは切らない）。 */
export const setPortraitCrop = (characterId: string, crop: PortraitCrop | null) =>
  apiSend<Character>(
    `/api/v1/characters/${characterId}/portrait/crop`,
    "PUT",
    { crop },
    "トリミングを保存できませんでした",
  );
export const uploadCharacterPortrait = (characterId: string, file: File) =>
  apiUpload<Character>(
    `/api/v1/characters/${characterId}/portrait`,
    file,
    "代表写真を登録できませんでした",
  );
export const deleteCharacter = (characterId: string) =>
  apiDelete(`/api/v1/characters/${characterId}`, "登場人物を削除できませんでした");

// --- 立ち絵（その人物の全身・素背景の1枚。合成はここから選ぶ） ---

/** 立ち絵の変更の結果。保存するまで、その立ち絵に預けておく1枚。 */
export interface CharacterFigureEdit {
  id: string;
  figure_id: string;
  state: "queued" | "running" | "completed" | "failed";
  has_image: boolean;
  url: string | null;
  instruction: string;
  prop_asset_ids: string[];
  job_id: string;
  error_message: string;
  created_at: string;
  updated_at: string;
}

export interface CharacterFigure {
  id: string;
  character_id: string;
  label: string;
  /** base=基準画像から / variation=別の立ち絵の変更を残した / upload=手持ちの画像 */
  origin: "base" | "variation" | "upload";
  state: "queued" | "running" | "completed" | "failed";
  seed: number;
  /** 何も選んでいないときに合成が使う1枚か。 */
  is_default: boolean;
  has_image: boolean;
  url: string | null;
  /** 画像URLに付ける版。画像を置き換えるたびに変わる。 */
  image_version: string;
  instruction: string;
  prop_asset_ids: string[];
  source_figure_id: string | null;
  job_id: string;
  ordinal: number;
  error_message: string;
  /** 預かっている変更の結果（保存前）。無ければ null。 */
  edit: CharacterFigureEdit | null;
  created_at: string;
  updated_at: string;
}

export const listCharacterFigures = (characterId: string) =>
  apiGet<CharacterFigure[]>(
    `/api/v1/characters/${characterId}/figures`,
    "立ち絵を取得できませんでした",
  );

/** 立ち絵を並べ替える。この人物の立ち絵すべてのidを、左から順に渡す。 */
export const reorderCharacterFigures = (characterId: string, figureIds: string[]) =>
  apiSend<CharacterFigure[]>(
    `/api/v1/characters/${characterId}/figures/order`,
    "PUT",
    { figure_ids: figureIds },
    "立ち絵を並べ替えられませんでした",
  );

/** 基準画像から正面全身の立ち絵を1枚作る。 */
export const generateCharacterFigure = (characterId: string) =>
  apiSend<{ job: Job }>(
    `/api/v1/characters/${characterId}/figures/generate`,
    "POST",
    {},
    "立ち絵の生成を開始できませんでした",
  );

/** いまある立ち絵の衣装・小物を変える（立ち絵の変更）。
 *
 * 指示だけでも、小物を添えても作れる。出来た1枚はその立ち絵に預けられ、
 * `saveCharacterFigureEdit` で保存するまで立ち絵には反映されない。
 */
export const startCharacterFigureEdit = (
  figureId: string,
  input: { instruction: string; prop_asset_ids: string[] },
) =>
  apiSend<{ job: Job }>(
    `/api/v1/character-figures/${figureId}/edits`,
    "POST",
    input,
    "立ち絵の変更を開始できませんでした",
  );

/** 変更の結果を採用し、元の立ち絵を置き換える。 */
export const saveCharacterFigureEdit = (editId: string) =>
  apiSend<CharacterFigure>(
    `/api/v1/character-figure-edits/${editId}/save`,
    "POST",
    undefined,
    "変更した立ち絵を採用できませんでした",
  );

/** 立ち絵をそのまま複製して、1枚増やす（元を残して変えたいときは、複製してから変更する）。 */
export const duplicateCharacterFigure = (figureId: string) =>
  apiSend<CharacterFigure>(
    `/api/v1/character-figures/${figureId}/duplicate`,
    "POST",
    undefined,
    "立ち絵を複製できませんでした",
  );

/** 変更の結果を保存せずに捨てる。 */
export const discardCharacterFigureEdit = (editId: string) =>
  apiDelete(`/api/v1/character-figure-edits/${editId}`, "変更した立ち絵を破棄できませんでした");

export const characterFigureEditUrl = (editId: string): string =>
  `${apiBaseUrl}/api/v1/character-figure-edits/${editId}/image`;

export const uploadCharacterFigure = (characterId: string, file: File) =>
  apiUpload<CharacterFigure>(
    `/api/v1/characters/${characterId}/figures/upload`,
    file,
    "立ち絵を登録できませんでした",
  );

export const updateCharacterFigure = (
  figureId: string,
  input: { label?: string; is_default?: boolean },
) =>
  apiSend<CharacterFigure>(
    `/api/v1/character-figures/${figureId}`,
    "PATCH",
    input,
    "立ち絵を更新できませんでした",
  );

export const deleteCharacterFigure = (figureId: string) =>
  apiDelete(`/api/v1/character-figures/${figureId}`, "立ち絵を削除できませんでした");

export const characterFigureUrl = (figureId: string): string =>
  `${apiBaseUrl}/api/v1/character-figures/${figureId}/image`;

// --- 背景・小物 ---
export const listAssets = (projectId: string, kind: AssetKind) =>
  apiGet<Asset[]>(`/api/v1/projects/${projectId}/${kind}`, "素材を取得できませんでした");
export const createAsset = (
  projectId: string,
  kind: AssetKind,
  input: { name: string; category?: string; description?: string },
) => apiSend<Asset>(`/api/v1/projects/${projectId}/${kind}`, "POST", input, "素材を作成できませんでした");
/** 背景・小物の名前を付け替える。画像はそのまま。 */
export const updateAsset = (kind: AssetKind, assetId: string, input: { name: string }) =>
  apiSend<Asset>(`/api/v1/${kind}/${assetId}`, "PATCH", input, "名前を変更できませんでした");
export const uploadAssetImage = (kind: AssetKind, assetId: string, file: File) =>
  apiUpload<Asset>(`/api/v1/${kind}/${assetId}/image`, file, "画像を登録できませんでした");
export const generateAsset = (
  kind: AssetKind,
  assetId: string,
  input: { prompt: string; base_url?: string },
) => apiSend<{ asset: Asset; job_id: string }>(`/api/v1/${kind}/${assetId}/generate`, "POST", input, "生成を開始できませんでした");
/** 元の背景から、小物を置いた／時間帯を変えた背景を作る。
 *
 * 時間帯を変える回は小物を渡せない——背景1枚だけを渡す編集でないと時間帯が変わらない。
 */
export const createBackgroundVariation = (
  assetId: string,
  input: {
    kind: BackgroundVariationKind;
    instruction: string;
    variant_label: string;
    prop_asset_ids: string[];
  },
) =>
  apiSend<{ asset: Asset; job_id: string }>(
    `/api/v1/background-assets/${assetId}/variations`,
    "POST",
    input,
    "背景の作成を開始できませんでした",
  );

export const deleteAsset = (kind: AssetKind, assetId: string) =>
  apiDelete(`/api/v1/${kind}/${assetId}`, "素材を削除できませんでした");

// --- 静止画 ---
export const listStillImages = (projectId: string) =>
  apiGet<StillImage[]>(`/api/v1/projects/${projectId}/still-images`, "静止画を取得できませんでした");
export const createStillImage = (
  projectId: string,
  input: { name?: string; background_asset_id?: string | null; character_ids?: string[] },
) => apiSend<StillImage>(`/api/v1/projects/${projectId}/still-images`, "POST", input, "静止画を作成できませんでした");
// quality: "fast" は4ステップで速く、"careful" は高速化LoRAを外して指示の反映を強める。
export const composeStillImage = (
  stillImageId: string,
  input: {
    prompt?: string;
    base_url?: string;
    quality?: "fast" | "careful";
    /** 画面に出ている人物ごとの指定。合成と一緒に保存され、その内容で合成する。 */
    character_specs?: Record<string, StillImageCharacterSpec>;
  },
) =>
  apiSend<{ still_image: StillImage; job_id: string }>(`/api/v1/still-images/${stillImageId}/compose`, "POST", input, "合成を開始できませんでした");
export const updateStillImage = (
  stillImageId: string,
  input: {
    name?: string;
    background_asset_id?: string | null;
    character_ids?: string[];
  },
) => apiSend<StillImage>(`/api/v1/still-images/${stillImageId}`, "PATCH", input, "静止画を変更できませんでした");
export const deleteStillImage = (stillImageId: string) =>
  apiDelete(`/api/v1/still-images/${stillImageId}`, "静止画を削除できませんでした");

/** 合成の作りから決まる上限。画面が同じ数字を持たずに済むようAPIから受け取る。 */
export interface CompositionLimits {
  /** 1枚の静止画に渡せる参照画像の枚数（人物の立ち絵と背景の合計）。 */
  max_reference_images: number;
  /** 1枚に入れられる人物の数。 */
  max_characters: number;
}
export const getCompositionLimits = () =>
  apiGet<CompositionLimits>("/api/v1/composition/limits", "合成の上限を取得できませんでした");

// --- カット ---
export const listShots = (stillImageId: string) =>
  apiGet<Shot[]>(`/api/v1/still-images/${stillImageId}/shots`, "カットを取得できませんでした");
export interface ShotFields {
  motion_prompt?: string;
  /** 行の並びをまるごと置き換える。 */
  speech_lines?: ShotSpeechLine[];
  ambience_prompt?: string;
  music_prompt?: string;
  composition_prompt?: string;
  timeline_mode?: TimelineMode;
  note?: string;
  duration_seconds?: number;
}
export const createShot = (stillImageId: string, input: ShotFields) =>
  apiSend<Shot>(`/api/v1/still-images/${stillImageId}/shots`, "POST", input, "カットを作成できませんでした");
export const updateShot = (shotId: string, input: ShotFields) =>
  apiSend<Shot>(`/api/v1/shots/${shotId}`, "PATCH", input, "カットを更新できませんでした");
export const generateShotVideo = (
  shotId: string,
  input: {
    motion_prompt?: string;
    speech_lines?: ShotSpeechLine[];
    ambience_prompt?: string;
    music_prompt?: string;
    timeline_mode?: TimelineMode;
    base_url?: string;
    continue_from_previous?: boolean;
    engine?: VideoEngine;
  },
) => apiSend<{ shot: Shot; job_id: string }>(`/api/v1/shots/${shotId}/generate-video`, "POST", input, "動画生成を開始できませんでした");
export const deleteShot = (shotId: string) =>
  apiDelete(`/api/v1/shots/${shotId}`, "カットを削除できませんでした");

// --- ビート（カット内の時間区間） ---
export interface ShotBeatFields {
  duration_seconds?: number;
  motion_prompt?: string;
  /** 行の並びをまるごと置き換える。 */
  speech_lines?: ShotSpeechLine[];
}
export const listShotBeats = (shotId: string) =>
  apiGet<ShotBeat[]>(`/api/v1/shots/${shotId}/beats`, "ビートを取得できませんでした");
export const createShotBeat = (shotId: string, input: ShotBeatFields) =>
  apiSend<ShotBeat>(`/api/v1/shots/${shotId}/beats`, "POST", input, "ビートを追加できませんでした");
export const updateShotBeat = (beatId: string, input: ShotBeatFields) =>
  apiSend<ShotBeat>(`/api/v1/shot-beats/${beatId}`, "PATCH", input, "ビートを更新できませんでした");
export const deleteShotBeat = (beatId: string) =>
  apiDelete(`/api/v1/shot-beats/${beatId}`, "ビートを削除できませんでした");

// --- 構図を変えた起点・終端フレーム ---
export const composeShotFrame = (
  shotId: string,
  input: { target?: ShotFrameTarget; composition_prompt?: string; base_url?: string },
) =>
  apiSend<{ shot: Shot; job_id: string }>(
    `/api/v1/shots/${shotId}/frame`,
    "POST",
    input,
    "構図の作り直しを開始できませんでした",
  );
export interface ShotPromptSegment {
  label: string;
  prompt: string;
  length_frames: number;
  seconds: number;
  /** 直接書き直した文か。 */
  edited: boolean;
}
/** 生成に使う文の、いまの状態。用意できていない文と古い文は中身が空で返る。 */
export interface ShotPromptState {
  engine: VideoEngine;
  timeline_mode: TimelineMode;
  segments: ShotPromptSegment[];
  total_seconds: number;
  /** 用意できた文があるか。 */
  ready: boolean;
  /** 用意したあとで指示・作り方が変わったか。 */
  stale: boolean;
  /** 直接書き直しているか。 */
  edit_mode: boolean;
}
/** 用意できている「生成に使う文」を読む（作りはしない）。 */
export const getShotPrompt = (shotId: string, engine: VideoEngine) =>
  apiGet<ShotPromptState>(
    `/api/v1/shots/${shotId}/prompt?engine=${engine}`,
    "生成に使う文を取得できませんでした",
  );
/** 動画は作らず、生成に使う文だけを先に用意する。 */
export const prepareShotPrompt = (shotId: string, engine: VideoEngine) =>
  apiSend<{ shot: Shot; job_id: string }>(
    `/api/v1/shots/${shotId}/prompt`,
    "POST",
    { engine },
    "生成に使う文を用意できませんでした",
  );
/** 直接書き直した文を保存する（書き直しモードのときだけ）。 */
export const writeShotPrompt = (shotId: string, engine: VideoEngine, segments: string[]) =>
  apiSend<ShotPromptState>(
    `/api/v1/shots/${shotId}/prompt?engine=${engine}`,
    "PATCH",
    { segments },
    "書き直した文を保存できませんでした",
  );
/** 文を直接書き直すモードの入切。入の間は、もとになる指示を変えられない。 */
export const setShotPromptEditMode = (shotId: string, engine: VideoEngine, editMode: boolean) =>
  apiSend<ShotPromptState>(
    `/api/v1/shots/${shotId}/prompt/edit-mode?engine=${engine}`,
    "POST",
    { edit_mode: editMode },
    "書き直しモードを切り替えられませんでした",
  );

export const clearShotFrame = (shotId: string, target: ShotFrameTarget) =>
  apiSend<Shot>(`/api/v1/shots/${shotId}/frame/${target}`, "DELETE", undefined, "フレームを外せませんでした");

// --- ジョブ・書き出し ---
/**
 * 状態の問い合わせにだけ付ける制限時間。
 *
 * どちらも軽い読み取りで、これだけ待って返らないなら返らない。返らない相手を
 * 待ち続けると、繰り返し問い合わせるたびに接続の口が埋まり、他の表示まで止まる。
 */
const JOB_FETCH_TIMEOUT_MS = 10_000;
const jobFetchOptions = (): RequestInit => ({ signal: AbortSignal.timeout(JOB_FETCH_TIMEOUT_MS) });

export const getJob = (jobId: string) =>
  apiGet<Job>(`/api/v1/jobs/${jobId}`, "ジョブを取得できませんでした", jobFetchOptions());
export const listJobs = () =>
  apiGet<Job[]>("/api/v1/jobs", "処理の一覧を取得できませんでした", jobFetchOptions());
/** 実行中の処理を中止する（ComfyUIの生成と子プロセスも止める）。 */
export const cancelJob = (jobId: string) =>
  apiSend<Job>(`/api/v1/jobs/${jobId}/cancel`, "POST", {}, "処理を中止できませんでした");
export const getExportStatus = () => apiGet<ExportStatus>("/api/v1/export/status", "書き出し状態を取得できませんでした");
/** この作品の書き出し済みファイル。作り直すまで残るので、開き直しても取り出せる。 */
export interface ProjectExportStatus {
  exists: boolean;
  created_at: string | null;
  size_bytes: number;
  /** いまのカット動画のfps（重複なし・小さい順）。作り方を混ぜると複数になる。読めなければ空。 */
  cut_fps: number[];
}
export const getProjectExport = (projectId: string) =>
  apiGet<ProjectExportStatus>(
    `/api/v1/projects/${projectId}/export`,
    "書き出し済みの動画を確認できませんでした",
  );
/** 書き出す。fps を省くと、カット動画のfpsに合わせる。 */
export const exportProject = (projectId: string, input: { fps?: number } = {}) =>
  apiSend<{
    project_id: string;
    shot_count: number;
    download_url: string;
    audio_included: boolean;
    /** 実際に書き出したfps。 */
    fps: number;
  }>(
    `/api/v1/projects/${projectId}/export`,
    "POST",
    input,
    "書き出しに失敗しました",
  );

/** 状態を取れない時間がこれを超えたら、待つのをやめて理由を返す。 */
const JOB_POLL_GIVE_UP_MS = 60_000;

/**
 * ジョブが終わるまで状態を追う。
 *
 * 途中で状態を取れなくても、すぐには諦めない。処理そのものはAPIの中で動き続けており、
 * 1回取れなかっただけで抜けると、出来上がるものを「失敗」として伝えてしまう。
 * 取れない状態が続いたときだけ、最後の理由をそのまま返す。
 */
export async function pollJob(jobId: string, onUpdate: (job: Job) => void): Promise<Job> {
  let unreachableSince: number | null = null;
  for (;;) {
    let job: Job | null = null;
    try {
      job = await getJob(jobId);
      unreachableSince = null;
    } catch {
      unreachableSince ??= Date.now();
      if (Date.now() - unreachableSince >= JOB_POLL_GIVE_UP_MS) {
        throw new Error(
          "処理の状態を確認できません。処理そのものは続いていることがあります。"
          + "しばらくしてから開き直すと、いまの状態を読み直せます",
        );
      }
    }
    if (job) {
      onUpdate(job);
      if (["completed", "failed", "cancelled", "interrupted"].includes(job.state)) {
        return job;
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 1200));
  }
}

// --- 人物候補とプロフィール ---

export interface CharacterProfile {
  name: string;
  gender_age: string;
  face: string;
  hair_style: string;
  hair_color: string;
  eyes: string;
  eye_color: string;
  body_type: string;
  base_outfit: string;
  accessories: string;
  personality: string;
  immutable_features: string[];
  art_style: string;
  notes: string;
}

export interface CharacterProfileRead extends CharacterProfile {
  id: string;
  project_id: string;
}

export interface CandidateImage {
  filename: string;
  subfolder: string;
  type: string;
}

export interface Candidate {
  id: string;
  character_id: string;
  ordinal: number;
  state: string;
  seed: number;
  is_base: boolean;
  image: CandidateImage | null;
  error_message: string | null;
}

export const getCharacterProfile = (characterId: string) =>
  apiGet<CharacterProfileRead>(
    `/api/v1/characters/${characterId}/profile`,
    "プロフィールを取得できませんでした",
  );

export const updateCharacterProfile = (characterId: string, input: Partial<CharacterProfile>) =>
  apiSend<CharacterProfileRead>(
    `/api/v1/characters/${characterId}/profile`,
    "PATCH",
    input,
    "プロフィールを保存できませんでした",
  );

/** `keepHistory` を false にすると、生成前にこの人物の候補をすべて削除する（上書き）。 */
export const generateCandidates = (
  characterId: string,
  count: number,
  variationStrength: number,
  keepHistory: boolean,
) =>
  apiSend<{ job: Job; candidates: Candidate[] }>(
    `/api/v1/characters/${characterId}/candidates/generate`,
    "POST",
    { count, variation_strength: variationStrength, keep_history: keepHistory },
    "人物候補の生成を開始できませんでした",
  );

/** 基準画像に選ばなかった人物候補を、画像ごと削除する。 */
export const deleteUnusedCandidates = (characterId: string) =>
  apiSend<{ deleted_count: number }>(
    `/api/v1/characters/${characterId}/candidates/unused`,
    "DELETE",
    undefined,
    "選ばなかった人物候補を削除できませんでした",
  );

/** 人物候補を作った1回ぶんの要約（前の回へ戻るための一覧）。 */
export interface BatchSummary {
  batch_job_id: string;
  created_at: string;
  image_count: number;
  completed_count: number;
  is_latest: boolean;
  has_base: boolean;
  similar_count: number;
  different_count: number;
  pending_count: number;
}

export const listCandidateBatches = (characterId: string) =>
  apiGet<BatchSummary[]>(
    `/api/v1/characters/${characterId}/candidate-batches`,
    "候補の生成履歴を取得できませんでした",
  );
export const listCandidates = (characterId: string, batchJobId?: string | null) =>
  apiGet<Candidate[]>(
    `/api/v1/characters/${characterId}/candidates`
      + (batchJobId ? `?batch_job_id=${encodeURIComponent(batchJobId)}` : ""),
    "人物候補を取得できませんでした",
  );

export const candidateImageUrl = (candidateId: string): string =>
  `${apiBaseUrl}/api/v1/character-candidates/${candidateId}/image`;

export const selectBaseCandidate = (candidateId: string) =>
  apiSend<Candidate>(
    `/api/v1/character-candidates/${candidateId}/select-base`,
    "POST",
    {},
    "基準画像を選択できませんでした",
  );

/**
 * 向き（角度）の作り方。
 * fast: アプリが取得するfp8mixedをそのまま使う。速いが左右の指定が効きにくい。
 * high: bf16を使う。左右は効くが、モデルが大きいぶん遅い。
 */
export interface BatchDeletionResult {
  batch_job_id: string;
  deleted_count: number;
}

/** 残した履歴を、画像の控えごと完全に削除する。 */
export const deleteCandidateBatch = (characterId: string, batchJobId: string) =>
  apiSend<BatchDeletionResult>(
    `/api/v1/characters/${characterId}/candidate-batches/${batchJobId}`,
    "DELETE",
    undefined,
    "候補の履歴を削除できませんでした",
  );
export interface StillImageCharacterSpec {
  pose: string;
  expression: string;
  direction: string;
  placement: string;
  notes: string;
}

/** 静止画1枚の中での、その人物の指定と、使う立ち絵。 */
export interface StillImageCharacter {
  character_id: string;
  character_name: string;
  spec: StillImageCharacterSpec;
  /** 実際に合成で使う立ち絵（選んでいなければ既定の1枚）。 */
  figure_id: string | null;
  /** 自分で選んだのか、既定のまま使っているのか。 */
  figure_is_default: boolean;
  figure_label: string;
  figure_url: string | null;
  has_figure: boolean;
}

export const listStillImageCharacters = (stillImageId: string) =>
  apiGet<StillImageCharacter[]>(
    `/api/v1/still-images/${stillImageId}/characters`,
    "人物の指定を取得できませんでした",
  );

/** 使う立ち絵を保存する。`figureId` を null にすると既定の立ち絵に戻る。
 *  指定には触れない（指定は合成のときに `composeStillImage` で保存する）。 */
export const chooseStillImageFigure = (
  stillImageId: string,
  characterId: string,
  figureId: string | null,
) =>
  apiSend<StillImageCharacter>(
    `/api/v1/still-images/${stillImageId}/characters/${characterId}`,
    "PUT",
    { figure_id: figureId },
    "使う立ち絵を保存できませんでした",
  );
