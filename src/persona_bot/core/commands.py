import asyncio
from functools import wraps
from typing import Callable

import discord
from discord import app_commands

from persona_bot.core.app_state import BotState
from persona_bot.core.background import TaskOwner, wait_for_cleanup
from persona_bot.core.voice_commands import VoiceCommandCoordinator, skip_superseded_voice_operation
from persona_bot.config.constants import VOICE_CONNECT_TIMEOUT_SECONDS
from persona_bot.services.autonomous_service import AutonomousService
from persona_bot.services.autonomous_stage import enable_stage_speaker
from persona_bot.services.blacklist_service import BlacklistService
from persona_bot.services.content_moderator import ContentModerator
from persona_bot.utils.discord_helpers import is_administrator, reject_blocked_interaction
from persona_bot.views.functions_view import FunctionsView
from persona_bot.services.gemini_service import GeminiService
from persona_bot.services.ng_word_service import NgWordFilter
from persona_bot.services.reminder_service import ReminderService
from persona_bot.services.stats_service import StatsService
from persona_bot.services.voice_guard import VoiceGuard
from persona_bot.utils.usage_graph import UsageTracker


async def handle_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError,
) -> None:
    """コマンドの失敗を応答状態に合わせて本人へ通知する。例外本文は公開しない。"""
    original = getattr(error, "original", error)
    print(f"スラッシュコマンドの実行に失敗しました: {type(original).__name__}")
    if isinstance(error, app_commands.NoPrivateMessage):
        notice = "このコマンドはサーバー内で実行してください。"
    elif (
        isinstance(error, (app_commands.MissingPermissions, app_commands.BotMissingPermissions))
        or isinstance(original, discord.Forbidden)
    ):
        notice = "⚠️ コマンドの実行に必要な権限がありません。サーバーとチャンネルの権限設定を確認してください。"
    elif isinstance(error, app_commands.CommandOnCooldown):
        notice = "⚠️ 操作の間隔が短すぎます。しばらく待ってからもう一度お試しください。"
    elif isinstance(error, app_commands.CheckFailure):
        notice = "⚠️ このコマンドを実行する条件を満たしていません。"
    else:
        notice = "⚠️ コマンドの実行中にエラーが発生しました。しばらく待ってからもう一度お試しください。"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(notice, ephemeral=True)
        else:
            await interaction.response.send_message(notice, ephemeral=True)
    except Exception as send_error:
        print(f"スラッシュコマンドの失敗通知を送信できませんでした: {type(send_error).__name__}")


