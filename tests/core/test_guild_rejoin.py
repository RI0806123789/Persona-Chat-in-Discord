"""退出後の再参加でも、保存済み設定と記念日のお祝い記録を引き継ぐ（#254）。"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from persona_bot.config.choices import AUTO_TOPIC_INTERVAL_CHOICES, MODEL_CHOICES
from persona_bot.core import commands
from persona_bot.core.app_state import BotState, GuildSettings
from persona_bot.core.database import Database
from persona_bot.core.guild_lifecycle import remove_guild_state
from persona_bot.services.auto_topic_service import AutoTopicService
from persona_bot.services.autonomous_service import AutonomousService
from tests.helpers.persona_helpers import install_persona_catalog


class GuildRejoinTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Database(Path(directory.name) / "bot.db")
        self.addCleanup(self.db.close)
        with patch("builtins.print"):
            self.db.initialize()
        install_persona_catalog(self, "../prompts/架空のペルソナ.txt")
        self.state = BotState()
        self.state.restore_from_database(self.db)
        self.stop = SimpleNamespace(stop_session=AsyncMock())
        self.guard = SimpleNamespace(release=Mock())

    async def save_settings(self, guild_id: int | None) -> GuildSettings:
        settings = await self.state.get_guild_async(guild_id)
        settings.current_prompt_file = "../prompts/架空のペルソナ.txt"
        settings.current_model_name = MODEL_CHOICES[-1].value
        settings.current_respond_mode = "all"
        settings.grounding_enabled = False
        settings.auto_topic_enabled = True
        settings.auto_topic_interval_hours = AUTO_TOPIC_INTERVAL_CHOICES[-1].value
        settings.autonomous_enabled = True
        await settings.persist()
        await settings.record_anniversary_post("11", "誕生日", "2026-01-01")
        return settings

    async def remove(self, guild_id: int) -> None:
        await remove_guild_state(guild_id, self.state, self.stop, self.guard)

    async def test_rejoin_restores_every_saved_setting_before_one_item_is_changed(self) -> None:
        removed = await self.save_settings(1)
        other = await self.save_settings(2)
        dm = await self.save_settings(None)
        before = self.db.guild_settings_load_all()
        posted_before = self.db.anniversary_posts_load_all()
        removed.channel_reset_points[10] = 20
        removed.auto_topic_channels[10] = datetime.now(timezone.utc)
        removed.auto_topic_posted_at[10] = datetime.now(timezone.utc)
        removed.autonomous_session = object()
        removed.autonomous_last_activity = datetime.now(timezone.utc)
        removed.autonomous_recent_activity.append(datetime.now(timezone.utc))
        removed.autonomous_start_failures = 3
        removed.autonomous_start_retry_at = datetime.now(timezone.utc)
        self.state.welcomed_guild_ids.add(1)

        await self.remove(1)
        restored = await self.state.get_guild_async(1)

        self.assertIsNot(restored, removed)
        self.assertEqual(restored.current_prompt_file, removed.current_prompt_file)
        self.assertEqual(restored.current_model_name, removed.current_model_name)
        self.assertEqual(restored.current_respond_mode, "all")
        self.assertFalse(restored.grounding_enabled)
        self.assertTrue(restored.auto_topic_enabled)
        self.assertEqual(restored.auto_topic_interval_hours, removed.auto_topic_interval_hours)
        self.assertTrue(restored.autonomous_enabled)
        self.assertEqual(restored.anniversary_posted_dates, posted_before[1])
        self.assertEqual(restored.channel_reset_points, {})
        self.assertEqual(restored.auto_topic_channels, {})
        self.assertEqual(restored.auto_topic_posted_at, {})
        self.assertIsNone(restored.autonomous_session)
        self.assertIsNone(restored.autonomous_last_activity)
        self.assertEqual(restored.autonomous_recent_activity, [])
        self.assertEqual(restored.autonomous_start_failures, 0)
        self.assertIsNone(restored.autonomous_start_retry_at)
        self.assertNotIn(1, self.state.welcomed_guild_ids)
        self.assertIs(self.state.get_guild(2), other)
        self.assertIs(self.state.get_guild(None), dm)

        restored.grounding_enabled = True
        await restored.persist()
        self.assertEqual(self.db.guild_settings_load_all(), {
            **before, 1: {**before[1], "grounding_enabled": True},
        })
        self.assertEqual(self.db.anniversary_posts_load_all(), posted_before)

    async def test_restored_anniversary_prevents_same_day_repost(self) -> None:
        await self.save_settings(1)
        await self.remove(1)
        restored = await self.state.get_guild_async(1)
        # 監視対象は再構築される一時状態なので、再参加後の会話による登録を再現する。
        restored.auto_topic_channels[10] = datetime.now(timezone.utc)
        client = SimpleNamespace(get_guild=Mock(return_value=object()))
        service = AutoTopicService(client, self.state, None, Mock(), Mock(), Mock(), Mock(), self.db)
        with patch.object(service, "_resolve_member", new=AsyncMock(return_value=object())) as member, \
                patch.object(service, "_pick_anniversary_channel", return_value=object()), \
                patch.object(service, "_post_anniversary", new=AsyncMock(return_value=True)) as post:
            await service._celebrate_user("11", "誕生日", "2026-01-01")
            client.get_guild.assert_not_called()
            member.assert_not_awaited()
            post.assert_not_awaited()
            await service._celebrate_user("11", "誕生日", "2027-01-01")
            post.assert_awaited_once()
        self.assertEqual(self.db.anniversary_posts_load_all()[1], {"11:誕生日": "2027-01-01"})

    async def test_anniversary_only_guild_is_restored_and_new_guild_uses_defaults(self) -> None:
        self.db.anniversary_post_record(1, "11", "誕生日", "2026-01-01")
        self.db.anniversary_post_record(2, "22", "記念日", "2026-01-02")
        restored = await self.state.get_guild_async(1)
        self.assertEqual(restored.anniversary_posted_dates, {"11:誕生日": "2026-01-01"})
        self.assertEqual(restored.current_model_name, GuildSettings().current_model_name)
        self.assertEqual(await self.state.get_guild_async(3), GuildSettings(guild_id=3))
        self.assertNotIn(2, self.state.guild_settings)
        self.assertEqual(self.db.guild_settings_load_all(), {})

    async def test_existing_guild_and_dm_access_do_not_read_database_again(self) -> None:
        existing = await self.save_settings(1)
        dm = self.state.get_guild(None)
        with patch.object(self.db, "guild_state_load", side_effect=AssertionError("再読込不要")):
            self.assertIs(self.state.get_guild(1), existing)
            self.assertIs(self.state.get_guild(None), dm)

    async def test_each_read_failure_leaves_no_default_cache_and_next_access_retries(self) -> None:
        await self.save_settings(1)
        before = self.db.guild_settings_load_all()
        posted_before = self.db.anniversary_posts_load_all()
        for denied_table in ("guild_settings", "anniversary_posts"):
            with self.subTest(denied_table=denied_table):
                await self.remove(1)

                def authorize(action, table, column, database_name, trigger):
                    if action == sqlite3.SQLITE_READ and table == denied_table:
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK

                self.db._conn.set_authorizer(authorize)
                try:
                    with self.assertRaises(sqlite3.DatabaseError):
                        await self.state.get_guild_async(1)
                    self.assertNotIn(1, self.state.guild_settings)
                finally:
                    self.db._conn.set_authorizer(None)
                restored = await self.state.get_guild_async(1)
                self.assertEqual(restored.current_respond_mode, "all")
                self.assertEqual(restored.anniversary_posted_dates, posted_before[1])
                self.assertEqual(self.db.guild_settings_load_all(), before)
                self.assertEqual(self.db.anniversary_posts_load_all(), posted_before)

    async def test_startup_bulk_restore_does_not_repeat_individual_reads(self) -> None:
        early = await self.state.get_guild_async(1)
        await self.save_settings(1)
        self.db.anniversary_post_record(2, "22", "記念日", "2026-01-02")
        with patch.object(self.db, "guild_state_load", side_effect=AssertionError("一括取得済み")):
            self.state.restore_from_database(self.db)
        self.assertIs(self.state.get_guild(1), early)
        self.assertEqual(self.state.get_guild(2).anniversary_posted_dates, {"22:記念日": "2026-01-02"})

    def block_first_load(self):
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        original = self.db.guild_state_load
        threads, calls = [], []

        def load(guild_id):
            threads.append(threading.get_ident())
            calls.append(guild_id)
            if len(calls) == 1:
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(5):
                    raise AssertionError("サーバー設定の読込待機が解除されませんでした。")
            return original(guild_id)

        patcher = patch.object(self.db, "guild_state_load", side_effect=load)
        patcher.start()
        self.addCleanup(patcher.stop)
        tasks = []

        def start():
            task = asyncio.create_task(self.state.get_guild_async(1))
            tasks.append(task)
            return task

        async def finish():
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)

        self.addAsyncCleanup(finish)
        return start, entered, release, threads, calls

    async def test_initial_load_keeps_loop_running_and_concurrent_requests_share_one_setting(self) -> None:
        other = await self.state.get_guild_async(2)
        start, entered, release, threads, calls = self.block_first_load()
        first = start()
        await asyncio.wait_for(entered.wait(), 2)
        second = start()
        await asyncio.wait_for(asyncio.sleep(0.02), 1)
        self.assertFalse(first.done())
        self.assertFalse(second.done())
        self.assertNotIn(1, self.state.guild_settings)
        self.assertEqual(calls, [1])
        self.assertNotIn(threading.get_ident(), threads)
        self.assertIs(await asyncio.wait_for(self.state.get_guild_async(2), 1), other)
        self.assertIs(await asyncio.wait_for(self.state.get_guild_async(None), 1), self.state.dm_settings)
        release.set()
        first_settings, second_settings = await asyncio.gather(first, second)
        self.assertIs(first_settings, second_settings)
        self.assertIs(self.state.get_guild(1), first_settings)
        self.assertEqual(calls, [1])

    async def test_canceled_initial_load_finishes_worker_without_publishing_defaults(self) -> None:
        await self.save_settings(1)
        await self.remove(1)
        start, entered, release, _threads, calls = self.block_first_load()
        task = start()
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertNotIn(1, self.state.guild_settings)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertNotIn(1, self.state.guild_settings)
        restored = await self.state.get_guild_async(1)
        self.assertEqual(restored.current_respond_mode, "all")
        self.assertEqual(calls, [1, 1])

    async def test_removed_guild_invalidates_old_load_without_overwriting_new_rejoin(self) -> None:
        await self.save_settings(1)
        await self.remove(1)
        start, entered, release, _threads, _calls = self.block_first_load()
        old = start()
        await asyncio.wait_for(entered.wait(), 2)
        waiting = start()
        await asyncio.sleep(0)
        await self.remove(1)
        self.assertNotIn(1, self.state.guild_settings)
        rejoined = await self.state.get_guild_async(1)
        self.assertEqual(rejoined.current_respond_mode, "all")
        release.set()
        for task in (old, waiting):
            with self.assertRaisesRegex(RuntimeError, "サーバー退出"):
                await task
        self.assertIs(self.state.get_guild(1), rejoined)

    async def test_sync_observers_and_session_stop_never_load_missing_guild(self) -> None:
        auto_topic = AutoTopicService(Mock(), self.state, None, Mock(), Mock(), Mock(), Mock(), self.db)
        autonomous = AutonomousService(Mock(), self.state, Mock(), Mock(), Mock(), Mock())
        with patch.object(self.db, "guild_state_load", side_effect=AssertionError("同期観測で読まない")):
            auto_topic.watch_channel(10, 1)
            auto_topic.mark_activity(10, 1)
            autonomous.mark_activity(10, 1)
            self.assertFalse(autonomous.is_comment_channel(10, 1))
            self.assertIsNone(autonomous.claim_comment_slot(10, 1))
            await autonomous.handle_external_move(SimpleNamespace(id=1))
            self.assertFalse(await autonomous.stop_session(1, reason="架空の停止"))
            with self.assertRaisesRegex(RuntimeError, "get_guild_async"):
                self.state.get_guild(1)
        self.assertNotIn(1, self.state.guild_settings)

    async def test_sync_observers_keep_default_creation_without_database(self) -> None:
        state = BotState()
        auto_topic = AutoTopicService(Mock(), state, None, Mock(), Mock(), Mock(), Mock(), None)
        autonomous = AutonomousService(Mock(), state, Mock(), Mock(), Mock(), Mock())
        auto_topic.watch_channel(10, 1)
        self.assertIn(10, state.get_guild(1).auto_topic_channels)
        autonomous.mark_activity(20, 2)
        self.assertIsNotNone(state.get_guild(2).autonomous_last_activity)
        self.assertFalse(autonomous.is_comment_channel(30, 3))
        self.assertIn(3, state.guild_settings)
        self.assertIsNone(autonomous.claim_comment_slot(40, 4))
        self.assertIn(4, state.guild_settings)

    async def test_functions_defers_before_initial_load_and_edits_original_panel(self) -> None:
        callbacks = {}
        tree = SimpleNamespace(command=lambda **kwargs: lambda callback: callbacks.setdefault(kwargs["name"], callback))
        commands.register_commands(
            tree, self.state, None, None, SimpleNamespace(is_blocked=Mock(return_value=False)),
            None, None, None, None, Mock(),
        )
        _start, entered, release, _threads, _calls = self.block_first_load()
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=11), guild_id=1,
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            edit_original_response=AsyncMock(), original_response=AsyncMock(return_value=object()),
        )
        view = SimpleNamespace(build_preview=Mock(return_value="架空の機能パネル"))
        with patch.object(commands, "is_administrator", return_value=False), patch.object(
            commands, "FunctionsView", return_value=view,
        ):
            task = asyncio.create_task(callbacks["functions"](interaction))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                interaction.response.defer.assert_awaited_once_with(ephemeral=True)
                self.assertNotIn(1, self.state.guild_settings)
                self.assertFalse(task.done())
            finally:
                release.set()
                await task
        interaction.response.send_message.assert_not_awaited()
        interaction.edit_original_response.assert_awaited_once_with(content="架空の機能パネル", view=view)
        self.assertIs(view.message, interaction.original_response.return_value)


if __name__ == "__main__":
    unittest.main()
