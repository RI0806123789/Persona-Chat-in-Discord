"""NGワードファイルのBOM除去と、検出したNGワードの返却順を検証する（#55）。"""

import codecs
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.services import ng_word_service
from persona_bot.services.ng_word_service import NgWordFilter


class NgWordFileTestCase(unittest.TestCase):
    """実際の data/ng/ を読まないよう、両方のNGワードファイルを一時ディレクトリへ差し替える。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.public = root / "NG_WORD.txt"
        self.private = root / "NG_WORD_private.txt"
        paths = patch.multiple(ng_word_service, NG_WORD_PATH=self.public, NG_WORD_PRIVATE_PATH=self.private)
        paths.start()
        self.addCleanup(paths.stop)
        # 読込件数のログでテスト出力を埋めない
        quiet = patch("builtins.print")
        quiet.start()
        self.addCleanup(quiet.stop)

    @staticmethod
    def write(path: Path, text: str, *, bom: bool) -> None:
        # メモ帳の「UTF-8 (BOM 付き)」保存と同じバイト列を作る
        path.write_bytes((codecs.BOM_UTF8 if bom else b"") + text.encode("utf-8"))


class NgWordBomTests(NgWordFileTestCase):
    def test_comment_on_first_line_after_bom_is_skipped(self) -> None:
        self.write(self.public, "# 架空のコメント\n架空語A\n", bom=True)
        self.assertEqual(ng_word_service.load_ng_words(), ["架空語A"])

    def test_word_on_first_line_after_bom_is_detected_and_masked(self) -> None:
        self.write(self.public, "架空語A\n架空語B\n", bom=True)
        ng_filter = NgWordFilter()
        self.assertEqual(ng_filter.words, ["架空語A", "架空語B"])
        self.assertTrue(ng_filter.contains_ng_word("これは架空語Aです"))
        self.assertEqual(ng_filter.mask_ng_words("これは架空語Aです"), "これは●●●●です")

    def test_private_file_with_bom_is_merged_without_duplicates(self) -> None:
        self.write(self.public, "架空語A\n", bom=False)
        self.write(self.private, "架空語A\n架空語C\n", bom=True)
        ng_filter = NgWordFilter()
        self.assertEqual(ng_filter.words, ["架空語A", "架空語C"])
        self.assertEqual(ng_filter.find_ng_words("架空語Cと架空語A"), ["架空語C", "架空語A"])

    def test_file_without_bom_keeps_previous_behavior(self) -> None:
        self.write(self.public, "# 架空のコメント\n\n  架空語A  \n", bom=False)
        self.assertEqual(ng_word_service.load_ng_words(), ["架空語A"])
        self.assertTrue(all("﻿" not in word for word in ng_word_service.load_ng_words()))


class NgWordOrderTests(NgWordFileTestCase):
    def test_find_ng_words_returns_unique_words_in_order_of_first_appearance(self) -> None:
        self.write(self.public, "\n".join(f"架空語{c}" for c in "ABCDE") + "\n", bom=False)
        ng_filter = NgWordFilter()
        text = "架空語E、架空語C、架空語A、架空語E、架空語D、架空語B、架空語C"
        expected = ["架空語E", "架空語C", "架空語A", "架空語D", "架空語B"]
        # set では文字列のハッシュ順（プロセスごとに変わる）になり、5語が一致する確率は1/120
        for _ in range(3):
            self.assertEqual(ng_filter.find_ng_words(text), expected)

    def test_no_match_returns_empty_list(self) -> None:
        self.write(self.public, "架空語A\n", bom=False)
        self.assertEqual(NgWordFilter().find_ng_words("問題のない文"), [])


class NgWordReloadTests(NgWordFileTestCase):
    def test_invalid_encoding_preserves_both_previous_files_and_reports_failure(self) -> None:
        self.write(self.public, "架空語A\n", bom=False)
        self.write(self.private, "架空語B\n", bom=False)
        ng_filter = NgWordFilter()
        self.write(self.public, "架空語C\n", bom=False)
        self.private.write_bytes(b"\xff")
        with self.assertRaises(UnicodeDecodeError):
            ng_filter.reload()
        self.assertEqual(ng_filter.words, ["架空語A", "架空語B"])
        self.assertTrue(ng_filter.contains_ng_word("架空語B"))
        self.assertFalse(ng_filter.contains_ng_word("架空語C"))

    def test_read_error_preserves_previous_snapshot_then_valid_reload_succeeds(self) -> None:
        self.write(self.public, "架空語A\n", bom=False)
        ng_filter = NgWordFilter()
        with patch.object(Path, "read_text", side_effect=PermissionError("架空のロック")):
            with self.assertRaises(PermissionError):
                ng_filter.reload()
        self.assertEqual(ng_filter.words, ["架空語A"])
        self.write(self.public, "架空語B\n", bom=False)
        ng_filter.reload()
        self.assertEqual(ng_filter.words, ["架空語B"])

    def test_reload_publishes_words_and_pattern_only_after_compilation(self) -> None:
        self.write(self.public, "架空語A\n", bom=False)
        ng_filter = NgWordFilter()
        self.write(self.public, "架空語B\n架空語C\n", bom=False)
        original = ng_word_service._build_pattern

        def compile_words(words):
            self.assertEqual(ng_filter.words, ["架空語A"])
            self.assertEqual(ng_filter.word_count, 1)
            self.assertEqual(ng_filter.mask_ng_words("架空語Aと架空語B"), "●●●●と架空語B")
            return original(words)

        with patch.object(ng_word_service, "_build_pattern", side_effect=compile_words):
            ng_filter.reload()
        self.assertEqual(ng_filter.words, ["架空語B", "架空語C"])
        self.assertEqual(ng_filter.mask_ng_words("架空語Aと架空語B"), "架空語Aと●●●●")


if __name__ == "__main__":
    unittest.main()
