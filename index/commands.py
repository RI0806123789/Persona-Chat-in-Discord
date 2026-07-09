import discord
from discord import app_commands

from app_state import BotState
from blacklist_service import BlacklistService
from ng_word_service import NgWordFilter
from settings_view import SettingsView
from usage_graph import UsageTracker


def register_commands(tree: app_commands.CommandTree, state: BotState, usage: UsageTracker, ng_filter: NgWordFilter, blacklist: BlacklistService) -> None:

    @tree.command(name="status", description="現在の設定を表示し、UI上で変更できます。")
    async def check_status(interaction: discord.Interaction) -> None:
        blocked_users = blacklist.get_all_blocked_users()
        blacklist_status = f"{len(blocked_users)}人" if blocked_users else "なし"

        view = SettingsView(
            state,
            ng_count=ng_filter.word_count,
            blacklist_status=blacklist_status,
        )

        await interaction.response.send_message(view.build_preview(), view=view)

    @tree.command(name="graph", description="これまでの利用結果をグラフで表示します。")
    async def show_graph(interaction: discord.Interaction) -> None:
        if not usage.has_data():
            await interaction.response.send_message("まだ利用データがありません。")
            return

        buffer = usage.build_graph_buffer()
        try:
            picture = discord.File(buffer, filename="usage_graph.png")
            await interaction.response.send_message("これまでの利用結果のグラフです。", file=picture)
        except Exception as error:
            print(f"グラフ送信中に予期せぬエラー: {error}")
            try:
                await interaction.followup.send(f"エラー: グラフの送信に失敗しました。 ({error})")
            except Exception as followup_error:
                print(f"グラフ送信エラー通知に失敗しました: {followup_error}")
        finally:
            buffer.close()

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

    @tree.command(name="ng_reload", description="NGワードリストを再読み込みします。")
    async def ng_reload(interaction: discord.Interaction) -> None:
        ng_filter.reload()
        await interaction.response.send_message(f"✅ NGワードリストを再読み込みしました。（{ng_filter.word_count}件）")

