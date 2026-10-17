"""音声の整形、合成エンジンの退避、PCM変換、読み上げキューを外部通信なしで検証する。"""

import asyncio
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord

from persona_bot.audio import voice


class SpeechTextTests(unittest.TestCase):
    def test_emotion_and_affinity_tags_are_removed_anywhere(self):
        tags = (
            "[V:10, A:-5]", "[v: +5 , a: -10 ]",
            "[happy]", "[SAD]", "[ angry ]", "[arousal:+5]",
            "[ valence : -2 ]", "[affinity:1]",
        )
        for tag in tags:
            for text in (tag + "架空の本文", "架空の" + tag + "本文", "架空の本文" + tag):
                with self.subTest(tag=tag, text=text):
                    self.assertEqual(voice.build_speech_text(text), "架空の本文")
        self.assertEqual(voice.build_speech_text("[happy][V:0, A:0] [arousal:+5]"), "")

    def test_bracketed_body_text_is_preserved(self):
        text = "[Python] [API v2] [日本語の補足] [1, 2] [arousal] [happy birthday]"
        self.assertEqual(voice.build_speech_text(text), text)

    def test_tags_are_removed_before_applying_length_limit(self):
        text = "あ" * voice.TTS_MAX_CHARS
        self.assertEqual(voice.build_speech_text("[happy]" + text + "[V:0, A:0]"), text)

    def test_markup_urls_code_mentions_and_custom_emoji_are_removed(self):
        text = '**架空の説明** https://example.com/path `内部コード` ```py\nprint(1)\n``` <@11> <@!22> <@&33> <#44> <:smile:55> <a:wave:66>\n続き'
        self.assertEqual(voice.build_speech_text(text), "架空の説明 続き")
        self.assertEqual(voice.build_speech_text('https://example.com `code` <:smile:55>'), "")

    def test_exact_limit_and_truncation_notice(self):
        for length in (voice.TTS_MAX_CHARS - 1, voice.TTS_MAX_CHARS):
            with self.subTest(length=length):
                self.assertEqual(voice.build_speech_text("あ" * length), "あ" * length)
        self.assertEqual(voice.build_speech_text("あ" * (voice.TTS_MAX_CHARS + 1)),
                         "あ" * voice.TTS_MAX_CHARS + voice.TTS_TRUNCATION_NOTICE)


class PcmDecodeTests(unittest.TestCase):
    def test_flat_profile_decodes_stereo_without_equalizer(self):
        samples = np.array([-100, -100, 200, 200], dtype=np.int16)
        with patch.object(voice.miniaudio, "decode", return_value=SimpleNamespace(samples=samples)) as decode, patch.object(
            voice, "apply_equalizer", side_effect=AssertionError("加工なしの経路でEQを呼んではいけません。"),
        ):
            result = voice._decode_to_pcm(b"fictional-mp3", {"eq": "flat"})
        self.assertEqual(result, samples.tobytes())
        self.assertEqual(decode.call_args.kwargs["nchannels"], voice.TTS_CHANNELS)
        self.assertEqual(decode.call_args.kwargs["sample_rate"], voice.TTS_SAMPLE_RATE)

    def test_equalized_mono_is_clipped_and_interleaved_for_both_channels(self):
        mono = np.array([-100, 0, 100], dtype=np.int16)
        with patch.object(voice, "needs_equalizer", return_value=True), patch.object(
            voice.miniaudio, "decode", return_value=SimpleNamespace(samples=mono),
        ) as decode, patch.object(voice, "apply_equalizer", return_value=np.array([-40000, 0, 40000], dtype=float)):
            result = voice._decode_to_pcm(b"fictional-mp3", {"eq": "fictional"})
        self.assertEqual(decode.call_args.kwargs["nchannels"], 1)
        np.testing.assert_array_equal(np.frombuffer(result, dtype=np.int16), [-32768, -32768, 0, 0, 32767, 32767])


