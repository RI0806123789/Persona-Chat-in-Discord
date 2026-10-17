// M3 のトップアプリバー（見出し + 小見出し + 右側の操作）。
import { useEffect, useRef, useState } from "react";
import { formatRelative, maskIf } from "../format";
import type { PageDef } from "../navigation";
import { SEED_OPTIONS } from "../theme/theme";
import type { ConnectionState, Profile } from "../types";
import { Icon } from "./Icon";
import { Button, IconButton, Switch } from "./ui";

export function TopAppBar({
  page,
  connection,
  profiles,
  selectedProfileId,
  onSelectProfile,
  onConnect,
  onDisconnect,
  connecting,
  rttMs,
  lastUpdated,
  refreshing,
  onRefresh,
  autoRefresh,
  onToggleAuto,
  privacy,
  onTogglePrivacy,
  theme,
  onToggleTheme,
  seedId,
  onSelectSeed,
  onOpenConnection,
  now,
}: {
  page: PageDef;
  connection: ConnectionState | null;
  profiles: Profile[];
  selectedProfileId: string;
  onSelectProfile: (id: string) => void;
  onConnect: () => void;
  onDisconnect: () => void;
  connecting: boolean;
  rttMs: number | null;
  lastUpdated: number | null;
  refreshing: boolean;
  onRefresh: () => void;
  autoRefresh: boolean;
  onToggleAuto: () => void;
  privacy: boolean;
  onTogglePrivacy: () => void;
  theme: "light" | "dark";
  onToggleTheme: () => void;
  seedId: string;
  onSelectSeed: (id: string) => void;
  onOpenConnection: () => void;
  now: number;
}) {
  const connected = connection?.connected ?? false;
  const profile = connection?.profile ?? null;

  return (
    <header className="m3-top-app-bar">
      <div className="m3-top-app-bar-title">
        <h1 className="headline-small">{page.label}</h1>
        <p className="body-medium">{page.subtitle}</p>
      </div>

      <div className="m3-top-app-bar-actions">
        <button
          type="button"
          className={`m3-conn-chip ${connected ? "is-connected" : connecting ? "is-connecting" : ""}`}
          onClick={onOpenConnection}
          title="SSH 接続の詳細"
        >
          <span className="m3-conn-dot" aria-hidden="true" />
          <span className="label-large mono">
            {connected && profile
              ? `${profile.username ? `${maskIf(privacy, profile.username)}@` : ""}${maskIf(privacy, profile.host)}`
              : connecting
                ? "接続中…"
                : "未接続"}
          </span>
          {connected && rttMs !== null && <span className="m3-conn-rtt label-medium">{rttMs} ms</span>}
        </button>

        <div className="m3-select">
          <select
            value={selectedProfileId}
            onChange={(event) => onSelectProfile(event.target.value)}
            disabled={connected || connecting}
            aria-label="接続先"
          >
            {profiles.map((p) => (
              <option key={p.id} value={p.id}>
                {p.environment === "production" ? "● " : ""}
                {p.label}
              </option>
            ))}
          </select>
          <Icon name="expand" size={22} />
        </div>
        {connected ? (
          <Button variant="tonal" icon="linkOff" onClick={onDisconnect}>
            切断
          </Button>
        ) : (
          <Button variant="filled" icon="plug" onClick={onConnect} disabled={!selectedProfileId} busy={connecting}>
            接続
          </Button>
        )}

        <span className="m3-divider-vertical" aria-hidden="true" />

        <Button variant="text" icon="refresh" onClick={onRefresh} disabled={!connected || refreshing} title="今すぐ更新 (r)" className={refreshing ? "is-refreshing" : ""}>
          {lastUpdated ? formatRelative(lastUpdated, now) : "—"}
        </Button>
        <Switch checked={autoRefresh} onChange={onToggleAuto} label="自動" />

        <IconButton icon={privacy ? "eyeOff" : "eye"} label="ホスト名などを伏せる (h)" selected={privacy} onClick={onTogglePrivacy} />
        <SeedMenu seedId={seedId} onSelect={onSelectSeed} />
        <IconButton icon={theme === "dark" ? "sun" : "moon"} label="テーマ切り替え (t)" onClick={onToggleTheme} />
      </div>
    </header>
  );
}

/** テーマの基準色（シード色）を選ぶメニュー。選んだ色から M3 の配色を作り直す。 */
function SeedMenu({ seedId, onSelect }: { seedId: string; onSelect: (id: string) => void }) {
  const [open, setOpen] = useState(false);
  const wrapper = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent) => {
      if (!wrapper.current?.contains(event.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", close);
    return () => window.removeEventListener("mousedown", close);
  }, [open]);

  return (
    <div className="m3-seed" ref={wrapper}>
      <IconButton icon="palette" label="テーマの色" onClick={() => setOpen((value) => !value)} aria-expanded={open} />
      <div className={`m3-menu m3-seed-menu ${open ? "is-open" : ""}`} role="menu" inert={!open}>
        <span className="label-medium m3-menu-heading">テーマの色（シード色）</span>
        {SEED_OPTIONS.map((seed) => (
          <button
            key={seed.id}
            type="button"
            role="menuitemradio"
            aria-checked={seed.id === seedId}
            className="m3-menu-item"
            onClick={() => {
              onSelect(seed.id);
              setOpen(false);
            }}
          >
            <span className="m3-seed-swatch" style={{ background: seed.hex }} aria-hidden="true" />
            {seed.label}
            {seed.id === seedId && <Icon name="check" size={20} className="m3-menu-check" />}
          </button>
        ))}
      </div>
    </div>
  );
}
