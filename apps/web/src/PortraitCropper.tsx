import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import type { PortraitCrop } from "./api";
import {
  isWholeImage,
  moveCrop,
  resizeCrop,
  toSquare,
  WHOLE_IMAGE,
  type CropCorner,
} from "./portraitCrop";

const CORNERS: CropCorner[] = ["nw", "ne", "sw", "se"];

/** 一覧の枠（正方形）に、決めた範囲がどう入るか。正方形でなければ余白が出る。 */
export function CropPreview({
  src,
  crop,
  aspect,
  alt,
}: {
  src: string;
  crop: PortraitCrop;
  aspect: number;
  alt: string;
}) {
  // 範囲の画素での縦横比。横長なら幅いっぱい、縦長なら高さいっぱいに置く。
  const ratio = (crop.width * aspect) / crop.height;
  return (
    <div className="vs-crop-preview">
      <div
        className="vs-crop-preview-window"
        style={{
          aspectRatio: String(ratio),
          width: ratio >= 1 ? "100%" : "auto",
          height: ratio >= 1 ? "auto" : "100%",
        }}
      >
        <img
          src={src}
          alt={alt}
          draggable={false}
          style={{
            width: `${100 / crop.width}%`,
            height: `${100 / crop.height}%`,
            left: `${(-crop.x / crop.width) * 100}%`,
            top: `${(-crop.y / crop.height) * 100}%`,
          }}
        />
      </div>
    </div>
  );
}

// つかんだときのポインターの位置（startX / startY）からの動きで計算する。
// 角をつまむときも、ポインターの位置へ角を合わせず動いた分だけ動かす（つまみの
// 中心から外れた所をつかむと、角がポインターへ跳ねてしまう）。
type DragStart = { pointerId: number; startX: number; startY: number; start: PortraitCrop };
type Drag = (DragStart & { kind: "move" }) | (DragStart & { kind: "resize"; corner: CropCorner });

/**
 * 代表写真の、一覧に出す範囲を決める。
 *
 * 写真の全体を出し、その上の枠を動かしたり角をつまんで大きさを変えたりする。
 * 横に、一覧の枠でどう見えるかを並べる（正方形でなければ余白が出る）。
 */
