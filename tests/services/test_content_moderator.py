"""不明な空応答で違反を作らず、明示的な不適切判定は維持する。"""

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from google.genai import types
from persona_bot.services.content_moderator import ContentModerator


class ModeratorFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_or_invalid_results_fail_open_in_both_entrypoints(self):
        moderator = ContentModerator(None, SimpleNamespace(words=[]))
        cases = [
            None, types.GenerateContentResponse(),
            types.GenerateContentResponse(candidates=[]),
            SimpleNamespace(text=""), SimpleNamespace(text="   "),
            SimpleNamespace(text="JSONではない架空の応答"),
            types.GenerateContentResponse(candidates=[types.Candidate(finish_reason="SAFETY")]),
            asyncio.TimeoutError(), RuntimeError("架空の通信障害"),
        ]
        for response in cases:
            with self.subTest(response=repr(response)):
                request = AsyncMock(side_effect=response) if isinstance(response, Exception) else AsyncMock(return_value=response)
                with patch("persona_bot.services.content_moderator.generate_content", request), patch("builtins.print"):
                    self.assertEqual(await moderator.check("架空の入力"), (True, ""))
                    self.assertEqual(await moderator.check_and_route("架空の入力"), (True, "", "none", None))

    async def test_explicit_prompt_block_or_unsafe_json_remains_unsafe(self):
        moderator = ContentModerator(None, SimpleNamespace(words=[]))
        responses = [
            types.GenerateContentResponse(prompt_feedback=types.GenerateContentResponsePromptFeedback(block_reason="SAFETY")),
            SimpleNamespace(text='{"safe":false,"reason":"架空の理由"}'),
        ]
        for response in responses:
            with self.subTest(response=response), patch(
                "persona_bot.services.content_moderator.generate_content", AsyncMock(return_value=response),
            ), patch("builtins.print"):
                self.assertFalse((await moderator.check("架空の入力"))[0])
                safe, reason, category, reminder = await moderator.check_and_route("架空の入力")
                self.assertFalse(safe)
                self.assertTrue(reason)
                self.assertEqual((category, reminder), ("none", None))
