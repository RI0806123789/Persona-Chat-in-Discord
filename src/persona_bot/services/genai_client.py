"""Gemini Developer API のクライアントと非同期生成の共通境界。"""

import asyncio
from math import ceil
from typing import Any
from weakref import WeakKeyDictionary

from google import genai
from google.genai import types
import httpx

from persona_bot.core.background import spawn


class _UploadStatusTransport(httpx.AsyncBaseTransport):
    """不完全な転送応答を明示的に失敗させ、SDK内の同期sleepを避ける。"""

    def __init__(self, transport: httpx.AsyncBaseTransport) -> None:
        self._transport = transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._transport.handle_async_request(request)
        commands = request.headers.get("x-goog-upload-command", "").split(",")
        if "upload" in [command.strip() for command in commands] and not response.headers.get("x-goog-upload-status"):
            await response.aclose()
            raise httpx.RemoteProtocolError("Geminiファイル転送の状態ヘッダーを取得できませんでした。", request=request)
        return response

    async def aclose(self) -> None:
        await self._transport.aclose()


_owned_http_clients: WeakKeyDictionary[genai.Client, httpx.AsyncClient] = WeakKeyDictionary()


def create_genai_client(api_key: str) -> genai.Client:
    """Developer APIとHTTPXを明示し、生成・転送の暗黙リトライを防ぐ。"""
    http_client = httpx.AsyncClient(transport=_UploadStatusTransport(httpx.AsyncHTTPTransport(retries=0)))
    try:
        client = genai.Client(
            api_key=api_key,
            vertexai=False,
            enterprise=False,
            http_options=types.HttpOptions(
                httpx_async_client=http_client,
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
    except BaseException:
        # 初期化時には未通信だが、作成済みのトランスポートも回収する。
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(http_client.aclose())
        else:
            spawn(http_client.aclose(), label="Geminiクライアント初期化失敗時の回収")
        raise
    _owned_http_clients[client] = http_client
    return client


async def close_genai_client(client: Any) -> None:
    """SDKが所有しないHTTPXも、SDK終了が失敗した場合を含め一度だけ回収する。"""
    http_client = _owned_http_clients.pop(client, None)
    try:
        try:
            await client.aio.aclose()
        finally:
            client.close()
    finally:
        if http_client is not None:
            await http_client.aclose()


async def generate_content(
    client: Any,
    *,
    model: str,
    contents: Any,
    timeout: float | None = None,
    **options: Any,
) -> Any:
    """残り秒数をSDKのミリ秒へ変換し、通信全体にも同じ期限を適用する。"""
    if timeout is not None and timeout <= 0:
        raise asyncio.TimeoutError()
    config = types.GenerateContentConfig(
        http_options=types.HttpOptions(
            timeout=ceil(timeout * 1000) if timeout is not None else None,
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
        **options,
    )
    request = client.aio.models.generate_content(model=model, contents=contents, config=config)
    return await asyncio.wait_for(request, timeout=timeout) if timeout is not None else await request
