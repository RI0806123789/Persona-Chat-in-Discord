import asyncio

import discord
from discord import app_commands
from discord.ext import tasks
import google.generativeai as genai
import re

from app_state import BotState
from affinity_service import AffinityService
from auto_topic_service import AutoTopicService
from blacklist_service import BlacklistService
from commands import register_commands
from constants import (
    AUTO_TOPIC_CHECK_MINUTES,
    BLACKLIST_MAX_VIOLATIONS,
    DATABASE_PATH,
    GEMINI_RESPONSE_TIMEOUT_SECONDS,
    HISTORY_LIMIT,
    MAX_DOCUMENT_SIZE_BYTES,
    MEMORY_MAINTENANCE_MINUTES,
    MODERATION_MODEL_NAME,
    MODERATION_TIMEOUT_SECONDS,
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
from paths import load_prompt_template, resolve_prompt_path, resolve_script_path
from prompting import build_full_prompt, build_memory_sections
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
# データベースは各サービスより先に用意する（テーブル作成・スキーマ移行・旧JSON取り込み）
database = Database(DATABASE_PATH)
database.initialize()
gemini = GeminiService(state, genai, settings.API_KEY_GEMINI)
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
auto_topic = AutoTopicService(client, state, memory, gemini, moderator, ng_filter, usage)

register_commands(tree, state, usage, ng_filter, blacklist, gemini, moderator, stats)


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


@client.event
async def on_ready() -> None:
    print(f"ログイン成功: {client.user}")
    memory.ensure_storage()

    if not state.memory_background_started:
        memory_maintenance_loop.start()
        state.memory_background_started = True

    if not state.document_worker_started:
        asyncio.create_task(documents.run_worker())
        state.document_worker_started = True

    if not state.auto_topic_background_started:
        auto_topic_loop.start()
        state.auto_topic_background_started = True

    # on_ready は再接続のたびに呼ばれるため、レート制限の厳しい sync は初回のみ実行する
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
    # 自発的な会話: ボット宛てかどうかに関係なく「チャンネルで会話が行われた」ことを
    # 記録する（メンション判定で return する前に呼ぶ必要がある）。
    auto_topic.mark_activity(message.channel.id)
    if state.current_respond_mode == "mention" and not is_dm and client.user not in message.mentions:
        return

    # --- ブラックリスト照合（ブロック中なら完全に無視） ---
    if blacklist.is_blocked(message.author.id):
        return

    question = clean_message_content(message, client.user)
    if not question and not message.attachments:
        return

    if question and ng_filter.contains_ng_word(question):
        detected = ng_filter.find_ng_words(question)
        print(f"NGワード検出 (ユーザー入力): {detected}")
        await notify_violation(message, "NGワードが含まれているため、メッセージを処理できません。")
        return

    # --- AI モデレーション（ユーザー入力） ---
    if question:
        is_safe, _reason = await moderator.check(question, direction="ユーザー入力")
        if not is_safe:
            await notify_violation(message, "不適切な内容が検出されたため、メッセージを処理できません。")
            return

    channel = message.channel
    if not is_supported_message_channel(channel):
        return

    state.touch()

    # 自発的な会話: ボットが会話に参加したチャンネルを監視対象に登録する
    # （DM は「テキストチャンネルへの話題提供」の対象外とする）。
    if not is_dm:
        auto_topic.watch_channel(message.channel.id)

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
    asyncio.create_task(
        stats.record(
            str(message.author.id),
            message.author.display_name,
            question,
            message.created_at,
        )
    )

    response_text = await handle_chat_message(message, question)
    if response_text:
        await play_tts_response(message, response_text, resolve_script_path("ffmpeg.exe"))


async def handle_chat_message(message: discord.Message, question: str) -> str | None:
    async with message.channel.typing():
        memory.ensure_storage()

        image_objects = await load_image_attachments(message.attachments)
        if not question:
            question = "この画像について説明・反応してください。"

        history_text = await fetch_channel_history_text(
            message.channel,
            state.channel_reset_points.get(message.channel.id),
            client.user,
            HISTORY_LIMIT,
        )
        memory_category = await memory.route_category(question)
        memory_context = memory.build_context(memory_category)
        document_context = memory.build_document_memory_context()

        try:
            prompt = load_prompt_template(resolve_prompt_path(state.current_prompt_file))
        except FileNotFoundError:
            print(f"エラー: {state.current_prompt_file} が見つかりません。")
            await message.reply(f"エラー: プロンプトファイル({state.current_prompt_file})が見つかりませんでした。")
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
            image_objects,
            timeout=GEMINI_RESPONSE_TIMEOUT_SECONDS,
            grounding=state.grounding_enabled,
        )

        if response_text is None:
            await message.reply("エラーが発生しました。（Gemini APIの呼び出しに失敗、または非対応の画像形式です）")
            return None

        usage.log_snapshot("直接会話", question, f"画像添付: {len(image_objects)}枚", full_prompt, response_text, usage_info)

        # --- タグ抽出とAffinity更新 ---
        # モデルは `[V: +5, A: -10]` のように空白を挟むことがあるため、
        # 各要素の前後に \s* を許容しないとタグがユーザーに露出する。
        pattern = r'\[V:\s*([+-]?\d+)\s*,\s*A:\s*([+-]?\d+)\s*\]'
        match = re.search(pattern, response_text)
        if match:
            v_change = int(match.group(1))
            a_change = int(match.group(2))
            response_text = re.sub(pattern, '', response_text).strip()
            affinity.update_emotion(str(message.author.id), v_change, a_change)
            asyncio.create_task(affinity.save_background())

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
        await memory.append_pending_log(question, response_text)
        asyncio.create_task(memory.flush_if_needed())
        return response_text


def main() -> None:
    client.run(settings.TOKEN_DISCORD)


if __name__ == "__main__":
    main()
