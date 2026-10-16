"""画像の容量制限・ワーカーでの展開と中断時の回収を確認する。"""

import asyncio
import io
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from PIL import Image

from persona_bot.core.background import spawn
from persona_bot.utils import discord_helpers
from tests.helpers.thread_probe import make_thread_probe


class ImageAttachmentTests(unittest.IsolatedAsyncioTestCase):
    def attachment(self, data=b"fictional", *, size=None):
        return SimpleNamespace(
            filename="fictional.png", content_type="image/png",
            size=len(data) if size is None else size, read=AsyncMock(return_value=data),
        )

    async def test_declared_oversize_is_not_downloaded(self):
        attachment = self.attachment(size=discord_helpers.MAX_IMAGE_SIZE_BYTES + 1)
        self.assertEqual(await discord_helpers.load_image_attachments([attachment]), [])
        attachment.read.assert_not_awaited()

    async def test_downloaded_oversize_is_not_decoded(self):
        attachment = self.attachment(b"12345", size=1)
        with patch.object(discord_helpers, "MAX_IMAGE_SIZE_BYTES", 4), patch.object(
            discord_helpers, "_decode_image_bytes",
        ) as decode:
            self.assertEqual(await discord_helpers.load_image_attachments([attachment]), [])
        attachment.read.assert_awaited_once()
        decode.assert_not_called()

    async def test_boundary_image_is_rgb_and_decoded_without_blocking_loop(self):
        with Image.new("RGBA", (2, 3), (10, 20, 30, 40)) as source, io.BytesIO() as buffer:
            source.save(buffer, format="PNG")
            data = buffer.getvalue()
        operation, threads = make_thread_probe(None)
        original = discord_helpers._decode_image_bytes

        def decode(image_bytes):
            operation()
            return original(image_bytes)

        with patch.object(discord_helpers, "MAX_IMAGE_SIZE_BYTES", len(data)), patch.object(
            discord_helpers, "_decode_image_bytes", side_effect=decode,
        ):
            images = await discord_helpers.load_image_attachments([self.attachment(data)])
        self.assertEqual(len(images), 1)
        self.addCleanup(images[0].close)
        self.assertEqual((images[0].mode, images[0].size), ("RGB", (2, 3)))
        self.assertEqual(images[0].getpixel((0, 0)), (10, 20, 30))
        self.assertTrue(threads)
        self.assertNotIn(threading.get_ident(), threads)

    async def test_invalid_and_failed_downloads_do_not_hide_later_images(self):
        failed = self.attachment()
        failed.read.side_effect = OSError("架空の取得失敗")
        with Image.new("RGB", (1, 1)) as source, io.BytesIO() as buffer:
            source.save(buffer, format="PNG")
            valid = self.attachment(buffer.getvalue())
        images = await discord_helpers.load_image_attachments([failed, self.attachment(), valid])
        self.assertEqual(len(images), 1)
        images[0].close()

    async def test_non_image_is_not_downloaded(self):
        attachment = self.attachment()
        attachment.filename, attachment.content_type = "fictional.txt", "text/plain"
        self.assertEqual(await discord_helpers.load_image_attachments([attachment]), [])
        attachment.read.assert_not_awaited()

    async def test_repeated_cancellation_waits_for_decode_and_closes_all_images(self):
        loop = asyncio.get_running_loop()
        entered, release = asyncio.Event(), threading.Event()
        first, second = Mock(), Mock()
        decoded = []

        def decode(data):
            if decoded:
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(3):
                    raise AssertionError("画像展開の再開を待てませんでした。")
                return second
            decoded.append(True)
            return first

        with patch.object(discord_helpers, "_decode_image_bytes", side_effect=decode):
            task = spawn(discord_helpers.load_image_attachments([
                self.attachment(), self.attachment(),
            ]), label="画像展開のキャンセル検証")
            try:
                await asyncio.wait_for(entered.wait(), 2)
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        first.close.assert_called_once()
        second.close.assert_called_once()
