import discord
from discord import ui

from persona_bot.core.app_state import GuildSettings
from persona_bot.core.background import run_in_thread
from persona_bot.config.choices import (
    MODEL_CHOICES,
    MODEL_DISPLAY_NAMES,
    RESPOND_MODE_CHOICES,
    RESPOND_MODE_DISPLAY_NAMES,
)
from persona_bot.config.constants import (
    DISCORD_SELECT_MAX_OPTIONS,
    PERSONA_SETTINGS_TEMPLATE_FILE_NAME,
    PROMPT_TEMPLATE_FILE_NAME,
)
from persona_bot.config.personas import (
    FOLLOW_DEFAULT_PERSONA, PersonaCatalog, current_persona_catalog, is_selectable_persona,
)
from persona_bot.views.view_parts import (
    begin_panel_operation, edit_panel_message, BackButton,
    HubFactory, PanelView, select_option_text,
)


def _build_status_text(
    persona_file: str,
    model_name: str,
    respond_mode: str,
    ng_count: int,
    blacklist_status: str,
    *,
    confirmed: bool = False,
    catalog: PersonaCatalog | None = None,
) -> str:
    """キャラ設定パネルのステータス表示テキストを組み立てる。"""
    catalog = current_persona_catalog() if catalog is None else catalog
    persona_display = _persona_status(catalog, persona_file)
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
        f"{_persona_notice(catalog)}"
    )


def _persona_status(catalog: PersonaCatalog, persona_file: str) -> str:
    """ペルソナの表示。未選択は既定の名前を、見つからないファイルは既定で応答中と添える。"""
    default_name = catalog.display_name(catalog.default_value)
    if persona_file == FOLLOW_DEFAULT_PERSONA:
        return f"既定（{default_name}）"
    if not catalog.contains(persona_file):
        return f"⚠️ {catalog.display_name(persona_file)}（ファイルが見つからないため、既定の{default_name}で応答中）"
    return catalog.display_name(persona_file)


def _persona_notice(catalog: PersonaCatalog) -> str:
    """ペルソナを作っていない・多すぎて全部を選択肢に出せないときの案内。"""
    if not catalog.personas:
        return (
            "\n\nℹ️ ペルソナが未登録のため、組み込みの標準プロンプトで応答しています。"
            f"`prompts/{PROMPT_TEMPLATE_FILE_NAME}` をコピーして `prompts/<名前>.txt` を作ると、"
            "このパネルを開き直したときに選べます（作り方は README の「ペルソナを作る」）。"
            f"表示名と声は `prompts/{PERSONA_SETTINGS_TEMPLATE_FILE_NAME}` を `prompts/personas.json` に"
            "コピーして決められます。"
        )
    if len(catalog.personas) > DISCORD_SELECT_MAX_OPTIONS:
        return f"\n\nℹ️ ペルソナが多いため、先頭の{DISCORD_SELECT_MAX_OPTIONS}件だけを選択肢に表示しています。"
    return ""


