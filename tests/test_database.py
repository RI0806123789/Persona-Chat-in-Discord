"""新規DBの初期化と、再起動を挟んだギルド・DM設定の保存を検証する。"""

from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from persona_bot.core.app_state import BotState
from persona_bot.core import database
from persona_bot.core.database import Database, SCHEMA_VERSION


class DatabaseInitializationTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "nested" / "bot.db"
        self.db = Database(self.path)
        self.addCleanup(lambda: self.db.close())

    def test_fresh_database_applies_all_migrations(self) -> None:
        self.db.initialize()
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        self.assertTrue({
            "affinity", "user_stats", "queue_tasks", "pending_log", "profile",
            "guild_settings", "anniversary_posts", "reminders", "feedback",
        }.issubset(tables))
        self.assertEqual(self.db.guild_settings_load_all(), {})
        self.assertEqual(self.db.reminder_list_pending("test-user"), [])

    def test_repeated_initialization_preserves_user_data(self) -> None:
        self.db.initialize()
        self.db.profile_merge("test-user", {"趣味": "架空の工作"})
        self.db.profile_merge("other-user", {"趣味": "架空の読書"})
        self.db.pending_append("test-user", "2026-01-01", "架空の質問", "架空の回答")
        self.db.initialize()
        self.db.close()
        self.db = Database(self.path)
        self.db.initialize()
        self.assertEqual(self.db.profile_load("test-user"), {"趣味": "架空の工作"})
        self.assertEqual(self.db.profile_load("other-user"), {"趣味": "架空の読書"})
        self.assertEqual(len(self.db.pending_fetch_entries("test-user")), 1)

    def prepare_version_five(self) -> None:
        # 繰り返し列を追加する前のDBに、架空の既存データを用意する。
        with patch.object(database, "SCHEMA_VERSION", 5):
            self.db.initialize()
        self.db.profile_merge("test-user", {"趣味": "架空の工作"})
        self.db.pending_append("test-user", "2026-01-01", "架空の質問", "架空の回答")
        with self.db._lock, self.db._conn:
            self.db._conn.execute(
                "INSERT INTO reminders"
                " (user_id, channel_id, message, remind_at, created_at)"
                " VALUES ('test-user', 10, '架空の予定', '2026-01-02', '2026-01-01')"
            )

    def assert_version_five_is_intact(self) -> None:
        # 別接続から、失敗したDDLもバージョン番号も永続化されていないことを確認する。
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 5)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(reminders)")}
            self.assertNotIn("recurrence", columns)
            self.assertNotIn("recurrence_anchor", columns)
            self.assertEqual(connection.execute(
                "SELECT reminder_id, user_id, message FROM reminders"
            ).fetchall(), [(1, "test-user", "架空の予定")])
        self.assertEqual(self.db.profile_load("test-user"), {"趣味": "架空の工作"})
        self.assertEqual(len(self.db.pending_fetch_entries("test-user")), 1)

    def assert_restart_completes_migration(self) -> None:
        self.db.close()
        self.db = Database(self.path)
        self.db.initialize()
        self.db.initialize()
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        reminders = self.db.reminder_list_pending("test-user")
        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0]["reminder_id"], 1)
        self.assertEqual(reminders[0]["message"], "架空の予定")
        self.assertEqual(reminders[0]["recurrence"], "none")
        self.assertEqual(reminders[0]["recurrence_anchor"], "")
        self.assertEqual(self.db.profile_load("test-user"), {"趣味": "架空の工作"})
        self.assertEqual(len(self.db.pending_fetch_entries("test-user")), 1)

    def check_failed_migration(self, *, completed_statements: int) -> None:
        self.prepare_version_five()
        statements = database._MIGRATIONS[6]
        broken = [
            *statements[:completed_statements],
            "SELECT * FROM fictional_missing_table",
            *statements[completed_statements:],
        ]
        with patch.dict(database._MIGRATIONS, {6: broken}):
            with self.assertRaisesRegex(sqlite3.OperationalError, "fictional_missing_table"):
                self.db.initialize()
        self.assertFalse(self.db._conn.in_transaction)
        self.assert_version_five_is_intact()
        self.assert_restart_completes_migration()

    def test_failure_after_first_ddl_rolls_back_and_retries(self) -> None:
        self.check_failed_migration(completed_statements=1)

    def test_failure_before_version_update_rolls_back_and_retries(self) -> None:
        self.check_failed_migration(completed_statements=len(database._MIGRATIONS[6]))

    def test_fresh_database_failure_rolls_back_schema_and_version(self) -> None:
        statements = [*database._MIGRATIONS[6], "SELECT * FROM fictional_missing_table"]
        with patch.dict(database._MIGRATIONS, {6: statements}):
            with self.assertRaises(sqlite3.OperationalError):
                self.db.initialize()
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall(), [])
        self.db.initialize()
        self.assertEqual(self.db.reminder_list_pending("test-user"), [])

    def test_process_exit_during_ddl_can_restart_without_data_loss(self) -> None:
        self.prepare_version_five()
        self.db.close()
        script = """
import os
import sys
sys.path.insert(0, sys.argv[2])
from persona_bot.core import database
db = database.Database(sys.argv[1])
db._conn.create_function("stop_migration", 0, lambda: os._exit(17))
statements = database._MIGRATIONS[6]
database._MIGRATIONS[6] = [statements[0], "SELECT stop_migration()", *statements[1:]]
db.initialize()
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", script, str(self.path),
             str(Path(__file__).resolve().parents[1] / "src")],
            capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        self.assertEqual(result.returncode, 17, result.stderr)
        self.db = Database(self.path)
        self.assert_version_five_is_intact()
        self.assert_restart_completes_migration()


class GuildSettingsPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "bot.db"
        self.db = Database(self.path)
        self.addCleanup(lambda: self.db.close())
        self.db.initialize()

    async def test_all_persisted_settings_round_trip_for_guilds_and_dm(self) -> None:
        state = BotState()
        state.restore_from_database(self.db)
        expected = {}
        for index, guild_id in enumerate((1, 2, None)):
            settings = state.get_guild(guild_id)
            values = {
                "current_prompt_file": f"../prompts/fictional-{index}.txt",
                "current_model_name": f"fictional-model-{index}",
                "current_respond_mode": "all" if index == 0 else "mention",
                "grounding_enabled": index != 1,
                "auto_topic_enabled": index == 1,
                "auto_topic_interval_hours": index + 1,
                "autonomous_enabled": index != 2,
            }
            for name, value in values.items():
                setattr(settings, name, value)
            settings.channel_reset_points[10] = 20
            await settings.persist()
            await settings.record_anniversary_post("test-user", "架空の記念日", "2026-01-01")
            expected[guild_id] = values

        self.db.close()
        self.db = Database(self.path)
        self.db.initialize()
        restored = BotState()
        restored.restore_from_database(self.db)
        for guild_id, values in expected.items():
            settings = restored.get_guild(guild_id)
            for name, value in values.items():
                self.assertEqual(getattr(settings, name), value, (guild_id, name))
            self.assertEqual(settings.anniversary_posted_dates, {"test-user:架空の記念日": "2026-01-01"})
            self.assertEqual(settings.channel_reset_points, {})

        # 復元済み・新規サーバーのどちらも、その後の変更を同じDBへ保存できる。
        restored.get_guild(1).grounding_enabled = False
        await restored.get_guild(1).persist()
        restored.get_guild(3).autonomous_enabled = True
        await restored.get_guild(3).persist()
        again = BotState()
        again.restore_from_database(self.db)
        self.assertFalse(again.get_guild(1).grounding_enabled)
        self.assertEqual(again.get_guild(1).current_model_name, expected[1]["current_model_name"])
        self.assertTrue(again.get_guild(3).autonomous_enabled)
        for guild_id in (2, None):
            for name, value in expected[guild_id].items():
                self.assertEqual(getattr(again.get_guild(guild_id), name), value)


if __name__ == "__main__":
    unittest.main()
