"""自律配信で使うチャンネル選択・ステージ枠・ボイス接続の制御。

配信の開始判断と状態の破棄は AutonomousService が担当する。
ここでは作成した枠のIDと接続の同一性を確認し、他の配信や手動接続を保護する。
"""

import asyncio
import random
from typing import Any

import discord

from persona_bot.core.app_state import AutonomousSession, GuildSettings
from persona_bot.core.background import wait_for_cleanup
from persona_bot.config.constants import (
    AUTONOMOUS_STAGE_TOPIC,
    AUTONOMOUS_VOICE_STATE_RETRIES,
    AUTONOMOUS_VOICE_STATE_WAIT_SECONDS,
)

VocalChannel = discord.VoiceChannel | discord.StageChannel


def pick_channel(guild: discord.Guild) -> VocalChannel | None:
    """出ていく先のチャンネルを1つ選ぶ。ステージを優先し、無ければボイスから選ぶ。

    候補が複数あればランダムに選ぶ（毎回同じ場所へ出ると単調になるため）。

    権限は必ず `permissions_for(guild.me)` で**チャンネル単位に**確認する。
    サーバー全体のロールで許可されていても、チャンネル個別の設定で拒否
    されていることがあるため（`/join` の接続権限チェックと同じ作法）。
    「入ってみて失敗したら次」にしないのは、無駄に API を叩くうえ、
    権限エラーとネットワーク障害を区別できなくなるため。
    """
    bot_member = guild.me
    if bot_member is None:
        # キャッシュ未取得などで自分自身の Member が引けないときは権限を判定できない。
        return None

    stages = [c for c in guild.stage_channels if can_use_stage(c, bot_member)]
    if stages:
        return random.choice(stages)
    voices = [c for c in guild.voice_channels if can_use_voice(c, bot_member)]
    if voices:
        return random.choice(voices)
    return None


def can_use_voice(channel: discord.VoiceChannel, bot_member: discord.Member) -> bool:
    """そのボイスチャンネルで配信できるか。

    独り言はチャンネル付属のチャットにも投稿し、そこがコメントの受付口にも
    なるため、接続・発話だけでなく閲覧と送信の権限も要る。
    """
    permissions = channel.permissions_for(bot_member)
    return bool(
        permissions.view_channel
        and permissions.connect
        and permissions.speak
        and permissions.send_messages
    )


def can_use_stage(channel: discord.StageChannel, bot_member: discord.Member) -> bool:
    """そのステージチャンネルで発言者として配信できるか。

    ステージは参加しただけでは観客（suppress=True）で音声を流せず、
    自分の抑制を解除するには mute_members が要る。さらにステージ枠が
    まだ無ければ自分で開く必要があり、それにはステージモデレーターの
    3権限（manage_channels / mute_members / move_members）が要る。
    条件を満たさないステージは候補から外す（入っても喋れないため）。
    """
    permissions = channel.permissions_for(bot_member)
    if not (permissions.view_channel and permissions.connect and permissions.send_messages):
        return False
    if not permissions.mute_members:
        return False
    if channel.instance is None and not (permissions.manage_channels and permissions.move_members):
        return False
    return True


async def prepare_stage(guild: discord.Guild, channel: discord.StageChannel) -> int | None:
    """ステージで発言できる状態にし、自分で開いた枠のIDを返す。

    ステージチャンネルは参加しただけでは「観客」（suppress=True）のままで
    音声を流せない。自分の抑制を解除するには mute_members 権限が要る。
    ステージ枠（StageInstance）がまだ無ければ自分で開く — その看板（topic）が
    チャンネル一覧に出るので、「会話相手募集中…」を人に見せる手段にもなる。
    他の人が開いた枠に相乗りした場合は None を返し、終了時にも閉じない。
    """
    created_instance = None
    if channel.instance is None:
        created_instance = await channel.create_instance(
            topic=AUTONOMOUS_STAGE_TOPIC,
            privacy_level=discord.PrivacyLevel.guild_only,
            # **send_start_notification を True にしないこと。** True にすると
            # Discord が「ステージが始まりました」という @everyone 相当の通知を
            # サーバー全体へ送る。ボットが自分の判断で何度も配信を始める機能で
            # これを有効にすると、そのたびに全員へ通知が飛ぶ（通知爆撃になる）。
            send_start_notification=False,
            reason="ボットの自律行動による配信開始",
        )

    try:
        await enable_stage_speaker(guild, channel)
    except (Exception, asyncio.CancelledError):
        # 枠を開いた後で発言者になれなかった場合、そのまま抜けると「会話相手
        # 募集中…」の看板を出したステージが誰も居ないまま開きっぱなしになる。
        # 自分で開いた枠だけは閉じてから、失敗を呼び出し元へ伝える。
        if created_instance is not None:
            await wait_for_cleanup(
                _rollback_created_stage(channel, created_instance), label="自律行動の開始中止の枠終了",
            )
        raise

    return created_instance.id if created_instance is not None else None


