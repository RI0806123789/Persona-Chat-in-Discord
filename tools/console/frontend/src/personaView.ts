// サーバー画面のペルソナ表示（backend/remote_scripts/insights.py の describe_persona の出力を文にする）。
// ボットは見つからない保存値を消さずに既定で応答するため、実効のペルソナと保存値を分けて見せる。
import type { GuildSettingsView } from "./types";

export const PERSONA_ESTIMATE_NOTE =
  "ペルソナは、サーバー上の prompts/ のファイル構成からボットと同じ規則で推定しています。" +
  "ボットは起動時とキャラ設定パネルを開いたときに一覧を読み直すため、その後に追加・削除したファイルはまだ反映されていないことがあります。";

export function describePersona(settings: GuildSettingsView): { value: string; notes: string[] } {
  const name = settings.persona ?? "—";
  // 既定に従っている（未選択・保存値の欠損）ことを値の横に示す。組み込みの標準は名前で分かる
  const followsDefault = settings.persona_source !== "saved" && settings.persona !== null && !settings.persona_builtin;
  const notes: string[] = [];
  if (settings.persona_missing) {
    const fallback = settings.persona_builtin ? "組み込みの標準プロンプト" : "既定のペルソナ";
    notes.push(
      `保存されたペルソナ「${settings.persona_missing}」が prompts/ に見つからないため、${fallback}で応答しています` +
        "（設定は残っており、ファイルを戻すと元のペルソナに戻ります）。",
    );
  } else if (settings.persona_builtin) {
    notes.push("prompts/ に使えるペルソナのファイルが無いため、組み込みの標準プロンプトで応答しています。");
  }
  return { value: followsDefault ? `${name}（既定）` : name, notes };
}
