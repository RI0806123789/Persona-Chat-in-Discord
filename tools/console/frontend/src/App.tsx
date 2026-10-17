import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, api, errorMessage } from "./api";
import { Icon } from "./components/Icon";
import { ConfirmDialog, ShortcutHelp, type ConfirmRequest } from "./components/overlays";
import { CompactFab, ModalNavigationRail, NavigationBar, NavigationRail, type FabAction } from "./components/Sidebar";
import { ToastProvider, useToast, type ToastInput } from "./components/toast";
import { TopAppBar, type ThemeChoice } from "./components/TopBar";
import { Kbd } from "./components/ui";
import { useLogStream, useMediaQuery, useNow, usePolling, useStoredState } from "./hooks";
import { PAGES, isPageId, type PageId } from "./navigation";
import { ConnectionPage } from "./pages/ConnectionPage";
import { DataPage } from "./pages/DataPage";
import { GuildsPage } from "./pages/GuildsPage";
import { LogsPage } from "./pages/LogsPage";
import { OperationsPage } from "./pages/OperationsPage";
import { OverviewPage } from "./pages/OverviewPage";
import { PrivacyContext, collectSecrets, createPrivacy } from "./privacy";
import { unsetFieldsOf } from "./profiles";
import { SEED_OPTIONS, applyTheme } from "./theme/theme";
import type { ConnectionState, Insights, Operation, Profile, Sample, ServiceAction, StatusResponse } from "./types";

const STATUS_INTERVAL_MS = 5000;
const INSIGHTS_INTERVAL_MS = 60_000; // Discord API の利用制限に配慮して1分ごと（バックエンドも60秒キャッシュする）
const CONNECTION_INTERVAL_MS = 10000;
const MAX_SAMPLES = 60; // 5秒 × 60 = 直近5分ぶん
const GO_KEY_TIMEOUT_MS = 1200; // g を押してから次のキーを待つ時間

// M3 のウィンドウサイズの区分（600 未満 = compact、1200 以上 = large）
const LARGE_QUERY = "(min-width: 1200px)";
const COMPACT_QUERY = "(max-width: 599px)";

const ACTION_LABELS: Record<ServiceAction, string> = { start: "起動", stop: "停止", restart: "再起動" };
const ACTION_EFFECTS: Record<ServiceAction, string> = {
  start: "ボットを起動します。ログイン成功のログが出れば復帰です。",
  stop: "ボットを止めます。Discord では応答しなくなり、自分で起動するまで戻りません。",
  restart: "ボットを再起動します。数秒間、Discord で応答しなくなります（ボイス接続や自律配信も切れます）。",
};

export default function App() {
  return (
    <ToastProvider>
      <Console />
    </ToastProvider>
  );
}

function toSample(status: StatusResponse): Sample {
  const memory = status.resources?.memory;
  return {
    t: status.fetched_at * 1000,
    rtt: status.rtt_ms,
    botMemory: status.service?.memory_bytes ?? null,
    hostMemoryRatio: memory && memory.total > 0 ? memory.used / memory.total : null,
    load1: status.resources?.load?.[0] ?? null,
  };
}

/** 入力中（テキスト欄など）かどうか。入力中は1文字のショートカットを効かせない。 */
function isTyping(target: EventTarget | null): boolean {
  const element = target as HTMLElement | null;
  return !!element && (element.tagName === "INPUT" || element.tagName === "TEXTAREA" || element.tagName === "SELECT" || element.isContentEditable);
}

