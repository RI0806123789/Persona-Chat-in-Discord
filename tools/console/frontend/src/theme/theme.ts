// Material 3 Expressive の配色を、基準の1色（シード色）からその場で作る。
// Google 公式の material-color-utilities の SchemeExpressive（2025 年版の仕様）を使う。
import {
  DynamicScheme,
  Hct,
  MaterialDynamicColors,
  SchemeExpressive,
  argbFromHex,
  customColor,
  hexFromArgb,
} from "@material/material-color-utilities";

export interface SeedOption {
  id: string;
  label: string;
  hex: string;
}

// 選べるシード色（Discord の青紫を既定にする）
export const SEED_OPTIONS: SeedOption[] = [
  { id: "blurple", label: "ブループル（Discord）", hex: "#5865F2" },
  { id: "teal", label: "ティール", hex: "#00897B" },
  { id: "rose", label: "ローズ", hex: "#D0436E" },
  { id: "amber", label: "アンバー", hex: "#C77700" },
  { id: "violet", label: "バイオレット", hex: "#7E57C2" },
];

// M3 の色の役割（CSS 変数 --md-sys-color-<名前> になる）
const ROLES = [
  "background", "onBackground",
  "surface", "surfaceDim", "surfaceBright",
  "surfaceContainerLowest", "surfaceContainerLow", "surfaceContainer", "surfaceContainerHigh", "surfaceContainerHighest",
  "onSurface", "surfaceVariant", "onSurfaceVariant",
  "inverseSurface", "inverseOnSurface", "inversePrimary",
  "outline", "outlineVariant", "shadow", "scrim", "surfaceTint",
  "primary", "onPrimary", "primaryContainer", "onPrimaryContainer",
  "secondary", "onSecondary", "secondaryContainer", "onSecondaryContainer",
  "tertiary", "onTertiary", "tertiaryContainer", "onTertiaryContainer",
  "error", "onError", "errorContainer", "onErrorContainer",
] as const;

// 状態を表す色（正常・注意・停止）。M3 の「カスタムカラー」としてテーマの色になじませる。
// 元の色はグラフ用に検証済みの状態パレット（good / warning / serious）。
const CUSTOM_COLORS = [
  { name: "success", hex: "#0ca30c" },
  { name: "warning", hex: "#fab219" },
  { name: "serious", hex: "#ec835a" },
];

function kebab(name: string): string {
  return name.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`);
}

function schemeVariables(seed: Hct, isDark: boolean): string {
  const scheme: DynamicScheme = new SchemeExpressive(seed, isDark, 0, "2025");
  const colors = new MaterialDynamicColors();
  const lines: string[] = [];
  for (const role of ROLES) {
    const dynamicColor = colors[role]();
    lines.push(`--md-sys-color-${kebab(role)}: ${hexFromArgb(dynamicColor.getArgb(scheme))};`);
  }
  // カスタムカラー: 公式の customColor で、色相をシード色へ寄せ（harmonize）、M3 の規則のトーンで4色を作る
  for (const custom of CUSTOM_COLORS) {
    const group = customColor(seed.toInt(), { name: custom.name, value: argbFromHex(custom.hex), blend: true });
    const roles = isDark ? group.dark : group.light;
    lines.push(`--md-custom-${custom.name}: ${hexFromArgb(roles.color)};`);
    lines.push(`--md-custom-on-${custom.name}: ${hexFromArgb(roles.onColor)};`);
    lines.push(`--md-custom-${custom.name}-container: ${hexFromArgb(roles.colorContainer)};`);
    lines.push(`--md-custom-on-${custom.name}-container: ${hexFromArgb(roles.onColorContainer)};`);
  }
  // ログの端末はどちらのテーマでも暗い面にする（ライトでは inverse-surface、ダークでは最も暗い面）
  const surfaceLowest = hexFromArgb(colors.surfaceContainerLowest().getArgb(scheme));
  const inverseSurface = hexFromArgb(colors.inverseSurface().getArgb(scheme));
  const inverseOn = hexFromArgb(colors.inverseOnSurface().getArgb(scheme));
  const onSurface = hexFromArgb(colors.onSurface().getArgb(scheme));
  lines.push(`--app-terminal-bg: ${isDark ? surfaceLowest : inverseSurface};`);
  lines.push(`--app-terminal-ink: ${isDark ? onSurface : inverseOn};`);
  // グラフの色は、色覚の違いに配慮して検証済みのパレットを使う（テーマの色からは作らない）
  lines.push(`--chart-series-1: ${isDark ? "#3987e5" : "#2a78d6"};`);
  lines.push(`--chart-bad: ${isDark ? "#e66767" : "#e34948"};`);
  return lines.join("\n  ");
}

/** シード色からライト / ダーク両方の CSS を作り、<style id="m3-theme"> に書き込む。 */
export function applyTheme(seedHex: string): void {
  const seed = Hct.fromInt(argbFromHex(seedHex));
  const light = schemeVariables(seed, false);
  const dark = schemeVariables(seed, true);
  const css = `
:root {
  color-scheme: light;
  ${light}
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    ${dark}
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  ${dark}
}`;
  let element = document.getElementById("m3-theme") as HTMLStyleElement | null;
  if (!element) {
    element = document.createElement("style");
    element.id = "m3-theme";
    document.head.appendChild(element);
  }
  element.textContent = css;
}
