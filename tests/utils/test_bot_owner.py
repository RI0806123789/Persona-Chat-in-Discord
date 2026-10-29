"""全サーバー共通の違反情報に対する、ボット所有者本人の認可を検証する（#279）。"""

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.utils.discord_helpers import is_bot_owner, is_current_bot_owner


class BotOwnerTests(unittest.IsolatedAsyncioTestCase):
    def interaction(self, *, owner_id=11, team=None):
        application = SimpleNamespace(owner=SimpleNamespace(id=owner_id), team=team)
        return SimpleNamespace(
            user=SimpleNamespace(id=11),
            client=SimpleNamespace(application=application, application_info=AsyncMock(return_value=application)),
        )

    async def test_personal_owner_and_team_owner_only(self):
        cases = (
            (11, None, True), (22, None, False),
            (11, SimpleNamespace(owner_id=22), False),
            (22, SimpleNamespace(owner_id=11), True),
            (11, SimpleNamespace(owner_id=None), False),
        )
        for owner_id, team, expected in cases:
            with self.subTest(owner_id=owner_id, team=team):
                interaction = self.interaction(owner_id=owner_id, team=team)
                self.assertEqual(is_bot_owner(interaction), expected)
                self.assertEqual(await is_current_bot_owner(interaction), expected)

    async def test_missing_or_invalid_identity_never_grants_ownership(self):
        for value in (None, True, "11", 0, -1):
            with self.subTest(owner_id=value):
                interaction = self.interaction(owner_id=value)
                self.assertFalse(is_bot_owner(interaction))
                self.assertFalse(await is_current_bot_owner(interaction))
        interaction = self.interaction()
        interaction.client.application = None
        self.assertFalse(is_bot_owner(interaction))

    async def test_fresh_owner_overrides_cached_owner_and_failure_denies(self):
        interaction = self.interaction()
        interaction.client.application_info.return_value = SimpleNamespace(owner=SimpleNamespace(id=22), team=None)
        self.assertTrue(is_bot_owner(interaction))
        self.assertFalse(await is_current_bot_owner(interaction))
        interaction.client.application_info.side_effect = RuntimeError("架空の取得失敗")
        with self.assertLogs("persona_bot.utils.discord_helpers", level="ERROR"):
            self.assertFalse(await is_current_bot_owner(interaction))
        interaction.client.application_info.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await is_current_bot_owner(interaction)
