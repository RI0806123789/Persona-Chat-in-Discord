"""タイムゾーンの解決を1か所に集めたヘルパー。

会話統計・記念日・リマインダーはいずれも「日本時間で何時か / 今日はいつか」を
基準に動くため、それぞれが ZoneInfo を組み立てていた。ZoneInfo はタイムゾーン
データベースが無い環境（tzdata を含まないスリムな Linux イメージなど）で
例外を投げるため、各所で try/except を書く必要があり重複していた。

解決に失敗した場合は UTC へフォールバックする（集計時刻や表示がずれるだけで、
ボット自体は動かせるようにするフェイルオープン設計）。
"""

from datetime import datetime, timezone, tzinfo
from zoneinfo import ZoneInfo

# 日本語の曜日名。datetime.weekday() は月曜=0・日曜=6 を返すため、この並び順と一致する。
#
# content_moderator.py（リマインダー登録用プロンプト）と reminder_service.py
# （リマインダーの表示）は、format_weekday() を通してこのテーブルを参照する。
# 登録時と表示時の表記を揃えるため、曜日名を変えるときはこのテーブルを直すこと。
WEEKDAY_NAMES_JA = ("月", "火", "水", "木", "金", "土", "日")


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


def format_weekday(dt: datetime) -> str:
    """datetime から日本語の曜日1文字（"月"〜"日"）を返す。

    プロンプトへ埋める現在日時と、ユーザーへ見せるリマインダーの日時表示の両方から
    使うことを想定している（WEEKDAY_NAMES_JA のコメント参照）。

    Args:
        dt: 曜日を取り出す日時。tz 有無は問わない（weekday() はどちらでも使える）。
    """
    return WEEKDAY_NAMES_JA[dt.weekday()]
