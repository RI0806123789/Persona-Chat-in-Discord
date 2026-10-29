import type { LogEntry } from "./types";

// 本文は運用ログの書式も区切り線も含められる。文字列だけでは会話終了を確定しない。
// PID は journald の _PID 由来。source だけではボットの識別名と区別できない。
function isSystemEntry(entry: LogEntry): boolean {
  return entry.pid === 1 && entry.source === "systemd";
}

const LOGGING_PREFIX = /^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4}\] \[(?:DEBUG|INFO|WARNING|ERROR|CRITICAL)\] /;
const LOGGER_NAME = /^\[[A-Za-z_][A-Za-z0-9_.]*\] /;

/** 旧print・loggingの原文・時刻を除いた表示文から、隠すための会話見出しだけを認識する。 */
export function conversationHeader(message: string): "user" | "ai" | null {
  const body = message.replace(LOGGING_PREFIX, "").replace(LOGGER_NAME, "");
  if (/^User \(.*?\)[：:]/.test(body)) return "user";
  if (/^AI[：:]/.test(body)) return "ai";
  return null;
}

/** 取得先頭は会話途中かもしれない。受信時に分類し、表示クリアや保持上限から独立させる。 */
export function createLogPrivacyTracker(): (entry: LogEntry) => LogEntry {
  let state: "unknown" | "conversation" = "unknown";
  return (entry) => {
    if (isSystemEntry(entry)) return { ...entry, messagePrivacy: "system" };
    if (conversationHeader(entry.message)) state = "conversation";
    // 既知の運用ログに似た本文や、別PIDの行でも公開可能とは判断しない。
    return { ...entry, messagePrivacy: state };
  };
}

/**
 * 受信時属性を優先する。保持・切り出しでは属性も引き継ぐこと。
 * 属性のない抜粋は先行文脈を復元できないため、unknownから保守的に再分類する。
 * unknownとconversationはどちらも隠すので、文脈のない部分配列でも本文は公開しない。
 */
export function conversationLines(entries: LogEntry[]): boolean[] {
  const classify = createLogPrivacyTracker();
  return entries.map((entry) => {
    const received = entry.messagePrivacy ?? classify(entry).messagePrivacy;
    return received !== "system" || !isSystemEntry(entry);
  });
}
