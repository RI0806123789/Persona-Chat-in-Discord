"""手書きプロフィールを取り込めない場合と、DB・原子的リセットの失敗を検証する。"""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.core.database import Database
from persona_bot.utils import storage


class ProfileAbsorptionTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.profile_path = self.directory / "profile.json"
        self.db = Database(self.directory / "bot.db", legacy_dir=self.directory, legacy_owner_id="")
        self.addCleanup(self.db.close)

    def write_profile(self, data):
        text = json.dumps(data, ensure_ascii=False, indent=4) + "\n"
        self.profile_path.write_text(text, encoding="utf-8")
        return self.profile_path.read_bytes()

    def test_shared_profile_without_owner_is_preserved_with_warning(self) -> None:
        original = self.write_profile({"趣味": "架空の工作"})
        with patch("builtins.print") as log:
            self.db.initialize()
        self.assertEqual(self.profile_path.read_bytes(), original)
        self.assertEqual(self.db.profile_load(""), {})
        self.assertTrue(any("profile.json" in str(call) and "保持" in str(call) for call in log.call_args_list))

    def test_empty_user_profiles_are_preserved_when_nothing_is_absorbed(self) -> None:
        original = self.write_profile({"test-user": {}})
        with patch("builtins.print") as log:
            self.db.initialize()
        self.assertEqual(self.profile_path.read_bytes(), original)
        self.assertTrue(any("保持" in str(call) for call in log.call_args_list))

    def test_per_user_profiles_merge_and_reset_after_success(self) -> None:
        self.db.initialize()
        self.db.profile_merge("test-user", {"趣味": "以前の架空の趣味", "好物": "架空のお菓子"})
        self.write_profile({"test-user": {"趣味": "架空の工作"}, "other-user": {"趣味": "架空の読書"}})
        self.db.initialize()
        self.assertEqual(json.loads(self.profile_path.read_text(encoding="utf-8")), {})
        self.assertEqual(self.db.profile_load("test-user"), {"趣味": "架空の工作", "好物": "架空のお菓子"})
        self.assertEqual(self.db.profile_load("other-user"), {"趣味": "架空の読書"})
        self.db.initialize()
        self.assertEqual(self.db.profile_load("other-user"), {"趣味": "架空の読書"})

    def test_bom_profile_is_absorbed_and_reset(self) -> None:
        self.profile_path.write_text('{"test-user": {"趣味": "架空の工作"}}', encoding="utf-8-sig")
        self.db.initialize()
        self.assertEqual(self.db.profile_load("test-user"), {"趣味": "架空の工作"})
        self.assertEqual(json.loads(self.profile_path.read_text(encoding="utf-8")), {})

    def test_shared_profile_with_owner_is_still_imported(self) -> None:
        self.db._legacy_owner_id = "legacy-user"
        self.write_profile({"趣味": "架空の工作"})
        self.db.initialize()
        self.assertEqual(self.db.profile_load("legacy-user"), {"趣味": "架空の工作"})
        self.assertEqual(json.loads(self.profile_path.read_text(encoding="utf-8")), {})

    def test_database_failure_preserves_source_for_retry(self) -> None:
        original = self.write_profile({"test-user": {"趣味": "架空の工作"}})
        with patch.object(self.db, "profile_merge", side_effect=RuntimeError("架空のDB書込エラー")):
            self.db.initialize()
        self.assertEqual(self.profile_path.read_bytes(), original)
        self.db.initialize()
        self.assertEqual(self.db.profile_load("test-user"), {"趣味": "架空の工作"})

    def test_reset_replace_failure_preserves_source_and_retries(self) -> None:
        original = self.write_profile({"test-user": {"趣味": "架空の工作"}})
        with patch.object(storage.os, "replace", side_effect=OSError("架空の置換エラー")) as replace:
            self.db.initialize()
        replace.assert_called_once()
        self.assertEqual(self.profile_path.read_bytes(), original)
        self.assertEqual(self.db.profile_load("test-user"), {"趣味": "架空の工作"})
        self.assertEqual(list(self.directory.glob(".profile.json.*.tmp")), [])
        self.db.initialize()
        self.assertEqual(json.loads(self.profile_path.read_text(encoding="utf-8")), {})

    def test_invalid_json_or_non_object_is_kept(self) -> None:
        for text in ('{"趣味":', '["架空のデータ"]'):
            with self.subTest(text=text):
                self.profile_path.write_text(text, encoding="utf-8")
                self.db.initialize()
                self.assertEqual(self.profile_path.read_text(encoding="utf-8"), text)


if __name__ == "__main__":
    unittest.main()
