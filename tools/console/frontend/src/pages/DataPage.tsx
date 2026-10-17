import { useCallback, useEffect, useState } from "react";
import { api, errorMessage } from "../api";
import { LoadingIndicator, WavyProgress } from "../components/expressive";
import { Button, Card, Chip, InlineError, Notice, Stat, StatusChip } from "../components/ui";
import { formatBytes, formatNumber, formatPercent } from "../format";
import type { DbSummary, Profile } from "../types";
import { NotConnected } from "./shared";

// 手元のボットの database.py の SCHEMA_VERSION（本番がこれより古ければ、新しい版がまだデプロイされていない）
const LATEST_SCHEMA_VERSION = 10;

const TABLE_LABELS: Record<string, string> = {
  affinity: "親密度・感情",
  user_stats: "会話統計",
  queue_tasks: "文書処理キュー",
  pending_log: "記憶圧縮前のログ",
  profile: "プロフィール記憶",
  guild_settings: "サーバー設定",
  anniversary_posts: "記念日の投稿記録",
  reminders: "リマインダー",
  feedback: "👍👎 の評価",
};

const STATUS_LABELS: Record<string, string> = {
  pending: "待機",
  processing: "処理中",
  done: "完了",
  failed: "失敗",
  canceled: "取り消し",
};

export function DataPage({
  connected,
  connecting,
  onConnect,
  selectedProfile,
  refreshToken,
}: {
  connected: boolean;
  connecting: boolean;
  onConnect: () => void;
  selectedProfile: Profile | null;
  refreshToken: number;
}) {
  const [summary, setSummary] = useState<DbSummary | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setSummary(await api.db());
      setError(null);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (connected) void load();
  }, [connected, load, refreshToken]);

  if (!connected) {
    return <NotConnected profile={selectedProfile} connecting={connecting} onConnect={onConnect} />;
  }

  return (
    <div className="grid">
      <Card
        title="bot.db"
        icon="database"
        source="ssh"
        className="span-4"
        actions={
          <Button variant="text" size="xs" icon="refresh" onClick={() => void load()} busy={loading}>
            集計し直す
          </Button>
        }
      >
        {error && <InlineError>{error}</InlineError>}
        {summary && !summary.exists && <InlineError>{summary.path} が見つかりません。</InlineError>}
        {!summary && !error && <LoadingIndicator size={40} />}
        {summary?.exists && (
          <>
            <div className="stat-row">
              <Stat label="ファイル" value={formatBytes(summary.size_bytes)} />
              <Stat label="WAL" value={formatBytes(summary.wal_bytes)} />
            </div>
            <div className="schema-row">
              <span className="label-large muted">スキーマ</span>
              <span className="title-medium mono">v{summary.schema_version ?? "?"}</span>
              {summary.schema_version === LATEST_SCHEMA_VERSION ? (
                <StatusChip tone="good" label="最新" />
              ) : (
                <StatusChip tone="warning" label={`最新は v${LATEST_SCHEMA_VERSION}`} />
              )}
            </div>
            <span className="body-small muted mono truncate" title={summary.path}>
              {summary.path}
            </span>
          </>
        )}
        <Notice icon="shield">読み取り専用（mode=ro）で開き、件数だけを持ち帰ります。会話や記憶の中身は読みません。</Notice>
      </Card>

      <Card title="テーブルごとの行数" icon="layers" source="ssh" className="span-8">
        {summary?.tables ? <TableRows tables={summary.tables} /> : <WavyProgress label="集計しています" />}
      </Card>

      <Card title="リマインダー / 文書キュー" icon="alarm" source="ssh" className="span-4">
        <StatusCounts title="リマインダー" counts={summary?.reminders_by_status} />
        <StatusCounts title="文書処理キュー" counts={summary?.queue_by_status} />
      </Card>

      <Card title="返信への評価" icon="thumbUp" source="ssh" className="span-4">
        <FeedbackSummary feedback={summary?.feedback} />
      </Card>

      <Card title="ブラックリスト" icon="block" source="ssh" className="span-4">
        {summary?.blacklist ? (
          <div className="stat-row">
            <Stat label="ブロック中" value={`${summary.blacklist.blocked} 人`} />
            <Stat label="違反記録あり" value={`${summary.blacklist.users} 人`} />
          </div>
        ) : (
          <span className="body-medium muted">ng/blacklist.json はまだありません。</span>
        )}
      </Card>

      <Card title="評価の推移（直近7日・日本時間）" icon="overview" source="ssh" className="span-12">
        {summary ? <FeedbackTrend days={summary.feedback_by_day ?? []} /> : <WavyProgress label="集計しています" />}
      </Card>

      {summary && summary.errors.length > 0 && (
        <div className="span-12">
          {summary.errors.map((message) => (
            <InlineError key={message}>{message}</InlineError>
          ))}
        </div>
      )}
    </div>
  );
}

