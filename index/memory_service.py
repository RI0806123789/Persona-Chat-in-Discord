import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from app_state import BotState
from constants import (
    DOCUMENT_SUMMARY_LIMIT,
    MEMORY_DIR,
    MEMORY_FLUSH_IDLE_SECONDS,
    MEMORY_FLUSH_THRESHOLD_LINES,
    PENDING_LOG_PATH,
    PROFILE_MEMORY_PATH,
    PROJECTS_MEMORY_PATH,
    QUEUE_PATH,
    ROUTER_MODEL_NAME,
)
from storage import (
    append_text_to_file,
    clear_file,
    extract_json_object,
    load_json_file,
    read_text_file,
    safe_response_text,
    write_json_file,
)


def default_profile_memory() -> dict[str, Any]:
    return {}


def default_projects_memory() -> dict[str, Any]:
    return {"active_projects": {}, "document_summaries": []}


def default_queue() -> dict[str, Any]:
    return {"tasks": []}


class MemoryService:
    def __init__(self, state: BotState, genai_module: Any) -> None:
        self._state = state
        self._genai = genai_module
        self.io_lock = asyncio.Lock()
        self.flush_lock = asyncio.Lock()

    def ensure_storage(self) -> None:
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        if not PROFILE_MEMORY_PATH.exists():
            write_json_file(PROFILE_MEMORY_PATH, default_profile_memory())
        if not PROJECTS_MEMORY_PATH.exists():
            write_json_file(PROJECTS_MEMORY_PATH, default_projects_memory())
        if not PENDING_LOG_PATH.exists():
            PENDING_LOG_PATH.write_text("", encoding="utf-8")
        if not QUEUE_PATH.exists():
            write_json_file(QUEUE_PATH, default_queue())

    def build_context(self, category: str) -> str:
        self.ensure_storage()
        if category == "profile":
            profile = load_json_file(PROFILE_MEMORY_PATH, default_profile_memory())
            return json.dumps(profile, ensure_ascii=False, indent=2)
        if category == "projects":
            projects = load_json_file(PROJECTS_MEMORY_PATH, default_projects_memory())
            return json.dumps(projects, ensure_ascii=False, indent=2)
        return ""

    def build_document_memory_context(self, limit: int = 3) -> str:
        self.ensure_storage()
        projects = load_json_file(PROJECTS_MEMORY_PATH, default_projects_memory())
        summaries = projects.get("document_summaries", [])
        if not isinstance(summaries, list) or not summaries:
            return ""

        lines: list[str] = []
        for summary in summaries[-limit:]:
            if not isinstance(summary, dict):
                continue
            document_name = str(summary.get("document_name", "unknown"))
            summary_text = str(summary.get("summary", "")).strip()
            if summary_text:
                lines.append(f"- {document_name}: {summary_text}")
        return "\n".join(lines)

    async def append_pending_log(self, question: str, response_text: str) -> None:
        self.ensure_storage()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{timestamp}] User: {question}\n[{timestamp}] Bot: {response_text}\n"
        async with self.io_lock:
            await asyncio.to_thread(append_text_to_file, PENDING_LOG_PATH, entry)

    async def route_category(self, question: str) -> str:
        if not question.strip():
            return "none"

        try:
            model = self._genai.GenerativeModel(ROUTER_MODEL_NAME)
            router_prompt = (
                "以下の質問に答えるために、ユーザーのプロフィール情報(profile)、進行中のプロジェクト情報(projects)、"
                "または過去の情報は不要(none)のどれが適切ですか？ JSONで category だけを返してください。\n"
                f"質問: {question}\n"
                '出力形式: {"category": "profile|projects|none"}'
            )
            response = await model.generate_content_async(
                router_prompt,
                generation_config={"temperature": 0, "response_mime_type": "application/json"},
            )
            parsed = extract_json_object(safe_response_text(response) or "")
            category = str((parsed or {}).get("category", "none")).lower()
            if category in {"profile", "projects", "none"}:
                return category
        except Exception as error:
            print(f"ルーティング判定エラー: {error}")
        return "none"

    async def store_document_summary(self, summary_data: dict[str, Any]) -> None:
        self.ensure_storage()
        summary_data = {**summary_data, "created_at": datetime.now(timezone.utc).isoformat()}
        async with self.io_lock:
            projects = load_json_file(PROJECTS_MEMORY_PATH, default_projects_memory())
            summaries = projects.get("document_summaries", [])
            if not isinstance(summaries, list):
                summaries = []
            summaries.append(summary_data)
            projects["document_summaries"] = summaries[-DOCUMENT_SUMMARY_LIMIT:]
            await asyncio.to_thread(write_json_file, PROJECTS_MEMORY_PATH, projects)

    async def flush_if_needed(self, force: bool = False) -> None:
        if self.flush_lock.locked():
            return

        async with self.flush_lock:
            async with self.io_lock:
                self.ensure_storage()
                pending_text = await asyncio.to_thread(read_text_file, PENDING_LOG_PATH)
                if not pending_text.strip():
                    return
                if not force and not self._is_flush_required(pending_text):
                    return

                profile_text = await asyncio.to_thread(read_text_file, PROFILE_MEMORY_PATH)
                projects_text = await asyncio.to_thread(read_text_file, PROJECTS_MEMORY_PATH)
                try:
                    profile_data, projects_data = await self._compact_memory(
                        pending_text,
                        profile_text,
                        projects_text,
                    )
                except _PromptBlockedError as error:
                    # pending_log に不適切な内容が含まれており永久に処理できない
                    # → pending_log をクリアして無限ループを防止
                    print(f"記憶圧縮: プロンプトがブロックされました ({error})")
                    print("記憶圧縮: 処理不能な pending_log をクリアします。")
                    await asyncio.to_thread(clear_file, PENDING_LOG_PATH)
                    return
                except Exception as error:
                    print(f"記憶圧縮エラー: {error}")
                    self._compact_fail_count = getattr(self, "_compact_fail_count", 0) + 1
                    if self._compact_fail_count >= 3:
                        print("記憶圧縮: 連続失敗のため pending_log をクリアします。")
                        await asyncio.to_thread(clear_file, PENDING_LOG_PATH)
                        self._compact_fail_count = 0
                    return

                if profile_data is None or projects_data is None:
                    print("記憶圧縮エラー: JSONの生成またはパースに失敗したため、pending_log は保持します。")
                    self._compact_fail_count = getattr(self, "_compact_fail_count", 0) + 1
                    if self._compact_fail_count >= 3:
                        print("記憶圧縮: 連続失敗のため pending_log をクリアします。")
                        await asyncio.to_thread(clear_file, PENDING_LOG_PATH)
                        self._compact_fail_count = 0
                    return

                self._compact_fail_count = 0
                await asyncio.to_thread(write_json_file, PROFILE_MEMORY_PATH, profile_data)
                await asyncio.to_thread(write_json_file, PROJECTS_MEMORY_PATH, projects_data)
                await asyncio.to_thread(clear_file, PENDING_LOG_PATH)

    def _is_flush_required(self, pending_text: str) -> bool:
        if len(pending_text.splitlines()) >= MEMORY_FLUSH_THRESHOLD_LINES:
            return True
        elapsed_seconds = (datetime.now(timezone.utc) - self._state.last_activity_timestamp).total_seconds()
        return elapsed_seconds >= MEMORY_FLUSH_IDLE_SECONDS

    async def _compact_memory(
        self,
        pending_text: str,
        profile_text: str,
        projects_text: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        compactor_prompt = (
            "次の一時ログと既存の記憶JSONから、永続化すべき重要な事実だけを抽出し、"
            "profile と projects を整理したJSONを返してください。不要な古い情報は削除してください。"
            "出力は必ずJSONのみで、形式は {\"profile\": {...}, \"projects\": {...}} にしてください。\n\n"
            f"--- pending_log.txt ---\n{pending_text}\n\n"
            f"--- profile.json ---\n{profile_text}\n\n"
            f"--- projects.json ---\n{projects_text}"
        )

        model = self._genai.GenerativeModel(self._state.current_model_name)
        response = await model.generate_content_async(
            compactor_prompt,
            generation_config={"temperature": 0, "response_mime_type": "application/json"},
        )

        # Gemini がプロンプトをブロックした場合を検知
        prompt_feedback = getattr(response, "prompt_feedback", None)
        if prompt_feedback:
            block_reason = getattr(prompt_feedback, "block_reason", None)
            if block_reason is not None and int(block_reason) != 0:
                reason_name = getattr(block_reason, "name", str(block_reason))
                raise _PromptBlockedError(reason_name)

        parsed = extract_json_object(safe_response_text(response) or "")
        if not parsed:
            return None, None

        profile_data = parsed.get("profile")
        projects_data = parsed.get("projects")
        if not isinstance(profile_data, dict) or not isinstance(projects_data, dict):
            return None, None
        return profile_data, projects_data


class _PromptBlockedError(Exception):
    """記憶圧縮プロンプトが Gemini の安全フィルタでブロックされた場合の例外。"""
