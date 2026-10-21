import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../src/hooks.ts", import.meta.url), "utf8");
const output = ts.transpileModule(source, { compilerOptions: {
  module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
} }).outputText;

// Reactの状態とEffect、WebSocket、時計だけを置き換え、実際のフックのイベント処理を実行する。
function harness(t, { throws = false } = {}) {
  const slots = [];
  const effects = [];
  const sockets = [];
  const timeouts = new Map();
  const intervals = new Map();
  let cursor = 0;
  let timer = 0;
  let enabled = true;
  let key = "first";
  const previous = { window: globalThis.window, WebSocket: globalThis.WebSocket };
  globalThis.window = {
    addEventListener() {},
    setTimeout: (callback) => { timeouts.set(++timer, callback); return timer; },
    clearTimeout: (id) => timeouts.delete(id),
    setInterval: (callback) => { intervals.set(++timer, callback); return timer; },
    clearInterval: (id) => intervals.delete(id),
  };
  globalThis.WebSocket = class {
    constructor() {
      if (throws) throw new Error("架空の接続拒否");
      this.closed = false;
      sockets.push(this);
    }
    close() { this.closed = true; }
    message(data) { this.onmessage?.({ data: JSON.stringify(data) }); }
  };
  const react = {
    useState(initial) {
      const index = cursor++;
      if (!(index in slots)) slots[index] = initial;
      return [slots[index], (next) => { slots[index] = typeof next === "function" ? next(slots[index]) : next; }];
    },
    useRef(initial) { const index = cursor++; return slots[index] ??= { current: initial }; },
    useCallback: (callback) => callback,
    useEffect(callback, dependencies) {
      const index = cursor++;
      const old = slots[index];
      if (!old || dependencies.some((value, i) => value !== old.dependencies[i])) {
        effects.push(() => {
          old?.cleanup?.();
          slots[index] = { dependencies, cleanup: callback() };
        });
      }
    },
  };
  const exports = {};
  new Function("require", "exports", output)((name) => ({
    react,
    "./api": { logStreamUrl: () => "ws://console.invalid/logs" },
    "./logs": { appendLogEntries: (before, added) => before.concat(added).slice(-3000) },
  })[name], exports);
  const render = () => { cursor = 0; return exports.useLogStream(enabled, 300, key); };
  const read = () => { render(); effects.splice(0).forEach((effect) => effect()); return render(); };
  t.after(() => {
    slots.forEach((slot) => slot?.cleanup?.());
    globalThis.window = previous.window;
    globalThis.WebSocket = previous.WebSocket;
  });
  read();
  return {
    sockets, timeouts, intervals, read,
    disable() { enabled = false; return read(); },
    switchConnection() { key = "second"; return read(); },
    flush() { for (const callback of intervals.values()) callback(); return read(); },
  };
}

const entry = { ts: 1, message: "架空のログ", priority: 6, source: "python" };

test("壊れたログの項目と終了理由はエラーにして既読ログを保持する", async (t) => {
  for (const broken of [null, {}, {...entry, ts: "1"}, {...entry, message: []},
    {...entry, source: {}}, {...entry, priority: "6"}, {...entry, pid: []}]) {
    await t.test(JSON.stringify(broken), (child) => {
      const h = harness(child);
      const socket = h.sockets[0];
      socket.onopen();
      socket.message({type: "entry", entry});
      socket.message({type: "entry", entry: broken});
      const stream = h.flush();
      assert.equal(stream.status, "error");
      assert.deepEqual(stream.entries, [entry]);
      assert.equal(socket.closed, true);
      assert.equal(h.timeouts.size, 0);
    });
  }
  const h = harness(t);
  h.sockets[0].message({type: "end", message: {invalid: true}});
  assert.equal(h.read().status, "error");
});

