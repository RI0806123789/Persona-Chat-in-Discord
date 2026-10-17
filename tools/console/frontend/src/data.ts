/** サーバーの評価集計と同じ日本時間で、今日を含む7日を並べる。 */
export function lastSevenDays(days: { day: string; good: number; bad: number }[], now = Date.now()) {
  const byDay = new Map(days.map((item) => [item.day, item]));
  return Array.from({ length: 7 }, (_, index) => {
    const date = new Date(now + 9 * 60 * 60 * 1000);
    date.setUTCDate(date.getUTCDate() - (6 - index));
    const key = `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, "0")}-${String(date.getUTCDate()).padStart(2, "0")}`;
    const found = byDay.get(key);
    return { key, label: `${date.getUTCMonth() + 1}/${date.getUTCDate()}`, good: found?.good ?? 0, bad: found?.bad ?? 0 };
  });
}
