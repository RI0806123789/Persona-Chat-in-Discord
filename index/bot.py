import asyncio
import sys

import discord
from discord import app_commands
from discord.ext import tasks
import google.generativeai as genai

# Windows の既定コンソール（cp932）は絵文字などを出力できず、print した時点で
# UnicodeEncodeError になって処理がそこで止まる。ボット名・ユーザーの発言・NGワード・
# モデルの応答にはいずれも絵文字が入りうるため、標準出力を UTF-8（変換できない文字は
# 置換）に設定してログ出力でボットの処理が中断されないようにする。
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except Exception as _error:  # 出力設定の失敗でボットを止めない
            print(f"標準出力のエンコーディング設定に失敗しました: {_error}")

from app_state import BotState
from affinity_service import AffinityService, extract_affinity_changes
from auto_topic_service import AutoTopicService
from background import spawn
from blacklist_service import BlacklistService
from commands import register_commands
from constants import (
    ANNIVERSARY_CHECK_MINUTES,
    AUTO_TOPIC_CHECK_MINUTES,
    BLACKLIST_MAX_VIOLATIONS,
    DATABASE_PATH,
    GEMINI_RESPONSE_TIMEOUT_SECONDS,
    HISTORY_LIMIT,
    LEGACY_MEMORY_OWNER_ID,
    MAX_DOCUMENT_SIZE_BYTES,
    MEMORY_MAINTENANCE_MINUTES,
    MODERATION_MODEL_NAME,
    MODERATION_TIMEOUT_SECONDS,
    REMINDER_CHECK_SECONDS,
)
from content_moderator import ContentModerator, screen_bot_response
from database import Database
from discord_helpers import (
    attachment_is_image,
    clean_message_content,
    fetch_channel_history_text,
    format_grounding_sources,
    is_supported_document_attachment,
    is_supported_message_channel,
    load_image_attachments,
    send_long_reply,
)
from document_service import DocumentService
from gemini_service import GeminiService
from memory_service import MemoryService
from ng_word_service import NgWordFilter
from paths import load_prompt_template, resolve_prompt_path
from prompting import build_full_prompt, build_memory_sections
from reminder_service import ReminderService
from settings_loader import load_settings
from stats_service import StatsService
from usage_graph import UsageTracker
from voice import play_tts_response


settings = load_settings()
genai.configure(api_key=settings.API_KEY_GEMINI)  # type: ignore[reportPrivateImportUsage]

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

state = BotState()
usage = UsageTracker()
database = Database(DATABASE_PATH, legacy_owner_id=LEGACY_MEMORY_OWNER_ID)
database.initialize()
# 保存済みのサーバーごとの設定（ペルソナ・モデル・応答モード・Google検索・自発会話）と
# 記念日の投稿記録をメモリへ復元する。以降の設定変更は各 GuildSettings.persist() で保存される。
state.restore_from_database(database)
gemini = GeminiService(genai, settings.API_KEY_GEMINI)
memory = MemoryService(state, genai, database)
affinity = AffinityService(database)
ng_filter = NgWordFilter()
moderator = ContentModerator(
    genai,
    ng_filter,
    model_name=MODERATION_MODEL_NAME,
    timeout=MODERATION_TIMEOUT_SECONDS,
)
blacklist = BlacklistService()
documents = DocumentService(client, state, memory, gemini, usage, database, ng_filter, moderator)
stats = StatsService(database, gemini)
auto_topic = AutoTopicService(client, state, memory, gemini, moderator, ng_filter, usage, database)
reminders = ReminderService(client, database, ng_filter)

register_commands(tree, state, usage, ng_filter, blacklist, gemini, moderator, stats, reminders)


@tasks.loop(minutes=MEMORY_MAINTENANCE_MINUTES)
async def memory_maintenance_loop() -> None:
    try:
        await memory.flush_if_needed()
    except Exception as error:
       print(f"バックグラウンド記憶処理エラー: {error}")


@memory_maintenance_loop.before_loop
async def before_memory_maintenance_loop() -> None:
    await client.wait_until_ready()


@tasks.loop(minutes=AUTO_TOPIC_CHECK_MINUTES)
async def auto_topic_loop() -> None:
    try:
        await auto_topic.run_pending_checks()
    except Exception as error:
        print(f"自発的な会話の見回りエラー: {error}")


@auto_topic_loop.before_loop
async def before_auto_topic_loop() -> None:
    await client.wait_until_ready()


@tasks.loop(minutes=ANNIVERSARY_CHECK_MINUTES)
async def anniversary_loop() -> None:
    try:
        await auto_topic.run_anniversary_checks()
    except Exception as error:
        print(f"記念日チェックエラー: {error}")


