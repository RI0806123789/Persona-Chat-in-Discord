// M3 Expressive ならではの部品: 形が変わり続けるローディング表示と、波打つプログレスインジケーター。
// どちらも動きは theme/motion.ts のばね（Standard = 跳ねない）で付け、「動きを減らす」設定では止める。
import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import { useReducedMotion } from "../hooks";
import { MOTION_SCHEME, MOTION_SCHEMES, springPosition, type SpringSpec } from "../theme/motion";

const SPATIAL = MOTION_SCHEMES[MOTION_SCHEME].spatial;

// ---------------------------------------------------------------- ローディング表示（形のモーフィング）

// M3 Expressive の既定のローディング表示は、7つの形（ソフトバースト → クッキー9 → 五角形 → 錠剤 →
// サニー → クッキー4 → 楕円）を順に変形しながら回る。ここでは「花びらの数」と「へこみの深さ」で
// 近い形を作る（極座標の r(θ) = 1 + a·cos(nθ)。角度ごとに半径を補間すれば、どの形の間でも滑らかに変形できる）。
const SHAPES: { lobes: number; depth: number }[] = [
  { lobes: 10, depth: 0.08 }, // ソフトバースト
  { lobes: 9, depth: 0.12 }, // クッキー9
  { lobes: 5, depth: 0.1 }, // 五角形
  { lobes: 2, depth: 0.22 }, // 錠剤
  { lobes: 8, depth: 0.1 }, // サニー
  { lobes: 4, depth: 0.16 }, // クッキー4
  { lobes: 2, depth: 0.1 }, // 楕円
];
const SAMPLES = 96;
// 1つの形に留まる時間。変形そのものはばね（spatial default）で約0.3秒かけて進み、残りは形を保ったまま回る
const MORPH_MS = 650;
// 変形のたびに加わる回転と、常にかかっている回転
const ROTATION_PER_MORPH = Math.PI / 2;
const IDLE_ROTATION_MS = 1600;

function radii(shapeIndex: number): number[] {
  const { lobes, depth } = SHAPES[shapeIndex];
  return Array.from({ length: SAMPLES }, (_, i) => 1 - depth + depth * Math.cos((i / SAMPLES) * Math.PI * 2 * lobes));
}

const SHAPE_RADII = SHAPES.map((_, i) => radii(i));

function pathFor(values: number[], size: number, rotation: number, scale: number): string {
  const center = size / 2;
  const radius = size * scale;
  const points = values.map((r, i) => {
    const angle = (i / SAMPLES) * Math.PI * 2 + rotation;
    return [center + Math.cos(angle) * r * radius, center + Math.sin(angle) * r * radius];
  });
  return `M${points.map(([x, y]) => `${x.toFixed(2)} ${y.toFixed(2)}`).join("L")}Z`;
}

/**
 * 形が変わるローディング表示。size は外形（contained のときは丸い容器）の大きさ。
 * M3 の寸法（容器 48 に対して形 38）に合わせ、contained では形を容器より小さく描く。
 */
