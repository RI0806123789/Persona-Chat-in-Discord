"""記憶圧縮中の文書要約保存を、一時 DB と架空のデータで検証する。"""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.core.background import spawn
from persona_bot.config.constants import DOCUMENT_SUMMARY_LIMIT
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

    async def check_additions(self, initial_count: int, addition_count: int) -> None:
        old = [{"document_name": f"old-{i}", "summary": "既存の要約"}
               for i in range(initial_count)]
        other = {"document_summaries": [{"document_name": "other", "summary": "別ユーザー"}]}
        wrapper = default_projects_memory()
        wrapper["users"] = {"test-user": {"document_summaries": old}, "other-user": other}
        write_json_file(self.projects_path, wrapper)
        await self.service.append_pending_log("test-user", "架空の質問", "架空の応答")
        started = asyncio.Event()
        resume = asyncio.Event()

        async def compact(pending_text, profile_text, projects_text):
            snapshot = json.loads(projects_text)
            self.assertEqual(snapshot["document_summaries"], old)
            started.set()
            await resume.wait()
            for summary in snapshot["document_summaries"]:
                summary["summary"] = "圧縮した要約"
            snapshot["project_note"] = "圧縮結果"
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
        self.assertEqual(len(summaries), min(initial_count + addition_count, DOCUMENT_SUMMARY_LIMIT))
        kept_additions = min(addition_count, DOCUMENT_SUMMARY_LIMIT)
        if kept_additions:
            self.assertEqual(
                [s["summary"] for s in summaries[-kept_additions:]],
                [f"追加-{i}" for i in range(addition_count - kept_additions, addition_count)],
            )
        self.assertEqual(projects["project_note"], "圧縮結果")
        self.assertTrue(all(s["summary"] == "圧縮した要約"
                            for s in summaries[:len(summaries) - kept_additions]))
        self.assertEqual(result["users"]["other-user"], other_latest)
        entries = self.db.pending_fetch_entries("test-user")
        self.assertEqual([entry["question"] for entry in entries], ["圧縮中の質問"])

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


if __name__ == "__main__":
    unittest.main()
