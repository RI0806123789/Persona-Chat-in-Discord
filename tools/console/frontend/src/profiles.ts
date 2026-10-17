import type { Profile } from "./types";

/**
 * 見本の値（YOUR_...）のまま書き換えられていない項目。
 * この項目を返さない古いバックエンド（起動したまま画面だけ新しくなった場合など）では、空として扱う。
 */
export function unsetFieldsOf(profile: Profile | null | undefined): string[] {
  return profile?.unset_fields ?? [];
}
