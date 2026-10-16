"""新SDKで生成期限・検索・画像・文書の既存動作を検証する。"""

import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from google import genai
from google.genai import errors, types
import httpx
from PIL import Image

from persona_bot.services import gemini_service
from persona_bot.services.gemini_service import GeminiService
from persona_bot.services.genai_client import close_genai_client, create_genai_client


def api_error(code: int, message: str) -> errors.APIError:
    error_type = errors.ServerError if code >= 500 else errors.ClientError
    return error_type(code, {"error": {"message": message}})


def unsupported_search() -> errors.APIError:
    return api_error(400, "google_search is not supported for this model")


class GeminiGenerationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.now = 100.0
        # 旧REST経路を含め、移行前の失敗確認でも外部通信を発生させない。
        rest_guard = patch("httpx.AsyncClient.post", new_callable=AsyncMock,
                           side_effect=AssertionError("外部通信は禁止です。"))
        rest_guard.start()
        self.addCleanup(rest_guard.stop)
        clock = patch.object(gemini_service, "monotonic", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.response = types.GenerateContentResponse(
            candidates=[types.Candidate(content=types.Content(parts=[types.Part(text="架空の応答")]))],
            usage_metadata=types.GenerateContentResponseUsageMetadata(total_token_count=7),
        )
        self.sdk_request = AsyncMock(return_value=self.response)
        self.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=self.sdk_request)))
        self.service = GeminiService(self.client)

    def timeouts_ms(self) -> list[int | None]:
        return [call.kwargs["config"].http_options.timeout for call in self.sdk_request.call_args_list]

    def models(self) -> list[str]:
        return [call.kwargs["model"] for call in self.sdk_request.call_args_list]

    def search_flags(self) -> list[bool]:
        return [bool(call.kwargs["config"].tools) for call in self.sdk_request.call_args_list]

    async def test_search_preserves_selected_model_sources_and_usage(self) -> None:
        self.response.candidates[0].grounding_metadata = types.GroundingMetadata(
            grounding_chunks=[types.GroundingChunk(web=types.GroundingChunkWeb(
                title=" 架空の資料 ", uri=" https://example.com/source ",
            ))],
        )
        result = await self.service.generate("架空の質問", "selected-model", timeout=30, grounding=True)
        self.assertEqual(result, ("架空の応答", self.response.usage_metadata,
                                  [{"title": "架空の資料", "uri": "https://example.com/source"}]))
        self.assertEqual(self.models(), ["selected-model"])
        self.assertIsInstance(self.sdk_request.call_args.kwargs["config"].tools[0].google_search, types.GoogleSearch)

    async def test_search_fallbacks_share_budget_and_restore_selected_model(self) -> None:
        async def generate(**kwargs):
            self.now += 8
            if self.sdk_request.await_count < 3:
                raise unsupported_search()
            return self.response

        self.sdk_request.side_effect = generate
        result = await self.service.generate("質問", "selected-model", timeout=30, grounding=True)
        self.assertEqual(result[0], "架空の応答")
        self.assertEqual(self.models(), ["selected-model", gemini_service.GROUNDING_MODEL_NAME, "selected-model"])
        self.assertEqual(self.timeouts_ms(), [30000, 22000, 14000])
        self.assertEqual(self.search_flags(), [True, True, False])
        self.assertTrue(all(call.kwargs["config"].http_options.retry_options.attempts == 1
                            for call in self.sdk_request.call_args_list))

    async def test_submodel_success_finishes_without_plain_generation(self) -> None:
        self.sdk_request.side_effect = [unsupported_search(), self.response]
        result = await self.service.generate("質問", "selected-model", timeout=30, grounding=True)
        self.assertEqual(result[0], "架空の応答")
        self.assertEqual(self.models(), ["selected-model", gemini_service.GROUNDING_MODEL_NAME])
        self.assertEqual(self.search_flags(), [True, True])

    async def test_expired_budget_does_not_start_another_attempt(self) -> None:
        async def generate(**kwargs):
            self.now += 30
            raise unsupported_search()

        self.sdk_request.side_effect = generate
        self.assertEqual(await self.service.generate("質問", "selected-model", timeout=30, grounding=True),
                         (None, None, []))
        self.sdk_request.assert_awaited_once()

    async def test_zero_and_negative_timeout_do_not_send_requests(self) -> None:
        for timeout in (0, -1):
            with self.subTest(timeout=timeout):
                self.assertEqual(await self.service.generate("質問", "selected-model", timeout=timeout, grounding=True),
                                 (None, None, []))
        self.sdk_request.assert_not_awaited()

    async def test_default_grounding_budget_uses_configured_constant(self) -> None:
        with patch.object(gemini_service, "GROUNDING_REQUEST_TIMEOUT_SECONDS", 17):
            await self.service.generate("質問", "selected-model", grounding=True)
        self.assertEqual(self.timeouts_ms(), [17000])

    async def test_timeouts_and_connection_errors_do_not_retry(self) -> None:
        for error in (asyncio.TimeoutError(), httpx.ReadTimeout("架空の期限切れ"), httpx.ConnectError("架空の通信障害")):
            with self.subTest(error=type(error).__name__):
                self.sdk_request.reset_mock()
                self.sdk_request.side_effect = error
                self.assertEqual(await self.service.generate("質問", "selected-model", timeout=30, grounding=True),
                                 (None, None, []))
                self.sdk_request.assert_awaited_once()

    async def test_server_auth_quota_and_unrelated_bad_requests_do_not_retry(self) -> None:
        for code, message in ((500, "障害"), (503, "障害"), (401, "認証失敗"), (403, "権限なし"),
                              (429, "利用枠不足"), (400, "Invalid input text"), (404, "model not found")):
            with self.subTest(code=code):
                self.sdk_request.reset_mock()
                self.sdk_request.side_effect = api_error(code, message)
                self.assertEqual(await self.service.generate("質問", "selected-model", timeout=30, grounding=True),
                                 (None, None, []))
                self.sdk_request.assert_awaited_once()

    async def test_submodel_failure_does_not_start_plain_fallback(self) -> None:
        self.sdk_request.side_effect = [unsupported_search(), api_error(503, "架空の障害")]
        self.assertEqual(await self.service.generate("質問", "selected-model", timeout=30, grounding=True),
                         (None, None, []))
        self.assertEqual(self.sdk_request.await_count, 2)

    async def test_retry_setting_disables_submodel_and_keeps_selected_model(self) -> None:
        self.sdk_request.side_effect = [unsupported_search(), self.response]
        with patch.object(gemini_service, "GROUNDING_SUBMODEL_RETRY_ENABLED", False):
            result = await self.service.generate("質問", "selected-model", timeout=30, grounding=True)
        self.assertEqual(result[0], "架空の応答")
        self.assertEqual(self.models(), ["selected-model", "selected-model"])
        self.assertEqual(self.search_flags(), [True, False])

    async def test_grounding_submodel_is_not_requested_twice(self) -> None:
        self.sdk_request.side_effect = [unsupported_search(), self.response]
        await self.service.generate("質問", gemini_service.GROUNDING_MODEL_NAME, timeout=30, grounding=True)
        self.assertEqual(self.models(), [gemini_service.GROUNDING_MODEL_NAME] * 2)
        self.assertEqual(self.search_flags(), [True, False])

    async def test_images_are_preserved_in_all_fallbacks(self) -> None:
        image = object()
        self.sdk_request.side_effect = [unsupported_search(), unsupported_search(), self.response]
        result = await self.service.generate("質問", "selected-model", images=[image], timeout=30, grounding=True)
        self.assertEqual(result[0], "架空の応答")
        self.assertTrue(all(call.kwargs["contents"] == ["質問", image] for call in self.sdk_request.call_args_list))

    async def test_plain_generation_keeps_response_shape_and_no_tools(self) -> None:
        self.assertEqual(await self.service.generate("質問", "selected-model", timeout=30),
                         ("架空の応答", self.response.usage_metadata, []))
        self.assertEqual(self.models(), ["selected-model"])
        self.assertEqual(self.search_flags(), [False])
        self.assertEqual(self.timeouts_ms(), [30000])

    async def test_plain_generation_does_not_retry_search_errors(self) -> None:
        self.sdk_request.side_effect = unsupported_search()
        self.assertEqual(await self.service.generate("質問", "selected-model", timeout=30), (None, None, []))
        self.sdk_request.assert_awaited_once()

    async def test_without_explicit_timeout_keeps_sdk_default_timeout(self) -> None:
        result = await self.service.generate("質問", "selected-model")
        self.assertEqual(result[0], "架空の応答")
        self.assertEqual(self.timeouts_ms(), [None])

    async def test_response_arriving_after_deadline_is_discarded(self) -> None:
        async def generate(**kwargs):
            self.now += 31
            return self.response

        self.sdk_request.side_effect = generate
        self.assertEqual(await self.service.generate("質問", "selected-model", timeout=30, grounding=True),
                         (None, None, []))
        self.sdk_request.assert_awaited_once()

    async def test_cancellation_propagates_without_fallback(self) -> None:
        for images in (None, ["架空の画像"]):
            with self.subTest(images=images):
                self.sdk_request.reset_mock()
                self.sdk_request.side_effect = asyncio.CancelledError()
                with self.assertRaises(asyncio.CancelledError):
                    await self.service.generate("質問", "selected-model", images=images, timeout=30, grounding=True)
                self.sdk_request.assert_awaited_once()

    async def test_total_timeout_cancels_pending_request(self) -> None:
        canceled = asyncio.Event()

        async def generate(**kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                canceled.set()

        self.sdk_request.side_effect = generate
        result = await asyncio.wait_for(self.service.generate("質問", "selected-model", timeout=0.01, grounding=True),
                                        timeout=0.5)
        self.assertEqual(result, (None, None, []))
        self.assertTrue(canceled.is_set())
        self.sdk_request.assert_awaited_once()

    async def test_empty_grounding_metadata_and_source_limit_are_safe(self) -> None:
        for metadata in (None, types.GroundingMetadata(), types.GroundingMetadata(grounding_chunks=[
            types.GroundingChunk(web=types.GroundingChunkWeb(title=None, uri="  ")),
            *[types.GroundingChunk(web=types.GroundingChunkWeb(uri=f"https://example.com/{index}"))
              for index in range(10)],
        ])):
            with self.subTest(metadata=metadata):
                self.response.candidates[0].grounding_metadata = metadata
                result = await self.service.generate("質問", "selected-model", timeout=30, grounding=True)
                self.assertTrue(all(source["uri"] for source in result[2]))
                self.assertLessEqual(len(result[2]), gemini_service.MAX_GROUNDING_SOURCES)


class GeminiDocumentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.uploaded = types.File(name="files/example", uri="https://example.com/file", mime_type="application/pdf")
        self.files = SimpleNamespace(upload=AsyncMock(return_value=self.uploaded), delete=AsyncMock())
        response = types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(parts=[
            types.Part(text='{"summary":" 架空の要約 ","key_points":["要点"],"warnings":"不正な型"}')
        ]))])
        self.generate = AsyncMock(return_value=response)
        self.client = SimpleNamespace(aio=SimpleNamespace(files=self.files, models=SimpleNamespace(generate_content=self.generate)))
        self.service = GeminiService(self.client)

    async def test_document_upload_uses_async_files_and_explicit_mime(self) -> None:
        for extension, mime in ((".pdf", "application/pdf"), (".PDF", "application/pdf"),
                                (".txt", "text/plain"), (".md", "text/plain"), (".csv", "text/plain")):
            with self.subTest(extension=extension):
                self.files.upload.reset_mock()
                path = Path("example" + extension)
                self.assertIs(await self.service.upload_file(path), self.uploaded)
                self.assertEqual(self.files.upload.call_args.kwargs["file"], path)
                config = self.files.upload.call_args.kwargs["config"]
                self.assertEqual(config.mime_type, mime)
                self.assertEqual(config.http_options.timeout, int(gemini_service.DOCUMENT_UPLOAD_TIMEOUT_SECONDS * 1000))
                self.assertEqual(config.http_options.retry_options.attempts, 1)

    async def test_upload_timeout_cancels_request(self) -> None:
        canceled = asyncio.Event()

        async def upload(**kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                canceled.set()

        self.files.upload.side_effect = upload
        with patch.object(gemini_service, "DOCUMENT_UPLOAD_TIMEOUT_SECONDS", 0.01):
            with self.assertRaises(asyncio.TimeoutError):
                await self.service.upload_file(Path("example.pdf"))
        self.assertTrue(canceled.is_set())

    async def test_document_summary_passes_file_and_json_config(self) -> None:
        result = await self.service.summarize_document(self.uploaded, "example.pdf", "selected-model")
        self.assertEqual(result, {"document_name": "example.pdf", "summary": "架空の要約", "key_points": ["要点"],
                                  "warnings": [], "usage_notes": []})
        kwargs = self.generate.call_args.kwargs
        self.assertEqual(kwargs["model"], "selected-model")
        self.assertIs(kwargs["contents"][1], self.uploaded)
        self.assertEqual(kwargs["config"].temperature, 0)
        self.assertEqual(kwargs["config"].response_mime_type, "application/json")
        self.assertEqual(kwargs["config"].http_options.timeout, int(gemini_service.DOCUMENT_SUMMARY_TIMEOUT_SECONDS * 1000))

    async def test_invalid_or_empty_summary_raises(self) -> None:
        for text in ("JSONではない", "{}", '{"summary":" "}'):
            with self.subTest(text=text):
                self.generate.return_value = SimpleNamespace(text=text, candidates=[])
                with self.assertRaises(ValueError):
                    await self.service.summarize_document(self.uploaded, "example.pdf", "selected-model")

    async def test_delete_uses_async_file_name_and_skips_nameless_files(self) -> None:
        await self.service.delete_file(SimpleNamespace(name=None))
        self.files.delete.assert_not_awaited()
        await self.service.delete_file(self.uploaded)
        self.files.delete.assert_awaited_once()
        self.assertEqual(self.files.delete.call_args.kwargs["name"], "files/example")

    async def test_delete_errors_are_logged_and_cancellation_propagates(self) -> None:
        self.files.delete.side_effect = api_error(503, "架空の削除エラー")
        await self.service.delete_file(self.uploaded)
        self.files.delete.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.service.delete_file(self.uploaded)


class GeminiSdkTransportTests(unittest.IsolatedAsyncioTestCase):
    """実SDKのJSON変換とFilesの転送を、ネットワークを使わず確認する。"""

    def sdk_client(self, http_client: httpx.AsyncClient) -> genai.Client:
        return genai.Client(api_key="YOUR_API_KEY", vertexai=False, enterprise=False,
                            http_options=types.HttpOptions(httpx_async_client=http_client,
                                                          retry_options=types.HttpRetryOptions(attempts=1)))

    async def test_pil_image_and_google_search_reach_sdk_request(self) -> None:
        requests = []

        async def handle(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={
                "candidates": [{"content": {"parts": [{"text": "画像の応答"}]},
                                "groundingMetadata": {"groundingChunks": [{"web": {
                                    "title": "資料", "uri": "https://example.com/source",
                                }}]}}],
                "usageMetadata": {"totalTokenCount": 9},
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http_client:
            client = self.sdk_client(http_client)
            try:
                with Image.new("RGB", (2, 2), "white") as image:
                    result = await GeminiService(client).generate("架空の画像", "selected-model", images=[image],
                                                                  timeout=10, grounding=True)
                self.assertEqual(result[0], "画像の応答")
                self.assertEqual(result[1].total_token_count, 9)
                self.assertEqual(result[2], [{"title": "資料", "uri": "https://example.com/source"}])
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0]["tools"], [{"googleSearch": {}}])
                parts = requests[0]["contents"][0]["parts"]
                self.assertEqual(parts[0], {"text": "架空の画像"})
                self.assertEqual(types.Blob.model_validate(parts[1]["inlineData"]).mime_type, "image/png")
                self.assertTrue(parts[1]["inlineData"]["data"])
            finally:
                await client.aio.aclose()
                client.close()

    async def test_real_sdk_search_errors_follow_only_supported_fallback_path(self) -> None:
        requests = []

        async def handle(request):
            requests.append((request.url.path, json.loads(request.content)))
            if len(requests) < 3:
                return httpx.Response(400, json={"error": {"code": 400,
                                                           "message": "google_search is not supported"}})
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "架空の応答"}]}}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http_client:
            client = self.sdk_client(http_client)
            try:
                result = await GeminiService(client).generate("架空の質問", "selected-model", timeout=10, grounding=True)
                self.assertEqual(result[0], "架空の応答")
                self.assertEqual([path.split("/")[-1] for path, _ in requests], [
                    "selected-model:generateContent", f"{gemini_service.GROUNDING_MODEL_NAME}:generateContent",
                    "selected-model:generateContent",
                ])
                self.assertEqual([bool(body.get("tools")) for _, body in requests], [True, True, False])
            finally:
                await client.aio.aclose()
                client.close()

    async def test_files_upload_summary_delete_are_serialized_by_real_sdk(self) -> None:
        for extension, mime in ((".pdf", "application/pdf"), (".txt", "text/plain"),
                                (".md", "text/plain"), (".csv", "text/plain")):
            with self.subTest(extension=extension), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / ("example" + extension)
                payload = b"%PDF-1.4\n" if extension == ".pdf" else "架空の文書".encode("utf-8")
                path.write_bytes(payload)
                requests = []

                async def handle(request):
                    requests.append(request)
                    if request.url.path == "/upload/v1beta/files":
                        return httpx.Response(200, json={}, headers={"x-goog-upload-url": "https://example.com/upload"})
                    if request.url.path == "/upload":
                        return httpx.Response(200, headers={"x-goog-upload-status": "final"}, json={"file": {
                            "name": "files/example", "uri": "https://example.com/file", "mimeType": mime,
                            "state": "ACTIVE",
                        }})
                    if request.url.path.endswith(":generateContent"):
                        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{
                            "text": '{"summary":"架空の要約"}',
                        }]}}]})
                    if request.method == "DELETE" and request.url.path == "/v1beta/files/example":
                        return httpx.Response(200, json={})
                    raise AssertionError(f"想定しない模擬リクエストです: {request.method} {request.url.path}")

                async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http_client:
                    client = self.sdk_client(http_client)
                    service = GeminiService(client)
                    try:
                        uploaded = await service.upload_file(path)
                        summary = await service.summarize_document(uploaded, path.name, "selected-model")
                        await service.delete_file(uploaded)
                        self.assertEqual(summary["summary"], "架空の要約")
                        self.assertEqual(len(requests), 4)
                        self.assertEqual(requests[0].headers["X-Goog-Upload-Header-Content-Type"], mime)
                        self.assertEqual(types.File.model_validate(json.loads(requests[0].content)["file"]).mime_type, mime)
                        self.assertEqual(requests[1].content, payload)
                        self.assertEqual(requests[1].headers["X-Goog-Upload-Command"], "upload, finalize")
                        body = json.loads(requests[2].content)
                        file_data = types.FileData.model_validate(body["contents"][0]["parts"][1]["fileData"])
                        self.assertEqual(file_data.file_uri, "https://example.com/file")
                        self.assertEqual(file_data.mime_type, mime)
                        self.assertEqual(body["generationConfig"]["responseMimeType"], "application/json")
                        self.assertEqual(body["generationConfig"]["temperature"], 0)
                    finally:
                        await client.aio.aclose()
                        client.close()

    async def test_missing_upload_status_fails_once_without_sdk_sleep_and_closes_response(self) -> None:
        class TrackedStream(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                yield b'{"error":{"code":503,"message":"example"}}'

            async def aclose(self):
                self.closed = True

        stream = TrackedStream()
        calls = []

        async def handle(request):
            calls.append(request)
            if request.headers.get("x-goog-upload-command") == "start":
                return httpx.Response(200, json={}, headers={"x-goog-upload-url": "https://example.com/upload"})
            return httpx.Response(503, stream=stream)

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "example.txt"
            path.write_text("架空の文書", encoding="utf-8")
            with patch("persona_bot.services.genai_client.httpx.AsyncHTTPTransport", return_value=httpx.MockTransport(handle)):
                client = create_genai_client("YOUR_API_KEY")
            try:
                with patch("google.genai._api_client.time.sleep", side_effect=AssertionError("同期sleepは禁止です。")), \
                        patch.object(gemini_service, "DOCUMENT_UPLOAD_TIMEOUT_SECONDS", 0.5):
                    with self.assertRaises(httpx.RemoteProtocolError):
                        await asyncio.wait_for(GeminiService(client).upload_file(path), timeout=1)
                self.assertEqual(len(calls), 2)
                self.assertTrue(stream.closed)
            finally:
                await close_genai_client(client)

    async def test_factory_upload_timeout_cancels_real_sdk_transfer(self) -> None:
        calls = []
        canceled = asyncio.Event()

        async def handle(request):
            calls.append(request)
            if request.headers.get("x-goog-upload-command") == "start":
                return httpx.Response(200, json={}, headers={"x-goog-upload-url": "https://example.com/upload"})
            try:
                await asyncio.Event().wait()
            finally:
                canceled.set()

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "example.txt"
            path.write_text("架空の文書", encoding="utf-8")
            with patch("persona_bot.services.genai_client.httpx.AsyncHTTPTransport", return_value=httpx.MockTransport(handle)):
                client = create_genai_client("YOUR_API_KEY")
            try:
                # SDKのローカル初期化・ファイル準備が終わり、転送待機へ到達できる余裕を持つ。
                with patch.object(gemini_service, "DOCUMENT_UPLOAD_TIMEOUT_SECONDS", 2.0):
                    with self.assertRaises(asyncio.TimeoutError):
                        await asyncio.wait_for(GeminiService(client).upload_file(path), timeout=4)
                self.assertEqual(len(calls), 2)
                self.assertTrue(canceled.is_set())
            finally:
                await close_genai_client(client)


if __name__ == "__main__":
    unittest.main()
