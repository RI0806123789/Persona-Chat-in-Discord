"""タイムゾーンの解決を1か所に集めたヘルパー。

会話統計・記念日・リマインダーはいずれも「日本時間で何時か / 今日はいつか」を
基準に動くため、それぞれが ZoneInfo を組み立てていた。ZoneInfo はタイムゾーン
データベースが無い環境（tzdata を含まないスリムな Linux イメージなど）で
例外を投げるため、各所で try/except を書く必要があり重複していた。

解決に失敗した場合は UTC へフォールバックする（集計時刻や表示がずれるだけで、
ボット自体は動かせるようにするフェイルオープン設計）。
"""

from datetime import timezone, tzinfo
from zoneinfo import ZoneInfo


def resolve_timezone(name: str, *, purpose: str = "") -> tzinfo:
    """タイムゾーン名を解決する。読み込めない環境では UTC を返す。

    Args:
        name: "Asia/Tokyo" のような IANA タイムゾーン名。
        purpose: 失敗時のログに出す用途名（例: "記念日の判定"）。原因を追いやすくする。
    """
    try:
        return ZoneInfo(name)
    except Exception as error:
        label = f"（{purpose}）" if purpose else ""
        print(f"タイムゾーン({name})の読み込みに失敗しました{label}。UTC で代用します: {error}")
        return timezone.utc


def timezone_label(tz: tzinfo) -> str:
    """解決済みタイムゾーンの表示名を返す（"Asia/Tokyo" / UTC 代用時は "UTC"）。

    **画面表示やプロンプトに載せる基準タイムゾーン名は、定数
    （REMINDER_TIMEZONE 等）ではなく必ずこの関数で作ること。**
    resolve_timezone は tzdata の無い環境で UTC へフォールバックするため、
    定数をそのまま表示すると「実際は UTC の時刻なのに Asia/Tokyo と名乗る」
    という食い違いが起きる。リマインダーでは、この食い違いがそのまま
    9時間ずれた時刻での登録につながる。
    """
    return str(tz)
