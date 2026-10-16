import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any

from persona_bot.core.app_state import BotState
from persona_bot.core.background import run_in_thread
from persona_bot.config.constants import (
    ANNIVERSARY_PROFILE_KEY,
    DOCUMENT_MEMORY_CONTEXT_LIMIT,
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
from persona_bot.core.database import Database
from persona_bot.services.genai_client import generate_content
from persona_bot.services.prompting import build_memory_compaction_prompt, build_router_prompt
from persona_bot.utils.storage import (
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
    def __init__(self, state: BotState, genai_client: Any, db: Database) -> None:
        self._state = state
        self._genai_client = genai_client
        self._db = db
        self.io_lock = asyncio.Lock()
        self.flush_lock = asyncio.Lock()
        # 記憶圧縮の連続失敗回数をユーザー別に数える（あるユーザーの失敗が
        # 他ユーザーの pending_log クリア判定に影響しないようにする）。
        self._compact_fail_count: dict[str, int] = {}
        # 文書要約は末尾の上限件数しか保存しないため、件数では追加を検出できない。
        # 単一プロセス内の更新世代をユーザー別に数え、io_lock の下で読み書きする。
        self._document_summary_versions: dict[str, int] = {}

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

    async def build_contexts(
        self, category: str, user_id: str, limit: int = DOCUMENT_MEMORY_CONTEXT_LIMIT,
    ) -> tuple[str, str]:
        """同じスナップショットから通常記憶と文書記憶を作り、JSONを1回だけ読む。"""
        async with self.io_lock:
            return await run_in_thread(self._build_contexts, category, str(user_id), limit)

    def _build_contexts(self, category: str, user_id: str, limit: int) -> tuple[str, str]:
        self.ensure_storage()
        wrapper = self._load_projects_wrapper()
        projects = self._user_projects(wrapper, user_id)
        context = ""
        if category == "profile":
            context = json.dumps(self._db.profile_load(user_id), ensure_ascii=False, indent=2)
        elif category == "projects":
            context = json.dumps(projects, ensure_ascii=False, indent=2)
        summaries = projects.get("document_summaries", [])
        lines = []
        if isinstance(summaries, list) and limit > 0:
            for summary in summaries[-limit:]:
                if not isinstance(summary, dict):
                    continue
                name = str(summary.get("document_name", "unknown"))
                text = str(summary.get("summary", "")).strip()
                if text:
                    lines.append(f"- {name}: {text}")
        return context, "\n".join(lines)

    async def build_context(self, category: str, user_id: str) -> str:
        context, _ = await self.build_contexts(category, user_id)
        return context

    async def build_document_memory_context(
        self, user_id: str, limit: int = DOCUMENT_MEMORY_CONTEXT_LIMIT,
    ) -> str:
        _, documents = await self.build_contexts("none", user_id, limit)
        return documents

    async def append_pending_log(self, user_id: str, question: str, response_text: str) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        async with self.io_lock:
            await run_in_thread(
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
            router_prompt = build_router_prompt(question)
            response = await generate_content(
                self._genai_client, model=ROUTER_MODEL_NAME, contents=router_prompt,
                timeout=ROUTER_TIMEOUT_SECONDS, temperature=0, response_mime_type="application/json",
            )
            parsed = extract_json_object(safe_response_text(response) or "")
            category = str((parsed or {}).get("category", "none")).lower()
            if category in MEMORY_ROUTE_CATEGORIES:
                return category
        except Exception as error:
            print(f"ルーティング判定エラー: {error}")
        return "none"

    async def store_document_summary(self, user_id: str, summary_data: dict[str, Any]) -> None:
        summary_data = {**summary_data, "created_at": datetime.now(timezone.utc).isoformat()}
        async with self.io_lock:
            await run_in_thread(self._store_document_summary, str(user_id), summary_data)

    def _store_document_summary(self, user_id: str, summary_data: dict[str, Any]) -> None:
        self.ensure_storage()
        wrapper = self._load_projects_wrapper()
        user_projects = wrapper["users"].setdefault(user_id, {})
        if not isinstance(user_projects, dict):
            user_projects = {}
            wrapper["users"][user_id] = user_projects
        summaries = user_projects.get("document_summaries", [])
        if not isinstance(summaries, list):
            summaries = []
        summaries.append(summary_data)
        user_projects["document_summaries"] = summaries[-DOCUMENT_SUMMARY_LIMIT:]
        write_json_file(PROJECTS_MEMORY_PATH, wrapper)
        self._document_summary_versions[user_id] = self._document_summary_versions.get(user_id, 0) + 1

    # ---- 記憶圧縮（ユーザー単位） -------------------------------------------

    async def flush_if_needed(self, force: bool = False) -> None:
        if self.flush_lock.locked():
            return

        # ロックの役割分担:
        #   flush_lock … フラッシュ処理そのものが同時に二重に走らないようにする（全体を保持）。
        #   io_lock    … ファイルI/O・DB書き込みの直列化だけに使う（読み書きのたびに短く取る）。
        # io_lock を最後まで保持したままにすると、Gemini 呼び出し
        # （最大 MEMORY_FLUSH_MAX_USERS_PER_RUN 人 × MEMORY_COMPACT_TIMEOUT_SECONDS）の間、
        # 無関係な別ユーザーの append_pending_log までブロックされてしまう。
        # 取得順序は必ず flush_lock → io_lock（逆順で取る箇所を作らないこと）。
        async with self.flush_lock:
            async with self.io_lock:
                await run_in_thread(self.ensure_storage)
                user_ids = await run_in_thread(self._db.pending_distinct_user_ids)
            if not user_ids:
                return

            # 他人の会話ログを同じ Gemini 呼び出しに混ぜないため、ユーザーごとに
            # 個別に圧縮する。1 回のフラッシュで処理する人数は上限で区切り、
            # 一度に大量の API 呼び出しが走らないようにする（残りは次回の見回りで処理）。
            processed = 0
            for user_id in user_ids:
                if processed >= MEMORY_FLUSH_MAX_USERS_PER_RUN:
                    break
                async with self.io_lock:
                    entries = await run_in_thread(self._db.pending_fetch_entries, str(user_id))
                if not entries:
                    continue
                # _is_flush_required は同期の純粋判定なのでロックは要らない。
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

        **Gemini 呼び出し（`_compact_memory`）の間は io_lock を手放す**（他ユーザーの
        会話ログ追記を数十秒ブロックしないため）。そのぶん「待っている間に増えたもの」を
        巻き込まないよう、次の2点を守ること:
          - pending_log の削除は圧縮に使った行の最大 id までに限定する。
          - projects.json は書き込み直前に読み直し、増えた文書要約を継ぎ足す。
        """
        pending_text = self._format_pending_entries(entries)
        # 圧縮対象に含まれた行の最大 id。削除範囲はここまでに限定する
        # （Gemini を待っている間に追記された新しい会話ログを道連れに消さないため）。
        max_entry_id = self._max_entry_id(entries)

        async with self.io_lock:
            profile_snapshot = await run_in_thread(self._db.profile_load, user_id)
            wrapper = await run_in_thread(self._load_projects_wrapper)
            # スナップショットと同じロック区間で世代を記録する。
            # 圧縮中に上限から押し出された要約があっても、追加件数を失わない。
            document_summary_version = self._document_summary_versions.get(user_id, 0)
        projects_snapshot = self._user_projects(wrapper, user_id)
        profile_text = json.dumps(profile_snapshot, ensure_ascii=False, indent=2)
        projects_text = json.dumps(projects_snapshot, ensure_ascii=False, indent=2)

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
            async with self.io_lock:
                await run_in_thread(self._db.pending_clear_upto, user_id, max_entry_id)
            return
        except Exception as error:
            await self._register_compact_failure(
                user_id, f"記憶圧縮エラー (user_id={user_id}): {error}", max_entry_id
            )
            return

        if profile_data is None or projects_data is None:
            await self._register_compact_failure(
                user_id,
                f"記憶圧縮エラー (user_id={user_id}): JSONの生成またはパースに失敗したため、pending_log は保持します。",
                max_entry_id,
            )
            return

        self._compact_fail_count[user_id] = 0
        async with self.io_lock:
            await run_in_thread(self._db.profile_replace_all, user_id, profile_data)
            # projects.json はこのユーザーのスライスだけ差し替える（他ユーザーの分は保持）。
            # 圧縮前のスナップショットではなく読み直すのは、待っている間に別経路
            # （文書要約の保存）が同じファイルを更新している可能性があるため。
            wrapper = await run_in_thread(self._load_projects_wrapper)
            await run_in_thread(
                self._save_compacted_projects, wrapper, user_id, projects_data, document_summary_version,
            )
            await run_in_thread(self._db.pending_clear_upto, user_id, max_entry_id)

    def _save_compacted_projects(self, wrapper, user_id, projects_data, document_summary_version) -> None:
        self._merge_new_document_summaries(wrapper, user_id, projects_data, document_summary_version)
        wrapper["users"][user_id] = projects_data
        write_json_file(PROJECTS_MEMORY_PATH, wrapper)

    @staticmethod
    def _max_entry_id(entries: list[dict[str, Any]]) -> int:
        """圧縮対象の pending_log 行の最大 id を返す（削除範囲の上限に使う）。

        `pending_fetch_entries` は `SELECT *` なので各行に AUTOINCREMENT の id が入る。
        万一 id を読めない行しか無い場合は 0 を返し、結果として何も削除されない
        （消しすぎるより、次回の見回りでやり直す方が安全なため）。
        """
        ids: list[int] = []
        for entry in entries:
            try:
                ids.append(int(entry["id"]))
            except (KeyError, TypeError, ValueError):
                continue
        return max(ids) if ids else 0

    def _merge_new_document_summaries(
        self,
        wrapper: dict[str, Any],
        user_id: str,
        projects_data: dict[str, Any],
        previous_version: int,
    ) -> None:
        """圧縮中に追記された文書要約を、LLM の出力（projects_data）へ継ぎ足す。

        **呼び出し側が io_lock を保持していること**（この関数はロックを取らない）。

        `_compact_memory` を待っている間に `store_document_summary` が
        document_summaries へ追記していると、LLM 出力での丸ごと差し替えでその分が
        消えてしまう。圧縮前からの更新世代の差で追加件数を求め、末尾から拾い直す。
        世代は実行中の圧縮にだけ必要で、プロセス再起動後には引き継がない。
        型が期待どおりでなければ安全側に倒して何もしない。
        """
        added_count = self._document_summary_versions.get(user_id, 0) - previous_version
        latest = self._user_projects(wrapper, user_id).get("document_summaries")
        if not isinstance(latest, list) or added_count <= 0:
            return

        merged = projects_data.get("document_summaries")
        if not isinstance(merged, list):
            merged = []
        merged.extend(latest[-added_count:])
        # 既存の store_document_summary と同じく、新しい方から上限件数だけ残す。
        projects_data["document_summaries"] = merged[-DOCUMENT_SUMMARY_LIMIT:]

    @staticmethod
    def _format_pending_entries(entries: list[dict[str, Any]]) -> str:
        """DB の pending_log 行を、記憶圧縮プロンプトへ渡すテキスト形式に整形する。"""
        lines: list[str] = []
        for entry in entries:
            lines.append(f"[{entry['created_at']}] User: {entry['question']}")
            lines.append(f"[{entry['created_at']}] Bot: {entry['response']}")
        return "\n".join(lines)

    async def _register_compact_failure(self, user_id: str, log_message: str, max_entry_id: int) -> None:
        """記憶圧縮の失敗を記録する。基本は pending_log を保持してリトライするが、
        連続失敗が上限に達したらそのユーザーの分をクリアして無限リトライを防ぐ。

        `max_entry_id` は圧縮対象だった行の最大 id。クリアはそこまでに限定する
        （Gemini 呼び出し中に追記された新しい会話ログまで消さないため）。
        """
        print(log_message)
        count = self._compact_fail_count.get(user_id, 0) + 1
        self._compact_fail_count[user_id] = count
        if count >= MEMORY_COMPACT_MAX_FAILURES:
            print(f"記憶圧縮: 連続失敗のため pending_log をクリアします (user_id={user_id})。")
            async with self.io_lock:
                await run_in_thread(self._db.pending_clear_upto, user_id, max_entry_id)
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
        compactor_prompt = build_memory_compaction_prompt(
            pending_text,
            profile_text,
            projects_text,
            ANNIVERSARY_PROFILE_KEY,
        )

        response = await generate_content(
            self._genai_client, model=ROUTER_MODEL_NAME, contents=compactor_prompt,
            timeout=MEMORY_COMPACT_TIMEOUT_SECONDS, temperature=0, response_mime_type="application/json",
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
