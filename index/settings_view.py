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


def _build_status_text(
    persona_file: str,
    model_name: str,
    respond_mode: str,
    ng_count: int,
    blacklist_status: str,
    grounding_enabled: bool,
    *,
    confirmed: bool = False,
) -> str:
    """設定パネルのステータス表示テキストを組み立てる。"""
    persona_display = PERSONA_DISPLAY_NAMES.get(persona_file, f"不明 ({persona_file})")
    model_display = MODEL_DISPLAY_NAMES.get(model_name, f"不明 ({model_name})")
    mode_display = RESPOND_MODE_DISPLAY_NAMES.get(respond_mode, f"不明 ({respond_mode})")
    grounding_display = "✅ 有効" if grounding_enabled else "❌ 無効"

    if confirmed:
        header = "✅ 設定を更新しました。"
    else:
        header = "⚙️ **設定パネル** — 変更したい項目を選択してください。"

    return (
        f"{header}\n"
        f"・**ペルソナ**: {persona_display}\n"
        f"・**モデル**: {model_display}\n"
        f"・**応答モード**: {mode_display}\n"
        f"・**Google検索**: {grounding_display}\n"
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
        view.state.grounding_enabled = view.temp_grounding

        confirmed_text = _build_status_text(
            view.temp_persona,
            view.temp_model,
            view.temp_respond_mode,
            view.ng_count,
            view.blacklist_status,
            view.temp_grounding,
            confirmed=True,
        )

        await interaction.response.edit_message(content=confirmed_text, view=None)
        view.stop()


class CancelButton(ui.Button):
    """キャンセルボタン。"""

    def __init__(self) -> None:
        super().__init__(
            label="キャンセル",
            style=discord.ButtonStyle.danger,
            row=3,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: SettingsView = self.view  # type: ignore[assignment]

        # パネルのメッセージを完全に削除
        if interaction.message is not None:
            await interaction.message.delete()
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


class GroundingToggleButton(ui.Button):
    """Google検索グラウンディングON/OFFトグルボタン。"""

    def __init__(self, enabled: bool) -> None:
        label = "🔍 Google検索: ON" if enabled else "🔍 Google検索: OFF"
        style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary
        super().__init__(
            label=label,
            style=style,
            row=4,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: SettingsView = self.view  # type: ignore[assignment]
        view.temp_grounding = not view.temp_grounding

        # ボタンの表示を更新するためにViewを再構築
        view.clear_items()
        view._add_all_items()
        await interaction.response.edit_message(content=view.build_preview(), view=view)


class SettingsView(ui.View):
    """設定パネル全体を表す View。

    3つのセレクトメニューと確定/キャンセルボタンを保持し、
    一時選択値を管理する。
    """

    def __init__(
        self,
        state: BotState,
        ng_count: int,
        blacklist_status: str,
        *,
        timeout: float = 120.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.state = state
        self.ng_count = ng_count
        self.blacklist_status = blacklist_status

        # 一時選択値（現在の設定を初期値として保存）
        self.temp_persona: str = state.current_prompt_file
        self.temp_model: str = state.current_model_name
        self.temp_respond_mode: str = state.current_respond_mode
        self.temp_grounding: bool = state.grounding_enabled

        # UIパーツを View に追加
        self._add_all_items()

    def _add_all_items(self) -> None:
        """UIパーツを View に追加する。トグル更新時にも使用する。"""
        self.add_item(PersonaSelect(self.temp_persona))
        self.add_item(ModelSelect(self.temp_model))
        self.add_item(RespondModeSelect(self.temp_respond_mode))
        self.add_item(ConfirmButton())
        self.add_item(CancelButton())
        self.add_item(GroundingToggleButton(self.temp_grounding))
        self.add_item(ResetButton())

    def build_preview(self) -> str:
        """現在の一時選択値からプレビュー用テキストを組み立てる。"""
        return _build_status_text(
            self.temp_persona,
            self.temp_model,
            self.temp_respond_mode,
            self.ng_count,
            self.blacklist_status,
            self.temp_grounding,
        )

    async def on_timeout(self) -> None:
        """タイムアウト時にUIを無効化する。"""
        self.stop()
