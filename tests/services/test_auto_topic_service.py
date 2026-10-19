"""自動投稿の権限確認・通知制限・再試行条件を外部通信なしで確認する。"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import discord
from persona_bot.services import auto_topic_service
from persona_bot.core.app_state import BotState
from persona_bot.core.background import spawn
from persona_bot.services.auto_topic_service import AutoTopicService
from tests.helpers.thread_probe import make_thread_probe


class AutoTopicServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.state = BotState()
        self.settings = self.state.get_guild(1)
        self.settings.auto_topic_enabled = True
        self.settings.auto_topic_interval_hours = 1
        self.settings.auto_topic_channels[10] = datetime.now(timezone.utc) - timedelta(hours=2)
        self.permissions = SimpleNamespace(view_channel=True, send_messages=True, send_messages_in_threads=True)
        self.channel = SimpleNamespace(
            id=10, name="架空のチャンネル", guild=SimpleNamespace(me=object()), send=AsyncMock(),
            permissions_for=Mock(return_value=self.permissions),
        )
        self.client = SimpleNamespace(user=None, get_channel=Mock(return_value=self.channel))
        self.gemini = SimpleNamespace(generate=AsyncMock(return_value=("@everyone @here <@&22> <@33>", None, [])))
        self.moderator = SimpleNamespace(check=AsyncMock(return_value=(True, "")))
        self.ng_filter = SimpleNamespace(mask_with_log=Mock(side_effect=lambda text, **kwargs: text))
        self.service = AutoTopicService(
            self.client, self.state, None, self.gemini, self.moderator, self.ng_filter,
            SimpleNamespace(log_snapshot=Mock()), None,
        )
        self.member = SimpleNamespace(id=11, display_name="架空の利用者", mention="<@11>")
        self.prompt = patch("persona_bot.config.paths.load_prompt_template", return_value="架空のペルソナ").start()
        self.addCleanup(patch.stopall)
        self.history = patch.object(
            auto_topic_service, "fetch_channel_history_text", new=AsyncMock(return_value="架空の履歴"),
        ).start()

    async def test_response_settings_are_captured_before_prompt_or_history_wait(self):
        for kind in ("topic", "anniversary"):
            for stage in ("prompt", "history"):
                with self.subTest(kind=kind, stage=stage):
                    self.settings.current_prompt_file = "../prompts/prompt_default.txt"
                    self.settings.current_model_name = "old-model"

                    def change():
                        self.settings.current_prompt_file = "../prompts/prompt_maid.txt"
                        self.settings.current_model_name = "new-model"

                    def prompt(path):
                        if stage == "prompt":
                            change()
                        return "架空のペルソナ"

                    async def history(*args):
                        if stage == "history":
                            change()
                        return "架空の履歴"

                    self.prompt.side_effect = prompt
                    self.history.side_effect = history
                    async def post():
                        if kind == "topic":
                            return await self.service._post_topic(self.channel, self.settings)
                        return await self.service._post_anniversary(self.channel, self.settings, self.member, "誕生日")

                    self.assertTrue(await post())
                    self.assertEqual(self.prompt.call_args.args[0].name, "prompt_default.txt")
                    self.assertEqual(self.gemini.generate.await_args.args[1], "old-model")
                    self.assertTrue(await post())
                    self.assertEqual(self.prompt.call_args.args[0].name, "prompt_maid.txt")
                    self.assertEqual(self.gemini.generate.await_args.args[1], "new-model")

    async def test_whitespace_or_tag_only_auto_posts_are_skipped_before_screening(self):
        for text in (" \n\t", "  \n[V:+1, A:+1]  "):
            with self.subTest(text=text):
                self.gemini.generate.return_value = (text, None, [])
                self.assertFalse(await self.service._post_topic(self.channel, self.settings))
                self.assertFalse(await self.service._post_anniversary(self.channel, self.settings, self.member, "誕生日"))
        self.moderator.check.assert_not_awaited()
        self.channel.send.assert_not_awaited()

    async def test_dm_is_not_watched_or_updated(self):
        self.service.watch_channel(20, None)
        self.assertEqual(self.state.dm_settings.auto_topic_channels, {})
        old = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.state.dm_settings.auto_topic_channels[20] = old
        self.service.mark_activity(20, None)
        self.assertEqual(self.state.dm_settings.auto_topic_channels, {20: old})
        self.service.watch_channel(30, 1)
        self.settings.auto_topic_channels[30] = old
        self.service.mark_activity(30, 1)
        self.assertGreater(self.settings.auto_topic_channels[30], old)

    async def test_topic_and_anniversary_prompt_loading_keeps_loop_responsive(self):
        for kind in ("topic", "anniversary"):
            with self.subTest(kind=kind):
                operation, threads = make_thread_probe("架空のペルソナ")
                self.prompt.side_effect = operation
                if kind == "topic":
                    result = await self.service._post_topic(self.channel, self.settings)
                else:
                    result = await self.service._post_anniversary(self.channel, self.settings, self.member, "誕生日")
                self.assertTrue(result)
                self.assertTrue(threads)
                self.assertNotIn(threading.get_ident(), threads)

    async def test_denied_permissions_or_missing_member_skip_generation_on_every_check(self):
        for condition in ("view_channel", "send_messages", "member", "dm"):
            with self.subTest(condition=condition):
                self.permissions.view_channel = condition != "view_channel"
                self.permissions.send_messages = condition != "send_messages"
                self.channel.guild = None if condition == "dm" else SimpleNamespace(
                    me=None if condition == "member" else object(),
                )
                await self.service.run_pending_checks()
                await self.service.run_pending_checks()
                self.assertFalse(await self.service._post_anniversary(self.channel, self.settings, self.member, "誕生日"))
        self.history.assert_not_awaited()
        self.gemini.generate.assert_not_awaited()
        self.moderator.check.assert_not_awaited()
        self.channel.send.assert_not_awaited()

    async def test_thread_uses_thread_send_permission(self):
        thread = Mock(spec=discord.Thread, id=10, guild=self.channel.guild, send=AsyncMock())
        thread.permissions_for.return_value = self.permissions
        self.permissions.send_messages = False
        self.assertTrue(self.service._can_send(thread))
        self.permissions.send_messages_in_threads = False
        self.assertFalse(await self.service._post_topic(thread, self.settings))
        self.gemini.generate.assert_not_awaited()

    async def test_permission_restoration_retries_then_silence_blocks_duplicate(self):
        self.permissions.send_messages = False
        await self.service.run_pending_checks()
        self.assertNotIn(10, self.settings.auto_topic_posted_at)
        self.permissions.send_messages = True
        await self.service.run_pending_checks()
        await self.service.run_pending_checks()
        self.gemini.generate.assert_awaited_once()
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.channel.send.call_args.kwargs["allowed_mentions"].to_dict(), {"parse": []})

    async def test_forbidden_after_generation_prevents_repeated_api_calls(self):
        self.channel.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="禁止"), "架空の権限変更")
        await self.service.run_pending_checks()
        await self.service.run_pending_checks()
        self.assertIn(10, self.settings.auto_topic_posted_at)
        self.gemini.generate.assert_awaited_once()
        self.channel.send.assert_awaited_once()

    async def test_temporary_send_failure_still_retries(self):
        self.channel.send.side_effect = [discord.HTTPException(SimpleNamespace(status=503, reason="一時障害"), "架空の障害"), None]
        await self.service.run_pending_checks()
        self.assertNotIn(10, self.settings.auto_topic_posted_at)
        await self.service.run_pending_checks()
        self.assertEqual(self.gemini.generate.await_count, 2)
        self.assertIn(10, self.settings.auto_topic_posted_at)

    async def test_anniversary_only_notifies_target_member(self):
        self.assertTrue(await self.service._post_anniversary(self.channel, self.settings, self.member, "誕生日"))
        self.assertTrue(self.channel.send.call_args.args[0].startswith("<@11> "))
        mentions = self.channel.send.call_args.kwargs["allowed_mentions"]
        self.assertEqual(mentions.to_dict(), {"users": [11], "parse": []})

    async def test_unsafe_generation_is_not_sent_or_retried(self):
        self.moderator.check.return_value = (False, "架空の判定")
        await self.service.run_pending_checks()
        await self.service.run_pending_checks()
        self.gemini.generate.assert_awaited_once()
        self.channel.send.assert_not_awaited()

    async def test_active_autonomous_session_skips_generation(self):
        self.settings.autonomous_session = object()
        await self.service.run_pending_checks()
        self.gemini.generate.assert_not_awaited()

    async def test_topic_rechecks_conditions_after_each_wait_without_recording_cancellation(self):
        for stage in ("prompt", "history", "generation", "moderation"):
            for change in ("disabled", "activity", "session", "interval", "unwatched"):
                with self.subTest(stage=stage, change=change):
                    self.settings.auto_topic_enabled = True
                    self.settings.auto_topic_interval_hours = 1
                    self.settings.autonomous_session = None
                    self.settings.auto_topic_channels[10] = datetime.now(timezone.utc) - timedelta(hours=2)
                    self.settings.auto_topic_posted_at.clear()
                    self.gemini.generate.reset_mock()
                    self.channel.send.reset_mock()
                    entered, release = asyncio.Event(), asyncio.Event()
                    result = {
                        "prompt": "架空のペルソナ", "history": "架空の履歴",
                        "generation": ("架空の話題", None, []), "moderation": (False, "架空の判定"),
                    }[stage]

                    async def pause(*args, **kwargs):
                        entered.set()
                        await release.wait()
                        return result

                    target, name = {
                        "prompt": (auto_topic_service, "load_persona_template"),
                        "history": (auto_topic_service, "fetch_channel_history_text"),
                        "generation": (self.gemini, "generate"),
                        "moderation": (self.moderator, "check"),
                    }[stage]
                    with patch.object(target, name, new=AsyncMock(side_effect=pause)) as waiting:
                        task = spawn(self.service.run_pending_checks(), label="話題生成中の条件変更の確認")
                        try:
                            await asyncio.wait_for(entered.wait(), 2)
                            if change == "disabled":
                                self.settings.auto_topic_enabled = False
                            elif change == "activity":
                                self.service.mark_activity(10, 1)
                            elif change == "session":
                                self.settings.autonomous_session = object()
                            elif change == "interval":
                                self.settings.auto_topic_interval_hours = 3
                            else:
                                self.settings.auto_topic_channels.pop(10)
                            release.set()
                            await asyncio.wait_for(task, 2)
                        finally:
                            release.set()
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                        waiting.assert_awaited_once()
                    self.channel.send.assert_not_awaited()
                    self.assertEqual(self.settings.auto_topic_posted_at, {})
                    if stage in ("prompt", "history"):
                        self.gemini.generate.assert_not_awaited()

                    # 中止を処理済みにせず、条件が戻った次の巡回で投稿できる。
                    self.settings.auto_topic_enabled = True
                    self.settings.auto_topic_interval_hours = 1
                    self.settings.autonomous_session = None
                    self.settings.auto_topic_channels[10] = datetime.now(timezone.utc) - timedelta(hours=2)
                    await self.service.run_pending_checks()
                    self.channel.send.assert_awaited_once()

    async def test_later_channels_use_current_settings_and_activity(self):
        for change in ("disabled", "session", "activity"):
            with self.subTest(change=change):
                self.settings.auto_topic_enabled = True
                self.settings.autonomous_session = None
                old = datetime.now(timezone.utc) - timedelta(hours=2)
                self.settings.auto_topic_channels = {10: old, 20: old}
                self.settings.auto_topic_posted_at.clear()
                self.channel.send.reset_mock()

                async def generate(*args, **kwargs):
                    if change == "disabled":
                        self.settings.auto_topic_enabled = False
                    elif change == "session":
                        self.settings.autonomous_session = object()
                    else:
                        self.service.mark_activity(20, 1)
                    return "架空の話題", None, []

                with patch.object(self.gemini, "generate", new=AsyncMock(side_effect=generate)) as generation:
                    await self.service.run_pending_checks()
                    generation.assert_awaited_once()
                self.assertEqual(self.channel.send.await_count, 1 if change == "activity" else 0)
                self.assertNotIn(20, self.settings.auto_topic_posted_at)


class AnniversaryChannelTests(unittest.IsolatedAsyncioTestCase):
    """記念日の投稿先の選び直しと、送信拒否後の再生成抑止を確認する（#56）。"""

    TODAY = "2026-10-16"

    def setUp(self):
        self.state = BotState()
        self.settings = self.state.get_guild(1)
        self.settings.auto_topic_enabled = True
        now = datetime.now(timezone.utc)
        # 10 が最新、20、30 の順に古い監視チャンネル
        for hours, channel_id in enumerate((10, 20, 30), start=1):
            self.settings.auto_topic_channels[channel_id] = now - timedelta(hours=hours)
            self.settings.auto_topic_posted_at[channel_id] = now - timedelta(days=1)
        self.channels = {channel_id: self.make_channel(channel_id) for channel_id in (10, 20, 30)}
        self.member = SimpleNamespace(id=11, display_name="架空の利用者", mention="<@11>")
        guild = SimpleNamespace(id=1, get_member=Mock(return_value=self.member))
        self.client = SimpleNamespace(
            user=None,
            get_channel=Mock(side_effect=self.channels.get),
            get_guild=Mock(return_value=guild),
        )
        self.gemini = SimpleNamespace(generate=AsyncMock(return_value=("架空のお祝い", None, [])))
        self.service = AutoTopicService(
            self.client, self.state, None, self.gemini,
            SimpleNamespace(check=AsyncMock(return_value=(True, ""))),
            SimpleNamespace(mask_with_log=Mock(side_effect=lambda text, **kwargs: text)),
            SimpleNamespace(log_snapshot=Mock()), None,
        )
        patch("persona_bot.config.paths.load_prompt_template", return_value="架空のペルソナ").start()
        patch.object(
            auto_topic_service, "fetch_channel_history_text", new=AsyncMock(return_value="架空の履歴"),
        ).start()
        patch("builtins.print").start()
        self.addCleanup(patch.stopall)

    @staticmethod
    def make_channel(channel_id: int) -> SimpleNamespace:
        permissions = SimpleNamespace(view_channel=True, send_messages=True, send_messages_in_threads=True)
        return SimpleNamespace(
            id=channel_id, name=f"架空のチャンネル{channel_id}", guild=SimpleNamespace(me=object()),
            send=AsyncMock(), permissions_for=Mock(return_value=permissions), permissions=permissions,
        )

    async def celebrate(self) -> None:
        await self.service._celebrate_user("11", "誕生日", self.TODAY)

    def assert_recorded(self, recorded: bool) -> None:
        self.assertEqual(self.settings.anniversary_posted_dates.get("11:誕生日") == self.TODAY, recorded)

    async def test_deleted_newest_channel_is_unwatched_and_next_channel_is_used(self):
        del self.channels[10]
        await self.celebrate()
        self.assertNotIn(10, self.settings.auto_topic_channels)
        self.assertNotIn(10, self.settings.auto_topic_posted_at)
        self.channels[20].send.assert_awaited_once()
        self.channels[30].send.assert_not_awaited()
        self.gemini.generate.assert_awaited_once()
        self.assert_recorded(True)

    async def test_disabled_auto_topic_keeps_watched_channels_but_posts_no_anniversary(self):
        self.settings.auto_topic_enabled = False
        await self.celebrate()
        self.client.get_guild.assert_not_called()
        self.gemini.generate.assert_not_awaited()
        for channel in self.channels.values():
            channel.send.assert_not_awaited()
        self.assertEqual(set(self.settings.auto_topic_channels), {10, 20, 30})
        self.assert_recorded(False)

    async def test_disabling_anniversary_during_wait_does_not_record_or_generate_later_users(self):
        for stage in ("member", "history", "generation", "moderation"):
            with self.subTest(stage=stage):
                self.settings.auto_topic_enabled = True
                self.settings.anniversary_posted_dates.clear()
                for channel in self.channels.values():
                    channel.send.reset_mock()
                entered, release = asyncio.Event(), asyncio.Event()

                async def pause(*args, **kwargs):
                    entered.set()
                    await release.wait()
                    return {
                        "member": self.member, "history": "架空の履歴",
                        "generation": ("架空のお祝い", None, []), "moderation": (False, "架空の判定"),
                    }[stage]

                target, name = {
                    "member": (self.service, "_resolve_member"),
                    "history": (auto_topic_service, "fetch_channel_history_text"),
                    "generation": (self.gemini, "generate"),
                    "moderation": (self.service._moderator, "check"),
                }[stage]
                with patch.object(target, name, new=AsyncMock(side_effect=pause)) as waiting:
                    task = spawn(self.celebrate(), label="お祝い生成中の無効化の確認")
                    try:
                        await asyncio.wait_for(entered.wait(), 2)
                        self.settings.auto_topic_enabled = False
                        release.set()
                        await asyncio.wait_for(task, 2)
                        await self.service._celebrate_user("22", "誕生日", self.TODAY)
                    finally:
                        release.set()
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    waiting.assert_awaited_once()
                for channel in self.channels.values():
                    channel.send.assert_not_awaited()
                self.assert_recorded(False)
                self.settings.auto_topic_enabled = True
                await self.celebrate()
                self.channels[10].send.assert_awaited_once()
                self.assert_recorded(True)

    async def test_unsendable_channels_are_skipped_without_api_calls(self):
        self.channels[10].permissions.send_messages = False
        self.channels[20].permissions.view_channel = False
        await self.celebrate()
        self.channels[30].send.assert_awaited_once()
        self.gemini.generate.assert_awaited_once()
        # 削除されたわけではないため、監視は続ける
        self.assertEqual(set(self.settings.auto_topic_channels), {10, 20, 30})
        self.assert_recorded(True)

    async def test_no_sendable_channel_retries_after_permission_is_restored(self):
        for channel in self.channels.values():
            channel.permissions.send_messages = False
        await self.celebrate()
        await self.celebrate()
        self.gemini.generate.assert_not_awaited()
        self.assert_recorded(False)
        self.channels[20].permissions.send_messages = True
        await self.celebrate()
        await self.celebrate()
        self.gemini.generate.assert_awaited_once()
        self.channels[20].send.assert_awaited_once()
        self.assert_recorded(True)

    async def test_forbidden_after_generation_stops_hourly_regeneration(self):
        self.channels[10].send.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason="禁止"), "架空の権限変更",
        )
        for _ in range(3):
            await self.celebrate()
        self.gemini.generate.assert_awaited_once()
        self.channels[10].send.assert_awaited_once()
        # 生成文は送信を拒否されたチャンネルの履歴から作ったため、他へ流用しない
        self.channels[20].send.assert_not_awaited()
        self.assert_recorded(True)

    async def test_temporary_generation_failure_retries_on_next_check(self):
        self.gemini.generate.side_effect = [(None, None, []), ("架空のお祝い", None, [])]
        await self.celebrate()
        self.assert_recorded(False)
        await self.celebrate()
        self.assertEqual(self.gemini.generate.await_count, 2)
        self.channels[10].send.assert_awaited_once()
        self.assert_recorded(True)
