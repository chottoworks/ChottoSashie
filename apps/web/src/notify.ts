/**
 * 長い処理の完了を知らせる。
 *
 * 立ち絵の生成も動画生成も数分かかる。画面を見ていない間に終わっても
 * 気づけないため、タイトルバーとデスクトップ通知で伝える。通知の許可は
 * 「長い処理を始めた時点」で求める（起動直後に出すと文脈が分からない）。
 */

/** 通知を出したい、時間のかかる処理。短いものは対象にしない。 */
const LONG_RUNNING_KINDS = new Set([
  "generation.video.shot",
  "setup.models.download",
  "setup.comfyui.provision",
  "character.candidates",
  "character.figures",
  "composition.still_image",
]);

export function isLongRunningKind(kind: string): boolean {
  return LONG_RUNNING_KINDS.has(kind);
}

/** 許可を求める。断られても処理は続くので、結果は見るだけ。 */
export async function requestNotificationPermission(): Promise<void> {
  if (typeof Notification === "undefined" || Notification.permission !== "default") return;
  try {
    await Notification.requestPermission();
  } catch {
    // 通知が使えない環境（古いWebView等）でも本体の処理は止めない。
  }
}

/** タイトルバーに進行中の印を出す。null で元へ戻す。 */
export function setTitleProgress(label: string | null): void {
  const base = document.title.replace(/^\(\d+%\)\s*/, "").replace(/^●\s*/, "");
  document.title = label ? `${label} ${base}` : base;
}

/** 完了・失敗を知らせる。 */
export function notifyFinished(title: string, body: string): void {
  if (typeof Notification === "undefined" || Notification.permission !== "granted") return;
  try {
    new Notification(title, { body });
  } catch {
    // 表示できなくても支障はない。
  }
}
