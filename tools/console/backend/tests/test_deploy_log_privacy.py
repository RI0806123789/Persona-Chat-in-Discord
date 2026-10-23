"""再起動ログの抜粋と文字数制限をまたぐ秘匿属性の回帰。実SSHや実データは使わない。"""

from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deploy import DeployRecord, DeployRunner, HEALTH_LOG_TAIL, OUTPUT_MAX_CHARS, Step


class DeployLogPrivacyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.record = DeployRecord(1, "fixture", "development", "bot", "main", 0, [])
        self.runner = DeployRunner(Mock(), Mock(), self.record)
        self.step = Step("health", "起動確認", "架空の確認")
        self.record.steps.append(self.step)

    def test_tail_without_conversation_header_keeps_sensitive_attribute(self) -> None:
        entries = [{"message": "AI：架空の開始"}] + [
            {"message": f"架空の応答途中{index}"} for index in range(HEALTH_LOG_TAIL + 5)
        ]
        self.runner._append_log_tail(self.step, entries)
        self.assertNotIn("AI：", self.step.output)
        self.assertTrue(self.record.to_dict()["steps"][0].get("output_contains_logs", False))
        self.assertTrue(self.step.output.endswith(entries[-1]["message"]))
        self.assertEqual(self.step.output.splitlines()[1:], [entry["message"] for entry in entries[-HEALTH_LOG_TAIL:]])

    def test_character_limit_and_later_append_keep_sensitive_attribute(self) -> None:
        self.runner._append_log_tail(self.step, [{"message": "架空の本文" * OUTPUT_MAX_CHARS}])
        self.assertEqual(len(self.step.output), OUTPUT_MAX_CHARS)
        self.assertNotIn("再起動後のログ", self.step.output)
        self.runner._append_output(self.step, "架空の後続診断")
        self.assertTrue(self.record.to_dict()["steps"][0].get("output_contains_logs", False))
        self.assertTrue(self.step.output.endswith("架空の後続診断"))
        self.assertEqual(len(self.step.output), OUTPUT_MAX_CHARS)

    def test_non_journal_output_is_explicit_and_raw_text_is_unchanged(self) -> None:
        self.runner._append_output(self.step, "Fast-forward\n架空のコマンド結果")
        self.runner._append_log_tail(self.step, [])
        self.assertFalse(self.record.to_dict()["steps"][0].get("output_contains_logs", False))
        self.assertTrue(self.step.output.startswith("Fast-forward\n架空のコマンド結果"))
