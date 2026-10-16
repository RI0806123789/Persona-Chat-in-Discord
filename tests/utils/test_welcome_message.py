"""サーバー参加時の案内が WELCOME_MESSAGE_ENABLED に従うことを、架空のサーバーで検証する。"""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.utils import discord_helpers
from persona_bot.utils.discord_helpers import post_welcome_message


def make_guild() -> SimpleNamespace:
    channel = SimpleNamespace(
        id=2,
        permissions_for=lambda member: SimpleNamespace(send_messages=True),
        send=AsyncMock(),
    )
    return SimpleNamespace(id=1, me=object(), system_channel=channel, text_channels=[channel])


class WelcomeMessageSettingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        quiet = patch("builtins.print")
        quiet.start()
        self.addCleanup(quiet.stop)

    async def test_disabled_setting_skips_everything(self) -> None:
        # #74: 無効なら送信せず、投稿先も探さず、投稿済みにも数えない。
        guild = make_guild()
        posted: set[int] = set()
        with patch.object(discord_helpers, "WELCOME_MESSAGE_ENABLED", False), \
                patch.object(discord_helpers, "pick_announcement_channel") as pick:
            self.assertFalse(await post_welcome_message(guild, "架空の案内", posted))
        pick.assert_not_called()
        guild.system_channel.send.assert_not_awaited()
        self.assertEqual(posted, set())

    async def test_enabled_setting_posts_once_without_mentions(self) -> None:
        guild = make_guild()
        posted: set[int] = set()
        with patch.object(discord_helpers, "WELCOME_MESSAGE_ENABLED", True):
            self.assertTrue(await post_welcome_message(guild, "架空の案内", posted))
            # 同じサーバーへの2回目（イベントの重複）は投稿しない。
            self.assertFalse(await post_welcome_message(guild, "架空の案内", posted))
        guild.system_channel.send.assert_awaited_once()
        args, kwargs = guild.system_channel.send.call_args
        self.assertEqual(args, ("架空の案内",))
        mentions = kwargs["allowed_mentions"]
        self.assertIsInstance(mentions, discord.AllowedMentions)
        self.assertEqual(mentions.to_dict(), discord.AllowedMentions.none().to_dict())
        self.assertEqual(posted, {1})


if __name__ == "__main__":
    unittest.main()
