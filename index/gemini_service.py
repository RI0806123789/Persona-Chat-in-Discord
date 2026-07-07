import asyncio
from pathlib import Path
from typing import Any, Sequence

from app_state import BotState
from constants import DOCUMENT_SUMMARY_TIMEOUT_SECONDS, DOCUMENT_UPLOAD_TIMEOUT_SECONDS
from storage import extract_json_object, safe_response_text


class GeminiService:
    def __init__(self, state: BotState, genai_module: Any) -> None:
        self._state = state
        self._genai = genai_module

    async def generate(
        self,
        prompt: str,
        images: Sequence[Any] | None = None,
        timeout: float | None = None,
    ) -> tuple[str | None, Any | None]:
        try:
            model = self._genai.GenerativeModel(self._state.current_model_name)
            content: list[Any] = [prompt]
            if images:
                content.extend(images)

            request = model.generate_content_async(content)
            response = await asyncio.wait_for(request, timeout=timeout) if timeout else await request
            return safe_response_text(response), getattr(response, "usage_metadata", None)
        except Exception as error:
            print(f"Error occurred while asking Gemini: {error}")
            return None, None

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