async def announce_voice_join(
    interaction: discord.Interaction, channel: discord.VoiceChannel | discord.StageChannel,
    *, already_connected: bool = False,
) -> None:
    """ステージでは発言者へ昇格してから、接続の結果を案内する。"""
    if isinstance(channel, discord.StageChannel):
        try:
            await enable_stage_speaker(interaction.guild, channel)
        except Exception as error:
            print(f"ステージの発言者への昇格に失敗しました: {type(error).__name__}: {error}")
            await interaction.followup.send(
                f"⚠️ {channel.name} へ接続していますが、ステージの発言者になれませんでした。"
                "ボットを発言者へ招待するか、管理者にステージの「メンバーをミュート」権限を確認してください。"
            )
            return
    message = (f"{channel.name} には既に接続しています。" if already_connected
               else f"{channel.name} に接続しました。")
    if isinstance(channel, discord.StageChannel):
        message += "ステージの発言者として読み上げます。"
    await interaction.followup.send(message)


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
    autonomous: AutonomousService,
    *,
    voice_guard: VoiceGuard | None = None,
    task_owner: TaskOwner | None = None,
) -> None:
    tree.on_error = handle_command_error
    command_tasks = task_owner if task_owner is not None else TaskOwner()
    voice_commands = VoiceCommandCoordinator()

    def owned_command(callback):
        """Discordが生成したコマンドの本体と失敗通知を、所有者の終了待機へ登録する。"""
        @wraps(callback)
        async def invoke(interaction: discord.Interaction) -> None:
            async def run_command() -> None:
                try:
                    await callback(interaction)
                except Exception as error:
                    # SDKと同じ例外形式を使い、失敗通知を所有期間内で一度だけ行う。
                    interaction.command_failed = True
                    if not isinstance(error, app_commands.AppCommandError):
                        error = app_commands.CommandInvokeError(interaction.command, error)
                    await wait_for_cleanup(
                        handle_command_error(interaction, error), label="スラッシュコマンドの失敗通知",
                    )

            await command_tasks.run(run_command())

        return invoke

    @tree.command(name="functions", description="各種機能パネルを開きます（キャラ設定 / 会話要約 / 会話統計 / 自発会話 / リマインダー）。")
    @owned_command
    async def functions(interaction: discord.Interaction) -> None:
        if await reject_blocked_interaction(interaction, blacklist.is_blocked):
            return
        # ブロック解除ボタンの表示可否を決めるため、実行者が管理者かどうかを判定する
        is_admin = is_administrator(interaction.user)
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
    @owned_command
    async def join(interaction: discord.Interaction) -> None:
        if await reject_blocked_interaction(interaction, blacklist.is_blocked):
            return
        member = interaction.user
        if not isinstance(member, discord.Member) or member.voice is None or member.voice.channel is None:
            await interaction.response.send_message("先にボイスチャンネルに参加してください。")
            return

        voice_channel = member.voice.channel
        guild = interaction.guild
        is_stage = isinstance(voice_channel, discord.StageChannel)

        # 接続を試す前に権限を確かめる。権限不足は接続失敗の最も多い原因だが、
        # connect() の Forbidden として出るため、後段でまとめて拾うと
        # 「接続に失敗しました」としか伝えられず原因にたどり着けない。
        bot_member = guild.me if guild is not None else None
        if bot_member is not None:
            permissions = voice_channel.permissions_for(bot_member)
            missing_permissions = [
                name for name, allowed in (
                    ("チャンネルを見る", permissions.view_channel),
                    ("接続", permissions.connect), ("発言", permissions.speak),
                )
                if not allowed
            ]
            if is_stage and not permissions.mute_members:
                missing_permissions.append("メンバーをミュート")
            if missing_permissions:
                permission_names = "・".join(f"「{name}」" for name in missing_permissions)
                await interaction.response.send_message(
                    f"⚠️ {voice_channel.name} でボットに{permission_names}の権限がありません。\n"
                    "サーバー設定 →「連携サービス」か、そのボイスチャンネルの権限設定で、"
                    f"ボットのロールに{permission_names}を許可してください"
                    "（チャンネル個別の権限で拒否されている場合もあります）。"
                    + ("ステージでは観客から発言者になるため「メンバーをミュート」が必要です。" if is_stage else "")
                )
                return

        await voice_commands.run(
            interaction, lambda is_current: join_voice(interaction, voice_channel, is_current),
        )

    async def join_voice(
        interaction: discord.Interaction, voice_channel: discord.VoiceChannel | discord.StageChannel,
        is_current: Callable[[], bool],
    ) -> None:
        guild = interaction.guild
        # 自律行動でボットが自分から通話に入っている最中かもしれない。人が呼んだ
        # 接続を優先するため、ステージ枠の後始末まで含めて先に配信を終了させる
        # （ここを素の disconnect だけで済ませると、ボットが開いたステージ枠が
        # 「配信中」の看板を出したまま残ってしまう）。
        if guild is not None:
            await autonomous.stop_session(
                guild.id, reason=f"/join による上書き（{interaction.user}）"
            )
            if await skip_superseded_voice_operation(interaction, is_current):
                return
            voice_client = guild.voice_client
            if voice_client is not None:
                # 既に同じチャンネルへ接続中なら、そのまま使う。切り直すと、読み上げ中の
                # 音声が途中で止まり、キューに積まれた返信も捨てられてしまう。
                # （自律行動の配信だった場合は、上の stop_session が接続ごと片付けている。）
                if (isinstance(voice_client, discord.VoiceClient) and voice_client.is_connected()
                        and getattr(voice_client.channel, "id", None) == voice_channel.id):
                    # 移された先で /join された場合も、ここを意図した接続先にする（元へ戻さない）。
                    if voice_guard is not None:
                        voice_guard.adopt(guild.id, voice_channel.id)
                    await announce_voice_join(interaction, voice_channel, already_connected=True)
                    return
                # 切り替えの前に保護を解く。移動を戻している最中の処理が、切る予定の接続を
                # 元のチャンネルへ動かそうとしないようにする。
                if voice_guard is not None:
                    voice_guard.release(guild.id)
                try:
                    await wait_for_cleanup(
                        voice_client.disconnect(force=True), label="参加前のボイス切断",
                    )
                except Exception as error:
                    print(f"接続切替エラー（既存接続の切断）: {type(error).__name__}")
                    await interaction.followup.send(
                        "⚠️ 既存のボイス接続を切断できなかったため、接続先を切り替えられませんでした。"
                        "しばらく待ってからもう一度お試しください。"
                    )
                    return

        if await skip_superseded_voice_operation(interaction, is_current):
            return
        # 例外の型ごとに原因を伝える。ここをまとめて握りつぶすと、利用者からは
        # 権限・ネットワーク・接続残りのどれなのか判別できなくなる。
        try:
            await wait_for_cleanup(
                voice_channel.connect(timeout=VOICE_CONNECT_TIMEOUT_SECONDS), label="ボイス参加の接続完了",
            )
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
            if not is_current():
                # 後続の有効な参加・退出に、この接続の切替・切断を委ねる。
                return
            # 接続イベントでも登録されるが、人が選んだ接続先を明示的に意図した接続先にする。
            if voice_guard is not None and guild is not None:
                voice_guard.adopt(guild.id, voice_channel.id)
            await announce_voice_join(interaction, voice_channel)

    @tree.command(
        name="leave",
        description="ボットがボイスチャンネルから退出します（自律行動の配信もここで止められます）",
    )
    @owned_command
    async def leave(interaction: discord.Interaction) -> None:
        if await reject_blocked_interaction(interaction, blacklist.is_blocked):
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("ボットはボイスチャンネルに参加していません。")
            return

        await voice_commands.run(interaction, lambda _: leave_voice(interaction))

    async def leave_voice(interaction: discord.Interaction) -> None:
        guild = interaction.guild
        # 退出の後に、移動を戻す処理が元のチャンネルへ入り直さないよう先に保護を解く。
        if voice_guard is not None:
            voice_guard.release(guild.id)

        # 自律行動で自分から入っている場合は、締めの投稿・ステージ枠の後片付け・
        # セッション記録の破棄・切断までをサービス側がまとめて行う
        # （idea.md の「既存の /leave コマンドで強制的に退出できるようにする」）。
        # 配信していなければ False が返るので、従来どおりの切断へ進む。
        stopped = await autonomous.stop_session(guild.id, reason=f"/leave（{interaction.user}）")
        if stopped:
            await interaction.followup.send("退出しました。（自律行動の配信を停止しました）")
            return

        if guild.voice_client is None:
            await interaction.followup.send("ボットはボイスチャンネルに参加していません。")
            return

        try:
            # stop_session を経ていない経路のみここへ来る。切断済みの可能性は無いが、
            # 念のため取り直してから切る。
            voice_client = guild.voice_client
            if voice_client is not None:
                await wait_for_cleanup(
                    voice_client.disconnect(force=True), label="退出時のボイス切断",
                )
            await interaction.followup.send("退出しました。")
        except Exception as error:
            print(f"切断エラー: {error}")
            await interaction.followup.send("退出に失敗しました。")

