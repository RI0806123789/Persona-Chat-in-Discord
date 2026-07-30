import asyncio

import discord
from discord import app_commands

from app_state import BotState
from blacklist_service import BlacklistService
from content_moderator import ContentModerator
from functions_view import FunctionsView
from gemini_service import GeminiService
from ng_word_service import NgWordFilter
from reminder_service import ReminderService
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
    reminders: ReminderService,
) -> None:

    @tree.command(name="functions", description="各種機能パネルを開きます（キャラ設定 / 会話要約 / 会話統計 / 自発会話 / リマインダー）。")
    async def functions(interaction: discord.Interaction) -> None:
        # ブロック解除ボタンの表示可否を決めるため、実行者が管理者かどうかを判定する
        member = interaction.user
        is_admin = isinstance(member, discord.Member) and member.guild_permissions.administrator
        # ペルソナ・モデル等はサーバー（ギルド）ごとに独立している。DM では guild_id が
        # 無いため、全DM共有の設定スロット（state.dm_settings）が使われる。
        guild_settings = state.get_guild(interaction.guild_id)
        view = FunctionsView(
            guild_settings, usage, ng_filter, blacklist, gemini, moderator, stats, reminders, is_admin=is_admin
        )
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
        guild = interaction.guild

        # 接続を試す前に権限を確かめる。権限不足は接続失敗の最も多い原因だが、
        # connect() の Forbidden として出るため、後段でまとめて拾うと
        # 「接続に失敗しました」としか伝えられず原因にたどり着けない。
        bot_member = guild.me if guild is not None else None
        if bot_member is not None:
            permissions = voice_channel.permissions_for(bot_member)
            if not permissions.connect:
                await interaction.response.send_message(
                    f"⚠️ {voice_channel.name} に接続する権限がボットにありません。\n"
                    "サーバー設定 →「連携サービス」か、そのボイスチャンネルの権限設定で、"
                    "ボットのロールに「接続」を許可してください"
                    "（チャンネル個別の権限で拒否されている場合もあります）。"
                )
                return

        await interaction.response.defer()
        if guild is not None and guild.voice_client is not None:
            await guild.voice_client.disconnect(force=True)

        # 例外の型ごとに原因を伝える。ここをまとめて握りつぶすと、利用者からは
        # 権限・ネットワーク・接続残りのどれなのか判別できなくなる。
        try:
            await voice_channel.connect()
        except discord.Forbidden:
            print(f"接続エラー (権限不足): {voice_channel.name}")
            await interaction.followup.send(
                f"⚠️ {voice_channel.name} への接続を拒否されました（権限不足）。"
                "チャンネルの権限設定でボットに「接続」を許可してください。"
            )
        except asyncio.TimeoutError:
            print(f"接続エラー (タイムアウト): {voice_channel.name}")
            await interaction.followup.send(
                "⚠️ 接続がタイムアウトしました。ボイス通信に必要な UDP が"
                "ネットワーク側（ファイアウォール・学内ネットワークなど）で遮断されている可能性があります。"
            )
        except discord.ClientException as error:
            # 例: 直前の接続が残っている（Already connected to a voice channel.）
            print(f"接続エラー (状態不整合): {error}")
            await interaction.followup.send(
                f"⚠️ 接続できませんでした（{error}）。"
                "しばらく待つか、ボットを再起動してからもう一度お試しください。"
            )
        except Exception as error:
            print(f"接続エラー ({type(error).__name__}): {error}")
            await interaction.followup.send(f"⚠️ 接続に失敗しました（{type(error).__name__}: {error}）。")
        else:
            await interaction.followup.send(f"{voice_channel.name} に接続しました。")

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

