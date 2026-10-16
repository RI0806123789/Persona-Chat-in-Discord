import discord
from discord import ui

from persona_bot.core.app_state import GuildSettings
from persona_bot.views.auto_topic_view import AutoTopicView
from persona_bot.services.blacklist_service import BlacklistService
from persona_bot.services.content_moderator import ContentModerator
from persona_bot.config.constants import DISCORD_SELECT_LABEL_MAX_LENGTH, DISCORD_SELECT_MAX_OPTIONS
from persona_bot.utils.discord_helpers import is_administrator
from persona_bot.services.gemini_service import GeminiService
from persona_bot.views.help_view import HelpView, resolve_bot_name
from persona_bot.services.ng_word_service import NgWordFilter
from persona_bot.services.reminder_service import ReminderService
from persona_bot.views.reminder_view import ReminderView
from persona_bot.views.settings_view import SettingsView
from persona_bot.views.stats_privacy_view import StatsPrivacyView
from persona_bot.services.stats_service import StatsService
from persona_bot.views.stats_view import StatsView
from persona_bot.views.summary_view import SummaryView
from persona_bot.utils.usage_graph import UsageTracker
from persona_bot.views.view_parts import BackButton, HubFactory, PanelView, send_graph_ephemeral, swap_panel


class OpenSettingsButton(ui.Button):
    """キャラ設定パネル（SettingsView）を開くボタン。"""

    def __init__(self) -> None:
        super().__init__(label="キャラ設定", style=discord.ButtonStyle.primary, emoji="⚙️", row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        hub: "FunctionsView" = self.view  # type: ignore[assignment]

        blocked_users = hub.blacklist.get_all_blocked_users()
        blacklist_status = f"{len(blocked_users)}人" if blocked_users else "なし"
        settings_view = SettingsView(
            hub.guild_settings,
            ng_count=hub.ng_filter.word_count,
            blacklist_status=blacklist_status,
            make_hub=hub.clone,
        )
        # 同じ（エフェメラルな）メッセージをキャラ設定パネルに差し替える
        await swap_panel(interaction, hub, settings_view, settings_view.build_preview())


class OpenSummaryButton(ui.Button):
    """会話要約パネル（SummaryView）を開くボタン。"""

    def __init__(self) -> None:
        super().__init__(label="会話要約", style=discord.ButtonStyle.success, emoji="📋", row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        hub: "FunctionsView" = self.view  # type: ignore[assignment]

        summary_view = SummaryView(
            hub.guild_settings,
            hub.gemini,
            hub.moderator,
            hub.ng_filter,
            hub.usage,
            make_hub=hub.clone,
        )
        # 同じ（エフェメラルな）メッセージを要約パネルに差し替える
        await swap_panel(interaction, hub, summary_view, summary_view.build_preview())


class GraphButton(ui.Button):
    """利用トークングラフを表示するボタン（その場でエフェメラルに表示）。"""

    def __init__(self) -> None:
        super().__init__(label="グラフ", style=discord.ButtonStyle.secondary, emoji="📊", row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        hub: "FunctionsView" = self.view  # type: ignore[assignment]

        if not hub.usage.has_data():
            await interaction.response.send_message("まだ利用データがありません。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        buffer = None
        try:
            buffer = await hub.usage.build_graph_buffer()
        except Exception as error:
            print(f"グラフ生成中にエラー: {error}")
            await interaction.followup.send("エラー: グラフの生成に失敗しました。", ephemeral=True)
            return
        # ハブは残したまま、グラフを新しいエフェメラルメッセージとして送る。
        await send_graph_ephemeral(
            interaction, buffer, "usage_graph.png", "📊 これまでの利用結果のグラフです。",
            followup=True, error_log="グラフ送信中に予期せぬエラー",
            error_notice="エラー: グラフの送信に失敗しました。",
        )


class GroundingToggleButton(ui.Button):
    """Google検索グラウンディングON/OFFトグルボタン。

    確定を待たず、押した瞬間にそのサーバーの GuildSettings.grounding_enabled を変更して保存する。
    """

    def __init__(self, enabled: bool) -> None:
        label = "🔍 Google検索: ON" if enabled else "🔍 Google検索: OFF"
        style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary
        super().__init__(label=label, style=style, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        hub: "FunctionsView" = self.view  # type: ignore[assignment]
        hub.guild_settings.grounding_enabled = not hub.guild_settings.grounding_enabled
        # このトグルは確定操作が無く押した瞬間に反映されるため、その場で保存する
        await hub.guild_settings.persist()

        # ボタンの表示（ON/OFF）を更新するために View を再構築
        hub.refresh_items()
        await interaction.response.edit_message(content=hub.build_preview(), view=hub)


class NgReloadButton(ui.Button):
    """NGワードリストを再読み込みするボタン。"""

    def __init__(self) -> None:
        super().__init__(label="NGワード再読込", style=discord.ButtonStyle.secondary, emoji="🔄", row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        hub: "FunctionsView" = self.view  # type: ignore[assignment]
        hub.ng_filter.reload()
        # ハブは残したまま、結果を新しいエフェメラルメッセージとして送る
        await interaction.response.send_message(
            f"✅ NGワードリストを再読み込みしました。（{hub.ng_filter.word_count}件）", ephemeral=True
        )


class StatsButton(ui.Button):
    """会話統計パネル（StatsView）を開くボタン。

    一般ユーザーは自分の統計のみ、管理者は他メンバーの統計・全体比較も閲覧できる。
    """

    def __init__(self) -> None:
        super().__init__(label="会話統計", style=discord.ButtonStyle.secondary, emoji="📈", row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        hub: "FunctionsView" = self.view  # type: ignore[assignment]

        if not hub.stats.has_data():
            await interaction.response.send_message("まだ会話統計データがありません。", ephemeral=True)
            return

        member = interaction.user
        is_admin = is_administrator(member)
        stats_view = StatsView(hub.stats, viewer_id=str(member.id), is_admin=is_admin, make_hub=hub.clone)
        # 同じ（エフェメラルな）メッセージを統計パネルに差し替える
        await swap_panel(interaction, hub, stats_view, stats_view.build_preview())


class AutoTopicButton(ui.Button):
    """自発会話パネル（AutoTopicView）を開くボタン。

    「ボットが自分から話し出す」系の設定はすべてこのパネルにまとめている
    （テキストへの話題提供＝自発的な会話、ボイス/ステージでの配信＝自律行動）。
    入口を分けると、どちらを切ったのか分からなくなるため統一している。
    """

    def __init__(self) -> None:
        super().__init__(label="自発会話", style=discord.ButtonStyle.primary, emoji="💬", row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        hub: "FunctionsView" = self.view  # type: ignore[assignment]

        auto_topic_view = AutoTopicView(hub.guild_settings, make_hub=hub.clone)
        # 同じ（エフェメラルな）メッセージを自発会話パネルに差し替える
        await swap_panel(interaction, hub, auto_topic_view, auto_topic_view.build_preview())


class ReminderButton(ui.Button):
    """リマインダーパネル（ReminderView）を開くボタン。

    表示するのは押した本人のリマインダーだけ（内容に予定などの私的情報が入るため）。
    """

    def __init__(self) -> None:
        super().__init__(label="リマインダー", style=discord.ButtonStyle.primary, emoji="⏰", row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        hub: "FunctionsView" = self.view  # type: ignore[assignment]

        viewer_id = str(interaction.user.id)
        records = await hub.reminders.list_pending(viewer_id)
        reminder_view = ReminderView(hub.reminders, records, viewer_id, make_hub=hub.clone)
        # 同じ（エフェメラルな）メッセージをリマインダーパネルに差し替える
        await swap_panel(interaction, hub, reminder_view, reminder_view.build_preview())


class HelpButton(ui.Button):
    """使い方パネル（HelpView）を開くボタン。

    メンションで会話できることや各機能の入口は、招待しただけでは気づけない。
    その説明をまとめた読み物パネルへの入口で、権限による制限は設けない
    （使い方は全員が見てよい情報のため）。
    """

    def __init__(self) -> None:
        super().__init__(label="使い方", style=discord.ButtonStyle.secondary, emoji="❓", row=3)

    async def callback(self, interaction: discord.Interaction) -> None:
        hub: "FunctionsView" = self.view  # type: ignore[assignment]

        # ボット名はハードコードせず、押された時点のクライアントから解決する
        # （別名のボットで同じコードを動かしてもメンション例が正しくなるように）。
        help_view = HelpView(
            bot_name=resolve_bot_name(interaction.client.user),
            make_hub=hub.clone,
        )
        # 同じ（エフェメラルな）メッセージを使い方パネルに差し替える
        await swap_panel(interaction, hub, help_view, help_view.build_preview())


class StatsPrivacyButton(ui.Button):
    """統計プライバシーパネル（StatsPrivacyView）を開くボタン。

    会話統計・性格推定の収集を本人が停止したり、蓄積データを削除したりする入口。
    対象は常に押した本人の分だけで、他人の設定は見ることも変えることもできない。
    """

    def __init__(self) -> None:
        super().__init__(label="統計プライバシー", style=discord.ButtonStyle.secondary, emoji="🔒", row=4)

    async def callback(self, interaction: discord.Interaction) -> None:
        hub: "FunctionsView" = self.view  # type: ignore[assignment]

        privacy_view = StatsPrivacyView(
            hub.stats,
            viewer_id=str(interaction.user.id),
            viewer_name=interaction.user.display_name,
            make_hub=hub.clone,
        )
        # 同じ（エフェメラルな）メッセージを統計プライバシーパネルに差し替える
        await swap_panel(interaction, hub, privacy_view, privacy_view.build_preview())


class UnblockButton(ui.Button):
    """ブロック解除パネル（UnblockView）を開くボタン（管理者専用）。"""

    def __init__(self) -> None:
        super().__init__(label="ブロック解除", style=discord.ButtonStyle.danger, emoji="🚫", row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        hub: "FunctionsView" = self.view  # type: ignore[assignment]

        if not is_administrator(interaction.user):
            await interaction.response.send_message(
                "この機能はサーバー管理者のみ利用できます。", ephemeral=True
            )
            return

        blocked_users = hub.blacklist.get_all_blocked_users()
        if not blocked_users:
            await interaction.response.send_message(
                "現在ブラックリストに登録されているユーザーはいません。", ephemeral=True
            )
            return

        unblock_view = UnblockView(hub.blacklist, blocked_users, make_hub=hub.clone)
        # 同じ（エフェメラルな）メッセージをブロック解除パネルに差し替える
        await swap_panel(
            interaction, hub, unblock_view, "🚫 ブロックを解除するユーザーを選択してください。"
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
                    label=name[:DISCORD_SELECT_LABEL_MAX_LENGTH],
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
        user_id = int(self.values[0])
        # 選択肢のラベルから表示名を拾う（通知メッセージ用）
        name = next((o.label for o in self.options if o.value == self.values[0]), str(user_id))
        await interaction.response.defer()
        if await view.blacklist.unblock(user_id):
            await interaction.edit_original_response(content=f"✅ {name} のブロックを解除しました。", view=None)
        else:
            await interaction.edit_original_response(
                content=f"{name} はブラックリストに登録されていませんでした。", view=None
            )
        view.stop()


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


class FunctionsView(PanelView):
    """各種機能への入口となるハブパネル。

    Discord の View は最大5行までのため、キャラ設定パネルと要約パネルを1枚に合体できない。
    そこで、ボタンで選んだ機能のパネルに同じメッセージを差し替える方式で統合する。
    パネル自体はエフェメラル（本人だけに見える）で表示される想定。
    """

    def __init__(
        self,
        guild_settings: GuildSettings,
        usage: UsageTracker,
        ng_filter: NgWordFilter,
        blacklist: BlacklistService,
        gemini: GeminiService,
        moderator: ContentModerator,
        stats: StatsService,
        reminders: ReminderService,
        *,
        is_admin: bool = False,
    ) -> None:
        super().__init__(
            timeout_notice=(
                "⏱️ 機能パネルは時間切れになりました。"
                "再度 /functions を実行してください。"
            )
        )
        # このパネルが開かれたサーバー（DM の場合は全DM共有スロット）専用の設定。
        self.guild_settings = guild_settings
        self.usage = usage
        self.ng_filter = ng_filter
        self.blacklist = blacklist
        self.gemini = gemini
        self.blocked_user_check = blacklist.is_blocked
        self.moderator = moderator
        self.stats = stats
        self.reminders = reminders
        self.is_admin = is_admin

        self._add_all_items()

    def _add_all_items(self) -> None:
        """UIパーツを View に追加する。Google検索トグル更新時にも使用する。"""
        # row0: パネルを開く系（キャラ設定 / 会話要約）＋ その場で実行するグラフ
        self.add_item(OpenSettingsButton())
        self.add_item(OpenSummaryButton())
        self.add_item(GraphButton())
        # row1: その場で実行する系（NG再読込）＋ パネルを開く会話統計・自発会話
        self.add_item(NgReloadButton())
        self.add_item(StatsButton())
        self.add_item(AutoTopicButton())
        # ブロック解除は管理者専用。管理者以外にはボタン自体を表示しない。
        if self.is_admin:
            self.add_item(UnblockButton())
        # row2: パネルを開くリマインダー ＋ Google検索のその場トグル
        self.add_item(ReminderButton())
        self.add_item(GroundingToggleButton(self.guild_settings.grounding_enabled))
        # row3: 使い方（各機能の説明。オンボーディングの入口）
        self.add_item(HelpButton())
        # row4: 統計プライバシー（本人の統計収集停止・データ削除）
        self.add_item(StatsPrivacyButton())

    def clone(self) -> "FunctionsView":
        """各サブパネルの「戻る」用に、同じ依存関係で新しいハブパネルを作る。"""
        return FunctionsView(
            self.guild_settings,
            self.usage,
            self.ng_filter,
            self.blacklist,
            self.gemini,
            self.moderator,
            self.stats,
            self.reminders,
            is_admin=self.is_admin,
        )

    def build_preview(self) -> str:
        """ハブパネルの表示テキスト。管理者のみブロック解除の案内を出す。"""
        grounding_display = "✅ 有効" if self.guild_settings.grounding_enabled else "❌ 無効"
        lines = [
            "🛠️ **機能パネル** — 使いたい機能を選んでください。",
            "・⚙️ キャラ設定 / 📋 会話要約 / 📈 会話統計 / 💬 自発会話 / ⏰ リマインダー … パネルを開く",
            "　└ 💬 自発会話 … テキストへの話題提供と、🎙️ 自律行動"
            "（ボットが自分からボイス/ステージへ出て配信する）の ON/OFF",
            "・📊 グラフ / 🔄 NGワード再読込 … その場で実行",
            f"・🔍 Google検索: {grounding_display} … ボタンでその場で切替",
            "・❓ 使い方 … 会話のしかたと各機能の説明を表示",
            "・🔒 統計プライバシー … 会話統計の収集停止・データ削除（あなた自身の分だけ）",
        ]
        if self.is_admin:
            lines.append("・🚫 ブロック解除 … 管理者専用")
        return "\n".join(lines)
