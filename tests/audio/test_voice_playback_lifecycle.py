"""discord.pyの実AudioPlayerで、停止・完了通知・ストリーム解放の順序を検証する。"""

import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import io
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

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

    async def test_invalid_request_after_playback_lock_does_not_stop_current_audio(self) -> None:
        valid = True
        lock = asyncio.Lock()
        await lock.acquire()
        with patch.object(voice, "_playback_locks", {self.client: lock}):
            task = asyncio.create_task(voice._play_pcm(self.client, b"pcm", is_valid=lambda: valid))
            try:
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                valid = False
                lock.release()
                await asyncio.wait_for(task, 2)
            finally:
                if lock.locked():
                    lock.release()
                await asyncio.gather(task, return_exceptions=True)
        self.client.stop.assert_not_called()
        self.client.play.assert_not_called()

    async def test_worker_rechecks_finished_player_join_after_executor_wait(self) -> None:
        # 終了済みAudioPlayerでも、実executorが埋まればjoinの完了待ちは発生する。
        # 合成だけをモックにし、依頼の投入・ワーカー・join・正常な再生は実装を通す。
        executor = ThreadPoolExecutor(max_workers=1)
        self.loop.set_default_executor(executor)
        for change in ("move", "replace", "end", "disconnect", "unchanged", "cancel"):
            with self.subTest(change=change):
                self.client.play.reset_mock()
                self.client.stop.reset_mock()
                self.client.is_connected.return_value = True
                self.client.channel = SimpleNamespace(id=10)
                guild = SimpleNamespace(id=1, voice_client=self.client)
                valid = True
                occupied, joining = asyncio.Event(), asyncio.Event()
                release_executor = threading.Event()
                old_source = discord.PCMAudio(io.BytesIO(b"\0" * 16))
                previous = AudioPlayer(old_source, self.client)
                self.players.append(previous)
                self.client._player = previous
                with patch.object(AudioPlayer, "_do_run", lambda player: None):
                    previous.start()
                    await asyncio.to_thread(previous.join)
                self.assertFalse(previous.is_alive())
                old_source.stream.close()

                def occupy():
                    self.loop.call_soon_threadsafe(occupied.set)
                    if not release_executor.wait(5):
                        raise TimeoutError("音声テストのexecutor待機が終了しませんでした")

                blocker = self.loop.run_in_executor(None, occupy)
                await asyncio.wait_for(occupied.wait(), 2)
                run_in_thread = voice.run_in_thread

                async def join_in_thread(func, *args):
                    if func == previous.join:
                        joining.set()
                    return await run_in_thread(func, *args)

                queue = deque([voice._SpeechRequest("架空の音声", {}, self.client, 10, lambda: valid)])
                with patch.object(voice, "_speech_queues", {1: queue}), patch.object(
                    voice, "_synthesize_pcm", AsyncMock(return_value=b"\0" * 16),
                ), patch.object(voice, "run_in_thread", join_in_thread), patch.object(
                    AudioPlayer, "_do_run", lambda player: None,
                ):
                    task = asyncio.create_task(voice._run_speech_worker(guild))
                    try:
                        await asyncio.wait_for(joining.wait(), 2)
                        self.client.play.assert_not_called()
                        if change == "move":
                            self.client.channel = SimpleNamespace(id=20)
                        elif change == "replace":
                            guild.voice_client = Mock(spec=discord.VoiceClient)
                        elif change == "end":
                            valid = False
                        elif change == "disconnect":
                            self.client.is_connected.return_value = False
                        elif change == "cancel":
                            for _ in range(3):
                                task.cancel()
                                await asyncio.sleep(0)
                                self.assertFalse(task.done())
                        release_executor.set()
                        if change == "cancel":
                            with self.assertRaises(asyncio.CancelledError):
                                await asyncio.wait_for(task, 2)
                        else:
                            await asyncio.wait_for(task, 2)
                        self.assertEqual(voice._speech_queues, {})
                    finally:
                        release_executor.set()
                        await asyncio.gather(blocker, task, return_exceptions=True)
                if change == "unchanged":
                    self.client.play.assert_called_once()
                    self.assertTrue(self.sources[-1].stream.closed)
                else:
                    self.client.play.assert_not_called()
                self.assertTrue(all(not player.is_alive() for player in self.players))


if __name__ == "__main__":
    unittest.main()
