import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
// フォントとアイコンはすべて npm に同梱のファイルから読む（CDN は使わない）
import "@fontsource-variable/roboto-flex/full.css";
import "@fontsource-variable/noto-sans-jp";
import "material-symbols/rounded.css";
import App from "./App";
import "./styles.css";
import { applyMotion } from "./theme/motion";
import { SEED_OPTIONS, applyTheme } from "./theme/theme";

function readStored(key: string): unknown {
  try {
    return JSON.parse(window.localStorage.getItem(key) ?? "null");
  } catch {
    return null;
  }
}

// 最初の描画より前に、配色・ライト / ダーク・動きを決める（一瞬だけ違う見た目が出ないように）
const savedSeed = readStored("pbc.seed");
applyTheme((SEED_OPTIONS.find((seed) => seed.id === savedSeed) ?? SEED_OPTIONS[0]).hex);
const savedTheme = readStored("pbc.theme");
if (savedTheme === "light" || savedTheme === "dark") document.documentElement.dataset.theme = savedTheme;
applyMotion();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
