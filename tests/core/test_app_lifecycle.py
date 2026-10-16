"""起動時の状態復元と終了時の後片付けを確かめる（#68）。"""

from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord
from persona_bot.core import bot_client
from persona_bot.core.app_state import BotState
from persona_bot.core.bot_client import PersonaClient
from persona_bot.core.database import Database


class RestoreAttachTests(unittest.IsolatedAsyncioTestCase):
    async def test_settings_created_before_restore_are_persisted(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        db = Database(Path(directory.name) / "bot.db")
        self.addCleanup(db.close)
        with patch("builtins.print"):
            db.initialize()
        state = BotState()
        early = state.get_guild(1)  # 復元より前に作られた設定
        state.restore_from_database(db)
        early.current_model_name = "fictional-model"
        await early.persist()
        restored = BotState()
        restored.restore_from_database(db)
        self.assertEqual(restored.get_guild(1).current_model_name, "fictional-model")


class ClientCloseTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_is_closed_last_even_when_earlier_steps_fail(self) -> None:
        cases = [(None, None), (OSError("架空のDiscord終了失敗"), None), (None, OSError("架空のGemini終了失敗"))]
        for discord_error, gemini_error in cases:
            with self.subTest(discord_error=discord_error, gemini_error=gemini_error):
                order = []

                def step(name, error):
                    def run(*args, **kwargs):
                        order.append(name)
                        if error is not None:
                            raise error
                    return run

                database = Mock(close=Mock(side_effect=step("db", None)))
                client = PersonaClient(genai_client=object(), database=database, intents=discord.Intents.none())
                with patch.object(discord.Client, "close", new=AsyncMock(side_effect=step("discord", discord_error))), \
                        patch.object(bot_client, "close_genai_client", new=AsyncMock(side_effect=step("gemini", gemini_error))):
                    if discord_error or gemini_error:
                        with self.assertRaises(OSError):
                            await client.close()
                    else:
                        await client.close()
                self.assertEqual(order, ["discord", "gemini", "db"])
                database.close.assert_called_once()

    async def test_client_without_database_still_closes(self) -> None:
        client = PersonaClient(genai_client=SimpleNamespace(), intents=discord.Intents.none())
        with patch.object(discord.Client, "close", new=AsyncMock()), \
                patch.object(bot_client, "close_genai_client", new=AsyncMock()) as closing:
            await client.close()
        closing.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
