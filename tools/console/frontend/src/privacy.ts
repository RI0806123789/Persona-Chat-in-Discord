// 伏せ字モード（スクリーンショットを Public の issue に貼るためのもの）。
// 欄ごとに隠す（mask）だけでなく、エラー文・ログ・コマンド履歴のような自由な文の中に出てくる
// ホスト名・ホスト鍵・ボット名・サーバー名・チャンネル名も、分かっている文字列を探して隠す（redact）。
import { createContext, useContext } from "react";
import type { ConnectionState, Insights, LogEntry, Profile } from "./types";

export const MASK = "••••••";

// これより短い文字列は、普通の単語まで巻き込んで隠してしまうので対象にしない
const MIN_SECRET_LENGTH = 3;
// 伏せ字の対象から外す、よくある値（隠しても意味が無く、文章が読めなくなるもの）
const NOT_SECRET = new Set(["localhost", "127.0.0.1", "bot", "root", "true", "false"]);

export interface Privacy {
  enabled: boolean;
  /** 欄の値をまるごと隠す */
  mask: (text: string | null | undefined) => string;
  /** 文の中に出てくる、分かっている秘密の文字列だけを隠す */
  redact: (text: string | null | undefined) => string;
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** 画面が知っている「隠すべき文字列」を集める。 */
export function collectSecrets(parts: {
  profile: Profile | null;
  profiles: Profile[];
  connection: ConnectionState | null;
  insights: Insights | null;
}): string[] {
  const values: (string | null | undefined)[] = [
    parts.connection?.fingerprint,
    parts.connection?.server?.hostname,
    parts.insights?.discord.bot_user,
  ];
  // ボット名は「名前#0000」の形で出ることがあるので、名前だけでも探す
  const botUser = parts.insights?.discord.bot_user;
  if (botUser?.includes("#")) values.push(botUser.split("#")[0]);
  for (const profile of [parts.profile, ...parts.profiles]) {
    values.push(profile?.host, profile?.username, profile?.label, profile?.bot_dir);
    const home = profile?.bot_dir?.match(/\/(?:home|Users)\/([^/]+)(?=\/|$)/);
    values.push(home?.[0], home?.[1]);
  }
  for (const guild of parts.insights?.discord.guilds ?? []) {
    values.push(guild.name, guild.voice_channel);
  }
  const secrets = values.filter(
    (value): value is string => !!value && value.length >= MIN_SECRET_LENGTH && !NOT_SECRET.has(value.toLowerCase()),
  );
  // 長いものから置き換える（短い名前が長い名前の一部を先に壊さないように）
  return [...new Set(secrets)].sort((a, b) => b.length - a.length);
}

export function createPrivacy(enabled: boolean, secrets: string[]): Privacy {
  const pattern = secrets.length ? new RegExp(secrets.map(escapeRegExp).join("|"), "g") : null;
  return {
    enabled,
    mask: (text) => {
      if (!text) return "—";
      return enabled ? MASK : text;
    },
    redact: (text) => {
      if (!text) return text ?? "";
      if (!enabled || !pattern) return text;
      return text.replace(pattern, MASK);
    },
  };
}

export const PrivacyContext = createContext<Privacy>(createPrivacy(false, []));

export function usePrivacy(): Privacy {
  return useContext(PrivacyContext);
}

// ---------------------------------------------------------------- ログの会話の本文

// ボットは会話ごとに次の形でログを出す（src/persona_bot/utils/usage_graph.py の log_snapshot）:
//   ------（区切り）/ User (場所)：質問 / 添付 / Prompt ... / Total token count：n / AI：応答（複数行のことがある）/ ------
// 応答の2行目以降は journald では別の行になるため、「AI：」から区切り線までを1つの塊として扱う。
const USER_LINE = /^User \((.*?)\)：/;
const AI_LINE = /^AI：/;
const SEPARATOR = /^-{20,}$/;
const PROMPT_LINE = /^Prompt text count：/;
// 「#雑談」のようなチャンネル名（ログの中ではチャンネル名を一覧から知る方法が無いため、形で探す）
const CHANNEL_MENTION = /#[^\s#、。，．,.:：)）」』\]]+/g;

/** 各行が「会話の本文」に当たるかどうか（true なら伏せ字モードで本文を隠す）。 */
export function conversationLines(entries: LogEntry[]): boolean[] {
  const result: boolean[] = [];
  let state: "none" | "user" | "ai" = "none";
  for (const entry of entries) {
    const message = entry.message;
    if (USER_LINE.test(message)) {
      state = "user";
      result.push(true);
    } else if (AI_LINE.test(message)) {
      state = "ai";
      result.push(true);
    } else if (SEPARATOR.test(message.trim())) {
      state = "none";
      result.push(false);
    } else if (state === "user" && PROMPT_LINE.test(message)) {
      state = "none";
      result.push(false);
    } else {
      result.push(state !== "none");
    }
  }
  return result;
}

/** 伏せ字モードで、ログ1行を見せる形にする。 */
export function redactLogLine(text: string, isConversation: boolean, privacy: Privacy): string {
  if (!privacy.enabled) return text;
  if (isConversation) {
    // 「User (場所)：」「AI：」の見出しだけ残し、誰が・どこで・何を話したかは隠す
    if (USER_LINE.test(text)) return `User (${MASK})：${MASK}`;
    if (AI_LINE.test(text)) return `AI：${MASK}`;
    return MASK;
  }
  return privacy.redact(text).replace(CHANNEL_MENTION, `#${MASK}`);
}

/**
 * 複数行の文章（デプロイの出力など）を伏せ字にする。ログと同じく、会話の本文は行の塊ごと隠す。
 * 再起動直後のログを出力に含めるため、そこに利用者の発言が混ざりうる。
 */
export function redactText(text: string, privacy: Privacy): string {
  if (!privacy.enabled || !text) return text;
  const lines = text.split("\n");
  const conversation = conversationLines(lines.map((message) => ({ ts: 0, message, priority: null, pid: null, source: "" })));
  return lines.map((line, index) => redactLogLine(line, conversation[index], privacy)).join("\n");
}
