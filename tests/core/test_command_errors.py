"""コマンド全体の失敗通知を応答前・応答後とも本人だけへ返す。"""

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord
from discord import app_commands
from persona_bot.core.commands import handle_command_error, register_commands
from persona_bot.utils.logging_setup import LOG_DATE_FORMAT, LOG_FORMAT, OperationalFormatter


LOG_FORMATTER = OperationalFormatter(LOG_FORMAT, LOG_DATE_FORMAT)


class CommandErrorTests(unittest.IsolatedAsyncioTestCase):
    def interaction(self, done=False):
        return SimpleNamespace(
            response=SimpleNamespace(is_done=Mock(return_value=done), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    def test_registration_attaches_the_global_error_handler(self):
        client = discord.Client(intents=discord.Intents.none())
        tree = app_commands.CommandTree(client)
        register_commands(tree, *([None] * 9))
        self.assertIs(tree.on_error, handle_command_error)

    async def test_errors_are_private_and_do_not_expose_exception_details(self):
        errors = [
            app_commands.CommandInvokeError(Mock(), RuntimeError("架空の非公開情報")),
            app_commands.CommandInvokeError(Mock(), discord.Forbidden(
                SimpleNamespace(status=403, reason="Forbidden"), "架空の非公開情報",
            )),
            app_commands.MissingPermissions(["manage_guild"]),
            app_commands.BotMissingPermissions(["view_channel"]),
            app_commands.NoPrivateMessage(), app_commands.CheckFailure(),
            app_commands.CommandOnCooldown(app_commands.Cooldown(1, 5), 3),
        ]
        for error in errors:
            for done in (False, True):
                with self.subTest(error=type(error).__name__, done=done), self.assertLogs("persona_bot.core.commands", level="ERROR") as logs:
                    interaction = self.interaction(done)
                    await handle_command_error(interaction, error)
                    sent = interaction.followup.send if done else interaction.response.send_message
                    unused = interaction.response.send_message if done else interaction.followup.send
                    sent.assert_awaited_once()
                    unused.assert_not_awaited()
                    self.assertTrue(sent.call_args.kwargs["ephemeral"])
                    self.assertNotIn("架空の非公開情報", sent.call_args.args[0])
                    self.assertTrue(logs.records)
                    self.assertNotIn("架空の非公開情報", "\n".join(LOG_FORMATTER.format(record) for record in logs.records))
                    self.assertIs(logs.records[0].exc_info[1], getattr(error, "original", error))

    async def test_notification_failure_is_logged_and_cancellation_propagates(self):
        for done in (False, True):
            interaction = self.interaction(done)
            sent = interaction.followup.send if done else interaction.response.send_message
            sent.side_effect = discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "架空の期限切れ")
            with self.assertLogs("persona_bot.core.commands", level="ERROR") as logs:
                await handle_command_error(interaction, app_commands.AppCommandError())
            self.assertIn("失敗通知を送信できませんでした", logs.records[-1].getMessage())
            self.assertNotIn("架空の期限切れ", LOG_FORMATTER.format(logs.records[-1]))
            self.assertIs(logs.records[-1].exc_info[1], sent.side_effect)
            sent.side_effect = asyncio.CancelledError
            with self.assertRaises(asyncio.CancelledError):
                await handle_command_error(interaction, app_commands.AppCommandError())
