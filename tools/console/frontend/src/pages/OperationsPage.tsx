import type { ReactNode } from "react";
import { LoadingIndicator } from "../components/expressive";
import { Icon, type IconName } from "../components/Icon";
import { Button, Card, EmptyState, StatusChip } from "../components/ui";
import { formatDateTime } from "../format";
import { usePrivacy } from "../privacy";
import { serviceTone } from "../status";
import type { Operation, Profile, ServiceAction, ServiceStatus } from "../types";
import { NotConnected } from "./shared";

const ACTIONS: { action: ServiceAction; title: string; description: string; icon: IconName; variant: "filled" | "danger" | "tonal" }[] = [
  { action: "restart", title: "再起動", description: "設定や .env を変えたあとに。数秒間ボットが応答しなくなります。", icon: "restart", variant: "filled" },
  { action: "stop", title: "停止", description: "ボットを止めます。自分で起動するまで Discord に戻りません。", icon: "stop", variant: "danger" },
  { action: "start", title: "起動", description: "止まっているボットを起動します。", icon: "play", variant: "tonal" },
];

const ACTION_LABELS: Record<ServiceAction, string> = { start: "起動", stop: "停止", restart: "再起動" };

const DEPLOY_STEPS = [
  { title: "git pull", detail: "main の最新を取得" },
  { title: "pip install -r requirements.txt", detail: "依存関係を更新（増えたときだけ）" },
  { title: "sudo systemctl restart bot", detail: "新しいコードで起動" },
  { title: "ヘルスチェック", detail: "ログイン成功のログが出るまで待つ" },
];

const SAFETY: { icon: IconName; text: ReactNode }[] = [
  { icon: "shield", text: <>実行できるのは <code>start / stop / restart</code> だけ。画面から任意のコマンドは送れません（バックエンドのホワイトリスト）。</> },
  { icon: "bolt", text: <><code>sudo -n</code> で実行するため、パスワードを求められたら待たずに失敗します（入力待ちで固まらない）。</> },
  { icon: "check", text: <>停止はサービス名の入力で確認し、本番環境では確認ダイアログに警告を出します。</> },
  { icon: "plug", text: <>バックエンドは 127.0.0.1 だけで待ち受け、専用ヘッダーの無い要求（ほかのサイトからの呼び出し）を断ります。</> },
];

export function OperationsPage({
  connected,
  connecting,
  onConnect,
  selectedProfile,
  profile,
  service,
  busyAction,
  onRequestAction,
  operations,
}: {
  connected: boolean;
  connecting: boolean;
  onConnect: () => void;
  selectedProfile: Profile | null;
  profile: Profile | null;
  service: ServiceStatus | null;
  busyAction: ServiceAction | null;
  onRequestAction: (action: ServiceAction) => void;
  operations: Operation[];
}) {
  const privacy = usePrivacy();
  const tone = serviceTone(service);
  const active = service?.active_state === "active";

  return (
    <div className="grid">
      {connected ? (
        <Card title="サービス操作" icon="power" source="ssh" className="span-12">
          <div className="ops-status">
            <span className="label-large muted">現在の状態</span>
            <StatusChip tone={tone.tone} label={tone.label} pulse={active} />
            <code className="body-small">{profile ? `sudo -n systemctl <操作> ${profile.service}` : ""}</code>
            {profile?.environment === "production" && <StatusChip tone="serious" icon="shield" label="本番環境に接続中" />}
          </div>
          <div className="action-grid">
            {ACTIONS.map((item) => {
              const disabled = busyAction !== null || !service || (item.action === "start" && active) || (item.action === "stop" && !active);
              return (
                <div key={item.action} className={`action-card action-${item.variant}`}>
                  <span className="action-icon">{busyAction === item.action ? <LoadingIndicator size={40} /> : <Icon name={item.icon} size={28} />}</span>
                  <strong className="title-large">{item.title}</strong>
                  <span className="body-medium muted">{item.description}</span>
                  <Button variant={item.variant} size="m" icon={item.icon} onClick={() => onRequestAction(item.action)} disabled={disabled}>
                    {busyAction === item.action ? `${item.title}しています…` : item.title}
                  </Button>
                </div>
              );
            })}
          </div>
        </Card>
      ) : (
        <div className="span-12">
          <NotConnected profile={selectedProfile} connecting={connecting} onConnect={onConnect} />
        </div>
      )}

      <Card title="操作の記録（このセッション）" icon="history" source="ssh" className="span-7">
        {operations.length ? (
          <div className="table-wrap">
            <table className="m3-table">
              <thead>
                <tr>
                  <th>時刻</th>
                  <th>操作</th>
                  <th>接続先</th>
                  <th>結果</th>
                  <th className="num">所要</th>
                </tr>
              </thead>
              <tbody>
                {operations.map((operation) => (
                  <tr key={operation.id}>
                    <td className="mono nowrap">{formatDateTime(operation.at * 1000)}</td>
                    <td>{ACTION_LABELS[operation.action]}</td>
                    <td>
                      {operation.profile_id}
                      {operation.environment === "production" && <span className="m3-tag is-prod">本番</span>}
                    </td>
                    <td>
                      <StatusChip tone={operation.ok ? "good" : "critical"} label={operation.ok ? "成功" : "失敗"} />
                      {!operation.ok && <p className="body-small muted">{privacy.redact(operation.message)}</p>}
                    </td>
                    <td className="num mono">{(operation.duration_ms / 1000).toFixed(1)}s</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState icon="history" title="まだ操作していません">
            <p className="body-medium">起動・停止・再起動をすると、ここに記録されます（バックエンドを再起動すると消えます）。</p>
          </EmptyState>
        )}
      </Card>

      <Card title="デプロイ（未実装）" icon="rocket" className="span-5" variant="outlined">
        <ol className="steps">
          {DEPLOY_STEPS.map((step, index) => (
            <li key={step.title}>
              <span className="step-index label-large">{index + 1}</span>
              <div>
                <strong className="body-large mono">{step.title}</strong>
                <span className="body-small muted">{step.detail}</span>
              </div>
            </li>
          ))}
        </ol>
        <Button variant="outlined" icon="rocket" disabled title="未実装">
          デプロイ（未実装）
        </Button>
        <span className="body-small muted">画面の案だけです。実装するなら、途中で失敗したときの巻き戻し方を先に決めておく必要があります。</span>
      </Card>

      <Card title="誤操作を防ぐ仕組み" icon="shield" className="span-12" variant="outlined">
        <ul className="safety-list">
          {SAFETY.map((item, index) => (
            <li key={index}>
              <span className="safety-icon">
                <Icon name={item.icon} size={20} />
              </span>
              <span className="body-medium">{item.text}</span>
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}
