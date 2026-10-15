"""長い予定があっても一覧の表示と取り消しができることを検証する。"""

from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from constants import REMINDER_MAX_PENDING_PER_USER, REMINDER_PANEL_MAX_LENGTH
from reminder_service import ReminderService
import reminder_view
from reminder_view import ReminderCancelSelect, ReminderView, _build_status_text


def make_records(count: int, *, body: str = "架空の予定") -> list[dict]:
    return [
        {
            "reminder_id": index + 1,
            "message": f"{index + 1:02d}：{body}",
            "remind_at": "2026-01-01T01:00:00+00:00",
            "recurrence": "daily",
        }
        for index in range(count)
    ]


class ReminderStatusTextTests(unittest.TestCase):
    def setUp(self) -> None:
        # 表示だけを呼び、DB・Discord・外部サービスへは接続しない。
        self.service = ReminderService(None, None, None)

    def assert_complete_notes(self, text: str) -> None:
        self.assertIn("取り消したいものはセレクトメニューから選んでください。", text)
        self.assertTrue(text.endswith("取り消すまでこの一覧に残り続けます。"))

    def test_empty_list_shows_registration_instructions(self) -> None:
        text = _build_status_text([], self.service)
        self.assertLessEqual(len(text), REMINDER_PANEL_MAX_LENGTH)
        self.assertIn("予定されているリマインダーはありません。", text)
        self.assertIn("30分後にゴミ出しって知らせて", text)
        self.assertNotIn("…ほか", text)

    def test_short_list_keeps_descriptions_and_notes(self) -> None:
        records = make_records(2)
        text = _build_status_text(records, self.service)
        for record in records:
            self.assertIn(f"・{self.service.describe(record)}", text)
        self.assertNotIn("…ほか", text)
        self.assert_complete_notes(text)

    def test_long_lists_fit_limit_and_report_every_omitted_record(self) -> None:
        for count in (9, REMINDER_MAX_PENDING_PER_USER):
            for body in ("あ" * 200, "x" * 200 + "…", "😀" * 200, "予定\n" * 100):
                with self.subTest(count=count, body=body[:5]):
                    records = make_records(count, body=body)
                    original = deepcopy(records)
                    text = _build_status_text(records, self.service)
                    self.assertLessEqual(len(text), REMINDER_PANEL_MAX_LENGTH)
                    shown = sum(f"・{self.service.describe(record)}" in text for record in records)
                    self.assertGreater(shown, 0)
                    self.assertLess(shown, count)
                    self.assertIn(f"…ほか {count - shown} 件", text)
                    self.assert_complete_notes(text)
                    self.assertEqual(records, original)

    def test_exact_limit_keeps_last_record_but_one_character_less_omits_it(self) -> None:
        records = make_records(2, body="あ" * 100)
        complete = _build_status_text(records, self.service)
        with patch.object(reminder_view, "REMINDER_PANEL_MAX_LENGTH", len(complete), create=True):
            self.assertEqual(_build_status_text(records, self.service), complete)
        with patch.object(reminder_view, "REMINDER_PANEL_MAX_LENGTH", len(complete) - 1, create=True):
            shortened = _build_status_text(records, self.service)
        self.assertLessEqual(len(shortened), len(complete) - 1)
        self.assertIn(f"・{self.service.describe(records[0])}", shortened)
        self.assertNotIn(f"・{self.service.describe(records[1])}", shortened)
        self.assertIn("…ほか 1 件", shortened)
        self.assert_complete_notes(shortened)

    def test_select_limit_is_also_included_in_omitted_count(self) -> None:
        records = make_records(26)
        text = _build_status_text(records, self.service)
        shown = sum(f"・{self.service.describe(record)}" in text for record in records)
        self.assertEqual(shown, 25)
        self.assertIn("…ほか 1 件", text)
        self.assertLessEqual(len(text), REMINDER_PANEL_MAX_LENGTH)

    def test_single_oversized_record_leaves_panel_instructions_available(self) -> None:
        text = _build_status_text(make_records(1, body="あ" * 5000), self.service)
        self.assertLessEqual(len(text), REMINDER_PANEL_MAX_LENGTH)
        self.assertIn("…ほか 1 件", text)
        self.assert_complete_notes(text)


class ReminderViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_records_remain_selectable_and_hidden_record_can_be_canceled(self) -> None:
        records = make_records(REMINDER_MAX_PENDING_PER_USER, body="😀" * 200)
        service = ReminderService(None, None, None)
        service.cancel = AsyncMock(return_value=records[-1])
        view = ReminderView(service, records, "11")
        self.addCleanup(view.stop)
        text = view.build_preview()
        self.assertLessEqual(len(text), REMINDER_PANEL_MAX_LENGTH)
        self.assertNotIn(service.describe(records[-1]), text)
        select = next(item for item in view.children if isinstance(item, ReminderCancelSelect))
        self.assertEqual([option.value for option in select.options],
                         [str(record["reminder_id"]) for record in records])
        self.assertTrue(all(len(option.label) <= 100 for option in select.options))
        self.assertTrue(all(len(option.description) <= 100 for option in select.options))
        # Discord に送られるコンポーネントにも全件残る。
        self.assertEqual(len(view.to_components()[0]["components"][0]["options"]), len(records))
        select._values = [str(records[-1]["reminder_id"])]
        interaction = SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock()))
        await select.callback(interaction)
        service.cancel.assert_awaited_once_with(records[-1]["reminder_id"], "11")
        self.assertIn("リマインダーを取り消しました。",
                      interaction.response.edit_message.call_args.kwargs["content"])
        self.assertIsNone(interaction.response.edit_message.call_args.kwargs["view"])
        self.assertTrue(view.is_finished())

    async def test_empty_list_does_not_build_an_empty_select(self) -> None:
        view = ReminderView(ReminderService(None, None, None), [], "11")
        self.addCleanup(view.stop)
        self.assertFalse(any(isinstance(item, ReminderCancelSelect) for item in view.children))
        self.assertEqual(len(view.children), 1)


if __name__ == "__main__":
    unittest.main()