function Console() {
  const rawNotify = useToast();
  const now = useNow();

  // ---- 表示の設定（ブラウザに覚えておく）
  const [storedPage, setStoredPage] = useStoredState<string>("pbc.page", "overview");
  const page: PageId = isPageId(storedPage) ? storedPage : "overview";
  const [themeChoice, setThemeChoice] = useStoredState<ThemeChoice>("pbc.theme", null);
  const systemDark = useMediaQuery("(prefers-color-scheme: dark)");
  const theme = themeChoice ?? (systemDark ? "dark" : "light");
  const [seedId, setSeedId] = useStoredState("pbc.seed", SEED_OPTIONS[0].id);
  const [railExpanded, setRailExpanded] = useStoredState("pbc.railExpanded", true);
  const [privacyOn, setPrivacyOn] = useStoredState("pbc.privacy", false);
  const [autoRefresh, setAutoRefresh] = useStoredState("pbc.autoRefresh", true);
  const [selectedProfileId, setSelectedProfileId] = useStoredState("pbc.profile", "");
  const large = useMediaQuery(LARGE_QUERY);
  const compact = useMediaQuery(COMPACT_QUERY);

  // ---- サーバーから取る状態
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [profilesSource, setProfilesSource] = useState("");
  const [profilesError, setProfilesError] = useState<string | null>(null);
  const [backendError, setBackendError] = useState<string | null>(null);
  const [connection, setConnection] = useState<ConnectionState | null>(null);
  const [connecting, setConnecting] = useState(false);
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [samples, setSamples] = useState<Sample[]>([]);
  const [insights, setInsights] = useState<Insights | null>(null);
  const [insightsFetchedAt, setInsightsFetchedAt] = useState<number | null>(null);
  const [insightsError, setInsightsError] = useState<string | null>(null);
  const [operations, setOperations] = useState<Operation[]>([]);
  const [busyAction, setBusyAction] = useState<ServiceAction | null>(null);

  // ---- 画面の部品
  const [confirm, setConfirm] = useState<ConfirmRequest | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [modalRailOpen, setModalRailOpen] = useState(false);
  const [goPending, setGoPending] = useState(false);
  const [refreshToken, setRefreshToken] = useState(0);
  const filterInputRef = useRef<HTMLInputElement>(null);
  const contentRef = useRef<HTMLElement>(null);
  const wasConnected = useRef(false);

  const connected = connection?.connected ?? false;
  const stream = useLogStream(connected);
  const selectedProfile = profiles.find((p) => p.id === selectedProfileId) ?? null;
  const pageDef = PAGES.find((p) => p.id === page) ?? PAGES[0];
  const production = connected && connection?.profile?.environment === "production";

  // ---- 伏せ字モード。画面が知っているホスト名・ボット名・サーバー名などを集め、文の中からも隠す
  const secrets = useMemo(
    () => collectSecrets({ profile: connection?.profile ?? null, profiles, connection, insights }),
    [connection, profiles, insights],
  );
  const privacy = useMemo(() => createPrivacy(privacyOn, secrets), [privacyOn, secrets]);
  const privacyRef = useRef(privacy);
  privacyRef.current = privacy;
  // 通知の詳細（接続エラーの文など）にもホスト名が入りうるので、出す前に伏せる
  const notify = useCallback(
    (toast: ToastInput) => rawNotify({ ...toast, detail: toast.detail && privacyRef.current.redact(toast.detail) }),
    [rawNotify],
  );

  useEffect(() => {
    if (themeChoice) document.documentElement.dataset.theme = themeChoice;
    else delete document.documentElement.dataset.theme;
  }, [themeChoice]);

  useEffect(() => {
    applyTheme((SEED_OPTIONS.find((seed) => seed.id === seedId) ?? SEED_OPTIONS[0]).hex);
  }, [seedId]);

  // 広い画面になったら、重ねて開いていたレールは常設のレールに置き換わるので閉じる
  useEffect(() => {
    if (large) setModalRailOpen(false);
  }, [large]);

  const navigate = useCallback(
    (next: PageId) => {
      setStoredPage(next);
      contentRef.current?.scrollTo({ top: 0 });
    },
    [setStoredPage],
  );

  const toggleRail = useCallback(() => {
    if (large) setRailExpanded((value) => !value);
    else setModalRailOpen((value) => !value);
  }, [large, setRailExpanded]);

  // ---- 接続状態
  const applyConnection = useCallback(
    (state: ConnectionState) => {
      if (wasConnected.current && !state.connected && state.last_error) {
        notify({ tone: "critical", title: "SSH 接続が切れました", detail: state.last_error });
      }
      if (!state.connected) {
        setStatus(null);
        setSamples([]);
        setInsights(null);
        setInsightsFetchedAt(null);
      }
      wasConnected.current = state.connected;
      setConnection(state);
    },
    [notify],
  );

  const refreshConnection = useCallback(async () => {
    try {
      applyConnection(await api.connection());
      setBackendError(null);
    } catch (error) {
      setBackendError(errorMessage(error));
    }
  }, [applyConnection]);
  usePolling(refreshConnection, CONNECTION_INTERVAL_MS, true);

  // 接続中は、選択欄を「実際に接続している接続先」に合わせる（別のタブや再読み込みで食い違わないように）
  const connectedProfileId = connected ? (connection?.profile?.id ?? null) : null;
  useEffect(() => {
    if (connectedProfileId) setSelectedProfileId(connectedProfileId);
  }, [connectedProfileId, setSelectedProfileId]);

  useEffect(() => {
    api
      .profiles()
      .then((result) => {
        setProfiles(result.profiles);
        setProfilesSource(result.source);
        setProfilesError(null);
        setSelectedProfileId((current) =>
          result.profiles.some((p) => p.id === current) ? current : (result.profiles[0]?.id ?? ""),
        );
      })
      .catch((error: unknown) => setProfilesError(errorMessage(error)));
  }, [setSelectedProfileId]);

  // ---- 状態の定期取得
  const refreshStatus = useCallback(async () => {
    setRefreshing(true);
    try {
      const next = await api.status();
      setStatus(next);
      setStatusError(null);
      setSamples((previous) => [...previous, toSample(next)].slice(-MAX_SAMPLES));
    } catch (error) {
      setStatusError(errorMessage(error));
      if (error instanceof ApiError && (error.status === 409 || error.status === 502)) void refreshConnection();
    } finally {
      setRefreshing(false);
    }
  }, [refreshConnection]);
  usePolling(refreshStatus, STATUS_INTERVAL_MS, connected && autoRefresh);

  const refreshInsights = useCallback(async (force = false) => {
    try {
      const result = await api.insights(force);
      setInsights(result.data);
      setInsightsFetchedAt(result.fetched_at);
      setInsightsError(null);
    } catch (error) {
      setInsightsError(errorMessage(error));
    }
  }, []);
  usePolling(refreshInsights, INSIGHTS_INTERVAL_MS, connected && autoRefresh);

  useEffect(() => {
    // 自動更新を切っていても、接続した直後に1回は取る
    if (connected && !autoRefresh) {
      void refreshStatus();
      void refreshInsights();
    }
    // 接続が変わったときだけ動かす（自動更新の切り替えでは取り直さない）
  }, [connected]);

  const refreshOperations = useCallback(async () => {
    try {
      setOperations((await api.operations()).operations);
    } catch {
      // 記録は補助情報なので、取れなくても画面は止めない
    }
  }, []);
  useEffect(() => {
    void refreshOperations();
  }, [refreshOperations, page]);

  // ---- 接続・切断
  const handleConnect = useCallback(async () => {
    if (!selectedProfileId) return;
    // 見本の値のままの接続先は、バックエンドも断るが、要求を送る前に直し方を案内する
    const target = profiles.find((p) => p.id === selectedProfileId);
    const unset = unsetFieldsOf(target);
    if (target && unset.length > 0) {
      notify({
        tone: "critical",
        title: `「${target.label}」はまだ設定されていません`,
        // 見本（Git に含まれる）へ実際の接続先を書かせないよう、見本を読んでいるときはコピーから案内する
        detail:
          profilesSource === "profiles.json"
            ? `backend/profiles.json の YOUR_ で始まる値（${unset.join("・")}）を書き換えてください。手順は tools/console/README.md にあります。`
            : `backend/profiles.example.json を backend/profiles.json にコピーし、YOUR_ で始まる値（${unset.join("・")}）を書き換えてください。手順は tools/console/README.md にあります。`,
      });
      return;
    }
    setConnecting(true);
    try {
      const state = await api.connect(selectedProfileId);
      applyConnection(state);
      notify({ tone: "good", title: "SSH で接続しました", detail: state.profile?.label });
    } catch (error) {
      notify({
        tone: "critical",
        title: "接続できませんでした",
        detail: errorMessage(error),
        action: { label: "再試行", onClick: () => void handleConnectRef.current() },
      });
      void refreshConnection();
    } finally {
      setConnecting(false);
    }
  }, [selectedProfileId, profiles, profilesSource, applyConnection, notify, refreshConnection]);
  // 通知の「再試行」からは、その時点の最新の接続処理を呼ぶ
  const handleConnectRef = useRef(handleConnect);
  handleConnectRef.current = handleConnect;

  const handleDisconnect = useCallback(async () => {
    try {
      applyConnection(await api.disconnect());
      notify({ tone: "info", title: "切断しました" });
    } catch (error) {
      notify({ tone: "critical", title: "切断できませんでした", detail: errorMessage(error) });
    }
  }, [applyConnection, notify]);

  // ---- サービス操作
  const runAction = useCallback(
    async (action: ServiceAction) => {
      setBusyAction(action);
      try {
        const operation = await api.service(action);
        notify(
          operation.ok
            ? { tone: "good", title: operation.message }
            : { tone: "critical", title: `${ACTION_LABELS[action]}に失敗しました`, detail: operation.message },
        );
      } catch (error) {
        notify({ tone: "critical", title: `${ACTION_LABELS[action]}できませんでした`, detail: errorMessage(error) });
      } finally {
        setBusyAction(null);
        void refreshOperations();
        void refreshStatus();
        window.setTimeout(() => void refreshStatus(), 2500); // 起動直後の状態変化を拾う
      }
    },
    [notify, refreshOperations, refreshStatus],
  );

  const requestAction = useCallback(
    (action: ServiceAction) => {
      const profile = connection?.profile;
      if (!profile || busyAction) return;
      const isProduction = profile.environment === "production";
      setConfirm({
        title: `${profile.service} を${ACTION_LABELS[action]}しますか？`,
        icon: action === "stop" ? "stop" : action === "start" ? "play" : "restart",
        body: (
          <>
            <p>{ACTION_EFFECTS[action]}</p>
            <p className="dialog-target">
              接続先: <strong>{profile.label}</strong>
            </p>
            {isProduction && (
              <p className="dialog-warning">
                <Icon name="alert" size={20} filled />
                本番環境です。Discord の利用者全員に影響します。
              </p>
            )}
          </>
        ),
        confirmLabel: `${ACTION_LABELS[action]}する`,
        tone: action === "stop" || (isProduction && action === "restart") ? "danger" : "primary",
        requireText: action === "stop" ? profile.service : undefined,
        onConfirm: () => void runAction(action),
      });
      setConfirmOpen(true);
    },
    [connection, busyAction, runAction],
  );

  const manualRefresh = useCallback(() => {
    if (!connected) return;
    void refreshStatus();
    void refreshInsights(true);
    setRefreshToken((n) => n + 1);
  }, [connected, refreshStatus, refreshInsights]);

  const toggleTheme = useCallback(() => setThemeChoice(theme === "dark" ? "light" : "dark"), [theme, setThemeChoice]);

  // ---- キーボード操作（Vim 風）。最新の処理を ref 経由で呼ぶので、登録は1回だけ
  const keyHandler = useRef<(event: KeyboardEvent) => void>(() => undefined);
  const goTimer = useRef<number | undefined>(undefined);
  const cancelGo = () => {
    window.clearTimeout(goTimer.current);
    setGoPending(false);
  };
  keyHandler.current = (event: KeyboardEvent) => {
    if (event.ctrlKey || event.metaKey || event.altKey || event.defaultPrevented) return;
    const target = event.target as HTMLElement | null;
    // メニューの中のキーはメニューが扱う（↓↑ j k など）
    if (target?.closest?.('[role="menu"]')) return;
    const typing = isTyping(target);

    if (event.key === "Escape") {
      if (goPending) cancelGo();
      else if (confirmOpen) setConfirmOpen(false);
      else if (helpOpen) setHelpOpen(false);
      else if (modalRailOpen) setModalRailOpen(false);
      else if (typing) target?.blur();
      return;
    }
    if (typing || confirmOpen) return;
    if (helpOpen) {
      if (event.key === "?") setHelpOpen(false);
      return;
    }

    if (goPending) {
      cancelGo();
      const destination = PAGES.find((p) => p.goKey === event.key);
      if (destination) {
        event.preventDefault();
        navigate(destination.id);
        setModalRailOpen(false);
      }
      return;
    }

    const index = PAGES.findIndex((p) => p.id === page);
    if (/^[1-6]$/.test(event.key)) {
      navigate(PAGES[Number(event.key) - 1].id);
      setModalRailOpen(false);
      return;
    }
    switch (event.key) {
      case "g":
        setGoPending(true);
        window.clearTimeout(goTimer.current);
        goTimer.current = window.setTimeout(() => setGoPending(false), GO_KEY_TIMEOUT_MS);
        break;
      case "j":
        navigate(PAGES[(index + 1) % PAGES.length].id);
        break;
      case "k":
        navigate(PAGES[(index - 1 + PAGES.length) % PAGES.length].id);
        break;
      case "r":
        manualRefresh();
        break;
      case "a":
        setAutoRefresh((value) => !value);
        break;
      case "/":
        event.preventDefault();
        setModalRailOpen(false);
        navigate("logs");
        window.setTimeout(() => filterInputRef.current?.focus(), 60);
        break;
      case "p":
        if (connected) stream.setPaused(!stream.paused);
        break;
      case "[":
        toggleRail();
        break;
      case "h":
        setPrivacyOn((value) => !value);
        break;
      case "t":
        toggleTheme();
        break;
      case "?":
        setModalRailOpen(false);
        setHelpOpen(true);
        break;
    }
  };
  useEffect(() => {
    const listener = (event: KeyboardEvent) => keyHandler.current(event);
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, []);

  // FAB には、その時の主な操作を置く（未接続なら接続、接続中なら更新）
  const fab: FabAction = connected
    ? { label: "今すぐ更新", icon: "refresh", onClick: manualRefresh, disabled: refreshing, busy: false }
    : { label: "接続", icon: "plug", onClick: () => void handleConnect(), disabled: connecting || !selectedProfileId, busy: connecting };

  const railProps = {
    page,
    onNavigate: navigate,
    profile: connection?.profile ?? selectedProfile,
    connected,
    fab,
    onHelp: () => setHelpOpen(true),
  };

  return (
    <PrivacyContext.Provider value={privacy}>
      <div className={`app ${production ? "is-production" : ""} ${compact ? "is-compact" : ""}`}>
        {!compact && <NavigationRail {...railProps} expanded={large && railExpanded} onMenu={toggleRail} />}
        <div className="main">
          <TopAppBar
            page={pageDef}
            connection={connection}
            profiles={profiles}
            profilesSource={profilesSource}
            selectedProfileId={selectedProfileId}
            onSelectProfile={setSelectedProfileId}
            onConnect={() => void handleConnect()}
            onDisconnect={() => void handleDisconnect()}
            connecting={connecting}
            rttMs={status?.rtt_ms ?? null}
            lastUpdated={status ? status.fetched_at * 1000 : null}
            refreshing={refreshing}
            onRefresh={manualRefresh}
            autoRefresh={autoRefresh}
            onToggleAuto={() => setAutoRefresh((value) => !value)}
            onTogglePrivacy={() => setPrivacyOn((value) => !value)}
            themeChoice={themeChoice}
            onSelectTheme={setThemeChoice}
            seedId={seedId}
            onSelectSeed={setSeedId}
            onOpenConnection={() => navigate("connection")}
            showMenuButton={compact}
            onMenu={() => setModalRailOpen(true)}
            now={now}
          />
          {backendError && (
            <div className="m3-banner is-error" role="alert">
              <Icon name="error" size={20} filled />
              <span className="body-medium">{privacy.redact(backendError)}</span>
            </div>
          )}
          {production && (
            <div className="m3-banner is-production">
              <Icon name="shield" size={20} filled />
              <span className="body-medium">本番サーバーに接続中です。操作は Discord の利用者に影響します。</span>
            </div>
          )}
          <main className="content" ref={contentRef}>
            <div key={page} className="page">
              {page === "overview" && (
                <OverviewPage
                  connected={connected}
                  connecting={connecting}
                  status={status}
                  statusError={statusError}
                  samples={samples}
                  connection={connection}
                  logs={stream.entries}
                  insights={insights}
                  insightsFetchedAt={insightsFetchedAt}
                  insightsError={insightsError}
                  onNavigate={navigate}
                  onRequestAction={requestAction}
                  busyAction={busyAction}
                  onConnect={() => void handleConnect()}
                  selectedProfile={selectedProfile}
                  now={now}
                />
              )}
              {page === "logs" && (
                <LogsPage
                  stream={stream}
                  connected={connected}
                  connecting={connecting}
                  onConnect={() => void handleConnect()}
                  selectedProfile={selectedProfile}
                  filterInputRef={filterInputRef}
                />
              )}
              {page === "operations" && (
                <OperationsPage
                  connected={connected}
                  connecting={connecting}
                  onConnect={() => void handleConnect()}
                  selectedProfile={selectedProfile}
                  profile={connection?.profile ?? null}
                  service={status?.service ?? null}
                  busyAction={busyAction}
                  onRequestAction={requestAction}
                  operations={operations}
                />
              )}
              {page === "data" && (
                <DataPage
                  connected={connected}
                  connecting={connecting}
                  onConnect={() => void handleConnect()}
                  selectedProfile={selectedProfile}
                  refreshToken={refreshToken}
                />
              )}
              {page === "guilds" && (
                <GuildsPage
                  connected={connected}
                  connecting={connecting}
                  onConnect={() => void handleConnect()}
                  selectedProfile={selectedProfile}
                  insights={insights}
                  insightsFetchedAt={insightsFetchedAt}
                  insightsError={insightsError}
                  now={now}
                />
              )}
              {page === "connection" && (
                <ConnectionPage
                  profiles={profiles}
                  profilesSource={profilesSource}
                  profilesError={profilesError}
                  selectedProfileId={selectedProfileId}
                  onSelectProfile={setSelectedProfileId}
                  connection={connection}
                  connecting={connecting}
                  onConnect={() => void handleConnect()}
                  onDisconnect={() => void handleDisconnect()}
                  now={now}
                />
              )}
            </div>
          </main>
        </div>
        {compact && (
          <>
            <CompactFab fab={fab} />
            <NavigationBar page={page} onNavigate={navigate} />
          </>
        )}
        {!large && <ModalNavigationRail {...railProps} open={modalRailOpen} onClose={() => setModalRailOpen(false)} />}
        <GoKeyHint visible={goPending} />
        <ConfirmDialog request={confirm} open={confirmOpen} onClose={() => setConfirmOpen(false)} />
        <ShortcutHelp open={helpOpen} onClose={() => setHelpOpen(false)} />
      </div>
    </PrivacyContext.Provider>
  );
}

/** g を押して次のキーを待っている間の表示（Vim の showcmd に当たる）。 */
function GoKeyHint({ visible }: { visible: boolean }) {
  return (
    <div className={`go-key-hint ${visible ? "is-visible" : ""}`} aria-live="polite" role="status">
      {visible && (
        <>
          <Kbd>g</Kbd>
          <span className="body-medium">
            {PAGES.map((p) => (
              <span key={p.id} className="go-key-option">
                <Kbd>{p.goKey}</Kbd> {p.short}
              </span>
            ))}
          </span>
        </>
      )}
    </div>
  );
}
