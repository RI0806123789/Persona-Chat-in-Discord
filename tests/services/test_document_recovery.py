"""起動時復旧の一時障害と停止を、一時DBと例外注入で確認する。"""

import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.core.app_state import BotState
from persona_bot.core.database import Database
from persona_bot.services import document_service
from persona_bot.services.document_service import DocumentService


class DocumentRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "bot.db"
        self.db = Database(self.path)
        self.addCleanup(self.db.close)
        self.db.initialize()
        for status in ("processing", "pending", "completed", "failed"):
            self.db.queue_upsert_task({
                "task_id": status, "status": status, "channel_id": 10, "message_id": 20,
                "author_id": 11, "created_at": "2026-01-01T00:00:00+00:00",
            })
        self.service = DocumentService(None, BotState(), None, None, None, self.db)

    async def test_recovery_retries_before_claiming_and_runs_only_once_after_success(self):
        for fail_first in (False, True):
            with self.subTest(fail_first=fail_first):
                self.service._stopping = False
                self.db.queue_update_task("processing", {"status": "processing"}, "fictional-reset")
                self.db.queue_update_task("pending", {"status": "pending"}, "fictional-reset")
                processed = []
                recover = self.db.queue_recover_stuck
                attempts = []

                def recovering(now):
                    attempts.append(now)
                    if fail_first and len(attempts) == 1:
                        raise sqlite3.OperationalError("架空の一時ロック")
                    return recover(now)

                async def process(task):
                    processed.append(task["task_id"])
                    await self.service._update_task(task["task_id"], status="completed")

                async def idle(seconds):
                    self.assertEqual(seconds, document_service.DOCUMENT_WORKER_IDLE_SECONDS)
                    if len(attempts) >= (2 if fail_first else 1):
                        self.service._stopping = True
                    else:
                        # 復旧前に通常取得・処理を始めない。
                        self.assertEqual(processed, [])
                        claim.assert_not_called()

                with patch.object(self.db, "queue_recover_stuck", side_effect=recovering) as recovery, patch.object(
                    self.db, "queue_claim_next_pending", wraps=self.db.queue_claim_next_pending,
                ) as claim, patch.object(self.service, "_process_task", new=AsyncMock(side_effect=process)), patch.object(
                    document_service.asyncio, "sleep", new=AsyncMock(side_effect=idle),
                ) as sleeping:
                    await self.service.run_worker()
                self.assertEqual(recovery.call_count, 2 if fail_first else 1)
                self.assertEqual(sleeping.await_count, 2 if fail_first else 1)
                self.assertCountEqual(processed, ["processing", "pending"])
                with closing(sqlite3.connect(self.path)) as connection:
                    statuses = dict(connection.execute("SELECT task_id, status FROM queue_tasks"))
                self.assertEqual(statuses, {
                    "processing": "completed", "pending": "completed", "completed": "completed", "failed": "failed",
                })

    async def test_continuous_recovery_failure_waits_and_worker_can_stop(self):
        waiting = asyncio.Event()

        async def idle(seconds):
            self.assertGreater(seconds, 0)
            if recovery.call_count == 3:
                waiting.set()
                await asyncio.Event().wait()

        with patch.object(self.db, "queue_recover_stuck", side_effect=sqlite3.OperationalError("架空の一時ロック")) as recovery, patch.object(
            self.db, "queue_claim_next_pending", wraps=self.db.queue_claim_next_pending,
        ) as claim, patch.object(document_service.asyncio, "sleep", new=AsyncMock(side_effect=idle)) as sleeping:
            self.service.start_worker()
            try:
                await asyncio.wait_for(waiting.wait(), 2)
                await asyncio.wait_for(self.service.stop_worker(), 2)
            finally:
                await self.service.stop_worker()
        self.assertEqual(recovery.call_count, 3)
        self.assertEqual(sleeping.await_count, 3)
        self.assertIsNone(self.service._worker)
        claim.assert_not_called()

    async def test_repeated_cancellation_waits_for_recovery_thread(self):
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        finished = threading.Event()
        recover = self.db.queue_recover_stuck

        def recovering(now):
            loop.call_soon_threadsafe(entered.set)
            try:
                if not release.wait(5):
                    raise TimeoutError("復旧テストの待機が終了しませんでした")
                return recover(now)
            finally:
                finished.set()

        with patch.object(self.db, "queue_recover_stuck", side_effect=recovering), patch.object(
            self.db, "queue_claim_next_pending", new=Mock(),
        ) as claim:
            self.service.start_worker()
            worker = self.service._worker
            try:
                await asyncio.wait_for(entered.wait(), 2)
                for _ in range(2):
                    worker.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(worker.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(worker, 2)
            finally:
                release.set()
                await self.service.stop_worker()
        self.assertTrue(finished.is_set())
        claim.assert_not_called()


if __name__ == "__main__":
    unittest.main()
