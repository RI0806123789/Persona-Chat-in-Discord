import discord
from discord import ui

from persona_bot.config.constants import DISCORD_SELECT_LABEL_MAX_LENGTH, DISCORD_SELECT_MAX_OPTIONS
from persona_bot.utils.discord_helpers import is_administrator
from persona_bot.services.feedback_service import FeedbackService, get_default_service
from persona_bot.services.stats_service import StatsService
from persona_bot.views.view_parts import BackButton, HubFactory, PanelView, send_graph_ephemeral

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
        view.refresh_items()
        await interaction.response.edit_message(content=view.build_preview(), view=view)


class StatsUserSelect(ui.Select):
    """集計対象ユーザーを選ぶセレクトメニュー（管理者のみ表示）。"""

    def __init__(self, users: list[tuple[str, str, int]], current: str) -> None:
        options = []
        for user_id, name, count in users[:DISCORD_SELECT_MAX_OPTIONS]:
            options.append(
                discord.SelectOption(
                    label=name[:DISCORD_SELECT_LABEL_MAX_LENGTH],
                    value=str(user_id),
                    description=f"発言 {count} 回",
                    default=(str(user_id) == current),
                )
            )
        super().__init__(placeholder="👤 対象ユーザーを選択…", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "StatsView" = self.view  # type: ignore[assignment]
        if not is_administrator(interaction.user):
            await interaction.response.send_message("この機能はサーバー管理者のみ利用できます。", ephemeral=True)
            return
        view.temp_target_id = self.values[0]
        view.refresh_items()
        await interaction.response.edit_message(content=view.build_preview(), view=view)


class RunStatsButton(ui.Button):
    """選択中の項目でダッシュボードを表示するボタン。"""

    def __init__(self) -> None:
        super().__init__(label="表示", style=discord.ButtonStyle.success, emoji="📈", row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "StatsView" = self.view  # type: ignore[assignment]

        # 一般ユーザーは自分のデータに固定する（他人のデータは管理者専用）。
        target_id = view.temp_target_id if view.is_admin else view.viewer_id
        if target_id != view.viewer_id and not is_administrator(interaction.user):
            await interaction.response.send_message("この機能はサーバー管理者のみ利用できます。", ephemeral=True)
            return

        # パネル（エフェメラル）を「集計中」に更新してから長時間処理に入る。
        await interaction.response.edit_message(content="🔄 集計中...", view=None)
        view.stop()

        # viewer_id（誰が見ているか）を必ず渡す。省略すると build_dashboard が
        # 「本人が見ている」とみなし、管理者が他人の統計を開いたときに
        # 🔕 収集停止中の注記まで見せてしまう（＝相手の opt-out が露見する）。
        # 管理者のユーザー選択は収集停止中の人を除外するため通常は選べないが、
        # 「選んだ後に相手が停止した」場合はパネルが開いたままなので到達しうる。
        images = []
        files = []
        try:
            caption, images = await view.stats.build_dashboard(
                target_id, view.temp_item, viewer_id=view.viewer_id
            )
            # 描画・解析の待機中にも権限は変わりうる。送信直前にも他人のデータを守る。
            if target_id != view.viewer_id and not is_administrator(interaction.user):
                await interaction.edit_original_response(
                    content="この機能はサーバー管理者のみ利用できます。", view=None,
                )
                return
            # 途中の生成失敗でも、それまでに作成したFileをfinallyで回収する。
            for filename, buffer in images:
                files.append(discord.File(buffer, filename=filename))
            await interaction.edit_original_response(content=caption)
            if files:
                # グラフ画像は本人だけに見えるエフェメラルの followup として送る。
                await interaction.followup.send(files=files, ephemeral=True)
        except Exception as error:
            print(f"会話統計の生成・送信中にエラー: {error}")
            notice = "エラー: 統計の表示に失敗しました。"
            try:
                await interaction.edit_original_response(content=notice, view=None)
            except Exception:
                await interaction.followup.send(notice, ephemeral=True)
        finally:
            # discord.Fileが差し替えたcloseを戻してから、元のバッファを閉じる。
            for file in files:
                file.close()
            for _filename, buffer in images:
                buffer.close()


class RankingButton(ui.Button):
    """発言回数ランキング（全体比較）を表示するボタン（管理者専用）。"""

    def __init__(self) -> None:
        super().__init__(label="全体比較", style=discord.ButtonStyle.secondary, emoji="📊", row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "StatsView" = self.view  # type: ignore[assignment]

        if not is_administrator(interaction.user):
            await interaction.response.send_message(
                "この機能はサーバー管理者のみ利用できます。", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        buffer = None
        try:
            buffer = await view.stats.build_ranking_graph()
        except Exception as error:
            print(f"グラフ生成中にエラー: {error}")
            await interaction.followup.send("エラー: グラフの生成に失敗しました。", ephemeral=True)
            return
        if buffer is None:
            await interaction.followup.send(
                "ランキングを表示できるデータがありません。", ephemeral=True
            )
            return
        # パネルは残したまま、ランキングを新しいエフェメラルメッセージで送る。
        await send_graph_ephemeral(
            interaction, buffer, "stats_ranking.png", "📊 発言回数ランキング（全体比較）です。",
            followup=True, error_log="ランキング送信中にエラー",
            error_notice="エラー: ランキングの送信に失敗しました。",
        )


class FeedbackButton(ui.Button):
    """ボット返信への 👍/👎 の集計を表示するボタン（管理者専用）。

    「どのモデル・どのペルソナの返信が good だったか」を見るためのもので、
    会話品質の分析という点で会話統計パネルの目的と一致するためここへ置く。
    """

    def __init__(self) -> None:
        super().__init__(label="フィードバック", style=discord.ButtonStyle.secondary, emoji="👍", row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "StatsView" = self.view  # type: ignore[assignment]

        # ボタンを出す条件（管理者のみ）とは別に、押された時点でも再判定する
        # （RankingButton と同じ二重チェック。権限はサーバー側の値で判断する）。
        if not is_administrator(interaction.user):
            await interaction.response.send_message(
                "この機能はサーバー管理者のみ利用できます。", ephemeral=True
            )
            return

        # 集計は「そのサーバー分だけ」に限定する。DM 分（guild_id=0）は誰にも見せない。
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "フィードバックの集計はサーバー内でのみ確認できます。", ephemeral=True
            )
            return

        feedback = view.feedback
        if feedback is None:
            await interaction.response.send_message(
                "フィードバック機能が利用できません。", ephemeral=True
            )
            return

        # パネルは残したまま、集計を新しいエフェメラルメッセージとして送る。
        summary = await feedback.build_summary_text(interaction.guild_id)
        await interaction.response.send_message(summary, ephemeral=True)


class StatsView(PanelView):
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
        feedback: FeedbackService | None = None,
    ) -> None:
        super().__init__(
            timeout_notice=(
                "⏱️ 会話統計パネルは時間切れになりました。"
                "再度 /functions を実行してください。"
            )
        )
        self.stats = stats
        self.viewer_id = viewer_id
        self.is_admin = is_admin
        # 👍 フィードバック集計に使うサービス。生成元（機能ハブ）から渡されなかった
        # 場合は bot.py が起動時に登録した既定インスタンスを使う。
        self.feedback = feedback if feedback is not None else get_default_service()
        # 「戻る」ボタンが機能パネルを再生成するための生成関数
        self.make_hub = make_hub

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
            # フィードバック集計も管理者専用。一般ユーザーにはボタン自体を出さない。
            if self.feedback is not None:
                self.add_item(FeedbackButton())
        # 「戻る」は他のパネルと同じく行の最後に置く。
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
            if self.feedback is not None:
                lines.append("※ 👍 フィードバック … 返信への 👍/👎 の集計（このサーバー分）を表示します。")
        else:
            lines.append("・**対象**: あなた自身（他メンバーの統計は管理者のみ閲覧可）")
        return "\n".join(lines)
