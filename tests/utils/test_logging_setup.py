"""運用ログの情報境界・形式・Windowsの出力文字コードを確認する。"""

import io
import logging
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(SRC))

from persona_bot.config.constants import LOG_DATE_FORMAT, LOG_FORMAT
from persona_bot.utils.logging_setup import OperationalFormatter, configure_logging


class LoggingSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = logging.getLogger()
        self.handlers = list(self.root.handlers)
        self.level = self.root.level
        self.library_levels = {name: logging.getLogger(name).level for name in (
            "discord", "google_genai", "google.genai", "httpx", "httpcore",
        )}
        self.addCleanup(self.restore_logging)

    def restore_logging(self) -> None:
        for handler in self.root.handlers:
            if handler not in self.handlers:
                handler.close()
        self.root.handlers = self.handlers
        self.root.setLevel(self.level)
        for name, level in self.library_levels.items():
            logging.getLogger(name).setLevel(level)

    def test_stdout_has_time_level_module_and_one_record_after_reconfiguration(self) -> None:
        output = io.StringIO()
        with patch("sys.stdout", output):
            configure_logging()
            configure_logging()
            logging.getLogger("persona_bot.core.example").info("確認メッセージ 😺")
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4}\] \[INFO\] \[persona_bot\.core\.example\] 確認メッセージ 😺$")
        self.assertEqual(len(self.root.handlers), 1)

    def test_identifier_filter_preserves_counts_without_mutating_record(self) -> None:
        identifier = "".join(("81", "0000", "0000", "0000", "000", "1"))
        record = logging.LogRecord("persona_bot.core.example", logging.INFO, __file__, 1,
                                   "記録: user_id=%s guild=22 channel_id=33 回数=3", (identifier,), None)
        formatted = OperationalFormatter(LOG_FORMAT, LOG_DATE_FORMAT).format(record)
        self.assertNotIn(identifier, formatted)
        self.assertIn("user_id=<ID> guild=<ID> channel_id=<ID> 回数=3", formatted)
        self.assertEqual(record.args, (identifier,))
        self.assertEqual(record.msg, "記録: user_id=%s guild=22 channel_id=33 回数=3")

    def test_exception_body_source_and_absolute_path_are_not_emitted(self) -> None:
        private = "架空の非公開入力と接続情報"
        try:
            raise ValueError(private)
        except ValueError:
            record = logging.LogRecord("persona_bot.core.example", logging.ERROR, __file__, 1,
                                       "処理に失敗しました。", (), sys.exc_info())
        # 別ハンドラが先に作った例外文を再利用しない。
        record.exc_text = private
        record.stack_info = str(Path(__file__).resolve())
        formatted = OperationalFormatter(LOG_FORMAT, LOG_DATE_FORMAT).format(record)
        self.assertIn("ValueError", formatted)
        self.assertIn("test_logging_setup.py:", formatted)
        self.assertNotIn(private, formatted)
        self.assertNotIn(str(Path(__file__).resolve()), formatted)
        self.assertNotIn("raise ValueError", formatted)
        self.assertEqual(record.exc_text, private)

    def test_missing_exception_metadata_does_not_break_logging(self) -> None:
        record = logging.LogRecord("persona_bot.core.example", logging.ERROR, __file__, 1,
                                   "失敗しました。", (), (None, None, None))
        self.assertIn("例外情報はありません", OperationalFormatter(LOG_FORMAT, LOG_DATE_FORMAT).format(record))

    def test_non_reconfigurable_cp932_stream_replaces_only_unencodable_characters(self) -> None:
        class LegacyStream:
            encoding = "cp932"

            def __init__(self):
                self.buffer = io.BytesIO()

            def write(self, text):
                return self.buffer.write(text.encode(self.encoding))

            def flush(self):
                pass

        output = LegacyStream()
        with patch("sys.stdout", output):
            configure_logging()
            logging.getLogger("persona_bot.core.example").warning("確認 😺 続行")
        text = output.buffer.getvalue().decode("cp932")
        self.assertIn("確認 ? 続行", text)
        self.assertIn("[WARNING]", text)

    def test_cp932_console_is_reconfigured_without_pythonioencoding(self) -> None:
        script = (
            "import logging, sys; "
            "sys.stdout.reconfigure(encoding='cp932', errors='strict'); "
            "from persona_bot.utils.logging_setup import configure_logging; "
            "configure_logging(); "
            "logging.getLogger('persona_bot.core.example').info('絵文字 😺')"
        )
        environment = os.environ.copy()
        environment.pop("PYTHONIOENCODING", None)
        environment["PYTHONPATH"] = str(SRC)
        result = subprocess.run([sys.executable, "-B", "-c", script], env=environment,
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        self.assertIn("絵文字 😺", result.stdout.decode("utf-8"))
        self.assertNotIn(b"UnicodeEncodeError", result.stderr)


if __name__ == "__main__":
    unittest.main()
