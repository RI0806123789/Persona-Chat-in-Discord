import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from app_state import BotState
from constants import (
    DOCUMENT_SUMMARY_LIMIT,
    MEMORY_COMPACT_MAX_FAILURES,
    MEMORY_COMPACT_TIMEOUT_SECONDS,
    MEMORY_DIR,
    MEMORY_FLUSH_IDLE_SECONDS,
    MEMORY_FLUSH_THRESHOLD_ENTRIES,
    PROFILE_MEMORY_PATH,
    PROJECTS_MEMORY_PATH,
    ROUTER_MODEL_NAME,
    ROUTER_TIMEOUT_SECONDS,
)
from database import Database
from storage import (
    extract_json_object,
    get_prompt_block_reason,
    load_json_file,
    read_text_file,
    safe_response_text,
    write_json_file,
)


def default_profile_memory() -> dict[str, Any]:
    return {}


def default_projects_memory() -> dict[str, Any]:
    return {"active_projects": {}, "document_summaries": []}


class MemoryService:
    def __init__(self, state: BotState, genai_module: Any, db: Database) -> None:
        self._state = state
        self._genai = genai_module
        self._db = db
        self.io_lock = asyncio.Lock()
        self.flush_lock = asyncio.Lock()
        self._compact_fail_count = 0

    def ensure_storage(self) -> None:
        # プロフィール記憶は DB の profile テーブルが本体。profile.json は
        # 「手書き追加用の受け皿」で、起動時に DB へ吸収され {} に戻される。
        # projects.json は LLM が構造を決める自由形式 JSON のためファイルのまま。
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        if not PROFILE_MEMORY_PATH.exists():
            write_json_file(PROFILE_MEMORY_PATH, default_profile_memory())
        if not PROJECTS_MEMORY_PATH.exists():
            write_json_file(PROJECTS_MEMORY_PATH, default_projects_memory())

    def build_context(self, category: str) -> str:
        self.ensure_storage()
        if category == "profile":
            profile = self._db.profile_load()
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
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        async with self.io_lock:
            await asyncio.to_thread(self._db.pending_append, timestamp, question, response_text)

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
            response = await asyncio.wait_for(
                model.generate_content_async(
                    router_prompt,
                    generation_config={"temperature": 0, "response_mime_type": "application/json"},
                ),
                timeout=ROUTER_TIMEOUT_SECONDS,
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
                entries = await asyncio.to_thread(self._db.pending_fetch_entries)
                if not entries:
                    return
                if not force and not self._is_flush_required(len(entries)):
                    return
                pending_text = self._format_pending_entries(entries)

                profile_snapshot = await asyncio.to_thread(self._db.profile_load)
                profile_text = json.dumps(profile_snapshot, ensure_ascii=False, indent=2)
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
                    await asyncio.to_thread(self._db.pending_clear)
                    return
                except Exception as error:
                    await self._register_compact_failure(f"記憶圧縮エラー: {error}")
                    return

                if profile_data is None or projects_data is None:
                    await self._register_compact_failure(
                        "記憶圧縮エラー: JSONの生成またはパースに失敗したため、pending_log は保持します。"
                    )
                    return

                self._compact_fail_count = 0
                await asyncio.to_thread(self._db.profile_replace_all, profile_data)
                await asyncio.to_thread(write_json_file, PROJECTS_MEMORY_PATH, projects_data)
                await asyncio.to_thread(self._db.pending_clear)

    @staticmethod
    def _format_pending_entries(entries: list[dict[str, Any]]) -> str:
        """DB の pending_log 行を、記憶圧縮プロンプトへ渡すテキスト形式に整形する。"""
        lines: list[str] = []
        for entry in entries:
            lines.append(f"[{entry['created_at']}] User: {entry['question']}")
            lines.append(f"[{entry['created_at']}] Bot: {entry['response']}")
        return "\n".join(lines)

    async def _register_compact_failure(self, log_message: str) -> None:
        """記憶圧縮の失敗を記録する。基本は pending_log を保持してリトライするが、
        連続失敗が上限に達したらクリアして無限リトライを防ぐ。"""
        print(log_message)
        self._compact_fail_count += 1
        if self._compact_fail_count >= MEMORY_COMPACT_MAX_FAILURES:
            print("記憶圧縮: 連続失敗のため pending_log をクリアします。")
            await asyncio.to_thread(self._db.pending_clear)
            self._compact_fail_count = 0

    def _is_flush_required(self, entry_count: int) -> bool:
        if entry_count >= MEMORY_FLUSH_THRESHOLD_ENTRIES:
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
            f"--- 一時ログ (pending_log) ---\n{pending_text}\n\n"
            f"--- プロフィール記憶 (profile) ---\n{profile_text}\n\n"
            f"--- プロジェクト記憶 (projects) ---\n{projects_text}"
        )

        model = self._genai.GenerativeModel(ROUTER_MODEL_NAME)
        response = await asyncio.wait_for(
            model.generate_content_async(
                compactor_prompt,
                generation_config={"temperature": 0, "response_mime_type": "application/json"},
            ),
            timeout=MEMORY_COMPACT_TIMEOUT_SECONDS,
        )

        # Gemini がプロンプトをブロックした場合を検知
        reason_name = get_prompt_block_reason(response)
        if reason_name is not None:
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
