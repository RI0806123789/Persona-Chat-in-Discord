import asyncio
from math import ceil
from pathlib import Path
from time import monotonic
from typing import Any, Sequence

from google.genai import types

from constants import (
    DOCUMENT_SUMMARY_TIMEOUT_SECONDS,
    DOCUMENT_UPLOAD_TIMEOUT_SECONDS,
    GROUNDING_MODEL_NAME,
    GROUNDING_REQUEST_TIMEOUT_SECONDS,
    GROUNDING_SUBMODEL_RETRY_ENABLED,
    MAX_GROUNDING_SOURCES,
)
from genai_client import generate_content
from prompting import build_document_summary_prompt
from storage import extract_json_object, safe_response_text


def _remaining_timeout(deadline: float | None) -> float | None:
    """生成開始時の締切から残り時間を求め、使い切っていれば次の通信を止める。"""
    if deadline is None:
        return None
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise asyncio.TimeoutError()
    return remaining


def _grounding_is_unsupported(status_code: Any, message: str) -> bool:
    """認証・利用枠・サーバー障害と、検索ツールの非対応を区別する。"""
    if status_code not in (400, 404, 422):
        return False
    normalized = message.lower()
    return any(name in normalized for name in (
        "google_search", "google search", "grounding", "search_retrieval",
    )) and any(reason in normalized for reason in (
        "unsupported", "not supported", "not available", "not enabled", "not found",
        "unknown name", "not permitted", "not allowed",
    ))


class GeminiService:
    """共有クライアントを使い、呼び出し元が選んだモデルで非同期生成する。"""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def generate(
        self,
        prompt: str,
        model_name: str,
        images: Sequence[Any] | None = None,
        timeout: float | None = None,
        grounding: bool = False,
    ) -> tuple[str | None, Any | None, list[dict[str, str]]]:
        # フォールバックのたびに待ち時間をリセットせず、生成全体で予算を共有する。
        budget = GROUNDING_REQUEST_TIMEOUT_SECONDS if grounding and timeout is None else timeout
        deadline = monotonic() + budget if budget is not None else None
        try:
            remaining = _remaining_timeout(deadline)
            request = self._generate(prompt, model_name, images, deadline, grounding)
            result = await asyncio.wait_for(request, timeout=remaining) if remaining is not None else await request
            # 締切を越えて戻った応答も成功として返さない。
            _remaining_timeout(deadline)
            return result
        except asyncio.TimeoutError:
            print("Gemini生成: 制限時間に達したため打ち切りました。")
            return None, None, []

    async def _generate(
        self,
        prompt: str,
        model_name: str,
        images: Sequence[Any] | None,
        deadline: float | None,
        grounding: bool,
    ) -> tuple[str | None, Any | None, list[dict[str, str]]]:
        contents: list[Any] = [prompt]
        if images:
            contents.extend(images)
        attempts = [(model_name, grounding)]
        if grounding:
            if GROUNDING_SUBMODEL_RETRY_ENABLED and model_name != GROUNDING_MODEL_NAME:
                attempts.append((GROUNDING_MODEL_NAME, True))
            attempts.append((model_name, False))

        for effective_model, use_grounding in attempts:
            remaining = _remaining_timeout(deadline)
            try:
                options: dict[str, Any] = {}
                if use_grounding:
                    options["tools"] = [types.Tool(google_search=types.GoogleSearch())]
                    print(f"Google検索グラウンディング: 有効（モデル: {effective_model}）")
                response = await generate_content(
                    self._client, model=effective_model, contents=contents, timeout=remaining, **options,
                )
                sources = self._grounding_sources(response) if use_grounding else []
                return safe_response_text(response), getattr(response, "usage_metadata", None), sources
            except asyncio.TimeoutError:
                raise
            except Exception as error:
                if use_grounding and _grounding_is_unsupported(getattr(error, "code", None), str(error)):
                    print("検索ツールが非対応のため、残り時間内で代替の生成を試します。")
                    continue
                # 通信・認証・利用枠・サーバー障害では追加のAPI呼び出しを行わない。
                print(f"Gemini生成エラー: {error}")
                return None, None, []
        return None, None, []

    @staticmethod
    def _grounding_sources(response: Any) -> list[dict[str, str]]:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return []
        metadata = getattr(candidates[0], "grounding_metadata", None)
        chunks = getattr(metadata, "grounding_chunks", None) or []
        sources = []
        for chunk in chunks[:MAX_GROUNDING_SOURCES]:
            web = getattr(chunk, "web", None)
            uri = (getattr(web, "uri", "") or "").strip()
            if uri:
                sources.append({"title": (getattr(web, "title", "") or "").strip(), "uri": uri})
        return sources

    async def upload_file(self, file_path: Path) -> Any:
        # テキスト系はOSごとのMIME推定に任せず、モデルが受け取れるtext/plainへ揃える。
        mime_type = "application/pdf" if file_path.suffix.lower() == ".pdf" else "text/plain"
        return await asyncio.wait_for(
            self._client.aio.files.upload(
                file=file_path,
                config=types.UploadFileConfig(
                    mime_type=mime_type,
                    http_options=types.HttpOptions(
                        timeout=ceil(DOCUMENT_UPLOAD_TIMEOUT_SECONDS * 1000),
                        retry_options=types.HttpRetryOptions(attempts=1),
                    ),
                ),
            ),
            timeout=DOCUMENT_UPLOAD_TIMEOUT_SECONDS,
        )

    async def delete_file(self, uploaded_file: Any) -> None:
        uploaded_name = getattr(uploaded_file, "name", None)
        if not uploaded_name:
            return
        try:
            await asyncio.wait_for(
                self._client.aio.files.delete(
                    name=uploaded_name,
                    config=types.DeleteFileConfig(http_options=types.HttpOptions(
                        timeout=ceil(DOCUMENT_UPLOAD_TIMEOUT_SECONDS * 1000),
                        retry_options=types.HttpRetryOptions(attempts=1),
                    )),
                ),
                timeout=DOCUMENT_UPLOAD_TIMEOUT_SECONDS,
            )
        except Exception as error:
            print(f"Geminiファイル削除エラー: {error}")

    async def summarize_document(self, uploaded_file: Any, document_name: str, model_name: str) -> dict[str, Any]:
        prompt = build_document_summary_prompt(document_name)
        response = await generate_content(
            self._client,
            model=model_name,
            contents=[prompt, uploaded_file],
            timeout=DOCUMENT_SUMMARY_TIMEOUT_SECONDS,
            temperature=0,
            response_mime_type="application/json",
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
