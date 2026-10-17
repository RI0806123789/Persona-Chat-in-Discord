import type { IconName } from "./components/Icon";

export type PageId = "overview" | "logs" | "operations" | "data" | "guilds" | "connection";

export interface PageDef {
  id: PageId;
  label: string;
  /** 折りたたんだレールとナビゲーションバー用の短い名前 */
  short: string;
  subtitle: string;
  icon: IconName;
  /** g に続けて押すキー（g o で概要へ、など） */
  goKey: string;
}

export const PAGES: PageDef[] = [
  { id: "overview", label: "概要", short: "概要", subtitle: "ボットとサーバーの状態をひと目で", icon: "overview", goKey: "o" },
  { id: "logs", label: "ログ", short: "ログ", subtitle: "journalctl -u bot -f をライブで追う", icon: "logs", goKey: "l" },
  { id: "operations", label: "サービス操作", short: "操作", subtitle: "起動・停止・再起動と操作の記録", icon: "power", goKey: "s" },
  { id: "data", label: "データ", short: "データ", subtitle: "bot.db の件数を読み取り専用で集計", icon: "database", goKey: "d" },
  { id: "guilds", label: "サーバー", short: "サーバー", subtitle: "参加中の Discord サーバーと、サーバーごとの設定", icon: "guilds", goKey: "g" },
  { id: "connection", label: "SSH 接続", short: "接続", subtitle: "接続先・ホスト鍵・コマンド履歴", icon: "plug", goKey: "c" },
];

export function isPageId(value: unknown): value is PageId {
  return PAGES.some((page) => page.id === value);
}
