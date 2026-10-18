import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

function compile(source, modules) {
  const exports = {};
  const output = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022,
  } }).outputText;
  new Function("require", "exports", output)((name) => modules[name] ?? {}, exports);
  return exports;
}

const source = async (name) => (await readFile(new URL(`../src/${name}`, import.meta.url), "utf8")).replaceAll("\r\n", "\n");
const scopes = compile(await source("requestScope.ts"), {});
const appSource = await source("App.tsx");
const dataSource = await source("pages/DataPage.tsx");
const privacySource = await source("privacy.ts");

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

// Reactの状態とEffectの境界だけを置き換え、実際の取得・切断コールバックを実行する。
function renderer() {
  const state = [];
  const effects = [];
  let index = 0;
  const hooks = {
    useState(initial) {
      const slot = index++;
      if (!(slot in state)) state[slot] = typeof initial === "function" ? initial() : initial;
      return [state[slot], (next) => { state[slot] = typeof next === "function" ? next(state[slot]) : next; }];
    },
    useRef(initial) {
      const slot = index++;
      return state[slot] ??= { current: initial };
    },
    useMemo: (factory) => factory(),
    useCallback: (callback) => callback,
    useEffect(callback, dependencies) {
      const slot = index++;
      const previous = state[slot];
      if (!previous || dependencies.some((value, i) => value !== previous.dependencies[i])) {
        effects.push(() => { previous?.cleanup?.(); state[slot] = { dependencies, cleanup: callback() }; });
      }
    },
    createContext: (value) => ({ value }),
    useContext: (context) => context.value,
  };
  return { hooks, render: (component, props) => { index = 0; return component(props); },
    commit: () => { effects.splice(0).forEach((effect) => effect()); } };
}

const connection = (id) => ({ connected: !!id, profile: id ? { id, label: `架空の${id}` } : null, connected_at: id ? 1 : null });

function consoleHarness(api) {
  const runtime = renderer();
  const notices = [];
  const privacy = compile(privacySource, { react: runtime.hooks });
  const modules = {
    react: runtime.hooks,
    "react/jsx-runtime": { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) },
    "./hooks": { useStoredState: (_key, initial) => runtime.hooks.useState(initial), useNow: () => 0,
      useMediaQuery: () => true, useLogStream: () => ({}), usePolling: () => {} },
    "./api": { api, errorMessage: (error) => error.message, ApiError: Error },
    "./components/toast": { useToast: () => (notice) => notices.push(notice) },
    "./privacy": privacy, "./requestScope": scopes,
    "./navigation": { PAGES: [{ id: "overview" }], isPageId: () => true },
    "./profiles": { unsetFieldsOf: () => [] },
    "./theme/theme": { SEED_OPTIONS: [{ id: "test", hex: "#123456" }] },
  };
  const exposed = "return {applyConnection, refreshStatus, refreshInsights, refreshDeploy, handleConnect, handleDisconnect, runAction, status, insights, deploy, statusError, insightsError, setSelectedProfileId, setProfiles, setPrivacyOn, notify};";
  const instrumented = appSource.replace("function Console() {", "export function Console() {")
    .replace("  return (\n    <PrivacyContext.Provider", `  ${exposed}\n  return (\n    <PrivacyContext.Provider`);
  assert.ok(instrumented.includes(exposed));
  const component = compile(instrumented, modules).Console;
  return { render: () => runtime.render(component), notices, privacy };
}

