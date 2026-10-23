"""旧設定の移行ログにキーの値や断片が残らないことを検証する。"""

from contextlib import redirect_stdout
import importlib
import io
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy"))

import migrate_config_to_env as migration


class MigrationLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="migration-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        index = self.root / "index"
        index.mkdir()
        self.private_source = index / "config_private.py"
        self.private_source.touch()
        self.fallback_source = index / "config.py"
        self.env_path = self.root / ".env"
        paths = patch.multiple(
            migration, PROJECT_ROOT=self.root, INDEX_DIR=index, ENV_PATH=self.env_path,
            SOURCE_FILES=(self.private_source, self.fallback_source),
        )
        paths.start()
        self.addCleanup(paths.stop)
        original_read = Path.read_text

        def read_source(path, *args, **kwargs):
            # 旧設定の架空キーはメモリ上だけに置く。実際のAST解析はそのまま通す。
            if path == self.private_source:
                return self.config_source
            return original_read(path, *args, **kwargs)

        source_read = patch.object(Path, "read_text", read_source)
        source_read.start()
        self.addCleanup(source_read.stop)
        self.values = {
            "API_KEY_GEMINI": "FAKE-gemini-private-value-GEND",
            "TOKEN_DISCORD": "MOCK-discord-private-value-DEND",
        }
        self.set_source(self.values)

    def set_source(self, values) -> None:
        lines = [f"{name} = {value!r}" for name, value in values.items()]
        # 移行元はimport・実行せず、ASTの代入だけを読むことも確認する。
        lines.append('raise AssertionError("旧設定を実行してはいけません")')
        self.config_source = "\n".join(lines) + "\n"

    def run_main(self, *args):
        output = io.StringIO()
        with patch.object(sys, "argv", ["migrate_config_to_env.py", *args]), redirect_stdout(output):
            result = migration.main()
        return result, output.getvalue()

    def assert_no_secret_fragments(self, output, values=None) -> None:
        for value in (values or self.values).values():
            self.assertNotIn(value, output)
            self.assertNotIn(value[:4], output)
            self.assertNotIn(value[-4:], output)

    def test_dry_run_shows_key_names_without_secret_fragments(self) -> None:
        result, output = self.run_main("--dry-run")
        self.assertEqual(result, 0)
        for name in self.values:
            self.assertIn(name, output)
        self.assert_no_secret_fragments(output)
        self.assertFalse(self.env_path.exists())

    def test_escaped_values_do_not_leak_in_dry_run(self) -> None:
        values = {
            "API_KEY_GEMINI": 'FAKE\\ESCAPED_SENTINEL\\"quoted"\nGEND',
            "TOKEN_DISCORD": 'MOCK\\DISCORD_SENTINEL\\"quoted"\nDEND',
        }
        self.set_source(values)
        result, output = self.run_main("--dry-run")
        self.assertEqual(result, 0)
        self.assert_no_secret_fragments(output, values)
        self.assertNotIn("ESCAPED_SENTINEL", output)
        self.assertNotIn("DISCORD_SENTINEL", output)
        self.assertFalse(self.env_path.exists())

    def test_dry_run_output_does_not_depend_on_secret_content_or_length(self) -> None:
        _, before = self.run_main("--dry-run")
        self.set_source({"API_KEY_GEMINI": "tiny", "TOKEN_DISCORD": "different-long-fictional-value"})
        result, after = self.run_main("--dry-run")
        self.assertEqual(result, 0)
        self.assertEqual(after, before)

    def test_dry_run_with_force_preserves_existing_env(self) -> None:
        original = b"EXISTING_TEST_CONFIGURATION=keep\n"
        self.env_path.write_bytes(original)
        result, output = self.run_main("--dry-run", "--force")
        self.assertEqual(result, 0)
        self.assertEqual(self.env_path.read_bytes(), original)
        self.assert_no_secret_fragments(output)

    def test_normal_migration_preserves_values_and_restricts_permissions(self) -> None:
        result, output = self.run_main()
        self.assertEqual(result, 0)
        self.assertEqual(dict(dotenv_values(self.env_path, interpolate=False)), self.values)
        self.assert_no_secret_fragments(output)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.env_path.stat().st_mode), 0o600)

    def test_next_step_command_imports_the_current_settings_loader(self) -> None:
        result, output = self.run_main()
        self.assertEqual(result, 0)
        line = next(line for line in output.splitlines() if "load_settings" in line)
        match = re.search(r"from ([\w.]+) import (\w+)", line)
        self.assertIsNotNone(match, line)
        # 案内どおり src/ から import できるか確かめる（load_settings は呼ばない＝実際の .env を読まない）
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
        self.addCleanup(sys.path.remove, sys.path[0])
        module = importlib.import_module(match[1])
        self.assertTrue(callable(getattr(module, match[2])))

    def test_force_updates_file_without_logging_secrets(self) -> None:
        self.env_path.write_text("OLD_TEST_CONFIGURATION=replace\n", encoding="utf-8")
        self.env_path.chmod(0o644)
        result, output = self.run_main("--force")
        self.assertEqual(result, 0)
        self.assertEqual(dict(dotenv_values(self.env_path, interpolate=False)), self.values)
        self.assert_no_secret_fragments(output)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.env_path.stat().st_mode), 0o600)

    def test_existing_env_is_not_overwritten_without_force(self) -> None:
        original = b"EXISTING_TEST_CONFIGURATION=keep\n"
        self.env_path.write_bytes(original)
        result, output = self.run_main()
        self.assertEqual(result, 1)
        self.assertEqual(self.env_path.read_bytes(), original)
        self.assert_no_secret_fragments(output)

    # ---- 保存に失敗したときの原本の保護（#209） ----------------------------------

    def leftovers(self) -> list[str]:
        # 一時ファイルが残っていないことを確かめる（.env と移行元のフォルダ以外は無いはず）。
        return sorted(path.name for path in self.root.iterdir() if path.name not in (".env", "index"))

    def assert_failed_without_touching_original(self, original: bytes, *, patches) -> None:
        self.env_path.write_bytes(original)
        with patches:
            result, output = self.run_main("--force")
        self.assertEqual(result, 1)
        self.assertEqual(self.env_path.read_bytes(), original)
        self.assertEqual(self.leftovers(), [])
        self.assert_no_secret_fragments(output)
        self.assertIn("変更していません", output)

    def test_permission_failure_aborts_without_writing_secrets_or_truncating(self) -> None:
        # fchmod が無い環境（Windows）でも失敗経路を試せるよう create=True で差し込む。
        self.assert_failed_without_touching_original(
            b"EXISTING_TEST_CONFIGURATION=keep\n",
            patches=patch.object(migration.os, "fchmod", create=True, side_effect=PermissionError(1, "架空の権限失敗")),
        )

    def test_write_failure_keeps_original_and_removes_temporary_file(self) -> None:
        self.assert_failed_without_touching_original(
            b"EXISTING_TEST_CONFIGURATION=keep\n",
            patches=patch.object(migration.os, "fsync", side_effect=OSError(28, "架空のディスク満杯")),
        )

    def test_replace_failure_keeps_original_and_removes_temporary_file(self) -> None:
        self.assert_failed_without_touching_original(
            b"EXISTING_TEST_CONFIGURATION=keep\n",
            patches=patch.object(migration.os, "replace", side_effect=PermissionError(13, "架空の置換失敗")),
        )

    def test_new_file_failure_leaves_no_env_or_temporary_file(self) -> None:
        with patch.object(migration.os, "fsync", side_effect=OSError(28, "架空のディスク満杯")):
            result, output = self.run_main()
        self.assertEqual(result, 1)
        self.assertFalse(self.env_path.exists())
        self.assertEqual(self.leftovers(), [])
        self.assert_no_secret_fragments(output)

    def test_temporary_file_is_created_private_and_secret_is_written_only_after_permission_step(self) -> None:
        events = []
        original_mkstemp = migration.tempfile.mkstemp

        def mkstemp(*args, **kwargs):
            fd, name = original_mkstemp(*args, **kwargs)
            events.append(("mkstemp", Path(name).parent == self.root))
            return fd, name

        with patch.object(migration.tempfile, "mkstemp", side_effect=mkstemp), patch.object(
            migration.os, "fchmod", create=True, side_effect=lambda fd, mode: events.append(("fchmod", mode)),
        ):
            result, _ = self.run_main()
        self.assertEqual(result, 0)
        # 置き換え先と同じフォルダに作り、書き込みの前に 600 を設定する。
        self.assertEqual(events, [("mkstemp", True), ("fchmod", 0o600)])
        self.assertEqual(dict(dotenv_values(self.env_path, interpolate=False)), self.values)
        self.assertEqual(self.leftovers(), [])
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.env_path.stat().st_mode), 0o600)

    def test_missing_key_does_not_log_the_available_secret(self) -> None:
        self.set_source({"API_KEY_GEMINI": self.values["API_KEY_GEMINI"]})
        result, output = self.run_main("--dry-run")
        self.assertEqual(result, 1)
        self.assertIn("TOKEN_DISCORD", output)
        self.assert_no_secret_fragments(output)
        self.assertFalse(self.env_path.exists())


if __name__ == "__main__":
    unittest.main()
