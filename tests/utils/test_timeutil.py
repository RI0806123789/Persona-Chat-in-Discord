"""OSのタイムゾーンDBがない環境でも、日本時間を使えることを確認する。"""

import os
from pathlib import Path
import subprocess
import sys
import unittest


class TimezoneDataTests(unittest.TestCase):
    def test_tokyo_resolves_without_system_timezone_database(self) -> None:
        # 別プロセスでOSのDBとZoneInfoキャッシュを使わず、tzdataによる解決を確かめる。
        script = """
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import TZPATH
sys.path.insert(0, 'src')
from persona_bot.utils.timeutil import resolve_timezone, timezone_label
assert TZPATH == (), TZPATH
tokyo = resolve_timezone('Asia/Tokyo', purpose='日本時間の回帰テスト')
assert timezone_label(tokyo) == 'Asia/Tokyo', timezone_label(tokyo)
utc = datetime(2026, 10, 18, 15, tzinfo=timezone.utc)
local = utc.astimezone(tokyo)
assert local.utcoffset() == timedelta(hours=9), local
assert local.isoformat() == '2026-10-19T00:00:00+09:00', local
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=Path(__file__).resolve().parents[2],
            env={**os.environ, "PYTHONTZPATH": "", "PYTHONIOENCODING": "utf-8"},
            capture_output=True, encoding="utf-8", timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("UTC で代用します", result.stdout)


if __name__ == "__main__":
    unittest.main()
