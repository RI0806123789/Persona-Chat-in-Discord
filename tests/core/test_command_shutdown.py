"""実際のCommandTree経路のコマンド本体と後片付けを終了時に回収する。"""

import asyncio
from contextlib import closing as closing_connection
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord
from discord import app_commands
from persona_bot.core import bot_client, commands
from persona_bot.core.app_state import BotState
from persona_bot.core.background import run_in_thread, wait_for_cleanup
from persona_bot.core.bot_client import PersonaClient
from persona_bot.core.database import Database
from tests.helpers.network_guard import block_network
from tests.helpers.persona_helpers import install_empty_persona_catalog
from persona_bot.utils.logging_setup import LOG_DATE_FORMAT, LOG_FORMAT, OperationalFormatter


LOG_FORMATTER = OperationalFormatter(LOG_FORMAT, LOG_DATE_FORMAT)


class CommandShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        block_network(self)
        install_empty_persona_catalog(self)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "bot.db"
        self.db = Database(self.path)
        self.addCleanup(self.db.close)
        self.db.initialize()
        self.order = []
        close_db = self.db.close

        def close():
            self.order.append("db")
            close_db()

        for patcher in (
            patch.object(self.db, "close", side_effect=close),
            patch.object(discord.Client, "close", new=AsyncMock(side_effect=lambda: self.order.append("discord"))),
            patch.object(bot_client, "close_genai_client", new=AsyncMock(side_effect=lambda *_: self.order.append("gemini"))),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = PersonaClient(genai_client=object(), database=self.db, intents=discord.Intents.none())
        await self.client._async_setup_hook()
        self.tree = app_commands.CommandTree(self.client)
        self.blacklist = SimpleNamespace(is_blocked=Mock(return_value=False))
        self.autonomous = SimpleNamespace(stop_session=AsyncMock(return_value=False))
        commands.register_commands(
            self.tree, BotState(), None, None, self.blacklist, None, None, None, None, self.autonomous,
            task_owner=self.client.background_tasks,
        )
        self.channel = Mock(spec=discord.VoiceChannel, id=20)
        self.channel.name = "架空の通話"
        self.channel.permissions_for.return_value = SimpleNamespace(view_channel=True, connect=True, speak=True)
        self.channel.connect = AsyncMock()
        self.guild = SimpleNamespace(id=1, me=object(), voice_client=None)
        self.dispatched = []
        self.addAsyncCleanup(self.cleanup_tasks)

    async def cleanup_tasks(self):
        for task in self.dispatched:
            task.cancel()
        await asyncio.gather(*self.dispatched, return_exceptions=True)
        await self.client.close()

    def interaction(self, name):
        return SimpleNamespace(
            data={"name": name, "type": 1}, type=discord.InteractionType.application_command,
            _state=self.client._connection, command_failed=False, guild=self.guild, guild_id=1,
            command=self.tree.get_command(name),
            user=Mock(spec=discord.Member, id=11, voice=SimpleNamespace(channel=self.channel),
                      guild_permissions=SimpleNamespace(administrator=False)),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), is_done=Mock(return_value=False)),
            followup=SimpleNamespace(send=AsyncMock()), original_response=AsyncMock(),
        )

    def dispatch(self, interaction):
        # SDK自身がタスクを作る経路を通し、直接コールバックを呼ばない。
        before = asyncio.all_tasks()
        self.tree._from_interaction(interaction)
        created, = asyncio.all_tasks() - before
        self.dispatched.append(created)
        return created

    async def assert_waiting_body_stops(self, name):
        started = asyncio.Event()
        release = asyncio.Event()
        interaction = self.interaction(name)

        async def wait(*args, **kwargs):
            started.set()
            await release.wait()

        response = interaction.response.send_message if name == "functions" else interaction.response.defer
        response.side_effect = wait
        with patch.object(commands, "FunctionsView", return_value=Mock()):
            with self.client.background_tasks.scope():
                body = self.dispatch(interaction)
            try:
                await asyncio.wait_for(started.wait(), 2)
                await asyncio.wait_for(self.client.close(), 2)
                release.set()
                await asyncio.gather(body, return_exceptions=True)
            finally:
                release.set()
                body.cancel()
                await asyncio.gather(body, return_exceptions=True)
        self.assertTrue(body.cancelled())
        self.autonomous.stop_session.assert_not_awaited()
        self.channel.connect.assert_not_awaited()
        interaction.original_response.assert_not_awaited()
        self.assertEqual(self.order, ["discord", "gemini", "db"])

    async def test_real_join_body_is_canceled_before_connections_close(self):
        """応答保留中の参加コマンドは終了後に接続へ進まない。"""
        await self.assert_waiting_body_stops("join")

    async def test_real_leave_body_is_canceled_before_connections_close(self):
        """応答保留中の退出コマンドも所有タスクとして回収する。"""
        await self.assert_waiting_body_stops("leave")

    async def test_real_functions_body_is_canceled_before_connections_close(self):
        """機能パネルの送信待ちも終了管理から漏らさない。"""
        await self.assert_waiting_body_stops("functions")

    async def test_new_dispatch_after_stopping_never_starts_any_command_body(self):
        """停止後の全コマンドはブラックリスト判定や応答も始めない。"""
        await self.client.close()
        for name in ("functions", "join", "leave"):
            with self.subTest(command=name):
                interaction = self.interaction(name)
                body = self.dispatch(interaction)
                await asyncio.gather(body, return_exceptions=True)
                self.assertTrue(body.cancelled())
                interaction.response.send_message.assert_not_awaited()
                interaction.response.defer.assert_not_awaited()
        self.blacklist.is_blocked.assert_not_called()
        self.channel.connect.assert_not_awaited()

    async def test_repeated_cancellation_waits_for_command_cleanup_and_database_write(self):
        """本体と終了要求への繰り返しキャンセルでも、後片付けの保存を先に終える。"""
        started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        interaction = self.interaction("join")

        async def cleanup():
            cleaning.set()
            await release.wait()
            await run_in_thread(self.db.pending_append, "11", "fictional-time", "架空の後片付け", "完了")
            self.order.append("cleanup")

        async def defer():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await wait_for_cleanup(cleanup(), label="コマンドの後片付け確認")

        interaction.response.defer.side_effect = defer
        body = self.dispatch(interaction)
        closing = None
        try:
            await asyncio.wait_for(started.wait(), 2)
            closing = asyncio.create_task(self.client.close())
            await asyncio.wait_for(cleaning.wait(), 2)
            for _ in range(2):
                body.cancel()
                closing.cancel()
                await asyncio.sleep(0)
                self.assertFalse(closing.done())
                self.assertEqual(self.order, [])
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(closing, 2)
            await asyncio.gather(body, return_exceptions=True)
        finally:
            release.set()
            await self.client.close()
            await asyncio.gather(*[task for task in (body, closing) if task is not None], return_exceptions=True)
        self.assertEqual(self.order, ["cleanup", "discord", "gemini", "db"])
        with closing_connection(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM pending_log").fetchone()[0], 1)
        self.channel.connect.assert_not_awaited()

    async def test_real_dispatch_keeps_blacklist_and_global_error_handler(self):
        """共通入口を通してもブロック判定とCommandTreeの失敗通知を維持する。"""
        blocked = self.interaction("join")
        self.blacklist.is_blocked.return_value = True
        await self.dispatch(blocked)
        blocked.response.send_message.assert_awaited_once()
        blocked.response.defer.assert_not_awaited()
        self.blacklist.is_blocked.return_value = False
        failing = self.interaction("join")
        failing.response.defer.side_effect = OSError("架空の応答エラー")
        await self.dispatch(failing)
        self.assertTrue(failing.command_failed)
        failing.response.send_message.assert_awaited_once()
        self.assertIs(self.tree.on_error, commands.handle_command_error)
        self.channel.connect.assert_not_awaited()

    async def test_shutdown_waits_for_error_notice_and_repeated_cancellation(self):
        """本体失敗後の通知まで所有し、通知完了後にキャンセルを伝えて共有接続を閉じる。"""
        entered, release = asyncio.Event(), asyncio.Event()
        interaction = self.interaction("join")
        interaction.response.is_done.return_value = True
        error = RuntimeError("架空の非公開情報")
        self.autonomous.stop_session.side_effect = error

        async def notify(*args, **kwargs):
            entered.set()
            await release.wait()
            self.order.append("notice")

        interaction.followup.send.side_effect = notify
        self.tree.on_error = AsyncMock(wraps=self.tree.on_error)
        closing = None
        with patch.object(commands, "handle_command_error", wraps=commands.handle_command_error) as handle:
            body = self.dispatch(interaction)
            try:
                await asyncio.wait_for(entered.wait(), 2)
                self.assertIn(body, self.client.background_tasks._tasks)
                closing = asyncio.create_task(self.client.close())
                for _ in range(2):
                    await asyncio.sleep(0)
                    body.cancel()
                    closing.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(body.done())
                    self.assertFalse(closing.done())
                    self.assertIn(body, self.client.background_tasks._tasks)
                    self.assertEqual(self.order, [])
                release.set()
                await asyncio.wait_for(asyncio.gather(body, closing, return_exceptions=True), 2)
                await self.client.close()
            finally:
                release.set()
                await asyncio.gather(*[task for task in (body, closing) if task is not None], return_exceptions=True)
                await self.client.close()
            handle.assert_awaited_once()
        self.assertTrue(body.cancelled())
        self.assertEqual(self.order, ["notice", "discord", "gemini", "db"])
        self.assertEqual(self.client.background_tasks._tasks, {})
        interaction.followup.send.assert_awaited_once()
        self.assertTrue(interaction.followup.send.call_args.kwargs["ephemeral"])
        self.tree.on_error.assert_not_awaited()

    async def test_body_error_wrapping_and_single_notice_through_real_tree(self):
        """通常例外だけを包み、既存のコマンド例外を保持して一度だけ通知する。"""
        self.tree.on_error = AsyncMock(wraps=self.tree.on_error)
        errors = [RuntimeError("架空の非公開情報"), TypeError("架空の型エラー"),
                  app_commands.MissingPermissions(["manage_guild"]),
                  app_commands.CommandOnCooldown(app_commands.Cooldown(1, 5), 3)]
        for name in ("functions", "join", "leave"):
            for error in errors:
                with self.subTest(command=name, error=type(error).__name__):
                    interaction = self.interaction(name)
                    self.blacklist.is_blocked.side_effect = error
                    with patch.object(commands, "handle_command_error", wraps=commands.handle_command_error) as handle, patch.object(
                        self.client, "dispatch",
                    ) as completion:
                        body = self.dispatch(interaction)
                        await asyncio.wait_for(body, 2)
                    handle.assert_awaited_once()
                    reported = handle.call_args.args[1]
                    if isinstance(error, app_commands.AppCommandError):
                        self.assertIs(reported, error)
                    else:
                        self.assertIsInstance(reported, app_commands.CommandInvokeError)
                        self.assertIs(reported.original, error)
                        self.assertIs(reported.command, interaction.command)
                    interaction.response.send_message.assert_awaited_once()
                    self.assertTrue(interaction.response.send_message.call_args.kwargs["ephemeral"])
                    interaction.followup.send.assert_not_awaited()
                    self.assertTrue(interaction.command_failed)
                    self.assertNotIn(body, self.client.background_tasks._tasks)
                    completion.assert_not_called()
        self.tree.on_error.assert_not_awaited()

    async def test_notification_failure_releases_owner_and_allows_shutdown(self):
        """通知先の切断や予期しない通知エラーでも再通知せず、終了を妨げない。"""
        self.autonomous.stop_session.side_effect = RuntimeError("架空の本体エラー")
        self.tree.on_error = AsyncMock(wraps=self.tree.on_error)
        errors = [discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "架空の期限切れ"),
                  OSError("架空の非公開情報")]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                interaction = self.interaction("leave")
                interaction.response.is_done.return_value = True
                interaction.followup.send.side_effect = error
                with self.assertLogs("persona_bot.core.commands", level="ERROR") as logs:
                    body = self.dispatch(interaction)
                    await asyncio.wait_for(body, 2)
                self.assertNotIn("架空の非公開情報", "\n".join(LOG_FORMATTER.format(record) for record in logs.records))
                self.assertTrue(any(record.exc_info and record.exc_info[1] is error for record in logs.records))
                interaction.followup.send.assert_awaited_once()
                interaction.response.send_message.assert_not_awaited()
                self.assertTrue(interaction.command_failed)
                self.assertNotIn(body, self.client.background_tasks._tasks)
        self.tree.on_error.assert_not_awaited()
        await asyncio.wait_for(self.client.close(), 2)
        self.assertEqual(self.order, ["discord", "gemini", "db"])

    async def test_body_and_notification_cancellation_propagate_without_retry(self):
        """本体・失敗通知のどちらのキャンセルも、二重通知せず呼出元へ伝播する。"""
        self.tree.on_error = AsyncMock(wraps=self.tree.on_error)
        for during_notice in (False, True):
            with self.subTest(during_notice=during_notice):
                interaction = self.interaction("leave")
                interaction.response.is_done.return_value = True
                self.autonomous.stop_session.side_effect = (
                    RuntimeError("架空の本体エラー") if during_notice else asyncio.CancelledError
                )
                interaction.followup.send.side_effect = asyncio.CancelledError
                with patch.object(commands, "handle_command_error", wraps=commands.handle_command_error) as handle:
                    body = self.dispatch(interaction)
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(body, 2)
                    self.assertEqual(handle.await_count, int(during_notice))
                self.assertEqual(interaction.followup.send.await_count, int(during_notice))
                self.assertNotIn(body, self.client.background_tasks._tasks)
        self.tree.on_error.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
