"""旧JSONのnullと、通常保存のNoneを同じ既定値で扱えることを検証する。"""

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.config.constants import STATS_HOURLY_BUCKETS
from persona_bot.core.database import Database


class NullableJsonValuesTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = Database(self.root / "bot.db", legacy_dir=self.root)
        self.addCleanup(self.db.close)

    @staticmethod
    def stats_entry(value):
        return dict.fromkeys((
            "user_name", "total_count", "hourly", "first_seen", "last_seen", "recent_samples", "analysis",
        ), value)

    @staticmethod
    def queue_task(value):
        return {
            "task_id": "fictional-task",
            **dict.fromkeys((
                "status", "channel_id", "message_id", "author_id", "question", "attachment_count",
                "created_at", "updated_at", "finished_at", "error",
            ), value),
        }

    def assert_stats_defaults(self, user_id: str) -> None:
        self.assertEqual(self.db.stats_load_all()[user_id], {
            "user_name": "", "total_count": 0, "hourly": [0] * STATS_HOURLY_BUCKETS,
            "first_seen": "", "last_seen": "", "recent_samples": [], "analysis": None, "opt_out": False,
        })

    def assert_queue_defaults(self) -> None:
        # pendingとして復元されるので、文書サービスが回収して処理を再開できる。
        task = self.db.queue_claim_next_pending("2026-01-01T00:00:00+00:00")
        self.assertIsNotNone(task)
        self.assertEqual(task, {
            "task_id": "fictional-task", "status": "processing", "channel_id": 0, "message_id": 0,
            "author_id": 0, "question": "", "attachment_count": 0, "created_at": "",
            "updated_at": "2026-01-01T00:00:00+00:00", "finished_at": None, "error": None,
        })

    def test_legacy_null_statistics_are_imported_and_backed_up(self) -> None:
        path = self.root / "user_stats.json"
        path.write_text(json.dumps({"11": self.stats_entry(None)}), encoding="utf-8")
        self.db.initialize()
        self.assert_stats_defaults("11")
        self.assertFalse(path.exists())
        self.assertTrue(path.with_name("user_stats.json.imported.bak").exists())

    def test_statistics_upsert_and_full_save_accept_null_and_missing_fields(self) -> None:
        self.db.initialize()
        for entry in ({}, self.stats_entry(None)):
            with self.subTest(entry=entry):
                self.db.stats_save_all({"11": entry})
                self.db.stats_save_user("22", entry)
                self.assert_stats_defaults("11")
                self.assert_stats_defaults("22")

    def test_legacy_null_queue_values_are_imported_and_backed_up(self) -> None:
        path = self.root / "queue.json"
        path.write_text(json.dumps({"tasks": [self.queue_task(None)]}), encoding="utf-8")
        self.db.initialize()
        self.assert_queue_defaults()
        self.assertFalse(path.exists())
        self.assertTrue(path.with_name("queue.json.imported.bak").exists())

    def test_queue_upsert_accepts_null_and_missing_fields(self) -> None:
        self.db.initialize()
        for task in ({"task_id": "fictional-task"}, self.queue_task(None)):
            with self.subTest(task=task):
                self.db.queue_upsert_task(task)
                self.assert_queue_defaults()


if __name__ == "__main__":
    unittest.main()
