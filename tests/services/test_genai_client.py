"""共通SDK境界のタイムアウトと暗黙リトライ抑止を確認する。"""

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from google import genai
from google.genai import errors, types
import httpx

from persona_bot.services import genai_client
from persona_bot.services.genai_client import close_genai_client, create_genai_client, generate_content
from persona_bot.services.gemini_service import GeminiService


class GenaiClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_grounding_factory_keeps_key_out_of_urls_and_error_logs(self) -> None:
        # 本番と同じfactory・SDKを使い、HTTP境界だけを差し替える。
        for failure in (None, "connection", "timeout", "http_status", 400, 503):
            with self.subTest(failure=failure):
                requests = []

                async def handle(request):
                    requests.append(request)
                    if failure == "connection":
                        raise httpx.ConnectError(f"架空の接続障害: {request.url}", request=request)
                    if failure == "timeout":
                        raise httpx.ReadTimeout(f"架空のタイムアウト: {request.url}", request=request)
                    if failure == "http_status":
                        response = httpx.Response(502, request=request)
                        response.raise_for_status()
                    if failure is not None:
                        return httpx.Response(failure, json={"error": {"code": failure, "message": "架空のAPI障害"}})
                    return httpx.Response(200, json={"candidates": [{
                        "content": {"parts": [{"text": "架空の検索結果"}]}, "finishReason": "STOP",
                    }]})

                with patch.object(genai_client.httpx, "AsyncHTTPTransport", return_value=httpx.MockTransport(handle)):
                    client = create_genai_client("YOUR_GEMINI_API_KEY")
                try:
                    with patch("builtins.print") as logging:
                        result = await GeminiService(client).generate("架空の質問", "selected-model", grounding=True, timeout=2)
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(requests[0].headers["x-goog-api-key"], "YOUR_GEMINI_API_KEY")
                    self.assertNotIn("key", requests[0].url.params)
                    self.assertNotIn("YOUR_GEMINI_API_KEY", str(requests[0].url))
                    self.assertNotIn("YOUR_GEMINI_API_KEY", str(logging.call_args_list))
                    self.assertIn(b"googleSearch", requests[0].content)
                    self.assertEqual(result[0], "架空の検索結果" if failure is None else None)
                finally:
                    await close_genai_client(client)

    def setUp(self) -> None:
        self.response = object()
        self.request = AsyncMock(return_value=self.response)
        self.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=self.request)))

    async def test_factory_fixes_backend_and_one_attempt_for_all_operations(self) -> None:
        with patch.object(genai_client.genai, "Client") as factory:
            result = create_genai_client("YOUR_API_KEY")
        self.assertIs(result, factory.return_value)
        self.assertEqual(factory.call_args.kwargs["api_key"], "YOUR_API_KEY")
        self.assertIs(factory.call_args.kwargs["vertexai"], False)
        self.assertIs(factory.call_args.kwargs["enterprise"], False)
        self.assertEqual(factory.call_args.kwargs["http_options"].retry_options.attempts, 1)
        await factory.call_args.kwargs["http_options"].httpx_async_client.aclose()

    async def test_timeout_seconds_are_rounded_up_to_milliseconds(self) -> None:
        result = await generate_content(self.client, model="selected-model", contents="架空の入力", timeout=1.2341,
                                        temperature=0, response_mime_type="application/json")
        self.assertIs(result, self.response)
        kwargs = self.request.call_args.kwargs
        self.assertEqual(kwargs["model"], "selected-model")
        self.assertEqual(kwargs["contents"], "架空の入力")
        self.assertIsInstance(kwargs["config"], types.GenerateContentConfig)
        self.assertEqual(kwargs["config"].http_options.timeout, 1235)
        self.assertEqual(kwargs["config"].http_options.retry_options.attempts, 1)
        self.assertEqual(kwargs["config"].temperature, 0)
        self.assertEqual(kwargs["config"].response_mime_type, "application/json")

    async def test_no_timeout_keeps_sdk_default_and_disables_retries(self) -> None:
        await generate_content(self.client, model="selected-model", contents=["架空の入力"])
        config = self.request.call_args.kwargs["config"]
        self.assertIsNone(config.http_options.timeout)
        self.assertEqual(config.http_options.retry_options.attempts, 1)

    async def test_expired_timeout_stops_before_sdk_request(self) -> None:
        for timeout in (0, -0.1):
            with self.subTest(timeout=timeout):
                with self.assertRaises(asyncio.TimeoutError):
                    await generate_content(self.client, model="selected-model", contents="架空の入力", timeout=timeout)
        self.request.assert_not_awaited()

    async def test_asyncio_deadline_cancels_pending_request(self) -> None:
        canceled = asyncio.Event()

        async def request(**kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                canceled.set()

        self.request.side_effect = request
        with self.assertRaises(asyncio.TimeoutError):
            await generate_content(self.client, model="selected-model", contents="架空の入力", timeout=0.01)
        self.assertTrue(canceled.is_set())

    async def test_user_cancellation_propagates(self) -> None:
        self.request.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await generate_content(self.client, model="selected-model", contents="架空の入力", timeout=10)

    async def test_real_sdk_transport_has_no_server_or_quota_retries(self) -> None:
        for status in (429, 503):
            with self.subTest(status=status):
                calls = []

                async def handle(request):
                    calls.append(request)
                    return httpx.Response(status, json={"error": {"message": "架空のAPI障害", "code": status}})

                async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http_client:
                    client = genai.Client(api_key="YOUR_API_KEY", vertexai=False, enterprise=False,
                                          http_options=types.HttpOptions(httpx_async_client=http_client))
                    try:
                        with self.assertRaises(errors.APIError):
                            await generate_content(client, model="selected-model", contents="架空の入力", timeout=1)
                        self.assertEqual(len(calls), 1)
                    finally:
                        await client.aio.aclose()
                        client.close()

    async def test_factory_transport_does_not_retry_connection_failures_with_aiohttp_installed(self) -> None:
        calls = []

        async def handle(request):
            calls.append(request)
            raise httpx.ConnectError("架空の通信障害", request=request)

        with patch.object(genai_client.httpx, "AsyncHTTPTransport", return_value=httpx.MockTransport(handle)):
            client = create_genai_client("YOUR_API_KEY")
        try:
            with patch("google.genai._api_client.asyncio.sleep", side_effect=AssertionError("SDK内の再送は禁止です。")):
                with self.assertRaises(httpx.ConnectError):
                    await generate_content(client, model="selected-model", contents="架空の入力", timeout=1)
            self.assertEqual(len(calls), 1)
        finally:
            await close_genai_client(client)

    async def test_close_reclaims_owned_httpx_even_when_sdk_async_close_fails(self) -> None:
        for error in (None, RuntimeError("架空の終了エラー")):
            with self.subTest(error=error):
                client = Mock(aio=SimpleNamespace(aclose=AsyncMock(side_effect=error)))
                http_client = Mock(spec=httpx.AsyncClient, aclose=AsyncMock())
                with patch.object(genai_client.httpx, "AsyncClient", return_value=http_client), \
                        patch.object(genai_client.genai, "Client", return_value=client):
                    self.assertIs(create_genai_client("YOUR_API_KEY"), client)
                if error is not None:
                    with self.assertRaises(RuntimeError):
                        await close_genai_client(client)
                    client.aio.aclose.side_effect = None
                else:
                    await close_genai_client(client)
                await close_genai_client(client)
                http_client.aclose.assert_awaited_once()
                self.assertEqual(client.close.call_count, 2)

    async def test_close_reclaims_owned_httpx_when_sdk_sync_close_fails(self) -> None:
        client = Mock(aio=SimpleNamespace(aclose=AsyncMock()), close=Mock(side_effect=RuntimeError("架空の終了エラー")))
        http_client = Mock(spec=httpx.AsyncClient, aclose=AsyncMock())
        with patch.object(genai_client.httpx, "AsyncClient", return_value=http_client), \
                patch.object(genai_client.genai, "Client", return_value=client):
            create_genai_client("YOUR_API_KEY")
        with self.assertRaises(RuntimeError):
            await close_genai_client(client)
        http_client.aclose.assert_awaited_once()

    async def test_real_factory_closes_owned_httpx_and_transport_once(self) -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
        transport.aclose = AsyncMock(wraps=transport.aclose)
        with patch.object(genai_client.httpx, "AsyncHTTPTransport", return_value=transport):
            client = create_genai_client("YOUR_API_KEY")
        owned = genai_client._owned_http_clients[client]
        self.assertFalse(owned.is_closed)
        await close_genai_client(client)
        self.assertTrue(owned.is_closed)
        await close_genai_client(client)
        transport.aclose.assert_awaited_once()

    async def test_failed_factory_reclaims_unused_httpx_on_running_loop(self) -> None:
        http_client = Mock(spec=httpx.AsyncClient, aclose=AsyncMock())
        with patch.object(genai_client.httpx, "AsyncClient", return_value=http_client), \
                patch.object(genai_client.genai, "Client", side_effect=ValueError("架空の初期化エラー")):
            with self.assertRaises(ValueError):
                create_genai_client("YOUR_API_KEY")
        await asyncio.sleep(0)
        http_client.aclose.assert_awaited_once()


class GenaiFactoryFailureTests(unittest.TestCase):
    def test_failed_factory_reclaims_unused_httpx_without_running_loop(self) -> None:
        http_client = Mock(spec=httpx.AsyncClient, aclose=AsyncMock())
        with patch.object(genai_client.httpx, "AsyncClient", return_value=http_client), \
                patch.object(genai_client.genai, "Client", side_effect=ValueError("架空の初期化エラー")):
            with self.assertRaises(ValueError):
                create_genai_client("YOUR_API_KEY")
        http_client.aclose.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
