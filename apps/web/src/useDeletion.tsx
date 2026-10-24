import { useCallback, useState } from "react";
import { ConfirmDialog } from "./ConfirmDialog";

/** 確認ダイアログに出す内容と、確認後に実行する処理。 */
export interface DeletionRequest {
  title: string;
  lead: string;
  items: string[];
  note?: string;
  perform: () => Promise<void>;
}

const MANAGED_ONLY_NOTE =
  "削除するのはこのアプリの保存領域だけです。既存のComfyUIや参照中のモデルフォルダには触れません。";

/**
 * 削除の共通フロー。
 *
 * 「内訳を読む → 確認ダイアログ → 実行」をまとめる。内訳の取得に失敗したときは
 * ダイアログを開かず、呼び出し側のエラー表示へ回す（消える前に必ず内訳を見せる）。
 */
export function useDeletion(onError: (message: string) => void) {
  const [request, setRequest] = useState<DeletionRequest | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const ask = useCallback(
    async (build: () => Promise<DeletionRequest>) => {
      setError(null);
      try {
        setRequest(await build());
      } catch (caught) {
        onError(caught instanceof Error ? caught.message : String(caught));
      }
    },
    [onError],
  );

  const confirm = useCallback(async () => {
    if (!request || busy) return;
    setBusy(true);
    setError(null);
    try {
      await request.perform();
      setRequest(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }, [busy, request]);

  const dialog = request ? (
    <ConfirmDialog
      title={request.title}
      lead={request.lead}
      items={request.items}
      note={request.note ?? MANAGED_ONLY_NOTE}
      confirmLabel="削除する"
      busyLabel="削除しています…"
      busy={busy}
      error={error}
      onConfirm={() => void confirm()}
      onCancel={() => {
        setRequest(null);
        setError(null);
      }}
    />
  ) : null;

  return { ask, dialog };
}

/** 「3つの静止画から外れます」の一文（参照が無いときは何も返さない）。 */
export function stillImageUsageItems(
  usage: { still_image_count: number; still_image_names: string[] },
  suffix: string,
): string[] {
  if (usage.still_image_count === 0) return [];
  const names = usage.still_image_names.slice(0, 3).join("・");
  const rest = usage.still_image_count > 3 ? ` ほか${usage.still_image_count - 3}件` : "";
  return [`${usage.still_image_count}つの静止画（${names}${rest}）から${suffix}`];
}
