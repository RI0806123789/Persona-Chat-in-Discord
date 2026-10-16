"""会話統計のユーザー単位保存・並行更新・プライバシー操作を検証する。"""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.core.background import spawn
from persona_bot.config.constants import STATS_RECENT_SAMPLES_MAX
from persona_bot.core.database import Database
from persona_bot.services.stats_service import StatsService


class UserStatsPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # OSのタイムゾーンDBやtzdataの有無に依存させず、日本時間の集計を検証する。
        timezone_patch = patch("persona_bot.services.stats_service.resolve_timezone", return_value=timezone(timedelta(hours=9)))
        timezone_patch.start()
        self.addCleanup(timezone_patch.stop)
        directory = tempfile.TemporaryDirectory(prefix="stats-test-")
        self.addCleanup(directory.cleanup)
        self.db = Database(Path(directory.name) / "bot.db")
        self.db.initialize()
        self.addCleanup(self.db.close)
        self.when = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.generated_analysis = {
            "topics": [{"label": "架空の話題", "weight": 1}],
            "big_five": {"openness": 50}, "personality": "架空の解析結果",
        }
        self.gemini = SimpleNamespace(generate=AsyncMock(
            return_value=(json.dumps(self.generated_analysis), None, [])
        ))
        self.seed(2)

    def seed(self, count: int) -> None:
        entries = {}
        for i in range(count):
            entry = StatsService._new_entry(f"架空の利用者{i}")
            entry.update(total_count=1, recent_samples=["架空の会話"], opt_out=False)
            entry["hourly"][9] = 1
            entries[f"user-{i}"] = entry
        if count > 1:
            entries["user-1"]["opt_out"] = True
        self.db.stats_save_all(entries)
        self.service = StatsService(self.db, self.gemini)

    async def measured_write(self, operation):
        statements = []
        before = self.db._conn.total_changes
        self.db._conn.set_trace_callback(statements.append)
        try:
            result = await operation()
        finally:
            self.db._conn.set_trace_callback(None)
        writes = [sql for sql in statements
                  if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE"))]
        self.assertEqual(len(writes), 1)
        self.assertFalse(any(sql.lstrip().upper().startswith("DELETE") for sql in writes))
        self.assertEqual(self.db._conn.total_changes - before, 1)
        return result

    async def test_write_count_is_constant_for_one_hundred_and_thousand_users(self) -> None:
        for count in (1, 100, 1000):
            with self.subTest(user_count=count):
                self.seed(count)
                before = self.db.stats_load_all()
                result = await self.measured_write(lambda: self.service.record(
                    "user-0", "変更した架空の名前", "追加の架空発言", self.when
                ))
                self.assertTrue(result)
                after = self.db.stats_load_all()
                self.assertEqual({k: v for k, v in after.items() if k != "user-0"},
                                 {k: v for k, v in before.items() if k != "user-0"})

    async def test_record_round_trip_preserves_all_fields(self) -> None:
        original = deepcopy(self.service.cache["user-0"])
        self.assertTrue(await self.service.record("user-0", "新しい架空の名前", "追加の発言", self.when))
        record = self.db.stats_load_all()["user-0"]
        self.assertEqual(record["total_count"], 2)
        self.assertEqual(record["hourly"][9], 2)
        self.assertEqual(record["recent_samples"], ["架空の会話", "追加の発言"])
        self.assertEqual(record["first_seen"], original["first_seen"])
        self.assertEqual(record["user_name"], "新しい架空の名前")
        self.assertFalse(record["opt_out"])
        self.assertEqual(record, StatsService(self.db, self.gemini).cache["user-0"])

    async def test_missing_timezone_data_records_in_utc(self) -> None:
        from persona_bot.utils.timeutil import resolve_timezone

        with patch("persona_bot.services.stats_service.resolve_timezone", side_effect=resolve_timezone):
            with patch("persona_bot.utils.timeutil.ZoneInfo", side_effect=ZoneInfoNotFoundError("架空の欠落")):
                service = StatsService(self.db, self.gemini)
        self.assertTrue(await service.record("fallback-user", "架空", "UTCで集計", self.when))
        record = self.db.stats_load_all()["fallback-user"]
        self.assertEqual(record["hourly"], [1] + [0] * 23)
        self.assertEqual(service._tz, timezone.utc)

    async def test_analysis_setting_and_clear_each_save_only_the_target_user(self) -> None:
        other = deepcopy(self.db.stats_load_all()["user-1"])
        analysis = await self.measured_write(lambda: self.service.ensure_analysis("user-0", force=True))
        self.assertEqual(analysis["topics"], self.generated_analysis["topics"])
        self.assertEqual(self.db.stats_load_all()["user-0"]["analysis"], analysis)
        self.assertTrue(await self.measured_write(lambda: self.service.set_opt_out("user-0", "架空", True)))
        self.assertTrue(await self.measured_write(lambda: self.service.clear_user_data("user-0")))
        result = self.db.stats_load_all()
        self.assertEqual(result["user-1"], other)
        self.assertTrue(result["user-0"]["opt_out"])
        self.assertEqual(result["user-0"]["total_count"], 0)
        self.assertEqual(result["user-0"]["hourly"], [0] * 24)
        self.assertEqual(result["user-0"]["recent_samples"], [])
        self.assertIsNone(result["user-0"]["analysis"])

    async def test_opted_out_user_is_not_recorded_or_analysed(self) -> None:
        before = self.db.stats_load_all()["user-1"]
        await self.service.record("user-1", "架空", "記録しない発言", self.when)
        self.assertIsNone(await self.service.ensure_analysis("user-1", force=True))
        self.gemini.generate.assert_not_awaited()
        self.assertEqual(self.db.stats_load_all()["user-1"], before)

    async def test_concurrent_records_keep_counts_and_recent_samples(self) -> None:
        count = STATS_RECENT_SAMPLES_MAX + 10
        results = await asyncio.gather(*(
            self.service.record("user-0", "架空", f"発言-{i}", self.when) for i in range(count)
        ))
        self.assertTrue(all(results))
        entry = self.db.stats_load_all()["user-0"]
        self.assertEqual(entry["total_count"], count + 1)
        self.assertEqual(entry["hourly"][9], count + 1)
        self.assertEqual(entry["recent_samples"], [f"発言-{i}" for i in range(count - STATS_RECENT_SAMPLES_MAX, count)])
        self.assertEqual(entry, self.service.cache["user-0"])

    async def test_concurrent_users_do_not_overwrite_each_other(self) -> None:
        results = await asyncio.gather(*(
            self.service.record(f"new-{i % 4}", "架空", f"発言-{i}", self.when) for i in range(40)
        ))
        self.assertTrue(all(results))
        data = self.db.stats_load_all()
        for i in range(4):
            self.assertEqual(data[f"new-{i}"]["total_count"], 10)

    async def test_database_failures_are_reported_for_record_and_privacy_operations(self) -> None:
        other = deepcopy(self.db.stats_load_all()["user-1"])
        with self.db._lock, self.db._conn:
            self.db._conn.execute(
                "CREATE TRIGGER fail_test_stats BEFORE INSERT ON user_stats "
                "BEGIN SELECT RAISE(ABORT, 'fictional write failure'); END"
            )
        self.assertFalse(await self.service.record("user-0", "架空", "追加", self.when))
        self.assertFalse(await self.service.set_opt_out("user-0", "架空", True))
        self.assertFalse(await self.service.clear_user_data("user-0"))
        self.assertEqual(self.db.stats_load_all()["user-1"], other)

    async def check_analysis_overlap(self, action: str) -> None:
        started, resume = asyncio.Event(), asyncio.Event()

        async def generate(*args, **kwargs):
            started.set()
            await resume.wait()
            return json.dumps(self.generated_analysis), None, []

        self.gemini.generate.side_effect = generate
        task = spawn(self.service.ensure_analysis("user-0", force=True), label="統計解析の回帰テスト")
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            if action == "clear":
                await self.service.clear_user_data("user-0")
            elif action == "opt_out":
                await self.service.set_opt_out("user-0", "架空", True)
            else:
                await self.service.record("user-0", "架空", "解析中の追加発言", self.when)
            resume.set()
            await asyncio.wait_for(task, timeout=5)
        finally:
            resume.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        entry = self.db.stats_load_all()["user-0"]
        if action in ("clear", "opt_out"):
            self.assertIsNone(entry["analysis"])
            if action == "clear":
                self.assertEqual(entry["recent_samples"], [])
                self.assertEqual(entry["total_count"], 0)
            else:
                self.assertTrue(entry["opt_out"])
        else:
            self.assertEqual(entry["total_count"], 2)
            self.assertIn("解析中の追加発言", entry["recent_samples"])
            self.assertEqual(entry["analysis"]["topics"], self.generated_analysis["topics"])

    async def test_analysis_does_not_restore_cleared_data(self) -> None:
        await self.check_analysis_overlap("clear")

    async def test_analysis_does_not_persist_after_opt_out(self) -> None:
        await self.check_analysis_overlap("opt_out")

    async def test_analysis_keeps_records_added_while_waiting(self) -> None:
        await self.check_analysis_overlap("record")

    async def test_failed_analysis_does_not_return_cache_cleared_while_waiting(self) -> None:
        for fail_with_exception in (False, True):
            with self.subTest(exception=fail_with_exception):
                self.seed(2)
                self.service.cache["user-0"]["analysis"] = deepcopy(self.generated_analysis)
                await self.service.save_background("user-0")

                async def generate(*args, **kwargs):
                    await self.service.clear_user_data("user-0")
                    if fail_with_exception:
                        raise RuntimeError("架空の解析エラー")
                    return "JSONではない架空の応答", None, []

                self.gemini.generate.side_effect = generate
                self.assertIsNone(await self.service.ensure_analysis("user-0", force=True))
                self.assertIsNone(self.db.stats_load_all()["user-0"]["analysis"])


if __name__ == "__main__":
    unittest.main()
