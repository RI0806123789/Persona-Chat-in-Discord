"""退出したサーバーだけの一時状態を、停止処理の後に回収する。"""

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.core.app_state import BotState
from persona_bot.core.guild_lifecycle import remove_guild_state


class GuildRemoveTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_removed_guild_is_released_after_stop_even_on_failure(self):
        for error in (None, OSError("架空の障害"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                state = BotState()
                removed, other, dm = state.get_guild(1), state.get_guild(2), state.get_guild(None)
                state.welcomed_guild_ids.update((1, 2))
                state._db = Mock()

                async def stop(*args, **kwargs):
                    self.assertIs(state.guild_settings[1], removed)
                    if error is not None:
                        raise error

                autonomous = SimpleNamespace(stop_session=AsyncMock(side_effect=stop))
                guard = SimpleNamespace(release=Mock())
                with patch("builtins.print"):
                    if isinstance(error, asyncio.CancelledError):
                        with self.assertRaises(asyncio.CancelledError):
                            await remove_guild_state(1, state, autonomous, guard)
                    else:
                        await remove_guild_state(1, state, autonomous, guard)
                autonomous.stop_session.assert_awaited_once_with(1, reason="サーバーからの退出")
                guard.release.assert_called_once_with(1)
                self.assertNotIn(1, state.guild_settings)
                self.assertIs(state.guild_settings[2], other)
                self.assertIs(state.dm_settings, dm)
                self.assertEqual(state.welcomed_guild_ids, {2})
                self.assertEqual(state._db.mock_calls, [])

    async def test_unknown_or_duplicate_removal_does_not_leave_settings(self):
        state = BotState()

        async def stop(guild_id, **kwargs):
            state.get_guild(guild_id)

        autonomous = SimpleNamespace(stop_session=AsyncMock(side_effect=stop))
        for _ in range(2):
            await remove_guild_state(1, state, autonomous, Mock())
            self.assertEqual(state.guild_settings, {})
