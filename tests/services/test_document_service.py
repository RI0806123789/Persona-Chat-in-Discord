"""文書応答の感情タグ除去と、一時ファイルの回収、pypdf が無い環境での読み込みを検証する。"""

import asyncio
import importlib.util
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.core.background import spawn
from persona_bot.core.app_state import BotState
from persona_bot.services import document_service
from persona_bot.services.document_service import DocumentService


class DocumentTemporaryFileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        original = tempfile.NamedTemporaryFile
        factory = patch.object(
            document_service.tempfile,
            "NamedTemporaryFile",
            side_effect=lambda **kwargs: original(dir=self.root, **kwargs),
        )
        factory.start()
        self.addCleanup(factory.stop)
        self.uploaded = SimpleNamespace(name="fictional-upload")
        self.gemini = SimpleNamespace(
            upload_file=AsyncMock(return_value=self.uploaded),
            summarize_document=AsyncMock(return_value={"summary": "架空の要約"}),
            delete_file=AsyncMock(),
        )
        self.memory = SimpleNamespace(store_document_summary=AsyncMock())
        self.service = DocumentService(None, None, self.memory, self.gemini, None, None)
        self.attachment = SimpleNamespace(
            filename="fictional.txt", size=10, read=AsyncMock(return_value=b"fictional text")
        )

    async def process(self) -> None:
        await self.service._process_attachment(self.attachment, "test-user", "test-model")

    def assert_no_files(self) -> None:
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_read_failure_removes_temporary_file(self) -> None:
        self.attachment.read.side_effect = OSError("架空の読み込み失敗")
        with self.assertRaisesRegex(OSError, "読み込み失敗"):
            await self.process()
        self.assert_no_files()
        self.gemini.upload_file.assert_not_awaited()

    async def test_actual_download_size_is_checked_and_temporary_file_is_removed(self) -> None:
        self.attachment.read.return_value = b"12345"
        self.attachment.size = 1
        with patch.object(document_service, "MAX_DOCUMENT_SIZE_BYTES", 4), patch.object(Path, "write_bytes") as write:
            with self.assertRaisesRegex(ValueError, "上限"):
                await self.process()
        self.assert_no_files()
        write.assert_not_called()
        self.gemini.upload_file.assert_not_awaited()

    async def test_actual_download_at_size_limit_is_accepted(self) -> None:
        self.attachment.read.return_value = b"1234"
        self.attachment.size = 1
        with patch.object(document_service, "MAX_DOCUMENT_SIZE_BYTES", 4):
            await self.process()
        self.assert_no_files()
        self.gemini.upload_file.assert_awaited_once()

    async def test_partial_write_failure_removes_temporary_file(self) -> None:
        original = Path.write_bytes

        def fail_write(path, data):
            original(path, data[:2])
            raise OSError("架空の書き込み失敗")

        with patch.object(Path, "write_bytes", fail_write):
            with self.assertRaisesRegex(OSError, "書き込み失敗"):
                await self.process()
        self.assert_no_files()
        self.gemini.upload_file.assert_not_awaited()

    async def test_read_cancellation_removes_temporary_file(self) -> None:
        self.attachment.read.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.process()
        self.assert_no_files()

    async def check_write_cancellation(self, cancel_count: int) -> None:
        original = Path.write_bytes
        started = asyncio.Event()
        release = threading.Event()
        finished = asyncio.Event()
        loop = asyncio.get_running_loop()

        def delayed_write(path, data):
            loop.call_soon_threadsafe(started.set)
            try:
                if not release.wait(timeout=5):
                    raise TimeoutError("テストの書き込み待機が終了しませんでした")
                return original(path, data)
            finally:
                loop.call_soon_threadsafe(finished.set)

        with patch.object(Path, "write_bytes", delayed_write):
            task = spawn(self.process(), label="一時ファイルのキャンセル検証")
            try:
                await asyncio.wait_for(started.wait(), timeout=5)
                for _ in range(cancel_count):
                    task.cancel()
                    await asyncio.sleep(0)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=5)
                await asyncio.wait_for(finished.wait(), timeout=5)
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assert_no_files()
        self.gemini.upload_file.assert_not_awaited()

    async def test_write_cancellation_does_not_recreate_file(self) -> None:
        await self.check_write_cancellation(1)

    async def test_repeated_write_cancellation_does_not_recreate_file(self) -> None:
        await self.check_write_cancellation(2)

    async def test_success_passes_file_to_upload_and_removes_it(self) -> None:
        async def upload(path):
            self.assertEqual(path.read_bytes(), b"fictional text")
            return self.uploaded

        self.gemini.upload_file.side_effect = upload
        await self.process()
        self.assert_no_files()
        self.memory.store_document_summary.assert_awaited_once_with(
            "test-user", {"summary": "架空の要約"}
        )
        self.gemini.delete_file.assert_awaited_once_with(self.uploaded)

    async def test_validation_failure_removes_temporary_file(self) -> None:
        self.attachment.read.return_value = b"\xff"
        with self.assertRaises(UnicodeDecodeError):
            await self.process()
        self.assert_no_files()
        self.gemini.upload_file.assert_not_awaited()

    async def test_upload_failure_removes_temporary_file(self) -> None:
        self.gemini.upload_file.side_effect = OSError("架空のアップロード失敗")
        with self.assertRaisesRegex(OSError, "アップロード失敗"):
            await self.process()
        self.assert_no_files()

    async def test_summary_failure_removes_remote_and_local_files(self) -> None:
        self.gemini.summarize_document.side_effect = OSError("架空の要約失敗")
        with self.assertRaisesRegex(OSError, "要約失敗"):
            await self.process()
        self.assert_no_files()
        self.gemini.delete_file.assert_awaited_once_with(self.uploaded)

    async def test_remote_cleanup_failure_still_removes_local_file(self) -> None:
        self.gemini.delete_file.side_effect = OSError("架空のアップロード先削除失敗")
        with self.assertRaisesRegex(OSError, "アップロード先削除失敗"):
            await self.process()
        self.assert_no_files()

    async def test_repeated_cancellation_waits_for_remote_cleanup_before_local_removal(self) -> None:
        summarizing, deleting, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def summarize(*args):
            summarizing.set()
            await asyncio.Event().wait()

        async def delete(*args):
            deleting.set()
            await release.wait()

        self.gemini.summarize_document.side_effect = summarize
        self.gemini.delete_file.side_effect = delete
        task = spawn(self.process(), label="文書削除中の再キャンセルの確認")
        try:
            await asyncio.wait_for(summarizing.wait(), 2)
            task.cancel()
            await asyncio.wait_for(deleting.wait(), 2)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertEqual(len(list(self.root.iterdir())), 1)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        self.gemini.delete_file.assert_awaited_once_with(self.uploaded)
        self.assert_no_files()

    async def test_pdf_validation_cancellation_waits_for_stream_to_close_before_removal(self) -> None:
        self.attachment.filename = "fictional.pdf"
        entered = asyncio.Event()
        release = threading.Event()
        streams = []
        loop = asyncio.get_running_loop()

        def read_pdf(stream):
            streams.append(stream)
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(timeout=5):
                raise TimeoutError("PDF検証の待機が終了しませんでした")
            return SimpleNamespace(is_encrypted=False)

        with patch.dict(sys.modules, {"pypdf": SimpleNamespace(PdfReader=read_pdf)}):
            task = spawn(self.process(), label="PDF検証キャンセルの確認")
            try:
                await asyncio.wait_for(entered.wait(), 2)
                for _ in range(2):
                    task.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(task.done())
                    self.assertFalse(streams[0].closed)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(streams[0].closed)
        self.assert_no_files()


class DocumentResponseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.memory = SimpleNamespace(
            route_category=AsyncMock(return_value="none"),
            build_contexts=AsyncMock(return_value=("", "")),
            append_pending_log=AsyncMock(),
            flush_if_needed=AsyncMock(),
        )
        self.gemini = SimpleNamespace(generate=AsyncMock())
        self.moderator = SimpleNamespace(check=AsyncMock(return_value=(True, "")))
        self.usage = SimpleNamespace(log_snapshot=Mock())
        self.service = DocumentService(
            SimpleNamespace(user=SimpleNamespace(id=99)),
            BotState(), self.memory, self.gemini, self.usage, None,
            moderator=self.moderator,
        )
        self.message = SimpleNamespace(
            content="文書を要約してください。",
            guild=SimpleNamespace(id=10),
            channel=SimpleNamespace(id=20),
            attachments=[SimpleNamespace(filename="fictional.txt")],
            reply=AsyncMock(),
        )
        self.service._fetch_message_from_task = AsyncMock(return_value=self.message)
        self.service._process_attachment = AsyncMock()
        self.service._update_task = AsyncMock()
        self.sent = AsyncMock()
        self.tasks = []

        def start(coro, **kwargs):
            task = spawn(coro, **kwargs)
            self.tasks.append(task)
            return task

        for name, value in (
            ("send_long_reply", self.sent),
            ("fetch_channel_history_text", AsyncMock(return_value="架空の履歴")),
            ("load_persona_template", AsyncMock(return_value="架空のペルソナ")),
            ("spawn", start),
        ):
            replacement = patch.object(document_service, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)

    async def asyncTearDown(self) -> None:
        await asyncio.gather(*self.tasks)

    async def process_response(self, text: str, sources=None) -> None:
        self.gemini.generate.return_value = (text, None, sources or [])
        await self.service._process_task({"task_id": "fictional-task", "author_id": "test-user"})

    def assert_clean_response(self, expected: str, *, sources: bool = False) -> None:
        self.sent.assert_awaited_once()
        self.assertEqual(self.sent.await_args.args[0], self.message)
        reply = self.sent.await_args.args[1]
        self.assertNotIn("[V:", reply)
        self.assertEqual(self.sent.await_args.kwargs, {"suppress_embeds": sources})
        if not sources:
            self.assertEqual(reply, expected)
        self.memory.append_pending_log.assert_awaited_once_with(
            "test-user", "文書を要約してください。", expected
        )
        self.assertEqual(self.service._update_task.await_args.kwargs["status"], "completed")
        self.message.reply.assert_not_awaited()

    async def test_affinity_tag_is_removed_before_screening_and_storage(self) -> None:
        await self.process_response("要約です。[V:1, A:2]")
        self.assert_clean_response("要約です。")
        self.moderator.check.assert_awaited_once_with("要約です。", direction="Bot応答")
        self.assertEqual(self.usage.log_snapshot.call_args.args[4], "要約です。")

    async def test_multiple_tags_with_spaces_are_removed(self) -> None:
        await self.process_response("[V: +1, A: -2]要約です。[V: -3, A: +4]")
        self.assert_clean_response("要約です。")

    async def test_response_without_tags_is_preserved(self) -> None:
        await self.process_response("**要点**\n- 架空の文書の内容です。")
        self.assert_clean_response("**要点**\n- 架空の文書の内容です。")

    async def test_reference_links_are_sent_but_not_saved_in_memory(self) -> None:
        await self.process_response(
            "要約です。[V:1, A:2]",
            [{"title": "参考資料", "uri": "https://example.com/reference"}],
        )
        self.assert_clean_response("要約です。", sources=True)
        self.assertEqual(
            self.sent.await_args.args[1],
            "要約です。\n\n🔍 **参考リンク:**\n・ [参考資料](<https://example.com/reference>)",
        )

    async def check_tag_only_response(self, sources=None) -> None:
        await self.process_response(" [V:1, A:2] ", sources)
        self.sent.assert_not_awaited()
        self.memory.append_pending_log.assert_not_awaited()
        self.moderator.check.assert_not_awaited()
        self.assertEqual(self.service._update_task.await_args.kwargs["status"], "failed")
        self.message.reply.assert_awaited_once()
        self.assertTrue(self.message.reply.await_args.args[0].strip())
        self.assertNotIn("[V:", self.message.reply.await_args.args[0])

    async def test_tag_only_response_reports_failure_without_sending_empty_text(self) -> None:
        await self.check_tag_only_response()

    async def test_tag_only_response_does_not_send_links_without_body(self) -> None:
        await self.check_tag_only_response([{"uri": "https://example.com/reference"}])

    async def test_moderation_replacement_is_still_sent_and_saved(self) -> None:
        self.moderator.check.return_value = (False, "架空の判定理由")
        await self.process_response("架空の応答。[V:1, A:2]")
        self.assert_clean_response("申し訳ありませんが、適切な応答を生成できませんでした。")


