"""新規DBの初期化と、再起動を挟んだギルド・DM設定の保存を検証する。"""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

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
             str(Path(__file__).resolve().parents[2] / "src")],
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


class LegacyDataEdgeCaseTests(unittest.TestCase):
    """引き継ぎ先の無い旧データ・壊れた旧 JSON・終了時の close を確かめる（#68）。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        quiet = patch("builtins.print")
        quiet.start()
        self.addCleanup(quiet.stop)

    def open(self, owner: str = "", root: Path | None = None) -> Database:
        db = Database((root or self.root) / "bot.db", legacy_owner_id=owner)
        self.addCleanup(db.close)
        return db

    def pending_users(self, db: Database) -> list[str]:
        return [row[0] for row in db._conn.execute("SELECT user_id FROM pending_log ORDER BY id")]

    def add_pending(self, db: Database, user_id: str) -> None:
        with db._lock, db._conn:
            db._conn.execute(
                "INSERT INTO pending_log (user_id, created_at, question, response) VALUES (?, ?, ?, ?)",
                (user_id, "2026-01-01", "架空の質問", "架空の応答"),
            )

    def test_unassigned_pending_rows_are_dropped_without_owner(self) -> None:
        db = self.open()
        db.initialize()
        self.add_pending(db, "")  # 旧・全体共有の未割当行
        self.add_pending(db, "test-user")
        self.assertEqual(db.pending_distinct_user_ids(), ["test-user"])
        db.initialize()  # 再起動相当。後始末が走る
        self.assertEqual(self.pending_users(db), ["test-user"])

    def test_unassigned_pending_rows_go_to_owner(self) -> None:
        db = self.open(owner="owner-user")
        db.initialize()
        self.add_pending(db, "")
        db.initialize()
        self.assertEqual(self.pending_users(db), ["owner-user"])

    def test_pending_log_txt_is_kept_as_backup_without_owner(self) -> None:
        text = "[2026-01-01] User: 架空の質問\n[2026-01-01] Bot: 架空の応答\n"
        for owner, expected in (("", []), ("owner-user", ["owner-user"])):
            with self.subTest(owner=owner):
                # ケースごとに別のフォルダを使い、前のケースの失敗が次へ波及しないようにする
                root = self.root / (owner or "no-owner")
                root.mkdir()
                (root / "pending_log.txt").write_text(text, encoding="utf-8")
                db = self.open(owner, root)
                db.initialize()
                self.assertEqual(self.pending_users(db), expected)
                self.assertEqual((root / "pending_log.txt.imported.bak").read_text(encoding="utf-8"), text)

    def test_broken_legacy_json_is_left_in_place_for_retry(self) -> None:
        files = {
            "affinity.json": "{架空の壊れたJSON",
            "user_stats.json": "[1, 2]",
            "queue.json": '{"tasks": "架空の非配列"}',
        }
        for name, text in files.items():
            (self.root / name).write_text(text, encoding="utf-8")
        self.open().initialize()
        for name, text in files.items():
            with self.subTest(file=name):
                self.assertEqual((self.root / name).read_text(encoding="utf-8"), text)
                self.assertFalse((self.root / f"{name}.imported.bak").exists())

    def test_valid_legacy_json_is_still_imported_and_renamed(self) -> None:
        entry = {"affinity": 70, "valence": 60, "arousal": 40, "last_interaction": 1.0}
        (self.root / "affinity.json").write_text(json.dumps({"test-user": entry}), encoding="utf-8")
        db = self.open()
        db.initialize()
        self.assertEqual(db.affinity_load_all()["test-user"]["affinity"], 70)
        self.assertFalse((self.root / "affinity.json").exists())
        self.assertTrue((self.root / "affinity.json.imported.bak").exists())

    def test_close_waits_for_a_running_operation_and_is_repeatable(self) -> None:
        db = self.open()
        db.initialize()
        closed = threading.Event()

        def close() -> None:
            db.close()
            closed.set()

        db._lock.acquire()  # 別スレッドの読み書きが実行中の状態
        try:
            worker = threading.Thread(target=close)
            worker.start()
            self.assertFalse(closed.wait(0.2))
            self.assertEqual(db._conn.execute("SELECT 1").fetchone()[0], 1)
        finally:
            db._lock.release()
        worker.join(5)
        self.assertTrue(closed.is_set())
        with self.assertRaises(sqlite3.ProgrammingError):
            db._conn.execute("SELECT 1")
        db.close()


class ReminderUserIndexTests(unittest.TestCase):
    """リマインダーのユーザー別の操作が専用の索引を使うことを確認する（#58）。"""

    INDEX = "idx_reminders_user_status"

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "bot.db"
        self.db = Database(self.path)
        self.addCleanup(lambda: self.db.close())

    def add(self, user_id: str, remind_at: str) -> int | None:
        return self.db.reminder_add(
            user_id=user_id, user_name="架空の利用者", guild_id=1, channel_id=10,
            message="架空の予定", remind_at=remind_at, created_at="2026-01-01T00:00:00+00:00",
        )

    def reminder_indexes(self) -> set[str]:
        with closing(sqlite3.connect(self.path)) as connection:
            return {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='reminders'"
            )}

    def query_plans(self, operation) -> list[str]:
        """操作が実際に発行した SQL（値は展開済み）を集め、ユーザー別の検索の実行計画を返す。"""
        statements: list[str] = []
        self.db._conn.set_trace_callback(statements.append)
        try:
            operation()
        finally:
            self.db._conn.set_trace_callback(None)
        targets = [statement for statement in statements
                   if "FROM reminders" in statement and "user_id = " in statement]
        self.assertTrue(targets, statements)
        with closing(sqlite3.connect(self.path)) as connection:
            return [" / ".join(row[3] for row in connection.execute(f"EXPLAIN QUERY PLAN {statement}"))
                    for statement in targets]

    def test_user_operations_use_user_index_without_temporary_sort(self) -> None:
        self.db.initialize()
        for user in range(30):
            for hour in range(3):
                self.add(f"user-{user}", f"2026-01-02T{hour:02d}:00:00+00:00")
        target = "user-7"
        operations = {
            "登録時の上限確認": lambda: self.add(target, "2026-01-02T01:00:00+00:00"),
            "一覧": lambda: self.db.reminder_list_pending(target),
            "件数": lambda: self.db.reminder_count_pending(target),
        }
        for name, operation in operations.items():
            with self.subTest(operation=name):
                for plan in self.query_plans(operation):
                    self.assertIn(self.INDEX, plan)
                    self.assertNotIn("TEMP B-TREE", plan)
        # 同じ時刻の予定は登録順（reminder_id）で並ぶ
        listed = [(row["remind_at"][11:13], row["reminder_id"]) for row in self.db.reminder_list_pending(target)]
        self.assertEqual(listed, sorted(listed))
        self.assertEqual([hour for hour, _ in listed], ["00", "01", "01", "02"])
        self.assertEqual(self.db.reminder_count_pending(target), 4)

    def test_version_nine_database_gains_index_and_keeps_reminders(self) -> None:
        with patch.object(database, "SCHEMA_VERSION", 9):
            self.db.initialize()
        reminder_id = self.add("test-user", "2026-01-02T00:00:00+00:00")
        self.assertNotIn(self.INDEX, self.reminder_indexes())
        self.db.close()
        self.db = Database(self.path)
        self.db.initialize()
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        self.assertEqual(self.reminder_indexes(), {"idx_reminders_due", self.INDEX})
        self.assertEqual([row["reminder_id"] for row in self.db.reminder_list_pending("test-user")], [reminder_id])


if __name__ == "__main__":
    unittest.main()
