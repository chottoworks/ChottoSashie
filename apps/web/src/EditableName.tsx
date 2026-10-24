import { useState } from "react";

/**
 * 名前の表示と、その場での付け替え。
 *
 * 登場人物・背景・小物は、作ったあとで呼び名を直したくなることが多い。消して作り直すと
 * 作った画像まで失うので、名前だけを変えられるようにする。
 */
export function EditableName({
  name,
  onSave,
  as: Tag = "strong",
}: {
  name: string;
  onSave: (name: string) => Promise<void>;
  /** 名前を出す要素（見出しなら h2）。 */
  as?: "strong" | "h2";
}) {
  const [draft, setDraft] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save() {
    if (draft === null) return;
    const trimmed = draft.trim();
    if (!trimmed) return;
    if (trimmed === name) {
      setDraft(null);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await onSave(trimmed);
      setDraft(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  if (draft === null) {
    return (
      <span className="vs-editable-name">
        <Tag>{name}</Tag>
        <button
          type="button"
          className="vs-btn ghost vs-rename"
          onClick={(e) => {
            // カード全体が押せる場所に置いても、名前の変更だけで済ませる。
            e.stopPropagation();
            setError(null);
            setDraft(name);
          }}
        >
          名前を変更
        </button>
      </span>
    );
  }

  return (
    <span className="vs-editable-name editing" onClick={(e) => e.stopPropagation()}>
      <input
        className="vs-input"
        value={draft}
        autoFocus
        aria-label="名前"
        disabled={saving}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") void save();
          if (e.key === "Escape") setDraft(null);
        }}
      />
      <button type="button" className="vs-btn" disabled={saving || !draft.trim()} onClick={() => void save()}>
        {saving ? "保存中..." : "保存"}
      </button>
      <button type="button" className="vs-btn ghost" disabled={saving} onClick={() => setDraft(null)}>
        やめる
      </button>
      {error && <span className="vs-error-inline">{error}</span>}
    </span>
  );
}