class OptionalPdfDependencyTests(unittest.TestCase):
    """pypdf が無くても文書処理を読み込め、PDF だけが理由の分かる形で失敗する（#57）。"""

    # sys.modules の値を None にすると、その名前の import は ModuleNotFoundError になる
    WITHOUT_PYPDF = {"pypdf": None, "pypdf.errors": None}

    def load_without_pypdf(self):
        # import 済みのモジュールを壊さないよう、同じソースから別名の複製を作って読み込む
        spec = importlib.util.spec_from_file_location("document_service_without_pypdf", document_service.__file__)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, self.WITHOUT_PYPDF):
            spec.loader.exec_module(module)
        return module

    def test_module_is_importable_without_pypdf(self) -> None:
        module = self.load_without_pypdf()
        self.assertTrue(callable(module.DocumentService))

    def test_pdf_validation_reports_missing_pypdf(self) -> None:
        module = self.load_without_pypdf()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fictional.pdf"
            path.write_bytes(b"%PDF-1.4 fictional")
            with patch.dict(sys.modules, self.WITHOUT_PYPDF):
                with self.assertRaisesRegex(ValueError, "pypdf"):
                    module.validate_pdf_file(path)

    def test_text_validation_does_not_need_pypdf(self) -> None:
        module = self.load_without_pypdf()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fictional.txt"
            path.write_text("架空の文書です。", encoding="utf-8")
            with patch.dict(sys.modules, self.WITHOUT_PYPDF):
                module.validate_text_file(path)


if __name__ == "__main__":
    unittest.main()
