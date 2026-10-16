"""ペルソナのプロンプトファイルを読み込むとき、UTF-8 BOM を除去することを検証する（#55）。"""

import codecs
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.config.paths import load_prompt_template


class PromptBomTests(unittest.TestCase):
    TEXT = "あなたは架空のペルソナです。\n口調は丁寧にしてください。\n"

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "prompt_test.txt"

    def test_bom_is_removed(self) -> None:
        # メモ帳の「UTF-8 (BOM 付き)」保存と同じバイト列を作る
        self.path.write_bytes(codecs.BOM_UTF8 + self.TEXT.encode("utf-8"))
        loaded = load_prompt_template(self.path)
        self.assertEqual(loaded, self.TEXT)
        self.assertFalse(loaded.startswith("﻿"))

    def test_file_without_bom_is_unchanged(self) -> None:
        self.path.write_bytes(self.TEXT.encode("utf-8"))
        self.assertEqual(load_prompt_template(self.path), self.TEXT)

    def test_string_path_is_accepted(self) -> None:
        self.path.write_bytes(codecs.BOM_UTF8 + self.TEXT.encode("utf-8"))
        self.assertEqual(load_prompt_template(str(self.path)), self.TEXT)


if __name__ == "__main__":
    unittest.main()