test("切断完了後に古い状態・インサイト・デプロイの成功やエラーを反映しない", async () => {
  for (const fail of [false, true]) {
    const status = deferred(), insights = deferred(), deploy = deferred();
    const api = { status: () => status.promise, insights: () => insights.promise, deploy: () => deploy.promise,
      disconnect: async () => connection(null), connection: async () => connection(null) };
    const harness = consoleHarness(api);
    harness.render().applyConnection(connection("A"));
    const current = harness.render();
    const pending = [current.refreshStatus(), current.refreshInsights(), current.refreshDeploy()];
    await current.handleDisconnect();
    if (fail) for (const response of [status, insights, deploy]) response.reject(new Error("古い接続のエラー"));
    else { status.resolve({ fetched_at: 1 }); insights.resolve({ data: { old: true }, fetched_at: 1 });
      deploy.resolve({ deploy: { profile_id: "A" }, running: true }); }
    await Promise.all(pending);
    const after = harness.render();
    assert.equal(after.status, null);
    assert.equal(after.insights, null);
    assert.equal(after.deploy, null);
    assert.equal(after.statusError, null);
    assert.equal(after.insightsError, null);
  }
});

test("切り替え前に予約したサービス操作後のタイマーは新しい接続で取得しない", async () => {
  const timers = [];
  const original = globalThis.window;
  globalThis.window = { setTimeout: (callback) => timers.push(callback) };
  try {
    let statusCalls = 0;
    const api = { service: async () => ({ ok: true, message: "架空の完了" }), operations: async () => ({ operations: [] }),
      status: async () => { statusCalls++; return { fetched_at: 1 }; } };
    const harness = consoleHarness(api);
    harness.render().applyConnection(connection("A"));
    await harness.render().runAction("restart", { profile_id: "A", connected_at: 1 });
    await Promise.resolve();
    assert.equal(timers.length, 1);
    const before = statusCalls;
    harness.render().applyConnection(connection("B"));
    timers[0]();
    await Promise.resolve();
    assert.equal(statusCalls, before);
  } finally { globalThis.window = original; }
});

test("切断の応答を待っている間に届いた状態も破棄する", async () => {
  const status = deferred(), disconnect = deferred();
  const harness = consoleHarness({ status: () => status.promise, disconnect: () => disconnect.promise,
    connection: async () => connection(null) });
  harness.render().applyConnection(connection("A"));
  const current = harness.render();
  const pending = current.refreshStatus();
  const closing = current.handleDisconnect();
  status.resolve({ fetched_at: 1 });
  await pending;
  assert.equal(harness.render().status, null);
  disconnect.resolve(connection(null));
  await closing;
});

test("同じ接続内でも後から開始した取得を古い応答が上書きしない", async () => {
  const old = deferred(), fresh = deferred();
  let calls = 0;
  const harness = consoleHarness({ insights: () => (++calls === 1 ? old.promise : fresh.promise) });
  harness.render().applyConnection(connection("A"));
  const current = harness.render();
  const first = current.refreshInsights(), second = current.refreshInsights();
  fresh.resolve({ data: { value: "新しい結果", discord: { guilds: [] } }, fetched_at: 2 });
  await second;
  old.resolve({ data: { value: "古い結果", discord: { guilds: [] } }, fetched_at: 1 });
  await first;
  assert.equal(harness.render().insights.value, "新しい結果");
});

test("データ画面は切り替え前のDB集計を破棄し、切断後も再表示しない", async () => {
  const runtime = renderer();
  const old = deferred(), fresh = deferred();
  let calls = 0;
  const instrumented = dataSource.replace("  if (!connected) {", "  return {summary, loading, error};\n  if (!connected) {");
  const component = compile(instrumented, { react: runtime.hooks, "../api": {
    api: { db: () => (++calls === 1 ? old.promise : fresh.promise) }, errorMessage: (error) => error.message,
  }, "../privacy": { usePrivacy: () => ({}) } }).DataPage;
  const render = (connected, connectionKey) => runtime.render(component, { connected, connectionKey, refreshToken: 0 });
  render(true, "A"); runtime.commit();
  render(true, "B"); runtime.commit();
  fresh.resolve({ value: "新しいDB" }); await fresh.promise; await Promise.resolve();
  old.resolve({ value: "古いDB" }); await old.promise; await Promise.resolve();
  assert.equal(render(true, "B").summary.value, "新しいDB");
  render(false, ""); runtime.commit();
  assert.equal(render(false, "").summary, null);
});

