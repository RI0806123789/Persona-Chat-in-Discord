"""エフェメラルパネルの往復後もWebhook経由で時間切れ表示にできることを確認する。"""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from view_parts import BackButton, PanelView, swap_panel


class PanelNavigationTests(unittest.IsolatedAsyncioTestCase):
    def make_panel(self):
        panel = PanelView(timeout_notice="時間切れです。")
        self.addCleanup(panel.stop)
        return panel

    def make_interaction(self):
        # 通常メッセージの編集はエフェメラルでは失敗する。使えばテストを失敗させる。
        channel_message = SimpleNamespace(edit=AsyncMock(side_effect=AssertionError("通常の編集は使用できません。")))
        webhook_message = SimpleNamespace(edit=AsyncMock())
        return SimpleNamespace(
            message=channel_message, original_response=AsyncMock(return_value=webhook_message),
            response=SimpleNamespace(edit_message=AsyncMock()),
        ), webhook_message

    async def test_subpanel_timeout_edits_original_response_and_stops_hub(self):
        hub, panel = self.make_panel(), self.make_panel()
        interaction, webhook_message = self.make_interaction()
        await swap_panel(interaction, hub, panel, "サブパネル")
        self.assertTrue(hub.is_finished())
        self.assertIs(panel.message, webhook_message)
        await panel.on_timeout()
        webhook_message.edit.assert_awaited_once_with(content="時間切れです。", view=None)
        interaction.message.edit.assert_not_awaited()

    async def test_back_button_rebinds_to_webhook_and_stops_subpanel(self):
        hub, panel = self.make_panel(), self.make_panel()
        hub.build_preview = Mock(return_value="機能パネル")
        panel.make_hub = Mock(return_value=hub)
        button = BackButton(row=0)
        panel.add_item(button)
        interaction, webhook_message = self.make_interaction()
        await button.callback(interaction)
        self.assertTrue(panel.is_finished())
        self.assertIs(hub.message, webhook_message)
        interaction.response.edit_message.assert_awaited_once_with(content="機能パネル", view=hub)
        await hub.on_timeout()
        webhook_message.edit.assert_awaited_once_with(content="時間切れです。", view=None)
        interaction.message.edit.assert_not_awaited()

    async def test_back_without_hub_closes_panel_without_fetch(self):
        panel = self.make_panel()
        button = BackButton(row=0)
        panel.add_item(button)
        interaction, _ = self.make_interaction()
        await button.callback(interaction)
        self.assertTrue(panel.is_finished())
        interaction.original_response.assert_not_awaited()
        interaction.response.edit_message.assert_awaited_once_with(content="パネルを閉じました。", view=None)