@anniversary_loop.before_loop
async def before_anniversary_loop() -> None:
    await client.wait_until_ready()


# リマインダーは分単位の指定に応えるため、記念日（時間単位）よりずっと短い間隔で見回る。
@tasks.loop(seconds=REMINDER_CHECK_SECONDS)
async def reminder_loop() -> None:
    try:
        await reminders.run_due_checks()
    except Exception as error:
        print(f"リマインダー確認エラー: {error}")


@reminder_loop.before_loop
async def before_reminder_loop() -> None:
    await client.wait_until_ready()


@client.event
async def on_ready() -> None:
    print(f"ログイン成功: {client.user}")
    memory.ensure_storage()

    if not state.memory_background_started:
        memory_maintenance_loop.start()
        state.memory_background_started = True

    if not state.document_worker_started:
        # 文書ワーカーはボットの生存期間ずっと走り続ける常駐コルーチン。
        # 参照を保持しないと GC で静かに止まり、添付ファイルが永久に処理されなくなる。
        spawn(documents.run_worker(), label="文書処理ワーカー")
        state.document_worker_started = True

    if not state.auto_topic_background_started:
        auto_topic_loop.start()
        state.auto_topic_background_started = True

    if not state.anniversary_background_started:
        anniversary_loop.start()
        state.anniversary_background_started = True

    if not state.reminder_background_started:
        reminder_loop.start()
        state.reminder_background_started = True

    if not state.commands_synced:
        await tree.sync()
        state.commands_synced = True


async def notify_violation(message: discord.Message, notice: str) -> None:
    """違反を1回記録し、ブロック状況に応じた警告をユーザーに返信する。"""
    count, just_blocked = blacklist.record_violation(message.author.id, str(message.author))
    if just_blocked:
        await message.reply(
            "🚫 違反が3回に達したため、ブラックリストに登録されました。"
            "以降のメッセージは無視されます。"
        )
    else:
        remaining = BLACKLIST_MAX_VIOLATIONS - count
        await message.reply(f"⚠️ {notice}（あと{remaining}回で応答を停止します）")


@client.event
async def on_message(message: discord.Message) -> None:
    if client.user is None:
        return
    if message.author == client.user or message.author.bot:
        return
    is_dm = isinstance(message.channel, discord.DMChannel)
    # DM はギルドに属さないため guild_id=None として「全DM共有」の設定スロットへ収束させる。
    guild_id = message.guild.id if message.guild is not None else None
    guild_settings = state.get_guild(guild_id)
    auto_topic.mark_activity(message.channel.id, guild_id)
    if guild_settings.current_respond_mode == "mention" and not is_dm and client.user not in message.mentions:
        return

    if blacklist.is_blocked(message.author.id):
        return

    question = clean_message_content(message, client.user)
    if not question and not message.attachments:
        return

    # 返信も履歴取得もできないチャンネル種別は、NG判定やモデレーション（Gemini 呼び出し）に
    # 進む前にここで打ち切る。後段に置くと、結局返信できない発言のために API を1回
    # 無駄打ちし、違反警告の message.reply() も失敗してしまう。
    channel = message.channel
    if not is_supported_message_channel(channel):
        return

    if question and ng_filter.contains_ng_word(question):
        detected = ng_filter.find_ng_words(question)
        print(f"NGワード検出 (ユーザー入力): {detected}")
        await notify_violation(message, "NGワードが含まれているため、メッセージを処理できません。")
        return

   # ここでの category は使わない（安全判定だけが効く）。
    memory_category = "none"
    reminder_request: dict[str, str] | None = None
    if question:
        is_safe, _reason, memory_category, reminder_request = await moderator.check_and_route(question)
        if not is_safe:
            await notify_violation(message, "不適切な内容が検出されたため、メッセージを処理できません。")
            return

    state.touch()

    # （DM は「テキストチャンネルへの話題提供」の対象外とする）。
    if not is_dm:
        auto_topic.watch_channel(message.channel.id, guild_id)

    document_attachments = [
        attachment
        for attachment in message.attachments
        if is_supported_document_attachment(attachment)
    ]
    unsupported_attachments = [
        attachment
        for attachment in message.attachments
        if not is_supported_document_attachment(attachment) and not attachment_is_image(attachment)
    ]

    if unsupported_attachments:
        await message.reply("未対応の添付ファイルが含まれています。pdf / txt / csv / md のみ対応しています。")
        return

    if document_attachments:
        for attachment in document_attachments:
            if attachment.size > MAX_DOCUMENT_SIZE_BYTES:
                await message.reply(f"ファイルサイズが上限({MAX_DOCUMENT_SIZE_BYTES // (1024 * 1024)}MB)を超えています。")
                return

        await documents.enqueue_from_message(message, question, document_attachments)
        await message.reply("受付が完了しました。順番に処理しています…")
        return

    # --- 会話統計の記録（ボットへの発言のみを集計。失敗してもチャット本体は止めない） ---
    spawn(
        stats.record(
            str(message.author.id),
            message.author.display_name,
            question,
            message.created_at,
        ),
        label="会話統計の記録",
    )

    # --- リマインダー依頼（モデレーションと同じ判定で抽出済み） ---
    # 添付ファイルの処理より後に置く: 文書を添えた依頼では文書処理を優先する。
    # 登録できたら確認だけを返し、通常のチャット応答は行わない
    # （同じ発言に対して確認とチャット返信の二重投稿になるのを避けるため）。
    # 日時が過去・解釈不能などで登録を見送った場合は None が返るので、
    # 依頼ではない発言を誤って拾ったものとして通常の会話へ流す。
    if reminder_request is not None:
        confirmation = await reminders.create_from_request(message, reminder_request)
        if confirmation:
            await message.reply(confirmation)
            return

    response_text = await handle_chat_message(message, question, memory_category)
    if response_text:
        # ボットが /join でボイスチャンネルに接続中のときだけ読み上げる（それ以外は no-op）。
        await play_tts_response(message, response_text)