test("データ画面は接続先が変わったら、開いていたテーブルの中身を閉じる", () => {
  const runtime = renderer();
  const instrumented = dataSource.replace("  if (!connected) {", "  return {openTable, selectTable};\n  if (!connected) {");
  assert.notEqual(instrumented, dataSource);
  const component = compile(instrumented, { react: runtime.hooks, "../api": {
    api: { db: () => new Promise(() => {}) }, errorMessage: (error) => error.message,
  }, "../privacy": { usePrivacy: () => ({}) } }).DataPage;
  const render = (connectionKey) => runtime.render(component, { connected: true, connectionKey, refreshToken: 0 });
  const originalFrame = globalThis.requestAnimationFrame;
  globalThis.requestAnimationFrame = () => 0; // スクロールは描画の後なので、ここでは呼ばない
  try {
    render("A"); runtime.commit();
    render("A").selectTable("feedback");
    assert.equal(render("A").openTable, "feedback");
    render("A").selectTable("feedback");
    assert.equal(render("A").openTable, null, "同じテーブルをもう一度選ぶと閉じる");
    render("A").selectTable("reminders");
    runtime.commit();
    render("B"); runtime.commit();
    assert.equal(render("B").openTable, null);
  } finally {
    globalThis.requestAnimationFrame = originalFrame;
  }
});

test("SSH設定に任せる場合もホーム所有者・パス・ラベルを伏せ字にする", () => {
  const harness = consoleHarness({});
  for (const home of ["home", "Users"]) {
    const path = ["", home, "FICTIONAL_OWNER", "project"].join("/");
    const profile = { host: "host.invalid", username: null, label: "架空の接続先", bot_dir: path };
    const secrets = harness.privacy.collectSecrets({ profile, profiles: [], connection: null, insights: null });
    const privacy = harness.privacy.createPrivacy(true, secrets);
    assert.equal(privacy.redact(`FICTIONAL_OWNER: ${path}`), `${harness.privacy.MASK}: ${harness.privacy.MASK}`);
    assert.equal(privacy.redact(profile.label), harness.privacy.MASK);
    assert.equal(harness.privacy.createPrivacy(false, secrets).redact(path), path);
  }
});

test("伏せ字モードで通知の見出しと詳細、短いホーム名のパスも隠す", () => {
  const harness = consoleHarness({});
  const home = ["", "home", "ab"].join("/");
  const profile = { host: "host.invalid", username: null, label: "架空の接続先", bot_dir: `${home}/project` };
  harness.render().setProfiles([profile]);
  harness.render().setPrivacyOn(true);
  harness.render().notify({ title: `架空の接続先 host.invalid`, detail: `${home}/.ssh/config` });
  const notice = harness.notices.at(-1);
  assert.equal(notice.title, `${harness.privacy.MASK} ${harness.privacy.MASK}`);
  assert.equal(notice.detail, `${harness.privacy.MASK}/.ssh/config`);
});

test("ログが途切れたときのツールチップにも伏せ字を適用する", async () => {
  const privacy = { redact: (text) => text.replaceAll("FICTIONAL_HOST", "MASKED") };
  const sourceText = (await source("pages/LogsPage.tsx")).replace("function StreamIndicator(", "export function StreamIndicator(");
  const jsx = (type, props) => ({ type, props });
  const component = compile(sourceText, { "../privacy": { usePrivacy: () => privacy },
    "react/jsx-runtime": { jsx, jsxs: jsx }, "../logs": { LEVEL_LABELS: {} } }).StreamIndicator;
  for (const status of ["error", "ended"]) {
    const node = component({ stream: { status, paused: false, message: "FICTIONAL_HOST の接続が切れました" } });
    assert.equal(node.props.title, "MASKED の接続が切れました");
  }
});
