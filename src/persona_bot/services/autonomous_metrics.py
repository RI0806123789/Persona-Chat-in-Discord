"""Discord に依存しない、自律行動のゲージ・活動窓・時間帯の計算。"""

from datetime import datetime, timedelta, timezone, tzinfo
from typing import Protocol, Sequence

from persona_bot.config.constants import (
    AUTONOMOUS_ACTIVITY_BONUS_MAX,
    AUTONOMOUS_ACTIVITY_BONUS_PER_MESSAGE,
    AUTONOMOUS_ACTIVITY_SAMPLES_MAX,
    AUTONOMOUS_ACTIVITY_WINDOW_MINUTES,
    AUTONOMOUS_QUIET_HOURS_END,
    AUTONOMOUS_QUIET_HOURS_START,
    AUTONOMOUS_START_RETRY_COOLDOWN_MINUTES,
    AUTONOMOUS_START_RETRY_MAX_MULTIPLIER,
    AUTONOMOUS_TIMEZONE,
)
from persona_bot.utils.timeutil import as_utc, resolve_timezone, timezone_label


# 解決失敗時の警告を繰り返さず、判定とパネル表示で同じ基準を使う。
QUIET_HOURS_TZ = resolve_timezone(AUTONOMOUS_TIMEZONE, purpose="自律行動の時間帯制限")


class TalkDesireState(Protocol):
    """ゲージ計算が参照する値だけ。Discord や設定の保存処理には依存しない。"""

    @property
    def auto_topic_interval_hours(self) -> float: ...

    @property
    def autonomous_last_activity(self) -> datetime | None: ...

    @property
    def autonomous_recent_activity(self) -> Sequence[datetime]: ...


def compute_talk_desire(guild_settings: TalkDesireState, *, now: datetime | None = None) -> float:
    """無会話時間と直近の活動量から、0〜100 の「話したいゲージ」を返す。

    基礎値は無会話時間に比例し、`auto_topic_interval_hours` で 100 に届く。
    直近の活動数を上限付きで加点するが、未観測のサーバーでは 0 のままとする。
    引数を変更しないため、配信判定とパネル表示のどちらからも呼べる。
    """
    now = as_utc(now if now is not None else datetime.now(timezone.utc))
    last_activity = guild_settings.autonomous_last_activity
    if last_activity is None:
        return 0.0

    full_minutes = max(1.0, guild_settings.auto_topic_interval_hours * 60.0)
    silence_minutes = max(0.0, (now - as_utc(last_activity)).total_seconds() / 60.0)
    base = 100.0 * silence_minutes / full_minutes
    window_start = now - timedelta(minutes=AUTONOMOUS_ACTIVITY_WINDOW_MINUTES)
    recent_count = sum(1 for at in guild_settings.autonomous_recent_activity if as_utc(at) >= window_start)
    bonus = min(AUTONOMOUS_ACTIVITY_BONUS_MAX, recent_count * AUTONOMOUS_ACTIVITY_BONUS_PER_MESSAGE)
    return max(0.0, min(100.0, base + bonus))


def updated_activity_samples(recent_activity: Sequence[datetime], *, now: datetime) -> list[datetime]:
    """時間窓を過ぎた活動を減衰させ、新しい1件を追加する。元の列は変更しない。"""
    now = as_utc(now)
    window_start = now - timedelta(minutes=AUTONOMOUS_ACTIVITY_WINDOW_MINUTES)
    samples = [as_utc(at) for at in recent_activity if as_utc(at) >= window_start]
    samples.append(now)
    return samples[-AUTONOMOUS_ACTIVITY_SAMPLES_MAX:]


def start_retry_wait_minutes(failures: int) -> float:
    """連続開始失敗の回数から、上限付きの再試行待ち時間を返す。"""
    multiplier = min(failures, AUTONOMOUS_START_RETRY_MAX_MULTIPLIER)
    return AUTONOMOUS_START_RETRY_COOLDOWN_MINUTES * multiplier


def is_quiet_hours(
    *, now: datetime | None = None, tz: tzinfo = QUIET_HOURS_TZ,
    start: int = AUTONOMOUS_QUIET_HOURS_START, end: int = AUTONOMOUS_QUIET_HOURS_END,
) -> bool:
    """開始時刻を含み終了時刻を含まない深夜制限。日またぎと無効指定にも対応する。"""
    if start == end:
        return False
    now = as_utc(now if now is not None else datetime.now(timezone.utc))
    hour = now.astimezone(tz).hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def describe_quiet_hours(
    *, tz: tzinfo = QUIET_HOURS_TZ,
    start: int = AUTONOMOUS_QUIET_HOURS_START, end: int = AUTONOMOUS_QUIET_HOURS_END,
) -> str:
    """深夜制限の表示文。UTC 代用時も判定に使う実際のタイムゾーン名を表示する。"""
    if start == end:
        return ""
    end_hour = (end - 1) % 24
    return f"{start:02d}:00〜{end_hour:02d}:59（{timezone_label(tz)}）"