async def enable_stage_speaker(guild: discord.Guild, channel: discord.StageChannel) -> None:
    """接続先のボイス状態を待ち、そのステージでだけ観客から発言者へ昇格する。"""
    member = guild.me
    for _ in range(AUTONOMOUS_VOICE_STATE_RETRIES):
        if (member is not None and member.voice is not None
                and getattr(member.voice.channel, "id", None) == channel.id):
            break
        await asyncio.sleep(AUTONOMOUS_VOICE_STATE_WAIT_SECONDS)
        member = guild.me
    if (member is None or member.voice is None
            or getattr(member.voice.channel, "id", None) != channel.id):
        raise RuntimeError(
            "接続先のボイス状態を取得できず、ステージの発言者になれませんでした"
            f"（{AUTONOMOUS_VOICE_STATE_RETRIES} 回待機）。"
        )
    await member.edit(suppress=False)


async def _rollback_created_stage(channel: discord.StageChannel, created_instance: discord.StageInstance) -> None:
    """開始準備に失敗した枠を閉じる。差し替わった枠や借りた枠には触らない。"""
    try:
        instance = channel.instance
        if instance is None or instance.id == created_instance.id:
            await created_instance.delete(reason="ボットの自律行動: 発言者になれなかったため中止")
    except Exception as error:
        # 削除が失敗しても、元の例外・キャンセルと接続の後片付けを妨げない。
        print(f"自律行動: 中止時のステージ枠の終了に失敗しました（{channel.name}）: {type(error).__name__}: {error}")


async def finish_session_cleanup(
    guild_id: int, guild_settings: GuildSettings, session: AutonomousSession,
    channel: Any, guild: discord.Guild | None, voice_client: discord.VoiceProtocol | None,
) -> None:
    """呼出元のキャンセルから独立して、所有枠の終了と元の接続の切断を完了する。"""
    try:
        await close_owned_stage(guild_id, guild_settings, session, channel)
    finally:
        # 新しい配信に同じVoiceClientが引き継がれた場合も、その配信を優先する。
        if guild is not None and voice_client is not None and guild_settings.autonomous_session is None:
            await disconnect(guild, expected_voice_client=voice_client)


async def close_owned_stage(
    guild_id: int, guild_settings: GuildSettings,
    session: AutonomousSession, channel: Any,
) -> None:
    """自分が開いた枠だけを閉じる。待機中に始まった別の配信には触らない。"""
    if (not session.stage_started_by_bot or session.stage_instance_id is None
            or not isinstance(channel, discord.StageChannel)
            or guild_settings.autonomous_session is not None):
        return
    try:
        instance = channel.instance
        if instance is None:
            # 作成直後やキック後はキャッシュに枠が無くても、Discord上には残りうる。
            instance = await channel.fetch_instance()
        if (guild_settings.autonomous_session is None
                and instance.id == session.stage_instance_id):
            await instance.delete(reason="ボットの自律行動による配信終了")
    except discord.HTTPException as error:
        print(f"自律行動: ステージ枠の終了に失敗しました（guild={guild_id}）: {error}")
    except Exception as error:
        print(f"自律行動: ステージ枠の終了で予期せぬエラー（guild={guild_id}）: {type(error).__name__}: {error}")


async def disconnect(
    guild: discord.Guild, *, expected_voice_client: discord.VoiceProtocol | None = None,
) -> None:
    """ボイス接続を切る（既に切れていても例外を外へ出さない）。"""
    voice_client = guild.voice_client
    if voice_client is None or (expected_voice_client is not None and voice_client is not expected_voice_client):
        return
    try:
        await voice_client.disconnect(force=True)
    except Exception as error:
        print(f"自律行動: ボイス切断に失敗しました（guild={guild.id}）: {error}")