async def handle_chat_message(message: discord.Message, question: str, memory_category: str = "none") -> str | None:
    async with message.channel.typing():
        memory.ensure_storage()

        user_id = str(message.author.id)
        guild_id = message.guild.id if message.guild is not None else None
        guild_settings = state.get_guild(guild_id)
        image_objects = await load_image_attachments(message.attachments)
        if not question:
            question = "この画像について説明・反応してください。"

        history_text = await fetch_channel_history_text(
            message.channel,
            guild_settings.channel_reset_points.get(message.channel.id),
            client.user,
            HISTORY_LIMIT,
        )
        # 記憶カテゴリは on_message のモデレーション統合判定で既に決まっている
        # （追加の Gemini 呼び出しはしない）。ユーザー別の記憶スペースを読み込む。
        memory_context = memory.build_context(memory_category, user_id)
        document_context = memory.build_document_memory_context(user_id)

        try:
            prompt = load_prompt_template(resolve_prompt_path(guild_settings.current_prompt_file))
        except FileNotFoundError:
            print(f"エラー: {guild_settings.current_prompt_file} が見つかりません。")
            await message.reply(f"エラー: プロンプトファイル({guild_settings.current_prompt_file})が見つかりませんでした。")
            return None

        full_prompt = build_full_prompt(
            prompt,
            build_memory_sections(memory_category, memory_context, document_context),
            history_text,
            question,
            affinity_prompt=affinity.build_dynamic_prompt(str(message.author.id))
        )
        response_text, usage_info, grounding_sources = await gemini.generate(
            full_prompt,
            guild_settings.current_model_name,
            image_objects,
            timeout=GEMINI_RESPONSE_TIMEOUT_SECONDS,
            grounding=guild_settings.grounding_enabled,
        )

        if response_text is None:
            await message.reply("エラーが発生しました。（Gemini APIの呼び出しに失敗、または非対応の画像形式です）")
            return None

        usage.log_snapshot("直接会話", question, f"画像添付: {len(image_objects)}枚", full_prompt, response_text, usage_info)

        # --- タグ抽出とAffinity更新 ---
        response_text, emotion_changes = extract_affinity_changes(response_text)
        if emotion_changes is not None:
            v_change, a_change = emotion_changes
            affinity.update_emotion(str(message.author.id), v_change, a_change)
            spawn(affinity.save_background(), label="親密度の保存")

        # タグを除去した結果、本文が空になった場合は空メッセージ送信を防ぐ
        # （モデルが感情タグだけを返したケース）。
        if not response_text.strip():
            print("応答本文が空です（感情タグのみ、または空応答）。")
            await message.reply("申し訳ありません、うまく応答を生成できませんでした。もう一度お試しください。")
            return None

        response_text = await screen_bot_response(response_text, ng_filter, moderator, log_label="Bot応答")

        # 参考リンクは送信用テキストにのみ追記する
        # （TTS 読み上げと長期記憶には URL の列挙はノイズになるため含めない）。
        reply_text = response_text
        if grounding_sources:
            reply_text += format_grounding_sources(grounding_sources)

        await send_long_reply(message, reply_text, suppress_embeds=bool(grounding_sources))
        await memory.append_pending_log(user_id, question, response_text)
        spawn(memory.flush_if_needed(), label="記憶圧縮（チャット便乗）")
        return response_text


def main() -> None:
    client.run(settings.TOKEN_DISCORD)


if __name__ == "__main__":
    main()