test("バックエンドのエラー理由と受信済みログを残し、自動再接続しない", (t) => {
  const h = harness(t);
  const socket = h.sockets[0];
  socket.onopen();
  socket.message({ type: "entry", entry });
  socket.message({ type: "error", message: "架空のログ取得権限エラー" });
  socket.onerror();
  socket.onclose({ code: 1008 });
  const stream = h.flush();
  assert.equal(stream.status, "error");
  assert.equal(stream.message, "架空のログ取得権限エラー");
  assert.deepEqual(stream.entries, [entry]);
  assert.equal(h.timeouts.size, 0);
  assert.equal(h.sockets.length, 1);
});

test("Origin拒絶・異常切断は理由を表示して停止する", (t) => {
  const h = harness(t);
  h.sockets[0].onclose({ code: 1008 });
  assert.equal(h.read().status, "error");
  assert.match(h.read().message, /1008/);
  assert.equal(h.timeouts.size, 0);
});

test("WebSocketのerrorが先に届いても、close後にエラーを維持する", (t) => {
  const h = harness(t);
  h.sockets[0].onerror();
  h.sockets[0].onclose({ code: 1006 });
  assert.equal(h.read().status, "error");
  assert.match(h.read().message, /通信に失敗/);
  assert.equal(h.timeouts.size, 0);
});

test("正常終了とサーバーの終了通知でも自動再接続しない", (t) => {
  const h = harness(t);
  h.sockets[0].message({ type: "end", message: "架空のサーバー終了" });
  h.sockets[0].onclose({ code: 1000 });
  assert.equal(h.read().status, "ended");
  assert.equal(h.read().message, "架空のサーバー終了");
  assert.equal(h.timeouts.size, 0);
});

test("手動再接続で原因を解除し、旧ソケットの遅れたイベントを無視する", (t) => {
  const h = harness(t);
  const old = h.sockets[0];
  old.onerror();
  h.read().retry();
  assert.equal(h.read().status, "connecting");
  assert.equal(h.read().message, null);
  assert.equal(h.sockets.length, 2);
  assert.equal(old.closed, true);
  old.message({ type: "error", message: "古いエラー" });
  old.onclose({ code: 1008 });
  old.onopen();
  h.sockets[1].onopen();
  assert.equal(h.read().status, "live");
  assert.equal(h.read().message, null);
  assert.equal(h.timeouts.size, 0);
});

test("SSH切断で受信を閉じ、接続先の切替では新しいソケットだけを使う", (t) => {
  const h = harness(t);
  const old = h.sockets[0];
  h.switchConnection();
  assert.equal(old.closed, true);
  assert.equal(h.sockets.length, 2);
  h.disable();
  assert.equal(h.read().status, "idle");
  assert.equal(h.sockets[1].closed, true);
  assert.equal(h.intervals.size, 0);
  old.onerror();
  assert.equal(h.read().status, "idle");
});

test("ソケット作成失敗と壊れた受信はエラーとして停止する", (t) => {
  const h = harness(t, { throws: true });
  assert.equal(h.read().status, "error");
  assert.match(h.read().message, /ログに接続できません/);
  assert.equal(h.timeouts.size, 0);
});

test("不正JSONは例外を外へ出さずに原因を残す", (t) => {
  const h = harness(t);
  const socket = h.sockets[0];
  socket.onmessage({ data: "not-json" });
  socket.onclose({ code: 1000 });
  assert.equal(h.read().status, "error");
  assert.match(h.read().message, /応答を読み取れません/);
  assert.equal(socket.closed, true);
  assert.equal(h.timeouts.size, 0);
});

test("一時停止中の新着は再開時に反映できる", (t) => {
  const h = harness(t);
  h.read().setPaused(true);
  h.sockets[0].message({ type: "entry", entry });
  assert.equal(h.flush().pendingCount, 1);
  assert.equal(h.read().entries.length, 0);
  h.read().setPaused(false);
  assert.deepEqual(h.flush().entries, [entry]);
  assert.equal(h.read().pendingCount, 0);
});
