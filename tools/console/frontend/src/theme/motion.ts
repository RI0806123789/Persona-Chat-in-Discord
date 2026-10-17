// M3 Expressive の動き（モーションスキーム）を、ばね（spring）の物理計算から作る。
// ばねの軌跡を時間で区切って CSS の linear() に変換し、<style id="m3-motion"> に書き込む。
//
// 使い方（CSS）: transition: transform var(--spring-spatial-default);
//   変数の中身は「所要時間 + linear(...)」なので、1つで時間と緩急の両方が決まる。
//   遅延などで時間だけが要るときは var(--spring-spatial-default-duration) を使う。

export type SpringKind = "spatial" | "effects";
export type SpringSpeed = "fast" | "default" | "slow";
export type MotionSchemeName = "standard" | "expressive";

export interface SpringSpec {
  /** 減衰比（1 未満で行き過ぎて戻る。1 で行き過ぎない） */
  damping: number;
  /** ばねの強さ（質量 1 として。大きいほど速く止まる） */
  stiffness: number;
}

type SchemeSpecs = Record<SpringKind, Record<SpringSpeed, SpringSpec>>;

// M3 Expressive のモーションスキーム（Jetpack Compose の MotionScheme と同じ値）。
// spatial = 位置・大きさ・形の変化、effects = 色・透明度の変化（effects は跳ねさせない）
const EFFECTS: SchemeSpecs["effects"] = {
  fast: { damping: 1, stiffness: 3800 },
  default: { damping: 1, stiffness: 1600 },
  slow: { damping: 1, stiffness: 800 },
};

export const MOTION_SCHEMES: Record<MotionSchemeName, SchemeSpecs> = {
  standard: {
    spatial: {
      fast: { damping: 0.9, stiffness: 1400 },
      default: { damping: 0.9, stiffness: 700 },
      slow: { damping: 0.9, stiffness: 300 },
    },
    effects: EFFECTS,
  },
  expressive: {
    spatial: {
      fast: { damping: 0.6, stiffness: 800 },
      default: { damping: 0.8, stiffness: 380 },
      slow: { damping: 0.8, stiffness: 200 },
    },
    effects: EFFECTS,
  },
};

/**
 * 使うモーションスキーム。利用者の好み（バウンドしない「ぬるっと」止まる動き）に合わせ、
 * 跳ねない Standard（減衰比 0.9 = 行き過ぎ約 0.15%）にしている。
 * "expressive" に差し替えると、全体が跳ねる動きに切り替わる（CSS 側の変更は要らない）。
 */
export const MOTION_SCHEME: MotionSchemeName = "standard";

// 目標との差がこれより小さくなったら「止まった」とみなす（所要時間の決め方）
const SETTLE_THRESHOLD = 0.001;
// linear() に並べる点の数（多いほど軌跡に忠実になるが、CSS が長くなる）
const CURVE_POINTS = 40;
const MAX_SECONDS = 5;
const STEP_SECONDS = 0.0005;

/** 0 から 1 へ動くばねの、時刻 t 秒での位置（初速 0・質量 1）。 */
export function springPosition({ damping, stiffness }: SpringSpec, t: number): number {
  if (t <= 0) return 0;
  const omega = Math.sqrt(stiffness);
  if (damping < 1) {
    const dampedOmega = omega * Math.sqrt(1 - damping * damping);
    const decay = Math.exp(-damping * omega * t);
    return 1 - decay * (Math.cos(dampedOmega * t) + ((damping * omega) / dampedOmega) * Math.sin(dampedOmega * t));
  }
  if (damping === 1) {
    return 1 - Math.exp(-omega * t) * (1 + omega * t);
  }
  // 過減衰（damping > 1）。今のスキームでは使わないが、トークンを差し替えても壊れないようにしておく
  const root = Math.sqrt(damping * damping - 1);
  const r1 = -omega * (damping - root);
  const r2 = -omega * (damping + root);
  return 1 - (r2 * Math.exp(r1 * t) - r1 * Math.exp(r2 * t)) / (r2 - r1);
}

/** ばねが止まる（目標との差が SETTLE_THRESHOLD を下回ったまま戻らない）までの秒数。 */
export function settleSeconds(spec: SpringSpec): number {
  let lastMoving = 0;
  for (let t = 0; t <= MAX_SECONDS; t += STEP_SECONDS) {
    if (Math.abs(1 - springPosition(spec, t)) >= SETTLE_THRESHOLD) lastMoving = t;
  }
  return lastMoving + STEP_SECONDS;
}

/** 目標を行き過ぎる量の最大値（0.0015 = 0.15%）。 */
export function overshootOf(spec: SpringSpec): number {
  const end = settleSeconds(spec);
  let peak = 0;
  for (let t = 0; t <= end; t += STEP_SECONDS) peak = Math.max(peak, springPosition(spec, t));
  return Math.max(0, peak - 1);
}

export interface SpringCurve {
  durationMs: number;
  easing: string;
}

/** ばねの軌跡を、止まるまでの時間で等分して CSS の linear() にする。 */
export function springToLinear(spec: SpringSpec): SpringCurve {
  const seconds = settleSeconds(spec);
  const points = Array.from({ length: CURVE_POINTS + 1 }, (_, index) => {
    if (index === CURVE_POINTS) return "1";
    const value = springPosition(spec, (seconds * index) / CURVE_POINTS);
    return String(Math.round(value * 10000) / 10000);
  });
  return { durationMs: Math.round(seconds * 1000), easing: `linear(${points.join(", ")})` };
}

/** 指定したスキームの全トークンを CSS 変数として書き込む。 */
export function applyMotion(name: MotionSchemeName = MOTION_SCHEME): void {
  const scheme = MOTION_SCHEMES[name];
  const lines: string[] = [];
  for (const kind of ["spatial", "effects"] as const) {
    for (const speed of ["fast", "default", "slow"] as const) {
      const { durationMs, easing } = springToLinear(scheme[kind][speed]);
      lines.push(`--spring-${kind}-${speed}: ${durationMs}ms ${easing};`);
      lines.push(`--spring-${kind}-${speed}-duration: ${durationMs}ms;`);
      lines.push(`--spring-${kind}-${speed}-easing: ${easing};`);
    }
  }
  let element = document.getElementById("m3-motion") as HTMLStyleElement | null;
  if (!element) {
    element = document.createElement("style");
    element.id = "m3-motion";
    document.head.appendChild(element);
  }
  element.dataset.scheme = name;
  element.textContent = `:root {\n  ${lines.join("\n  ")}\n}`;
}
