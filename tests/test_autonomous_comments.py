"""コメント受付の流量制限を、入力モデレーションより前に適用する経路を検証する。"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import discord
from app_state import AutonomousSession, BotState
import autonomous_service
from autonomous_service import AutonomousService
from background import spawn
from constants import AUTONOMOUS_COMMENT_COOLDOWN_SECONDS, MAX_DOCUMENT_SIZE_BYTES
from discord_helpers import (
    attachment_is_image, clean_message_content, is_supported_document_attachment,
    is_supported_message_channel,
)
from message_handler_helpers import make_message_handler


class AutonomousCommentAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.state = BotState()
        self.settings = self.state.get_guild(1)
        self.settings.current_respond_mode = "mention"
        self.session = self.new_session()
        self.settings.autonomous_session = self.session
        self.client = SimpleNamespace(user=SimpleNamespace(id=99))
        self.message = SimpleNamespace(
            content="架空のコメントです。", attachments=[], mentions=[],
            author=SimpleNamespace(id=11, bot=False, display_name="架空の視聴者"),
            guild=SimpleNamespace(id=1), channel=Mock(spec=discord.VoiceChannel, id=10),
            created_at=self.now, reply=AsyncMock(),
        )

        @asynccontextmanager
        async def typing():
            yield

        self.message.channel.typing.side_effect = typing
        self.moderator = SimpleNamespace(
            check_and_route=AsyncMock(return_value=(True, "", "none", None)),
            check=AsyncMock(return_value=(True, "")),
        )
        self.gemini = SimpleNamespace(generate=AsyncMock(return_value=("配信への架空の応答", None, [])))
        self.ng_filter = SimpleNamespace(
            contains_ng_word=Mock(return_value=False), mask_with_log=Mock(side_effect=lambda text, **kwargs: text),
        )
        self.service = AutonomousService(
            self.client, self.state, self.gemini, self.moderator, self.ng_filter,
            SimpleNamespace(log_snapshot=Mock()),
        )
        self.service._load_persona = Mock(return_value="架空のペルソナ")
        self.sent = AsyncMock()
        for name, replacement in (
            ("load_image_attachments", AsyncMock(return_value=[])),
            ("fetch_channel_history_text", AsyncMock(return_value="")),
            ("send_long_reply", self.sent),
            ("datetime", Mock(wraps=datetime)),
        ):
            patcher = patch.object(autonomous_service, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        autonomous_service.datetime.now.side_effect = lambda *args: self.now
        self.tasks = []

        def start(coro, **kwargs):
            task = spawn(coro, **kwargs)
            self.tasks.append(task)
            return task

        self.normal_reply = AsyncMock(return_value="通常の架空の応答")
        self.notify = AsyncMock()
        self.topic = SimpleNamespace(mark_activity=Mock(), watch_channel=Mock())
        namespace = {
            "discord": discord, "client": self.client, "state": self.state,
            "auto_topic": self.topic, "autonomous": self.service,
            "blacklist": SimpleNamespace(is_blocked=Mock(return_value=False)),
            "ng_filter": self.ng_filter, "moderator": self.moderator,
            "stats": SimpleNamespace(record=AsyncMock()), "spawn": start,
            "notify_violation": self.notify, "play_tts_response": AsyncMock(),
            "clean_message_content": clean_message_content,
            "is_supported_message_channel": is_supported_message_channel,
            "is_supported_document_attachment": is_supported_document_attachment,
            "attachment_is_image": attachment_is_image,
            "MAX_DOCUMENT_SIZE_BYTES": MAX_DOCUMENT_SIZE_BYTES,
        }
        self.handler = make_message_handler(namespace, self)
        self.handler.handle_chat_message = self.normal_reply
        self.handler.notify_violation = self.notify
        self.on_message = self.handler.on_message

    async def asyncTearDown(self) -> None:
        await asyncio.gather(*self.tasks)

    def new_session(self):
        return AutonomousSession(
            channel_id=10, is_stage=False, started_at=self.now,
            last_human_at=self.now - timedelta(minutes=1),
            last_spoke_at=self.now - timedelta(minutes=1),
        )

    async def test_five_comments_make_only_one_input_and_output_check(self) -> None:
        for _ in range(5):
            await self.on_message(self.message)
            self.now += timedelta(seconds=1)
        self.assertEqual(self.moderator.check_and_route.await_count, 1)
        self.assertEqual(self.moderator.check.await_count, 1)
        self.assertEqual(self.gemini.generate.await_count, 1)
        self.sent.assert_awaited_once()
        self.assertEqual(self.topic.mark_activity.call_count, 5)
        self.assertEqual(len(self.settings.autonomous_recent_activity), 5)
        self.assertEqual(self.session.last_human_at, self.now - timedelta(seconds=1))

    async def test_cooldown_keeps_local_ng_checks_without_more_ai_calls(self) -> None:
        await self.on_message(self.message)
        claimed_at = self.session.last_comment_reply_at
        self.ng_filter.contains_ng_word.reset_mock()
        self.ng_filter.contains_ng_word.return_value = True
        self.ng_filter.find_ng_words = Mock(return_value=["架空のNG対象"])

        # 受付枠が埋まっていても、ローカルNG判定と違反通知は省略しない。
        for _ in range(3):
            await self.on_message(self.message)

        self.assertEqual(self.ng_filter.contains_ng_word.call_count, 3)
        self.assertEqual(self.ng_filter.find_ng_words.call_count, 3)
        self.assertEqual(self.notify.await_count, 3)
        self.moderator.check_and_route.assert_awaited_once()
        self.moderator.check.assert_awaited_once()
        self.gemini.generate.assert_awaited_once()
        self.sent.assert_awaited_once()
        self.assertEqual(self.session.last_comment_reply_at, claimed_at)

    async def test_concurrent_comments_share_one_admission_slot(self) -> None:
        async def check(*args):
            await asyncio.sleep(0)
            return True, "", "none", None

        self.moderator.check_and_route.side_effect = check
        await asyncio.gather(*(self.on_message(self.message) for _ in range(5)))
        self.assertEqual(self.moderator.check_and_route.await_count, 1)
        self.assertEqual(self.gemini.generate.await_count, 1)
        self.sent.assert_awaited_once()

    async def test_next_comment_after_cooldown_is_accepted(self) -> None:
        await self.on_message(self.message)
        self.now += timedelta(seconds=AUTONOMOUS_COMMENT_COOLDOWN_SECONDS + 1)
        await self.on_message(self.message)
        self.assertEqual(self.moderator.check_and_route.await_count, 2)
        self.assertEqual(self.gemini.generate.await_count, 2)

    async def test_first_comment_after_monologue_is_accepted(self) -> None:
        self.session.last_spoke_at = self.now
        await self.on_message(self.message)
        self.moderator.check_and_route.assert_awaited_once()
        self.sent.assert_awaited_once()

    async def test_admitted_unsafe_input_is_still_rejected(self) -> None:
        self.moderator.check_and_route.return_value = (False, "架空の判定", "none", None)
        await self.on_message(self.message)
        self.notify.assert_awaited_once()
        self.gemini.generate.assert_not_awaited()
        self.sent.assert_not_awaited()

    async def test_admitted_unsafe_output_is_still_screened(self) -> None:
        self.moderator.check.return_value = (False, "架空の判定")
        await self.on_message(self.message)
        self.moderator.check.assert_awaited_once()
        self.assertEqual(self.sent.await_args.args[1], "申し訳ありませんが、適切な応答を生成できませんでした。")

    async def end_session_during_input(self, *args):
        self.settings.autonomous_session = None
        return True, "", "none", None

    async def test_session_end_falls_back_to_normal_mention(self) -> None:
        self.message.mentions = [self.client.user]
        self.moderator.check_and_route.side_effect = self.end_session_during_input
        await self.on_message(self.message)
        self.normal_reply.assert_awaited_once()
        self.gemini.generate.assert_not_awaited()

    async def test_session_end_without_normal_acceptance_is_not_answered(self) -> None:
        self.moderator.check_and_route.side_effect = self.end_session_during_input
        await self.on_message(self.message)
        self.normal_reply.assert_not_awaited()
        self.gemini.generate.assert_not_awaited()
        self.sent.assert_not_awaited()

    async def test_replaced_session_does_not_reuse_old_admission(self) -> None:
        replacement = self.new_session()

        async def replace(*args):
            self.settings.autonomous_session = replacement
            return True, "", "none", None

        self.moderator.check_and_route.side_effect = replace
        await self.on_message(self.message)
        self.gemini.generate.assert_not_awaited()
        self.sent.assert_not_awaited()
        self.assertIsNone(replacement.last_comment_reply_at)

    async def test_direct_comment_handler_still_applies_cooldown(self) -> None:
        await self.service.handle_comment(self.message, self.message.content, self.settings)
        await self.service.handle_comment(self.message, self.message.content, self.settings)
        self.gemini.generate.assert_awaited_once()
        self.sent.assert_awaited_once()

    async def test_normal_mention_outside_stream_is_not_rate_limited(self) -> None:
        self.message.channel.id = 20
        self.message.mentions = [self.client.user]
        for _ in range(2):
            await self.on_message(self.message)
        self.assertEqual(self.moderator.check_and_route.await_count, 2)
        self.assertEqual(self.normal_reply.await_count, 2)
        self.assertIsNone(self.session.last_comment_reply_at)


if __name__ == "__main__":
    unittest.main()
