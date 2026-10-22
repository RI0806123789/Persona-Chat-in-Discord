"""会話の中でサブエージェントの使用を明記した依頼の振り分けと、人格役の返信を検証する（#170）。"""

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

from persona_bot.config.constants import AGENT_ROUTER_HISTORY_LIMIT
from persona_bot.core.app_state import BotState
from persona_bot.services.agent_plan import AgentPlan
from persona_bot.services.agent_service import AgentService
from persona_bot.services.gemini_service import GenerationResult
from tests.helpers.message_handler_helpers import make_message_handler
from tests.helpers.persona_helpers import install_empty_persona_catalog

PLAN = AgentPlan(True, "架空の調査担当", "架空の街のカフェを調べる", "箇条書き", ("候補の検索", "特徴の比較"))
SOURCE = {"title": "架空の出典", "uri": "https://example.invalid/cafe"}


def close_coroutine(coro, **kwargs):
    # 会話統計の記録など、裏で走らせる処理はこのテストの対象外なので実行せずに閉じる
    coro.close()


class AgentDelegationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        install_empty_persona_catalog(self)
        self.state = BotState()
        self.state.get_guild(1).current_respond_mode = "all"
        channel = Mock(spec=discord.TextChannel, id=10)

        @asynccontextmanager
        async def typing():
            yield

        channel.typing.side_effect = typing
        self.progress_message = SimpleNamespace(edit=AsyncMock())
        self.message = SimpleNamespace(
            author=SimpleNamespace(id=11, bot=False, display_name="架空の利用者"),
            guild=SimpleNamespace(id=1), channel=channel,
            content="サブエージェントを使って、架空の街のカフェを調べて", attachments=[], mentions=[],
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            reply=AsyncMock(return_value=self.progress_message),
        )
        self.moderator = SimpleNamespace(
            check_and_route=AsyncMock(return_value=(True, "", "none", None)),
            check_route_and_plan=AsyncMock(return_value=(True, "", "none", None, PLAN)),
            check=AsyncMock(return_value=(True, "")),
        )
        self.generations = []

        async def generate(prompt, model_name, images=None, timeout=None, grounding=False):
            self.generations.append(SimpleNamespace(prompt=prompt, model=model_name, grounding=grounding))
            if "依頼主（別のAI）" in prompt:
                step = sum("依頼主（別のAI）" in item.prompt for item in self.generations)
                return GenerationResult(f"架空の子の出力{step}", None, [SOURCE], model_name)
            return GenerationResult("架空の人格役の返信です。", None, [], model_name)

        self.gemini = SimpleNamespace(generate_with_metadata=AsyncMock(side_effect=generate))
        self.usage = SimpleNamespace(log_snapshot=Mock(), log_tokens=Mock())
        self.agent = AgentService(self.gemini, self.usage)
        self.history = AsyncMock(return_value="架空の履歴")
        self.send = AsyncMock(return_value=SimpleNamespace(id=500))
        self.tts = AsyncMock()
        self.handler = make_message_handler({
            "client": SimpleNamespace(user=SimpleNamespace(id=99)), "state": self.state,
            "auto_topic": SimpleNamespace(mark_activity=Mock(), watch_channel=Mock()),
            "autonomous": SimpleNamespace(
                mark_activity=Mock(), is_comment_channel=Mock(return_value=False), claim_comment_slot=Mock(),
            ),
            "blacklist": SimpleNamespace(is_blocked=Mock(return_value=False)),
            "ng_filter": SimpleNamespace(
                contains_ng_word=Mock(return_value=False), mask_with_log=Mock(side_effect=lambda text, **kwargs: text),
            ),
            "moderator": self.moderator, "gemini": self.gemini, "usage": self.usage, "agent": self.agent,
            "stats": SimpleNamespace(record=AsyncMock()),
            "memory": SimpleNamespace(
                build_contexts=AsyncMock(return_value=("", "")),
                append_pending_log=AsyncMock(return_value=None), flush_if_needed=AsyncMock(),
            ),
            "affinity": SimpleNamespace(build_dynamic_prompt=Mock(return_value="")),
            "reminders": SimpleNamespace(create_from_request=AsyncMock()),
            "documents": SimpleNamespace(enqueue_from_message=AsyncMock()),
            "feedback": SimpleNamespace(track_reply=Mock()),
            "spawn": close_coroutine, "play_tts_response": self.tts,
            "load_image_attachments": AsyncMock(return_value=[]),
            "fetch_channel_history_text": self.history,
            "load_persona_template": AsyncMock(return_value="架空のペルソナ"),
            "send_long_reply": self.send,
        }, self)

    def child_calls(self):
        return [item for item in self.generations if "依頼主（別のAI）" in item.prompt]

    def persona_calls(self):
        return [item for item in self.generations if "依頼主（別のAI）" not in item.prompt]

    async def test_delegated_request_runs_child_then_persona_replies(self) -> None:
        await self.handler.on_message(self.message)

        # 委任の判定はルーターの1回の呼び出しに統合され、直近の会話を委任内容の整理に渡す
        self.moderator.check_route_and_plan.assert_awaited_once()
        self.assertEqual(self.moderator.check_route_and_plan.await_args.args[1], "架空の履歴")
        self.assertEqual(self.history.await_args_list[0].args[3], AGENT_ROUTER_HISTORY_LIMIT)
        self.moderator.check_and_route.assert_not_awaited()

        # 子は手順の数だけ検索付きで動き、ペルソナは渡されない
        self.assertEqual(len(self.child_calls()), len(PLAN.steps))
        self.assertTrue(all(item.grounding for item in self.child_calls()))
        self.assertTrue(all("架空のペルソナ" not in item.prompt for item in self.child_calls()))

        # 人格役は子の最終報告を受け取り、検索せずにペルソナとして返信する
        persona = self.persona_calls()
        self.assertEqual(len(persona), 1)
        self.assertIn("架空のペルソナ", persona[0].prompt)
        self.assertIn("架空の調査担当）からの報告", persona[0].prompt)
        self.assertIn(f"架空の子の出力{len(PLAN.steps)}", persona[0].prompt)
        self.assertFalse(persona[0].grounding)

        # 返信は人格役だけが送り、子の出典を添える。読み上げも人格役の返信
        reply_text = self.send.await_args.args[1]
        self.assertTrue(reply_text.startswith("架空の人格役の返信です。"))
        self.assertIn(SOURCE["uri"], reply_text)
        self.assertEqual(self.tts.await_args.args[1], "架空の人格役の返信です。")

        # 進捗は定型文の1通を書き換え、終わったら中止ボタンを外す
        self.assertIn("架空の調査担当", self.message.reply.await_args_list[0].args[0])
        self.assertIsNone(self.progress_message.edit.await_args.kwargs["view"])
        # 実行枠は返却される
        self.assertTrue(self.agent.try_acquire("11"))

    async def test_message_without_mention_of_subagent_uses_normal_routing(self) -> None:
        self.message.content = "架空の街のカフェを調べて"
        await self.handler.on_message(self.message)
        self.moderator.check_and_route.assert_awaited_once()
        self.moderator.check_route_and_plan.assert_not_awaited()
        self.assertEqual(self.child_calls(), [])
        self.assertEqual(len(self.persona_calls()), 1)

    async def test_declined_or_missing_plan_answers_directly(self) -> None:
        for plan, declined in ((AgentPlan(delegate=False), True), (None, False)):
            with self.subTest(plan=plan):
                self.generations.clear()
                self.moderator.check_route_and_plan.return_value = (True, "", "none", None, plan)
                await self.handler.on_message(self.message)
                self.assertEqual(self.child_calls(), [])
                persona = self.persona_calls()
                self.assertEqual(len(persona), 1)
                # 不要と判断したときだけ、人格役がその旨を一言添える
                self.assertEqual("サブエージェント（調査を任せる子のAI）の使用を求めました" in persona[0].prompt, declined)
                # 普段どおりサーバーの検索設定に従う
                self.assertTrue(persona[0].grounding)

    async def test_failed_child_reports_with_template_and_skips_persona(self) -> None:
        self.gemini.generate_with_metadata.side_effect = lambda *args, **kwargs: GenerationResult(None, None, [], None)
        await self.handler.on_message(self.message)
        self.assertEqual(self.gemini.generate_with_metadata.await_count, 1)
        self.send.assert_not_awaited()
        self.tts.assert_not_awaited()
        self.assertIn("うまくいきませんでした", self.progress_message.edit.await_args.kwargs["content"])
        self.assertTrue(self.agent.try_acquire("11"))

    async def test_busy_user_gets_template_without_generation(self) -> None:
        self.assertTrue(self.agent.try_acquire("11"))
        await self.handler.on_message(self.message)
        self.gemini.generate_with_metadata.assert_not_awaited()
        self.assertIn("別のサブエージェントの作業", self.message.reply.await_args.args[0])
        self.send.assert_not_awaited()

    async def test_unsafe_request_is_not_delegated(self) -> None:
        self.moderator.check_route_and_plan.return_value = (False, "架空の理由", "none", None, None)
        self.handler.notify_violation = AsyncMock()
        await self.handler.on_message(self.message)
        self.handler.notify_violation.assert_awaited_once()
        self.gemini.generate_with_metadata.assert_not_awaited()

    async def test_broadcast_comment_does_not_use_subagent(self) -> None:
        self.handler.autonomous.is_comment_channel.return_value = True
        self.handler.autonomous.claim_comment_slot.return_value = object()
        self.handler.autonomous.handle_comment = AsyncMock(return_value=None)
        await self.handler.on_message(self.message)
        self.moderator.check_and_route.assert_awaited_once()
        self.moderator.check_route_and_plan.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
