"""親密度の対象ユーザー保存・保存順序と、既存定数による調整を検証する。"""

import asyncio
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import affinity_service
from affinity_service import AffinityService
from background import spawn
import database
from database import Database


class AffinityPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "bot.db"
        self.db = Database(self.path)
        self.db.initialize()
        self.addCleanup(lambda: self.db.close())
        clock = patch.object(affinity_service.time, "time", return_value=1000.0)
        clock.start()
        self.addCleanup(clock.stop)
        self.seed(3)

    def seed(self, count: int) -> None:
        self.db.affinity_save_all({
            f"user-{i}": {
                "affinity": 50, "valence": 50, "arousal": 50, "last_interaction": 1000.0,
            } for i in range(count)
        })
        self.service = AffinityService(self.db)

    async def persist(self, user_id: str) -> None:
        await self.service.save_background(user_id)

    async def test_write_count_is_constant_for_one_hundred_and_thousand_users(self) -> None:
        for count in (1, 100, 1000):
            with self.subTest(user_count=count):
                self.seed(count)
                before = self.db.affinity_load_all()
                updated = self.service.update_emotion("user-0", 30, -10).copy()
                statements = []
                before_changes = self.db._conn.total_changes
                self.db._conn.set_trace_callback(statements.append)
                try:
                    await self.persist("user-0")
                finally:
                    self.db._conn.set_trace_callback(None)
                writes = [statement for statement in statements if statement.lstrip().upper().startswith(
                    ("INSERT", "UPDATE", "DELETE", "REPLACE")
                )]
                self.assertEqual(len(writes), 1)
                self.assertNotIn("DELETE", writes[0].upper())
                self.assertEqual(self.db._conn.total_changes - before_changes, 1)
                after = self.db.affinity_load_all()
                self.assertEqual(after["user-0"], updated)
                self.assertEqual({key: value for key, value in after.items() if key != "user-0"},
                                 {key: value for key, value in before.items() if key != "user-0"})

    async def test_update_never_deletes_or_reinserts_other_rows(self) -> None:
        with self.db._lock, self.db._conn:
            # 値が同じでも削除・再挿入されていれば、DB自身が保存を拒否する。
            self.db._conn.executescript("""
                CREATE TRIGGER prevent_affinity_delete BEFORE DELETE ON affinity
                BEGIN SELECT RAISE(ABORT, '架空の削除検知'); END;
                CREATE TRIGGER protect_other_affinity_insert BEFORE INSERT ON affinity
                WHEN NEW.user_id != 'user-0'
                BEGIN SELECT RAISE(ABORT, '架空の他ユーザー挿入検知'); END;
                CREATE TRIGGER protect_other_affinity_update BEFORE UPDATE ON affinity
                WHEN OLD.user_id != 'user-0'
                BEGIN SELECT RAISE(ABORT, '架空の他ユーザー更新検知'); END;
            """)
            before_rowids = dict(self.db._conn.execute("SELECT user_id, rowid FROM affinity"))
        updated = self.service.update_emotion("user-0", 10, -5)
        await self.persist("user-0")
        self.assertEqual(self.db.affinity_load_all()["user-0"], updated)
        with self.db._lock:
            self.assertEqual(dict(self.db._conn.execute("SELECT user_id, rowid FROM affinity")), before_rowids)

    async def test_saved_values_survive_database_reopening(self) -> None:
        expected = self.service.update_emotion("new-user", 25, -35).copy()
        await self.persist("new-user")
        self.db.close()
        self.db = Database(self.path)
        self.db.initialize()
        restored = AffinityService(self.db)
        self.assertEqual(restored.cache["new-user"], expected)
        self.assertEqual(set(restored.cache["new-user"]),
                         {"affinity", "valence", "arousal", "last_interaction"})

    async def test_database_defaults_match_between_upsert_full_save_and_legacy_import(self) -> None:
        legacy = self.path.parent / "fictional-affinity.json"
        legacy.write_text('{"legacy-user": {}}', encoding="utf-8")
        with patch.object(database, "AFFINITY_NEUTRAL", 40):
            self.db.affinity_save_all({"explicit-user": {}})
            self.db.affinity_upsert("new-user", {})
            self.db._import_affinity_json(legacy)
        for user_id, entry in self.db.affinity_load_all().items():
            with self.subTest(user_id=user_id):
                self.assertEqual(entry, {
                    "affinity": 40, "valence": 40, "arousal": 40, "last_interaction": 0.0,
                })

    async def test_missing_user_does_not_rewrite_existing_data(self) -> None:
        before = self.db.affinity_load_all()
        before_changes = self.db._conn.total_changes
        await self.persist("missing-user")
        self.assertEqual(self.db._conn.total_changes, before_changes)
        self.assertEqual(self.db.affinity_load_all(), before)

    async def test_independent_users_keep_both_updates(self) -> None:
        first = self.service.update_emotion("user-0", 15, -5).copy()
        second = self.service.update_emotion("user-1", -20, 30).copy()
        untouched = self.service.cache["user-2"].copy()
        await asyncio.gather(self.persist("user-0"), self.persist("user-1"))
        self.assertEqual(self.db.affinity_load_all(), {
            "user-0": first, "user-1": second, "user-2": untouched,
        })

    async def check_delayed_save(self, *, cancel_first: bool) -> None:
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        original = self.db.affinity_upsert
        snapshots = []

        def write(user_id, entry):
            snapshots.append(deepcopy(entry))
            if len(snapshots) == 1:
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(3):
                    raise AssertionError("架空の保存処理を再開できませんでした")
            original(user_id, entry)

        self.service.update_emotion("user-0", 5, 0)
        with patch.object(self.db, "affinity_upsert", side_effect=write):
            first = spawn(self.persist("user-0"), label="親密度の先行保存")
            second = None
            try:
                await asyncio.wait_for(entered.wait(), 2)
                if cancel_first:
                    for _ in range(2):
                        first.cancel()
                        await asyncio.sleep(0)
                expected = self.service.update_emotion("user-0", 7, 0).copy()
                second = spawn(self.persist("user-0"), label="親密度の後続保存")
                for _ in range(5):
                    await asyncio.sleep(0)
                self.assertEqual(len(snapshots), 1)
                self.assertTrue(self.service._save_lock.locked())
            finally:
                release.set()
                await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
        self.assertEqual(first.cancelled(), cancel_first)
        self.assertEqual([entry["valence"] for entry in snapshots], [55, 62])
        self.assertEqual(self.db.affinity_load_all()["user-0"], expected)

    async def test_later_update_is_saved_after_earlier_snapshot(self) -> None:
        await self.check_delayed_save(cancel_first=False)

    async def test_canceled_save_keeps_lock_until_worker_finishes(self) -> None:
        await self.check_delayed_save(cancel_first=True)

    async def test_save_failure_keeps_old_database_and_cached_update_for_retry(self) -> None:
        before = self.db.affinity_load_all()
        expected = self.service.update_emotion("user-0", 8, -6).copy()
        with patch.object(self.db, "affinity_upsert", side_effect=OSError("架空の保存失敗")):
            await self.persist("user-0")
        self.assertEqual(self.db.affinity_load_all(), before)
        self.assertEqual(self.service.cache["user-0"], expected)
        await self.persist("user-0")
        self.assertEqual(self.db.affinity_load_all()["user-0"], expected)


class AffinityTuningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = AffinityService.__new__(AffinityService)
        self.service.cache = {}
        clock = patch.object(affinity_service.time, "time", return_value=1000.0)
        clock.start()
        self.addCleanup(clock.stop)
        values = patch.multiple(
            affinity_service, AFFINITY_HALF_LIFE_SECONDS=60.0, AFFINITY_NEUTRAL=40,
            AFFINITY_MIN=20, AFFINITY_MAX=80,
            AFFINITY_RAISE_THRESHOLD=45, AFFINITY_LOWER_THRESHOLD=35, create=True,
        )
        values.start()
        self.addCleanup(values.stop)

    def test_new_user_uses_configured_neutral_value(self) -> None:
        data = self.service.get_user_data("new-user")
        self.assertEqual((data["affinity"], data["valence"], data["arousal"]), (40, 40, 40))

    def test_half_life_converges_toward_configured_neutral_value(self) -> None:
        data = self.service._apply_decay({
            "affinity": 70, "valence": 80, "arousal": 20, "last_interaction": 940.0,
        })
        self.assertEqual((data["affinity"], data["valence"], data["arousal"]), (70, 60, 30))

    def test_configured_thresholds_keep_strict_boundaries(self) -> None:
        for change, expected_affinity in ((6, 41), (5, 40), (-5, 40), (-6, 39)):
            with self.subTest(change=change):
                self.service.cache.clear()
                data = self.service.update_emotion("new-user", change, 0)
                self.assertEqual(data["affinity"], expected_affinity)

    def test_configured_bounds_apply_to_emotion_and_affinity(self) -> None:
        self.service.cache["upper-user"] = {
            "affinity": 80, "valence": 40, "arousal": 40, "last_interaction": 1000.0,
        }
        self.service.cache["lower-user"] = {
            "affinity": 20, "valence": 40, "arousal": 40, "last_interaction": 1000.0,
        }
        upper = self.service.update_emotion("upper-user", 1000, -1000)
        lower = self.service.update_emotion("lower-user", -1000, 1000)
        self.assertEqual((upper["affinity"], upper["valence"], upper["arousal"]), (80, 80, 20))
        self.assertEqual((lower["affinity"], lower["valence"], lower["arousal"]), (20, 20, 80))


if __name__ == "__main__":
    unittest.main()
