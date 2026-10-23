// M3 Expressive のナビゲーション。画面の幅（M3 のウィンドウサイズの区分）で形を変える。
//   1200px 以上 … 常設のナビゲーションレール（広げる / 折りたたむを [ で切り替え、ブラウザに覚えておく）
//   600〜1199px … 折りたたんだレール。[ やメニューボタンで、広げたレールを画面の上に重ねて開く（モーダル）
//   600px 未満 … 下部のナビゲーションバー（[ やメニューボタンで、同じモーダルのレールを開ける）
import { useRef } from "react";
import { usePrivacy } from "../privacy";
import { useModalFocus } from "../hooks";
import { PAGES, type PageId } from "../navigation";
import type { Profile } from "../types";
import { Icon } from "./Icon";
import { IconButton, Kbd } from "./ui";

export interface FabAction {
  label: string;
  icon: "plug" | "refresh";
  onClick: () => void;
  disabled: boolean;
  busy: boolean;
}

interface RailProps {
  page: PageId;
  onNavigate: (page: PageId) => void;
  profile: Profile | null;
  connected: boolean;
  fab: FabAction;
  onHelp: () => void;
}

function Fab({ fab, className = "" }: { fab: FabAction; className?: string }) {
  return (
    <button type="button" className={`m3-fab ${className}`} onClick={fab.onClick} disabled={fab.disabled} title={fab.label} aria-label={fab.label}>
      {fab.busy ? <span className="m3-btn-spinner" aria-hidden="true" /> : <Icon name={fab.icon} size={24} />}
      <span className="m3-fab-label label-large">{fab.label}</span>
    </button>
  );
}

/** レールの中身（常設とモーダルで共有する）。 */
function RailBody({
  page,
  onNavigate,
  profile,
  connected,
  fab,
  onHelp,
  menuButton,
}: RailProps & { menuButton: { label: string; icon: "menu" | "menuOpen"; onClick: () => void } }) {
  const privacy = usePrivacy();
  return (
    <>
      <div className="m3-rail-top">
        <IconButton icon={menuButton.icon} label={menuButton.label} onClick={menuButton.onClick} />
        <div className="m3-rail-brand">
          <Icon name="bot" size={22} filled />
          <span className="title-medium">Persona Bot</span>
        </div>
      </div>

      <Fab fab={fab} />

      <nav className="m3-rail-items" aria-label="メインメニュー">
        {PAGES.map((item, index) => {
          const active = item.id === page;
          return (
            <button
              key={item.id}
              type="button"
              className={`m3-rail-item ${active ? "is-active" : ""}`}
              aria-current={active ? "page" : undefined}
              aria-keyshortcuts={String(index + 1)}
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
          <Icon name={connected ? (profile?.environment === "production" ? "shield" : "dns") : "linkOff"} size={20} />
          <div>
            <span className="label-small">接続先</span>
            <strong className="label-large">{connected && profile ? privacy.mask(profile.label) : "未接続"}</strong>
            {connected && profile && (
              <span className="label-small m3-env-tag">{profile.environment === "production" ? "本番環境" : "開発環境"}</span>
            )}
          </div>
        </div>
        <button type="button" className="m3-rail-help" onClick={onHelp} title="ショートカット (?)" aria-keyshortcuts="?">
          <Icon name="keyboard" size={20} />
          <span className="label-large">ショートカット</span>
          <Kbd>?</Kbd>
        </button>
      </div>
    </>
  );
}

/** 常設のレール（600px 以上）。expanded は 1200px 以上のときだけ効く。 */
export function NavigationRail({
  expanded,
  onMenu,
  ...props
}: RailProps & { expanded: boolean; onMenu: () => void }) {
  return (
    <aside className={`m3-rail ${expanded ? "is-expanded" : ""}`}>
      <RailBody
        {...props}
        menuButton={{
          label: expanded ? "ナビゲーションを折りたたむ ([)" : "ナビゲーションを広げる ([)",
          icon: expanded ? "menuOpen" : "menu",
          onClick: onMenu,
        }}
      />
    </aside>
  );
}

/** 画面の上に重ねて開く、広げたレール（1200px 未満）。項目を選ぶ・Esc・外側のクリックで閉じる。 */
export function ModalNavigationRail({ open, onClose, onNavigate, onHelp, ...props }: RailProps & { open: boolean; onClose: () => void }) {
  const panelRef = useRef<HTMLElement>(null);
  useModalFocus(open, panelRef);
  return (
    <div
      className={`m3-modal-rail-scrim ${open ? "is-open" : ""}`}
      inert={!open}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <aside ref={panelRef} className="m3-rail is-expanded is-modal" role="dialog" aria-modal="true" aria-label="ナビゲーション">
        <RailBody
          {...props}
          onNavigate={(page) => {
            onNavigate(page);
            onClose();
          }}
          onHelp={() => {
            onClose();
            onHelp();
          }}
          menuButton={{ label: "ナビゲーションを閉じる ([)", icon: "menuOpen", onClick: onClose }}
        />
      </aside>
    </div>
  );
}

export function NavigationBar({ page, onNavigate }: { page: PageId; onNavigate: (page: PageId) => void }) {
  return (
    <nav className="m3-nav-bar" aria-label="メインメニュー">
      {PAGES.map((item, index) => {
        const active = item.id === page;
        return (
          <button
            key={item.id}
            type="button"
            className={`m3-nav-bar-item ${active ? "is-active" : ""}`}
            aria-current={active ? "page" : undefined}
            aria-keyshortcuts={String(index + 1)}
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

/** 狭い画面（600px 未満）で、ナビゲーションバーの上に浮かぶ FAB。 */
export function CompactFab({ fab }: { fab: FabAction }) {
  return <Fab fab={fab} className="m3-fab-compact" />;
}
