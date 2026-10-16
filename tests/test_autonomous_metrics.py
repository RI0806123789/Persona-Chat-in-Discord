"""自律行動の計算をDiscordオブジェクト・外部通信なしで検証する。"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from autonomous_metrics import (
    compute_talk_desire, describe_quiet_hours, is_quiet_hours,
    start_retry_wait_minutes, updated_activity_samples,
)
from constants import AUTONOMOUS_ACTIVITY_SAMPLES_MAX, AUTONOMOUS_ACTIVITY_WINDOW_MINUTES


class AutonomousMetricsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        self.settings = SimpleNamespace(
            auto_topic_interval_hours=2,
            autonomous_last_activity=self.now - timedelta(hours=1),
            autonomous_recent_activity=[],
        )

    def test_import_needs_no_discord_or_guild_settings(self) -> None:
        result = subprocess.run(
            [sys.executable, "-B", "-c",
             "import sys; sys.modules['discord'] = None; sys.modules['app_state'] = None; "
             "sys.path.insert(0, sys.argv[1]); import autonomous_metrics", str(SRC)],
            capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unobserved_guild_has_no_desire_even_with_samples(self) -> None:
        self.settings.autonomous_last_activity = None
        self.settings.autonomous_recent_activity = [self.now]
        self.assertEqual(compute_talk_desire(self.settings, now=self.now), 0)

    def test_silence_and_recent_activity_are_added_without_mutating_samples(self) -> None:
        samples = [self.now - timedelta(minutes=1), self.now]
        self.settings.autonomous_recent_activity = samples
        self.assertEqual(compute_talk_desire(self.settings, now=self.now), 54)
        self.assertEqual(samples, [self.now - timedelta(minutes=1), self.now])

    def test_activity_decays_just_after_window_boundary(self) -> None:
        boundary = self.now - timedelta(minutes=AUTONOMOUS_ACTIVITY_WINDOW_MINUTES)
        self.settings.autonomous_recent_activity = [boundary - timedelta(microseconds=1), boundary]
        self.assertEqual(compute_talk_desire(self.settings, now=self.now), 52)
        self.assertAlmostEqual(
            compute_talk_desire(self.settings, now=self.now + timedelta(microseconds=1)),
            50, places=6,
        )

    def test_activity_bonus_and_total_desire_are_capped(self) -> None:
        self.settings.autonomous_recent_activity = [self.now] * 100
        self.assertEqual(compute_talk_desire(self.settings, now=self.now), 80)
        self.settings.autonomous_last_activity = self.now - timedelta(hours=3)
        self.assertEqual(compute_talk_desire(self.settings, now=self.now), 100)

    def test_future_activity_and_zero_interval_do_not_produce_negative_or_invalid_values(self) -> None:
        self.settings.autonomous_last_activity = self.now + timedelta(minutes=1)
        self.assertEqual(compute_talk_desire(self.settings, now=self.now), 0)
        self.settings.auto_topic_interval_hours = 0
        self.settings.autonomous_last_activity = self.now - timedelta(minutes=1)
        self.assertEqual(compute_talk_desire(self.settings, now=self.now), 100)

    def test_activity_update_prunes_old_samples_and_keeps_boundary(self) -> None:
        boundary = self.now - timedelta(minutes=AUTONOMOUS_ACTIVITY_WINDOW_MINUTES)
        samples = [boundary - timedelta(microseconds=1), boundary]
        self.assertEqual(updated_activity_samples(samples, now=self.now), [boundary, self.now])
        self.assertEqual(len(samples), 2)

    def test_activity_update_keeps_latest_samples_with_count_limit(self) -> None:
        samples = [self.now - timedelta(seconds=count) for count in range(150, 0, -1)]
        updated = updated_activity_samples(samples, now=self.now)
        self.assertEqual(len(updated), AUTONOMOUS_ACTIVITY_SAMPLES_MAX)
        self.assertEqual(updated, (samples + [self.now])[-AUTONOMOUS_ACTIVITY_SAMPLES_MAX:])

    def test_retry_delay_grows_and_stops_at_existing_limit(self) -> None:
        self.assertEqual(start_retry_wait_minutes(1), 30)
        self.assertEqual(start_retry_wait_minutes(2), 60)
        self.assertEqual(start_retry_wait_minutes(8), 240)
        self.assertEqual(start_retry_wait_minutes(100), 240)

    def test_quiet_hours_include_start_and_exclude_end(self) -> None:
        for hour, expected in ((0, True), (5, True), (6, False), (23, False)):
            with self.subTest(hour=hour):
                self.assertEqual(
                    is_quiet_hours(now=self.now.replace(hour=hour), tz=timezone.utc, start=0, end=6),
                    expected,
                )

    def test_quiet_hours_support_midnight_crossing_and_disable_setting(self) -> None:
        for hour, expected in ((21, False), (22, True), (0, True), (5, True), (6, False)):
            with self.subTest(hour=hour):
                self.assertEqual(
                    is_quiet_hours(now=self.now.replace(hour=hour), tz=timezone.utc, start=22, end=6),
                    expected,
                )
        self.assertFalse(is_quiet_hours(now=self.now, start=6, end=6))
        self.assertEqual(describe_quiet_hours(start=6, end=6), "")

    def test_quiet_hours_judge_in_resolved_timezone_and_describe_utc_fallback(self) -> None:
        tokyo = timezone(timedelta(hours=9))
        at = self.now.replace(hour=15)
        self.assertTrue(is_quiet_hours(now=at, tz=tokyo, start=0, end=6))
        self.assertFalse(is_quiet_hours(now=at, tz=timezone.utc, start=0, end=6))
        self.assertEqual(describe_quiet_hours(tz=timezone.utc, start=0, end=6), "00:00〜05:59（UTC）")
        self.assertEqual(describe_quiet_hours(tz=timezone.utc, start=22, end=6), "22:00〜05:59（UTC）")


if __name__ == "__main__":
    unittest.main()
