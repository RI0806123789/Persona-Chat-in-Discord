"""共通化した送信・権限・マスク処理と、調整定数の反映を検証する。"""

import asyncio
import io
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord
from persona_bot.services import auto_topic_service
from persona_bot.services.auto_topic_service import AutoTopicService
from persona_bot.services.autonomous_service import AutonomousService
from persona_bot.core import commands
from persona_bot.services import content_moderator
from persona_bot.utils.discord_helpers import is_administrator, send_long_message, send_long_reply, split_message
from persona_bot.views import functions_view
from persona_bot.views import help_view
from persona_bot.core.message_handler import MessageHandler
from persona_bot.services import ng_word_service
from persona_bot.services.ng_word_service import NgWordFilter
from persona_bot.services import reminder_service
from persona_bot.services.reminder_service import ReminderService
from persona_bot.views import reminder_view
from persona_bot.views import stats_view
from persona_bot.views import summary_view
from persona_bot.views import view_parts


def make_filter() -> NgWordFilter:
    # 実際の公開・非公開NGワードファイルは読まない。
    with patch.object(ng_word_service, "load_ng_words", return_value=["架空NG"]):
        return NgWordFilter()


def make_interaction(*, user=None):
    return SimpleNamespace(
        user=user,
        response=SimpleNamespace(
            defer=AsyncMock(), send_message=AsyncMock(), edit_message=AsyncMock(),
            is_done=Mock(return_value=False),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(), original_response=AsyncMock(),
    )


def make_member(*, admin: bool):
    member = Mock(spec=discord.Member, id=11)
    member.guild_permissions = SimpleNamespace(administrator=admin)
    return member


class LongMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_message_propagates_options_to_every_chunk(self) -> None:
        channel = SimpleNamespace(send=AsyncMock())
        mentions = discord.AllowedMentions.none()
        await send_long_message(channel, "あ" * 4001, suppress_embeds=True, allowed_mentions=mentions)
        self.assertEqual([len(call.args[0]) for call in channel.send.call_args_list], [2000, 2000, 1])
        for call in channel.send.call_args_list:
            self.assertEqual(call.kwargs, {"suppress_embeds": True, "allowed_mentions": mentions})

    async def test_reply_returns_first_message_and_sends_remaining_chunks_in_order(self) -> None:
        first = object()
        message = SimpleNamespace(reply=AsyncMock(return_value=first), channel=SimpleNamespace(send=AsyncMock()))
        result = await send_long_reply(message, "あ" * 4001, suppress_embeds=True)
        self.assertIs(result, first)
        message.reply.assert_awaited_once_with("あ" * 2000, suppress_embeds=True)
        self.assertEqual([len(call.args[0]) for call in message.channel.send.call_args_list], [2000, 1])

    async def test_topic_and_anniversary_restrict_mentions_in_every_chunk(self) -> None:
        text = "あ" * 4001
        channel = SimpleNamespace(
            id=10, name="架空のチャンネル", send=AsyncMock(), guild=SimpleNamespace(me=object()),
            permissions_for=Mock(return_value=SimpleNamespace(view_channel=True, send_messages=True)),
        )
        settings = SimpleNamespace(current_prompt_file="架空のパス", current_model_name="架空モデル", channel_reset_points={})
        service = AutoTopicService(
            SimpleNamespace(user=None), None, None,
            SimpleNamespace(generate=AsyncMock(return_value=(text, None, []))), None, None,
            SimpleNamespace(log_snapshot=Mock()), None,
        )
        service._screen_auto_post = AsyncMock(return_value=(text, True))
        member = SimpleNamespace(id=11, display_name="架空の利用者", mention="<@11>")
        with patch.object(auto_topic_service, "load_prompt_template", return_value="架空のペルソナ"), \
                patch.object(auto_topic_service, "fetch_channel_history_text", new=AsyncMock(return_value="")), \
                patch.object(auto_topic_service, "send_long_message", wraps=send_long_message, create=True) as sending:
            self.assertTrue(await service._post_topic(channel, settings))
            self.assertTrue(await service._post_anniversary(channel, settings, member, "誕生日"))
        self.assertEqual([call.args[1] for call in sending.call_args_list], [text, f"<@11> {text}"])
        for call in channel.send.call_args_list[:3]:
            self.assertEqual(call.kwargs["allowed_mentions"].to_dict(), {"parse": []})
        for call in channel.send.call_args_list[3:]:
            self.assertEqual(call.kwargs["allowed_mentions"].to_dict(), {"users": [11], "parse": []})

    async def test_reminder_keeps_mention_next_date_and_chunk_limits(self) -> None:
        channel = SimpleNamespace(send=AsyncMock())
        service = ReminderService(SimpleNamespace(get_channel=Mock(return_value=channel)), None, make_filter())
        with patch.object(reminder_service, "send_long_message", wraps=send_long_message) as sending:
            await service._notify({
                "channel_id": 10, "user_id": "11", "message": "あ" * 4001,
                "next_remind_at": "2026-01-02T01:00:00+00:00",
            })
        sending.assert_awaited_once()
        body = sending.call_args.args[1]
        self.assertIn("⏰ <@11>", body)
        self.assertIn("🔁 次回は", body)
        self.assertTrue(all(len(call.args[0]) <= 2000 for call in channel.send.call_args_list))
        self.assertTrue(all(call.kwargs["allowed_mentions"] is None for call in channel.send.call_args_list))

    async def test_summary_keeps_public_channel_and_private_followup_paths_separate(self) -> None:
        text = "あ" * 4001
        for private in (False, True):
            with self.subTest(private=private):
                view = discord.ui.View()
                self.addCleanup(view.stop)
                for key, value in {
                    "temp_private": private, "temp_period": 1, "guild_settings": None,
                    "gemini": None, "moderator": None, "ng_filter": None, "usage": None,
                }.items():
                    setattr(view, key, value)
                button = summary_view.RunSummaryButton()
                view.add_item(button)
                interaction = make_interaction()
                interaction.channel = Mock(spec=discord.TextChannel, send=AsyncMock())
                interaction.client = SimpleNamespace(user=None)
                with patch.object(summary_view, "generate_channel_summary", new=AsyncMock(return_value=(True, text))), \
                        patch.object(summary_view, "send_long_message", wraps=send_long_message) as sending:
                    await button.callback(interaction)
                if private:
                    sending.assert_not_awaited()
                    interaction.channel.send.assert_not_awaited()
                    interaction.edit_original_response.assert_awaited_once_with(content=text[:2000])
                    self.assertEqual([len(call.args[0]) for call in interaction.followup.send.call_args_list], [2000, 1])
                    self.assertTrue(all(call.kwargs == {"ephemeral": True} for call in interaction.followup.send.call_args_list))
                else:
                    sending.assert_awaited_once_with(interaction.channel, text)
                    self.assertEqual(interaction.channel.send.await_count, 3)
                    interaction.followup.send.assert_not_awaited()


class GraphSendingTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_response_and_deferred_followup_both_close_buffer(self) -> None:
        for followup in (False, True):
            with self.subTest(followup=followup):
                interaction = make_interaction()
                buffer = io.BytesIO(b"fictional-png")
                options = {"followup": True} if followup else {}
                await view_parts.send_graph_ephemeral(interaction, buffer, "fictional.png", "架空のグラフ", **options)
                sender = interaction.followup.send if followup else interaction.response.send_message
                other = interaction.response.send_message if followup else interaction.followup.send
                sender.assert_awaited_once()
                self.assertEqual(sender.call_args.args, ("架空のグラフ",))
                self.assertTrue(sender.call_args.kwargs["ephemeral"])
                self.assertEqual(sender.call_args.kwargs["file"].filename, "fictional.png")
                other.assert_not_awaited()
                self.assertTrue(buffer.closed)

    async def test_send_failure_keeps_contextual_notice_and_closes_buffer(self) -> None:
        for followup in (False, True):
            with self.subTest(followup=followup):
                interaction = make_interaction()
                sender = interaction.followup.send if followup else interaction.response.send_message
                sender.side_effect = [OSError("架空の送信失敗"), None]
                buffer = io.BytesIO(b"fictional-png")
                with patch("builtins.print") as logging:
                    await view_parts.send_graph_ephemeral(
                        interaction, buffer, "fictional.png", "架空のグラフ", followup=followup,
                        error_log="架空の文脈", error_notice="架空のエラー案内",
                    )
                logging.assert_called_once_with("架空の文脈: 架空の送信失敗")
                self.assertEqual(sender.await_args.args, ("架空のエラー案内",))
                self.assertEqual(sender.await_args.kwargs, {"ephemeral": True})
                self.assertTrue(buffer.closed)

    async def test_file_construction_failure_still_closes_buffer(self) -> None:
        buffer = io.BytesIO(b"fictional-png")
        interaction = make_interaction()
        with patch.object(view_parts.discord, "File", side_effect=OSError("架空の画像失敗")):
            await view_parts.send_graph_ephemeral(interaction, buffer, "fictional.png", "架空")
        self.assertTrue(buffer.closed)
        interaction.response.send_message.assert_awaited_once()
        self.assertIn("架空の画像失敗", interaction.response.send_message.call_args.args[0])

    async def test_cancellation_propagates_after_closing_buffer(self) -> None:
        interaction = make_interaction()
        interaction.followup.send.side_effect = asyncio.CancelledError()
        buffer = io.BytesIO(b"fictional-png")
        with self.assertRaises(asyncio.CancelledError):
            await view_parts.send_graph_ephemeral(interaction, buffer, "fictional.png", "架空", followup=True)
        self.assertTrue(buffer.closed)
        interaction.followup.send.assert_awaited_once()

    async def test_both_buttons_use_helper_and_keep_error_text(self) -> None:
        for module, button_class, notice in (
            (functions_view, functions_view.GraphButton, "エラー: グラフの送信に失敗しました。"),
            (stats_view, stats_view.RankingButton, "エラー: ランキングの送信に失敗しました。"),
        ):
            for failed in (False, True):
                with self.subTest(button=button_class.__name__, failed=failed):
                    buffer = io.BytesIO(b"fictional-png")
                    view = discord.ui.View()
                    self.addCleanup(view.stop)
                    view.usage = SimpleNamespace(has_data=Mock(return_value=True), build_graph_buffer=AsyncMock(return_value=buffer))
                    view.stats = SimpleNamespace(build_ranking_graph=AsyncMock(return_value=buffer))
                    button = button_class()
                    view.add_item(button)
                    interaction = make_interaction(user=make_member(admin=True))
                    if failed:
                        interaction.followup.send.side_effect = [OSError("架空の送信失敗"), None]
                    with patch.object(module, "send_graph_ephemeral", wraps=view_parts.send_graph_ephemeral) as sending:
                        await button.callback(interaction)
                    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
                    interaction.response.send_message.assert_not_awaited()
                    sending.assert_awaited_once()
                    self.assertTrue(sending.call_args.kwargs["followup"])
                    self.assertTrue(buffer.closed)
                    if failed:
                        self.assertEqual(interaction.followup.send.await_args.args, (notice,))


class SharedMaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_output_paths_share_masking_and_keep_log_labels(self) -> None:
        ng_filter = make_filter()
        moderator = SimpleNamespace(check=AsyncMock(return_value=(True, "")))
        auto = AutoTopicService(None, None, None, None, moderator, ng_filter, None, None)
        autonomous = AutonomousService.__new__(AutonomousService)
        autonomous._ng_filter = ng_filter
        channel = SimpleNamespace(send=AsyncMock())
        reminder = ReminderService(SimpleNamespace(get_channel=Mock(return_value=channel)), None, ng_filter)
        with patch.object(ng_filter, "mask_with_log", wraps=ng_filter.mask_with_log) as masking, patch("builtins.print") as logging:
            text = await content_moderator.screen_bot_response("架空NGの本文", ng_filter, moderator)
            automatic, _ = await auto._screen_auto_post("架空NGの本文[V:1, A:2]", "自発的な会話")
            speech = autonomous._screen_speech("架空NGの本文[V:1, A:2]", "自律行動の独り言")
            await reminder._notify({"channel_id": 10, "user_id": "11", "message": "架空NGの本文"})
        self.assertEqual((text, automatic, speech), ("●●●●の本文",) * 3)
        self.assertNotIn("架空NG", channel.send.call_args.args[0])
        labels = [call.kwargs["label"] for call in masking.call_args_list]
        self.assertEqual(labels, ["Bot応答", "自発的な会話", "自律行動の独り言", "リマインダー"])
        self.assertEqual([call.args[0] for call in logging.call_args_list],
                         [f"NGワード検出 ({label}): ['架空NG']" for label in labels])
        self.assertTrue(all(call.args == ("●●●●の本文",) for call in moderator.check.call_args_list))

    async def test_safe_text_is_unchanged_and_does_not_log(self) -> None:
        ng_filter = make_filter()
        with patch("builtins.print") as logging:
            self.assertEqual(ng_filter.mask_with_log("安全な本文", label="架空"), "安全な本文")
        logging.assert_not_called()


class AdministratorAndConstantsTests(unittest.IsolatedAsyncioTestCase):
    async def test_administrator_requires_a_member_with_server_permission(self) -> None:
        self.assertTrue(is_administrator(make_member(admin=True)))
        self.assertFalse(is_administrator(make_member(admin=False)))
        self.assertFalse(is_administrator(Mock(spec=discord.User)))
        self.assertFalse(is_administrator(SimpleNamespace(guild_permissions=SimpleNamespace(administrator=True))))

    async def test_ranking_and_unblock_recheck_permission_at_button_press(self) -> None:
        for module, button_class in ((functions_view, functions_view.UnblockButton), (stats_view, stats_view.RankingButton)):
            for user in (make_member(admin=False), Mock(spec=discord.User)):
                with self.subTest(button=button_class.__name__, user=type(user).__name__):
                    view = discord.ui.View()
                    self.addCleanup(view.stop)
                    view.blacklist = SimpleNamespace(get_all_blocked_users=Mock())
                    view.stats = SimpleNamespace(build_ranking_graph=AsyncMock())
                    button = button_class()
                    view.add_item(button)
                    interaction = make_interaction(user=user)
                    with patch.object(module, "is_administrator", wraps=is_administrator) as check:
                        await button.callback(interaction)
                    check.assert_called_once_with(user)
                    interaction.response.defer.assert_not_awaited()
                    self.assertIn("管理者のみ", interaction.response.send_message.call_args.args[0])
                    view.blacklist.get_all_blocked_users.assert_not_called()
                    view.stats.build_ranking_graph.assert_not_awaited()

    async def test_functions_command_and_stats_panel_share_administrator_policy(self) -> None:
        callbacks = {}
        tree = SimpleNamespace(command=lambda **kwargs: lambda function: callbacks.setdefault(kwargs["name"], function))
        state = SimpleNamespace(get_guild=Mock(return_value=object()))
        commands.register_commands(tree, state, *([None] * 8))
        user = make_member(admin=True)
        interaction = make_interaction(user=user)
        interaction.guild_id = 1
        panel = SimpleNamespace(build_preview=Mock(return_value="架空"))
        with patch.object(commands, "FunctionsView", return_value=panel) as factory, \
                patch.object(commands, "is_administrator", wraps=is_administrator) as check:
            await callbacks["functions"](interaction)
        check.assert_called_once_with(user)
        self.assertTrue(factory.call_args.kwargs["is_admin"])
        view = discord.ui.View()
        self.addCleanup(view.stop)
        view.stats = SimpleNamespace(has_data=Mock(return_value=True))
        view.clone = Mock()
        button = functions_view.StatsButton()
        view.add_item(button)
        with patch.object(functions_view, "StatsView", return_value=panel) as factory, \
                patch.object(functions_view, "swap_panel", new=AsyncMock()), \
                patch.object(functions_view, "is_administrator", wraps=is_administrator) as check:
            await button.callback(interaction)
        check.assert_called_once_with(user)
        self.assertTrue(factory.call_args.kwargs["is_admin"])

    async def test_select_limits_and_labels_follow_constants(self) -> None:
        for module in (functions_view, reminder_view, stats_view):
            with self.subTest(module=module.__name__), \
                    patch.object(module, "DISCORD_SELECT_MAX_OPTIONS", 2), \
                    patch.object(module, "DISCORD_SELECT_LABEL_MAX_LENGTH", 4):
                if module is functions_view:
                    select = module.UnblockSelect([{"user_id": i, "user_name": "あ" * 110} for i in range(3)])
                elif module is reminder_view:
                    select = module.ReminderCancelSelect(
                        [{"reminder_id": i, "message": "あ" * 110} for i in range(3)],
                        SimpleNamespace(describe=Mock(return_value="い" * 110)),
                    )
                    self.assertTrue(all(len(option.description) == 4 for option in select.options))
                else:
                    select = module.StatsUserSelect([(str(i), "あ" * 110, 1) for i in range(3)], "0")
                self.assertEqual(len(select.options), 2)
                self.assertTrue(all(len(option.label) == 4 for option in select.options))

    async def test_moderation_sampling_and_notice_use_the_same_limit(self) -> None:
        with patch.object(content_moderator, "MODERATION_NG_WORD_SAMPLE_LIMIT", 2):
            text = content_moderator._build_prompt("架空の質問", "Bot応答", ["語句A", "語句B", "語句C"])
            self.assertIn("語句A, 語句B", text)
            self.assertNotIn("語句C", text)
            self.assertIn("他にもあります", text)
            text = content_moderator._build_prompt("架空", "Bot応答", ["語句A", "語句B"])
            self.assertNotIn("他にもあります", text)

    async def test_help_and_block_notice_follow_configured_threshold(self) -> None:
        with patch.object(help_view, "BLACKLIST_MAX_VIOLATIONS", 5), patch.object(help_view, "VIEW_TIMEOUT_SECONDS", 30):
            text = help_view.build_help_text("架空のボット")
        self.assertIn("5回でボットが応答しなくなります", text)
        self.assertIn("30秒で時間切れ", text)
        handler = MessageHandler.__new__(MessageHandler)
        handler.blacklist = SimpleNamespace(record_violation=AsyncMock(return_value=(5, True)))
        message = SimpleNamespace(author=SimpleNamespace(id=11), reply=AsyncMock())
        with patch("persona_bot.core.message_handler.BLACKLIST_MAX_VIOLATIONS", 5):
            await handler.notify_violation(message, "架空の警告")
        self.assertIn("違反が5回に達したため", message.reply.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
