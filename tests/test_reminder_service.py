"""一時DBを使い、並行登録でもユーザー別の上限を維持することを検証する。"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from constants import REMINDER_MAX_PENDING_PER_USER
from database import Database
import reminder_service
from reminder_service import ReminderService


class ReminderRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "bot.db"
        self.db = Database(self.path)
        self.db.initialize()
        self.addCleanup(self.db.close)
        self.service = ReminderService(None, self.db, None)
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        clock = patch.object(reminder_service, "_utc_now", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def add_record(self, *, db=None, user_id="11", guild_id=1, recurrence="none"):
        return (db or self.db).reminder_add(
            user_id=user_id, user_name="架空の利用者", guild_id=guild_id,
            channel_id=10, message="架空の予定",
            remind_at=(self.now + timedelta(days=1)).isoformat(),
            created_at=self.now.isoformat(), recurrence=recurrence,
            recurrence_anchor=(self.now + timedelta(days=1)).isoformat() if recurrence != "none" else "",
        )

    async def register(self, *, guild_id=1, recurrence="none", user_id=11):
        message = SimpleNamespace(
            author=SimpleNamespace(id=user_id, display_name="架空の利用者"),
            guild=SimpleNamespace(id=guild_id) if guild_id is not None else None,
            channel=SimpleNamespace(id=10),
        )
        return await self.service.create_from_request(message, {
            "at": (self.now + timedelta(days=1)).isoformat(),
            "message": "架空の予定", "repeat": recurrence,
        })

    async def check_last_slot(self, *, mixed: bool) -> None:
        for _ in range(REMINDER_MAX_PENDING_PER_USER - 1):
            self.add_record()
        original_add = self.db.reminder_add
        barrier = threading.Barrier(2)
        inserted_ids = []

        def synchronized_add(**kwargs):
            # 実際の登録入口に両スレッドが到達してから、最後の1枠を取り合う。
            barrier.wait(timeout=5)
            reminder_id = original_add(**kwargs)
            inserted_ids.append(reminder_id)
            return reminder_id

        with patch.object(self.db, "reminder_add", side_effect=synchronized_add) as add:
            results = await asyncio.wait_for(
                asyncio.gather(
                    self.register(),
                    self.register(guild_id=None if mixed else 1, recurrence="weekly" if mixed else "none"),
                ),
                timeout=10,
            )
        self.assertEqual(add.call_count, 2)
        self.assertFalse(barrier.broken)
        self.assertEqual(len(inserted_ids), 2)
        self.assertEqual(sum(reminder_id is not None for reminder_id in inserted_ids), 1)
        self.assertEqual(sum("お知らせします" in (result or "") for result in results), 1)
        self.assertEqual(sum("1人あたり" in (result or "") for result in results), 1)
        self.assertEqual(self.db.reminder_count_pending("11"), REMINDER_MAX_PENDING_PER_USER)

    async def test_last_slot_accepts_only_one_concurrent_request(self) -> None:
        await self.check_last_slot(mixed=False)

    async def test_guild_dm_and_recurring_requests_share_the_limit(self) -> None:
        await self.check_last_slot(mixed=True)

    async def test_many_requests_never_exceed_limit(self) -> None:
        kinds = ("none", "daily", "weekly", "monthly")
        results = await asyncio.gather(*(
            self.register(guild_id=None if i % 2 else 1, recurrence=kinds[i % len(kinds)])
            for i in range(REMINDER_MAX_PENDING_PER_USER + 12)
        ))
        self.assertEqual(sum("お知らせします" in (result or "") for result in results),
                         REMINDER_MAX_PENDING_PER_USER)
        self.assertEqual(sum("1人あたり" in (result or "") for result in results), 12)
        self.assertEqual(self.db.reminder_count_pending("11"), REMINDER_MAX_PENDING_PER_USER)

    async def test_other_user_has_independent_slots(self) -> None:
        for _ in range(REMINDER_MAX_PENDING_PER_USER):
            self.add_record()
        self.assertIn("1人あたり", await self.register())
        self.assertIn("お知らせします", await self.register(user_id=12))
        self.assertEqual(self.db.reminder_count_pending("11"), REMINDER_MAX_PENDING_PER_USER)
        self.assertEqual(self.db.reminder_count_pending("12"), 1)

    async def test_cancelled_and_completed_reminders_do_not_use_slots(self) -> None:
        for _ in range(REMINDER_MAX_PENDING_PER_USER):
            self.add_record()
        record = self.db.reminder_list_pending("11")[0]
        self.db.reminder_cancel(record["reminder_id"], "11")
        self.assertIn("お知らせします", await self.register())
        self.db.reminder_claim_due((self.now + timedelta(days=2)).isoformat(), lambda record: None)
        self.assertEqual(self.db.reminder_count_pending("11"), 0)
        self.assertIn("お知らせします", await self.register(recurrence="daily"))

    async def test_database_error_does_not_return_success(self) -> None:
        with self.db._lock, self.db._conn:
            self.db._conn.execute(
                "CREATE TRIGGER fail_test_reminder BEFORE INSERT ON reminders "
                "BEGIN SELECT RAISE(ABORT, 'fictional write failure'); END"
            )
        result = await self.register()
        self.assertIn("登録に失敗", result)
        self.assertNotIn("お知らせします", result)
        self.assertEqual(self.db.reminder_count_pending("11"), 0)

    async def test_database_insert_at_limit_returns_no_id(self) -> None:
        for _ in range(REMINDER_MAX_PENDING_PER_USER):
            self.add_record()
        self.assertIsNone(self.add_record())
        self.assertEqual(self.db.reminder_count_pending("11"), REMINDER_MAX_PENDING_PER_USER)

    async def test_separate_connections_cannot_claim_the_same_slot(self) -> None:
        for _ in range(REMINDER_MAX_PENDING_PER_USER - 1):
            self.add_record()
        other_db = Database(self.path)
        other_db.initialize()
        self.addCleanup(other_db.close)
        barrier = threading.Barrier(2)

        def add(db):
            barrier.wait(timeout=5)
            return self.add_record(db=db)

        results = await asyncio.wait_for(
            asyncio.gather(asyncio.to_thread(add, self.db), asyncio.to_thread(add, other_db)),
            timeout=10,
        )
        self.assertFalse(barrier.broken)
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.db.reminder_count_pending("11"), REMINDER_MAX_PENDING_PER_USER)


if __name__ == "__main__":
    unittest.main()
