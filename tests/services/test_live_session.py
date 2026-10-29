"""Gemini Live の接続（#268）の設定・再開・再接続・利用枠の扱いを、模擬の接続で確かめる。"""

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from google.genai import errors, types
from persona_bot.config import constants
from persona_bot.services.live_session import (
    CLOSED_FAILED, CLOSED_QUOTA, LiveOptions, LiveSession, LiveUnavailableError,
    build_live_config, is_config_error, is_quota_error,
)
from tests.helpers.network_guard import block_network

OPTIONS = LiveOptions(system_instruction="架空のペルソナ", voice_name="Kore", grounding=True)


def message(**fields):
    base = {"server_content": None, "session_resumption_update": None, "go_away": None, "usage_metadata": None}
    base.update(fields)
    return SimpleNamespace(**base)


class FakeSession:
    """receive() は台本の応答を1つずつ返し、最後に指定の例外を投げる（None なら待ち続ける）。"""

    def __init__(self, script, end=None):
        self.script = list(script)
        self.end = end
        self.sent = []
        self.closed = asyncio.Event()

    async def receive(self):
        while self.script:
            yield self.script.pop(0)
            await asyncio.sleep(0)
            return
        if self.end is not None:
            raise self.end
        await self.closed.wait()
        raise errors.APIError(1000, {"message": "架空の切断"})

    async def send_realtime_input(self, **payload):
        self.sent.append(payload)


class FakeConnect:
    def __init__(self, outcomes):
        # outcomes: FakeSession か、接続時に投げる例外
        self.outcomes = list(outcomes)
        self.configs = []
        self.sessions = []

    def __call__(self, *, model, config):
        self.configs.append(config)
        outcome = self.outcomes.pop(0)
        outer = self

        class Context:
            async def __aenter__(self):
                if isinstance(outcome, BaseException):
                    raise outcome
                outer.sessions.append(outcome)
                return outcome

            async def __aexit__(self, *exc):
                if not isinstance(outcome, BaseException):
                    outcome.closed.set()

        return Context()


