// バックエンド（backend/app.py）が返す JSON の型。キー名はバックエンドに合わせて snake_case。

export type Environment = "development" | "production";
export type ServiceAction = "start" | "stop" | "restart";

export interface Profile {
  id: string;
  label: string;
  environment: Environment;
  host: string;
  port: number | null;
  username: string | null;
  service: string;
  bot_dir: string;
  note: string;
}

export interface ProfilesResponse {
  profiles: Profile[];
  source: string;
}

export interface ServerInfo {
  hostname: string;
  kernel: string;
  os: string;
}

export interface ConnectionState {
  connected: boolean;
  profile: Profile | null;
  connected_at: number | null;
  fingerprint: string | null;
  server: ServerInfo | null;
  last_error: string | null;
}

export interface ServiceStatus {
  name: string;
  description: string;
  load_state: string;
  active_state: string;
  sub_state: string;
  result: string;
  unit_file_state: string;
  main_pid: number | null;
  restarts: number | null;
  memory_bytes: number | null;
  cpu_seconds: number | null;
  uptime_seconds: number | null;
}

export interface Usage {
  total: number;
  used: number;
  available: number;
}

export interface Resources {
  uptime_seconds: number | null;
  load: [number, number, number] | null;
  cpu_count: number | null;
  memory: Usage | null;
  swap: Usage | null;
  disk: Usage | null;
}

export interface GitInfo {
  branch: string;
  commit: string;
  subject: string;
  committed_at: string;
  describe: string;
  dirty_files: number;
}

export interface StatusResponse {
  rtt_ms: number | null;
  fetched_at: number;
  service: ServiceStatus | null;
  service_error: string | null;
  resources: Resources | null;
  resources_error: string | null;
  git: GitInfo | null;
  git_error: string | null;
}

export interface LogEntry {
  ts: number;
  message: string;
  priority: number | null;
  pid: number | null;
  source: string;
}

export interface Operation {
  id: number;
  at: number;
  profile_id: string;
  environment: Environment;
  action: ServiceAction;
  ok: boolean;
  message: string;
  duration_ms: number;
}

export interface CommandRecord {
  at: number;
  label: string;
  command: string;
  exit_status: number | null;
  duration_ms: number;
  ok: boolean;
  error: string | null;
}

export interface DbSummary {
  path: string;
  exists: boolean;
  errors: string[];
  size_bytes?: number;
  wal_bytes?: number;
  schema_version?: number;
  tables?: Record<string, number>;
  reminders_by_status?: Record<string, number>;
  queue_by_status?: Record<string, number>;
  feedback?: { good: number; bad: number };
  feedback_by_day?: { day: string; good: number; bad: number }[];
  blacklist?: { users: number; blocked: number } | null;
}

// ---- ボットの中の状態（backend/remote_scripts/insights.py の出力）

export interface GuildSettingsView {
  persona: string | null;
  model: string | null;
  respond_mode: string | null;
  grounding: boolean;
  auto_topic_enabled: boolean;
  auto_topic_interval_hours: number | null;
  /** null = この版のボットには自律行動が無い */
  autonomous_enabled: boolean | null;
  /** false = DB に保存された設定が無く、既定値で動いている */
  saved: boolean;
}

export interface GuildInsight {
  name: string;
  members: number | null;
  online: number | null;
  voice_channel: string | null;
  settings: GuildSettingsView;
}

export interface DiscordInsight {
  ok: boolean;
  error: string | null;
  api_rtt_ms?: number;
  bot_user?: string | null;
  guilds?: GuildInsight[];
}

export interface ActivityInsight {
  window_hours: number;
  gemini_calls: number;
  gemini_tokens: number;
  conversations: number;
  last_conversation_at: number | null;
  gateway_connects: number;
  gateway_resumes: number;
  gateway_invalidated: number;
  heartbeat_blocked: number;
  exceptions: number;
  autonomous_events: { at: number; text: string }[];
}

export interface Insights {
  errors: string[];
  source_dir: string | null;
  version: string | null;
  autonomous_supported: boolean;
  discord: DiscordInsight;
  dm_settings: GuildSettingsView;
  activity: ActivityInsight;
  generated_at: number;
}

export interface InsightsResponse {
  data: Insights;
  fetched_at: number;
  cached: boolean;
}

// 画面側で貯める時系列（スパークライン用）
export interface Sample {
  t: number;
  rtt: number | null;
  botMemory: number | null;
  hostMemoryRatio: number | null;
  load1: number | null;
}
