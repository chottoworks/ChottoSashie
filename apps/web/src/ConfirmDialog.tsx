import { useEffect, useRef, type ReactNode } from "react";

/**
 * 取り消せない操作の確認ダイアログ。
 *
 * 削除は元に戻せないので、「何を消すか」に加えて「一緒に消えるもの」を必ず並べてから
 * 実行する。
 */
export interface ConfirmDialogProps {
  title: string;
  /** 何をするのかの一文。 */
  lead: string;
  /** 一緒に消えるものの内訳（0件のときは省略される）。 */
  items?: string[];
  /** 補足（外部フォルダには触れない、など）。 */
  note?: string;
  /** 実行の前に選ばせたいことがあるときの追加操作（例：履歴を残すか上書きするか）。 */
  extra?: ReactNode;
  confirmLabel: string;
  cancelLabel?: string;
  /** 実行中に確認ボタンへ出す文言。 */
  busyLabel?: string;
  busy?: boolean;
  error?: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  title,
  lead,
  items = [],
  note,
  extra,
  confirmLabel,
  cancelLabel = "やめる",
  busyLabel = "実行しています…",
  busy = false,
  error = null,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const cancelRef = useRef<HTMLButtonElement>(null);

  // 開いた直後は「やめる」に合わせる。Enter連打で消してしまわないようにする。
  useEffect(() => {
    cancelRef.current?.focus();
  }, []);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onCancel();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [busy, onCancel]);

  return (
    <div className="confirm-overlay" role="presentation" onMouseDown={() => { if (!busy) onCancel(); }}>
      <div
        className="confirm-dialog"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="confirm-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <h2 id="confirm-title">{title}</h2>
        <p className="confirm-lead">{lead}</p>
        {items.length > 0 && (
          <>
            <p className="confirm-items-label">一緒に削除されるもの</p>
            <ul className="confirm-items">
              {items.map((item) => <li key={item}>{item}</li>)}
            </ul>
          </>
        )}
        {note && <p className="confirm-note">{note}</p>}
        {extra && <div className="confirm-extra">{extra}</div>}
        {error && <p className="confirm-error" role="alert">{error}</p>}
        <div className="confirm-actions">
          <button type="button" ref={cancelRef} onClick={onCancel} disabled={busy}>
            {cancelLabel}
          </button>
          <button type="button" className="confirm-danger" onClick={onConfirm} disabled={busy}>
            {busy ? busyLabel : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
