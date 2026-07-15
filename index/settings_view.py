import discord
from discord import ui

from app_state import BotState
from choices import (
    MODEL_CHOICES,
    MODEL_DISPLAY_NAMES,
    PERSONA_CHOICES,
    PERSONA_DISPLAY_NAMES,
    RESPOND_MODE_CHOICES,
    RESPOND_MODE_DISPLAY_NAMES,
)
from paths import resolve_prompt_path
from view_parts import BackButton, HubFactory


def _build_status_text(
    persona_file: str,
    model_name: str,
    respond_mode: str,
    ng_count: int,
    blacklist_status: str,
    *,
    confirmed: bool = False,
) -> str:
    """キャラ設定パネルのステータス表示テキストを組み立てる。"""
    persona_display = PERSONA_DISPLAY_NAMES.get(persona_file, f"不明 ({persona_file})")
    model_display = MODEL_DISPLAY_NAMES.get(model_name, f"不明 ({model_name})")
    mode_display = RESPOND_MODE_DISPLAY_NAMES.get(respond_mode, f"不明 ({respond_mode})")

    if confirmed:
        header = "✅ キャラ設定を更新しました。"
    else:
        header = "⚙️ **キャラ設定パネル** — 変更したい項目を選択してください。"

    return (
        f"{header}\n"
        f"・**ペルソナ**: {persona_display}\n"
        f"・**モデル**: {model_display}\n"
        f"・**応答モード**: {mode_display}\n"
        f"・**NGワード**: {ng_count}件\n"
        f"・**ブラックリスト**: {blacklist_status}"
    )


class PersonaSelect(ui.Select):
    """ペルソナ選択用セレクトメニュー。"""

    def __init__(self, current_value: str) -> None:
        options = [
            discord.SelectOption(
                label=choice.name,
                value=choice.value,
                default=(choice.value == current_value),
            )
            for choice in PERSONA_CHOICES
        ]
        super().__init__(
            placeholder="👤 ペルソナを選択…",
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: SettingsView = self.view  # type: ignore[assignment]
        view.temp_persona = self.values[0]
        view.clear_items()
        view._add_all_items()
        await interaction.response.edit_message(content=view.build_preview(), view=view)


class ModelSelect(ui.Select):
    """AIモデル選択用セレクトメニュー。"""

    def __init__(self, current_value: str) -> None:
        options = [
            discord.SelectOption(
                label=choice.name,
                value=choice.value,
                default=(choice.value == current_value),
            )
            for choice in MODEL_CHOICES
        ]
        super().__init__(
            placeholder="🤖 AIモデルを選択…",
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: SettingsView = self.view  # type: ignore[assignment]
        view.temp_model = self.values[0]
        view.clear_items()
        view._add_all_items()
        await interaction.response.edit_message(content=view.build_preview(), view=view)


class RespondModeSelect(ui.Select):
    """応答モード選択用セレクトメニュー。"""

    def __init__(self, current_value: str) -> None:
        options = [
            discord.SelectOption(
                label=choice.name,
                value=choice.value,
                default=(choice.value == current_value),
            )
            for choice in RESPOND_MODE_CHOICES
        ]
        super().__init__(
            placeholder="💬 応答モードを選択…",
            options=options,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: SettingsView = self.view  # type: ignore[assignment]
        view.temp_respond_mode = self.values[0]
        view.clear_items()
        view._add_all_items()
        await interaction.response.edit_message(content=view.build_preview(), view=view)


class ConfirmButton(ui.Button):
    """確定ボタン。"""

    def __init__(self) -> None:
        super().__init__(
            label="確定",
            style=discord.ButtonStyle.success,
            row=3,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: SettingsView = self.view  # type: ignore[assignment]

        # ペルソナのファイル存在チェック
        if not resolve_prompt_path(view.temp_persona).exists():
            await interaction.response.edit_message(
                content=f"エラー: ファイル「{view.temp_persona}」が見つかりませんでした。設定は変更されていません。",
                view=None,
            )
            view.stop()
            return

        # 一時選択値をグローバルな BotState に反映
        view.state.current_prompt_file = view.temp_persona
        view.state.current_model_name = view.temp_model
        view.state.current_respond_mode = view.temp_respond_mode

        confirmed_text = _build_status_text(
            view.temp_persona,
            view.temp_model,
            view.temp_respond_mode,
            view.ng_count,
            view.blacklist_status,
            confirmed=True,
        )

        await interaction.response.edit_message(content=confirmed_text, view=None)
        view.stop()


class ResetButton(ui.Button):
    """会話履歴リセットボタン。"""

    def __init__(self) -> None:
        super().__init__(
            label="会話リセット",
            style=discord.ButtonStyle.secondary,
            emoji="🔄",
            row=4,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: SettingsView = self.view  # type: ignore[assignment]

        channel = interaction.channel
        channel_id = getattr(channel, "id", None)
        if channel_id is None:
            await interaction.response.edit_message(
                content="エラー: チャンネルが取得できませんでした。",
                view=None,
            )
            view.stop()
            return

        # リセットポイントをこのメッセージのIDに設定
        if interaction.message is not None:
            view.state.channel_reset_points[int(channel_id)] = interaction.message.id

        await interaction.response.edit_message(
            content="🔄 会話履歴をリセットしました。ここから新しい会話を始めます。",
            view=None,
        )
        view.stop()


class SettingsView(ui.View):
    """キャラ設定パネル全体を表す View。

    3つのセレクトメニューと確定/戻るボタンを保持し、一時選択値を管理する。
    Google検索トグルは機能パネル（ハブ）側にある。
    """

    def __init__(
        self,
        state: BotState,
        ng_count: int,
        blacklist_status: str,
        *,
        make_hub: HubFactory | None = None,
        timeout: float = 120.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.state = state
        self.ng_count = ng_count
        self.blacklist_status = blacklist_status
        # 「戻る」ボタンが機能パネルを再生成するための生成関数
        self.make_hub = make_hub
        # タイムアウト時に UI を取り除くためのパネルメッセージ参照（送信後に設定される）
        self.message: discord.Message | None = None

        # 一時選択値（現在の設定を初期値として保存）
        self.temp_persona: str = state.current_prompt_file
        self.temp_model: str = state.current_model_name
        self.temp_respond_mode: str = state.current_respond_mode

        # UIパーツを View に追加
        self._add_all_items()

    def _add_all_items(self) -> None:
        """UIパーツを View に追加する。セレクト更新時にも使用する。

        セレクト類（row0〜2）、確定/戻る（row3）、会話リセット（row4）の順に並べる。
        """
        self.add_item(PersonaSelect(self.temp_persona))
        self.add_item(ModelSelect(self.temp_model))
        self.add_item(RespondModeSelect(self.temp_respond_mode))
        self.add_item(ConfirmButton())
        self.add_item(BackButton(row=3))
        self.add_item(ResetButton())

    def build_preview(self) -> str:
        """現在の一時選択値からプレビュー用テキストを組み立てる。"""
        return _build_status_text(
            self.temp_persona,
            self.temp_model,
            self.temp_respond_mode,
            self.ng_count,
            self.blacklist_status,
        )

    async def on_timeout(self) -> None:
        """タイムアウト時に、操作できなくなった UI をメッセージから取り除く。"""
        if self.message is None:
            return
        try:
            await self.message.edit(
                content="⏱️ キャラ設定パネルは時間切れになりました。設定は変更されていません。再度 /functions を実行してください。",
                view=None,
            )
        except discord.HTTPException as error:
            print(f"キャラ設定パネルのタイムアウト処理エラー: {error}")
