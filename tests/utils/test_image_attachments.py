"""画像の容量制限・ワーカーでの展開と中断時の回収を確認する。"""

import asyncio
import io
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

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

    @staticmethod
    def png(size):
        """小さな架空画像だけを圧縮し、実メモリを圧迫しない入力にする。"""
        with Image.new("RGBA", size) as source, io.BytesIO() as buffer:
            source.save(buffer, format="PNG")
            return buffer.getvalue()

    async def test_pixel_and_dimension_limits_reject_before_conversion(self):
        """圧縮サイズ内でも画素数・縦横の上限超過はRGB領域を作る前に拒否する。"""
        cases = (
            ("画素数", (4, 4), 4, 15),
            ("横幅", (4, 1), 3, 100),
            ("高さ", (1, 4), 3, 100),
            ("幅なし", (0, 2), 3, 100),
            ("高さなし", (2, 0), 3, 100),
        )
        for case, size, dimension, pixels in cases:
            with self.subTest(制限=case):
                source = MagicMock(size=size)
                source.__enter__.return_value = source
                with patch.object(discord_helpers.Image, "open", return_value=source) as opened, patch.multiple(
                    discord_helpers, MAX_IMAGE_DIMENSION=dimension, MAX_IMAGE_PIXELS=pixels,
                ):
                    self.assertEqual(await discord_helpers.load_image_attachments([self.attachment()]), [])
                source.convert.assert_not_called()
                source.__exit__.assert_called_once()
                self.assertTrue(opened.call_args.args[0].closed)

    async def test_rgb_budget_rejects_before_conversion_and_closes_input(self):
        """1枚だけでも残り予算を1バイト超えれば変換せず入力を閉じる。"""
        source = MagicMock(size=(2, 3))
        source.__enter__.return_value = source
        with patch.object(discord_helpers.Image, "open", return_value=source) as opened:
            with self.assertRaisesRegex(ValueError, "合計容量"):
                discord_helpers._decode_image_bytes(b"fictional", 17)
        source.convert.assert_not_called()
        source.__exit__.assert_called_once()
        self.assertTrue(opened.call_args.args[0].closed)

    async def test_all_image_limits_accept_exact_boundary(self):
        """圧縮サイズ・画素数・寸法・枚数・RGB合計の境界値は受け付ける。"""
        data = self.png((2, 3))
        attachments = [self.attachment(data), self.attachment(data)]
        with patch.multiple(
            discord_helpers, MAX_IMAGE_SIZE_BYTES=len(data), MAX_IMAGE_PIXELS=6,
            MAX_IMAGE_DIMENSION=3, MAX_IMAGE_ATTACHMENTS=2, MAX_IMAGE_TOTAL_RGB_BYTES=36,
        ):
            images = await discord_helpers.load_image_attachments(attachments)
        self.addCleanup(discord_helpers.close_image_attachments, images)
        self.assertEqual([(image.mode, image.size) for image in images], [("RGB", (2, 3))] * 2)
        self.assertEqual(sum(image.width * image.height * 3 for image in images), 36)
        for attachment in attachments:
            attachment.read.assert_awaited_once()

    async def test_cumulative_budget_includes_previous_images(self):
        """個別には安全な画像でも保持済み画像と合計し、残り予算内の後続だけを読む。"""
        attachments = [self.attachment(self.png(size)) for size in ((2, 3), (2, 3), (1, 2))]
        original = discord_helpers._decode_image_bytes
        with patch.multiple(discord_helpers, MAX_IMAGE_PIXELS=6, MAX_IMAGE_TOTAL_RGB_BYTES=24), patch.object(
            discord_helpers, "_decode_image_bytes", wraps=original,
        ) as decode:
            images = await discord_helpers.load_image_attachments(attachments)
        self.addCleanup(discord_helpers.close_image_attachments, images)
        self.assertEqual([image.size for image in images], [(2, 3), (1, 2)])
        self.assertEqual([call.args[1] for call in decode.call_args_list], [24, 6, 6])

    async def test_exhausted_budget_skips_later_downloads(self):
        """累計予算が尽きた時点で後続画像の取得も止める。"""
        attachments = [self.attachment(self.png((2, 3))) for _ in range(2)]
        with patch.object(discord_helpers, "MAX_IMAGE_TOTAL_RGB_BYTES", 18):
            images = await discord_helpers.load_image_attachments(attachments)
        self.addCleanup(discord_helpers.close_image_attachments, images)
        self.assertEqual(len(images), 1)
        attachments[1].read.assert_not_awaited()

    async def test_attachment_count_skips_later_downloads(self):
        """画像以外を数えず、画像の受付枚数を超えた分は取得しない。"""
        attachments = [self.attachment(self.png((1, 1))) for _ in range(4)]
        attachments[0].filename, attachments[0].content_type = "架空.txt", "text/plain"
        with patch.object(discord_helpers, "MAX_IMAGE_ATTACHMENTS", 2):
            images = await discord_helpers.load_image_attachments(attachments)
        self.addCleanup(discord_helpers.close_image_attachments, images)
        self.assertEqual(len(images), 2)
        attachments[0].read.assert_not_awaited()
        attachments[-1].read.assert_not_awaited()

    async def test_failed_images_also_consume_attempt_limit(self):
        """不正画像や過大な申告でも試行枚数を消費し、取得を繰り返さない。"""
        too_large = self.attachment(size=discord_helpers.MAX_IMAGE_SIZE_BYTES + 1)
        invalid = self.attachment()
        later = self.attachment(self.png((1, 1)))
        with patch.object(discord_helpers, "MAX_IMAGE_ATTACHMENTS", 2):
            self.assertEqual(await discord_helpers.load_image_attachments([too_large, invalid, later]), [])
        too_large.read.assert_not_awaited()
        invalid.read.assert_awaited_once()
        later.read.assert_not_awaited()

    async def test_conversion_failure_closes_source_and_preserves_held_image(self):
        """変換失敗の入力を閉じ、成功した画像は受取側へ所有権を渡す。"""
        first = MagicMock(size=(2, 2))
        second = MagicMock(size=(2, 2))
        for source in (first, second):
            source.__enter__.return_value = source
        converted = Image.new("RGB", (2, 2))
        self.addCleanup(converted.close)
        first.convert.return_value = converted
        second.convert.side_effect = OSError("架空の変換失敗")
        with patch.object(discord_helpers.Image, "open", side_effect=[first, second]) as opened:
            images = await discord_helpers.load_image_attachments([self.attachment(), self.attachment()])
        self.assertEqual(images, [converted])
        converted.getpixel((0, 0))
        for source in (first, second):
            source.__exit__.assert_called_once()
        self.assertTrue(all(call.args[0].closed for call in opened.call_args_list))
        discord_helpers.close_image_attachments(images)
        with self.assertRaises(ValueError):
            converted.getpixel((0, 0))

    async def test_rejection_after_success_keeps_accepted_image_open(self):
        """上限超過した後続画像を拒否しても先行画像を勝手に閉じない。"""
        with patch.multiple(discord_helpers, MAX_IMAGE_PIXELS=4, MAX_IMAGE_DIMENSION=2):
            images = await discord_helpers.load_image_attachments([
                self.attachment(self.png((2, 2))), self.attachment(self.png((3, 2))),
            ])
        self.assertEqual(len(images), 1)
        images[0].getpixel((0, 0))
        discord_helpers.close_image_attachments(images)
        with self.assertRaises(ValueError):
            images[0].getpixel((0, 0))

    async def test_download_cancellation_closes_previously_decoded_images(self):
        """次の画像取得中のキャンセルでも取得済み画像を回収する。"""
        first = Image.new("RGB", (2, 2))
        later = self.attachment()
        later.read.side_effect = asyncio.CancelledError
        with patch.object(discord_helpers, "_decode_image_bytes", return_value=first):
            with self.assertRaises(asyncio.CancelledError):
                await discord_helpers.load_image_attachments([self.attachment(), later])
        with self.assertRaises(ValueError):
            first.getpixel((0, 0))

    async def test_open_failure_closes_input_buffer(self):
        """ヘッダーを開けなかった場合も圧縮データの入力バッファを閉じる。"""
        with patch.object(discord_helpers.Image, "open", side_effect=OSError("架空の読込失敗")) as opened:
            with self.assertRaises(OSError):
                discord_helpers._decode_image_bytes(b"fictional", 100)
        self.assertTrue(opened.call_args.args[0].closed)

    def test_cleanup_skips_noncallable_close_and_continues_after_failure(self):
        first = Mock(close=Mock(side_effect=OSError("架空の解放失敗")))
        last = Image.new("RGB", (2, 2))
        discord_helpers.close_image_attachments([first, object(), SimpleNamespace(close=None), last])
        first.close.assert_called_once()
        with self.assertRaises(ValueError):
            last.getpixel((0, 0))

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

        def decode(image_bytes, remaining_rgb_bytes):
            operation()
            return original(image_bytes, remaining_rgb_bytes)

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
        first, second = Mock(size=(2, 2)), Mock(size=(2, 2))
        decoded = []

        def decode(data, remaining_rgb_bytes):
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