function TableRows({ tables }: { tables: Record<string, number> }) {
  const rows = Object.entries(tables).sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...rows.map(([, count]) => count));
  return (
    <ul className="bar-list">
      {rows.map(([name, count]) => (
        <li key={name}>
          <span className="bar-label">
            <span className="body-medium mono">{name}</span>
            <span className="body-small muted">{TABLE_LABELS[name] ?? ""}</span>
          </span>
          <span className="bar-track">
            <span className="bar-fill" style={{ width: `${(count / max) * 100}%` }} />
          </span>
          <span className="bar-value title-small mono">{formatNumber(count)}</span>
        </li>
      ))}
    </ul>
  );
}

function StatusCounts({ title, counts }: { title: string; counts: Record<string, number> | undefined }) {
  const entries = Object.entries(counts ?? {});
  return (
    <div className="status-counts">
      <span className="label-large muted">{title}</span>
      {entries.length ? (
        <div className="chips">
          {entries.map(([status, count]) => (
            <Chip key={status}>
              {STATUS_LABELS[status] ?? status} <strong className="mono">{count}</strong>
            </Chip>
          ))}
        </div>
      ) : (
        <span className="body-medium muted">なし</span>
      )}
    </div>
  );
}

function FeedbackSummary({ feedback }: { feedback: { good: number; bad: number } | undefined }) {
  if (!feedback) return <WavyProgress label="集計しています" />;
  const total = feedback.good + feedback.bad;
  const ratio = total ? feedback.good / total : null;
  return (
    <>
      <div className="metric">
        <span className="display-small">{formatPercent(ratio)}</span>
        <span className="body-medium muted">が 👍（{total} 件中）</span>
      </div>
      <div className="split-bar" role="img" aria-label={`👍 ${feedback.good} 件、👎 ${feedback.bad} 件`}>
        <span className="split-good" style={{ flexGrow: feedback.good || 0.0001 }} />
        <span className="split-bad" style={{ flexGrow: feedback.bad || 0.0001 }} />
      </div>
      <div className="legend body-medium">
        <span>
          <i className="swatch swatch-good" /> 👍 {formatNumber(feedback.good)}
        </span>
        <span>
          <i className="swatch swatch-bad" /> 👎 {formatNumber(feedback.bad)}
        </span>
      </div>
    </>
  );
}

/** 直近7日（今日を含む）の日付を、評価の無い日も 0 件として並べる。 */
function lastSevenDays(days: { day: string; good: number; bad: number }[]) {
  const byDay = new Map(days.map((item) => [item.day, item]));
  return Array.from({ length: 7 }, (_, index) => {
    const date = new Date();
    date.setDate(date.getDate() - (6 - index));
    const key = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
    const found = byDay.get(key);
    return { key, label: `${date.getMonth() + 1}/${date.getDate()}`, good: found?.good ?? 0, bad: found?.bad ?? 0 };
  });
}

function FeedbackTrend({ days }: { days: { day: string; good: number; bad: number }[] }) {
  const series = lastSevenDays(days);
  const max = Math.max(1, ...series.map((day) => day.good + day.bad));
  const total = series.reduce((sum, day) => sum + day.good + day.bad, 0);
  return (
    <div className="trend">
      <div className="legend body-medium">
        <span>
          <i className="swatch swatch-good" /> 👍
        </span>
        <span>
          <i className="swatch swatch-bad" /> 👎
        </span>
        <span className="muted">直近7日で {total} 件</span>
      </div>
      <div className="trend-bars" role="img" aria-label={`直近7日の評価の推移（合計 ${total} 件）`}>
        {series.map((day) => (
          <div key={day.key} className="trend-col" tabIndex={0}>
            <div className="trend-stack" style={{ height: `${((day.good + day.bad) / max) * 100}%` }}>
              {day.bad > 0 && <span className="trend-bad" style={{ flexGrow: day.bad }} />}
              {day.good > 0 && <span className="trend-good" style={{ flexGrow: day.good }} />}
            </div>
            <span className="trend-label label-medium">{day.label}</span>
            <span className="m3-tooltip trend-tip">
              <strong>{day.label}</strong>
              👍 {day.good} / 👎 {day.bad}
            </span>
          </div>
        ))}
      </div>
      {total === 0 && <span className="body-small muted">直近7日の評価はありません。</span>}
    </div>
  );
}
