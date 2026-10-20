"""ボット自身の予期しないボイスチャンネル移動を検知し、接続先を守る（#171）。

Discord には接続せず、ボイス状態の更新イベントと VoiceClient をモックで再現する。
実際の Discord での移動操作（管理者のドラッグ）は未確認。
"""

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord

from persona_bot.config import constants
from persona_bot.services import voice_guard as voice_guard_module
from persona_bot.services.voice_guard import VoiceGuard

BOT_ID = 99
HOME_ID = 10
MOVED_ID = 20


def make_channel(channel_id: int, *, spec: type = discord.VoiceChannel, **permissions) -> Mock:
    channel = Mock(spec=spec, id=channel_id)
    channel.name = f"架空の通話{channel_id}"
    allowed = dict(connect=True, speak=True, mute_members=False)
    allowed.update(permissions)
    channel.permissions_for.return_value = SimpleNamespace(**allowed)
    return channel


def voice_state(channel) -> SimpleNamespace:
    return SimpleNamespace(channel=channel)


class VoiceGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.home = make_channel(HOME_ID)
        self.moved = make_channel(MOVED_ID)
        self.channels = {HOME_ID: self.home, MOVED_ID: self.moved}
        self.guild = SimpleNamespace(
            id=1, me=object(), voice_client=None,
            get_channel=Mock(side_effect=lambda channel_id: self.channels.get(channel_id)),
        )
        self.voice_client = self.make_voice_client(self.home)
        self.guild.voice_client = self.voice_client
        self.client = SimpleNamespace(user=SimpleNamespace(id=BOT_ID), get_guild=Mock(return_value=self.guild))
        self.autonomous = SimpleNamespace(
            manages_connection=Mock(return_value=False), handle_external_move=AsyncMock(),
        )
        self.guard = VoiceGuard(self.client, self.autonomous)
        self.member = SimpleNamespace(id=BOT_ID, guild=self.guild)
        self.tasks: list[asyncio.Task] = []

        def start(coro, **kwargs):
            task = asyncio.ensure_future(coro)
            self.tasks.append(task)
            return task

        for name, value in {
            "spawn": Mock(side_effect=start),
            "VOICE_MOVE_GUARD_SETTLE_SECONDS": 0,
            "VOICE_MOVE_GUARD_SETTLE_RETRIES": 2,
        }.items():
            patcher = patch.object(voice_guard_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_voice_client(self, channel) -> Mock:
        voice_client = Mock(spec=discord.VoiceClient)
        voice_client.channel = channel
        voice_client.is_connected.return_value = True
        voice_client.is_playing.return_value = True

        async def move_to(target, *, timeout):
            voice_client.channel = target
            # 戻った結果のボイス状態の更新も、実際と同じくイベントとして届く
            self.guard.handle_voice_state_update(self.member, voice_state(self.moved), voice_state(target))

        async def disconnect(*, force):
            if self.guild.voice_client is voice_client:
                self.guild.voice_client = None

        voice_client.move_to = AsyncMock(side_effect=move_to)
        voice_client.disconnect = AsyncMock(side_effect=disconnect)
        return voice_client

    def event(self, before, after, *, member=None) -> None:
        self.guard.handle_voice_state_update(member or self.member, voice_state(before), voice_state(after))

    async def settle(self) -> None:
        while self.tasks:
            await self.tasks.pop(0)

    async def connect_then_move(self) -> None:
        self.event(None, self.home)
        self.voice_client.channel = self.moved
        self.event(self.home, self.moved)
        await self.settle()

    def assert_left_without_return(self) -> None:
        self.voice_client.move_to.assert_not_awaited()
        self.voice_client.disconnect.assert_awaited_once_with(force=True)
        self.assertIsNone(self.guard.home_channel_id(1))

    # ---- 基本の動作 ------------------------------------------------------------

    async def test_manual_move_returns_to_the_original_channel(self) -> None:
        await self.connect_then_move()
        # 移動先で古い読み上げを最後まで流さない
        self.voice_client.stop.assert_called_once()
        self.voice_client.move_to.assert_awaited_once_with(
            self.home, timeout=constants.VOICE_CONNECT_TIMEOUT_SECONDS,
        )
        self.assertIs(self.voice_client.channel, self.home)
        self.voice_client.disconnect.assert_not_awaited()
        self.assertEqual(self.guard.home_channel_id(1), HOME_ID)

    async def test_own_connections_disconnects_and_unchanged_updates_are_not_moves(self) -> None:
        self.event(None, self.home)                  # /join・配信の開始
        self.event(self.home, self.home)             # ミュート・発言者の切り替え
        self.event(self.home, None)                  # /leave・配信の終了・外部からの切断
        self.assertIsNone(self.guard.home_channel_id(1))
        self.event(None, self.moved)                 # /join での切り替え（切断→接続）
        self.assertEqual(self.guard.home_channel_id(1), MOVED_ID)
        # ボット以外の利用者の移動は見ない
        self.event(self.moved, self.home, member=SimpleNamespace(id=5, guild=self.guild))
        await self.settle()
        voice_guard_module.spawn.assert_not_called()
        self.voice_client.move_to.assert_not_awaited()
        self.voice_client.disconnect.assert_not_awaited()

    async def test_disabled_guard_leaves_moves_as_they_are(self) -> None:
        with patch.object(voice_guard_module, "VOICE_MOVE_GUARD_ENABLED", False):
            await self.connect_then_move()
        voice_guard_module.spawn.assert_not_called()
        self.voice_client.move_to.assert_not_awaited()

    async def test_move_without_a_seen_connection_returns_to_the_channel_before_the_move(self) -> None:
        self.voice_client.channel = self.moved
        self.event(self.home, self.moved)
        await self.settle()
        self.voice_client.move_to.assert_awaited_once()
        self.assertIs(self.voice_client.channel, self.home)

    async def test_return_to_stage_promotes_the_bot_to_speaker_again(self) -> None:
        self.home = make_channel(HOME_ID, spec=discord.StageChannel, mute_members=True)
        self.channels[HOME_ID] = self.home
        self.voice_client.channel = self.home
        promote = AsyncMock()
        with patch.object(voice_guard_module, "enable_stage_speaker", promote):
            await self.connect_then_move()
        promote.assert_awaited_once_with(self.guild, self.home)

    # ---- 戻れない場合は退出し、無限に再試行しない ------------------------------

    async def test_unreturnable_moves_leave_the_moved_channel(self) -> None:
        cases = {
            "元のチャンネルの削除": lambda: self.channels.pop(HOME_ID),
            "接続権限の喪失": lambda: setattr(
                self.home.permissions_for.return_value, "connect", False),
            "発言権限の喪失": lambda: setattr(
                self.home.permissions_for.return_value, "speak", False),
            "ステージで発言者になれない": lambda: self.channels.__setitem__(
                HOME_ID, make_channel(HOME_ID, spec=discord.StageChannel)),
        }
        for name, break_return in cases.items():
            with self.subTest(case=name):
                await self.asyncSetUp()
                break_return()
                await self.connect_then_move()
                self.assert_left_without_return()

    async def test_failed_or_timed_out_return_leaves_instead_of_retrying(self) -> None:
        cases = {
            "移動の例外": AsyncMock(side_effect=discord.HTTPException(Mock(status=500), "架空の失敗")),
            # discord.py の move_to は時間切れでも例外を出さずに戻ることがある
            "時間切れで元へ戻れない": AsyncMock(return_value=None),
        }
        for name, move_to in cases.items():
            with self.subTest(case=name):
                await self.asyncSetUp()
                self.voice_client.move_to = move_to
                await self.connect_then_move()
                move_to.assert_awaited_once()
                self.voice_client.disconnect.assert_awaited_once_with(force=True)
                self.assertIsNone(self.guard.home_channel_id(1))
                # 退出の後に、どこかへ自動で入り直さない
                self.event(self.moved, None)
                await self.settle()
                self.assertIsNone(self.guard.home_channel_id(1))

    async def test_repeated_moves_stop_returning_after_the_limit(self) -> None:
        self.event(None, self.home)
        for _ in range(constants.VOICE_MOVE_GUARD_MAX_RETURNS):
            self.voice_client.channel = self.moved
            self.event(self.home, self.moved)
            await self.settle()
            self.assertIs(self.voice_client.channel, self.home)
        # /join の打ち直し（同じチャンネルの登録し直し）でも歯止めは外れない
        self.guard.adopt(1, HOME_ID)

        self.voice_client.channel = self.moved
        self.event(self.home, self.moved)
        await self.settle()
        self.assertEqual(self.voice_client.move_to.await_count, constants.VOICE_MOVE_GUARD_MAX_RETURNS)
        self.voice_client.disconnect.assert_awaited_once_with(force=True)
        self.assertIsNone(self.guard.home_channel_id(1))

    async def test_returns_older_than_the_window_are_not_counted(self) -> None:
        clock = [1000.0]
        with patch.object(voice_guard_module, "monotonic", side_effect=lambda: clock[0]):
            self.event(None, self.home)
            for _ in range(constants.VOICE_MOVE_GUARD_MAX_RETURNS + 2):
                self.voice_client.channel = self.moved
                self.event(self.home, self.moved)
                await self.settle()
                clock[0] += constants.VOICE_MOVE_GUARD_WINDOW_SECONDS + 1
        self.assertEqual(self.voice_client.move_to.await_count, constants.VOICE_MOVE_GUARD_MAX_RETURNS + 2)
        self.voice_client.disconnect.assert_not_awaited()

    async def test_connection_that_never_settles_is_disconnected(self) -> None:
        self.voice_client.is_connected.return_value = False
        await self.connect_then_move()
        self.assert_left_without_return()

    # ---- 自律行動と人の操作との競合 --------------------------------------------

    async def test_autonomous_connection_is_closed_by_the_autonomous_service(self) -> None:
        self.autonomous.manages_connection.return_value = True
        await self.connect_then_move()
        self.autonomous.handle_external_move.assert_awaited_once_with(self.guild)
        self.voice_client.move_to.assert_not_awaited()
        self.voice_client.disconnect.assert_not_awaited()

    async def test_leave_during_the_wait_cancels_the_return(self) -> None:
        self.event(None, self.home)
        self.voice_client.channel = self.moved
        self.event(self.home, self.moved)
        # 保護処理が動き出す前に /leave された（保護を解いてから切断する）
        self.guard.release(1)
        await self.voice_client.disconnect(force=True)
        await self.settle()
        self.voice_client.move_to.assert_not_awaited()

    async def test_join_in_the_moved_channel_during_the_wait_keeps_the_bot_there(self) -> None:
        self.event(None, self.home)
        self.voice_client.channel = self.moved
        self.event(self.home, self.moved)
        # 移された先の利用者が /join した（同じチャンネルなので接続を使い続ける）
        self.guard.adopt(1, MOVED_ID)
        await self.settle()
        self.voice_client.move_to.assert_not_awaited()
        self.voice_client.disconnect.assert_not_awaited()

    async def test_connection_replaced_during_the_return_is_not_touched(self) -> None:
        replacement = self.make_voice_client(self.moved)

        async def replaced_by_join(target, *, timeout):
            # 戻している間に /join が切り替えた（保護を解き、新しい接続を登録）
            self.guard.release(1)
            self.guild.voice_client = replacement
            self.guard.adopt(1, MOVED_ID)

        self.voice_client.move_to = AsyncMock(side_effect=replaced_by_join)
        await self.connect_then_move()
        self.voice_client.disconnect.assert_not_awaited()
        replacement.disconnect.assert_not_awaited()
        replacement.move_to.assert_not_awaited()
        self.assertEqual(self.guard.home_channel_id(1), MOVED_ID)


if __name__ == "__main__":
    unittest.main()
