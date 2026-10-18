/** 接続世代・デプロイ・サービス操作の非同期処理と後片付け。 */
import { useCallback, useEffect, useRef, useState, type RefObject } from "react";
import { api, errorMessage } from "./api";
import { usePolling } from "./hooks";
import { RequestScope } from "./requestScope";
import type { ToastInput } from "./components/toast";
import type { DeployRecord, DeployStatus, ServiceAction } from "./types";

type ServiceTarget = { profile_id: string; connected_at: number };
type Scope = { requestScope: RefObject<RequestScope>; wasConnected: RefObject<boolean>; connectionChanging: RefObject<boolean> };
type Notify = (toast: ToastInput) => void;

export function useConnectionScope() {
  const requestScope = useRef(new RequestScope());
  const connectionChanging = useRef(false);
  const wasConnected = useRef(false);
  const dataConnectionKey = useRef("");
  const connectionProfileId = useRef<string | null>(null);
  useEffect(() => () => requestScope.current.invalidate(), []);
  return { requestScope, connectionChanging, wasConnected, dataConnectionKey, connectionProfileId };
}

// デプロイが終わったときの通知
const DEPLOY_DONE: Record<Exclude<DeployStatus, "running">, { tone: "good" | "critical" | "info"; title: string }> = {
  succeeded: { tone: "good", title: "デプロイしました" },
  up_to_date: { tone: "info", title: "最新でした（何も変えていません）" },
  failed: { tone: "critical", title: "デプロイを止めました（何も変えていません）" },
  rolled_back: { tone: "critical", title: "デプロイに失敗したため、前の版へ戻しました" },
  rollback_failed: { tone: "critical", title: "巻き戻しにも失敗しました。手動の対応が必要です" },
};
const ACTION_LABELS: Record<ServiceAction, string> = { start: "起動", stop: "停止", restart: "再起動" };

export function useDeployController(
  { requestScope, wasConnected, connectionChanging, connectionProfileId }: Scope & { connectionProfileId: RefObject<string | null> },
  notify: Notify, refreshAfterChange: () => void, connected: boolean, connectionKey: string,
) {
  const [deploy, setDeploy] = useState<DeployRecord | null>(null);
  const [deployRunning, setDeployRunning] = useState(false);
  const deployWasRunning = useRef(false);
  const deployStarting = useRef<(() => boolean) | null>(null);
  const refreshDeploy = useCallback(async () => {
    if (!wasConnected.current || connectionChanging.current || deployStarting.current?.()) return;
    const isCurrent = requestScope.current.start("deploy");
    try {
      const result = await api.deploy();
      if (!isCurrent()) return;
      if (result.deploy && result.deploy.profile_id !== connectionProfileId.current) return;
      setDeploy(result.deploy);
      setDeployRunning(result.running);
      // 実行中 → 終わった、に変わったら結果を知らせ、状態を取り直す
      if (deployWasRunning.current && !result.running && result.deploy && result.deploy.status !== "running") {
        const done = DEPLOY_DONE[result.deploy.status];
        notify({ tone: done.tone, title: done.title, detail: result.deploy.message });
        refreshAfterChange();
      }
      deployWasRunning.current = result.running;
    } catch {
      // 記録は補助情報なので、取れなくても画面は止めない
    }
  }, [notify, refreshAfterChange]);
  usePolling(refreshDeploy, deployRunning ? 1000 : 15000, connected, connectionKey);

  const runDeploy = useCallback(async (target: ServiceTarget) => {
    if (!wasConnected.current || connectionChanging.current || deployStarting.current?.()) return;
    const isCurrent = requestScope.current.start("deploy");
    deployStarting.current = isCurrent;
    try {
      const result = await api.startDeploy(target);
      if (!isCurrent()) return;
      setDeploy(result.deploy);
      setDeployRunning(result.running);
      deployWasRunning.current = result.running;
      notify({ tone: "info", title: "デプロイを始めました", detail: "サービス操作のページで、1ステップずつ進み具合を確かめられます。" });
    } catch (error) {
      if (!isCurrent()) return;
      notify({ tone: "critical", title: "デプロイを始められませんでした", detail: errorMessage(error) });
    } finally {
      if (deployStarting.current === isCurrent) deployStarting.current = null;
    }
  }, [notify]);

  return { deploy, deployRunning, deployWasRunning, setDeploy, setDeployRunning, refreshDeploy, runDeploy };
}

export function useServiceController(
  { requestScope, wasConnected, connectionChanging }: Scope,
  notify: Notify, refreshOperations: () => Promise<void>, refreshAfterChange: () => void,
) {
  const [actionState, setActionState] = useState<{ action: ServiceAction; current: () => boolean } | null>(null);
  const busyAction = actionState?.current() ? actionState.action : null;
  const runAction = useCallback(
    async (action: ServiceAction, target: ServiceTarget) => {
      if (!wasConnected.current || connectionChanging.current) return;
      const isCurrent = requestScope.current.current();
      setActionState({ action, current: isCurrent });
      try {
        const operation = await api.service(action, target);
        if (!isCurrent()) return;
        notify(
          operation.ok
            ? { tone: "good", title: operation.message }
            : { tone: "critical", title: `${ACTION_LABELS[action]}に失敗しました`, detail: operation.message },
        );
      } catch (error) {
        if (!isCurrent()) return;
        notify({ tone: "critical", title: `${ACTION_LABELS[action]}できませんでした`, detail: errorMessage(error) });
      } finally {
        if (isCurrent()) {
          setActionState(null);
          void refreshOperations();
          refreshAfterChange();
          requestScope.current.schedule(() => { if (isCurrent()) refreshAfterChange(); }, 2500); // 起動直後の状態変化を拾う
        }
      }
    },
    [notify, refreshOperations, refreshAfterChange],
  );

  return { busyAction, runAction };
}
