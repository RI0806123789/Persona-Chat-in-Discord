// M3 Expressive ならではの部品: 形が変わり続けるローディング表示と、波打つプログレスインジケーター。
import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from "react";

// ---------------------------------------------------------------- ローディング表示（形のモーフィング）

// M3 Expressive の既定のローディング表示は、7つの形（ソフトバースト → クッキー9 → 五角形 → 錠剤 →
// サニー → クッキー4 → 楕円）を順に変形しながら回る。ここでは「花びらの数」と「へこみの深さ」で
// 近い形を作る（極座標の r(θ) = 1 + a·cos(nθ)。角度ごとに半径を補間すれば、どの形の間でも滑らかに変形できる）。
const SHAPES: { lobes: number; depth: number; stretch?: number }[] = [
  { lobes: 10, depth: 0.08 }, // ソフトバースト
  { lobes: 9, depth: 0.12 }, // クッキー9
  { lobes: 5, depth: 0.1 }, // 五角形
  { lobes: 2, depth: 0.22 }, // 錠剤
  { lobes: 8, depth: 0.1 }, // サニー
  { lobes: 4, depth: 0.16 }, // クッキー4
  { lobes: 2, depth: 0.1 }, // 楕円
];
const SAMPLES = 96;
const MORPH_MS = 650;

function radii(shapeIndex: number): number[] {
  const { lobes, depth } = SHAPES[shapeIndex];
  return Array.from({ length: SAMPLES }, (_, i) => 1 - depth + depth * Math.cos((i / SAMPLES) * Math.PI * 2 * lobes));
}

const SHAPE_RADII = SHAPES.map((_, i) => radii(i));

function pathFor(values: number[], size: number, rotation: number): string {
  const center = size / 2;
  const scale = size * 0.42;
  const points = values.map((r, i) => {
    const angle = (i / SAMPLES) * Math.PI * 2 + rotation;
    return [center + Math.cos(angle) * r * scale, center + Math.sin(angle) * r * scale];
  });
  return `M${points.map(([x, y]) => `${x.toFixed(2)} ${y.toFixed(2)}`).join("L")}Z`;
}

// ばね（減衰比 0.9 = ほぼ行き過ぎない）に近い進み方
function springProgress(t: number): number {
  const omega = 9;
  const zeta = 0.9;
  const wd = omega * Math.sqrt(1 - zeta * zeta);
  return 1 - Math.exp(-zeta * omega * t) * (Math.cos(wd * t) + (zeta / Math.sqrt(1 - zeta * zeta)) * Math.sin(wd * t));
}

export function LoadingIndicator({ size = 48, contained = false, label = "読み込み中" }: { size?: number; contained?: boolean; label?: string }) {
  const pathRef = useRef<SVGPathElement>(null);

  useEffect(() => {
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const path = pathRef.current;
    if (!path) return;
    if (reduced) {
      path.setAttribute("d", pathFor(SHAPE_RADII[0], size, 0));
      return;
    }
    let frame = 0;
    const started = performance.now();
    const tick = (now: number) => {
      // requestAnimationFrame の時刻は「そのフレームの開始時刻」なので、started より少し前になることがある。
      // 負のまま使うと形の番号が -1 になり、存在しない形を読んで例外でループが止まる
      const elapsed = Math.max(0, now - started);
      const step = Math.floor(elapsed / MORPH_MS);
      const from = SHAPE_RADII[step % SHAPES.length];
      const to = SHAPE_RADII[(step + 1) % SHAPES.length];
      const t = Math.min(1, springProgress(((elapsed % MORPH_MS) / MORPH_MS) * 1.4));
      const values = from.map((r, i) => r + (to[i] - r) * t);
      // 形を変えるたびに少しずつ回る（1回の変形で 90°）
      const rotation = (step + t) * (Math.PI / 2) + elapsed / 1600;
      path.setAttribute("d", pathFor(values, size, rotation));
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [size]);

  return (
    <span className={`loading-indicator ${contained ? "is-contained" : ""}`} role="progressbar" aria-label={label} style={{ width: size, height: size }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden="true">
        <path ref={pathRef} d={pathFor(SHAPE_RADII[0], size, 0)} />
      </svg>
    </span>
  );
}

// ---------------------------------------------------------------- 波打つプログレスインジケーター

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.round(entry.contentRect.width)));
    observer.observe(element);
    setWidth(Math.round(element.getBoundingClientRect().width));
    return () => observer.disconnect();
  }, []);
  return [ref, width] as const;
}

const WAVE_LENGTH = 40;
const WAVE_AMPLITUDE = 3;
const TRACK_HEIGHT = 4;
const GAP = 4;

function wavePath(from: number, to: number, height: number): string {
  const middle = height / 2;
  const parts: string[] = [];
  for (let x = from; x <= to; x += 2) {
    const y = middle + Math.sin((x / WAVE_LENGTH) * Math.PI * 2) * WAVE_AMPLITUDE;
    parts.push(`${parts.length ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(2)}`);
  }
  return parts.join("");
}

/**
 * M3 Expressive の線形プログレスインジケーター（波打つ版）。
 * value を渡すと決定的（使用率など）、省略すると不確定（読み込み中）になる。
 * 決定的な場合、進んだ部分だけが波打ち、残りは平らな軌道と末尾の「停止インジケーター（点）」になる。
 */
export function WavyProgress({
  value,
  tone = "primary",
  label,
  flat = false,
}: {
  value?: number | null;
  tone?: "primary" | "warning" | "error";
  label: string;
  flat?: boolean;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const height = WAVE_AMPLITUDE * 2 + TRACK_HEIGHT + 2;
  const middle = height / 2;
  const indeterminate = value === undefined;
  const ratio = value === null || value === undefined ? 0 : Math.max(0, Math.min(1, value));
  const activeEnd = Math.max(0, width * ratio);

  return (
    <div
      ref={ref}
      className={`wavy-progress tone-${tone} ${indeterminate ? "is-indeterminate" : ""}`}
      role="progressbar"
      aria-label={label}
      aria-valuemin={indeterminate ? undefined : 0}
      aria-valuemax={indeterminate ? undefined : 100}
      aria-valuenow={indeterminate ? undefined : Math.round(ratio * 100)}
      style={{ height }}
    >
      {width > 0 && (
        <svg width={width} height={height} aria-hidden="true" style={{ "--w": width } as CSSProperties}>
          {indeterminate ? (
            <>
              <line className="wavy-track" x1={0} x2={width} y1={middle} y2={middle} />
              <g className="wavy-moving">
                <path className="wavy-active" d={wavePath(-WAVE_LENGTH, width * 0.45, height)} />
              </g>
            </>
          ) : (
            <>
              {activeEnd > 2 &&
                (flat ? (
                  <line className="wavy-active" x1={TRACK_HEIGHT / 2} x2={activeEnd} y1={middle} y2={middle} />
                ) : (
                  <path className="wavy-active" d={wavePath(TRACK_HEIGHT / 2, activeEnd, height)} />
                ))}
              {activeEnd + GAP < width - TRACK_HEIGHT && (
                <line className="wavy-track" x1={activeEnd + GAP + TRACK_HEIGHT / 2} x2={width - TRACK_HEIGHT / 2} y1={middle} y2={middle} />
              )}
              <circle className="wavy-stop" cx={width - TRACK_HEIGHT / 2} cy={middle} r={TRACK_HEIGHT / 2} />
            </>
          )}
        </svg>
      )}
    </div>
  );
}
