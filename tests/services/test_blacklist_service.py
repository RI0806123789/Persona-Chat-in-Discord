"""ブラックリストの初回保存と、手書きなどで崩れた記録への耐性を確かめる（#68）。"""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.services.blacklist_service import BlacklistService


class BlacklistFileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        quiet = patch("builtins.print")
        quiet.start()
        self.addCleanup(quiet.stop)

    def saved(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    async def test_first_violation_creates_the_missing_directory(self) -> None:
        path = self.root / "ng" / "blacklist.json"  # ng/ がまだ無いクリーンな環境
        service = BlacklistService(path)
        self.assertEqual(await service.record_violation(11, "架空の利用者"), (1, False))
        self.assertEqual(self.saved(path)["11"]["count"], 1)

    async def test_malformed_records_do_not_break_checks_or_updates(self) -> None:
        path = self.root / "blacklist.json"
        path.write_text(json.dumps({
            "11": "架空の壊れた値",
            "12": {"count": "架空の非数値", "blocked": False},
            "架空の非数値ID": {"count": 3, "blocked": True},
            "13": {"count": 3, "blocked": True, "user_name": "架空のブロック中"},
        }, ensure_ascii=False), encoding="utf-8")
        service = BlacklistService(path)
        self.assertFalse(service.is_blocked(11))
        self.assertEqual(service.get_violation_count(11), 0)
        self.assertEqual(service.get_violation_count(12), 0)
        self.assertTrue(service.is_blocked(13))
        self.assertEqual([user["user_id"] for user in service.get_all_blocked_users()], [13])
        # 壊れた記録は新しく数え直し、保存時に正しい形へ置き換わる
        self.assertEqual(await service.record_violation(11, "架空の利用者"), (1, False))
        self.assertEqual(await service.record_violation(12, "架空の利用者"), (1, False))
        self.assertEqual(await service.record_violation(13, "架空の利用者"), (3, False))
        saved = self.saved(path)
        self.assertEqual((saved["11"]["count"], saved["12"]["count"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
