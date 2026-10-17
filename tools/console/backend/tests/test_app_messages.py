"""バックエンド（app.py）が画面に出す案内文の回帰テスト。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402


class SudoPasswordMessageTests(unittest.TestCase):
    def test_allows_only_the_three_service_actions(self) -> None:
        message = app._sudo_password_message("ubuntu", "bot")
        self.assertIn("NOPASSWD", message)
        for action in ("start", "stop", "restart"):
            self.assertIn(f"/usr/bin/systemctl {action} bot", message)
        # 何でも実行できる書き方（ALL を許可するコマンド）を案内しないこと
        self.assertNotIn("NOPASSWD: ALL", message)

    def test_placeholder_when_user_comes_from_ssh_config(self) -> None:
        # ~/.ssh/config からユーザー名を読む接続先では、プロファイルにユーザー名が無い
        self.assertIn("<ユーザー名> ALL=(root)", app._sudo_password_message(None, "bot"))


if __name__ == "__main__":
    unittest.main()
