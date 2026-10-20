"""discord.pyの実AudioPlayerで、停止・完了通知・ストリーム解放の順序を検証する。"""

import asyncio
import io
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord
from discord.player import AudioPlayer

from persona_bot.audio import voice


class PlaybackLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.client = Mock(spec=discord.VoiceClient)
        self.client._player = None
        self.client.is_connected.return_value = True
        self.players = []
        self.sources = []
        self.release = threading.Event()
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()
        self.loop = asyncio.get_running_loop()
        self.client.is_playing.side_effect = lambda: discord.VoiceClient.is_playing(self.client)
        self.client.is_paused.side_effect = lambda: discord.VoiceClient.is_paused(self.client)

        def stop():
            discord.VoiceClient.stop(self.client)
            self.stopped.set()

        def play(source, *, after):
            self.sources.append(source)
            discord.VoiceClient.play(self.client, source, after=after)
            self.players.append(self.client._player)

        self.client.stop.side_effect = stop
        self.client.play.side_effect = play
        # 外部通信とコーデックだけ止め、play/run/after/cleanup/stopは実装を使う。
        for patcher in (patch.object(AudioPlayer, "_speak"), patch.object(discord.opus, "Encoder")):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def asyncTearDown(self) -> None:
        self.release.set()
        for player in self.players:
            player.stop()
            await asyncio.to_thread(player.join, 2)
            self.assertFalse(player.is_alive())

    def blocking_read(self, player):
        player.source.read()
        self.loop.call_soon_threadsafe(self.started.set)
        if not self.release.wait(5):
            raise TimeoutError("音声テストの待機が終了しませんでした")
        # stop()が呼ばれても、進行中の読み取りはまだ終わっていない場面を再現する。
        player.source.read()

    async def test_timeout_waits_for_audio_thread_before_closing_stream(self) -> None:
        with patch.object(AudioPlayer, "_do_run", lambda player: self.blocking_read(player)), patch.object(
            voice, "TTS_PLAY_TIMEOUT_MARGIN_SECONDS", 0.01,
        ):
            task = asyncio.create_task(voice._play_pcm(self.client, b"\0" * 16))
            try:
                await asyncio.wait_for(self.started.wait(), 2)
                await asyncio.wait_for(self.stopped.wait(), 2)
                self.assertFalse(task.done())
                self.assertFalse(self.sources[0].stream.closed)
                self.release.set()
                await asyncio.wait_for(task, 2)
            finally:
                self.release.set()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(self.sources[0].stream.closed)
        self.assertIsNone(self.players[0]._current_error)

    async def test_repeated_cancellation_keeps_stream_open_until_audio_thread_stops(self) -> None:
        with patch.object(AudioPlayer, "_do_run", lambda player: self.blocking_read(player)):
            task = asyncio.create_task(voice._play_pcm(self.client, b"\0" * 16))
            try:
                await asyncio.wait_for(self.started.wait(), 2)
                task.cancel()
                await asyncio.wait_for(self.stopped.wait(), 2)
                for _ in range(2):
                    task.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(task.done())
                    self.assertFalse(self.sources[0].stream.closed)
                self.release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
            finally:
                self.release.set()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(self.sources[0].stream.closed)
        self.assertIsNone(self.players[0]._current_error)

    async def test_after_callback_does_not_imply_audio_thread_has_finished(self) -> None:
        cleaning = asyncio.Event()
        original_cleanup = discord.PCMAudio.cleanup

        def cleanup(source):
            self.loop.call_soon_threadsafe(cleaning.set)
            if not self.release.wait(5):
                raise TimeoutError("音声テストの後片付けが終了しませんでした")
            original_cleanup(source)

        with patch.object(AudioPlayer, "_do_run", lambda player: None), patch.object(
            discord.PCMAudio, "cleanup", cleanup,
        ):
            task = asyncio.create_task(voice._play_pcm(self.client, b"\0" * 16))
            try:
                await asyncio.wait_for(cleaning.wait(), 2)
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertFalse(self.sources[0].stream.closed)
                self.release.set()
                await asyncio.wait_for(task, 2)
            finally:
                self.release.set()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(self.sources[0].stream.closed)

    async def test_existing_playing_or_paused_audio_stops_before_replacement_starts(self) -> None:
        for paused in (False, True):
            with self.subTest(paused=paused):
                self.release.clear()
                self.started.clear()
                self.stopped.clear()
                self.client.play.reset_mock()
                old_source = discord.PCMAudio(io.BytesIO(b"\0" * 16))
                with patch.object(AudioPlayer, "_do_run", lambda player: self.blocking_read(player)):
                    previous = AudioPlayer(old_source, self.client)
                    self.players.append(previous)
                    self.client._player = previous
                    previous.start()
                    await asyncio.wait_for(self.started.wait(), 2)
                    if paused:
                        previous.pause()
                    task = asyncio.create_task(voice._play_pcm(self.client, b"\0" * 16))
                    try:
                        await asyncio.wait_for(self.stopped.wait(), 2)
                        self.client.play.assert_not_called()
                        self.assertTrue(previous.is_alive())
                        self.assertFalse(old_source.stream.closed)
                        self.release.set()
                        await asyncio.wait_for(task, 2)
                    finally:
                        self.release.set()
                        await asyncio.gather(task, return_exceptions=True)
                        old_source.stream.close()
                self.client.play.assert_called_once()
                self.assertFalse(previous.is_alive())
                self.assertIsNone(previous._current_error)

    async def test_simultaneous_requests_share_the_playback_lock(self) -> None:
        with patch.object(AudioPlayer, "_do_run", lambda player: self.blocking_read(player)):
            first = asyncio.create_task(voice._play_pcm(self.client, b"\0" * 16))
            second = None
            try:
                await asyncio.wait_for(self.started.wait(), 2)
                second = asyncio.create_task(voice._play_pcm(self.client, b"\0" * 32))
                await asyncio.sleep(0)
                self.client.play.assert_called_once()
                self.client.stop.assert_not_called()
                self.assertFalse(second.done())
                self.release.set()
                await asyncio.wait_for(asyncio.gather(first, second), 2)
            finally:
                self.release.set()
                await asyncio.gather(*[task for task in (first, second) if task is not None], return_exceptions=True)
        self.assertEqual(self.client.play.call_count, 2)
        self.assertTrue(all(source.stream.closed for source in self.sources))
        self.assertTrue(all(player._current_error is None for player in self.players))


if __name__ == "__main__":
    unittest.main()
