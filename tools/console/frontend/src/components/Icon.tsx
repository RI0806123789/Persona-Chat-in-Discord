// アイコンは Material Symbols（Rounded・可変フォント）。npm の material-symbols に同梱のフォントを使い、CDN は使わない。
// 画面のコードでは用途の名前（overview など）で指定し、ここで Material Symbols の名前に置き換える。
const SYMBOLS = {
  overview: "monitoring",
  logs: "terminal",
  power: "power_settings_new",
  database: "database",
  guilds: "forum",
  plug: "lan",
  refresh: "refresh",
  restart: "restart_alt",
  play: "play_arrow",
  pause: "pause",
  stop: "stop",
  git: "commit",
  cpu: "memory",
  memory: "memory_alt",
  disk: "hard_drive",
  clock: "schedule",
  keyboard: "keyboard",
  search: "search",
  close: "close",
  check: "check",
  checkCircle: "check_circle",
  alert: "warning",
  error: "error",
  info: "info",
  eye: "visibility",
  eyeOff: "visibility_off",
  sun: "light_mode",
  moon: "dark_mode",
  rocket: "rocket_launch",
  undo: "undo",
  skip: "skip_next",
  timer: "timer",
  shield: "shield",
  bot: "smart_toy",
  arrowDown: "arrow_downward",
  trash: "delete",
  terminal: "terminal",
  layers: "layers",
  menu: "menu",
  menuOpen: "menu_open",
  palette: "palette",
  expand: "arrow_drop_down",
  cloud: "cloud",
  chat: "chat",
  sync: "sync",
  voice: "headset_mic",
  group: "group",
  bolt: "bolt",
  history: "history",
  stage: "campaign",
  person: "person",
  token: "toll",
  sparkle: "auto_awesome",
  linkOff: "link_off",
  dns: "dns",
  thumbUp: "thumb_up",
  thumbDown: "thumb_down",
  block: "block",
  alarm: "alarm",
  description: "description",
  radio: "radio_button_unchecked",
  radioChecked: "radio_button_checked",
  chevronLeft: "chevron_left",
  chevronRight: "chevron_right",
  expandMore: "expand_more",
  table: "table_view",
} as const;

export type IconName = keyof typeof SYMBOLS;

export function Icon({
  name,
  size = 20,
  filled = false,
  weight = 400,
  className = "",
}: {
  name: IconName;
  size?: number;
  filled?: boolean;
  weight?: number;
  className?: string;
}) {
  return (
    <span
      className={`material-symbols-rounded icon ${className}`}
      style={{
        fontSize: size,
        fontVariationSettings: `"FILL" ${filled ? 1 : 0}, "wght" ${weight}, "GRAD" 0, "opsz" ${Math.min(48, Math.max(20, size))}`,
      }}
      aria-hidden="true"
    >
      {SYMBOLS[name]}
    </span>
  );
}
