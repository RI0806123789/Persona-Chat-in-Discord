import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
// フォントとアイコンはすべて npm に同梱のファイルから読む（CDN は使わない）
import "@fontsource-variable/roboto-flex/full.css";
import "@fontsource-variable/noto-sans-jp";
import "material-symbols/rounded.css";
import App from "./App";
import "./styles.css";
import { SEED_OPTIONS, applyTheme } from "./theme/theme";

// 最初の描画より前に配色を決める（一瞬だけ色の無い画面が出ないように）
function initialSeedHex(): string {
  try {
    const saved = JSON.parse(window.localStorage.getItem("pbc.seed") ?? "null") as string | null;
    return (SEED_OPTIONS.find((seed) => seed.id === saved) ?? SEED_OPTIONS[0]).hex;
  } catch {
    return SEED_OPTIONS[0].hex;
  }
}
applyTheme(initialSeedHex());

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