export function PortraitCropper({
  src,
  initial,
  busy,
  onSave,
  onCancel,
}: {
  src: string;
  initial: PortraitCrop | null;
  busy: boolean;
  /** 全体と同じ範囲なら null を渡す（範囲を決めていないのと同じ）。 */
  onSave: (crop: PortraitCrop | null) => void;
  onCancel: () => void;
}) {
  const [crop, setCrop] = useState<PortraitCrop>(initial ?? WHOLE_IMAGE);
  const [square, setSquare] = useState(false);
  // 画像の幅 / 高さ（画素）。読み込むまでは分からないので、それまで枠を出さない。
  const [aspect, setAspect] = useState<number | null>(null);
  const cropperRef = useRef<HTMLDivElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const drag = useRef<Drag | null>(null);

  // 写真の大きさが分かり、横の見え方まで描いてから、写真と操作がまとめて画面に入る
  // 位置へ動かす（読み込んだ時点では見え方がまだ無く、そのあと高さが伸びる）。
  useEffect(() => {
    if (aspect !== null) cropperRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [aspect]);

  function pointerPosition(event: ReactPointerEvent) {
    const rect = stageRef.current!.getBoundingClientRect();
    return {
      x: (event.clientX - rect.left) / rect.width,
      y: (event.clientY - rect.top) / rect.height,
    };
  }

  function beginMove(event: ReactPointerEvent<HTMLDivElement>) {
    if (busy) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    const { x, y } = pointerPosition(event);
    drag.current = { kind: "move", pointerId: event.pointerId, startX: x, startY: y, start: crop };
  }

  function beginResize(event: ReactPointerEvent<HTMLSpanElement>, corner: CropCorner) {
    if (busy) return;
    event.preventDefault();
    // 枠を動かす操作に伝わらないようにする（角は枠の中にある）。
    event.stopPropagation();
    event.currentTarget.setPointerCapture(event.pointerId);
    const { x, y } = pointerPosition(event);
    drag.current = {
      kind: "resize",
      corner,
      pointerId: event.pointerId,
      startX: x,
      startY: y,
      start: crop,
    };
  }

  function onPointerMove(event: ReactPointerEvent) {
    const current = drag.current;
    if (!current || current.pointerId !== event.pointerId || aspect === null) return;
    const { x, y } = pointerPosition(event);
    const dx = x - current.startX;
    const dy = y - current.startY;
    if (current.kind === "move") {
      setCrop(moveCrop(current.start, dx, dy));
      return;
    }
    const { start, corner } = current;
    const cornerX = corner === "nw" || corner === "sw" ? start.x : start.x + start.width;
    const cornerY = corner === "nw" || corner === "ne" ? start.y : start.y + start.height;
    setCrop(resizeCrop(start, corner, cornerX + dx, cornerY + dy, square, aspect));
  }

  function endDrag(event: ReactPointerEvent) {
    if (drag.current?.pointerId === event.pointerId) drag.current = null;
  }

  function toggleSquare(checked: boolean) {
    setSquare(checked);
    if (checked && aspect !== null) setCrop((value) => toSquare(value, aspect));
  }

  function resetToWhole() {
    setCrop(square && aspect !== null ? toSquare(WHOLE_IMAGE, aspect) : WHOLE_IMAGE);
  }

  return (
    <div ref={cropperRef} className="vs-cropper">
      <div className="vs-cropper-body">
        {/* つまんだ要素がポインターを捕まえ、動きはここまで伝わってくる。 */}
        <div
          ref={stageRef}
          className="vs-cropper-stage"
          onPointerMove={onPointerMove}
          onPointerUp={endDrag}
          onPointerCancel={endDrag}
        >
          <img
            src={src}
            alt="代表写真の全体"
            draggable={false}
            onLoad={(event) => {
              const image = event.currentTarget;
              setAspect(image.naturalWidth / image.naturalHeight);
            }}
          />
          {aspect !== null && (
            <div
              className="vs-cropper-frame"
              style={{
                left: `${crop.x * 100}%`,
                top: `${crop.y * 100}%`,
                width: `${crop.width * 100}%`,
                height: `${crop.height * 100}%`,
              }}
              onPointerDown={beginMove}
            >
              {CORNERS.map((corner) => (
                <span
                  key={corner}
                  className={`vs-cropper-handle ${corner}`}
                  onPointerDown={(event) => beginResize(event, corner)}
                />
              ))}
            </div>
          )}
        </div>
        {/* 操作は写真の横に置く。下に並べると、縦長の写真では画面の外へ押し出され、
            スクロールしないと「正方形にそろえる」や保存が見えない。 */}
        <div className="vs-cropper-side">
          {aspect !== null && (
            <>
              <span className="vs-muted">一覧での見え方</span>
              <CropPreview src={src} crop={crop} aspect={aspect} alt="一覧での見え方" />
            </>
          )}
          <p className="vs-muted">
            枠をドラッグして位置を、四隅をドラッグして大きさを変えます。
          </p>
          <label className="vs-cropper-square">
            <input
              type="checkbox"
              checked={square}
              disabled={busy || aspect === null}
              onChange={(event) => toggleSquare(event.target.checked)}
            />
            正方形にそろえる
          </label>
          <button type="button" className="vs-btn ghost" disabled={busy} onClick={resetToWhole}>
            全体に広げる
          </button>
          <div className="vs-row vs-cropper-actions">
            <button
              type="button"
              className="vs-btn"
              disabled={busy || aspect === null}
              onClick={() => onSave(isWholeImage(crop) ? null : crop)}
            >
              この範囲にする
            </button>
            <button type="button" className="vs-btn ghost" disabled={busy} onClick={onCancel}>
              やめる
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
