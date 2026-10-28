"""サブエージェントの起動条件と、ルーターが返す委任内容の正規化を検証する（#170）。"""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.config.constants import (
    AGENT_MAX_STEPS, AGENT_ROLE_MAX_CHARS, AGENT_STEP_NAME_MAX_CHARS, AGENT_TASK_MAX_CHARS,
)
from persona_bot.services import agent_plan
from persona_bot.services.agent_plan import AgentPlan, normalize_agent_plan, requests_subagent
from persona_bot.services.content_moderator import ContentModerator


class TriggerTests(unittest.TestCase):
    def test_only_messages_that_mention_subagent_trigger(self) -> None:
        for text in (
            "サブエージェントを使って、架空の街のカフェを調べて",
            "サブ エージェントで比較して",
            "Use a SubAgent for this",
            "sub-agent で調べて",
        ):
            with self.subTest(text=text):
                self.assertTrue(requests_subagent(text))
        for text in ("", "架空の街のカフェを調べて", "エージェントって何？", "サブクエスト"):
            with self.subTest(text=text):
                self.assertFalse(requests_subagent(text))

    def test_disabled_feature_never_triggers(self) -> None:
        with patch.object(agent_plan, "AGENT_ENABLED", False):
            self.assertFalse(requests_subagent("サブエージェントで調べて"))


class NormalizeTests(unittest.TestCase):
    def test_unreadable_values_mean_no_judgement(self) -> None:
        for value in (None, "true", [], {}, {"delegate": "たぶん"}, {"delegate": True}, {"delegate": True, "task": "  "}):
            with self.subTest(value=value):
                self.assertIsNone(normalize_agent_plan(value))

    def test_declined_plan_has_no_instructions(self) -> None:
        for flag in (False, "false", "no"):
            with self.subTest(flag=flag):
                self.assertEqual(normalize_agent_plan({"delegate": flag, "task": "無視される"}), AgentPlan(delegate=False))

    def test_delegated_plan_is_trimmed_to_limits(self) -> None:
        plan = normalize_agent_plan({
            "delegate": "true",
            "role": "架空の\nリサーチ   担当" + "あ" * AGENT_ROLE_MAX_CHARS,
            "task": "  " + "い" * (AGENT_TASK_MAX_CHARS + 10) + "  ",
            "expected_output": "箇条書きで",
            "steps": ["候補の\n検索", "", 3, "比較" + "う" * AGENT_STEP_NAME_MAX_CHARS] + ["追加"] * AGENT_MAX_STEPS,
        })
        self.assertTrue(plan.delegate)
        self.assertTrue(plan.role.startswith("架空の リサーチ 担当"))
        self.assertLessEqual(len(plan.role), AGENT_ROLE_MAX_CHARS)
        self.assertEqual(len(plan.task), AGENT_TASK_MAX_CHARS)
        self.assertEqual(plan.expected_output, "箇条書きで")
        # 空・文字列以外の手順は捨て、改行は空白1つにし、上限の数で切る
        self.assertEqual(plan.steps[0], "候補の 検索")
        self.assertEqual(len(plan.steps), AGENT_MAX_STEPS)
        self.assertTrue(all(len(step) <= AGENT_STEP_NAME_MAX_CHARS for step in plan.steps))

    def test_missing_role_and_steps_get_defaults(self) -> None:
        plan = normalize_agent_plan({"delegate": True, "task": "架空の調査"})
        self.assertEqual((plan.role, plan.steps), ("調査担当", ("依頼された作業",)))

    def test_all_extracted_instructions_are_single_line_and_have_no_active_mentions(self) -> None:
        plan = normalize_agent_plan({
            "delegate": True, "role": "@everyone\n調査担当", "task": "架空の調査\r\n@here",
            "expected_output": "箇条書き\n<@22>", "steps": ["検索\n<@&11>"],
        })
        self.assertEqual(plan.task, "架空の調査 ＠here")
        for text in (plan.role, plan.task, plan.expected_output, *plan.steps):
            self.assertNotIn("@", text)
            self.assertNotIn("\n", text)
            self.assertNotIn("\r", text)


class RouterIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """委任の判定は check_and_route と同じ1回の呼び出しに統合されている。"""

    def setUp(self) -> None:
        self.moderator = ContentModerator(None, SimpleNamespace(words=[]))

    async def test_plan_is_returned_from_the_single_router_call(self) -> None:
        response = SimpleNamespace(text=(
            '{"safe": true, "category": "profile", "reminder": null, "agent": {"delegate": true, '
            '"role": "架空の調査担当", "task": "架空の街のカフェを調べる", "expected_output": "箇条書き", '
            '"steps": ["検索", "比較"]}}'
        ))
        request = AsyncMock(return_value=response)
        with patch("persona_bot.services.content_moderator.generate_content", request):
            safe, reason, category, reminder, plan = await self.moderator.check_route_and_plan(
                "サブエージェントで調べて", "架空の利用者: さっきの架空の街の話",
            )
        self.assertEqual((safe, reason, category, reminder), (True, "", "profile", None))
        self.assertEqual(plan, AgentPlan(True, "架空の調査担当", "架空の街のカフェを調べる", "箇条書き", ("検索", "比較")))
        request.assert_awaited_once()
        options = request.await_args.kwargs
        # 指示・出力例はシステム側へ、会話履歴は判定用データだけへ渡す。
        self.assertIn("追加タスク3", options["system_instruction"])
        self.assertIn("さっきの架空の街の話", options["contents"])
        self.assertNotIn("さっきの架空の街の話", options["system_instruction"])
        self.assertIn('"reminder": null, "agent": null}', options["system_instruction"])

    async def test_normal_routing_prompt_does_not_include_agent_task(self) -> None:
        request = AsyncMock(return_value=SimpleNamespace(text='{"safe": true, "category": "none", "reminder": null}'))
        with patch("persona_bot.services.content_moderator.generate_content", request):
            self.assertEqual(await self.moderator.check_and_route("架空の質問"), (True, "", "none", None))
        for prompt in (request.await_args.kwargs["contents"], request.await_args.kwargs["system_instruction"]):
            self.assertNotIn("追加タスク3", prompt)
            self.assertNotIn('"agent"', prompt)
        self.assertNotIn("agent", request.await_args.kwargs["response_schema"]["properties"])

    async def test_unsafe_or_unreadable_results_never_delegate(self) -> None:
        cases = [
            SimpleNamespace(text='{"safe": false, "reason": "架空の理由", "agent": {"delegate": true, "task": "架空"}}'),
            SimpleNamespace(text="JSONではない架空の応答"),
            RuntimeError("架空の通信障害"),
        ]
        for response in cases:
            request = AsyncMock(side_effect=response) if isinstance(response, Exception) else AsyncMock(return_value=response)
            with self.subTest(response=repr(response)), patch(
                "persona_bot.services.content_moderator.generate_content", request,
            ), patch("builtins.print"):
                result = await self.moderator.check_route_and_plan("サブエージェントで調べて")
                self.assertIsNone(result[4])


if __name__ == "__main__":
    unittest.main()
