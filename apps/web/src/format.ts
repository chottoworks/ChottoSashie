/** 容量を読みやすい単位にする。 */
export function formatBytes(value: number | null): string {
  if (value === null) return "未確認";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(unit >= 3 ? 1 : 0)} ${units[unit]}`;
}

/**
 * 保存するときの既定のファイル名を作る。
 *
 * 利用者が後から見て何のファイルか分かるように、作品名やシーン名をそのまま使う。
 * ただしWindowsのファイルシステムはOSが嫌う文字を受け付けないので、ここで落とす
 * （落とさないと保存の段階で弾かれ、何が悪いのか分からないまま止まる）。
 */
export function safeFileName(parts: (string | null | undefined)[], extension: string): string {
  const cleaned = parts
    .map((part) => (part ?? "")
      // Windowsがファイル名に使えない文字と、制御文字を落とす。
      .replace(/[\/:*?"<>|]/g, "")
      // eslint-disable-next-line no-control-regex
      .replace(/[\u0000-\u001f]/g, "")
      .replace(/\s+/g, " ")
      .trim())
    .filter(Boolean);
  // 先頭と末尾の点・空白は、Windowsが黙って落とすので先に外しておく。
  const joined = cleaned.join("_").replace(/^[.\s]+/, "").replace(/[.\s]+$/, "");
  // 長すぎる名前は保存できないことがあるため、頭から適当なところで切る。
  const base = [...(joined || "無題")].slice(0, 80).join("");
  return extension ? `${base}.${extension}` : base;
}
