// M3 のスナックバー（画面下に短く出る通知）。
import { createContext, useCallback, useContext, useState, type ReactNode } from "react";
import { Icon } from "./Icon";

export interface ToastInput {
  tone: "good" | "critical" | "info";
  title: string;
  detail?: string;
}

interface Toast extends ToastInput {
  id: number;
  leaving: boolean;
}

const VISIBLE_MS = 5000;
const LEAVE_MS = 260;

const ToastContext = createContext<(toast: ToastInput) => void>(() => undefined);

let nextId = 1;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const dismiss = useCallback((id: number) => {
    setToasts((previous) => previous.map((t) => (t.id === id ? { ...t, leaving: true } : t)));
    window.setTimeout(() => setToasts((previous) => previous.filter((t) => t.id !== id)), LEAVE_MS);
  }, []);

  const notify = useCallback(
    (toast: ToastInput) => {
      const id = nextId++;
      setToasts((previous) => [...previous.slice(-2), { ...toast, id, leaving: false }]);
      window.setTimeout(() => dismiss(id), VISIBLE_MS);
    },
    [dismiss],
  );

  return (
    <ToastContext.Provider value={notify}>
      {children}
      <div className="m3-snackbars" aria-live="polite">
        {toasts.map((toast) => (
          <div key={toast.id} className={`m3-snackbar tone-${toast.tone} ${toast.leaving ? "is-leaving" : ""}`}>
            <Icon name={toast.tone === "good" ? "checkCircle" : toast.tone === "critical" ? "error" : "info"} size={20} filled />
            <div className="m3-snackbar-text">
              <strong className="body-medium">{toast.title}</strong>
              {toast.detail && <p className="body-small">{toast.detail}</p>}
            </div>
            <button type="button" className="m3-snackbar-close" onClick={() => dismiss(toast.id)} aria-label="閉じる">
              <Icon name="close" size={20} />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  return useContext(ToastContext);
}