class PersonaSelect(ui.Select):
    """ペルソナ選択用セレクトメニュー。"""

    def __init__(self, current_value: str, catalog: PersonaCatalog) -> None:
        # 選択肢は prompts/ のファイルから作る（Discord の上限を超える分は出さない）
        options = [
            discord.SelectOption(
                label=select_option_text(persona.name, fallback=persona.value),
                value=persona.value,
                default=(persona.value == current_value),
            )
            for persona in catalog.personas[:DISCORD_SELECT_MAX_OPTIONS]
        ]
        super().__init__(
            placeholder="👤 ペルソナを選択…",
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: SettingsView = self.view  # type: ignore[assignment]
        begin_panel_operation(view, interaction)
        view.temp_persona = self.values[0]
        view.refresh_items()
        await edit_panel_message(interaction, content=view.build_preview(), view=view)


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
        begin_panel_operation(view, interaction)
        view.temp_model = self.values[0]
        view.refresh_items()
        await edit_panel_message(interaction, content=view.build_preview(), view=view)


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
        begin_panel_operation(view, interaction)
        view.temp_respond_mode = self.values[0]
        view.refresh_items()
        await edit_panel_message(interaction, content=view.build_preview(), view=view)


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
        begin_panel_operation(view, interaction)

        # ペルソナを変えたときだけ、選んだファイルが prompts/ にあるかを確かめる。
        # 変えていなければ、見つからない保存値もそのまま残す（ファイルを戻せば元に戻る）。
        await interaction.response.defer(ephemeral=True)
        persona_changed = view.temp_persona != view.guild_settings.current_prompt_file
        if persona_changed and not await run_in_thread(is_selectable_persona, view.temp_persona):
            await edit_panel_message(interaction, deferred=True,
                content=f"エラー: ファイル「{view.temp_persona}」が見つかりませんでした。設定は変更されていません。",
                view=None,
            )
            return

        # 一時選択値をこのサーバー（ギルド）専用の設定に反映し、DB へ保存する
        view.guild_settings.current_prompt_file = view.temp_persona
        view.guild_settings.current_model_name = view.temp_model
        view.guild_settings.current_respond_mode = view.temp_respond_mode
        await view.guild_settings.persist()

        confirmed_text = _build_status_text(
            view.temp_persona,
            view.temp_model,
            view.temp_respond_mode,
            view.ng_count,
            view.blacklist_status,
            confirmed=True,
            catalog=view.catalog,
        )

        await edit_panel_message(interaction, deferred=True, content=confirmed_text, view=None)


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
        begin_panel_operation(view, interaction)

        channel = interaction.channel
        channel_id = getattr(channel, "id", None)
        if channel_id is None:
            await edit_panel_message(interaction,
                content="エラー: チャンネルが取得できませんでした。",
                view=None,
            )
            return

        # パネル作成時ではなく、ボタン押下時点を境界にする。
        # Interaction ID もメッセージと同じ Snowflake なので履歴の after に使える。
        view.guild_settings.channel_reset_points[int(channel_id)] = interaction.id

        await edit_panel_message(interaction,
            content="🔄 会話履歴をリセットしました。ここから新しい会話を始めます。",
            view=None,
        )


class SettingsView(PanelView):
    """キャラ設定パネル全体を表す View。

    3つのセレクトメニューと確定/戻るボタンを保持し、一時選択値を管理する。
    Google検索トグルは機能パネル（ハブ）側にある。
    """

    def __init__(
        self,
        guild_settings: GuildSettings,
        ng_count: int,
        blacklist_status: str,
        *,
        make_hub: HubFactory | None = None,
    ) -> None:
        super().__init__(
            timeout_notice=(
                "⏱️ キャラ設定パネルは時間切れになりました。設定は変更されていません。"
                "再度 /functions を実行してください。"
            )
        )
        self.guild_settings = guild_settings
        self.ng_count = ng_count
        self.blacklist_status = blacklist_status
        # 「戻る」ボタンが機能パネルを再生成するための生成関数
        self.make_hub = make_hub

        # パネルを開いた時点のペルソナ一覧。選択肢と表示をこの一覧にそろえる
        # （開いている間に別のパネルが一覧を読み直しても、選択肢が入れ替わらないように）。
        self.catalog = current_persona_catalog()

        # 一時選択値（現在の設定を初期値として保存）
        self.temp_persona: str = guild_settings.current_prompt_file
        self.temp_model: str = guild_settings.current_model_name
        self.temp_respond_mode: str = guild_settings.current_respond_mode

        # UIパーツを View に追加
        self._add_all_items()

    def _add_all_items(self) -> None:
        """UIパーツを View に追加する。セレクト更新時にも使用する。

        セレクト類（row0〜2）、確定/戻る（row3）、会話リセット（row4）の順に並べる。
        ペルソナが1つも無いときはペルソナのセレクトを出さない（選択肢0件のセレクトは
        Discord に拒否され、パネルごと開けなくなるため）。
        """
        if self.catalog.personas:
            self.add_item(PersonaSelect(self.temp_persona, self.catalog))
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
            catalog=self.catalog,
        )
