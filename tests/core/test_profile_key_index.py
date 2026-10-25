"""記念日巡回のキー検索索引と、既存のユーザー別記憶の互換性を確認する（#249）。"""

from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.core import database
from persona_bot.core.database import Database, SCHEMA_VERSION


class ProfileKeyIndexTests(unittest.TestCase):
    INDEX = "idx_profile_key_user"
    SELECT = "SELECT user_id, value FROM profile WHERE key = ?"

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "bot.db"
        self.db = Database(self.path)
        self.addCleanup(lambda: self.db.close())

    def plan(self, statement: str, *params) -> str:
        with closing(sqlite3.connect(self.path)) as connection:
            return " / ".join(row[3] for row in connection.execute(
                f"EXPLAIN QUERY PLAN {statement}", params,
            ))

    def reopen(self) -> None:
        self.db.close()
        self.db = Database(self.path)
        self.db.initialize()
        self.db.initialize()

    def assert_index(self) -> None:
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            indices = [row[1] for row in connection.execute("PRAGMA index_list(profile)")]
            self.assertEqual(indices.count(self.INDEX), 1)
            self.assertEqual([row[2] for row in connection.execute(
                f"PRAGMA index_info({self.INDEX})",
            )], ["key", "user_id"])

    def test_fresh_database_and_reinitialization_create_one_index(self) -> None:
        self.db.initialize()
        self.db.profile_merge("user-a", {"趣味": "架空の工作"})
        self.reopen()
        self.assert_index()
        self.assertEqual(self.db.profile_load("user-a"), {"趣味": "架空の工作"})

    def test_version_thirteen_migration_preserves_missing_and_invalid_json(self) -> None:
        with patch.object(database, "SCHEMA_VERSION", 13):
            self.db.initialize()
        self.db.profile_merge("user-a", {"anniversaries": [{"label": "架空の記念日", "month": 5, "day": 1}]})
        self.db.profile_merge("user-b", {"anniversaries": [], "趣味": "架空の読書"})
        self.db.profile_merge("user-c", {"anniversaries": None})
        with self.db._lock, self.db._conn:
            self.db._conn.execute("INSERT INTO profile VALUES (?, ?, ?)", ("user-d", "anniversaries", "{不正"))
        before = self.db.profile_load_all_for_key("anniversaries")
        self.assertEqual(before["user-c"], None)
        self.assertEqual(before["user-d"], None)
        self.reopen()
        self.assert_index()
        self.assertEqual(self.db.profile_load_all_for_key("anniversaries"), before)
        self.assertEqual(self.db.profile_load_all_for_key("存在しないキー"), {})
        self.assertEqual(self.db.profile_load("user-b"), {"anniversaries": [], "趣味": "架空の読書"})

    def test_representative_database_searches_index_and_preserves_results(self) -> None:
        with patch.object(database, "SCHEMA_VERSION", 13):
            self.db.initialize()
        # Issueの再現と同じ10万件の無関係なキーと、10人の記念日だけを使う。
        with self.db._lock, self.db._conn:
            self.db._conn.executemany("INSERT INTO profile VALUES (?, ?, ?)", (
                (f"user-{user}", f"key-{key}", '"架空の値"')
                for user in range(1000) for key in range(100)
            ))
        expected = {}
        for user in range(10):
            value = [{"label": "架空の記念日", "month": 5, "day": user + 1}]
            expected[f"user-{user}"] = value
            self.db.profile_merge(f"user-{user}", {"anniversaries": value})
        self.assertIn("SCAN profile", self.plan(self.SELECT, "anniversaries"))
        self.assertEqual(self.db.profile_load_all_for_key("anniversaries"), expected)
        self.reopen()
        plan = self.plan(self.SELECT, "anniversaries")
        self.assertIn(self.INDEX, plan)
        self.assertIn("SEARCH profile", plan)
        self.assertNotIn("SCAN profile", plan)
        self.assertEqual(self.db.profile_load_all_for_key("anniversaries"), expected)
        # 従来のユーザー単位取得・削除は主キーの先頭を引き続き使える。
        for statement in ("SELECT key, value FROM profile WHERE user_id = ?",
                          "DELETE FROM profile WHERE user_id = ?"):
            with self.subTest(statement=statement):
                user_plan = self.plan(statement, "user-1")
                self.assertIn("sqlite_autoindex_profile_1", user_plan)
                self.assertNotIn("SCAN profile", user_plan)

    def test_merge_replacement_and_compaction_keep_index_consistent_and_users_separate(self) -> None:
        self.db.initialize()
        self.db.profile_merge("user-a", {"anniversaries": ["古い架空の記念日"], "趣味": "架空の工作"})
        self.db.profile_merge("user-b", {"anniversaries": ["別の架空の記念日"]})
        self.db.profile_merge("user-a", {"anniversaries": ["新しい架空の記念日"]})
        self.assertEqual(self.db.profile_load_all_for_key("anniversaries")["user-a"], ["新しい架空の記念日"])
        self.db.profile_replace_all("user-a", {"趣味": "架空の読書"})
        self.assertEqual(self.db.profile_load_all_for_key("anniversaries"), {"user-b": ["別の架空の記念日"]})
        self.db.pending_append("user-a", "2026-01-01", "架空の質問", "架空の回答")
        max_id = self.db.pending_fetch_entries("user-a")[-1]["id"]
        compacted = {"anniversaries": [{"label": "架空の記念日", "month": 1, "day": 1}]}

        def fail_projects_save() -> None:
            raise OSError("架空の保存失敗")

        with self.assertRaises(OSError):
            self.db.save_compacted_memory("user-a", compacted, max_id, fail_projects_save)
        self.assertNotIn("user-a", self.db.profile_load_all_for_key("anniversaries"))
        self.assertEqual(len(self.db.pending_fetch_entries("user-a")), 1)
        self.db.save_compacted_memory("user-a", compacted, max_id, lambda: None)
        self.assertEqual(self.db.profile_load("user-a"), compacted)
        self.assertEqual(self.db.profile_load_all_for_key("anniversaries"), {
            "user-a": compacted["anniversaries"], "user-b": ["別の架空の記念日"],
        })
        self.assertEqual(self.db.pending_fetch_entries("user-a"), [])


if __name__ == "__main__":
    unittest.main()
