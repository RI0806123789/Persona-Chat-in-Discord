import { Button, EmptyState } from "../components/ui";
import { unsetFieldsOf } from "../profiles";
import type { Profile } from "../types";

/** SSH 未接続のときに、SSH が必要なページの代わりに出す案内。 */
export function NotConnected({ profile, connecting, onConnect }: { profile: Profile | null; connecting: boolean; onConnect: () => void }) {
  return (
    <section className="m3-card m3-card-filled not-connected">
      <EmptyState icon="plug" title="SSH に接続していません">
        <p className="body-medium">
          {unsetFieldsOf(profile).length > 0 && profile ? (
            <>
              <strong>{profile.label}</strong> は見本の値のままです。backend/profiles.json（無ければ profiles.example.json をコピーして作る）の YOUR_ で始まる値（{unsetFieldsOf(profile).join("・")}）を書き換えてから接続してください。
            </>
          ) : profile ? (
            <>
              <strong>{profile.label}</strong> に接続すると、サーバーから実データを取得します。
            </>
          ) : (
            "接続先（backend/profiles.json）を確認してください。"
          )}
        </p>
        <Button variant="filled" size="m" icon="plug" onClick={onConnect} disabled={!profile} busy={connecting}>
          {connecting ? "接続しています…" : "接続する"}
        </Button>
      </EmptyState>
    </section>
  );
}
