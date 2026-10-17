"""手元のリポジトリから読む情報（local_repo.py）の回帰テスト。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_repo import DATABASE_PY, local_schema_version  # noqa: E402


class LocalSchemaVersionTests(unittest.TestCase):
    def test_reads_this_repository(self) -> None:
        # このリポジトリの database.py から、整数の版が読めること（ボットのコードは import しない）
        self.assertTrue(DATABASE_PY.is_file())
        version = local_schema_version()
        self.assertIsInstance(version, int)
        self.assertGreaterEqual(version, 1)

    def test_reads_plain_and_annotated_assignments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "database.py"
            path.write_text("OTHER = 1\nSCHEMA_VERSION = 12\n", encoding="utf-8")
            self.assertEqual(local_schema_version(path), 12)
            path.write_text("SCHEMA_VERSION: int = 13\n", encoding="utf-8")
            self.assertEqual(local_schema_version(path), 13)

    def test_returns_none_when_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "database.py"
            self.assertIsNone(local_schema_version(path))  # ファイルが無い
            path.write_text("SCHEMA_VERSION = (\n", encoding="utf-8")
            self.assertIsNone(local_schema_version(path))  # 構文エラー
            path.write_text("SCHEMA_VERSION = compute()\n", encoding="utf-8")
            self.assertIsNone(local_schema_version(path))  # 定数でない
            path.write_text("SCHEMA_VERSION = True\n", encoding="utf-8")
            self.assertIsNone(local_schema_version(path))  # bool は版として扱わない


if __name__ == "__main__":
    unittest.main()
