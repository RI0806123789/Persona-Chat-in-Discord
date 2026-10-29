import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

// 既存の TypeScript だけで検証する。Node.js 20 でも動き、追加のテストライブラリは不要。
const source = await readFile(new URL("../src/logs.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext } });
const { appendLogEntries, classifyEntries, displayMessage, MAX_LOG_ENTRIES } = await import(
  `data:text/javascript;base64,${Buffer.from(compiled.outputText).toString("base64")}`
);

const entry = (message, overrides = {}) => ({ ts: 0, message, priority: 6, pid: 42, source: "python", ...overrides });

test("discord.py のレベルを使い、UTC の前置きを表示から外す", () => {
  const rows = [
    entry("[2026-10-18 01:02:03] [INFO    ] discord.client: Error という単語を含む情報"),
    entry("[2026-10-18 01:02:03] [WARNING ] discord.gateway: heartbeat blocked"),
    entry("[2026-10-18 01:02:03] [ERROR   ] discord.client: 応答できません"),
    entry("サービスを開始", { source: "systemd" }),
  ];
  assert.deepEqual(classifyEntries(rows), ["info", "warn", "error", "system"]);
  assert.equal(displayMessage(rows[0]), "discord.client: Error という単語を含む情報");
});

test("#283 ボット共通loggingの時差付き時刻とレベルを認識しモジュール名を残す", () => {
  const prefix = "[2026-10-29T07:32:26+0900]";
  const rows = [
    entry(`${prefix} [INFO] [persona_bot.core.database] Errorという語を含む情報`),
    entry(`${prefix} [WARNING] [persona_bot.core.database] 処理を継続します`),
    entry(`${prefix} [ERROR] [persona_bot.core.database] 処理を停止しました`),
    entry("[2026-10-28T22:32:26-0900] [CRITICAL] [__main__] 処理を停止しました"),
  ];
  assert.deepEqual(classifyEntries(rows), ["info", "warn", "error", "error"]);
  assert.equal(displayMessage(rows[0]), "[persona_bot.core.database] Errorという語を含む情報");
  assert.deepEqual(classifyEntries([
    entry("Traceback (most recent call last):"), entry('  File "example.py", line 1'), rows[0],
  ]), ["error", "error", "info"]);
});

test("例外の Traceback は空行・コード・例外本文まで同じレベルにする", () => {
  const rows = ["Traceback (most recent call last):", '  File "example.py", line 1', "    call()", "",
    "ValueError: 架空の例外", "ログイン成功"].map((text) => entry(text));
  assert.deepEqual(classifyEntries(rows), ["error", "error", "error", "error", "error", "info"]);
});

test("heartbeat のスタックは警告を引き継ぎ、次の通常ログでは終了する", () => {
  const rows = ["[2026-10-18 01:02:03] [WARNING ] discord.gateway: heartbeat blocked",
    "Loop thread traceback (most recent call last):", '  File "example.py", line 1', "    call()", "ログイン成功"];
  assert.deepEqual(classifyEntries(rows.map((text) => entry(text))), ["warn", "warn", "warn", "warn", "info"]);
});

test("logging のエラー本文に Traceback が含まれていてもエラーレベルを保つ", () => {
  const rows = [entry("通常のログ"), entry("[2026-10-18 01:02:03] [ERROR   ] discord.client: Traceback (most recent call last):"),
    entry('  File "example.py", line 1')];
  assert.deepEqual(classifyEntries(rows), ["info", "error", "error"]);
});

test("Traceback の途中でも別の logging や systemd の行は独立して分類する", () => {
  const rows = [entry("Traceback (most recent call last):"), entry('  File "example.py", line 1'),
    entry("[2026-10-18 01:02:03] [INFO    ] discord.client: 接続"), entry("サービスを開始", { source: "systemd" })];
  assert.deepEqual(classifyEntries(rows), ["error", "error", "info", "system"]);
});

test("長時間の受信と一時停止後の一括反映でも、最新の上限件数を保持する", () => {
  const all = Array.from({ length: MAX_LOG_ENTRIES * 3 }, (_, index) => entry(String(index)));
  let paused = [];
  for (let offset = 0; offset < all.length; offset += 200) {
    paused = appendLogEntries(paused, all.slice(offset, offset + 200));
    assert.ok(paused.length <= MAX_LOG_ENTRIES);
  }
  const displayed = appendLogEntries([entry("古い表示")], paused);
  assert.equal(displayed.length, MAX_LOG_ENTRIES);
  assert.equal(displayed[0].message, String(all.length - MAX_LOG_ENTRIES));
  assert.equal(displayed.at(-1).message, String(all.length - 1));
});
