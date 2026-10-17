import type { LogEntry } from "./types";

export type LogLevel = "error" | "warn" | "info" | "system";

export const MAX_LOG_ENTRIES = 3000;

/** ライブ受信中・一時停止中のどちらも、古い行を落として保持量を制限する。 */
export function appendLogEntries(previous: LogEntry[], incoming: LogEntry[]): LogEntry[] {
  return previous.concat(incoming).slice(-MAX_LOG_ENTRIES);
}

export const LEVEL_LABELS: Record<LogLevel, string> = {
  error: "エラー",
  warn: "警告",
  info: "情報",
  system: "systemd",
};

// discord.py の logging が出す行: "[2026-10-16 12:04:29] [INFO    ] discord.gateway: 本文"
// 先頭の時刻は UTC なので、画面では journald の時刻（ローカル時刻）だけを見せる
const LOGGING_PREFIX = /^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] \[(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s*\] /;

// ボット自身は print でログを出すため、journald の重要度はほぼ常に「情報(6)」になる。
// そこで logging の書式があればそのレベルを、無ければ本文の言葉から重さを推定する
const ERROR_PATTERN = /Traceback|Error|ERROR|Exception|エラー|失敗|例外|^\s+File "/;
const WARN_PATTERN = /警告|Warning|WARN|タイムアウト|見送/;

export function levelOf(entry: LogEntry): LogLevel {
  if (entry.priority !== null && entry.priority <= 3) return "error";
  if (entry.priority === 4) return "warn";
  if (entry.source === "systemd") return "system";
  const prefixed = LOGGING_PREFIX.exec(entry.message);
  if (prefixed) {
    if (prefixed[1] === "ERROR" || prefixed[1] === "CRITICAL") return "error";
    if (prefixed[1] === "WARNING") return "warn";
    return "info";
  }
  if (ERROR_PATTERN.test(entry.message)) return "error";
  if (WARN_PATTERN.test(entry.message)) return "warn";
  return "info";
}

const EXCEPTION_TRACEBACK = /^Traceback \(most recent call last\)/;
// discord.py が「heartbeat blocked」の警告に続けて出す「ループが何をしていたか」のスタックなど
const OTHER_TRACEBACK = /traceback \(most recent call last\)/i;

/**
 * 並んだログをまとめて分類する。Traceback（複数行）は、字下げされた続きの行も含めて1つの塊として同じレベルにする。
 * 1行ずつ levelOf で判定すると、「File ...」はエラー、「return ...」は情報、のように塊の中で色がばらばらになるため。
 */
export function classifyEntries(entries: LogEntry[]): LogLevel[] {
  const levels: LogLevel[] = [];
  let block: LogLevel | null = null; // Traceback の途中なら、その塊のレベル
  let isException = false; // 例外の Traceback なら、最後の字下げなしの行（例外の本文）までが塊
  for (const entry of entries) {
    const message = entry.message;
    if (EXCEPTION_TRACEBACK.test(message)) {
      block = "error";
      isException = true;
    } else if (OTHER_TRACEBACK.test(message)) {
      // 直前の警告などの続きなので、そのレベルを引き継ぐ
      const own = levelOf(entry);
      const previous = levels[levels.length - 1];
      block = own === "error" || own === "warn" ? own : previous === "error" || previous === "warn" ? previous : "warn";
      isException = false;
    } else if (block && /^\s/.test(message)) {
      // 字下げされた行は塊の続き
    } else if (block && !message.trim()) {
      // 空行もスタックの区切りとして同じレベルで表示する
    } else if (block && isException) {
      if (LOGGING_PREFIX.test(message) || entry.source === "systemd") {
        block = null; // 途中で別のログが割り込んだ場合は、新しい行として判定する
        levels.push(levelOf(entry));
        continue;
      }
      levels.push("error"); // 例外の本文（例: ValueError: ...）で塊が終わる
      block = null;
      continue;
    } else {
      block = null;
    }
    levels.push(block ?? levelOf(entry));
  }
  return levels;
}

/** 画面に出す本文。logging の時刻とレベルの前置きを外す（レベルは別の欄に出すため）。 */
export function displayMessage(entry: LogEntry): string {
  return entry.message.replace(LOGGING_PREFIX, "");
}
