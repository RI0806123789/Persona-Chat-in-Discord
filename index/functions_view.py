import discord
from discord import ui

from app_state import BotState
from auto_topic_view import AutoTopicView
from blacklist_service import BlacklistService
from content_moderator import ContentModerator
from gemini_service import GeminiService
from ng_word_service import NgWordFilter
from settings_view import SettingsView
from stats_service import StatsService
from stats_view import StatsView
from summary_view import SummaryView
from usage_graph import UsageTracker
from view_parts import BackButton, HubFactory


class OpenSettingsButton(ui.Button):
    """キャラ設定パネル（SettingsView）を開くボタン。"""

    def __init__(self) -> None:
        super().__init__(label="キャラ設定", style=discord.ButtonStyle.primary, emoji="⚙️", row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        hub: "FunctionsView" = self.view  # type: ignore[assignment]

        blocked_users = hub.blacklist.get_all_blocked_users()
        blacklist_status = f"{len(blocked_users)}人" if blocked_users else "なし"
        settings_view = SettingsView(
            hub.state,
            ng_count=hub.ng_filter.word_count,
            blacklist_status=blacklist_status,
            make_hub=hub.clone,
        )
        # 同じ（エフェメラルな）メッセージをキャラ設定パネルに差し替える
        await interaction.response.edit_message(content=settings_view.build_preview(), view=settings_view)
        settings_view.message = interaction.message
        # ハブは役目を終えたので停止（タイムアウトでの誤上書きを防ぐ）
        hub.stop()


class OpenSummaryButton(ui.Button):
    """会話要約パネル（SummaryView）を開くボタン。"""

    def __init__(self) -> None:
        super().__init__(label="会話要約", style=discord.ButtonStyle.success, emoji="📋", row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        hub: "FunctionsView" = self.view  # type: ignore[assignment]

        summary_view = SummaryView(
            hub.state,
            hub.gemini,
            hub.moderator,
            hub.ng_filter,
            hub.usage,
            make_hub=hub.clone,
        )
        # 同じ（エフェメラルな）メッセージを要約パネルに差し替える
        await interaction.response.edit_message(content=summary_view.build_preview(), view=summary_view)
        summary_view.message = interaction.message
        # ハブは役目を終えたので停止（タイムアウトでの誤上書きを防ぐ）
        hub.stop()


class GraphButton(ui.Button):
    """利用トークングラフを表示するボタン（その場でエフェメラルに表示）。"""

    def __init__(self) -> None:
        super().__init__(label="グラフ", style=discord.ButtonStyle.secondary, emoji="📊", row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        hub: "FunctionsView" = self.view  # type: ignore[assignment]

        if not hub.usage.has_data():
            await interaction.response.send_message("まだ利用データがありません。", ephemeral=True)
            return

        buffer = hub.usage.build_graph_buffer()
        try:
            picture = discord.File(buffer, filename="usage_graph.png")
            # ハブは残したまま、グラフを新しいエフェメラルメッセージとして送る
            await interaction.response.send_message(
                "📊 これまでの利用結果のグラフです。", file=picture, ephemeral=True
            )
        except Exception as error:
            print(f"グラフ送信中に予期せぬエラー: {error}")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    f"エラー: グラフの送信に失敗しました。（{error}）", ephemeral=True
                )
        finally:
            buffer.close()


class GroundingToggleButton(ui.Button):
    """Google検索グラウンディングON/OFFトグルボタン。

    ハブは「その場で実行」系のため、確定を待たず押した瞬間に BotState へ反映する。
    """

    def __init__(self, enabled: bool) -> None:
        label = "🔍 Google検索: ON" if enabled else "🔍 Google検索: OFF"
        style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary
        super().__init__(label=label, style=style, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        hub: "FunctionsView" = self.view  # type: ignore[assignment]
        hub.state.grounding_enabled = not hub.state.grounding_enabled

        # ボタンの表示（ON/OFF）を更新するために View を再構築
        hub.clear_items()
        hub._add_all_items()
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
        is_admin = isinstance(member, discord.Member) and member.guild_permissions.administrator
        stats_view = StatsView(hub.stats, viewer_id=str(member.id), is_admin=is_admin, make_hub=hub.clone)
        # 同じ（エフェメラルな）メッセージを統計パネルに差し替える
        await interaction.response.edit_message(content=stats_view.build_preview(), view=stats_view)
        stats_view.message = interaction.message
        # ハブは役目を終えたので停止（タイムアウトでの誤上書きを防ぐ）
        hub.stop()


class AutoTopicButton(ui.Button):
    """自発会話パネル（AutoTopicView）を開くボタン。"""

    def __init__(self) -> None:
        super().__init__(label="自発会話", style=discord.ButtonStyle.primary, emoji="💬", row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        hub: "FunctionsView" = self.view  # type: ignore[assignment]

        auto_topic_view = AutoTopicView(hub.state, make_hub=hub.clone)
        # 同じ（エフェメラルな）メッセージを自発会話パネルに差し替える
        await interaction.response.edit_message(content=auto_topic_view.build_preview(), view=auto_topic_view)
        auto_topic_view.message = interaction.message
        # ハブは役目を終えたので停止（タイムアウトでの誤上書きを防ぐ）
        hub.stop()


class UnblockButton(ui.Button):
    """ブロック解除パネル（UnblockView）を開くボタン（管理者専用）。"""

    def __init__(self) -> None:
        super().__init__(label="ブロック解除", style=discord.ButtonStyle.danger, emoji="🚫", row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        hub: "FunctionsView" = self.view  # type: ignore[assignment]

        member = interaction.user
        is_admin = isinstance(member, discord.Member) and member.guild_permissions.administrator
        if not is_admin:
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
        await interaction.response.edit_message(
            content="🚫 ブロックを解除するユーザーを選択してください。", view=unblock_view
        )
        unblock_view.message = interaction.message
        hub.stop()


class UnblockSelect(ui.Select):
    """ブロック中ユーザーから解除対象を選ぶセレクトメニュー。"""

    def __init__(self, blocked_users: list) -> None:
        options = []
        # Discord のセレクトは最大25件まで
        for record in blocked_users[:25]:
            user_id = record["user_id"]
            name = record.get("user_name") or str(user_id)
            count = record.get("count", "?")
            options.append(
                discord.SelectOption(
                    label=name[:100],
                    value=str(user_id),
                    description=f"違反 {count} 回",
                )
            )
        super().__init__(placeholder="解除するユーザーを選択…", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "UnblockView" = self.view  # type: ignore[assignment]
        user_id = int(self.values[0])
        # 選択肢のラベルから表示名を拾う（通知メッセージ用）
        name = next((o.label for o in self.options if o.value == self.values[0]), str(user_id))
        if view.blacklist.unblock(user_id):
            await interaction.response.edit_message(content=f"✅ {name} のブロックを解除しました。", view=None)
        else:
            await interaction.response.edit_message(
                content=f"{name} はブラックリストに登録されていませんでした。", view=None
            )
        view.stop()


class UnblockView(ui.View):
    """ブロック解除用のパネル。ブロック中ユーザーの選択メニューを持つ。"""

    def __init__(
        self,
        blacklist: BlacklistService,
        blocked_users: list,
        *,
        make_hub: HubFactory | None = None,
        timeout: float = 120.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.blacklist = blacklist
        # 「戻る」ボタンが機能パネルを再生成するための生成関数
        self.make_hub = make_hub
        self.message: discord.Message | None = None
        self.add_item(UnblockSelect(blocked_users))
        self.add_item(BackButton(row=1))

    async def on_timeout(self) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(
                content="⏱️ ブロック解除パネルは時間切れになりました。再度 /functions を実行してください。",
                view=None,
            )
        except discord.HTTPException as error:
            print(f"ブロック解除パネルのタイムアウト処理エラー: {error}")


class FunctionsView(ui.View):
    """各種機能への入口となるハブパネル。

    Discord の View は最大5行までのため、キャラ設定パネルと要約パネルを1枚に合体できない。
    そこで、ボタンで選んだ機能のパネルに同じメッセージを差し替える方式で統合する。
    パネル自体はエフェメラル（本人だけに見える）で表示される想定。
    """

    def __init__(
        self,
        state: BotState,
        usage: UsageTracker,
        ng_filter: NgWordFilter,
        blacklist: BlacklistService,
        gemini: GeminiService,
        moderator: ContentModerator,
        stats: StatsService,
        *,
        is_admin: bool = False,
        timeout: float = 120.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.state = state
        self.usage = usage
        self.ng_filter = ng_filter
        self.blacklist = blacklist
        self.gemini = gemini
        self.moderator = moderator
        self.stats = stats
        self.is_admin = is_admin
        # タイムアウト時に UI を取り除くためのパネルメッセージ参照（送信後に設定される）
        self.message: discord.Message | None = None

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
        # row2: Google検索のその場トグル
        self.add_item(GroundingToggleButton(self.state.grounding_enabled))

    def clone(self) -> "FunctionsView":
        """各サブパネルの「戻る」用に、同じ依存関係で新しいハブパネルを作る。"""
        return FunctionsView(
            self.state,
            self.usage,
            self.ng_filter,
            self.blacklist,
            self.gemini,
            self.moderator,
            self.stats,
            is_admin=self.is_admin,
        )

    def build_preview(self) -> str:
        """ハブパネルの表示テキスト。管理者のみブロック解除の案内を出す。"""
        grounding_display = "✅ 有効" if self.state.grounding_enabled else "❌ 無効"
        lines = [
            "🛠️ **機能パネル** — 使いたい機能を選んでください。",
            "・⚙️ キャラ設定 / 📋 会話要約 / 📈 会話統計 / 💬 自発会話 … パネルを開く",
            "・📊 グラフ / 🔄 NGワード再読込 … その場で実行",
            f"・🔍 Google検索: {grounding_display} … ボタンでその場で切替",
        ]
        if self.is_admin:
            lines.append("・🚫 ブロック解除 … 管理者専用")
        return "\n".join(lines)

    async def on_timeout(self) -> None:
        """タイムアウト時に、操作できなくなった UI をメッセージから取り除く。"""
        if self.message is None:
            return
        try:
            await self.message.edit(
                content="⏱️ 機能パネルは時間切れになりました。再度 /functions を実行してください。",
                view=None,
            )
        except discord.HTTPException as error:
            print(f"機能パネルのタイムアウト処理エラー: {error}")
