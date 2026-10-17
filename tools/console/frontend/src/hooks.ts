import { useCallback, useEffect, useRef, useState, type RefObject } from "react";
import { logStreamUrl } from "./api";
import type { LogEntry } from "./types";

/** enabled の間、callback を intervalMs ごとに呼ぶ（前の呼び出しが終わってから次を予約する）。 */
export function usePolling(callback: () => Promise<void> | void, intervalMs: number, enabled: boolean) {
  const saved = useRef(callback);
  useEffect(() => {
    saved.current = callback;
  }, [callback]);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    let timer: number | undefined;
    const tick = async () => {
      try {
        await saved.current();
      } finally {
        if (!cancelled) timer = window.setTimeout(tick, intervalMs);
      }
    };
    void tick();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [intervalMs, enabled]);
}

/** 「◯秒前」の表示を更新するための現在時刻。 */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs]);
  return now;
}

/** localStorage に覚えておく設定。使えない環境（プライベートウィンドウ等）では普通の state として動く。 */
export function useStoredState<T>(key: string, initial: T) {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = window.localStorage.getItem(key);
      return raw === null ? initial : (JSON.parse(raw) as T);
    } catch {
      return initial;
    }
  });
  useEffect(() => {
    try {
      window.localStorage.setItem(key, JSON.stringify(value));
    } catch {
      // 保存できなくても動作は続ける
    }
  }, [key, value]);
  return [value, setValue] as const;
}

/** CSS のメディアクエリ（OS のダークモードなど）の現在値。 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const media = window.matchMedia(query);
    const onChange = () => setMatches(media.matches);
    media.addEventListener("change", onChange);
    onChange();
    return () => media.removeEventListener("change", onChange);
  }, [query]);
  return matches;
}

/** OS の「視差効果を減らす / アニメーションを減らす」設定。途中で切り替えても追従する。 */
export function useReducedMotion(): boolean {
  return useMediaQuery("(prefers-reduced-motion: reduce)");
}

const FOCUSABLE =
  'button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])';

/**
 * モーダル（ダイアログ・モーダルのレール）のフォーカス管理。
 * 開いたら中へフォーカスを移し、Tab / Shift+Tab を中で循環させ、閉じたら開く前の場所へ戻す。
 */
export function useModalFocus(open: boolean, containerRef: RefObject<HTMLElement | null>, initialFocusRef?: RefObject<HTMLElement | null>) {
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    const container = containerRef.current;
    // 開く動きが始まってからフォーカスする（スクロール位置が飛ばないよう preventScroll）
    const timer = window.setTimeout(() => {
      const target = initialFocusRef?.current ?? container?.querySelector<HTMLElement>(FOCUSABLE);
      target?.focus({ preventScroll: true });
    }, 60);

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Tab" || !container) return;
      const items = Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((item) => item.offsetParent !== null);
      if (items.length === 0) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && (document.activeElement === first || !container.contains(document.activeElement))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !container.contains(document.activeElement))) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener("keydown", onKeyDown);
      if (previous && document.contains(previous)) previous.focus({ preventScroll: true });
    };
  }, [open, containerRef, initialFocusRef]);
}

const MAX_LOG_ENTRIES = 3000;
const FLUSH_INTERVAL_MS = 120;
const RECONNECT_DELAY_MS = 4000;

export type StreamStatus = "idle" | "connecting" | "live" | "ended" | "error";

export interface LogStream {
  entries: LogEntry[];
  status: StreamStatus;
  message: string | null;
  paused: boolean;
  pendingCount: number;
  setPaused: (paused: boolean) => void;
  clear: () => void;
}

type StreamMessage =
  | { type: "entry"; entry: LogEntry }
  | { type: "error"; message: string }
  | { type: "end"; message: string };

/**
 * journalctl -f をバックエンド経由（WebSocket）で受け取り続ける。
 * 1件ごとに再描画すると重いので、FLUSH_INTERVAL_MS ごとにまとめて反映する。
 */
export function useLogStream(enabled: boolean, initialLines = 300): LogStream {
  const [entries, setEntries] = useState<LogEntry[]>([]);
  const [status, setStatus] = useState<StreamStatus>("idle");
  const [message, setMessage] = useState<string | null>(null);
  const [paused, setPausedState] = useState(false);
  const [pendingCount, setPendingCount] = useState(0);
  const [reconnectToken, setReconnectToken] = useState(0);
  const bufferRef = useRef<LogEntry[]>([]); // 次の反映で追加する分
  const heldRef = useRef<LogEntry[]>([]); // 一時停止中にたまった分
  const pausedRef = useRef(false);

  useEffect(() => {
    if (!enabled) {
      setStatus("idle");
      return;
    }
    let disposed = false;
    let retryTimer: number | undefined;
    bufferRef.current = [];
    heldRef.current = [];
    setEntries([]);
    setPendingCount(0);
    setStatus("connecting");
    setMessage(null);

    const socket = new WebSocket(logStreamUrl(initialLines));
    socket.onopen = () => {
      if (!disposed) setStatus("live");
    };
    socket.onmessage = (event: MessageEvent<string>) => {
      const data = JSON.parse(event.data) as StreamMessage;
      if (data.type === "entry") {
        (pausedRef.current ? heldRef.current : bufferRef.current).push(data.entry);
      } else {
        setStatus(data.type === "error" ? "error" : "ended");
        setMessage(data.message);
      }
    };
    socket.onclose = () => {
      if (disposed) return;
      setStatus((current) => (current === "error" ? current : "ended"));
      retryTimer = window.setTimeout(() => setReconnectToken((n) => n + 1), RECONNECT_DELAY_MS);
    };

    const flushTimer = window.setInterval(() => {
      if (bufferRef.current.length > 0) {
        const batch = bufferRef.current;
        bufferRef.current = [];
        setEntries((previous) => {
          const merged = previous.concat(batch);
          return merged.length > MAX_LOG_ENTRIES ? merged.slice(-MAX_LOG_ENTRIES) : merged;
        });
      }
      if (pausedRef.current) setPendingCount(heldRef.current.length);
    }, FLUSH_INTERVAL_MS);

    return () => {
      disposed = true;
      window.clearInterval(flushTimer);
      window.clearTimeout(retryTimer);
      socket.close();
    };
  }, [enabled, initialLines, reconnectToken]);

  const setPaused = useCallback((next: boolean) => {
    pausedRef.current = next;
    setPausedState(next);
    if (!next) {
      bufferRef.current.push(...heldRef.current);
      heldRef.current = [];
      setPendingCount(0);
    }
  }, []);

  const clear = useCallback(() => {
    bufferRef.current = [];
    heldRef.current = [];
    setPendingCount(0);
    setEntries([]);
  }, []);

  return { entries, status, message, paused, pendingCount, setPaused, clear };
}
