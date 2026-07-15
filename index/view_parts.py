from typing import Any, Callable

import discord
from discord import ui

# ハブ（機能パネル）を再生成する呼び出し可能オブジェクトの型。
# functions_view を直接 import すると循環参照になるため、呼び出し側から
# FunctionsView.clone のような生成関数を受け取る形にする。
HubFactory = Callable[[], Any]


class BackButton(ui.Button):
    """機能パネル（ハブ）へ戻るボタン。各サブパネル共通。

    親 View が保持する make_hub でハブパネルを再生成し、同じメッセージを
    差し替える。make_hub が無い場合は従来どおりパネルを閉じる。
    """

    def __init__(self, *, row: int) -> None:
        super().__init__(label="戻る", style=discord.ButtonStyle.secondary, emoji="↩️", row=row)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        make_hub: HubFactory | None = getattr(view, "make_hub", None)
        if make_hub is None:
            # エフェメラルメッセージは delete() できないため、内容を編集して閉じる。
            await interaction.response.edit_message(content="パネルを閉じました。", view=None)
            if view is not None:
                view.stop()
            return

        hub = make_hub()
        await interaction.response.edit_message(content=hub.build_preview(), view=hub)
        hub.message = interaction.message
        if view is not None:
            view.stop()
