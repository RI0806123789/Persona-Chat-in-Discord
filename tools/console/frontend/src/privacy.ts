// 伏せ字モード（スクリーンショットを Public の issue に貼るためのもの）。
// 欄ごとに隠す（mask）だけでなく、エラー文・ログ・コマンド履歴のような自由な文の中に出てくる
// ホスト名・ホスト鍵・ボット名・サーバー名・チャンネル名・利用者の表示名も、分かっている文字列を探して隠す（redact）。
// bot.db のテーブルの中身は、列の種類ごとに隠す（dbTables.ts の isSecretKind）。
import { createContext, useContext } from "react";
import type { ConnectionState, GitInfo, Insights, Profile } from "./types";
import { conversationLines } from "./logPrivacy";
export { conversationLines } from "./logPrivacy";

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
  git?: GitInfo | null;
  /** データページで開いたテーブルの中身に出てきた、利用者の表示名 */
  userNames?: string[];
}): string[] {
  const values: (string | null | undefined)[] = [
    parts.connection?.fingerprint,
    parts.connection?.server?.hostname,
    parts.insights?.discord.bot_user,
    parts.git?.branch,
    ...(parts.userNames ?? []),
  ];
  // ボット名は「名前#0000」の形で出ることがあるので、名前だけでも探す
  const botUser = parts.insights?.discord.bot_user;
  if (botUser?.includes("#")) values.push(botUser.split("#")[0]);
  for (const profile of [parts.profile, ...parts.profiles]) {
    values.push(profile?.host, profile?.username, profile?.label, profile?.bot_dir, profile?.id, profile?.deploy_branch);
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

// 任意本文は運用ログと区別できないため、受信時の属性で隠す（logPrivacy.ts）。
const USER_LINE = /^User \((.*?)\)：/;
const AI_LINE = /^AI：/;
const CHANNEL_MENTION = /#[^\s#、。，．,.:：)）」』\]]+/g;

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
 * 複数行の文章（デプロイの出力など）を伏せ字にする。ログと同じく、境界が不明な本文も隠す。
 * 再起動直後のログを出力に含めるため、そこに利用者の発言が混ざりうる。
 */
export function redactText(text: string, privacy: Privacy): string {
  if (!privacy.enabled || !text) return text;
  const lines = text.split("\n");
  const conversation = conversationLines(lines.map((message) => ({ ts: 0, message, priority: null, pid: null, source: "" })));
  return lines.map((line, index) => redactLogLine(line, conversation[index], privacy)).join("\n");
}
