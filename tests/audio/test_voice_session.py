"""自律配信の読み上げを、キュー待機後・音声合成後にも取り消せることを検証する。"""

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord
from persona_bot.audio import voice


class VoiceSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.client = Mock(spec=discord.VoiceClient)
        self.client.is_connected.return_value = True
        self.client.channel = SimpleNamespace(id=10)
        self.guild = SimpleNamespace(id=1, voice_client=self.client)
        self.valid = True
        self.profile = {"voice": "架空の話者"}
        self.synthesize = AsyncMock(return_value=b"pcm")
        self.real_play = voice._play_pcm
        self.play = AsyncMock()
        self.workers = []

        def start(coro, **kwargs):
            self.workers.append(coro)
            self.addCleanup(coro.close)

        for name, replacement in (
            ("_speech_queues", {}), ("spawn", start),
            ("resolve_profile", Mock(return_value=self.profile)),
            ("_synthesize_pcm", self.synthesize), ("_play_pcm", self.play),
        ):
            patcher = patch.object(voice, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def enqueue(self):
        await voice.speak_text(
            self.guild, "架空の配信音声", "架空のペルソナ", is_valid=lambda: self.valid,
        )

    async def test_invalid_session_is_not_enqueued(self) -> None:
        self.valid = False
        await self.enqueue()
        self.assertEqual(self.workers, [])
        self.assertEqual(voice._speech_queues, {})

    async def test_ended_session_is_skipped_after_queue_wait_without_losing_normal_reply(self) -> None:
        await self.enqueue()
        await voice.speak_text(self.guild, "通常の架空の返信", "架空のペルソナ")
        self.valid = False
        await self.workers[0]
        self.synthesize.assert_awaited_once_with("通常の架空の返信", self.profile)
        self.play.assert_awaited_once_with(self.client, b"pcm", is_valid=ANY)
        self.assertEqual(voice._speech_queues, {})

    async def test_session_end_during_synthesis_discards_pcm(self) -> None:
        async def synthesize(*args):
            self.valid = False
            await asyncio.sleep(0)
            return b"pcm"

        self.synthesize.side_effect = synthesize
        await self.enqueue()
        await self.workers[0]
        self.synthesize.assert_awaited_once()
        self.play.assert_not_awaited()

    async def test_worker_rechecks_connection_and_session_after_playback_lock(self) -> None:
        for change in ("move", "replace", "end", "normal_reply_move"):
            with self.subTest(change=change):
                self.guild.voice_client = self.client
                self.client.channel = SimpleNamespace(id=10)
                self.valid = True
                self.workers.clear()
                lock = asyncio.Lock()
                await lock.acquire()
                entered = asyncio.Event()

                async def play(*args, **kwargs):
                    entered.set()
                    await self.real_play(*args, **kwargs)

                with patch.object(voice, "_play_pcm", side_effect=play), patch.object(
                    voice, "_playback_locks", {self.client: lock},
                ), patch.object(voice, "_play_pcm_locked", AsyncMock()) as locked_play:
                    if change == "normal_reply_move":
                        await voice.speak_text(self.guild, "通常の架空の返信")
                    else:
                        await self.enqueue()
                    task = asyncio.create_task(self.workers[0])
                    try:
                        await asyncio.wait_for(entered.wait(), 2)
                        self.assertFalse(task.done())
                        if change in ("move", "normal_reply_move"):
                            self.client.channel = SimpleNamespace(id=20)
                        elif change == "replace":
                            self.guild.voice_client = Mock(spec=discord.VoiceClient)
                        else:
                            self.valid = False
                        lock.release()
                        await asyncio.wait_for(task, 2)
                    finally:
                        if lock.locked():
                            lock.release()
                        await asyncio.gather(task, return_exceptions=True)
                    locked_play.assert_not_awaited()
                self.assertEqual(voice._speech_queues, {})

    async def test_connection_replacement_or_disconnect_during_synthesis_discards_pcm(self) -> None:
        for change in ("replace", "disconnect"):
            with self.subTest(change=change):
                self.guild.voice_client = self.client
                self.client.is_connected.return_value = True
                self.workers.clear()

                async def synthesize(*args):
                    if change == "replace":
                        self.guild.voice_client = Mock(spec=discord.VoiceClient)
                    else:
                        self.client.is_connected.return_value = False
                    await asyncio.sleep(0)
                    return b"pcm"

                self.synthesize.side_effect = synthesize
                await self.enqueue()
                await self.workers[0]
                self.play.assert_not_awaited()

    async def test_comment_wrapper_preserves_guard_and_voice_profile(self) -> None:
        message = SimpleNamespace(guild=self.guild)
        await voice.play_tts_response(
            message, "架空のコメント応答", "架空のペルソナ", is_valid=lambda: self.valid,
        )
        await self.workers[0]
        self.synthesize.assert_awaited_once_with("架空のコメント応答", self.profile)
        self.play.assert_awaited_once_with(self.client, b"pcm", is_valid=ANY)

    async def test_normal_queue_discards_old_connection_and_channel_requests(self) -> None:
        for change in ("replace", "move"):
            with self.subTest(change=change):
                self.guild.voice_client = self.client
                self.client.channel = SimpleNamespace(id=10)
                self.synthesize.reset_mock()
                self.play.reset_mock()
                self.workers.clear()
                started, release = asyncio.Event(), asyncio.Event()

                async def synthesize(text, profile):
                    if text == "旧接続の一件目":
                        started.set()
                        await release.wait()
                    return text.encode()

                self.synthesize.side_effect = synthesize
                await voice.speak_text(self.guild, "旧接続の一件目")
                await voice.speak_text(self.guild, "旧接続の二件目")
                worker = asyncio.create_task(self.workers[0])
                try:
                    await asyncio.wait_for(started.wait(), 2)
                    if change == "replace":
                        new = Mock(spec=discord.VoiceClient)
                        new.is_connected.return_value = True
                        self.guild.voice_client = new
                    else:
                        new = self.client
                    new.channel = SimpleNamespace(id=20)
                    # 投入経路の整理に依存せず、ワーカー自身も古い待機要素を捨てる。
                    release.set()
                    await asyncio.wait_for(worker, 2)
                    self.play.assert_not_awaited()
                    self.synthesize.assert_awaited_once_with("旧接続の一件目", self.profile)
                    await voice.speak_text(self.guild, "新接続の返信")
                    await self.workers[-1]
                    self.play.assert_awaited_once_with(new, "新接続の返信".encode(), is_valid=ANY)
                    self.assertEqual(voice._speech_queues, {})
                finally:
                    release.set()
                    await asyncio.gather(worker, return_exceptions=True)

    async def test_full_old_queue_accepts_new_connection_and_preserves_its_order(self) -> None:
        with patch.object(voice, "TTS_QUEUE_MAX", 2):
            await voice.speak_text(self.guild, "旧一件目")
            await voice.speak_text(self.guild, "旧二件目")
            new = Mock(spec=discord.VoiceClient)
            new.channel = SimpleNamespace(id=20)
            new.is_connected.return_value = True
            self.guild.voice_client = new
            await voice.speak_text(self.guild, "新一件目")
            await voice.speak_text(self.guild, "新二件目")
            await self.workers[0]
        self.assertEqual([call.args[0] for call in self.synthesize.await_args_list], ["新一件目", "新二件目"])
        self.assertEqual([call.args[0] for call in self.play.await_args_list], [new, new])

    async def test_full_expired_session_queue_accepts_current_and_normal_speech(self) -> None:
        with patch.object(voice, "TTS_QUEUE_MAX", 2):
            await self.enqueue()
            await self.enqueue()
            queue = voice._speech_queues[self.guild.id]
            self.valid = False
            await voice.speak_text(self.guild, "新しい配信の音声", is_valid=lambda: True)
            await voice.speak_text(self.guild, "通常の返信")
            self.assertIs(voice._speech_queues[self.guild.id], queue)
            await self.workers[0]

        self.assertEqual(
            [call.args[0] for call in self.synthesize.await_args_list],
            ["新しい配信の音声", "通常の返信"],
        )
        self.assertEqual(len(self.workers), 1)
        self.assertEqual(self.play.await_count, 2)
        self.assertEqual(voice._speech_queues, {})

    async def test_same_channel_reuse_keeps_both_waiting_requests(self) -> None:
        await voice.speak_text(self.guild, "一件目")
        self.client.channel = SimpleNamespace(id=10)
        await voice.speak_text(self.guild, "二件目")
        await self.workers[0]
        self.assertEqual([call.args[0] for call in self.synthesize.await_args_list], ["一件目", "二件目"])


if __name__ == "__main__":
    unittest.main()
