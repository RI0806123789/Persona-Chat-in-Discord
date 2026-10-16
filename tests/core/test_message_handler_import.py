"""メッセージ処理のimportが設定読込・実DB・外部通信を起動しないことを検証する。"""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


class MessageHandlerImportTests(unittest.TestCase):
    def test_import_without_secrets_or_runtime_files(self) -> None:
        source = Path(__file__).resolve().parents[2] / "src"
        script = textwrap.dedent("""\
            import sys
            from unittest.mock import patch
            sys.path.insert(0, sys.argv[1])
            with patch('persona_bot.config.settings_loader.load_settings', side_effect=AssertionError('設定読込は禁止')), \\
                 patch('persona_bot.core.database.Database', side_effect=AssertionError('実DBの生成は禁止')), \\
                 patch('socket.socket.connect', side_effect=AssertionError('外部通信は禁止')):
                from persona_bot.core.message_handler import MessageHandler
                assert 'bot' not in sys.modules
            """)
        environment = os.environ.copy()
        for key in ("API_KEY_GEMINI", "TOKEN_DISCORD"):
            environment.pop(key, None)
        environment["PYTHONIOENCODING"] = "utf-8"
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-B", "-c", script, str(source)],
                cwd=directory, env=environment, capture_output=True, text=True,
                encoding="utf-8", timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
