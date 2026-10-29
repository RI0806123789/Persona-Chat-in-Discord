"""JSON保存の中断と破損時の退避を、架空の一時ファイルで検証する。"""

from copy import deepcopy
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.utils.storage import load_json_file, write_json_file
from persona_bot.utils.logging_setup import LOG_DATE_FORMAT, LOG_FORMAT, OperationalFormatter


LOG_FORMATTER = OperationalFormatter(LOG_FORMAT, LOG_DATE_FORMAT)


class JsonStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "projects.json"
        self.original = {"users": {"first-user": {"summary": "架空の既存記憶"},
                                   "other-user": {"summary": "別ユーザーの記憶"}}}
        self.original_bytes = json.dumps(self.original, ensure_ascii=False).encode("utf-8")
        self.updated = deepcopy(self.original)
        self.updated["users"]["first-user"]["summary"] = "架空の更新した記憶"

    def seed(self) -> None:
        self.path.write_bytes(self.original_bytes)

    def test_missing_file_returns_independent_defaults(self) -> None:
        default = {"users": {}}
        loaded = load_json_file(self.path, default)
        loaded["users"]["new-user"] = {}
        self.assertEqual(default, {"users": {}})
        self.assertFalse(self.path.exists())

    def test_valid_file_keeps_all_users(self) -> None:
        self.seed()
        self.assertEqual(load_json_file(self.path, {}), self.original)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_bom_prefixed_file_is_read_like_plain_utf8(self) -> None:
        # メモ帳の「UTF-8 (BOM 付き)」保存を想定。BOM だけを理由に退避しない（#153）。
        self.path.write_bytes(b"\xef\xbb\xbf" + self.original_bytes)
        with self.assertNoLogs("persona_bot.utils.storage", level="WARNING"):
            loaded = load_json_file(self.path, {"users": {}})
        self.assertEqual(loaded, self.original)
        self.assertEqual(self.path.read_bytes(), b"\xef\xbb\xbf" + self.original_bytes)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_atomic_replacement_occurs_after_fsync(self) -> None:
        self.seed()
        original_replace = os.replace
        original_fsync = os.fsync
        synchronized = False
        replaced = False

        def fsync(descriptor: int) -> None:
            nonlocal synchronized
            self.assertEqual(self.path.read_bytes(), self.original_bytes)
            original_fsync(descriptor)
            synchronized = True

        def replace(source, destination) -> None:
            nonlocal replaced
            self.assertTrue(synchronized)
            self.assertEqual(Path(source).parent, self.path.parent)
            self.assertNotEqual(Path(source), self.path)
            self.assertEqual(Path(destination), self.path)
            self.assertEqual(self.path.read_bytes(), self.original_bytes)
            self.assertEqual(json.loads(Path(source).read_text(encoding="utf-8")), self.updated)
            original_replace(source, destination)
            replaced = True

        with patch("os.fsync", side_effect=fsync), patch("os.replace", side_effect=replace):
            write_json_file(self.path, self.updated)

        self.assertTrue(synchronized)
        self.assertTrue(replaced)
        self.assertEqual(load_json_file(self.path, {}), self.updated)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_fsync_failure_preserves_existing_file(self) -> None:
        self.seed()
        with patch("os.fsync", side_effect=OSError("架空のディスク障害")):
            with self.assertRaises(OSError):
                write_json_file(self.path, self.updated)
        self.assertEqual(self.path.read_bytes(), self.original_bytes)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_replace_failure_preserves_existing_file(self) -> None:
        self.seed()
        with patch("os.replace", side_effect=PermissionError("架空の置換失敗")):
            with self.assertRaises(PermissionError):
                write_json_file(self.path, self.updated)
        self.assertEqual(self.path.read_bytes(), self.original_bytes)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_interruption_before_replace_preserves_existing_file(self) -> None:
        self.seed()
        with patch("os.replace", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                write_json_file(self.path, self.updated)
        self.assertEqual(self.path.read_bytes(), self.original_bytes)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_serialization_failure_preserves_existing_file(self) -> None:
        self.seed()
        with self.assertRaises(TypeError):
            write_json_file(self.path, {"unsupported": object()})
        self.assertEqual(self.path.read_bytes(), self.original_bytes)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_replacement_preserves_existing_permission_mode(self) -> None:
        self.seed()
        self.path.chmod(0o640)
        original_mode = stat.S_IMODE(self.path.stat().st_mode)
        write_json_file(self.path, self.updated)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), original_mode)

    def test_partial_temporary_write_failure_preserves_existing_file(self) -> None:
        self.seed()
        original_factory = tempfile.NamedTemporaryFile

        def temporary_file(**kwargs):
            handle = original_factory(**kwargs)
            write = handle.write

            def partial_write(text: str) -> None:
                write(text[:len(text) // 2])
                raise OSError("架空の書き込み途中の障害")

            handle.write = partial_write
            return handle

        with patch("tempfile.NamedTemporaryFile", side_effect=temporary_file):
            with self.assertRaises(OSError):
                write_json_file(self.path, self.updated)
        self.assertEqual(self.path.read_bytes(), self.original_bytes)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_corrupt_file_is_backed_up_before_default_can_be_saved(self) -> None:
        # BOM を吸収しても、BOM の後ろが壊れている・空・object 以外なら従来どおり退避する。
        bom = b"\xef\xbb\xbf"
        broken_documents = [b'{"users": ', b"", b"  \n", b"[]", b"null", b"\xff",
                            bom, bom + b'{"users": ', bom + b"[]", bom + b"\xff"]
        for broken in broken_documents:
            with self.subTest(content=broken):
                self.path.write_bytes(broken)
                before = set(self.root.glob("projects.json.corrupt-*.bak"))
                with self.assertLogs("persona_bot.utils.storage", level="WARNING") as logs:
                    loaded = load_json_file(self.path, {"users": {}})
                self.assertEqual(loaded, {"users": {}})
                backups = set(self.root.glob("projects.json.corrupt-*.bak")) - before
                self.assertEqual(len(backups), 1)
                backup = backups.pop()
                self.assertEqual(backup.read_bytes(), broken)
                self.assertFalse(self.path.exists())
                log_text = "\n".join(LOG_FORMATTER.format(record) for record in logs.records)
                self.assertIn("<PATH>", log_text)
                self.assertNotIn(backup.name, log_text)
                self.assertNotIn(str(self.root), log_text)
                write_json_file(self.path, loaded)
                self.assertEqual(backup.read_bytes(), broken)

    def test_failed_backup_stops_loading_and_preserves_corrupt_file(self) -> None:
        broken = b'{"users": '
        self.path.write_bytes(broken)
        with patch("os.replace", side_effect=PermissionError("架空の退避失敗")):
            with self.assertLogs("persona_bot.utils.storage", level="ERROR"):
                with self.assertRaises(PermissionError):
                    load_json_file(self.path, {"users": {}})
        self.assertEqual(self.path.read_bytes(), broken)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_read_permission_failure_is_not_treated_as_empty_data(self) -> None:
        self.seed()
        with patch.object(Path, "read_text", side_effect=PermissionError("架空の読み込み失敗")):
            with self.assertRaises(PermissionError):
                load_json_file(self.path, {"users": {}})
        self.assertEqual(self.path.read_bytes(), self.original_bytes)
        self.assertEqual(list(self.root.iterdir()), [self.path])


if __name__ == "__main__":
    unittest.main()
