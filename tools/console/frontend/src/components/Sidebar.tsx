// M3 Expressive のナビゲーションレール（展開 / 折りたたみ）と、狭い画面用のナビゲーションバー。
import { PAGES, type PageId } from "../navigation";
import type { Profile } from "../types";
import { Icon } from "./Icon";
import { IconButton, Kbd } from "./ui";

export function NavigationRail({
  page,
  onNavigate,
  expanded,
  onToggleExpanded,
  profile,
  connected,
  connecting,
  fab,
  onHelp,
}: {
  page: PageId;
  onNavigate: (page: PageId) => void;
  expanded: boolean;
  onToggleExpanded: () => void;
  profile: Profile | null;
  connected: boolean;
  connecting: boolean;
  fab: { label: string; icon: "plug" | "refresh"; onClick: () => void; disabled: boolean };
  onHelp: () => void;
}) {
  return (
    <aside className={`m3-rail ${expanded ? "is-expanded" : ""}`}>
      <div className="m3-rail-top">
        <IconButton
          icon={expanded ? "menuOpen" : "menu"}
          label={expanded ? "ナビゲーションを折りたたむ ([)" : "ナビゲーションを広げる ([)"}
          onClick={onToggleExpanded}
        />
        <div className="m3-rail-brand">
          <Icon name="bot" size={22} filled />
          <span className="title-medium">Persona Bot</span>
        </div>
      </div>

      <button type="button" className="m3-fab" onClick={fab.onClick} disabled={fab.disabled} title={fab.label}>
        {connecting ? <span className="m3-btn-spinner" aria-hidden="true" /> : <Icon name={fab.icon} size={24} />}
        <span className="m3-fab-label label-large">{fab.label}</span>
      </button>

      <nav className="m3-rail-items" aria-label="メインメニュー">
        {PAGES.map((item, index) => {
          const active = item.id === page;
          return (
            <button
              key={item.id}
              type="button"
              className={`m3-rail-item ${active ? "is-active" : ""}`}
              aria-current={active ? "page" : undefined}
              onClick={() => onNavigate(item.id)}
              title={`${item.label}（${index + 1}）`}
            >
              <span className="m3-rail-indicator">
                <Icon name={item.icon} size={24} filled={active} />
                <span className="m3-rail-label-inline label-large">{item.label}</span>
              </span>
              <span className="m3-rail-label label-medium">{item.short}</span>
            </button>
          );
        })}
      </nav>

      <div className="m3-rail-foot">
        <div className={`m3-env ${connected && profile?.environment === "production" ? "is-production" : ""}`}>
          <Icon name={connected ? "dns" : "linkOff"} size={20} />
          <div>
            <span className="label-small">接続先</span>
            <strong className="label-large">{connected && profile ? profile.label : "未接続"}</strong>
            {connected && profile && (
              <span className="label-small m3-env-tag">{profile.environment === "production" ? "本番環境" : "開発環境"}</span>
            )}
          </div>
        </div>
        <button type="button" className="m3-rail-help" onClick={onHelp} title="ショートカット (?)">
          <Icon name="keyboard" size={20} />
          <span className="label-large">ショートカット</span>
          <Kbd>?</Kbd>
        </button>
      </div>
    </aside>
  );
}

export function NavigationBar({ page, onNavigate }: { page: PageId; onNavigate: (page: PageId) => void }) {
  return (
    <nav className="m3-nav-bar" aria-label="メインメニュー">
      {PAGES.map((item) => {
        const active = item.id === page;
        return (
          <button
            key={item.id}
            type="button"
            className={`m3-nav-bar-item ${active ? "is-active" : ""}`}
            aria-current={active ? "page" : undefined}
            onClick={() => onNavigate(item.id)}
          >
            <span className="m3-nav-bar-indicator">
              <Icon name={item.icon} size={24} filled={active} />
            </span>
            <span className="label-medium">{item.short}</span>
          </button>
        );
      })}
    </nav>
  );
}
