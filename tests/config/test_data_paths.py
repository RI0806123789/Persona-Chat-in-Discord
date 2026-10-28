"""旧配置を見落として空の記憶で再開しないことを確認する。"""

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.config.data_paths import ensure_data_directory_ready


class DataPathsTests(unittest.TestCase):
    def test_fresh_or_migrated_project_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ensure_data_directory_ready(root)
            (root / "data/memory").mkdir(parents=True)
            (root / "data/memory/bot.db").write_bytes(b"fictional")
            ensure_data_directory_ready(root)

    def test_legacy_files_prevent_startup_without_creating_new_data(self) -> None:
        for relative in ("memory/bot.db", "memory/profile.json", "ng/blacklist.json", "ng/NG_WORD_private.txt"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                original = root / relative
                original.parent.mkdir()
                original.write_bytes(b"fictional")
                with self.assertRaisesRegex(RuntimeError, "migrate_data_directory.py"):
                    ensure_data_directory_ready(root)
                self.assertEqual(original.read_bytes(), b"fictional")
                self.assertFalse((root / "data").exists())

    def test_empty_legacy_directory_or_gitkeep_does_not_block_startup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "memory").mkdir()
            (root / "memory/.gitkeep").touch()
            (root / "ng").mkdir()
            ensure_data_directory_ready(root)


if __name__ == "__main__":
    unittest.main()
