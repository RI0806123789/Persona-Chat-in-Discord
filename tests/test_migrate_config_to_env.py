"""旧設定の移行ログにキーの値や断片が残らないことを検証する。"""

from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy"))

import migrate_config_to_env as migration


class MigrationLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="migration-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        index = self.root / "index"
        index.mkdir()
        self.private_source = index / "config_private.py"
        self.fallback_source = index / "config.py"
        self.env_path = self.root / ".env"
        paths = patch.multiple(
            migration, PROJECT_ROOT=self.root, INDEX_DIR=index, ENV_PATH=self.env_path,
            SOURCE_FILES=(self.private_source, self.fallback_source),
        )
        paths.start()
        self.addCleanup(paths.stop)
        self.values = {
            "API_KEY_GEMINI": "FAKE-gemini-private-value-GEND",
            "TOKEN_DISCORD": "MOCK-discord-private-value-DEND",
        }
        self.write_source(self.values)

    def write_source(self, values) -> None:
        lines = [f"{name} = {value!r}" for name, value in values.items()]
        # 移行元はimport・実行せず、ASTの代入だけを読むことも確認する。
        lines.append('raise AssertionError("旧設定を実行してはいけません")')
        self.private_source.write_text("\n".join(lines) + "\n", encoding="utf-8")

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
        self.write_source(values)
        result, output = self.run_main("--dry-run")
        self.assertEqual(result, 0)
        self.assert_no_secret_fragments(output, values)
        self.assertNotIn("ESCAPED_SENTINEL", output)
        self.assertNotIn("DISCORD_SENTINEL", output)
        self.assertFalse(self.env_path.exists())

    def test_dry_run_output_does_not_depend_on_secret_content_or_length(self) -> None:
        _, before = self.run_main("--dry-run")
        self.write_source({"API_KEY_GEMINI": "tiny", "TOKEN_DISCORD": "different-long-fictional-value"})
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

    def test_missing_key_does_not_log_the_available_secret(self) -> None:
        self.write_source({"API_KEY_GEMINI": self.values["API_KEY_GEMINI"]})
        result, output = self.run_main("--dry-run")
        self.assertEqual(result, 1)
        self.assertIn("TOKEN_DISCORD", output)
        self.assert_no_secret_fragments(output)
        self.assertFalse(self.env_path.exists())


if __name__ == "__main__":
    unittest.main()
