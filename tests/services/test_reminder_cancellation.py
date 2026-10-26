"""一時DBと待機イベントで、通知と本人の取消の順序を検証する。"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.core.database import Database
from persona_bot.services import reminder_service
from persona_bot.services.reminder_service import ReminderService


class ReminderCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Database(Path(directory.name) / "bot.db")
        with patch("builtins.print"):
            self.db.initialize()
        self.addCleanup(self.db.close)
        self.now = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
        clock = patch.object(reminder_service, "_utc_now", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.channel = SimpleNamespace(guild=None, send=AsyncMock())
        self.client = Mock(get_channel=Mock(return_value=self.channel))
        self.service = ReminderService(self.client, self.db, Mock(
            mask_with_log=Mock(side_effect=lambda text, **kwargs: text),
        ))

    def add_record(self, recurrence="daily", user_id="11"):
        scheduled = (self.now - timedelta(seconds=1)).isoformat()
        return self.db.reminder_add(
            user_id=user_id, user_name="架空の利用者", guild_id=1, channel_id=10,
            message="架空の予定", remind_at=scheduled, created_at=scheduled,
            recurrence=recurrence, recurrence_anchor=scheduled if recurrence != "none" else "",
        )

    def start_check(self, release):
        task = asyncio.create_task(self.service.run_due_checks())

        async def finish():
            release.set()
            await task

        self.addAsyncCleanup(finish)
        return task

    def start_cancel(self, reminder_id, release):
        task = asyncio.create_task(self.service.cancel(reminder_id, "11"))

        async def finish():
            release.set()
            await asyncio.gather(task, return_exceptions=True)

        self.addAsyncCleanup(finish)
        return task

    async def test_cancel_during_channel_fetch_suppresses_each_recurring_kind(self):
        for kind in ("daily", "weekly", "monthly"):
            with self.subTest(kind=kind):
                reminder_id = self.add_record(kind)
                entered, release = asyncio.Event(), asyncio.Event()

                async def fetch(channel_id):
                    entered.set()
                    await release.wait()
                    return self.channel

                self.client.get_channel.return_value = None
                self.client.fetch_channel = AsyncMock(side_effect=fetch)
                task = self.start_check(release)
                await asyncio.wait_for(entered.wait(), 3)
                self.assertIsNotNone(await self.service.cancel(reminder_id, "11"))
                self.assertEqual(await self.service.list_pending("11"), [])
                self.channel.send.assert_not_awaited()
                release.set()
                await task
                self.channel.send.assert_not_awaited()

    async def test_cancel_during_thread_reopening_suppresses_notification_and_preview(self):
        reminder_id = self.add_record()
        entered, release = asyncio.Event(), asyncio.Event()
        channel = Mock(spec=discord.Thread, archived=True, locked=False, send=AsyncMock())
        channel.guild = SimpleNamespace(me=object())
        channel.permissions_for.return_value = SimpleNamespace(
            view_channel=True, send_messages_in_threads=True,
        )

        async def reopen(**kwargs):
            entered.set()
            await release.wait()
            return channel

        channel.edit = AsyncMock(side_effect=reopen)
        self.client.get_channel.return_value = channel
        task = self.start_check(release)
        await asyncio.wait_for(entered.wait(), 3)
        self.assertIsNotNone(await self.service.cancel(reminder_id, "11"))
        release.set()
        await task
        channel.send.assert_not_awaited()
        channel.parent.send.assert_not_called()

    async def test_cancel_queued_notification_while_another_reminder_is_sending(self):
        first = self.add_record()
        second = self.add_record("weekly")
        entered, release = asyncio.Event(), asyncio.Event()

        async def send(*args, **kwargs):
            entered.set()
            await release.wait()

        self.channel.send.side_effect = send
        task = self.start_check(release)
        await asyncio.wait_for(entered.wait(), 3)
        self.assertIsNotNone(await asyncio.wait_for(self.service.cancel(second, "11"), 3))
        self.assertTrue(self.db.reminder_is_pending(first))
        release.set()
        await task
        self.channel.send.assert_awaited_once()
        self.assertFalse(self.db.reminder_is_pending(second))

    async def test_cancel_after_send_started_waits_for_this_notification_and_stops_next(self):
        reminder_id = self.add_record()
        entered, release = asyncio.Event(), asyncio.Event()

        async def send(*args, **kwargs):
            entered.set()
            await release.wait()

        self.channel.send.side_effect = send
        task = self.start_check(release)
        await asyncio.wait_for(entered.wait(), 3)
        cancel = self.start_cancel(reminder_id, release)
        await asyncio.sleep(0)
        self.assertFalse(cancel.done())
        # 通信待機中もDBの共有同期ロックは解放されている。
        self.assertTrue(await asyncio.wait_for(
            asyncio.to_thread(self.db.reminder_is_pending, reminder_id), 3,
        ))
        release.set()
        await task
        self.assertIsNotNone(await cancel)
        self.assertEqual(await self.service.list_pending("11"), [])
        with patch.object(reminder_service, "_utc_now", return_value=self.now + timedelta(days=2)):
            await self.service.run_due_checks()
        self.channel.send.assert_awaited_once()
        self.assertEqual(dict(self.service._delivery_locks), {})

    async def test_send_failure_releases_lock_before_a_waiting_cancel_finishes(self):
        reminder_id = self.add_record()
        entered, release = asyncio.Event(), asyncio.Event()

        async def send(*args, **kwargs):
            entered.set()
            await release.wait()
            raise OSError("架空の送信失敗")

        self.channel.send.side_effect = send
        task = self.start_check(release)
        await asyncio.wait_for(entered.wait(), 3)
        cancel = self.start_cancel(reminder_id, release)
        await asyncio.sleep(0)
        self.assertFalse(cancel.done())
        release.set()
        await task
        self.assertIsNotNone(await cancel)
        self.assertFalse(self.db.reminder_is_pending(reminder_id))
        self.assertEqual(dict(self.service._delivery_locks), {})

    async def test_canceled_cancel_waiter_keeps_the_senders_lock_for_the_next_waiter(self):
        reminder_id = self.add_record()
        entered, release = asyncio.Event(), asyncio.Event()

        async def send(*args, **kwargs):
            entered.set()
            await release.wait()

        self.channel.send.side_effect = send
        task = self.start_check(release)
        await asyncio.wait_for(entered.wait(), 3)
        abandoned = self.start_cancel(reminder_id, release)
        await asyncio.sleep(0)
        abandoned.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await abandoned
        cancel = self.start_cancel(reminder_id, release)
        await asyncio.sleep(0)
        self.assertFalse(cancel.done())
        self.assertTrue(self.db.reminder_is_pending(reminder_id))
        release.set()
        await task
        self.assertIsNotNone(await cancel)
        self.assertEqual(dict(self.service._delivery_locks), {})

    async def test_other_user_cannot_suppress_notification(self):
        reminder_id = self.add_record()
        self.assertIsNone(await self.service.cancel(reminder_id, "12"))
        await self.service.run_due_checks()
        self.channel.send.assert_awaited_once()
        self.assertIn("次回は", self.channel.send.await_args.args[0])

    async def test_normal_single_and_recurring_notifications_keep_claim_and_mentions(self):
        for kind in ("none", "daily", "weekly", "monthly"):
            with self.subTest(kind=kind):
                reminder_id = self.add_record(kind)
                self.channel.send.reset_mock()
                await asyncio.gather(self.service.run_due_checks(), self.service.run_due_checks())
                self.channel.send.assert_awaited_once()
                text = self.channel.send.await_args.args[0]
                self.assertEqual("次回は" in text, kind != "none")
                mentions = self.channel.send.await_args.kwargs["allowed_mentions"].to_dict()
                self.assertEqual(mentions["users"], [11])
                self.assertNotIn("everyone", mentions["parse"])
                if kind != "none":
                    self.assertIsNotNone(await self.service.cancel(reminder_id, "11"))
                else:
                    self.assertIsNone(await self.service.cancel(reminder_id, "11"))

    async def test_failed_final_state_read_does_not_send_or_cancel_the_series(self):
        reminder_id = self.add_record()
        with patch.object(self.db, "reminder_is_pending", side_effect=OSError("架空の読込失敗")):
            await self.service.run_due_checks()
        self.channel.send.assert_not_awaited()
        self.assertTrue(self.db.reminder_is_pending(reminder_id))
        self.assertEqual(dict(self.service._delivery_locks), {})


if __name__ == "__main__":
    unittest.main()
