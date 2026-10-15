"""生成全体の締切・検索フォールバック・既存の応答形式を検証する。"""

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx
import gemini_service
from gemini_service import GeminiService


class UnsupportedSearchError(Exception):
    code = 400


class ServerError(Exception):
    code = 503


class GeminiGenerationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.now = 100.0
        clock = patch.object(gemini_service, "monotonic", side_effect=lambda: self.now, create=True)
        clock.start()
        self.addCleanup(clock.stop)
        self.client = MagicMock(spec=httpx.AsyncClient)
        self.client.__aenter__ = AsyncMock(return_value=self.client)
        self.client.__aexit__ = AsyncMock(return_value=False)
        self.client.post = AsyncMock(return_value=self.rest_response())
        factory = patch.object(gemini_service.httpx, "AsyncClient", return_value=self.client)
        self.client_factory = factory.start()
        self.addCleanup(factory.stop)
        self.sdk_response = SimpleNamespace(
            text="SDKの架空の応答", usage_metadata={"total_token_count": 7}, candidates=[],
        )
        self.sdk_request = AsyncMock(return_value=self.sdk_response)
        self.genai = SimpleNamespace(
            GenerativeModel=Mock(return_value=SimpleNamespace(generate_content_async=self.sdk_request)),
            protos=SimpleNamespace(GoogleSearch=Mock(), Tool=Mock(return_value="検索ツール")),
        )
        self.service = GeminiService(self.genai, "YOUR_GEMINI_API_KEY")

    def rest_response(self, status: int = 200, message: str = "") -> httpx.Response:
        if status != 200:
            return httpx.Response(status, json={"error": {"message": message}})
        return httpx.Response(200, json={
            "candidates": [{
                "content": {"parts": [{"text": "架空の"}, {"text": "検索応答"}]},
                "groundingMetadata": {"groundingChunks": [
                    {"web": {"title": " 架空の資料 ", "uri": " https://example.com/source "}},
                ]},
            }],
            "usageMetadata": {"totalTokenCount": 11},
        })

    def unsupported_response(self) -> httpx.Response:
        return self.rest_response(400, "google_search is not supported for this model")

    async def test_rest_success_preserves_selected_model_sources_and_usage(self) -> None:
        result = await self.service.generate("架空の質問", "selected-model", timeout=30, grounding=True)
        self.assertEqual(result, (
            "架空の検索応答", {"totalTokenCount": 11},
            [{"title": "架空の資料", "uri": "https://example.com/source"}],
        ))
        self.assertIn("/selected-model:generateContent", self.client.post.call_args.args[0])
        self.genai.GenerativeModel.assert_not_called()

    async def test_each_rest_attempt_receives_only_remaining_time_and_reuses_client(self) -> None:
        async def post(*args, **kwargs):
            self.now += 12
            return self.unsupported_response() if self.client.post.await_count == 1 else self.rest_response()

        self.client.post.side_effect = post
        await self.service.generate("架空の質問", "selected-model", timeout=30, grounding=True)
        self.assertEqual([call.kwargs["timeout"] for call in self.client.post.call_args_list], [30, 18])
        self.client_factory.assert_called_once()

    async def test_rest_and_both_sdk_fallbacks_share_one_budget_and_selected_model(self) -> None:
        async def post(*args, **kwargs):
            self.now += 8
            return self.unsupported_response()

        async def generate(*args, **kwargs):
            self.now += 3
            if self.sdk_request.await_count == 1:
                raise UnsupportedSearchError("google_search is not supported for this model")
            return self.sdk_response

        self.client.post.side_effect = post
        self.sdk_request.side_effect = generate
        original_wait_for = asyncio.wait_for
        with patch.object(gemini_service.asyncio, "wait_for", wraps=original_wait_for) as waiting:
            result = await self.service.generate("架空の質問", "selected-model", timeout=30, grounding=True)
        self.assertEqual(result[0], "SDKの架空の応答")
        self.assertEqual([call.kwargs["timeout"] for call in self.client.post.call_args_list], [30, 22])
        timeouts = [call.kwargs["timeout"] for call in waiting.call_args_list]
        self.assertIn(14, timeouts)
        self.assertIn(11, timeouts)
        self.assertEqual([call.kwargs["request_options"] for call in self.sdk_request.call_args_list],
                         [{"timeout": 14, "retry": None}, {"timeout": 11, "retry": None}])
        self.assertEqual([call.args[0] for call in self.genai.GenerativeModel.call_args_list],
                         ["selected-model", "selected-model"])

    async def test_expired_budget_does_not_start_another_attempt(self) -> None:
        async def post(*args, **kwargs):
            self.now += 30
            return self.unsupported_response()

        self.client.post.side_effect = post
        self.assertEqual(await self.service.generate("質問", "selected-model", timeout=30, grounding=True),
                         (None, None, []))
        self.client.post.assert_awaited_once()
        self.sdk_request.assert_not_awaited()

    async def test_zero_and_negative_timeout_do_not_send_requests(self) -> None:
        for timeout in (0, -1):
            with self.subTest(timeout=timeout):
                self.assertEqual(await self.service.generate("質問", "selected-model", timeout=timeout, grounding=True),
                                 (None, None, []))
        self.client.post.assert_not_awaited()
        self.sdk_request.assert_not_awaited()

    async def test_default_grounding_budget_uses_configured_constant(self) -> None:
        with patch.object(gemini_service, "GROUNDING_REQUEST_TIMEOUT_SECONDS", 17, create=True):
            await self.service.generate("質問", "selected-model", grounding=True)
        self.assertEqual(self.client.post.call_args.kwargs["timeout"], 17)

    async def test_rest_timeout_does_not_retry(self) -> None:
        for error in (asyncio.TimeoutError(), httpx.ReadTimeout("架空の読み込み期限切れ")):
            with self.subTest(error=type(error).__name__):
                self.client.post.reset_mock()
                self.client.post.side_effect = error
                self.assertEqual(await self.service.generate("質問", "selected-model", timeout=30, grounding=True),
                                 (None, None, []))
                self.client.post.assert_awaited_once()
                self.sdk_request.assert_not_awaited()

    async def test_server_auth_quota_and_unrelated_bad_request_do_not_retry(self) -> None:
        for status, message in (
            (500, "一時的な障害"), (503, "一時的な障害"),
            (401, "認証失敗"), (403, "権限なし"), (429, "利用枠不足"),
            (400, "Invalid input text"), (404, "model not found"),
        ):
            with self.subTest(status=status):
                self.client.post.reset_mock()
                self.client.post.return_value = self.rest_response(status, message)
                self.assertEqual(await self.service.generate("質問", "selected-model", timeout=30, grounding=True),
                                 (None, None, []))
                self.client.post.assert_awaited_once()
                self.sdk_request.assert_not_awaited()

    async def test_retry_setting_disables_submodel_but_keeps_selected_sdk_model(self) -> None:
        self.client.post.return_value = self.unsupported_response()
        with patch.object(gemini_service, "GROUNDING_SUBMODEL_RETRY_ENABLED", False):
            result = await self.service.generate("質問", "selected-model", timeout=30, grounding=True)
        self.assertEqual(result[0], "SDKの架空の応答")
        self.client.post.assert_awaited_once()
        self.assertEqual(self.genai.GenerativeModel.call_args.args[0], "selected-model")

    async def test_grounding_submodel_is_not_requested_twice(self) -> None:
        self.client.post.return_value = self.unsupported_response()
        await self.service.generate("質問", gemini_service.GROUNDING_MODEL_NAME, timeout=30, grounding=True)
        self.client.post.assert_awaited_once()
        self.sdk_request.assert_awaited_once()

    async def test_sdk_timeout_and_server_error_do_not_retry_without_grounding(self) -> None:
        for error in (asyncio.TimeoutError(), ServerError("架空のAPI障害")):
            with self.subTest(error=type(error).__name__):
                self.sdk_request.reset_mock()
                self.sdk_request.side_effect = error
                self.assertEqual(await self.service.generate("質問", "selected-model", images=["架空の画像"],
                                                            timeout=30, grounding=True), (None, None, []))
                self.sdk_request.assert_awaited_once()
        self.client.post.assert_not_awaited()

    async def test_images_and_sdk_grounding_sources_are_preserved(self) -> None:
        self.sdk_response.candidates = [SimpleNamespace(grounding_metadata=SimpleNamespace(
            grounding_chunks=[SimpleNamespace(web=SimpleNamespace(title="資料", uri="https://example.com/source"))],
        ))]
        image = object()
        result = await self.service.generate("質問", "selected-model", images=[image], timeout=30, grounding=True)
        self.assertEqual(self.sdk_request.call_args.args[0], ["質問", image])
        self.assertEqual(result, ("SDKの架空の応答", {"total_token_count": 7},
                                  [{"title": "資料", "uri": "https://example.com/source"}]))
        self.client.post.assert_not_awaited()

    async def test_plain_sdk_generation_keeps_existing_response_shape(self) -> None:
        self.assertEqual(await self.service.generate("質問", "selected-model", timeout=30),
                         ("SDKの架空の応答", {"total_token_count": 7}, []))
        self.sdk_request.assert_awaited_once_with(["質問"], request_options={"timeout": 30, "retry": None})
        self.client.post.assert_not_awaited()

    async def test_sdk_without_explicit_timeout_keeps_sdk_default_timeout(self) -> None:
        result = await self.service.generate("質問", "selected-model")
        self.assertEqual(result[0], "SDKの架空の応答")
        self.sdk_request.assert_awaited_once_with(["質問"], request_options={"retry": None})

    async def test_response_arriving_after_deadline_is_not_returned_as_success(self) -> None:
        async def post(*args, **kwargs):
            self.now += 31
            return self.rest_response()

        self.client.post.side_effect = post
        self.assertEqual(await self.service.generate("質問", "selected-model", timeout=30, grounding=True),
                         (None, None, []))
        self.client.post.assert_awaited_once()
        self.sdk_request.assert_not_awaited()

    async def test_cancellation_propagates_and_does_not_start_fallback(self) -> None:
        for images in (None, ["架空の画像"]):
            with self.subTest(images=images):
                self.client.post.reset_mock()
                self.sdk_request.reset_mock()
                self.client.post.side_effect = asyncio.CancelledError()
                self.sdk_request.side_effect = asyncio.CancelledError()
                with self.assertRaises(asyncio.CancelledError):
                    await self.service.generate("質問", "selected-model", images=images, timeout=30, grounding=True)
                self.assertEqual(self.client.post.await_count + self.sdk_request.await_count, 1)

    async def test_total_timeout_covers_client_opening_before_any_rest_request(self) -> None:
        opening_canceled = asyncio.Event()

        async def open_client():
            try:
                await asyncio.Event().wait()
            finally:
                opening_canceled.set()

        self.client.__aenter__.side_effect = open_client
        result = await asyncio.wait_for(
            self.service.generate("質問", "selected-model", timeout=0.01, grounding=True), timeout=0.5,
        )
        self.assertEqual(result, (None, None, []))
        self.assertTrue(opening_canceled.is_set())
        self.client.post.assert_not_awaited()
        self.sdk_request.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
