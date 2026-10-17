// M3 のトップアプリバー（見出し + 小見出し + 右側の操作）。
// 接続状態のチップ・接続先の選択（メニュー）・接続 / 切断・更新・自動更新のスイッチ・伏せ字・表示の設定を置く。
import { formatRelative } from "../format";
import type { PageDef } from "../navigation";
import { usePrivacy } from "../privacy";
import { unsetFieldsOf } from "../profiles";
import { SEED_OPTIONS } from "../theme/theme";
import type { ConnectionState, Profile } from "../types";
import { Icon } from "./Icon";
import { MenuDivider, MenuHeading, MenuSurface, useMenu } from "./menu";
import { Button, IconButton, Switch } from "./ui";

export type ThemeChoice = "light" | "dark" | null;

const THEME_OPTIONS: { value: ThemeChoice; label: string; icon: "sync" | "sun" | "moon" }[] = [
  { value: null, label: "システムに合わせる", icon: "sync" },
  { value: "light", label: "ライト", icon: "sun" },
  { value: "dark", label: "ダーク", icon: "moon" },
];

export function TopAppBar({
  page,
  connection,
  profiles,
  profilesSource,
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
  onTogglePrivacy,
  themeChoice,
  onSelectTheme,
  seedId,
  onSelectSeed,
  onOpenConnection,
  showMenuButton,
  onMenu,
  now,
}: {
  page: PageDef;
  connection: ConnectionState | null;
  profiles: Profile[];
  /** 接続先を読んだファイル名（profiles.json か、無ければ profiles.example.json） */
  profilesSource: string;
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
  onTogglePrivacy: () => void;
  themeChoice: ThemeChoice;
  onSelectTheme: (choice: ThemeChoice) => void;
  seedId: string;
  onSelectSeed: (id: string) => void;
  onOpenConnection: () => void;
  /** 常設のレールが無い（狭い）画面で、モーダルのレールを開くボタンを出す */
  showMenuButton: boolean;
  onMenu: () => void;
  now: number;
}) {
  const privacy = usePrivacy();
  const connected = connection?.connected ?? false;
  const profile = connection?.profile ?? null;

  return (
    <header className="m3-top-app-bar">
      {showMenuButton && <IconButton icon="menu" label="ナビゲーションを開く ([)" onClick={onMenu} className="m3-top-app-bar-nav" />}
      <div className="m3-top-app-bar-title">
        <h1 className="headline-small">{page.label}</h1>
        <p className="body-medium">{page.subtitle}</p>
      </div>

      <div className="m3-top-app-bar-actions">
        {privacy.enabled && (
          <span className="m3-status tone-info privacy-chip" title="ホスト名・ホスト鍵・ボット名・サーバー名・チャンネル名・会話を伏せています">
            <Icon name="eyeOff" size={16} filled />
            伏せ字中
          </span>
        )}
        <button
          type="button"
          className={`m3-conn-chip ${connected ? "is-connected" : connecting ? "is-connecting" : ""}`}
          onClick={onOpenConnection}
          title="SSH 接続の詳細（g c）"
        >
          <span className="m3-conn-dot" aria-hidden="true" />
          <span className="label-large mono">
            {connected && profile
              ? `${profile.username ? `${privacy.mask(profile.username)}@` : ""}${privacy.mask(profile.host)}`
              : connecting
                ? "接続中…"
                : "未接続"}
          </span>
          {connected && rttMs !== null && <span className="m3-conn-rtt label-medium">{rttMs} ms</span>}
        </button>

        <ProfileMenu
          profiles={profiles}
          source={profilesSource}
          selectedProfileId={selectedProfileId}
          onSelect={onSelectProfile}
          disabled={connected || connecting}
        />
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

        <Button
          variant="text"
          icon="refresh"
          onClick={onRefresh}
          disabled={!connected || refreshing}
          title="今すぐ更新 (r)"
          aria-label={`今すぐ更新（最終更新 ${lastUpdated ? formatRelative(lastUpdated, now) : "なし"}）`}
          className={`top-refresh ${refreshing ? "is-refreshing" : ""}`}
        >
          {lastUpdated ? formatRelative(lastUpdated, now) : "—"}
        </Button>
        <Switch checked={autoRefresh} onChange={onToggleAuto} label="自動" title="自動更新 (a)" />

        <IconButton
          icon={privacy.enabled ? "eyeOff" : "eye"}
          label={privacy.enabled ? "伏せ字をやめる (h)" : "ホスト名などを伏せる (h)"}
          selected={privacy.enabled}
          onClick={onTogglePrivacy}
        />
        <DisplayMenu themeChoice={themeChoice} onSelectTheme={onSelectTheme} seedId={seedId} onSelectSeed={onSelectSeed} />
      </div>
    </header>
  );
}

