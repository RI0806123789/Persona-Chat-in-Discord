import discord
from discord import app_commands

from app_state import BotState
from blacklist_service import BlacklistService
from content_moderator import ContentModerator
from functions_view import FunctionsView
from gemini_service import GeminiService
from ng_word_service import NgWordFilter
from stats_service import StatsService
from usage_graph import UsageTracker


def register_commands(
    tree: app_commands.CommandTree,
    state: BotState,
    usage: UsageTracker,
    ng_filter: NgWordFilter,
    blacklist: BlacklistService,
    gemini: GeminiService,
    moderator: ContentModerator,
    stats: StatsService,
) -> None:

    @tree.command(name="functions", description="各種機能パネルを開きます（キャラ設定 / 会話要約 / 会話統計 / 自発会話）。")
    async def functions(interaction: discord.Interaction) -> None:
        # ブロック解除ボタンの表示可否を決めるため、実行者が管理者かどうかを判定する
        member = interaction.user
        is_admin = isinstance(member, discord.Member) and member.guild_permissions.administrator
        view = FunctionsView(state, usage, ng_filter, blacklist, gemini, moderator, stats, is_admin=is_admin)
        # 機能パネルは本人だけに見えるよう ephemeral で送信する
        await interaction.response.send_message(view.build_preview(), view=view, ephemeral=True)
        # タイムアウト時に UI を取り除けるよう、送信したパネルメッセージを View に持たせる
        view.message = await interaction.original_response()

    @tree.command(name="join", description="ボットがボイスチャンネルに参加します")
    async def join(interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member) or member.voice is None or member.voice.channel is None:
            await interaction.response.send_message("先にボイスチャンネルに参加してください。")
            return

        voice_channel = member.voice.channel
        await interaction.response.defer()
        if interaction.guild and interaction.guild.voice_client is not None:
            await interaction.guild.voice_client.disconnect(force=True)
        try:
            await voice_channel.connect()
            await interaction.followup.send(f"{voice_channel.name} に接続しました。")
        except Exception as error:
            print(f"接続エラー: {error}")
            await interaction.followup.send("接続に失敗しました。")

    @tree.command(name="leave", description="ボットがボイスチャンネルから退出します")
    async def leave(interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.guild.voice_client is None:
            await interaction.response.send_message("ボットはボイスチャンネルに参加していません。")
            return

        await interaction.response.defer()
        try:
            await interaction.guild.voice_client.disconnect(force=True)
            await interaction.followup.send("退出しました。")
        except Exception as error:
            print(f"切断エラー: {error}")
            await interaction.followup.send("退出に失敗しました。")