class ConfigTests(unittest.TestCase):
    def test_manual_activity_transcription_resumption_and_compression(self):
        config = build_live_config(OPTIONS, handle="架空のハンドル", minimal=False)
        self.assertEqual(config.response_modalities, [types.Modality.AUDIO])
        self.assertEqual(config.system_instruction, "架空のペルソナ")
        self.assertTrue(config.realtime_input_config.automatic_activity_detection.disabled)
        self.assertEqual(
            config.realtime_input_config.activity_handling, types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
        )
        self.assertIsNotNone(config.input_audio_transcription)
        self.assertIsNotNone(config.output_audio_transcription)
        self.assertEqual(config.session_resumption.handle, "架空のハンドル")
        self.assertEqual(config.context_window_compression.trigger_tokens, constants.LIVE_COMPRESSION_TRIGGER_TOKENS)
        self.assertEqual(
            config.context_window_compression.sliding_window.target_tokens, constants.LIVE_COMPRESSION_TARGET_TOKENS,
        )
        self.assertEqual(config.speech_config.voice_config.prebuilt_voice_config.voice_name, "Kore")
        self.assertIsNotNone(config.tools[0].google_search)

    def test_minimal_config_drops_voice_and_search(self):
        config = build_live_config(OPTIONS, handle=None, minimal=True)
        self.assertIsNone(config.speech_config)
        self.assertIsNone(config.tools)
        self.assertIsNone(config.session_resumption.handle)

    def test_no_voice_or_search_unless_requested(self):
        config = build_live_config(
            LiveOptions(system_instruction="x", voice_name="", grounding=False), handle=None, minimal=False,
        )
        self.assertIsNone(config.speech_config)
        self.assertIsNone(config.tools)

    def test_error_classification(self):
        quota = errors.ClientError(429, {"message": "架空の上限"})
        exhausted = errors.APIError(1011, {"message": "You exceeded your current quota"})
        config = errors.APIError(1007, {"message": "Invalid voice"})
        server = errors.ServerError(503, {"message": "架空の障害"})
        self.assertTrue(is_quota_error(quota))
        self.assertTrue(is_quota_error(exhausted))
        self.assertFalse(is_quota_error(config))
        self.assertTrue(is_config_error(config))
        self.assertFalse(is_config_error(quota))
        self.assertFalse(is_config_error(server))


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        block_network(self)
        self.messages = []
        self.closed_reasons = []
        self.reconnected = 0
        self.sleeps = []

    def make(self, outcomes) -> tuple[LiveSession, FakeConnect]:
        connect = FakeConnect(outcomes)

        async def on_message(received):
            self.messages.append(received)

        async def on_closed(reason):
            self.closed_reasons.append(reason)

        def on_reconnected():
            self.reconnected += 1

        async def sleep(seconds):
            self.sleeps.append(seconds)

        session = LiveSession(
            None, OPTIONS, on_message=on_message, on_reconnected=on_reconnected, on_closed=on_closed,
            connect=connect, sleep=sleep,
        )
        self.addAsyncCleanup(session.close)
        return session, connect

    async def wait_until(self, condition, timeout=2.0):
        async with asyncio.timeout(timeout):
            while not condition():
                await asyncio.sleep(0.01)

    async def test_messages_are_forwarded_and_sends_use_realtime_input(self):
        first = FakeSession([message(server_content="架空の応答")])
        live, _connect = self.make([first])
        await live.start()
        await self.wait_until(lambda: self.messages)
        self.assertTrue(live.connected)
        self.assertTrue(await live.activity_start())
        self.assertTrue(await live.send_audio(b"pcm"))
        self.assertTrue(await live.activity_end())
        self.assertIsInstance(first.sent[0]["activity_start"], types.ActivityStart)
        self.assertEqual(first.sent[1]["audio"].mime_type, constants.LIVE_INPUT_MIME_TYPE)
        self.assertIsInstance(first.sent[2]["activity_end"], types.ActivityEnd)

    async def test_go_away_reconnects_immediately_with_the_resumption_handle(self):
        first = FakeSession([
            message(session_resumption_update=SimpleNamespace(resumable=True, new_handle="h1")),
            message(go_away=SimpleNamespace(time_left="10s")),
        ])
        second = FakeSession([])
        live, connect = self.make([first, second])
        await live.start()
        await self.wait_until(lambda: self.reconnected == 1)
        self.assertEqual(connect.configs[1].session_resumption.handle, "h1")
        self.assertEqual(self.sleeps, [])  # 予告による張り替えは待たない
        self.assertEqual(live.generation, 2)

    async def test_unexpected_drop_reconnects_after_backoff(self):
        first = FakeSession([], end=errors.ServerError(503, {"message": "架空の障害"}))
        second = FakeSession([])
        live, _connect = self.make([first, second])
        with self.assertLogs("persona_bot.services.live_session", level="WARNING"):
            await live.start()
            await self.wait_until(lambda: self.reconnected == 1)
        self.assertEqual(self.sleeps, [constants.LIVE_RECONNECT_BACKOFF_SECONDS[0]])

    async def test_quota_during_session_closes_without_reconnecting(self):
        first = FakeSession([], end=errors.ClientError(429, {"message": "架空の上限"}))
        live, connect = self.make([first])
        with self.assertLogs("persona_bot.services.live_session", level="WARNING"):
            await live.start()
            await self.wait_until(lambda: self.closed_reasons)
        self.assertEqual(self.closed_reasons, [CLOSED_QUOTA])
        self.assertEqual(len(connect.configs), 1)

    async def test_failed_reconnects_are_limited_then_reported(self):
        failure = errors.ServerError(503, {"message": "架空の障害"})
        first = FakeSession([], end=failure)
        outcomes = [first] + [errors.ServerError(503, {"message": "架空"})] * constants.LIVE_RECONNECT_MAX_ATTEMPTS
        live, connect = self.make(outcomes)
        with self.assertLogs("persona_bot.services.live_session", level="WARNING"):
            await live.start()
            await self.wait_until(lambda: self.closed_reasons)
        self.assertEqual(self.closed_reasons, [CLOSED_FAILED])
        self.assertEqual(len(connect.configs), 1 + constants.LIVE_RECONNECT_MAX_ATTEMPTS)

    async def test_failed_resume_retries_with_a_fresh_session(self):
        first = FakeSession(
            [message(session_resumption_update=SimpleNamespace(resumable=True, new_handle="h1"))],
            end=errors.ServerError(503, {"message": "架空の障害"}),
        )
        second = FakeSession([])
        live, connect = self.make([first, errors.APIError(1008, {"message": "invalid handle"}), second])
        with self.assertLogs("persona_bot.services.live_session", level="WARNING"):
            await live.start()
            await self.wait_until(lambda: self.reconnected == 1)
        self.assertEqual(connect.configs[1].session_resumption.handle, "h1")
        self.assertIsNone(connect.configs[2].session_resumption.handle)

    async def test_config_error_at_start_retries_without_voice_and_search(self):
        live, connect = self.make([errors.APIError(1007, {"message": "Invalid voice"}), FakeSession([])])
        with self.assertLogs("persona_bot.services.live_session", level="WARNING"):
            await live.start()
        self.assertIsNotNone(connect.configs[0].speech_config)
        self.assertIsNone(connect.configs[1].speech_config)
        self.assertIsNone(connect.configs[1].tools)

    async def test_start_failure_reports_quota(self):
        for error, quota in ((errors.ClientError(429, {"message": "架空の上限"}), True),
                             (errors.ServerError(503, {"message": "架空の障害"}), False)):
            with self.subTest(quota=quota):
                live, _connect = self.make([error])
                with self.assertLogs("persona_bot.services.live_session", level="ERROR"):
                    with self.assertRaises(LiveUnavailableError) as raised:
                        await live.start()
                self.assertEqual(raised.exception.quota, quota)
                self.assertFalse(live.connected)

    async def test_close_stops_without_reconnecting(self):
        first = FakeSession([])
        live, connect = self.make([first])
        await live.start()
        await live.close()
        await asyncio.sleep(0.05)
        self.assertFalse(live.connected)
        self.assertEqual(len(connect.configs), 1)
        self.assertEqual(self.closed_reasons, [])
        self.assertFalse(await live.send_audio(b"pcm"))

    async def test_restart_fresh_drops_the_handle(self):
        first = FakeSession([message(session_resumption_update=SimpleNamespace(resumable=True, new_handle="h1"))])
        second = FakeSession([])
        live, connect = self.make([first, second])
        await live.start()
        await self.wait_until(lambda: self.messages)
        await live.restart_fresh()
        await self.wait_until(lambda: self.reconnected == 1)
        self.assertIsNone(connect.configs[1].session_resumption.handle)
        self.assertEqual(self.sleeps, [])


if __name__ == "__main__":
    unittest.main()