/** 接続先の選択。ブラウザ標準の選択欄の代わりに M3 のメニューを使い、本番 / 開発の札を付ける。 */
function ProfileMenu({
  profiles,
  source,
  selectedProfileId,
  onSelect,
  disabled,
}: {
  profiles: Profile[];
  source: string;
  selectedProfileId: string;
  onSelect: (id: string) => void;
  disabled: boolean;
}) {
  const menu = useMenu();
  const selected = profiles.find((profile) => profile.id === selectedProfileId) ?? null;
  return (
    <div className="m3-menu-anchor" ref={menu.anchorRef}>
      <button
        ref={menu.triggerRef}
        type="button"
        className={`m3-select-button ${menu.open ? "is-open" : ""}`}
        onClick={menu.toggle}
        disabled={disabled || profiles.length === 0}
        aria-label={`接続先: ${selected?.label ?? "未選択"}`}
        title={disabled ? "切断すると変更できます" : "接続先を選ぶ"}
        {...menu.triggerProps}
      >
        {selected && <EnvTag environment={selected.environment} />}
        <span className="label-large m3-select-text">{selected?.label ?? "接続先を選ぶ"}</span>
        <Icon name="expand" size={22} className="m3-select-arrow" />
      </button>
      <MenuSurface controller={menu} label="接続先" className="m3-profile-menu">
        <MenuHeading>接続先（backend/{source || "profiles.json"}）</MenuHeading>
        {profiles.map((profile) => (
          <button
            key={profile.id}
            type="button"
            role="menuitemradio"
            aria-checked={profile.id === selectedProfileId}
            tabIndex={-1}
            className="m3-menu-item"
            onClick={() => {
              onSelect(profile.id);
              menu.close(true);
            }}
          >
            <EnvTag environment={profile.environment} />
            <span className="m3-menu-item-text">{profile.label}</span>
            <UnsetTag profile={profile} />
            {profile.id === selectedProfileId && <Icon name="check" size={20} className="m3-menu-check" />}
          </button>
        ))}
      </MenuSurface>
    </div>
  );
}

export function EnvTag({ environment }: { environment: Profile["environment"] }) {
  return <span className={`m3-tag ${environment === "production" ? "is-prod" : "is-dev"}`}>{environment === "production" ? "本番" : "開発"}</span>;
}

/** 見本の値（YOUR_...）のまま書き換えられていない接続先に付ける札。 */
export function UnsetTag({ profile }: { profile: Profile }) {
  const unset = unsetFieldsOf(profile);
  if (unset.length === 0) return null;
  return (
    <span className="m3-tag is-unset" title={`見本の値のまま: ${unset.join("・")}`}>
      要設定
    </span>
  );
}

/** 表示の設定: ライト / ダーク（またはシステムに合わせる）と、配色の基準色（シード色）。 */
function DisplayMenu({
  themeChoice,
  onSelectTheme,
  seedId,
  onSelectSeed,
}: {
  themeChoice: ThemeChoice;
  onSelectTheme: (choice: ThemeChoice) => void;
  seedId: string;
  onSelectSeed: (id: string) => void;
}) {
  const menu = useMenu();
  return (
    <div className="m3-menu-anchor" ref={menu.anchorRef}>
      <IconButton
        ref={menu.triggerRef}
        icon="palette"
        label="表示の設定（テーマ・色）"
        className={menu.open ? "is-open" : ""}
        onClick={menu.toggle}
        {...menu.triggerProps}
      />
      <MenuSurface controller={menu} label="表示の設定" className="m3-display-menu">
        <MenuHeading>テーマ（t で切り替え）</MenuHeading>
        {THEME_OPTIONS.map((option) => (
          <button
            key={option.label}
            type="button"
            role="menuitemradio"
            aria-checked={themeChoice === option.value}
            tabIndex={-1}
            className="m3-menu-item"
            onClick={() => onSelectTheme(option.value)}
          >
            <Icon name={option.icon} size={20} />
            <span className="m3-menu-item-text">{option.label}</span>
            {themeChoice === option.value && <Icon name="check" size={20} className="m3-menu-check" />}
          </button>
        ))}
        <MenuDivider />
        <MenuHeading>配色の基準色（シード色）</MenuHeading>
        {SEED_OPTIONS.map((seed) => (
          <button
            key={seed.id}
            type="button"
            role="menuitemradio"
            aria-checked={seed.id === seedId}
            tabIndex={-1}
            className="m3-menu-item"
            onClick={() => onSelectSeed(seed.id)}
          >
            <span className="m3-seed-swatch" style={{ background: seed.hex }} aria-hidden="true" />
            <span className="m3-menu-item-text">{seed.label}</span>
            {seed.id === seedId && <Icon name="check" size={20} className="m3-menu-check" />}
          </button>
        ))}
      </MenuSurface>
    </div>
  );
}
