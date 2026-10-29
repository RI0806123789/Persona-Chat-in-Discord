"""/join・/leave と音声会話（Gemini Live。#268）の連携を確かめる。"""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord
from persona_bot.audio.voice_receive import LiveVoiceClient
from persona_bot.core.app_state import BotState
from persona_bot.core.commands import register_commands
from persona_bot.core.voice_commands import VoiceCommandCoordinator
from persona_bot.services.live_session import LiveUnavailableError
from persona_bot.services.voice_conversation import (
    NOTICE_LIVE_CAPACITY, NOTICE_LIVE_QUOTA, NOTICE_LIVE_UNAVAILABLE,
)
from tests.helpers.network_guard import block_network


class FakeLiveVoiceClient(LiveVoiceClient):
    """受信に対応した接続の代わり（実際の接続処理は行わない）。isinstance の判定はそのまま通る。"""

    created: list["FakeLiveVoiceClient"] = []

    def __init__(self, client, channel):
        self.client = client
        self.channel = channel
        self.connected = True
        self.disconnect = AsyncMock()
        FakeLiveVoiceClient.created.append(self)

    def is_connected(self) -> bool:
        return self.connected


class JoinLiveVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        block_network(self)
        self.commands = {}

        def command(**kwargs):
            def register(callback):
                self.commands[kwargs["name"]] = callback
                return callback
            return register

        self.autonomous = SimpleNamespace(stop_session=AsyncMock(return_value=False))
        self.voice_guard = Mock(name="voice_guard")
        self.live = SimpleNamespace(
            reserve=Mock(return_value=True), release_reservation=Mock(), start=AsyncMock(),
            stop=AsyncMock(return_value=False), get=Mock(return_value=None),
        )
        self.coordinator = VoiceCommandCoordinator()
        with patch("persona_bot.core.commands.VoiceCommandCoordinator", return_value=self.coordinator):
            register_commands(
                SimpleNamespace(command=command), BotState(), None, None,
                SimpleNamespace(is_blocked=Mock(return_value=False)),
                None, None, None, None, self.autonomous,
                voice_guard=self.voice_guard, voice_conversation=self.live,
            )
        self.channel = Mock(spec=discord.VoiceChannel, id=20)
        self.channel.name = "架空の通話"
        self.channel.permissions_for.return_value = SimpleNamespace(view_channel=True, connect=True, speak=True)
        FakeLiveVoiceClient.created = []
        self.created = FakeLiveVoiceClient.created

        async def connect(*, timeout, cls):
            voice_client = cls(SimpleNamespace(), self.channel)
            self.guild.voice_client = voice_client
            return voice_client

        self.channel.connect = AsyncMock(side_effect=connect)
        self.guild = SimpleNamespace(id=1, me=object(), voice_client=None)
        self.interaction = SimpleNamespace(
            guild=self.guild, guild_id=1, channel=object(),
            command=SimpleNamespace(name="join"),
            user=Mock(spec=discord.Member, voice=SimpleNamespace(channel=self.channel)),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), is_done=Mock(return_value=False)),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def join(self) -> str:
        with patch("persona_bot.core.commands.LiveVoiceClient", FakeLiveVoiceClient):
            await self.commands["join"](self.interaction)
        return self.interaction.followup.send.call_args.args[0]

    async def test_join_connects_with_the_receiving_client_and_starts_live(self):
        reply = await self.join()
        self.assertEqual(len(self.created), 1)
        self.live.reserve.assert_called_with(1)
        self.live.start.assert_awaited_once_with(
            self.guild, self.created[0], self.channel, ANY, fallback_channel=self.interaction.channel,
        )
        self.assertIn("同意して参加", reply)
        self.live.release_reservation.assert_called_with(1)

    async def test_capacity_refuses_before_connecting(self):
        self.live.reserve.return_value = False
        reply = await self.join()
        self.assertEqual(reply, NOTICE_LIVE_CAPACITY)
        self.channel.connect.assert_not_awaited()
        self.assertEqual(
            self.interaction.followup.send.await_args.kwargs["allowed_mentions"].to_dict(), {"parse": []},
        )

    async def test_live_failure_disconnects_and_explains(self):
        for error, notice in ((LiveUnavailableError(quota=True), NOTICE_LIVE_QUOTA),
                              (LiveUnavailableError(quota=False), NOTICE_LIVE_UNAVAILABLE),
                              (FileNotFoundError("架空のペルソナ"), NOTICE_LIVE_UNAVAILABLE)):
            with self.subTest(error=type(error).__name__):
                self.guild.voice_client = None
                FakeLiveVoiceClient.created.clear()
                self.live.start.side_effect = error
                with self.assertLogs("persona_bot.core.commands", level="ERROR") if not isinstance(error, LiveUnavailableError) else _no_logs():
                    reply = await self.join()
                self.assertEqual(reply, notice)
                self.created[0].disconnect.assert_awaited_once_with(force=True)
                self.voice_guard.release.assert_called_with(1)

    async def test_same_channel_reuses_a_running_conversation(self):
        voice_client = FakeLiveVoiceClient(None, self.channel)
        self.guild.voice_client = voice_client
        self.live.get.return_value = SimpleNamespace(stopped=False, voice_client=voice_client)
        reply = await self.join()
        self.live.start.assert_not_awaited()
        self.channel.connect.assert_not_awaited()
        self.assertIn("既に接続しています", reply)

    async def test_same_channel_without_conversation_starts_on_the_same_connection(self):
        voice_client = FakeLiveVoiceClient(None, self.channel)
        self.guild.voice_client = voice_client
        await self.join()
        self.channel.connect.assert_not_awaited()
        self.live.start.assert_awaited_once()
        self.assertIs(self.live.start.await_args.args[1], voice_client)

    async def test_plain_voice_client_is_reconnected_for_receiving(self):
        plain = Mock(spec=discord.VoiceClient)
        plain.channel = self.channel
        plain.is_connected.return_value = True
        plain.disconnect = AsyncMock()
        self.guild.voice_client = plain
        await self.join()
        plain.disconnect.assert_awaited_once_with(force=True)
        self.live.stop.assert_awaited_once()
        self.channel.connect.assert_awaited_once()

    async def test_leave_stops_the_conversation_before_disconnecting(self):
        voice_client = FakeLiveVoiceClient(None, self.channel)
        self.guild.voice_client = voice_client
        order = []
        self.live.stop.side_effect = lambda *args, **kwargs: order.append("stop") or True
        voice_client.disconnect.side_effect = lambda **kwargs: order.append("disconnect")
        self.interaction.command = SimpleNamespace(name="leave")
        await self.commands["leave"](self.interaction)
        self.assertEqual(order, ["stop", "disconnect"])
        self.assertIn("退出しました", self.interaction.followup.send.await_args.args[0])


class _no_logs:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


if __name__ == "__main__":
    unittest.main()
