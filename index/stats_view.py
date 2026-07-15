import discord
from discord import ui

from stats_service import StatsService
from view_parts import BackButton, HubFactory

# 項目セレクトの値 → 表示名
_ITEM_LABELS = {
    "all": "全部",
    "time": "時間帯",
    "topic": "話題",
    "personality": "性格推定",
}


class StatsItemSelect(ui.Select):
    """表示する統計項目を選ぶセレクトメニュー。"""

    def __init__(self, current: str) -> None:
        options = [
            discord.SelectOption(label=label, value=value, default=(value == current))
            for value, label in _ITEM_LABELS.items()
        ]
        super().__init__(placeholder="📊 表示する項目を選択…", options=options, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "StatsView" = self.view  # type: ignore[assignment]
        view.temp_item = self.values[0]
        view.clear_items()
        view._add_all_items()
        await interaction.response.edit_message(content=view.build_preview(), view=view)


class StatsUserSelect(ui.Select):
    """集計対象ユーザーを選ぶセレクトメニュー（管理者のみ表示）。"""

    def __init__(self, users: list[tuple[str, str, int]], current: str) -> None:
        options = []
        for user_id, name, count in users:
            options.append(
                discord.SelectOption(
                    label=name[:100],
                    value=str(user_id),
                    description=f"発言 {count} 回",
                    default=(str(user_id) == current),
                )
            )
        super().__init__(placeholder="👤 対象ユーザーを選択…", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "StatsView" = self.view  # type: ignore[assignment]
        view.temp_target_id = self.values[0]
        view.clear_items()
        view._add_all_items()
        await interaction.response.edit_message(content=view.build_preview(), view=view)


class RunStatsButton(ui.Button):
    """選択中の項目でダッシュボードを表示するボタン。"""

    def __init__(self) -> None:
        super().__init__(label="表示", style=discord.ButtonStyle.success, emoji="📈", row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "StatsView" = self.view  # type: ignore[assignment]

        # 一般ユーザーは自分のデータに固定する（他人のデータは管理者専用）。
        target_id = view.temp_target_id if view.is_admin else view.viewer_id

        # パネル（エフェメラル）を「集計中」に更新してから長時間処理に入る。
        await interaction.response.edit_message(content="🔄 集計中...", view=None)
        view.stop()

        caption, images = await view.stats.build_dashboard(target_id, view.temp_item)
        files = [discord.File(buffer, filename=filename) for filename, buffer in images]
        try:
            await interaction.edit_original_response(content=caption)
            if files:
                # グラフ画像は本人だけに見えるエフェメラルの followup として送る。
                await interaction.followup.send(files=files, ephemeral=True)
        except Exception as error:
            print(f"会話統計の送信中にエラー: {error}")
            await interaction.followup.send(f"エラー: 統計の表示に失敗しました。（{error}）", ephemeral=True)
        finally:
            for _filename, buffer in images:
                buffer.close()


class RankingButton(ui.Button):
    """発言回数ランキング（全体比較）を表示するボタン（管理者専用）。"""

    def __init__(self) -> None:
        super().__init__(label="全体比較", style=discord.ButtonStyle.secondary, emoji="📊", row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "StatsView" = self.view  # type: ignore[assignment]

        member = interaction.user
        is_admin = isinstance(member, discord.Member) and member.guild_permissions.administrator
        if not is_admin:
            await interaction.response.send_message(
                "この機能はサーバー管理者のみ利用できます。", ephemeral=True
            )
            return

        buffer = view.stats.build_ranking_graph()
        if buffer is None:
            await interaction.response.send_message(
                "ランキングを表示できるデータがありません。", ephemeral=True
            )
            return
        try:
            picture = discord.File(buffer, filename="stats_ranking.png")
            # パネルは残したまま、ランキングを新しいエフェメラルメッセージで送る。
            await interaction.response.send_message(
                "📊 発言回数ランキング（全体比較）です。", file=picture, ephemeral=True
            )
        except Exception as error:
            print(f"ランキング送信中にエラー: {error}")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    f"エラー: ランキングの送信に失敗しました。（{error}）", ephemeral=True
                )
        finally:
            buffer.close()


class StatsView(ui.View):
    """会話統計の表示パネル。

    一般ユーザーは自分の統計のみ閲覧でき、管理者はユーザーを選んで
    他メンバーの統計や全体比較も閲覧できる。パネルはエフェメラル前提。
    """

    def __init__(
        self,
        stats: StatsService,
        *,
        viewer_id: str,
        is_admin: bool,
        make_hub: HubFactory | None = None,
        timeout: float = 120.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.stats = stats
        self.viewer_id = viewer_id
        self.is_admin = is_admin
        # 「戻る」ボタンが機能パネルを再生成するための生成関数
        self.make_hub = make_hub
        self.message: discord.Message | None = None

        # 一時選択値
        self.temp_item: str = "all"
        # 対象ユーザー。一般ユーザーは自分固定、管理者は選択で変更できる。
        self.temp_target_id: str = viewer_id

        self._add_all_items()

    def _add_all_items(self) -> None:
        """UI パーツを View に追加する。選択更新時にも呼び出す。"""
        if self.is_admin:
            users = self.stats.get_user_options()
            if users:
                self.add_item(StatsUserSelect(users, self.temp_target_id))
        self.add_item(StatsItemSelect(self.temp_item))
        self.add_item(RunStatsButton())
        if self.is_admin:
            self.add_item(RankingButton())
        self.add_item(BackButton(row=2))

    def build_preview(self) -> str:
        """現在の一時選択値からパネル表示テキストを組み立てる。"""
        lines = [
            "📈 **会話統計パネル** — 項目を選んで「表示」を押してください。",
            f"・**項目**: {_ITEM_LABELS.get(self.temp_item, self.temp_item)}",
        ]
        if self.is_admin:
            lines.append(f"・**対象**: {self.stats.display_name(self.temp_target_id)} さん")
            lines.append("※ 管理者はユーザーを選んで他メンバーの統計・全体比較も閲覧できます。")
        else:
            lines.append("・**対象**: あなた自身（他メンバーの統計は管理者のみ閲覧可）")
        return "\n".join(lines)

    async def on_timeout(self) -> None:
        """タイムアウト時に、操作できなくなった UI をメッセージから取り除く。"""
        if self.message is None:
            return
        try:
            await self.message.edit(
                content="⏱️ 会話統計パネルは時間切れになりました。再度 /functions を実行してください。",
                view=None,
            )
        except discord.HTTPException as error:
            print(f"会話統計パネルのタイムアウト処理エラー: {error}")
