import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any

from app_state import BotState
from constants import (
    ANNIVERSARY_PROFILE_KEY,
    DOCUMENT_SUMMARY_LIMIT,
    LEGACY_MEMORY_OWNER_ID,
    MEMORY_COMPACT_MAX_FAILURES,
    MEMORY_COMPACT_TIMEOUT_SECONDS,
    MEMORY_DIR,
    MEMORY_FLUSH_IDLE_SECONDS,
    MEMORY_FLUSH_MAX_USERS_PER_RUN,
    MEMORY_FLUSH_THRESHOLD_ENTRIES,
    MEMORY_ROUTE_CATEGORIES,
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
    safe_response_text,
    write_json_file,
)

# projects.json のユーザー別スキーマ識別子。この値が無ければ旧・全体共有形式とみなす。
_PROJECTS_SCHEMA = "per_user_v1"

# 記憶参照が明らかに不要な短い定型発言（あいさつ・相槌など）。ルーターの LLM 呼び出しを
# 省いて category="none" を即決するための簡易判定に使う（API 呼び出し削減）。
_TRIVIAL_MESSAGE_PATTERN = re.compile(
    r"^(おはよう|おはよ|こんにちは|こんばんは|やあ|やっほー?|ハロー|hello|hi|hey"
    r"|ありがと(う|ね)?|thanks?|thx|了解|りょうかい|おk|ok|おけ|うん|はい|いいえ"
    r"|なるほど|そうだね|そっか|わかった|分かった|おやすみ|おつかれ?|お疲れ様?でした?"
    r"|バイバイ|またね|乙|w+|笑|草)[！!。、.?？~〜ー\s]*$",
    re.IGNORECASE,
)


def default_profile_memory() -> dict[str, Any]:
    return {}


def default_projects_memory() -> dict[str, Any]:
    """projects.json の空の初期構造（ユーザー別スキーマ）。"""
    return {"_schema": _PROJECTS_SCHEMA, "users": {}}


