"""LLMが返すnullや非文字列を利用者向けの本文として採用しない。"""

import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.services.content_moderator import ContentModerator, _normalize_reminder


class LlmValueValidationTests(unittest.IsolatedAsyncioTestCase):
    def test_reminder_requires_nonempty_strings_for_both_required_fields(self) -> None:
        valid = {"at": " 2026-12-01 12:00 ", "message": " 架空の予定 ", "repeat": " DAILY "}
        self.assertEqual(_normalize_reminder(valid), {
            "at": "2026-12-01 12:00", "message": "架空の予定", "repeat": "daily",
        })
        for key in ("at", "message"):
            for value in (None, 42, True, [], {}, " "):
                with self.subTest(key=key, value=value):
                    self.assertIsNone(_normalize_reminder({**valid, key: value}))

    async def test_unsafe_result_keeps_unsafe_flag_without_stringifying_invalid_reason(self) -> None:
        moderator = ContentModerator(None, SimpleNamespace(words=[]))
        for reason in (None, 42, True, [], {}):
            with self.subTest(reason=reason), patch(
                "persona_bot.services.content_moderator.generate_content", new=AsyncMock(
                    return_value=SimpleNamespace(text=json.dumps({"safe": False, "reason": reason})),
                ),
            ):
                self.assertEqual(await moderator.check("架空の入力"), (False, ""))


if __name__ == "__main__":
    unittest.main()
