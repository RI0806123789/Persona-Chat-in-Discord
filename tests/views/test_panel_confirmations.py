"""サブパネルの選択・確定・解除の結果を、画面と設定の両方で確かめる（#152）。

キャラ設定と自発会話は「確定を押すまで GuildSettings を変えない」一時選択値方式で、
ブロック解除は選んだ利用者だけを解除する。Discord はモックで、DB には書き込まない。
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import discord

from persona_bot.config import personas
from persona_bot.config.choices import MODEL_CHOICES, RESPOND_MODE_CHOICES
from persona_bot.config.constants import DISCORD_SELECT_MAX_OPTIONS
from persona_bot.core.app_state import BotState
from persona_bot.views import settings_view
from persona_bot.views.auto_topic_view import (
    AutonomousToggleButton, AutoTopicConfirmButton, AutoTopicIntervalSelect, AutoTopicToggleButton, AutoTopicView,
)
from persona_bot.views.settings_view import ConfirmButton, ModelSelect, PersonaSelect, RespondModeSelect, SettingsView
from persona_bot.views.unblock_view import UnblockSelect, UnblockView
from tests.helpers.persona_helpers import install_persona_catalog
from tests.helpers.member_helpers import make_member, make_owner_client


def make_interaction(*, admin: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        user=make_member(admin=admin),
        client=make_owner_client(),
        response=SimpleNamespace(
            defer=AsyncMock(), edit_message=AsyncMock(), send_message=AsyncMock(), is_done=Mock(return_value=False),
        ),
        edit_original_response=AsyncMock(),
    )


def child(view, item_type):
    return next(item for item in view.children if isinstance(item, item_type))


async def press(view, item_type, *, values=None):
    """パネル上の部品を1つ操作する（セレクトなら値を選んでから）。"""
    item = child(view, item_type)
    if values is not None:
        item._values = list(values)
    interaction = make_interaction()
    await item.callback(interaction)
    return interaction


class SettingsConfirmTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        catalog = install_persona_catalog(self, "../prompts/架空のペルソナA.txt", "../prompts/架空のペルソナB.txt")
        self.settings = BotState().get_guild(1)
        self.settings.current_prompt_file = catalog.personas[0].value
        self.settings.persist = AsyncMock()
        self.before = (
            self.settings.current_prompt_file, self.settings.current_model_name, self.settings.current_respond_mode,
        )
        # 現在値と違う選択肢を選ぶ
        self.persona = next(p.value for p in catalog.personas if p.value != self.before[0])
        self.model = next(c.value for c in MODEL_CHOICES if c.value != self.before[1])
        self.mode = next(c.value for c in RESPOND_MODE_CHOICES if c.value != self.before[2])
        self.view = SettingsView(self.settings, 3, "なし")
        self.addCleanup(self.view.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    async def choose_all(self) -> None:
        for item_type, value in ((PersonaSelect, self.persona), (ModelSelect, self.model), (RespondModeSelect, self.mode)):
            interaction = await press(self.view, item_type, values=[value])
            # 選んだだけではパネルの表示が変わるだけで、サーバーの設定は変えない
            interaction.response.edit_message.assert_awaited_once()
        self.assertEqual(
            (self.settings.current_prompt_file, self.settings.current_model_name, self.settings.current_respond_mode),
            self.before,
        )
        self.settings.persist.assert_not_awaited()

    def persona_path(self, exists: bool) -> Path:
        path = Path(self.temp.name) / "架空のペルソナ.txt"
        if exists:
            path.write_text("架空のペルソナです。", encoding="utf-8")
        return path

    async def test_selected_values_are_applied_and_saved_only_on_confirm(self) -> None:
        await self.choose_all()
        with patch.object(personas, "resolve_prompt_path", return_value=self.persona_path(True)) as resolve:
            interaction = await press(self.view, ConfirmButton)
        resolve.assert_called_once_with(self.persona)
        self.assertEqual(
            (self.settings.current_prompt_file, self.settings.current_model_name, self.settings.current_respond_mode),
            (self.persona, self.model, self.mode),
        )
        self.settings.persist.assert_awaited_once()
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        self.assertIsNone(interaction.edit_original_response.await_args.kwargs["view"])
        self.assertTrue(self.view.is_finished())

    async def test_missing_persona_file_keeps_settings_unchanged(self) -> None:
        await self.choose_all()
        with patch.object(personas, "resolve_prompt_path", return_value=self.persona_path(False)):
            interaction = await press(self.view, ConfirmButton)
        self.assertEqual(
            (self.settings.current_prompt_file, self.settings.current_model_name, self.settings.current_respond_mode),
            self.before,
        )
        self.settings.persist.assert_not_awaited()
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        content = interaction.edit_original_response.await_args.kwargs["content"]
        self.assertIn("見つかりませんでした", content)
        self.assertIn("設定は変更されていません", content)


class AutoTopicConfirmTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.settings = BotState().get_guild(1)
        self.settings.persist = AsyncMock()
        self.settings.auto_topic_enabled = False
        self.settings.autonomous_enabled = False
        self.settings.auto_topic_interval_hours = 1
        # 配信の開始に失敗し続けて、再挑戦までの待ち時間が入っている状態
        self.settings.autonomous_start_failures = 3
        self.settings.autonomous_start_retry_at = datetime.now(timezone.utc) + timedelta(hours=2)
        self.view = AutoTopicView(self.settings)
        self.addCleanup(self.view.stop)

    async def test_toggles_and_interval_are_applied_only_on_confirm(self) -> None:
        await press(self.view, AutoTopicIntervalSelect, values=["6"])
        await press(self.view, AutoTopicToggleButton)
        await press(self.view, AutonomousToggleButton)
        self.assertEqual(
            (self.settings.auto_topic_enabled, self.settings.autonomous_enabled, self.settings.auto_topic_interval_hours),
            (False, False, 1),
        )
        self.assertEqual(self.settings.autonomous_start_failures, 3)
        self.settings.persist.assert_not_awaited()

        interaction = await press(self.view, AutoTopicConfirmButton)
        self.assertEqual(
            (self.settings.auto_topic_enabled, self.settings.autonomous_enabled, self.settings.auto_topic_interval_hours),
            (True, True, 6),
        )
        # 確定で再挑戦の待ち時間を解除し、直した権限をすぐ試せるようにする
        self.assertIsNone(self.settings.autonomous_start_retry_at)
        self.assertEqual(self.settings.autonomous_start_failures, 0)
        self.settings.persist.assert_awaited_once()
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        self.assertIsNone(interaction.edit_original_response.await_args.kwargs["view"])

    async def test_toggling_twice_returns_to_the_original_value(self) -> None:
        await press(self.view, AutoTopicToggleButton)
        await press(self.view, AutoTopicToggleButton)
        await press(self.view, AutoTopicConfirmButton)
        self.assertFalse(self.settings.auto_topic_enabled)
        self.settings.persist.assert_awaited_once()


class UnblockPanelTests(unittest.IsolatedAsyncioTestCase):
    def make_view(self, users, *, unblocked: bool = True):
        blacklist = SimpleNamespace(unblock=AsyncMock(return_value=unblocked), is_blocked=Mock(return_value=False))
        view = UnblockView(blacklist, users)
        self.addCleanup(view.stop)
        return view, blacklist

    async def test_unblock_reports_success_or_already_unblocked_user(self) -> None:
        users = [{"user_id": 22, "user_name": "架空の対象", "count": 3}]
        for unblocked, expected in ((True, "✅ 架空の対象 のブロックを解除しました。"),
                                    (False, "架空の対象 はブラックリストに登録されていませんでした。")):
            with self.subTest(unblocked=unblocked):
                view, blacklist = self.make_view(users, unblocked=unblocked)
                interaction = await press(view, UnblockSelect, values=["22"])
                blacklist.unblock.assert_awaited_once_with(22)
                interaction.response.defer.assert_awaited_once_with(ephemeral=True)
                self.assertEqual(interaction.edit_original_response.await_args.kwargs,
                                 {"content": expected, "view": None})

    async def test_select_lists_up_to_the_discord_limit_with_violation_counts(self) -> None:
        users = [{"user_id": 1000 + index, "user_name": f"架空{index}", "count": 3} for index in range(30)]
        view, _ = self.make_view(users)
        select = child(view, UnblockSelect)
        self.assertEqual(len(select.options), DISCORD_SELECT_MAX_OPTIONS)
        self.assertEqual(select.options[0].value, "1000")
        self.assertEqual(select.options[0].description, "違反 3 回")
        # 戻るボタンも持つ
        self.assertEqual(len(view.children), 2)


if __name__ == "__main__":
    unittest.main()
