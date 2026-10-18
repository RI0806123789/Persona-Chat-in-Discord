import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../src/dbTables.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } });
const {
  DB_TABLE_SPECS,
  hourlyCounts,
  inlineText,
  isSecretKind,
  isViewableTable,
  namesInPage,
  parseDbTime,
  personaName,
  readAnalysis,
  resolveColumns,
} = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputText).toString("base64")}`);

test("画面のテーブル定義は、バックエンドが中身を返すテーブル（DB_TABLES）と一致する", async () => {
  const backend = await readFile(new URL("../../backend/remote_commands.py", import.meta.url), "utf8");
  const block = /^DB_TABLES = \(([\s\S]*?)^\)/m.exec(backend);
  assert.ok(block, "remote_commands.py に DB_TABLES が見つかりません");
  const names = [...block[1].matchAll(/"([a-z_]+)"/g)].map((match) => match[1]);
  assert.deepEqual(Object.keys(DB_TABLE_SPECS).sort(), names.sort());
  assert.equal(isViewableTable("sqlite_sequence"), false);
  assert.equal(isViewableTable("constructor"), false);
});

test("伏せ字モードで、Discord の ID・名前・本文の列はすべて隠す", () => {
  const textKeys = new Set(["user_name", "question", "response", "message", "value", "key", "label", "recent_samples", "analysis", "error"]);
  for (const [table, spec] of Object.entries(DB_TABLE_SPECS)) {
    for (const column of spec.columns) {
      // reminder_id は登録順の連番で、Discord の ID ではない
      const isDiscordId = column.key.endsWith("_id") && column.key !== "reminder_id";
      if (isDiscordId || textKeys.has(column.key)) {
        assert.ok(isSecretKind(column.kind), `${table}.${column.key}（${column.kind}）が伏せ字の対象になっていません`);
      }
    }
  }
  for (const kind of ["isoTime", "level", "count", "status", "model", "persona", "rating", "hourly"]) {
    assert.equal(isSecretKind(kind), false, kind);
  }
});

test("古い版の DB に無い列は外し、知らない列は最後に足す", () => {
  const columns = resolveColumns("feedback", ["rating", "message_id", "future_column"]);
  assert.deepEqual(
    columns.map((column) => [column.key, column.kind]),
    [["rating", "rating"], ["message_id", "id"], ["future_column", "raw"]],
  );
  assert.equal(isSecretKind(columns.at(-1).kind), true);
});

test("日時は UTC として読み、pending_log の書式と UNIX 秒にも対応する", () => {
  const expected = Date.UTC(2026, 9, 18, 3, 12, 45);
  assert.equal(parseDbTime("2026-10-18T03:12:45+00:00", "isoTime"), expected);
  assert.equal(parseDbTime("2026-10-18T12:12:45+09:00", "isoTime"), expected);
  assert.equal(parseDbTime("2026-10-18T03:12:45", "isoTime"), expected);
  assert.equal(parseDbTime("2026-10-18 03:12:45 UTC", "logTime"), expected);
  assert.equal(parseDbTime(expected / 1000, "unixTime"), expected);
  assert.equal(parseDbTime(0, "unixTime"), null);
  assert.equal(parseDbTime("", "isoTime"), null);
  assert.equal(parseDbTime("読めない", "logTime"), null);
});

test("ペルソナはファイル名で見せる", () => {
  assert.equal(personaName("../prompts/prompt_tsun.txt"), "prompt_tsun");
  assert.equal(personaName("..\\prompts\\prompt_default.txt"), "prompt_default");
  assert.equal(personaName(""), "—");
});

test("JSON の値を1行にし、時間帯は 24 個にそろえる", () => {
  assert.equal(inlineText(["猫", "カレー"]), "猫、カレー");
  assert.equal(inlineText({ 学年: 2 }), '{"学年":2}');
  assert.equal(inlineText("改行\nを含む  文"), "改行 を含む 文");
  assert.equal(inlineText(null), "—");
  const hourly = hourlyCounts([1, null, "3", -2, 5]);
  assert.equal(hourly.length, 24);
  assert.deepEqual(hourly.slice(0, 5), [1, 0, 0, 0, 5]);
  assert.deepEqual(hourlyCounts("壊れた値"), Array(24).fill(0));
});

test("解析結果は型をそろえて読み、形が違えば null にする", () => {
  const analysis = readAnalysis({
    topics: ["ゲーム", { label: "勉強", weight: 0.3 }, { weight: 1 }, 5],
    big_five: { openness: 120, neuroticism: -3, extraversion: "高い" },
    personality: "好奇心が強い",
    computed_at: "2026-10-18T03:12:45+00:00",
    sample_count: 6,
  });
  assert.deepEqual(analysis.topics, [{ label: "ゲーム", weight: 1 }, { label: "勉強", weight: 0.3 }]);
  assert.deepEqual(analysis.bigFive.map((trait) => [trait.key, trait.value]), [["openness", 100], ["neuroticism", 0]]);
  assert.equal(analysis.sampleCount, 6);
  assert.equal(readAnalysis(null), null);
  assert.equal(readAnalysis(["配列"]), null);
});

test("ページに出てくる表示名を、重複なく集める", () => {
  const names = namesInPage({
    users: { 1: "架空のさくら", 2: "架空のたろう" },
    rows: [{ cells: { user_name: "架空のさくら" } }, { cells: { user_name: "記録時の別名" } }, { cells: { user_name: "" } }],
  });
  assert.deepEqual(names.sort(), ["架空のさくら", "架空のたろう", "記録時の別名"].sort());
});
