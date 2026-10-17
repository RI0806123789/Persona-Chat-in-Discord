import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorMessage } from "../api";
import { LoadingIndicator, WavyProgress } from "../components/expressive";
import { Button, Card, Chip, ConnectedButtonGroup, InlineError, Notice, Stat, StatusChip } from "../components/ui";
import { formatBytes, formatNumber, formatPercent } from "../format";
import { lastSevenDays } from "../data";
import { usePrivacy } from "../privacy";
import type { DbSummary, Profile } from "../types";
import { NotConnected } from "./shared";

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
  completed: "完了",
  failed: "失敗",
  canceled: "取り消し",
};

export function DataPage({
  connected,
  connecting,
  onConnect,
  selectedProfile,
  refreshToken,
  connectionKey,
}: {
  connected: boolean;
  connecting: boolean;
  onConnect: () => void;
  selectedProfile: Profile | null;
  refreshToken: number;
  connectionKey: string;
}) {
  const [summary, setSummary] = useState<DbSummary | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const privacy = usePrivacy();
  const requestId = useRef(0);

  const load = useCallback(async () => {
    const current = ++requestId.current;
    setLoading(true);
    try {
      const data = await api.db();
      if (current !== requestId.current) return;
      setSummary(data);
      setError(null);
    } catch (caught) {
      if (current !== requestId.current) return;
      setError(errorMessage(caught));
    } finally {
      if (current === requestId.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    setSummary(null);
    setError(null);
    if (connected) void load();
    return () => { requestId.current += 1; };
  }, [connected, load, refreshToken, connectionKey]);

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
        {summary && !summary.exists && <InlineError>{privacy.mask(summary.path)} が見つかりません。</InlineError>}
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
              <SchemaChip deployed={summary.schema_version} latest={summary.latest_schema_version} />
            </div>
            <span className="body-small muted mono truncate" title={privacy.mask(summary.path)}>
              {privacy.mask(summary.path)}
            </span>
          </>
        )}
        <Notice icon="shield">読み取り専用（mode=ro）で開き、件数だけを持ち帰ります。会話や記憶の中身は読みません。</Notice>
      </Card>

      <Card title="テーブルごとの行数" icon="layers" source="ssh" className="span-8">
        {summary?.tables ? (
          <TableRows tables={summary.tables} />
        ) : summary ? (
          <span className="body-medium muted">取得できません</span>
        ) : (
          <WavyProgress label="集計しています" />
        )}
      </Card>

      <Card title="リマインダー / 文書キュー" icon="alarm" source="ssh" className="span-4">
        <StatusCounts title="リマインダー" counts={summary ? summary.reminders_by_status ?? null : undefined} />
        <StatusCounts title="文書処理キュー" counts={summary ? summary.queue_by_status ?? null : undefined} />
      </Card>

      <Card title="返信への評価" icon="thumbUp" source="ssh" className="span-4">
        <FeedbackSummary feedback={summary ? summary.feedback ?? null : undefined} />
      </Card>

      <Card title="ブラックリスト" icon="block" source="ssh" className="span-4">
        {summary?.blacklist ? (
          <div className="stat-row">
            <Stat label="ブロック中" value={`${summary.blacklist.blocked} 人`} />
            <Stat label="違反記録あり" value={`${summary.blacklist.users} 人`} />
          </div>
        ) : (
          <span className="body-medium muted">違反記録は未作成、または取得できません。</span>
        )}
      </Card>

      <Card title="評価の推移（直近7日・日本時間）" icon="overview" source="ssh" className="span-12">
        {!summary ? (
          <WavyProgress label="集計しています" />
        ) : summary.feedback_by_day ? (
          <FeedbackTrend days={summary.feedback_by_day} />
        ) : (
          <span className="body-medium muted">日別の評価を取得できません</span>
        )}
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

/** 本番のスキーマの版を、手元のリポジトリ（src/persona_bot/core/database.py）の版と比べる。 */
function SchemaChip({ deployed, latest }: { deployed: number | undefined; latest: number | null | undefined }) {
  if (latest === null || latest === undefined) return <StatusChip tone="neutral" label="手元の版を読めません" />;
  if (deployed === latest) return <StatusChip tone="good" icon="checkCircle" label="手元と同じ" />;
  if (deployed !== undefined && deployed > latest) return <StatusChip tone="warning" icon="alert" label={`手元（v${latest}）より新しい`} />;
  return <StatusChip tone="warning" icon="alert" label={`手元は v${latest}（未デプロイ）`} />;
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

function StatusCounts({ title, counts }: { title: string; counts: Record<string, number> | null | undefined }) {
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
        <span className="body-medium muted">{counts === null ? "取得できません" : counts === undefined ? "集計しています" : "なし"}</span>
      )}
    </div>
  );
}

function FeedbackSummary({ feedback }: { feedback: { good: number; bad: number } | null | undefined }) {
  if (feedback === null) return <span className="body-medium muted">取得できません</span>;
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

type TrendView = "chart" | "table";

function FeedbackTrend({ days }: { days: { day: string; good: number; bad: number }[] }) {
  const [view, setView] = useState<TrendView>("chart");
  const series = lastSevenDays(days);
  const max = Math.max(1, ...series.map((day) => day.good + day.bad));
  const total = series.reduce((sum, day) => sum + day.good + day.bad, 0);
  return (
    <div className="trend">
      <div className="trend-head">
        <div className="legend body-medium">
          <span>
            <i className="swatch swatch-good" /> 👍
          </span>
          <span>
            <i className="swatch swatch-bad" /> 👎
          </span>
          <span className="muted">直近7日で {total} 件</span>
        </div>
        <ConnectedButtonGroup
          label="表示のしかた"
          value={view}
          onChange={setView}
          options={[
            { value: "chart", label: "グラフ", icon: "overview" },
            { value: "table", label: "表", icon: "layers" },
          ]}
        />
      </div>
      {view === "table" ? (
        <div className="table-wrap">
          <table className="m3-table is-compact">
            <caption className="visually-hidden">直近7日の評価（日本時間）</caption>
            <thead>
              <tr>
                <th>日付</th>
                <th className="num">👍</th>
                <th className="num">👎</th>
                <th className="num">計</th>
                <th className="num">満足度</th>
              </tr>
            </thead>
            <tbody>
              {series.map((day) => {
                const sum = day.good + day.bad;
                return (
                  <tr key={day.key}>
                    <td className="mono">{day.label}</td>
                    <td className="num mono">{day.good}</td>
                    <td className="num mono">{day.bad}</td>
                    <td className="num mono">{sum}</td>
                    <td className="num mono">{sum ? formatPercent(day.good / sum) : "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
      <div className="trend-bars" role="group" aria-label={`直近7日の評価の推移（合計 ${total} 件）`}>
        {series.map((day) => (
          <div key={day.key} className="trend-col" tabIndex={0} aria-label={`${day.label}: 👍 ${day.good} 件、👎 ${day.bad} 件`}>
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
      )}
      {total === 0 && <span className="body-small muted">直近7日の評価はありません。</span>}
    </div>
  );
}
