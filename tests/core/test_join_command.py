"""/join の権限確認・接続の再利用・接続待ち時間を確かめる（#67・#87）。"""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord
from persona_bot.config import constants
from persona_bot.core.app_state import BotState
from persona_bot.core.commands import register_commands
from persona_bot.services import autonomous_stage


class JoinCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.commands = {}

        def command(**kwargs):
            def register(callback):
                self.commands[kwargs["name"]] = callback
                return callback
            return register

        self.autonomous = SimpleNamespace(stop_session=AsyncMock(return_value=False))
        register_commands(SimpleNamespace(command=command), BotState(), None, None,
                          SimpleNamespace(is_blocked=Mock(return_value=False)),
                          None, None, None, None, self.autonomous)
        self.channel = self.make_channel(20)
        self.guild = SimpleNamespace(id=1, me=object(), voice_client=None)
        self.interaction = SimpleNamespace(
            guild=self.guild,
            user=Mock(spec=discord.Member, voice=SimpleNamespace(channel=self.channel)),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    @staticmethod
    def make_channel(channel_id: int) -> Mock:
        channel = Mock(spec=discord.VoiceChannel, id=channel_id)
        channel.name = f"架空の通話{channel_id}"
        channel.permissions_for.return_value = SimpleNamespace(connect=True, speak=True)
        channel.connect = AsyncMock()
        return channel

    @staticmethod
    def make_voice_client(channel: Mock, *, connected: bool = True) -> Mock:
        voice_client = Mock(spec=discord.VoiceClient)
        voice_client.channel = channel
        voice_client.is_connected.return_value = connected
        voice_client.disconnect = AsyncMock()
        return voice_client

    async def join(self) -> str:
        await self.commands["join"](self.interaction)
        return self.interaction.followup.send.call_args.args[0]

    async def test_same_channel_keeps_the_current_connection(self) -> None:
        voice_client = self.make_voice_client(self.channel)
        self.guild.voice_client = voice_client
        reply = await self.join()
        # 自律行動の配信の後片付けは従来どおり先に行う
        self.autonomous.stop_session.assert_awaited_once()
        voice_client.disconnect.assert_not_awaited()
        self.channel.connect.assert_not_awaited()
        self.assertIn("既に接続しています", reply)

    async def test_other_channel_or_dropped_connection_reconnects_with_timeout(self) -> None:
        cases = {
            "別のチャンネル": self.make_voice_client(self.make_channel(30)),
            "切れた接続": self.make_voice_client(self.channel, connected=False),
        }
        for name, voice_client in cases.items():
            with self.subTest(case=name):
                self.channel.connect.reset_mock()
                self.guild.voice_client = voice_client
                reply = await self.join()
                voice_client.disconnect.assert_awaited_once_with(force=True)
                self.channel.connect.assert_awaited_once_with(timeout=constants.VOICE_CONNECT_TIMEOUT_SECONDS)
                self.assertIn("に接続しました", reply)

    async def test_first_join_connects_with_timeout(self) -> None:
        reply = await self.join()
        self.channel.connect.assert_awaited_once_with(timeout=constants.VOICE_CONNECT_TIMEOUT_SECONDS)
        self.assertIn("に接続しました", reply)

    def make_stage(self, *, can_promote=True):
        stage = Mock(spec=discord.StageChannel, id=40)
        stage.name = "架空のステージ"
        stage.permissions_for.return_value = SimpleNamespace(connect=True, speak=True, mute_members=can_promote)
        stage.connect = AsyncMock()
        self.interaction.user.voice.channel = stage
        self.guild.me = SimpleNamespace(voice=SimpleNamespace(channel=stage), edit=AsyncMock())
        return stage

    async def test_stage_join_promotes_bot_before_reporting_speaking(self) -> None:
        stage = self.make_stage()
        reply = await self.join()
        stage.connect.assert_awaited_once_with(timeout=constants.VOICE_CONNECT_TIMEOUT_SECONDS)
        self.guild.me.edit.assert_awaited_once_with(suppress=False)
        self.assertIn("発言者", reply)

    async def test_existing_stage_connection_is_promoted_without_reconnect(self) -> None:
        stage = self.make_stage()
        self.guild.voice_client = self.make_voice_client(stage)
        reply = await self.join()
        stage.connect.assert_not_awaited()
        self.guild.me.edit.assert_awaited_once_with(suppress=False)
        self.assertIn("既に接続", reply)

    async def test_stage_without_moderator_permission_explains_before_connect(self) -> None:
        stage = self.make_stage(can_promote=False)
        await self.commands["join"](self.interaction)
        self.assertIn("メンバーをミュート", self.interaction.response.send_message.call_args.args[0])
        self.interaction.response.defer.assert_not_awaited()
        self.autonomous.stop_session.assert_not_awaited()
        stage.connect.assert_not_awaited()

    async def test_moved_stage_voice_state_is_not_promoted_or_reported_as_speaker(self) -> None:
        self.make_stage()
        self.guild.me.voice.channel = SimpleNamespace(id=50)
        with patch.object(autonomous_stage.asyncio, "sleep", new=AsyncMock()):
            reply = await self.join()
        self.guild.me.edit.assert_not_awaited()
        self.assertIn("発言者になれませんでした", reply)
        self.assertNotIn("発言者として読み上げ", reply)

    async def test_missing_permissions_reject_before_changing_existing_connection(self) -> None:
        for connect, speak in ((False, True), (True, False), (False, False)):
            with self.subTest(connect=connect, speak=speak):
                self.interaction.response.send_message.reset_mock()
                self.channel.permissions_for.return_value = SimpleNamespace(connect=connect, speak=speak)
                current = self.make_voice_client(self.make_channel(30))
                self.guild.voice_client = current

                await self.commands["join"](self.interaction)

                self.channel.permissions_for.assert_called_with(self.guild.me)
                self.interaction.response.send_message.assert_awaited_once()
                reply = self.interaction.response.send_message.call_args.args[0]
                self.assertIn(self.channel.name, reply)
                self.assertEqual("「接続」" in reply, not connect)
                self.assertEqual("「発言」" in reply, not speak)
                self.assertIn("権限", reply)
                self.interaction.response.defer.assert_not_awaited()
                self.autonomous.stop_session.assert_not_awaited()
                current.disconnect.assert_not_awaited()
                self.channel.connect.assert_not_awaited()
                self.interaction.followup.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
