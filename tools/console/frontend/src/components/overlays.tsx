// M3 の基本ダイアログ（角丸 28px・上にアイコン・見出しは headline small）。
// 閉じる動きを見せるため、閉じても DOM に残して中身だけ保持する。
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Icon, type IconName } from "./Icon";
import { Button, IconButton, Kbd } from "./ui";

export interface ConfirmRequest {
  title: string;
  body: ReactNode;
  confirmLabel: string;
  tone: "danger" | "primary";
  icon: IconName;
  /** これと同じ文字を入力しないと実行できない（停止など影響の大きい操作用） */
  requireText?: string;
  onConfirm: () => void;
}

export function ConfirmDialog({ request, open, onClose }: { request: ConfirmRequest | null; open: boolean; onClose: () => void }) {
  const [typed, setTyped] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    setTyped("");
    const timer = window.setTimeout(() => (request?.requireText ? inputRef.current : confirmRef.current)?.focus(), 120);
    return () => window.clearTimeout(timer);
  }, [open, request]);

  const allowed = !request?.requireText || typed === request.requireText;

  return (
    <div
      className={`m3-scrim ${open ? "is-open" : ""}`}
      inert={!open}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className={`m3-dialog ${request?.tone === "danger" ? "is-danger" : ""}`} role="alertdialog" aria-modal="true" aria-labelledby="confirm-title">
        <span className="m3-dialog-icon">
          <Icon name={request?.icon ?? "info"} size={24} />
        </span>
        <h2 id="confirm-title" className="headline-small">
          {request?.title}
        </h2>
        <div className="m3-dialog-body body-medium">{request?.body}</div>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (allowed && request) {
              request.onConfirm();
              onClose();
            }
          }}
        >
          {request?.requireText && (
            <label className="m3-text-field">
              <input
                ref={inputRef}
                value={typed}
                onChange={(event) => setTyped(event.target.value)}
                autoComplete="off"
                spellCheck={false}
                placeholder=" "
              />
              <span className="m3-text-field-label">確認のため「{request.requireText}」と入力</span>
            </label>
          )}
          <div className="m3-dialog-actions">
            <Button variant="text" onClick={onClose}>
              キャンセル
            </Button>
            <Button ref={confirmRef} type="submit" variant={request?.tone === "danger" ? "danger" : "filled"} disabled={!allowed}>
              {request?.confirmLabel ?? "実行"}
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}

// 「~」で始まる要素はキーではなく区切りの文字として表示する
const SHORTCUTS: { keys: string[]; label: string }[] = [
  { keys: ["1", "~〜", "6"], label: "ページを切り替える" },
  { keys: ["j", "~/", "k"], label: "次 / 前のページへ" },
  { keys: ["g", "~→", "o l s d g c"], label: "概要 / ログ / 操作 / データ / サーバー / 接続 へ移動" },
  { keys: ["r"], label: "今すぐ更新" },
  { keys: ["a"], label: "自動更新の ON / OFF" },
  { keys: ["/"], label: "ログを絞り込む" },
  { keys: ["p"], label: "ログのライブ表示を一時停止 / 再開" },
  { keys: ["["], label: "ナビゲーションレールの展開 / 折りたたみ" },
  { keys: ["h"], label: "ホスト名などを伏せる（スクショ用）" },
  { keys: ["t"], label: "ライト / ダークの切り替え" },
  { keys: ["?"], label: "このヘルプ" },
  { keys: ["Esc"], label: "閉じる / 入力欄から抜ける" },
];

export function ShortcutHelp({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <div
      className={`m3-scrim ${open ? "is-open" : ""}`}
      inert={!open}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="m3-dialog m3-dialog-wide" role="dialog" aria-modal="true" aria-labelledby="shortcut-title">
        <header className="m3-dialog-head">
          <span className="m3-dialog-icon">
            <Icon name="keyboard" size={24} />
          </span>
          <h2 id="shortcut-title" className="headline-small">
            キーボードショートカット
          </h2>
          <IconButton icon="close" label="閉じる" onClick={onClose} />
        </header>
        <ul className="shortcut-list">
          {SHORTCUTS.map((shortcut) => (
            <li key={shortcut.label}>
              <span className="shortcut-keys">
                {shortcut.keys.map((key, index) =>
                  key.startsWith("~") ? (
                    <span key={index} className="shortcut-sep">
                      {key.slice(1)}
                    </span>
                  ) : (
                    <Kbd key={index}>{key}</Kbd>
                  ),
                )}
              </span>
              <span className="body-medium">{shortcut.label}</span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
