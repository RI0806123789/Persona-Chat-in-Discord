"""ブロック解除パネル（/functions → 🚫 ブロック解除。管理者専用）。

ハブ（functions_view.py）の UnblockButton から開く。ほかのサブパネル（settings_view.py など）と
同じく、パネル本体は独立したモジュールに置き、ハブはボタンだけを持つ。
管理者の判定はボタンを押したときと、解除を選んだときの両方で行う（パネルを開いた後に
権限を外された管理者が、開いたままの画面から解除できないようにするため）。
"""

import discord
from discord import ui

from persona_bot.config.constants import DISCORD_SELECT_MAX_OPTIONS
from persona_bot.services.blacklist_service import BlacklistService
from persona_bot.utils.discord_helpers import is_administrator, is_current_administrator
from persona_bot.views.view_parts import (
    BackButton, HubFactory, PanelView, begin_panel_operation, edit_panel_message, select_option_text,
)


class UnblockSelect(ui.Select):
    """ブロック中ユーザーから解除対象を選ぶセレクトメニュー。"""

    def __init__(self, blocked_users: list) -> None:
        options = []
        # Discord のセレクトは最大25件まで
        for record in blocked_users[:DISCORD_SELECT_MAX_OPTIONS]:
            user_id = record["user_id"]
            name = record.get("user_name") or str(user_id)
            count = record.get("count", "?")
            options.append(
                discord.SelectOption(
                    label=select_option_text(name, fallback=str(user_id)),
                    value=str(user_id),
                    description=f"違反 {count} 回",
                )
            )
        super().__init__(placeholder="解除するユーザーを選択…", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "UnblockView" = self.view  # type: ignore[assignment]
        if not is_administrator(interaction.user):
            await interaction.response.send_message("この機能はサーバー管理者のみ利用できます。", ephemeral=True)
            return
        begin_panel_operation(view, interaction)
        user_id = int(self.values[0])
        # 選択肢のラベルから表示名を拾う（通知メッセージ用）
        name = next((o.label for o in self.options if o.value == self.values[0]), str(user_id))
        await interaction.response.defer(ephemeral=True)
        # 待機中に権限が外されても、操作時の写しだけで解除しない。
        if not await is_current_administrator(interaction):
            await edit_panel_message(
                interaction, deferred=True,
                content="この機能はサーバー管理者のみ利用できます。", view=None,
            )
            return
        if await view.blacklist.unblock(user_id):
            await edit_panel_message(interaction, deferred=True, content=f"✅ {name} のブロックを解除しました。", view=None)
        else:
            await edit_panel_message(interaction, deferred=True,
                content=f"{name} はブラックリストに登録されていませんでした。", view=None
            )


class UnblockView(PanelView):
    """ブロック解除用のパネル。ブロック中ユーザーの選択メニューを持つ。"""

    def __init__(
        self,
        blacklist: BlacklistService,
        blocked_users: list,
        *,
        make_hub: HubFactory | None = None,
    ) -> None:
        super().__init__(
            timeout_notice=(
                "⏱️ ブロック解除パネルは時間切れになりました。"
                "再度 /functions を実行してください。"
            )
        )
        self.blacklist = blacklist
        self.blocked_users = blocked_users
        self.blocked_user_check = blacklist.is_blocked
        # 「戻る」ボタンが機能パネルを再生成するための生成関数
        self.make_hub = make_hub

        self._add_all_items()

    def _add_all_items(self) -> None:
        """UI パーツを View に追加する。"""
        self.add_item(UnblockSelect(self.blocked_users))
        self.add_item(BackButton(row=1))
