import asyncio
from pathlib import Path
from typing import Any, Sequence

import httpx

from app_state import BotState
from constants import DOCUMENT_SUMMARY_TIMEOUT_SECONDS, DOCUMENT_UPLOAD_TIMEOUT_SECONDS, GROUNDING_MODEL_NAME, MAX_GROUNDING_SOURCES
from storage import extract_json_object, safe_response_text

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiService:
    def __init__(self, state: BotState, genai_module: Any, api_key: str = "") -> None:
        self._state = state
        self._genai = genai_module
        self._api_key = api_key

    async def generate(
        self,
        prompt: str,
        images: Sequence[Any] | None = None,
        timeout: float | None = None,
        grounding: bool = False,
    ) -> tuple[str | None, Any | None, list[dict[str, str]]]:
        if grounding and not images:
            return await self._generate_with_grounding(prompt, timeout)
        # 画像あり時は REST 経路が使えないため、SDK 側のグラウンディングツールで対応する
        return await self._generate_with_sdk(prompt, images, timeout, grounding=grounding)

    def _build_grounding_tools(self) -> list[Any] | None:
        """SDK 経由のグラウンディング用ツールを構築する。

        gemini-2.x / 3.x は `google_search`、旧 gemini-1.5 系は
        `google_search_retrieval` を使う。SDK が対応している方を優先して選ぶ。
        """
        protos = getattr(self._genai, "protos", None)
        if protos is None:
            return None

        if hasattr(protos, "GoogleSearch"):
            try:
                return [protos.Tool(google_search=protos.GoogleSearch())]
            except Exception as error:
                print(f"grounding tool (google_search) 構築失敗: {error}")

        if hasattr(protos, "GoogleSearchRetrieval"):
            try:
                return [protos.Tool(google_search_retrieval=protos.GoogleSearchRetrieval())]
            except Exception as error:
                print(f"grounding tool (google_search_retrieval) 構築失敗: {error}")

        return None

    async def _generate_with_sdk(
        self,
        prompt: str,
        images: Sequence[Any] | None = None,
        timeout: float | None = None,
        grounding: bool = False,
    ) -> tuple[str | None, Any | None, list[dict[str, str]]]:
        """SDK 経由での生成（フォールバック時にもグラウンディングを維持）。"""
        try:
            tools = self._build_grounding_tools() if grounding else None
            model = self._genai.GenerativeModel(self._state.current_model_name, tools=tools)
            content: list[Any] = [prompt]
            if images:
                content.extend(images)

            try:
                request = model.generate_content_async(content)
                response = await asyncio.wait_for(request, timeout=timeout) if timeout else await request
            except Exception as inner_error:
                if grounding:
                    print(f"SDK grounding API error: {inner_error} - falling back to no-grounding")
                    model = self._genai.GenerativeModel(self._state.current_model_name)
                    request = model.generate_content_async(content)
                    response = await asyncio.wait_for(request, timeout=timeout) if timeout else await request
                else:
                    raise inner_error

            sources = []
            if grounding:
                try:
                    if hasattr(response, "candidates") and response.candidates:
                        candidate = response.candidates[0]
                        if hasattr(candidate, "grounding_metadata") and candidate.grounding_metadata:
                            chunks = getattr(candidate.grounding_metadata, "grounding_chunks", [])
                            for chunk in chunks[:MAX_GROUNDING_SOURCES]:
                                web = getattr(chunk, "web", None)
                                uri = getattr(web, "uri", "") if web is not None else ""
                                if uri:
                                    title = getattr(web, "title", "") or ""
                                    sources.append({"title": title, "uri": uri})
                except Exception as e:
                    print(f"SDK grounding parsing error: {e}")

            return safe_response_text(response), getattr(response, "usage_metadata", None), sources
        except Exception as error:
            print(f"Error occurred while asking Gemini: {error}")
            return None, None, []

    async def _generate_with_grounding(
        self,
        prompt: str,
        timeout: float | None = None,
        _retry_model: str | None = None,
    ) -> tuple[str | None, Any | None, list[dict[str, str]]]:
        """REST API 直接呼び出しによるグラウンディング付き生成。

        まずユーザーが選択中のモデルで試し、失敗したら既知のグラウンディング
        対応モデル(GROUNDING_MODEL_NAME)でリトライ、それでもダメなら SDK 生成に
        フォールバックする。
        """
        is_retry = _retry_model is not None
        model_name = _retry_model or self._state.current_model_name
        url = f"{GEMINI_API_BASE}/{model_name}:generateContent"
        print(f"🔍 Google検索グラウンディング: 有効（モデル: {model_name}）")

        body: dict[str, Any] = {
            "contents": [{"parts": [{"text": prompt}]}],
            "tools": [{"google_search": {}}],
        }

        async def _fallback() -> tuple[str | None, Any | None, list[dict[str, str]]]:
            # まだリトライしておらず、選択モデルが既知の対応モデルと異なる場合のみ
            # GROUNDING_MODEL_NAME で一度リトライ。それ以外は SDK にフォールバック。
            if not is_retry and model_name != GROUNDING_MODEL_NAME:
                print(f"   => サブモデル（{GROUNDING_MODEL_NAME}）でリトライします...")
                return await self._generate_with_grounding(prompt, timeout, _retry_model=GROUNDING_MODEL_NAME)
            print("   => SDK にフォールバック")
            return await self._generate_with_sdk(prompt, timeout=timeout, grounding=True)

        try:
            async with httpx.AsyncClient() as client:
                response = await asyncio.wait_for(
                    client.post(
                        url,
                        params={"key": self._api_key},
                        json=body,
                        timeout=timeout or 60.0,
                    ),
                    timeout=timeout or 60.0,
                )

            if response.status_code != 200:
                print(f"🔍 グラウンディング API エラー: {response.status_code}")
                return await _fallback()

            data = response.json()
            return self._parse_grounding_response(data)
        except Exception as error:
            print(f"🔍 グラウンディング例外: {error}")
            return await _fallback()

    def _parse_grounding_response(
        self, data: dict[str, Any]
    ) -> tuple[str | None, Any | None, list[dict[str, str]]]:
        """REST API レスポンス JSON からテキスト・メタデータ・ソースを抽出する。"""
        candidates = data.get("candidates", [])
        if not candidates:
            return None, None, []

        candidate = candidates[0]

        # テキスト抽出
        parts = candidate.get("content", {}).get("parts", [])
        text_parts = [p.get("text", "") for p in parts if "text" in p]
        text = "".join(text_parts) if text_parts else None

        # 使用量メタデータ
        usage = data.get("usageMetadata")

        # グラウンディングソース抽出
        sources: list[dict[str, str]] = []
        grounding_metadata = candidate.get("groundingMetadata", {})
        chunks = grounding_metadata.get("groundingChunks", [])

        for chunk in chunks[:MAX_GROUNDING_SOURCES]:
            web = chunk.get("web", {})
            title = web.get("title", "")
            uri = web.get("uri", "")
            if uri:
                sources.append({"title": title.strip(), "uri": uri.strip()})

        print(f"🔍 グラウンディング結果: ソース {len(sources)} 件取得")
        return text, usage, sources

    async def upload_file(self, file_path: Path) -> Any:
        upload_file = getattr(self._genai, "upload_file")
        return await asyncio.wait_for(
            asyncio.to_thread(upload_file, str(file_path)),
            timeout=DOCUMENT_UPLOAD_TIMEOUT_SECONDS,
        )

    async def delete_file(self, uploaded_file: Any) -> None:
        uploaded_name = getattr(uploaded_file, "name", None)
        if not uploaded_name:
            return
        try:
            delete_file = getattr(self._genai, "delete_file")
            await asyncio.to_thread(delete_file, uploaded_name)
        except Exception as error:
            print(f"Geminiファイル削除エラー: {error}")

    async def summarize_document(self, uploaded_file: Any, document_name: str) -> dict[str, Any]:
        model = self._genai.GenerativeModel(self._state.current_model_name)
        prompt = (
            "このドキュメントの核となる要点とルールを、日本語で構造化して抽出してください。"
            "出力は必ずJSONのみで、次の形式にしてください: "
            '{"summary":"...","key_points":["..."],"warnings":["..."],"usage_notes":["..."]}'
            f"\nファイル名: {document_name}"
        )
        response = await asyncio.wait_for(
            model.generate_content_async(
                [prompt, uploaded_file],
                generation_config={"temperature": 0, "response_mime_type": "application/json"},
            ),
            timeout=DOCUMENT_SUMMARY_TIMEOUT_SECONDS,
        )
        parsed = extract_json_object(safe_response_text(response) or "")
        if not parsed:
            raise ValueError("要約JSONを取得できませんでした。")

        summary_text = str(parsed.get("summary", "")).strip()
        if not summary_text:
            raise ValueError("要約テキストが空です。")

        return {
            "document_name": document_name,
            "summary": summary_text,
            "key_points": parsed.get("key_points", []) if isinstance(parsed.get("key_points", []), list) else [],
            "warnings": parsed.get("warnings", []) if isinstance(parsed.get("warnings", []), list) else [],
            "usage_notes": parsed.get("usage_notes", []) if isinstance(parsed.get("usage_notes", []), list) else [],
        }
