"""自律配信の読み上げを、キュー待機後・音声合成後にも取り消せることを検証する。"""

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord
from persona_bot.audio import voice


class VoiceSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.client = Mock(spec=discord.VoiceClient)
        self.client.is_connected.return_value = True
        self.guild = SimpleNamespace(id=1, voice_client=self.client)
        self.valid = True
        self.profile = {"voice": "架空の話者"}
        self.synthesize = AsyncMock(return_value=b"pcm")
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
        self.play.assert_awaited_once_with(self.client, b"pcm")
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
        self.play.assert_awaited_once_with(self.client, b"pcm")


if __name__ == "__main__":
    unittest.main()
