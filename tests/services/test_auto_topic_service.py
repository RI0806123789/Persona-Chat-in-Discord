"""自動投稿の権限確認・通知制限・再試行条件を外部通信なしで確認する。"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord
from persona_bot.services import auto_topic_service
from persona_bot.core.app_state import BotState
from persona_bot.services.auto_topic_service import AutoTopicService


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
        self.prompt = patch.object(auto_topic_service, "load_prompt_template", return_value="架空のペルソナ").start()
        self.addCleanup(patch.stopall)
        self.history = patch.object(
            auto_topic_service, "fetch_channel_history_text", new=AsyncMock(return_value="架空の履歴"),
        ).start()

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
