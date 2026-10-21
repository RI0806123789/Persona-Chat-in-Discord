"""メンション必須モードでも、接続中のボイスチャットのNGワードを判定する（#172）。

返信する条件（応答モードのゲート）と、ローカルNG判定の対象を分けたことを確かめる。
"""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord
from persona_bot.config.constants import BLACKLIST_MAX_VIOLATIONS
from tests.helpers.ng_pipeline import (
    FICTIONAL_NG_WORD, GUILD_ID, VOICE_CHANNEL_ID, NgPipeline, make_channel, make_voice_client,
)

NG_TEXT = f"これは{FICTIONAL_NG_WORD}を含む架空の発言です。"
SAFE_TEXT = "これは架空の普通の発言です。"


class VoiceChatNgGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.pipeline = NgPipeline(self)

    async def send(self, message) -> None:
        await self.pipeline.handler.on_message(message)

    def assert_no_ai_or_reply(self, message) -> None:
        self.pipeline.moderator.check_and_route.assert_not_awaited()
        self.pipeline.gemini.generate_with_metadata.assert_not_awaited()
        message.reply.assert_not_awaited()

    async def test_unaddressed_ng_in_connected_voice_chat_blocks_on_third_violation(self) -> None:
        replies = []
        for _ in range(BLACKLIST_MAX_VIOLATIONS):
            message = self.pipeline.message(NG_TEXT)
            await self.send(message)
            message.reply.assert_awaited_once()
            replies.append(message.reply.call_args.args[0])

        self.assertIn("あと2回", replies[0])
        self.assertIn("あと1回", replies[1])
        self.assertIn("ブラックリストに登録されました", replies[2])
        self.assertTrue(self.pipeline.blacklist.is_blocked(11))
        self.assertEqual(len(self.pipeline.blacklist.get_all_blocked_users()), 1)
        self.assertTrue(self.pipeline.reloaded_blacklist().is_blocked(11))
        # 宛先でない発言のために Gemini を呼ばず、通常の返信もしない
        self.pipeline.moderator.check_and_route.assert_not_awaited()
        self.pipeline.gemini.generate_with_metadata.assert_not_awaited()

    async def test_safe_unaddressed_message_adds_no_ai_call_or_reply(self) -> None:
        message = self.pipeline.message(SAFE_TEXT)
        await self.send(message)
        self.assert_no_ai_or_reply(message)
        self.assertEqual(self.pipeline.blacklist.get_violation_count(11), 0)
        # 活動の観測は従来どおり行う
        self.pipeline.auto_topic.mark_activity.assert_called_once_with(VOICE_CHANNEL_ID, GUILD_ID)
        self.pipeline.autonomous.mark_activity.assert_called_once_with(VOICE_CHANNEL_ID, GUILD_ID)

    async def test_stage_chat_of_the_connected_stage_is_checked(self) -> None:
        message = self.pipeline.message(NG_TEXT, channel=make_channel(discord.StageChannel, VOICE_CHANNEL_ID))
        await self.send(message)
        self.assertEqual(self.pipeline.blacklist.get_violation_count(11), 1)
        self.pipeline.moderator.check_and_route.assert_not_awaited()

    async def test_unaddressed_ng_outside_the_connected_voice_chat_is_not_checked(self) -> None:
        cases = {
            "通常のテキストチャンネル": (make_channel(discord.TextChannel, 30), make_voice_client(VOICE_CHANNEL_ID)),
            "未接続のボイスチャンネル": (make_channel(discord.VoiceChannel, VOICE_CHANNEL_ID), None),
            "切れた接続": (
                make_channel(discord.VoiceChannel, VOICE_CHANNEL_ID),
                make_voice_client(VOICE_CHANNEL_ID, connected=False),
            ),
            "別のボイスチャンネルに接続中": (
                make_channel(discord.VoiceChannel, VOICE_CHANNEL_ID), make_voice_client(21),
            ),
        }
        for name, (channel, voice_client) in cases.items():
            with self.subTest(case=name):
                self.pipeline.guild.voice_client = voice_client
                message = self.pipeline.message(NG_TEXT, channel=channel)
                await self.send(message)
                self.assert_no_ai_or_reply(message)
                self.assertEqual(self.pipeline.blacklist.get_violation_count(11), 0)

    async def test_checked_range_follows_the_bot_after_it_is_moved(self) -> None:
        # ボットが別の通話（21）へ移された後は、移動前のチャットではなく移動後のチャットを見る
        self.pipeline.guild.voice_client = make_voice_client(21)
        old_chat = self.pipeline.message(NG_TEXT, channel=make_channel(discord.VoiceChannel, VOICE_CHANNEL_ID))
        await self.send(old_chat)
        old_chat.reply.assert_not_awaited()

        new_chat = self.pipeline.message(NG_TEXT, channel=make_channel(discord.VoiceChannel, 21))
        await self.send(new_chat)
        new_chat.reply.assert_awaited_once()
        self.assertEqual(self.pipeline.blacklist.get_violation_count(11), 1)

    async def test_existing_addressed_paths_still_record_violations(self) -> None:
        cases = {
            "メンションあり": dict(mention=True),
            "全発言モード": dict(mode="all"),
            "自律配信のコメント": dict(comment=True),
        }
        for user_id, (name, case) in enumerate(cases.items(), start=100):
            with self.subTest(case=name):
                self.pipeline.guild_settings.current_respond_mode = case.get("mode", "mention")
                self.pipeline.autonomous.is_comment_channel.return_value = case.get("comment", False)
                message = self.pipeline.message(NG_TEXT, mention=case.get("mention", False), user_id=user_id)
                await self.send(message)
                self.assertEqual(self.pipeline.blacklist.get_violation_count(user_id), 1)
                self.assertIn("メッセージを処理できません", message.reply.call_args.args[0])
                # ローカルNGで拒否した入力は、配信コメントの受付枠も消費しない
                self.pipeline.autonomous.claim_comment_slot.assert_not_called()
                self.pipeline.moderator.check_and_route.assert_not_awaited()

    async def test_blocked_user_in_voice_chat_is_still_ignored_before_activity(self) -> None:
        for _ in range(BLACKLIST_MAX_VIOLATIONS):
            await self.send(self.pipeline.message(NG_TEXT))
        self.pipeline.auto_topic.mark_activity.reset_mock()
        self.pipeline.autonomous.mark_activity.reset_mock()

        message = self.pipeline.message(NG_TEXT)
        await self.send(message)
        message.reply.assert_not_awaited()
        self.pipeline.auto_topic.mark_activity.assert_not_called()
        self.pipeline.autonomous.mark_activity.assert_not_called()

    async def test_dm_keeps_the_normal_path(self) -> None:
        dm_channel = make_channel(discord.DMChannel, 40)
        message = self.pipeline.message(NG_TEXT, channel=dm_channel)
        message.guild = None
        await self.send(message)
        self.assertEqual(self.pipeline.blacklist.get_violation_count(11), 1)
        self.assertIn("メッセージを処理できません", message.reply.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
