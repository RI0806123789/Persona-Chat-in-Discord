"""記憶圧縮と文書要約の保持を、一時 DB と架空のデータで検証する。"""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.core.background import spawn
from persona_bot.config.constants import DOCUMENT_SUMMARY_LIMIT, MEMORY_COMPACT_MAX_FAILURES
from persona_bot.core.database import Database
from persona_bot.services import memory_service
from persona_bot.services.memory_service import MemoryService, default_projects_memory
from persona_bot.utils.storage import load_json_file, write_json_file


class DocumentSummaryCompactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.projects_path = root / "projects.json"
        self.paths = patch.multiple(
            memory_service,
            MEMORY_DIR=root,
            PROFILE_MEMORY_PATH=root / "profile.json",
            PROJECTS_MEMORY_PATH=self.projects_path,
        )
        self.paths.start()
        self.addCleanup(self.paths.stop)
        self.db = Database(root / "bot.db")
        self.db.initialize()
        self.addCleanup(self.db.close)
        self.service = MemoryService(None, None, self.db)

    async def test_existing_users_survive_missing_or_incorrect_schema_and_repeated_load(self) -> None:
        for schema in (None, "誤った識別子"):
            with self.subTest(schema=schema):
                wrapper = {"users": {"test-user": {"計画": "架空の計画"}}, "メモ": "保持する値"}
                if schema is not None:
                    wrapper["_schema"] = schema
                write_json_file(self.projects_path, wrapper)
                result = self.service._load_projects_wrapper()
                self.assertEqual(result["users"], wrapper["users"])
                self.assertEqual(result["メモ"], "保持する値")
                self.assertEqual(self.service._load_projects_wrapper(), result)
                self.assertEqual(load_json_file(self.projects_path, {}), result)

    async def check_user_failure_isolation(self, boundary):
        wrapper = default_projects_memory()
        wrapper["users"] = {"user-a": {"note": "元の計画"}, "user-b": {"note": "別の計画"}}
        write_json_file(self.projects_path, wrapper)
        for user_id in ("user-a", "user-b"):
            self.db.profile_replace_all(user_id, {"note": "元のプロフィール"})
            await self.service.append_pending_log(user_id, "架空の質問", "架空の応答")
        entries = self.db.pending_fetch_entries("user-a")
        self.service._compact_fail_count["user-a"] = 1
        self.service._compact_memory = AsyncMock(return_value=({"note": "新しいプロフィール"}, {"note": "新しい計画"}))
        target, name = {
            "entries": (self.db, "pending_fetch_entries"),
            "snapshot": (self.service, "_load_memory_snapshot"),
            "save": (self.service, "_save_compacted_projects"),
        }[boundary]
        original = getattr(target, name)

        def fail_first(*args):
            user_id = args[1] if boundary == "save" else args[0]
            if user_id == "user-a":
                raise OSError("架空のユーザー固有の保存・読込失敗")
            return original(*args)

        with patch.object(target, name, side_effect=fail_first):
            await self.service.flush_if_needed(force=True)
        self.assertEqual(self.db.pending_fetch_entries("user-a"), entries)
        self.assertEqual(self.db.profile_load("user-a"), {"note": "元のプロフィール"})
        self.assertEqual(load_json_file(self.projects_path, {})["users"]["user-a"], {"note": "元の計画"})
        self.assertEqual(self.service._compact_fail_count["user-a"], 1)
        self.assertEqual(self.db.pending_fetch_entries("user-b"), [])
        self.assertEqual(self.db.profile_load("user-b"), {"note": "新しいプロフィール"})
        self.assertEqual(load_json_file(self.projects_path, {})["users"]["user-b"], {"note": "新しい計画"})
        # 障害解消後には失敗したユーザーだけが再処理される。
        await self.service.flush_if_needed(force=True)
        self.assertEqual(self.db.pending_fetch_entries("user-a"), [])
        self.assertEqual(self.service._compact_fail_count["user-a"], 0)

    async def test_save_error_preserves_data_and_continues_with_next_user(self):
        await self.check_user_failure_isolation("save")

    async def test_snapshot_read_error_preserves_data_and_continues_with_next_user(self):
        await self.check_user_failure_isolation("snapshot")

    async def test_pending_read_error_preserves_data_and_continues_with_next_user(self):
        await self.check_user_failure_isolation("entries")

    async def test_shared_failure_and_user_attempt_limits_bound_each_run(self):
        for index in range(7):
            await self.service.append_pending_log(f"user-{index}", "架空の質問", "架空の応答")
        original = [self.db.pending_fetch_entries(f"user-{index}") for index in range(7)]
        with patch.object(self.service, "_flush_user", new=AsyncMock(side_effect=OSError("架空の共有障害"))) as flush:
            await self.service.flush_if_needed(force=True)
            self.assertEqual(flush.await_count, memory_service.MEMORY_FLUSH_MAX_CONSECUTIVE_ERRORS)
        with patch.object(self.db, "pending_fetch_entries", side_effect=OSError("架空のDB障害")) as read, \
                patch.object(memory_service, "MEMORY_FLUSH_MAX_USERS_PER_RUN", 2), \
                patch.object(memory_service, "MEMORY_FLUSH_MAX_CONSECUTIVE_ERRORS", 10):
            await self.service.flush_if_needed(force=True)
            self.assertEqual(read.call_count, 2)
        self.assertEqual([self.db.pending_fetch_entries(f"user-{index}") for index in range(7)], original)

    async def test_flush_cancellation_propagates_and_preserves_all_pending(self):
        for user_id in ("user-a", "user-b"):
            await self.service.append_pending_log(user_id, "架空の質問", "架空の応答")
        with patch.object(self.service, "_flush_user", new=AsyncMock(side_effect=asyncio.CancelledError)) as flush:
            with self.assertRaises(asyncio.CancelledError):
                await self.service.flush_if_needed(force=True)
            flush.assert_awaited_once()
        self.assertFalse(self.service.io_lock.locked())
        self.assertFalse(self.service.flush_lock.locked())
        self.assertEqual([len(self.db.pending_fetch_entries(user_id)) for user_id in ("user-a", "user-b")], [1, 1])

    async def test_projects_context_excludes_document_summaries_and_invalid_text(self) -> None:
        wrapper = default_projects_memory()
        wrapper["users"] = {"test-user": {
            "計画": "架空の計画",
            "document_summaries": [
                {"document_name": "old.txt", "summary": "古い要約"},
                {"document_name": "invalid.txt", "summary": None},
                {"document_name": None, "summary": "有効な要約"},
                {"document_name": "invalid.txt", "summary": {"無効": "要約"}},
            ],
        }}
        write_json_file(self.projects_path, wrapper)
        context, documents = await self.service.build_contexts("projects", "test-user", limit=3)
        self.assertEqual(json.loads(context), {"計画": "架空の計画"})
        self.assertEqual(documents, "- unknown: 有効な要約")

    async def test_force_flush_waits_for_ordinary_flush_and_processes_new_entries(self) -> None:
        await self.service.append_pending_log("test-user", "最初の架空の質問", "架空の回答")
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def compact(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await release.wait()
            return {}, {}

        self.service._compact_memory = AsyncMock(side_effect=compact)
        with patch.object(self.service, "_is_flush_required", return_value=True):
            ordinary = spawn(self.service.flush_if_needed(), label="通常フラッシュの確認")
            forced = None
            try:
                await asyncio.wait_for(started.wait(), 2)
                await self.service.append_pending_log("test-user", "追加の架空の質問", "架空の回答")
                forced = spawn(self.service.flush_if_needed(force=True), label="強制フラッシュの確認")
                await asyncio.sleep(0)
                self.assertFalse(forced.done())
                # 通常の見回りだけは重複させず、すぐに終了する。
                await asyncio.wait_for(self.service.flush_if_needed(), 1)
                release.set()
                await asyncio.wait_for(asyncio.gather(ordinary, forced), 3)
            finally:
                release.set()
                tasks = [task for task in (ordinary, forced) if task is not None]
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(calls, 2)
        self.assertEqual(self.db.pending_fetch_entries("test-user"), [])

    async def check_additions(
        self, initial_count: int, addition_count: int, *, llm_summaries: list | None = None,
    ) -> None:
        """圧縮の前後で文書要約が失われないことを確かめる。

        llm_summaries を渡すと、LLM が要約を作り話で出力した場合を再現する。
        """
        old = [{"document_name": f"old-{i}", "summary": f"既存の要約-{i}"}
               for i in range(initial_count)]
        other = {"document_summaries": [{"document_name": "other", "summary": "別ユーザー"}]}
        wrapper = default_projects_memory()
        wrapper["users"] = {
            "test-user": {"project_note": "圧縮前のメモ", "document_summaries": old},
            "other-user": other,
        }
        write_json_file(self.projects_path, wrapper)
        await self.service.append_pending_log("test-user", "架空の質問", "架空の応答")
        started = asyncio.Event()
        resume = asyncio.Event()

        async def compact(pending_text, profile_text, projects_text):
            snapshot = json.loads(projects_text)
            # 文書要約は LLM へ渡さない（#73）。他のプロジェクト記憶は渡す。
            self.assertNotIn("document_summaries", snapshot)
            self.assertNotIn("既存の要約", projects_text)
            self.assertEqual(snapshot["project_note"], "圧縮前のメモ")
            started.set()
            await resume.wait()
            # LLM の出力は文書要約を含まない（または作り話を含む）丸ごとの差し替え。
            snapshot["project_note"] = "圧縮結果"
            if llm_summaries is not None:
                snapshot["document_summaries"] = deepcopy(llm_summaries)
            return {}, snapshot

        self.service._compact_memory = AsyncMock(side_effect=compact)
        task = spawn(self.service.flush_if_needed(force=True), label="記憶圧縮の回帰テスト")
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            for i in range(addition_count):
                # 名前が同じでも、追加ごとの要約を区別して保持する。
                await self.service.store_document_summary(
                    "test-user", {"document_name": "same.txt", "summary": f"追加-{i}"}
                )
            await self.service.store_document_summary(
                "other-user", {"document_name": "other-new", "summary": "他ユーザーの追加"}
            )
            await self.service.append_pending_log("test-user", "圧縮中の質問", "圧縮中の応答")
            latest = load_json_file(self.projects_path, {})
            other_latest = deepcopy(latest["users"]["other-user"])
            resume.set()
            await asyncio.wait_for(task, timeout=5)
        finally:
            resume.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        result = load_json_file(self.projects_path, {})
        projects = result["users"]["test-user"]
        summaries = projects["document_summaries"]
        # 圧縮直前の一覧（既存＋圧縮中の追加。上限超過分は古い方から押し出し済み）がそのまま残る。
        self.assertEqual(summaries, latest["users"]["test-user"]["document_summaries"])
        expected = [s["summary"] for s in old] + [f"追加-{i}" for i in range(addition_count)]
        self.assertEqual([s["summary"] for s in summaries], expected[-DOCUMENT_SUMMARY_LIMIT:])
        self.assertNotIn("作り話", json.dumps(projects, ensure_ascii=False))
        self.assertEqual(projects["project_note"], "圧縮結果")
        self.assertEqual(result["users"]["other-user"], other_latest)
        entries = self.db.pending_fetch_entries("test-user")
        self.assertEqual([entry["question"] for entry in entries], ["圧縮中の質問"])

    async def test_existing_summaries_survive_when_llm_omits_them(self) -> None:
        # #73: 圧縮中に追加が無くても、圧縮前からの要約が消えない。
        await self.check_additions(3, 0)

    async def prepare_compaction_save(self) -> list[dict]:
        self.original = default_projects_memory()
        self.original["users"] = {"test-user": {"note": "既存の記憶"}, "other-user": {"note": "他人の記憶"}}
        write_json_file(self.projects_path, self.original)
        self.db.profile_replace_all("test-user", {"note": "既存のプロフィール"})
        await self.service.append_pending_log("test-user", "架空の質問", "架空の回答")
        self.service._compact_fail_count["test-user"] = 2
        self.service._compact_memory = AsyncMock(return_value=({"note": "新しいプロフィール"}, {"note": "新しい記憶"}))
        return self.db.pending_fetch_entries("test-user")

    async def test_json_save_failure_rolls_back_profile_and_pending_log_then_retries(self) -> None:
        entries = await self.prepare_compaction_save()
        with patch.object(memory_service, "write_json_file", side_effect=OSError("架空のJSON保存失敗")):
            with self.assertRaises(OSError):
                await self.service._flush_user("test-user", entries)
        self.assertEqual(self.db.profile_load("test-user"), {"note": "既存のプロフィール"})
        self.assertEqual(self.db.pending_fetch_entries("test-user"), entries)
        self.assertEqual(load_json_file(self.projects_path, {}), self.original)
        self.assertEqual(self.service._compact_fail_count["test-user"], 2)
        await self.service._flush_user("test-user", entries)
        self.assertEqual(self.db.profile_load("test-user"), {"note": "新しいプロフィール"})
        self.assertEqual(self.db.pending_fetch_entries("test-user"), [])
        self.assertEqual(self.service._compact_fail_count["test-user"], 0)

    async def test_database_commit_failure_restores_json_profile_and_pending_log(self) -> None:
        entries = await self.prepare_compaction_save()
        connection = self.db._conn

        class FailedCommit:
            def __getattr__(self, name):
                return getattr(connection, name)

            def __enter__(self):
                return connection.__enter__()

            def __exit__(self, kind, error, traceback):
                if kind is None:
                    connection.rollback()
                    raise OSError("架空のDB確定失敗")
                return connection.__exit__(kind, error, traceback)

        with patch.object(self.db, "_conn", FailedCommit()):
            with self.assertRaises(OSError):
                await self.service._flush_user("test-user", entries)
        self.assertEqual(load_json_file(self.projects_path, {}), self.original)
        self.assertEqual(self.db.profile_load("test-user"), {"note": "既存のプロフィール"})
        self.assertEqual(self.db.pending_fetch_entries("test-user"), entries)
        self.assertEqual(self.service._compact_fail_count["test-user"], 2)

    async def test_repeated_cancellation_finishes_all_saves_before_releasing_lock(self) -> None:
        entries = await self.prepare_compaction_save()
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        original_save = self.service._save_compacted_projects

        def delayed_save(*args):
            loop.call_soon_threadsafe(started.set)
            if not release.wait(timeout=5):
                raise TimeoutError("架空の保存待機が終了しませんでした")
            original_save(*args)

        with patch.object(self.service, "_save_compacted_projects", side_effect=delayed_save):
            task = asyncio.create_task(self.service._flush_user("test-user", entries))
            pending = None
            try:
                await asyncio.wait_for(started.wait(), 2)
                pending = asyncio.create_task(self.service.append_pending_log("test-user", "保存中の質問", "保存中の回答"))
                for _ in range(2):
                    task.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(task.done())
                    self.assertTrue(self.service.io_lock.locked())
                    self.assertFalse(pending.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                await pending
            finally:
                release.set()
                await asyncio.gather(task, *([pending] if pending else []), return_exceptions=True)
        self.assertEqual(self.db.profile_load("test-user"), {"note": "新しいプロフィール"})
        self.assertEqual(load_json_file(self.projects_path, {})["users"]["test-user"], {"note": "新しい記憶"})
        self.assertEqual([entry["question"] for entry in self.db.pending_fetch_entries("test-user")], ["保存中の質問"])
        self.assertEqual(self.service._compact_fail_count["test-user"], 0)

    async def test_summaries_invented_by_llm_are_discarded(self) -> None:
        invented = [{"document_name": "fake.txt", "summary": "LLMの作り話"}]
        await self.check_additions(3, 1, llm_summaries=invented)
        await self.check_additions(0, 0, llm_summaries=invented)

    async def test_user_without_summaries_does_not_gain_the_key(self) -> None:
        wrapper = default_projects_memory()
        wrapper["users"] = {"test-user": {"project_note": "圧縮前のメモ"}}
        write_json_file(self.projects_path, wrapper)
        await self.service.append_pending_log("test-user", "架空の質問", "架空の応答")

        async def compact(pending_text, profile_text, projects_text):
            snapshot = json.loads(projects_text)
            snapshot["document_summaries"] = [{"document_name": "fake.txt", "summary": "LLMの作り話"}]
            return {}, snapshot

        self.service._compact_memory = AsyncMock(side_effect=compact)
        await self.service.flush_if_needed(force=True)
        projects = load_json_file(self.projects_path, {})["users"]["test-user"]
        self.assertEqual(projects, {"project_note": "圧縮前のメモ"})

    async def test_malformed_summaries_are_kept_as_is(self) -> None:
        # 手書きなどで型が崩れていても、圧縮では書き換えずに残す。
        wrapper = default_projects_memory()
        wrapper["users"] = {"test-user": {"document_summaries": {"手書き": "架空の要約"}}}
        write_json_file(self.projects_path, wrapper)
        await self.service.append_pending_log("test-user", "架空の質問", "架空の応答")
        self.service._compact_memory = AsyncMock(return_value=({}, {"project_note": "圧縮結果"}))
        await self.service.flush_if_needed(force=True)
        projects = load_json_file(self.projects_path, {})["users"]["test-user"]
        self.assertEqual(projects["document_summaries"], {"手書き": "架空の要約"})
        self.assertEqual(projects["project_note"], "圧縮結果")

    async def test_addition_at_limit_is_preserved(self) -> None:
        await self.check_additions(DOCUMENT_SUMMARY_LIMIT, 1)

    async def test_two_additions_crossing_limit_are_preserved(self) -> None:
        await self.check_additions(DOCUMENT_SUMMARY_LIMIT - 1, 2)

    async def test_more_than_limit_additions_keep_latest(self) -> None:
        await self.check_additions(DOCUMENT_SUMMARY_LIMIT, DOCUMENT_SUMMARY_LIMIT + 2)

    async def test_first_summary_added_during_compaction(self) -> None:
        await self.check_additions(0, 1)

    async def test_other_user_additions_do_not_change_compacted_summaries(self) -> None:
        await self.check_additions(DOCUMENT_SUMMARY_LIMIT, 0)

    async def test_next_compaction_does_not_duplicate_previous_additions(self) -> None:
        await self.check_additions(DOCUMENT_SUMMARY_LIMIT, 1)
        before = load_json_file(self.projects_path, {})

        async def compact(pending_text, profile_text, projects_text):
            return {}, json.loads(projects_text)

        self.service._compact_memory = AsyncMock(side_effect=compact)
        await self.service.flush_if_needed(force=True)
        self.assertEqual(load_json_file(self.projects_path, {}), before)
        self.assertEqual(self.db.pending_fetch_entries("test-user"), [])


class CompactionFailureCountTests(unittest.IsolatedAsyncioTestCase):
    """プロンプトブロックでログを消したら、連続失敗の回数も戻すことを確認する（#66）。"""

    async def asyncSetUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        paths = patch.multiple(
            memory_service, MEMORY_DIR=root,
            PROFILE_MEMORY_PATH=root / "profile.json", PROJECTS_MEMORY_PATH=root / "projects.json",
        )
        paths.start()
        self.addCleanup(paths.stop)
        quiet = patch("builtins.print")
        quiet.start()
        self.addCleanup(quiet.stop)
        self.db = Database(root / "bot.db")
        self.db.initialize()
        self.addCleanup(self.db.close)
        self.service = MemoryService(None, None, self.db)

    async def flush_once(self, error: Exception) -> None:
        self.service._compact_memory = AsyncMock(side_effect=error)
        entries = self.db.pending_fetch_entries("test-user")
        await self.service._flush_user("test-user", entries)

    async def test_prompt_block_resets_failure_count_for_new_logs(self) -> None:
        await self.service.append_pending_log("test-user", "架空の質問1", "架空の応答")
        await self.flush_once(RuntimeError("架空の一時エラー"))
        self.assertEqual(self.service._compact_fail_count["test-user"], 1)
        await self.flush_once(memory_service._PromptBlockedError("架空のブロック"))
        self.assertEqual(self.db.pending_fetch_entries("test-user"), [])
        self.assertEqual(self.service._compact_fail_count.get("test-user", 0), 0)
        # 新しいログは、上限未満の一時エラーでは消えない
        await self.service.append_pending_log("test-user", "架空の質問2", "架空の応答")
        for _ in range(MEMORY_COMPACT_MAX_FAILURES - 1):
            await self.flush_once(RuntimeError("架空の一時エラー"))
        self.assertEqual(len(self.db.pending_fetch_entries("test-user")), 1)
        await self.flush_once(RuntimeError("架空の一時エラー"))
        self.assertEqual(self.db.pending_fetch_entries("test-user"), [])


if __name__ == "__main__":
    unittest.main()
