"""統計画像の所有権、生成失敗の案内、パネル操作時の権限を検証する。"""

import asyncio
import io
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord
from persona_bot.views import stats_view
from persona_bot.views.functions_view import UnblockSelect, UnblockView
from persona_bot.views.stats_view import RunStatsButton, StatsUserSelect, StatsView


class StatsDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.buffers = [io.BytesIO(b"png-1"), io.BytesIO(b"png-2")]
        for buffer in self.buffers:
            self.addCleanup(buffer.close)
        self.stats = SimpleNamespace(
            build_dashboard=AsyncMock(return_value=("架空の統計", [(f"{i}.png", b) for i, b in enumerate(self.buffers)])),
            get_user_options=Mock(return_value=[("11", "架空の本人", 1), ("22", "架空の対象", 2)]),
            display_name=Mock(return_value="架空の対象"),
        )
        self.view = StatsView(self.stats, viewer_id="11", is_admin=True)
        self.addCleanup(self.view.stop)
        self.button = next(item for item in self.view.children if isinstance(item, RunStatsButton))
        self.interaction = SimpleNamespace(
            user=Mock(spec=discord.Member, guild_permissions=SimpleNamespace(administrator=True)),
            response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()),
            edit_original_response=AsyncMock(), followup=SimpleNamespace(send=AsyncMock()),
        )

    async def test_success_and_send_failure_release_files_before_buffers(self) -> None:
        for fails in (False, True):
            with self.subTest(fails=fails):
                buffers = [io.BytesIO(b"png") for _ in range(2)]
                self.stats.build_dashboard.return_value = ("架空の統計", [(f"{i}.png", b) for i, b in enumerate(buffers)])
                self.interaction.followup.send.side_effect = RuntimeError("架空の送信失敗") if fails else None
                await self.button.callback(self.interaction)
                self.assertTrue(all(buffer.closed for buffer in buffers))
                for file in self.interaction.followup.send.await_args.kwargs["files"]:
                    self.assertTrue(file.fp.closed)

    async def test_generation_failure_replaces_busy_panel_with_error(self) -> None:
        self.stats.build_dashboard.side_effect = RuntimeError("架空の生成失敗")
        await self.button.callback(self.interaction)
        self.assertTrue(self.view.is_finished())
        self.assertIn("失敗", self.interaction.edit_original_response.await_args.kwargs["content"])
        self.assertNotIn("集計中", self.interaction.edit_original_response.await_args.kwargs["content"])

    async def test_partial_file_creation_failure_releases_every_buffer(self) -> None:
        original = discord.File
        calls = []

        def create(buffer, **kwargs):
            calls.append(buffer)
            if len(calls) == 2:
                raise RuntimeError("架空のファイル生成失敗")
            return original(buffer, **kwargs)

        with patch.object(stats_view.discord, "File", side_effect=create):
            await self.button.callback(self.interaction)
        self.assertTrue(all(buffer.closed for buffer in self.buffers))
        self.assertIn("失敗", self.interaction.edit_original_response.await_args.kwargs["content"])

    async def test_send_cancellation_releases_files_and_propagates(self) -> None:
        self.interaction.followup.send.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.button.callback(self.interaction)
        self.assertTrue(all(buffer.closed for buffer in self.buffers))

    async def test_revoked_admin_cannot_view_another_user(self) -> None:
        self.view.temp_target_id = "22"
        self.interaction.user.guild_permissions.administrator = False
        await self.button.callback(self.interaction)
        self.stats.build_dashboard.assert_not_awaited()
        self.interaction.response.send_message.assert_awaited_once()
        self.assertTrue(self.interaction.response.send_message.await_args.kwargs["ephemeral"])

    async def test_non_admin_still_views_self_and_viewer_is_passed(self) -> None:
        self.view.is_admin = False
        self.view.temp_target_id = "22"
        self.interaction.user.guild_permissions.administrator = False
        await self.button.callback(self.interaction)
        self.stats.build_dashboard.assert_awaited_once_with("11", "all", viewer_id="11")

    async def test_revoked_admin_cannot_change_target(self) -> None:
        select = next(item for item in self.view.children if isinstance(item, StatsUserSelect))
        select._values = ["22"]
        self.interaction.user.guild_permissions.administrator = False
        await select.callback(self.interaction)
        self.assertEqual(self.view.temp_target_id, "11")
        self.interaction.response.edit_message.assert_not_awaited()
        self.interaction.response.send_message.assert_awaited_once()

    async def test_permission_revoked_during_generation_does_not_send_other_users_graph(self) -> None:
        self.view.temp_target_id = "22"
        result = self.stats.build_dashboard.return_value

        async def generate(*args, **kwargs):
            self.interaction.user.guild_permissions.administrator = False
            await asyncio.sleep(0)
            return result

        self.stats.build_dashboard.side_effect = generate
        await self.button.callback(self.interaction)
        self.interaction.followup.send.assert_not_awaited()
        self.assertNotEqual(self.interaction.edit_original_response.await_args.kwargs["content"], "架空の統計")
        self.assertTrue(all(buffer.closed for buffer in self.buffers))


class UnblockPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_permission_is_rechecked_before_unblocking(self) -> None:
        for admin in (False, True):
            with self.subTest(admin=admin):
                blacklist = SimpleNamespace(unblock=AsyncMock(return_value=True), is_blocked=Mock(return_value=False))
                view = UnblockView(blacklist, [{"user_id": 22, "user_name": "架空の対象"}])
                self.addCleanup(view.stop)
                select = next(item for item in view.children if isinstance(item, UnblockSelect))
                select._values = ["22"]
                interaction = SimpleNamespace(
                    user=Mock(spec=discord.Member, guild_permissions=SimpleNamespace(administrator=admin)),
                    response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
                    edit_original_response=AsyncMock(),
                )
                await select.callback(interaction)
                if admin:
                    blacklist.unblock.assert_awaited_once_with(22)
                else:
                    blacklist.unblock.assert_not_awaited()
                    interaction.response.defer.assert_not_awaited()
                    interaction.response.send_message.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