class MemoryService:
    def __init__(self, state: BotState, genai_module: Any, db: Database) -> None:
        self._state = state
        self._genai = genai_module
        self._db = db
        self.io_lock = asyncio.Lock()
        self.flush_lock = asyncio.Lock()
        # 記憶圧縮の連続失敗回数をユーザー別に数える（あるユーザーの失敗が
        # 他ユーザーの pending_log クリア判定に影響しないようにする）。
        self._compact_fail_count: dict[str, int] = {}

    def ensure_storage(self) -> None:
        # プロフィール記憶は DB の profile テーブルが本体（ユーザー別）。profile.json は
        # 「手書き追加用の受け皿」で、起動時に DB へ吸収され {} に戻される。
        # projects.json は LLM が構造を決める自由形式 JSON をユーザー別に束ねたファイル。
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        if not PROFILE_MEMORY_PATH.exists():
            write_json_file(PROFILE_MEMORY_PATH, default_profile_memory())
        if not PROJECTS_MEMORY_PATH.exists():
            write_json_file(PROJECTS_MEMORY_PATH, default_projects_memory())

    # ---- projects.json（ユーザー別・自由形式JSON） ---------------------------

    def _load_projects_wrapper(self) -> dict[str, Any]:
        """projects.json をユーザー別スキーマの {"_schema":.., "users":{uid:{...}}} で返す。

        旧・全体共有形式（トップレベルがそのまま自由形式JSON）を読み込んだ場合は、
        その中身を丸ごと owner のプロジェクト記憶として引き継ぐ形へ移行し、
        ファイルへ書き戻す（冪等。移行済みなら何もしない）。
        """
        data = load_json_file(PROJECTS_MEMORY_PATH, default_projects_memory())
        if not isinstance(data, dict):
            data = default_projects_memory()

        if data.get("_schema") != _PROJECTS_SCHEMA:
            migrated = default_projects_memory()
            # 旧・全体共有形式の中身を owner の記憶として引き継ぐ。
            # 値がすべて空（初期シードのみ）の場合は引き継がない。
            legacy = {k: v for k, v in data.items() if k not in ("_schema", "users")}
            has_content = any(value for value in legacy.values())
            if has_content and LEGACY_MEMORY_OWNER_ID:
                migrated["users"][LEGACY_MEMORY_OWNER_ID] = legacy
            data = migrated
            write_json_file(PROJECTS_MEMORY_PATH, data)
            print("記憶: projects.json をユーザー別構造へ移行しました。")

        if not isinstance(data.get("users"), dict):
            data["users"] = {}
        return data

    def _user_projects(self, wrapper: dict[str, Any], user_id: str) -> dict[str, Any]:
        users = wrapper.get("users", {})
        value = users.get(str(user_id), {})
        return value if isinstance(value, dict) else {}

    # ---- プロンプトへ差し込む記憶コンテキスト --------------------------------

    def build_context(self, category: str, user_id: str) -> str:
        self.ensure_storage()
        if category == "profile":
            profile = self._db.profile_load(str(user_id))
            return json.dumps(profile, ensure_ascii=False, indent=2)
        if category == "projects":
            wrapper = self._load_projects_wrapper()
            projects = self._user_projects(wrapper, user_id)
            return json.dumps(projects, ensure_ascii=False, indent=2)
        return ""

    def build_document_memory_context(self, user_id: str, limit: int = 3) -> str:
        self.ensure_storage()
        wrapper = self._load_projects_wrapper()
        projects = self._user_projects(wrapper, user_id)
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

    async def append_pending_log(self, user_id: str, question: str, response_text: str) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        async with self.io_lock:
            await asyncio.to_thread(
                self._db.pending_append, str(user_id), timestamp, question, response_text
            )

    @staticmethod
    def _looks_memory_irrelevant(question: str) -> bool:
        """長期記憶の参照が明らかに不要そうな発言か（あいさつ・相槌・短文）を簡易判定する。

        True の場合、ルーターの LLM 呼び出しを省いて category="none" とする。
        取りこぼしても route_category は失敗時 none にフォールバックする設計なので、
        安全側（少し多めに none 判定）に倒しても実害は小さい。
        """
        text = question.strip()
        if not text:
            return True
        if _TRIVIAL_MESSAGE_PATTERN.match(text):
            return True
        return False

    async def route_category(self, question: str) -> str:
        if not question.strip():
            return "none"

        # 記憶参照が不要そうな短い定型発言は LLM を呼ばずに none で即決する（API削減）。
        if self._looks_memory_irrelevant(question):
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
            if category in MEMORY_ROUTE_CATEGORIES:
                return category
        except Exception as error:
            print(f"ルーティング判定エラー: {error}")
        return "none"

    async def store_document_summary(self, user_id: str, summary_data: dict[str, Any]) -> None:
        self.ensure_storage()
        summary_data = {**summary_data, "created_at": datetime.now(timezone.utc).isoformat()}
        async with self.io_lock:
            wrapper = self._load_projects_wrapper()
            user_projects = wrapper["users"].setdefault(str(user_id), {})
            if not isinstance(user_projects, dict):
                user_projects = {}
                wrapper["users"][str(user_id)] = user_projects
            summaries = user_projects.get("document_summaries", [])
            if not isinstance(summaries, list):
                summaries = []
            summaries.append(summary_data)
            user_projects["document_summaries"] = summaries[-DOCUMENT_SUMMARY_LIMIT:]
            await asyncio.to_thread(write_json_file, PROJECTS_MEMORY_PATH, wrapper)

    # ---- 記憶圧縮（ユーザー単位） -------------------------------------------

    async def flush_if_needed(self, force: bool = False) -> None:
        if self.flush_lock.locked():
            return

        async with self.flush_lock:
            async with self.io_lock:
                self.ensure_storage()
                user_ids = await asyncio.to_thread(self._db.pending_distinct_user_ids)
                if not user_ids:
                    return

                # 他人の会話ログを同じ Gemini 呼び出しに混ぜないため、ユーザーごとに
                # 個別に圧縮する。1 回のフラッシュで処理する人数は上限で区切り、
                # 一度に大量の API 呼び出しが走らないようにする（残りは次回の見回りで処理）。
                processed = 0
                for user_id in user_ids:
                    if processed >= MEMORY_FLUSH_MAX_USERS_PER_RUN:
                        break
                    entries = await asyncio.to_thread(self._db.pending_fetch_entries, str(user_id))
                    if not entries:
                        continue
                    if not force and not self._is_flush_required(len(entries)):
                        continue
                    processed += 1
                    await self._flush_user(str(user_id), entries)

    async def _flush_user(self, user_id: str, entries: list[dict[str, Any]]) -> None:
        """1 ユーザー分の pending_log を profile / projects へ圧縮統合する。

        入力プロンプトにはこのユーザーの会話ログ・記憶だけを含める
        （他ユーザーの情報は一切混ぜない）。フェイルセーフ設計で、パース失敗時は
        pending_log を保持（リトライ）するが、連続失敗やセーフティブロック時は
        無限ループを避けるためそのユーザーの pending_log をクリアする。
        """
        pending_text = self._format_pending_entries(entries)

        profile_snapshot = await asyncio.to_thread(self._db.profile_load, user_id)
        profile_text = json.dumps(profile_snapshot, ensure_ascii=False, indent=2)
        wrapper = self._load_projects_wrapper()
        projects_text = json.dumps(self._user_projects(wrapper, user_id), ensure_ascii=False, indent=2)

        try:
            profile_data, projects_data = await self._compact_memory(
                pending_text,
                profile_text,
                projects_text,
            )
        except _PromptBlockedError as error:
            # pending_log に不適切な内容が含まれており永久に処理できない
            # → このユーザーの pending_log をクリアして無限ループを防止
            print(f"記憶圧縮: プロンプトがブロックされました (user_id={user_id}, {error})")
            print(f"記憶圧縮: 処理不能な pending_log をクリアします (user_id={user_id})。")
            await asyncio.to_thread(self._db.pending_clear, user_id)
            return
        except Exception as error:
            await self._register_compact_failure(user_id, f"記憶圧縮エラー (user_id={user_id}): {error}")
            return

        if profile_data is None or projects_data is None:
            await self._register_compact_failure(
                user_id,
                f"記憶圧縮エラー (user_id={user_id}): JSONの生成またはパースに失敗したため、pending_log は保持します。",
            )
            return

        self._compact_fail_count[user_id] = 0
        await asyncio.to_thread(self._db.profile_replace_all, user_id, profile_data)
        # projects.json はこのユーザーのスライスだけ差し替える（他ユーザーの分は保持）。
        wrapper = self._load_projects_wrapper()
        wrapper["users"][user_id] = projects_data
        await asyncio.to_thread(write_json_file, PROJECTS_MEMORY_PATH, wrapper)
        await asyncio.to_thread(self._db.pending_clear, user_id)

    @staticmethod
    def _format_pending_entries(entries: list[dict[str, Any]]) -> str:
        """DB の pending_log 行を、記憶圧縮プロンプトへ渡すテキスト形式に整形する。"""
        lines: list[str] = []
        for entry in entries:
            lines.append(f"[{entry['created_at']}] User: {entry['question']}")
            lines.append(f"[{entry['created_at']}] Bot: {entry['response']}")
        return "\n".join(lines)

    async def _register_compact_failure(self, user_id: str, log_message: str) -> None:
        """記憶圧縮の失敗を記録する。基本は pending_log を保持してリトライするが、
        連続失敗が上限に達したらそのユーザーの分をクリアして無限リトライを防ぐ。"""
        print(log_message)
        count = self._compact_fail_count.get(user_id, 0) + 1
        self._compact_fail_count[user_id] = count
        if count >= MEMORY_COMPACT_MAX_FAILURES:
            print(f"記憶圧縮: 連続失敗のため pending_log をクリアします (user_id={user_id})。")
            await asyncio.to_thread(self._db.pending_clear, user_id)
            self._compact_fail_count[user_id] = 0

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
            "【記念日・誕生日の扱い】一時ログの中でユーザーが誕生日や記念日など"
            "「毎年同じ月日に繰り返し祝いたいイベント」について話していた場合、"
            f'profile 内に "{ANNIVERSARY_PROFILE_KEY}" というキーで次の形式のリストとして保存してください: '
            '[{"label": "呼び名(例: 誕生日)", "month": 1〜12の整数, "day": 1〜31の整数}]。'
            "既存の記念日エントリがあれば失わないようマージし（同じ label は上書き、それ以外は保持）、"
            "月日が確定できない情報や単発の出来事（例: 今日の予定）は記念日として保存しないでください。\n\n"
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
