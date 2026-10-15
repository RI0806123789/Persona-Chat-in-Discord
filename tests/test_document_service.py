"""文書処理の異常系でも一時ファイルを残さないことを検証する。"""

import asyncio
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from background import spawn
import document_service
from document_service import DocumentService


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


if __name__ == "__main__":
    unittest.main()
