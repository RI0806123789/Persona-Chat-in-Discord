"""設定変更を挟んでも、生成・評価・読み上げで同じ応答設定を使うことを検証する。"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord
from persona_bot.services.affinity_service import extract_affinity_changes
from persona_bot.core.app_state import BotState
from persona_bot.core.background import spawn
from tests.helpers.message_handler_helpers import make_message_handler
from persona_bot.config.constants import GEMINI_RESPONSE_TIMEOUT_SECONDS, HISTORY_LIMIT, MAX_DOCUMENT_SIZE_BYTES
from persona_bot.services.content_moderator import screen_bot_response
from persona_bot.utils.discord_helpers import (
    attachment_is_image, clean_message_content, format_grounding_sources,
    is_supported_document_attachment, is_supported_message_channel,
)
from persona_bot.services.prompting import build_full_prompt, build_memory_sections


class ReplySettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tasks = []
        self.stage = "generate"
        self.response = "架空の応答です。"
        self.state = BotState()
        self.guild_id = 1
        self.generations = []
        self.settings = self.state.get_guild(self.guild_id)
        self.configure_old()
        self.message = SimpleNamespace(
            author=SimpleNamespace(id=11, bot=False, display_name="架空の利用者"),
            guild=SimpleNamespace(id=self.guild_id),
            channel=Mock(spec=discord.TextChannel, id=10),
            content="架空の質問です。", attachments=[], mentions=[],
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), reply=AsyncMock(),
        )

        @asynccontextmanager
        async def typing():
            await self.switch_at("typing")
            yield

        self.message.channel.typing.side_effect = typing

        async def images(attachments):
            await self.switch_at("images")
            return []

        async def generate(*args, **kwargs):
            self.generations.append((args, kwargs))
            await self.switch_at("generate")
            return self.response, None, []

        async def moderate(*args, **kwargs):
            await self.switch_at("moderate")
            return True, ""

        async def send(*args, **kwargs):
            await self.switch_at("send")
            return SimpleNamespace(id=500)

        def start(coro, **kwargs):
            task = spawn(coro, **kwargs)
            self.tasks.append(task)
            return task

        self.feedback = SimpleNamespace(track_reply=Mock())
        self.voice = AsyncMock()
        self.loader = Mock(side_effect=lambda path: f"ペルソナ: {path.as_posix()}")
        self.namespace = {
            "discord": discord, "state": self.state,
            "client": SimpleNamespace(user=SimpleNamespace(id=99)),
            "auto_topic": SimpleNamespace(mark_activity=Mock(), watch_channel=Mock()),
            "autonomous": SimpleNamespace(
                mark_activity=Mock(), is_comment_channel=Mock(return_value=False),
                claim_comment_slot=Mock(return_value=object()),
            ),
            "blacklist": SimpleNamespace(is_blocked=Mock(return_value=False)),
            "ng_filter": SimpleNamespace(
                contains_ng_word=Mock(return_value=False), mask_with_log=Mock(side_effect=lambda text, **kwargs: text),
            ),
            "moderator": SimpleNamespace(
                check_and_route=AsyncMock(return_value=(True, "", "none", None)),
                check=AsyncMock(side_effect=moderate),
            ),
            "stats": SimpleNamespace(record=AsyncMock()),
            "memory": SimpleNamespace(
                build_contexts=AsyncMock(return_value=("", "")),
                append_pending_log=AsyncMock(), flush_if_needed=AsyncMock(),
            ),
            "affinity": SimpleNamespace(build_dynamic_prompt=Mock(return_value="架空の親密度")),
            "gemini": SimpleNamespace(generate=AsyncMock(side_effect=generate)),
            "usage": SimpleNamespace(log_snapshot=Mock()),
            "feedback": self.feedback, "play_tts_response": self.voice,
            "load_image_attachments": AsyncMock(side_effect=images),
            "fetch_channel_history_text": AsyncMock(return_value=""),
            "load_prompt_template": self.loader, "resolve_prompt_path": Path,
            "send_long_reply": AsyncMock(side_effect=send), "spawn": start,
            "clean_message_content": clean_message_content,
            "is_supported_message_channel": is_supported_message_channel,
            "is_supported_document_attachment": is_supported_document_attachment,
            "attachment_is_image": attachment_is_image,
            "format_grounding_sources": format_grounding_sources,
            "screen_bot_response": screen_bot_response,
            "extract_affinity_changes": extract_affinity_changes,
            "build_full_prompt": build_full_prompt, "build_memory_sections": build_memory_sections,
            "HISTORY_LIMIT": HISTORY_LIMIT, "MAX_DOCUMENT_SIZE_BYTES": MAX_DOCUMENT_SIZE_BYTES,
            "GEMINI_RESPONSE_TIMEOUT_SECONDS": GEMINI_RESPONSE_TIMEOUT_SECONDS,
        }
        self.handler = make_message_handler(self.namespace, self)
        self.on_message = self.handler.on_message

    async def asyncTearDown(self) -> None:
        await asyncio.gather(*self.tasks)

    def configure_old(self) -> None:
        self.settings.current_model_name = "old-model"
        self.settings.current_prompt_file = "../prompts/prompt_default.txt"
        self.settings.grounding_enabled = True
        self.settings.current_respond_mode = "all"

    async def switch_at(self, stage) -> None:
        if self.stage == stage:
            self.settings.current_model_name = "new-model"
            self.settings.current_prompt_file = "../prompts/prompt_maid.txt"
            self.settings.grounding_enabled = False
        await asyncio.sleep(0)

    def assert_response_settings(self, *, model, prompt_file, grounding) -> None:
        args, kwargs = self.generations[-1]
        self.assertEqual(args[1], model)
        self.assertIn(f"ペルソナ: {prompt_file}", args[0])
        self.assertEqual(kwargs["grounding"], grounding)
        self.assertEqual(self.feedback.track_reply.call_args.kwargs, {
            "target_user_id": "11", "model_name": model, "prompt_file": prompt_file,
        })
        self.assertEqual(self.voice.await_args.args, (self.message, "架空の応答です。", prompt_file))

    async def check_old_settings_are_used(self) -> None:
        await self.on_message(self.message)
        self.assert_response_settings(model="old-model", prompt_file="../prompts/prompt_default.txt", grounding=True)

    async def test_generation_change_uses_old_settings_then_next_reply_uses_new(self) -> None:
        await self.check_old_settings_are_used()
        await self.on_message(self.message)
        self.assert_response_settings(model="new-model", prompt_file="../prompts/prompt_maid.txt", grounding=False)

    async def test_settings_are_captured_before_typing_await(self) -> None:
        self.stage = "typing"
        await self.check_old_settings_are_used()

    async def test_attachment_wait_does_not_mix_old_and_new_settings(self) -> None:
        self.stage = "images"
        await self.check_old_settings_are_used()

    async def test_moderation_change_does_not_relabel_feedback_or_voice(self) -> None:
        self.stage = "moderate"
        await self.check_old_settings_are_used()

    async def test_send_change_does_not_relabel_feedback_or_voice(self) -> None:
        self.stage = "send"
        await self.check_old_settings_are_used()

    async def test_dm_reply_uses_same_snapshot(self) -> None:
        self.settings = self.state.get_guild(None)
        self.configure_old()
        self.message.guild = None
        self.message.channel = Mock(spec=discord.DMChannel, id=10)

        @asynccontextmanager
        async def typing():
            yield

        self.message.channel.typing.side_effect = typing
        await self.check_old_settings_are_used()

    async def test_generation_failure_is_not_tracked_or_spoken(self) -> None:
        self.response = None
        await self.on_message(self.message)
        self.feedback.track_reply.assert_not_called()
        self.voice.assert_not_awaited()
        self.message.reply.assert_awaited_once()

    async def test_affinity_tag_saves_only_the_reply_authors_changes(self) -> None:
        self.response = "架空の応答です。[V:1, A:2]"
        affinity = self.namespace["affinity"]
        affinity.update_emotion = Mock()
        affinity.save_background = AsyncMock()
        await self.on_message(self.message)
        await asyncio.gather(*self.tasks)
        affinity.update_emotion.assert_called_once_with("11", 1, 2)
        affinity.save_background.assert_awaited_once_with("11")

    async def test_autonomous_comment_passes_snapshot_to_generation_and_voice(self) -> None:
        self.namespace["autonomous"].is_comment_channel.return_value = True
        self.namespace["autonomous"].can_speak_session = Mock(return_value=True)

        async def comment(*args, **kwargs):
            await self.switch_at("generate")
            return self.response

        self.namespace["autonomous"].handle_comment = AsyncMock(side_effect=comment)
        await self.on_message(self.message)
        self.namespace["gemini"].generate.assert_not_awaited()
        self.feedback.track_reply.assert_not_called()
        settings = self.namespace["autonomous"].handle_comment.await_args.kwargs["response_settings"]
        self.assertEqual(settings.model_name, "old-model")
        self.assertTrue(settings.grounding_enabled)
        self.voice.assert_awaited_once_with(
            self.message, self.response, settings.prompt_file, is_valid=ANY,
        )
        self.assertEqual(settings.prompt_file, "../prompts/prompt_default.txt")


if __name__ == "__main__":
    unittest.main()
