import asyncio
from pathlib import Path
from time import monotonic
from typing import Any, Sequence

import httpx

from constants import (
    DOCUMENT_SUMMARY_TIMEOUT_SECONDS,
    DOCUMENT_UPLOAD_TIMEOUT_SECONDS,
    GROUNDING_MODEL_NAME,
    GROUNDING_REQUEST_TIMEOUT_SECONDS,
    GROUNDING_SUBMODEL_RETRY_ENABLED,
    MAX_GROUNDING_SOURCES,
)
from prompting import build_document_summary_prompt
from storage import extract_json_object, safe_response_text

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


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
    """Gemini API 呼び出しをまとめるサービス。

    どのモデルを使うかはギルド（サーバー）ごとに異なりうるため、モデル名は
    内部で保持せず、呼び出し側（GuildSettings.current_model_name 等）から
    毎回 model_name 引数として渡してもらう設計にしている。
    """

    def __init__(self, genai_module: Any, api_key: str = "") -> None:
        self._genai = genai_module
        self._api_key = api_key

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
            if grounding and not images:
                request = self._generate_with_grounding(prompt, model_name, deadline)
            else:
                # 画像は SDK の検索ツールで扱い、REST 経路には渡さない。
                request = self._generate_with_sdk(prompt, model_name, images, deadline, grounding=grounding)
            # 接続開始・終了も含めて生成全体を制限する。外部からのキャンセルは捕まえない。
            result = await asyncio.wait_for(request, timeout=remaining) if remaining is not None else await request
            _remaining_timeout(deadline)
            return result
        except asyncio.TimeoutError:
            print("Gemini生成: 制限時間に達したため打ち切りました。")
            return None, None, []

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
                print(f"検索ツール（google_search）の構築失敗: {error}")

        if hasattr(protos, "GoogleSearchRetrieval"):
            try:
                return [protos.Tool(google_search_retrieval=protos.GoogleSearchRetrieval())]
            except Exception as error:
                print(f"検索ツール（google_search_retrieval）の構築失敗: {error}")

        return None

    async def _generate_with_sdk(
        self,
        prompt: str,
        model_name: str,
        images: Sequence[Any] | None = None,
        deadline: float | None = None,
        grounding: bool = False,
    ) -> tuple[str | None, Any | None, list[dict[str, str]]]:
        """SDK 経由での生成（フォールバック時にもグラウンディングを維持）。"""
        try:
            content: list[Any] = [prompt]
            if images:
                content.extend(images)

            tools = self._build_grounding_tools() if grounding else None
            for use_grounding in ((True, False) if grounding and tools else (False,)):
                remaining = _remaining_timeout(deadline)
                model = self._genai.GenerativeModel(model_name, tools=tools if use_grounding else None)
                try:
                    # SDK内部にも残り時間を渡し、通信障害時の自動再試行を抑える。
                    request_options: dict[str, Any] = {"retry": None}
                    if remaining is not None:
                        request_options["timeout"] = remaining
                    request = model.generate_content_async(content, request_options=request_options)
                    response = await asyncio.wait_for(request, timeout=remaining) if remaining is not None else await request
                    break
                except asyncio.TimeoutError:
                    raise
                except Exception as error:
                    if use_grounding and _grounding_is_unsupported(getattr(error, "code", None), str(error)):
                        print("SDK検索ツールが非対応のため、選択モデルで検索なしの生成を試します。")
                        continue
                    raise

            sources = []
            if use_grounding:
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
                except Exception as error:
                    print(f"SDK検索結果の解析エラー: {error}")

            return safe_response_text(response), getattr(response, "usage_metadata", None), sources
        except asyncio.TimeoutError:
            raise
        except Exception as error:
            print(f"Gemini SDK生成エラー: {error}")
            return None, None, []

    async def _generate_with_grounding(
        self,
        prompt: str,
        model_name: str,
        deadline: float | None = None,
    ) -> tuple[str | None, Any | None, list[dict[str, str]]]:
        """REST API 直接呼び出しによるグラウンディング付き生成。

        検索ツール非対応の4xxに限り対応モデル・SDKへ進む。
        通信やサーバー障害・認証・利用枠不足では、追加のAPI呼び出しをしない。
        """
        body: dict[str, Any] = {
            "contents": [{"parts": [{"text": prompt}]}],
            "tools": [{"google_search": {}}],
        }

        models = [model_name]
        if GROUNDING_SUBMODEL_RETRY_ENABLED and model_name != GROUNDING_MODEL_NAME:
            models.append(GROUNDING_MODEL_NAME)
        try:
            _remaining_timeout(deadline)
            # 1回の生成に含まれるREST再試行で接続を共有し、終了時に確実に閉じる。
            async with httpx.AsyncClient() as client:
                for effective_model in models:
                    remaining = _remaining_timeout(deadline)
                    print(f"🔍 Google検索グラウンディング: 有効（モデル: {effective_model}）")
                    response = await asyncio.wait_for(
                        client.post(
                            f"{GEMINI_API_BASE}/{effective_model}:generateContent",
                            params={"key": self._api_key}, json=body, timeout=remaining,
                        ),
                        timeout=remaining,
                    )
                    if response.status_code == 200:
                        return self._parse_grounding_response(response.json())
                    print(f"🔍 グラウンディング API エラー: {response.status_code}")
                    if not _grounding_is_unsupported(response.status_code, response.text):
                        return None, None, []
            # サブモデルを使っても、最終SDK生成のモデルはユーザーの選択を保つ。
            _remaining_timeout(deadline)
            print(f"   => SDK にフォールバック（モデル: {model_name}）")
            return await self._generate_with_sdk(prompt, model_name, deadline=deadline, grounding=True)
        except asyncio.TimeoutError:
            raise
        except Exception as error:
            print(f"🔍 グラウンディング例外: {error}")
            return None, None, []

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

    async def summarize_document(self, uploaded_file: Any, document_name: str, model_name: str) -> dict[str, Any]:
        model = self._genai.GenerativeModel(model_name)
        prompt = build_document_summary_prompt(document_name)
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
