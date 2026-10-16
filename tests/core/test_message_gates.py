"""受信時の関門の順序・画像だけの発言・違反警告・未対応添付の案内を検証する（#65）。"""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord
from persona_bot.config import constants
from persona_bot.config.constants import BLACKLIST_MAX_VIOLATIONS
from persona_bot.core.app_state import BotState
from tests.helpers.message_handler_helpers import make_message_handler


def close_coroutine(coro, **kwargs):
    # 会話統計の記録など、裏で走らせる処理はこのテストの対象外なので実行せずに閉じる
    coro.close()


class MessageGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.state = BotState()
        self.state.get_guild(1).current_respond_mode = "all"
        channel = Mock(spec=discord.TextChannel, id=10)

        @asynccontextmanager
        async def typing():
            yield

        channel.typing.side_effect = typing
        self.message = SimpleNamespace(
            author=SimpleNamespace(id=11, bot=False, display_name="架空の利用者"),
            guild=SimpleNamespace(id=1), channel=channel,
            content="架空の質問です。", attachments=[], mentions=[],
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), reply=AsyncMock(),
        )
        self.auto_topic = SimpleNamespace(mark_activity=Mock(), watch_channel=Mock())
        self.autonomous = SimpleNamespace(
            mark_activity=Mock(), is_comment_channel=Mock(return_value=False), claim_comment_slot=Mock(),
        )
        self.blacklist = SimpleNamespace(is_blocked=Mock(return_value=False), record_violation=AsyncMock())
        self.moderator = SimpleNamespace(check_and_route=AsyncMock(return_value=(True, "", "none", None)))
        self.gemini = SimpleNamespace(generate=AsyncMock(return_value=(None, None, [])))
        self.images = AsyncMock(return_value=[])
        self.handler = make_message_handler({
            "client": SimpleNamespace(user=SimpleNamespace(id=99)), "state": self.state,
            "auto_topic": self.auto_topic, "autonomous": self.autonomous, "blacklist": self.blacklist,
            "ng_filter": SimpleNamespace(contains_ng_word=Mock(return_value=False)),
            "moderator": self.moderator, "gemini": self.gemini,
            "stats": SimpleNamespace(record=AsyncMock()),
            "memory": SimpleNamespace(build_contexts=AsyncMock(return_value=("", ""))),
            "affinity": SimpleNamespace(build_dynamic_prompt=Mock(return_value="")),
            "reminders": SimpleNamespace(create_from_request=AsyncMock()),
            "documents": SimpleNamespace(enqueue_from_message=AsyncMock()),
            "spawn": close_coroutine, "play_tts_response": AsyncMock(),
            "load_image_attachments": self.images,
            "fetch_channel_history_text": AsyncMock(return_value=""),
            "load_prompt_template": Mock(return_value="架空のペルソナ"), "resolve_prompt_path": Path,
        }, self)

    @staticmethod
    def attachment(filename: str, content_type: str | None = None) -> SimpleNamespace:
        return SimpleNamespace(filename=filename, content_type=content_type, size=10)

    # ---- ブロック中のユーザー --------------------------------------------------

    async def test_blocked_user_is_dropped_before_activity_is_observed(self) -> None:
        self.blacklist.is_blocked.return_value = True
        await self.handler.on_message(self.message)
        self.auto_topic.mark_activity.assert_not_called()
        self.autonomous.mark_activity.assert_not_called()
        self.autonomous.is_comment_channel.assert_not_called()
        self.moderator.check_and_route.assert_not_awaited()
        self.message.reply.assert_not_awaited()

    async def test_unblocked_user_is_still_observed_even_without_mention(self) -> None:
        self.state.get_guild(1).current_respond_mode = "mention"
        await self.handler.on_message(self.message)
        self.auto_topic.mark_activity.assert_called_once_with(10, 1)
        self.autonomous.mark_activity.assert_called_once_with(10, 1)
        self.moderator.check_and_route.assert_not_awaited()

    # ---- 画像だけの発言 --------------------------------------------------------

    async def test_image_only_message_with_unreadable_images_does_not_call_gemini(self) -> None:
        self.message.content = ""
        self.message.attachments = [self.attachment("broken.png", "image/png")]
        await self.handler.on_message(self.message)
        self.images.assert_awaited_once()
        self.gemini.generate.assert_not_awaited()
        self.message.reply.assert_awaited_once()
        reply = self.message.reply.call_args.args[0]
        self.assertIn("画像を読み込めませんでした", reply)
        self.assertIn("png", reply)

    async def test_image_only_message_with_readable_image_uses_shared_question(self) -> None:
        image = object()
        self.images.return_value = [image]
        self.message.content = ""
        self.message.attachments = [self.attachment("fictional.png", "image/png")]
        await self.handler.on_message(self.message)
        self.gemini.generate.assert_awaited_once()
        prompt, _model, images = self.gemini.generate.call_args.args
        self.assertIn(constants.IMAGE_ONLY_QUESTION, prompt)
        self.assertEqual(images, [image])

    async def test_question_with_unreadable_image_still_answers_text(self) -> None:
        self.message.attachments = [self.attachment("broken.png", "image/png")]
        await self.handler.on_message(self.message)
        self.gemini.generate.assert_awaited_once()
        self.assertIn("架空の質問です。", self.gemini.generate.call_args.args[0])

    # ---- 違反警告 --------------------------------------------------------------

    async def test_violation_warning_matches_recorded_state(self) -> None:
        cases = {
            "初回": ((1, False), f"あと{BLACKLIST_MAX_VIOLATIONS - 1}回"),
            "今回でブロック": ((BLACKLIST_MAX_VIOLATIONS, True), "ブラックリストに登録されました"),
        }
        for name, (recorded, expected) in cases.items():
            with self.subTest(case=name):
                self.message.reply.reset_mock()
                self.blacklist.record_violation.return_value = recorded
                await self.handler.notify_violation(self.message, "架空の違反")
                self.message.reply.assert_awaited_once()
                self.assertIn(expected, self.message.reply.call_args.args[0])

    async def test_violation_after_concurrent_block_does_not_warn_zero_remaining(self) -> None:
        # 並行した別の違反で既にブロック済み（record_violation は回数を増やさず False を返す）
        self.blacklist.record_violation.return_value = (BLACKLIST_MAX_VIOLATIONS, False)
        await self.handler.notify_violation(self.message, "架空の違反")
        self.message.reply.assert_not_awaited()

    # ---- 未対応の添付 ----------------------------------------------------------

    async def test_unsupported_attachment_guide_mentions_images_and_documents(self) -> None:
        self.message.attachments = [self.attachment("fictional.exe", "application/octet-stream")]
        await self.handler.on_message(self.message)
        self.message.reply.assert_awaited_once()
        reply = self.message.reply.call_args.args[0]
        self.assertIn("画像（", reply)
        self.assertIn("png", reply)
        self.assertIn("pdf", reply)
        self.gemini.generate.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
