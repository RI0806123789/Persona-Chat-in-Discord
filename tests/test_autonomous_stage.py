"""自律配信の行き先選択とステージ開始をDiscord境界のモックで検証する。"""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import autonomous_stage
from autonomous_stage import pick_channel, prepare_stage


class AutonomousChannelSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.member = object()
        self.stage = self.make_channel()
        self.voice = self.make_channel()
        self.guild = SimpleNamespace(
            me=self.member, stage_channels=[self.stage], voice_channels=[self.voice],
        )

    @staticmethod
    def make_channel():
        permissions = SimpleNamespace(
            view_channel=True, connect=True, speak=True, send_messages=True,
            mute_members=True, manage_channels=True, move_members=True,
        )
        return SimpleNamespace(instance=None, permissions_for=Mock(return_value=permissions))

    def test_stage_is_preferred_and_permissions_are_checked_for_bot_member(self) -> None:
        self.assertIs(pick_channel(self.guild), self.stage)
        self.stage.permissions_for.assert_called_once_with(self.member)

    def test_voice_is_used_when_stage_speaker_permission_is_missing(self) -> None:
        self.stage.permissions_for.return_value.mute_members = False
        self.assertIs(pick_channel(self.guild), self.voice)

    def test_new_stage_needs_management_permissions_but_existing_stage_does_not(self) -> None:
        self.stage.permissions_for.return_value.manage_channels = False
        self.assertIs(pick_channel(self.guild), self.voice)
        self.stage.instance = SimpleNamespace(id=101)
        self.assertIs(pick_channel(self.guild), self.stage)

    def test_channels_without_comment_access_are_excluded(self) -> None:
        self.stage.permissions_for.return_value.send_messages = False
        self.voice.permissions_for.return_value.view_channel = False
        self.assertIsNone(pick_channel(self.guild))

    def test_missing_bot_member_skips_permission_checks(self) -> None:
        self.guild.me = None
        self.assertIsNone(pick_channel(self.guild))
        self.stage.permissions_for.assert_not_called()
        self.voice.permissions_for.assert_not_called()


class AutonomousStagePreparationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.instance = SimpleNamespace(id=101, delete=AsyncMock())
        self.channel = SimpleNamespace(
            name="架空のステージ", instance=None,
            create_instance=AsyncMock(return_value=self.instance),
        )
        self.member = SimpleNamespace(voice=SimpleNamespace(channel=self.channel), edit=AsyncMock())
        self.guild = SimpleNamespace(me=self.member)

    async def test_created_stage_returns_response_id_without_sending_start_notification(self) -> None:
        self.assertEqual(await prepare_stage(self.guild, self.channel), 101)
        self.assertFalse(self.channel.create_instance.await_args.kwargs["send_start_notification"])
        self.member.edit.assert_awaited_once_with(suppress=False)
        self.assertIsNone(self.channel.instance)

    async def test_existing_stage_is_borrowed_without_ownership(self) -> None:
        self.channel.instance = self.instance
        self.assertIsNone(await prepare_stage(self.guild, self.channel))
        self.channel.create_instance.assert_not_awaited()
        self.member.edit.assert_awaited_once_with(suppress=False)

    async def test_speaker_failure_closes_only_stage_created_by_bot(self) -> None:
        self.member.edit.side_effect = RuntimeError("架空の発言権限エラー")
        with self.assertRaises(RuntimeError):
            await prepare_stage(self.guild, self.channel)
        self.instance.delete.assert_awaited_once()

        self.instance.delete.reset_mock()
        self.channel.instance = self.instance
        with self.assertRaises(RuntimeError):
            await prepare_stage(self.guild, self.channel)
        self.instance.delete.assert_not_awaited()

    async def test_stage_replaced_while_enabling_speaker_is_not_deleted(self) -> None:
        replacement = SimpleNamespace(id=202, delete=AsyncMock())

        async def replaced(**kwargs):
            self.channel.instance = replacement
            raise RuntimeError("架空の発言権限エラー")

        self.member.edit.side_effect = replaced
        with self.assertRaises(RuntimeError):
            await prepare_stage(self.guild, self.channel)
        self.instance.delete.assert_not_awaited()
        replacement.delete.assert_not_awaited()

    async def test_voice_cache_is_retried_before_enabling_speaker(self) -> None:
        self.member.voice = None

        async def cache_updated(*args):
            self.member.voice = SimpleNamespace(channel=self.channel)

        with patch.object(autonomous_stage.asyncio, "sleep", new=AsyncMock(side_effect=cache_updated)) as sleep:
            self.assertEqual(await prepare_stage(self.guild, self.channel), 101)
        sleep.assert_awaited_once()
        self.member.edit.assert_awaited_once_with(suppress=False)

    async def test_missing_voice_cache_closes_created_stage_without_editing_member(self) -> None:
        self.member.voice = None
        with patch.object(autonomous_stage.asyncio, "sleep", new=AsyncMock()):
            with self.assertRaises(RuntimeError):
                await prepare_stage(self.guild, self.channel)
        self.instance.delete.assert_awaited_once()
        self.member.edit.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