export function LoadingIndicator({ size = 48, contained = false, label = "読み込み中" }: { size?: number; contained?: boolean; label?: string }) {
  const pathRef = useRef<SVGPathElement>(null);
  const reduced = useReducedMotion();
  const scale = contained ? 0.32 : 0.42;

  useEffect(() => {
    const path = pathRef.current;
    if (!path) return;
    if (reduced) {
      path.setAttribute("d", pathFor(SHAPE_RADII[0], size, 0, scale));
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
      const t = springPosition(SPATIAL.default, (elapsed % MORPH_MS) / 1000);
      const values = from.map((r, i) => r + (to[i] - r) * t);
      const rotation = (step + t) * ROTATION_PER_MORPH + elapsed / IDLE_ROTATION_MS;
      path.setAttribute("d", pathFor(values, size, rotation, scale));
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [size, reduced, scale]);

  return (
    <span className={`loading-indicator ${contained ? "is-contained" : ""}`} role="progressbar" aria-label={label} style={{ width: size, height: size }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden="true">
        <path ref={pathRef} d={pathFor(SHAPE_RADII[0], size, 0, scale)} />
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
// 波が流れる速さ（1秒あたりの移動量 px）
const WAVE_SPEED = 24;
// M3 の既定と同じく、0〜10% と 95% 以上では波を平らにする（端の近くで波打つと、値が読みにくいため）
const FLAT_BELOW = 0.1;
const FLAT_ABOVE = 0.95;

function wavePath(from: number, to: number, middle: number, amplitude: number, phase: number): string {
  const parts: string[] = [];
  const end = Math.max(from, to);
  for (let x = from; ; x += 2) {
    const clamped = Math.min(x, end);
    const y = middle + Math.sin(((clamped - phase) / WAVE_LENGTH) * Math.PI * 2) * amplitude;
    parts.push(`${parts.length ? "L" : "M"}${clamped.toFixed(1)} ${y.toFixed(2)}`);
    if (clamped >= end) break;
  }
  return parts.join("");
}

/** 目標値へ、ばねで追いかける1次元の状態（requestAnimationFrame の中で進める）。 */
class SpringFollower {
  value: number;
  private velocity = 0;

  constructor(
    initial: number,
    private readonly spec: SpringSpec,
  ) {
    this.value = initial;
  }

  /** dt 秒だけ進め、止まったかどうかを返す。 */
  step(target: number, dt: number): boolean {
    const damping = 2 * this.spec.damping * Math.sqrt(this.spec.stiffness);
    // 大きな dt をそのまま使うと発散するので、細かく刻んで積分する
    for (let remaining = Math.min(dt, 0.1); remaining > 0; remaining -= 0.004) {
      const h = Math.min(0.004, remaining);
      this.velocity += (this.spec.stiffness * (target - this.value) - damping * this.velocity) * h;
      this.value += this.velocity * h;
    }
    const settled = Math.abs(target - this.value) < 0.0005 && Math.abs(this.velocity) < 0.001;
    if (settled) {
      this.value = target;
      this.velocity = 0;
    }
    return settled;
  }

  jump(value: number) {
    this.value = value;
    this.velocity = 0;
  }
}

/**
 * M3 Expressive の線形プログレスインジケーター（波打つ版）。
 * value を渡すと決定的（使用率など）、省略すると不確定（読み込み中）になる。
 * 決定的な場合、進んだ部分だけが波打って流れ、残りは平らな軌道と末尾の「停止インジケーター（点）」になる。
 * 値が変わると、進んだ部分の長さと波の高さがばねで滑らかに変わる。
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
  const reduced = useReducedMotion();
  const height = WAVE_AMPLITUDE * 2 + TRACK_HEIGHT + 2;
  const middle = height / 2;
  const indeterminate = value === undefined;
  const ratio = value === null || value === undefined ? 0 : Math.max(0, Math.min(1, value));
  const targetAmplitude = flat || ratio <= FLAT_BELOW || ratio >= FLAT_ABOVE ? 0 : 1;

  const activeRef = useRef<SVGPathElement>(null);
  const trackRef = useRef<SVGLineElement>(null);
  const progress = useRef(new SpringFollower(ratio, SPATIAL.slow));
  const amplitude = useRef(new SpringFollower(targetAmplitude, SPATIAL.default));
  // 波の位相の起点。値が変わるたびに効果を張り直しても、波が先頭へ飛ばないよう部品の寿命で固定する
  const phaseOrigin = useRef(performance.now());

  useEffect(() => {
    if (indeterminate || width <= 0) return;
    const draw = (phase: number) => {
      const activeEnd = width * progress.current.value;
      const active = activeRef.current;
      const track = trackRef.current;
      if (active) {
        const visible = activeEnd > TRACK_HEIGHT;
        active.style.display = visible ? "" : "none";
        if (visible) {
          active.setAttribute("d", wavePath(TRACK_HEIGHT / 2, activeEnd, middle, WAVE_AMPLITUDE * amplitude.current.value, phase));
        }
      }
      if (track) {
        const start = activeEnd + GAP + TRACK_HEIGHT / 2;
        const end = width - TRACK_HEIGHT * 1.5 - GAP;
        track.style.display = start < end ? "" : "none";
        track.setAttribute("x1", String(start));
        track.setAttribute("x2", String(end));
      }
    };

    if (reduced) {
      progress.current.jump(ratio);
      amplitude.current.jump(targetAmplitude);
      draw(0);
      return;
    }
    let frame = 0;
    let last = performance.now();
    const tick = (now: number) => {
      const dt = Math.max(0, (now - last) / 1000);
      last = now;
      progress.current.step(ratio, dt);
      amplitude.current.step(targetAmplitude, dt);
      // 波は進む向き（右）へ流れ続ける（位相をずらす）
      draw(((now - phaseOrigin.current) / 1000) * WAVE_SPEED);
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [indeterminate, width, ratio, targetAmplitude, reduced, middle]);

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
                <path className="wavy-active" d={wavePath(-WAVE_LENGTH, width * 0.45, middle, WAVE_AMPLITUDE, 0)} />
              </g>
            </>
          ) : (
            <>
              <path ref={activeRef} className="wavy-active" />
              <line ref={trackRef} className="wavy-track" y1={middle} y2={middle} />
              <circle className="wavy-stop" cx={width - TRACK_HEIGHT / 2} cy={middle} r={TRACK_HEIGHT / 2} />
            </>
          )}
        </svg>
      )}
    </div>
  );
}
