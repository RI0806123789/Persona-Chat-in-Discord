"""入力の違反が3回でブロック・保存・一覧・再読込まで一致することを確かめる（#173）。

保存に失敗した違反は回数に数えず、「あと N 回」のような成功の通知も出さないこと、
ボット自身の応答のNG・不適切判定を利用者の違反として数えないことも確かめる。
"""

import asyncio
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.config.constants import BLACKLIST_MAX_VIOLATIONS
from persona_bot.services.content_moderator import ContentModerator
from tests.helpers.ng_pipeline import FICTIONAL_NG_WORD, NgPipeline
from persona_bot.utils.logging_setup import LOG_DATE_FORMAT, LOG_FORMAT, OperationalFormatter


LOG_FORMATTER = OperationalFormatter(LOG_FORMAT, LOG_DATE_FORMAT)


NG_TEXT = f"{FICTIONAL_NG_WORD}を含む架空の質問です。"


class ViolationRecordingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.pipeline = NgPipeline(self)

    async def send(self, text: str = NG_TEXT, *, user_id: int = 11):
        message = self.pipeline.message(text, mention=True, user_id=user_id)
        await self.pipeline.handler.on_message(message)
        return message

    def assert_blocked_everywhere(self, user_id: int = 11) -> None:
        self.assertTrue(self.pipeline.blacklist.is_blocked(user_id))
        self.assertEqual(
            [record["user_id"] for record in self.pipeline.blacklist.get_all_blocked_users()], [user_id],
        )
        reloaded = self.pipeline.reloaded_blacklist()
        self.assertTrue(reloaded.is_blocked(user_id))
        self.assertEqual(reloaded.get_violation_count(user_id), BLACKLIST_MAX_VIOLATIONS)

    async def test_three_local_ng_inputs_block_and_log_each_recorded_count(self) -> None:
        with self.assertLogs("persona_bot.core.message_handler", level="INFO") as logs:
            for _ in range(BLACKLIST_MAX_VIOLATIONS):
                await self.send()
        self.assert_blocked_everywhere()
        log = "\n".join(LOG_FORMATTER.format(record) for record in logs.records)
        # 検出ログだけでなく、記録できた回数もログで追える
        self.assertIn(f"1/{BLACKLIST_MAX_VIOLATIONS} 回目を記録しました", log)
        self.assertIn(f"2/{BLACKLIST_MAX_VIOLATIONS} 回目を記録しました", log)
        self.assertIn("ブラックリストに登録しました", log)
        self.assertIn("NGワード検出 (ユーザー入力): 1 件", log)
        self.assertIn("user_id=<ID>", log)
        self.assertNotIn(FICTIONAL_NG_WORD, log)

    async def test_three_ai_unsafe_inputs_block(self) -> None:
        self.pipeline.moderator.check_and_route.return_value = (False, "架空の理由", "none", None)
        replies = []
        for _ in range(BLACKLIST_MAX_VIOLATIONS):
            message = await self.send("架空の不適切な質問です。")
            replies.append(message.reply.call_args.args[0])
        self.assertTrue(all("不適切な内容" in reply for reply in replies[:-1]))
        self.assertIn("ブラックリストに登録されました", replies[-1])
        self.assert_blocked_everywhere()

    async def test_concurrent_ng_inputs_block_once_without_extra_warning(self) -> None:
        messages = [self.pipeline.message(NG_TEXT, mention=True) for _ in range(BLACKLIST_MAX_VIOLATIONS)]
        await asyncio.gather(*(self.pipeline.handler.on_message(message) for message in messages))
        self.assert_blocked_everywhere()
        replies = [m.reply.call_args.args[0] for m in messages if m.reply.await_count]
        self.assertEqual(sum("ブラックリストに登録されました" in reply for reply in replies), 1)
        self.assertFalse(any("あと0回" in reply for reply in replies))

    async def test_save_failure_is_not_reported_as_a_counted_violation(self) -> None:
        with patch.object(self.pipeline.blacklist, "_save", side_effect=PermissionError("架空の保存拒否")), \
                self.assertLogs("persona_bot.core.message_handler", level="INFO") as logs:
            messages = [await self.send() for _ in range(BLACKLIST_MAX_VIOLATIONS)]

        for message in messages:
            # 発言は処理しないことだけを伝え、事実と食い違う残り回数・登録の案内は出さない
            message.reply.assert_awaited_once()
            reply = message.reply.call_args.args[0]
            self.assertIn("NGワードが含まれている", reply)
            self.assertNotIn("あと", reply)
            self.assertNotIn("ブラックリストに登録されました", reply)
        log_text = "\n".join(LOG_FORMATTER.format(record) for record in logs.records)
        self.assertIn("保存に失敗したため、今回の違反は回数に数えていません", log_text)
        self.assertIn("PermissionError", log_text)
        self.assertIn("user_id=<ID>", log_text)
        self.assertNotIn("架空の保存拒否", log_text)
        self.assertFalse(self.pipeline.blacklist.is_blocked(11))
        self.assertEqual(self.pipeline.blacklist.get_violation_count(11), 0)
        self.assertFalse(self.pipeline.blacklist_path.exists())
        self.pipeline.gemini.generate_with_metadata.assert_not_awaited()

        # 保存が直った後は、保存できた分から数え直す（未保存の違反は持ち越さない）
        message = await self.send()
        self.assertIn(f"あと{BLACKLIST_MAX_VIOLATIONS - 1}回", message.reply.call_args.args[0])
        self.assertEqual(self.pipeline.reloaded_blacklist().get_violation_count(11), 1)

    async def test_bot_response_ng_or_unsafe_output_is_not_counted_against_user(self) -> None:
        self.pipeline.gemini.generate_with_metadata.return_value = (
            f"{FICTIONAL_NG_WORD}を含む架空の応答です。", None, [], "fictional-model",
        )
        self.pipeline.moderator.check.return_value = (False, "架空の応答の不適切判定")
        for _ in range(BLACKLIST_MAX_VIOLATIONS):
            await self.send("架空の普通の質問です。")
        self.assertEqual(self.pipeline.blacklist.get_violation_count(11), 0)
        self.assertFalse(self.pipeline.blacklist.is_blocked(11))
        sent_text = self.pipeline.send_long_reply.call_args.args[1]
        self.assertIn("適切な応答を生成できませんでした", sent_text)

    async def test_blocked_user_is_ignored_and_unblock_starts_counting_again(self) -> None:
        for _ in range(BLACKLIST_MAX_VIOLATIONS):
            await self.send()
        ignored = await self.send()
        ignored.reply.assert_not_awaited()
        self.assertEqual(self.pipeline.blacklist.get_violation_count(11), BLACKLIST_MAX_VIOLATIONS)

        self.assertTrue(await self.pipeline.blacklist.unblock(11))
        self.assertFalse(self.pipeline.reloaded_blacklist().is_blocked(11))
        again = await self.send()
        self.assertIn(f"あと{BLACKLIST_MAX_VIOLATIONS - 1}回", again.reply.call_args.args[0])
        self.assertEqual(self.pipeline.reloaded_blacklist().get_violation_count(11), 1)

    async def test_repeated_moderation_empty_responses_never_count_as_violations(self) -> None:
        self.pipeline.handler.moderator = ContentModerator(None, SimpleNamespace(words=[]))
        with patch("persona_bot.services.content_moderator.generate_content", AsyncMock(return_value=None)):
            for _ in range(BLACKLIST_MAX_VIOLATIONS + 1):
                await self.send("架空の安全な入力")
        self.assertEqual(self.pipeline.blacklist.get_violation_count(11), 0)
        self.assertFalse(self.pipeline.blacklist.is_blocked(11))
        self.assertFalse(self.pipeline.blacklist_path.exists())
        self.assertEqual(self.pipeline.send_long_reply.await_count, BLACKLIST_MAX_VIOLATIONS + 1)

    async def test_notice_failure_preserves_saved_violation_or_save_failure_result(self) -> None:
        for error_type, status in ((discord.NotFound, 404), (discord.Forbidden, 403), (discord.HTTPException, 400)):
            for case in ("warning", "block", "save_failure"):
                with self.subTest(error=error_type.__name__, case=case):
                    pipeline = NgPipeline(self)
                    if case == "block":
                        for _ in range(BLACKLIST_MAX_VIOLATIONS - 1):
                            await pipeline.blacklist.record_violation(11, "架空の利用者")
                    message = pipeline.message(NG_TEXT, mention=True)
                    message.reply.side_effect = error_type(SimpleNamespace(status=status, reason="架空の失敗"), "架空の失敗")
                    with patch.object(pipeline.blacklist, "_save", wraps=pipeline.blacklist._save,
                                      side_effect=OSError("架空の保存失敗") if case == "save_failure" else None), \
                            self.assertLogs("persona_bot.core.message_handler", level="INFO") as logs:
                        await pipeline.handler.on_message(message)
                    expected = {"warning": 1, "block": BLACKLIST_MAX_VIOLATIONS, "save_failure": 0}[case]
                    self.assertEqual(pipeline.blacklist.get_violation_count(11), expected)
                    self.assertEqual(pipeline.reloaded_blacklist().get_violation_count(11), expected)
                    self.assertIn("違反通知: 送信に失敗しました", "\n".join(LOG_FORMATTER.format(record) for record in logs.records))


if __name__ == "__main__":
    unittest.main()
