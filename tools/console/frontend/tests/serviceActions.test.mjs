import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = async (name) => readFile(new URL(`../src/${name}`, import.meta.url), "utf8");
function compile(text, modules) {
  const exports = {};
  const output = ts.transpileModule(text, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022,
  } }).outputText;
  new Function("require", "exports", output)((name) => modules[name] ?? {}, exports);
  return exports;
}

const status = compile(await source("status.ts"), {});
const ui = Object.fromEntries(["Button", "Card", "EmptyState", "StatusChip", "Chip", "InlineError", "Notice", "Sparkline", "SplitButton", "Stat"]
  .map((name) => [name, name]));
const modules = {
  "react/jsx-runtime": { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) },
  "../status": status, "../components/ui": ui, "../components/Icon": { Icon: "Icon" },
  "../components/expressive": { LoadingIndicator: "LoadingIndicator", WavyProgress: "WavyProgress" },
  "../privacy": { usePrivacy: () => ({ redact: (value) => value }) },
  "../format": new Proxy({}, { get: () => () => "架空の表示値" }),
  "./DeployCard": { DeployCard: "DeployCard" }, "./shared": { NotConnected: "NotConnected" },
};
const { OperationsPage } = compile(await source("pages/OperationsPage.tsx"), modules);
const { OverviewPage } = compile(await source("pages/OverviewPage.tsx"), modules);

function nodes(node) {
  if (Array.isArray(node)) return node.flatMap(nodes);
  if (!node || typeof node !== "object") return [];
  return [node, ...nodes(node.props?.children)];
}

function stopControl(page, service, overrides = {}) {
  const actions = [];
  const props = { connected: true, connecting: false, selectedProfile: null, profile: null, service,
    busyAction: null, deployRunning: false, operations: [], git: null, deploy: null,
    status: service ? { service } : null, samples: [], logs: [], connection: null, insights: null,
    statusError: null, now: 0, onNavigate: () => {}, onRequestAction: (action) => actions.push(action), ...overrides };
  if (page === "operations") {
    const stop = nodes(OperationsPage(props)).find((node) => node.type === "Button" && node.props.icon === "stop");
    return { disabled: !stop || !!stop.props.disabled, click: () => stop.props.onClick(), actions };
  }
  const hero = nodes(OverviewPage(props)).find((node) => node.type?.name === "ServiceHero");
  if (!hero) return { disabled: true, actions };
  const split = nodes(hero.type(hero.props)).find((node) => node.type === "SplitButton");
  const stop = split.props.items.find((item) => item.label === "停止");
  return { disabled: !!(split.props.disabled || split.props.busy || stop.disabled), click: () => stop.onSelect(), actions };
}

const service = (active_state, sub_state, load_state = "loaded") => ({
  name: "fictional-bot", active_state, sub_state, load_state, main_pid: 0,
});

test("再起動待ち・起動中・再読み込み中から両画面の停止操作へ進める", () => {
  for (const [active, sub] of [["active", "running"], ["activating", "auto-restart"], ["activating", "start"], ["reloading", "reload"]]) {
    const current = service(active, sub);
    assert.equal(status.canStopService(current), true);
    for (const page of ["operations", "overview"]) {
      const control = stopControl(page, current);
      assert.equal(control.disabled, false, `${page}: ${active}/${sub}`);
      control.click();
      assert.deepEqual(control.actions, ["stop"]);
    }
  }
  // マスク後も既に動いているサービスは停止できる。
  assert.equal(status.canStopService(service("active", "running", "masked")), true);
});

test("停止済み・停止処理中・未登録・状態不明からの停止は両画面で無効", () => {
  for (const current of [null, service("inactive", "dead"), service("failed", "failed"),
    service("deactivating", "stop"), service("unknown", "unknown"), service("active", "running", "not-found")]) {
    assert.equal(status.canStopService(current), false);
    for (const page of ["operations", "overview"]) assert.equal(stopControl(page, current).disabled, true);
  }
});

test("接続前・操作中・デプロイ中の停止ガードを両画面で維持する", () => {
  const current = service("activating", "auto-restart");
  for (const overrides of [{ connected: false }, { busyAction: "start" }, { busyAction: "stop" }, { deployRunning: true }]) {
    for (const page of ["operations", "overview"]) assert.equal(stopControl(page, current, overrides).disabled, true);
  }
});