class SynthesisTests(unittest.IsolatedAsyncioTestCase):
    async def test_edge_stream_ignores_metadata_and_keeps_audio_order(self):
        async def stream():
            for chunk in ({"type": "WordBoundary"}, {"type": "audio", "data": b"first"}, {"type": "audio", "data": b"second"}):
                yield chunk

        profile = {"voice": "fictional-voice", "rate": "+5%", "pitch": "+10Hz"}
        with patch.object(voice.edge_tts, "Communicate", return_value=SimpleNamespace(stream=stream)) as engine:
            self.assertEqual(await voice._fetch_edge_mp3("架空の音声", profile), b"firstsecond")
        engine.assert_called_once_with("架空の音声", "fictional-voice", rate="+5%", pitch="+10Hz")

    async def test_edge_failure_or_empty_response_falls_back_once_and_recovers(self):
        for failure in (OSError("架空の障害"), b""):
            with self.subTest(failure=type(failure).__name__):
                threads = []

                def fallback(text):
                    threads.append(threading.get_ident())
                    return b"fallback"

                with patch.object(voice, "_edge_tts_failing", False), patch.object(
                    voice, "_fetch_edge_mp3", new=AsyncMock(side_effect=[failure, failure, b"recovered"]),
                ), patch.object(voice, "_fetch_gtts_mp3", side_effect=fallback) as backup, patch("builtins.print") as logging:
                    self.assertEqual(await voice._fetch_mp3("架空", {}), b"fallback")
                    self.assertEqual(await voice._fetch_mp3("架空", {}), b"fallback")
                    self.assertEqual(await voice._fetch_mp3("架空", {}), b"recovered")
                    self.assertFalse(voice._edge_tts_failing)
                    self.assertEqual(backup.call_count, 2)
                    self.assertEqual(logging.call_count, 2)
                self.assertNotIn(threading.get_ident(), threads)

    async def test_edge_success_skips_fallback_and_cancellation_does_not_trigger_it(self):
        for result in (b"audio", asyncio.CancelledError()):
            with self.subTest(result=type(result).__name__), patch.object(voice, "_fetch_edge_mp3", new=AsyncMock(side_effect=[result])), patch.object(
                voice, "_fetch_gtts_mp3",
            ) as backup:
                if isinstance(result, BaseException):
                    with self.assertRaises(asyncio.CancelledError):
                        await voice._fetch_mp3("架空", {})
                else:
                    self.assertEqual(await voice._fetch_mp3("架空", {}), result)
                backup.assert_not_called()

    async def test_pcm_decode_runs_in_worker_thread(self):
        threads = []

        def decode(*args):
            threads.append(threading.get_ident())
            return b"pcm"

        with patch.object(voice, "_fetch_mp3", new=AsyncMock(return_value=b"audio")), patch.object(voice, "_decode_to_pcm", side_effect=decode):
            self.assertEqual(await voice._synthesize_pcm("架空", {}), b"pcm")
        self.assertTrue(threads)
        self.assertNotIn(threading.get_ident(), threads)


class SpeechQueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = Mock(spec=discord.VoiceClient)
        self.client.is_connected.return_value = True
        self.guild = SimpleNamespace(id=1, voice_client=self.client)
        self.workers = []

        def start(coro, **kwargs):
            self.workers.append(coro)
            self.addCleanup(coro.close)

        for name, replacement in (("_speech_queues", {}), ("spawn", start)):
            patcher = patch.object(voice, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_empty_or_disconnected_requests_do_not_create_worker(self):
        for guild, text in ((None, "架空"), (self.guild, None), (self.guild, "  "), (self.guild, "https://example.com")):
            await voice.speak_text(guild, text)
        self.client.is_connected.return_value = False
        await voice.speak_text(self.guild, "架空")
        self.guild.voice_client = None
        await voice.speak_text(self.guild, "架空")
        self.assertEqual(self.workers, [])
        self.assertEqual(voice._speech_queues, {})

    async def test_both_entrypoints_synthesize_only_body_text(self):
        with patch.object(voice, "_synthesize_pcm", new=AsyncMock(return_value=b"pcm")) as synthesize, patch.object(
            voice, "_play_pcm", new=AsyncMock(),
        ) as play:
            await voice.speak_text(self.guild, "[happy]架空の独り言[V:0, A:0]")
            await voice.play_tts_response(SimpleNamespace(guild=self.guild), "架空の返信[sad][arousal:+5]")
            await self.workers[0]
        self.assertEqual([call.args[0] for call in synthesize.call_args_list], ["架空の独り言", "架空の返信"])
        self.assertEqual(play.await_count, 2)
        self.assertEqual(voice._speech_queues, {})

    async def test_tag_only_requests_do_not_create_worker(self):
        await voice.speak_text(self.guild, "[happy][V:0, A:0]")
        await voice.play_tts_response(SimpleNamespace(guild=self.guild), "[sad][arousal:+5]")
        self.assertEqual(self.workers, [])
        self.assertEqual(voice._speech_queues, {})

    async def test_queue_overflow_drops_only_newest_request_and_keeps_requested_profiles(self):
        first, second = {"voice": "first"}, {"voice": "second"}
        with patch.object(voice, "TTS_QUEUE_MAX", 2), patch.object(voice, "resolve_profile", side_effect=[first, second, first]), patch.object(
            voice, "_synthesize_pcm", new=AsyncMock(return_value=b"pcm"),
        ) as synthesize, patch.object(voice, "_play_pcm", new=AsyncMock()) as play:
            await voice.speak_text(self.guild, "最初", "first.txt")
            await voice.speak_text(self.guild, "次", "second.txt")
            await voice.speak_text(self.guild, "溢れた分", "first.txt")
            self.assertEqual(len(self.workers), 1)
            await self.workers[0]
        self.assertEqual([call.args for call in synthesize.call_args_list], [("最初", first), ("次", second)])
        self.assertEqual(play.await_count, 2)
        self.assertEqual(voice._speech_queues, {})

    async def test_synthesis_error_and_timeout_skip_to_next_request(self):
        with patch.object(voice, "_synthesize_pcm", new=AsyncMock(side_effect=[asyncio.TimeoutError(), OSError("架空の合成障害"), b"pcm"])), patch.object(
            voice, "_play_pcm", new=AsyncMock(),
        ) as play:
            for text in ("時間切れ", "失敗", "正常"):
                await voice.speak_text(self.guild, text)
            await self.workers[0]
        play.assert_awaited_once_with(self.client, b"pcm")
        self.assertEqual(voice._speech_queues, {})

    async def test_worker_cancellation_clears_registration(self):
        with patch.object(voice, "_synthesize_pcm", new=AsyncMock(side_effect=asyncio.CancelledError())):
            await voice.speak_text(self.guild, "架空")
            with self.assertRaises(asyncio.CancelledError):
                await self.workers[0]
        self.assertEqual(voice._speech_queues, {})

    async def test_playback_completion_can_arrive_from_audio_thread(self):
        threads = []

        def play(source, *, after):
            thread = threading.Thread(target=lambda: after(None))
            threads.append(thread)
            thread.start()
            source.cleanup()

        self.client.play.side_effect = play
        await asyncio.wait_for(voice._play_pcm(self.client, b"\0" * 16), 2)
        for thread in threads:
            thread.join(1)
        self.client.stop.assert_not_called()

    async def test_missing_completion_stops_playback_instead_of_hanging_worker(self):
        async def timeout(awaitable, *, timeout):
            awaitable.close()
            raise asyncio.TimeoutError

        self.client.play.side_effect = lambda source, **kwargs: source.cleanup()
        with patch.object(voice.asyncio, "wait_for", side_effect=timeout):
            await voice._play_pcm(self.client, b"\0" * 16)
        self.client.stop.assert_called_once()
