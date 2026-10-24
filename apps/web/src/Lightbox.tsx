import { useCallback, useEffect, useState, type ReactNode } from "react";

/** 拡大表示する1件。 */
export interface LightboxItem {
  src: string;
  caption: string;
  kind?: "image" | "video";
}

/**
 * 生成物の拡大表示。
 *
 * 出来上がりを確かめるのが作品づくりの中心なので、サムネイルだけで終わらせない。
 * 動画も画像と同じ扱いで開ける。アニメーションwebpは画像として、音声付きmp4は
 * 再生操作の要る動画として出し分ける（音は再生操作なしでは鳴らせない）。
 *
 * 一覧から開いたときは、拡大したまま前後へ切り替えられる（←→キーでも）。いちいち
 * 閉じて次を開き直すと、並べて見比べたいときに手間がかかる。画像そのものを押しても
 * 閉じる（拡大した画像が画面のほとんどを占めるので、外側だけでは押しにくい）。
 */
export function Lightbox({
  items,
  index,
  onIndexChange,
  onClose,
}: {
  items: LightboxItem[];
  index: number;
  onIndexChange: (index: number) => void;
  onClose: () => void;
}) {
  const count = items.length;
  const item = items[Math.min(Math.max(index, 0), count - 1)];
  const canStep = count > 1;
  const step = useCallback(
    (delta: number) => onIndexChange((index + delta + count) % count),
    [count, index, onIndexChange],
  );

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
      if (canStep && event.key === "ArrowLeft") step(-1);
      if (canStep && event.key === "ArrowRight") step(1);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [canStep, onClose, step]);

  if (!item) return null;
  return (
    <div className="vs-lightbox" role="presentation" onMouseDown={onClose}>
      {canStep && (
        <button
          type="button"
          className="vs-lightbox-step prev"
          aria-label="前の画像"
          onMouseDown={(event) => event.stopPropagation()}
          onClick={() => step(-1)}
        >
          ‹
        </button>
      )}
      <figure onMouseDown={(event) => event.stopPropagation()}>
        {item.kind === "video" ? (
          // 動画は再生操作に使うので、押しても閉じない。
          <video key={item.src} src={item.src} controls autoPlay loop playsInline />
        ) : (
          <img
            key={item.src}
            src={item.src}
            alt={item.caption}
            title="押すと閉じます"
            className="vs-lightbox-image"
            onClick={onClose}
          />
        )}
        <figcaption>
          <span>
            {item.caption}
            {canStep && <span className="vs-lightbox-count">{index + 1} / {count}</span>}
          </span>
          <button className="vs-btn ghost" onClick={onClose}>
            閉じる
          </button>
        </figcaption>
      </figure>
      {canStep && (
        <button
          type="button"
          className="vs-lightbox-step next"
          aria-label="次の画像"
          onMouseDown={(event) => event.stopPropagation()}
          onClick={() => step(1)}
        >
          ›
        </button>
      )}
    </div>
  );
}

/**
 * 2枚を横並びで拡大する表示。
 *
 * 「基準画像と同じ人物か」を判断するときに使うので、拡大したときも必ず基準画像と
 * 並べる。片方だけ大きくしても比べられない。
 */
export function CompareLightbox({
  title,
  left,
  right,
  actions,
  onClose,
}: {
  title: string;
  left: { src: string | null; caption: string };
  right: { src: string; caption: string; hint?: string };
  /** 比べたその場で決められるようにする操作（判定など）。 */
  actions?: ReactNode;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  return (
    <div className="vs-lightbox" role="presentation" onMouseDown={onClose}>
      <section
        className="vs-lightbox-compare"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header>
          <strong>{title}</strong>
          <button className="vs-btn ghost" onClick={onClose}>
            閉じる
          </button>
        </header>
        <div className="vs-lightbox-compare-grid">
          <figure>
            {left.src ? (
              <img src={left.src} alt={left.caption} title="押すと閉じます" className="vs-lightbox-image" onClick={onClose} />
            ) : (
              <div className="vs-candidate-placeholder">{left.caption}</div>
            )}
            <figcaption>{left.caption}</figcaption>
          </figure>
          <figure>
            <img
              src={right.src}
              alt={right.caption}
              title={right.hint ?? "押すと閉じます"}
              className="vs-lightbox-image"
              onClick={onClose}
            />
            <figcaption>{right.caption}</figcaption>
          </figure>
        </div>
        {actions && <div className="vs-lightbox-actions">{actions}</div>}
        <p className="vs-muted">Escキー、背景、または「閉じる」で戻れます。</p>
      </section>
    </div>
  );
}

/** 拡大表示の開閉。1枚だけでも、一覧から開いて前後へ送る形でも開ける。 */
export function useLightbox() {
  const [gallery, setGallery] = useState<{ items: LightboxItem[]; index: number } | null>(null);
  return {
    open: (src: string, caption: string, kind: "image" | "video" = "image") =>
      setGallery({ items: [{ src, caption, kind }], index: 0 }),
    /** 一覧のうち `index` 番目を開く。拡大したまま前後へ切り替えられる。 */
    openGallery: (items: LightboxItem[], index: number) =>
      setGallery(items.length ? { items, index: Math.min(Math.max(index, 0), items.length - 1) } : null),
    node: gallery ? (
      <Lightbox
        items={gallery.items}
        index={gallery.index}
        onIndexChange={(index) => setGallery((current) => (current ? { ...current, index } : current))}
        onClose={() => setGallery(null)}
      />
    ) : null,
  };
}
