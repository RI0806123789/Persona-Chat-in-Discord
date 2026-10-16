"""/join が同じチャンネルへの再接続を避け、接続の待ち時間を明示することを確かめる（#67）。"""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord
from persona_bot.config import constants
from persona_bot.core.app_state import BotState
from persona_bot.core.commands import register_commands


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
        channel.permissions_for.return_value = SimpleNamespace(connect=True)
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


if __name__ == "__main__":
    unittest.main()
