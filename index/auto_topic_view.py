import discord
from discord import ui

from app_state import BotState
from choices import AUTO_TOPIC_INTERVAL_CHOICES, AUTO_TOPIC_INTERVAL_DISPLAY_NAMES
from view_parts import BackButton, HubFactory


def _build_status_text(
    enabled: bool,
    interval_hours: int,
    watched_count: int,
    *,
    confirmed: bool = False,
) -> str:
    """自発会話パネルのステータス表示テキストを組み立てる。"""
    enabled_display = "✅ 有効" if enabled else "❌ 無効"
    interval_display = AUTO_TOPIC_INTERVAL_DISPLAY_NAMES.get(interval_hours, f"{interval_hours}時間")

    if confirmed:
        header = "✅ 自発的な会話の設定を更新しました。"
    else:
        header = "💬 **自発会話パネル** — 無会話が続いたときに、ボットから話題提供する機能です。"

    return (
        f"{header}\n"
        f"・**自発的な会話**: {enabled_display}\n"
        f"・**無会話しきい値**: {interval_display}\n"
        f"・**監視中チャンネル**: {watched_count}件（ボットが会話したチャンネルが自動で対象になります）\n"
        "※ 一度話題提供したら、誰かが発言するまで同じチャンネルには再投稿しません。"
    )


class AutoTopicIntervalSelect(ui.Select):
    """無会話しきい値（何時間会話が無ければ話題提供するか）のセレクトメニュー。"""

    def __init__(self, current_value: int) -> None:
        options = [
            discord.SelectOption(
                label=choice.name,
                # SelectOption の value は文字列のみ。int は str に変換して保持する。
                value=str(choice.value),
                default=(choice.value == current_value),
            )
            for choice in AUTO_TOPIC_INTERVAL_CHOICES
        ]
        super().__init__(placeholder="🕒 無会話しきい値を選択…", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "AutoTopicView" = self.view  # type: ignore[assignment]
        view.temp_interval_hours = int(self.values[0])
        view.clear_items()
        view._add_all_items()
        await interaction.response.edit_message(content=view.build_preview(), view=view)


class AutoTopicToggleButton(ui.Button):
    """自発的な会話の ON/OFF トグルボタン。"""

    def __init__(self, enabled: bool) -> None:
        label = "💬 自発的な会話: ON" if enabled else "💬 自発的な会話: OFF"
        style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary
        super().__init__(label=label, style=style, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "AutoTopicView" = self.view  # type: ignore[assignment]
        view.temp_enabled = not view.temp_enabled
        view.clear_items()
        view._add_all_items()
        await interaction.response.edit_message(content=view.build_preview(), view=view)


class AutoTopicConfirmButton(ui.Button):
    """確定ボタン。ここで初めて BotState に反映する。"""

    def __init__(self) -> None:
        super().__init__(label="確定", style=discord.ButtonStyle.success, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "AutoTopicView" = self.view  # type: ignore[assignment]

        # 一時選択値をグローバルな BotState に反映
        view.state.auto_topic_enabled = view.temp_enabled
        view.state.auto_topic_interval_hours = view.temp_interval_hours

        confirmed_text = _build_status_text(
            view.temp_enabled,
            view.temp_interval_hours,
            len(view.state.auto_topic_channels),
            confirmed=True,
        )
        await interaction.response.edit_message(content=confirmed_text, view=None)
        view.stop()


class AutoTopicView(ui.View):
    """自発的な会話の設定パネル全体を表す View。

    しきい値セレクトと ON/OFF トグルを保持し、確定を押すまで BotState を
    変更しない（SettingsView と同じ一時選択値方式）。
    パネル自体はエフェメラル（本人だけに見える）で表示される想定。
    """

    def __init__(
        self,
        state: BotState,
        *,
        make_hub: HubFactory | None = None,
        timeout: float = 120.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.state = state
        # 「戻る」ボタンが機能パネルを再生成するための生成関数
        self.make_hub = make_hub
        # タイムアウト時に UI を取り除くためのパネルメッセージ参照（送信後に設定される）
        self.message: discord.Message | None = None

        # 一時選択値（現在の設定を初期値として保存）
        self.temp_enabled: bool = state.auto_topic_enabled
        self.temp_interval_hours: int = state.auto_topic_interval_hours

        self._add_all_items()

    def _add_all_items(self) -> None:
        """UIパーツを View に追加する。選択・トグル更新時にも使用する。"""
        self.add_item(AutoTopicIntervalSelect(self.temp_interval_hours))
        self.add_item(AutoTopicToggleButton(self.temp_enabled))
        self.add_item(AutoTopicConfirmButton())
        self.add_item(BackButton(row=2))

    def build_preview(self) -> str:
        """現在の一時選択値からパネル表示テキストを組み立てる。"""
        return _build_status_text(
            self.temp_enabled,
            self.temp_interval_hours,
            len(self.state.auto_topic_channels),
        )

    async def on_timeout(self) -> None:
        """タイムアウト時に、操作できなくなった UI をメッセージから取り除く。"""
        if self.message is None:
            return
        try:
            await self.message.edit(
                content="⏱️ 自発会話パネルは時間切れになりました。設定は変更されていません。再度 /functions を実行してください。",
                view=None,
            )
        except discord.HTTPException as error:
            print(f"自発会話パネルのタイムアウト処理エラー: {error}")
