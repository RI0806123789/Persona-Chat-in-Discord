import asyncio

import discord
from discord import app_commands
from discord.ext import tasks
import google.generativeai as genai
from app_state import BotState
from commands import register_commands
from constants import (
    GEMINI_RESPONSE_TIMEOUT_SECONDS,
    HISTORY_LIMIT,
    MAX_DOCUMENT_SIZE_BYTES,
    MEMORY_MAINTENANCE_MINUTES,
    MODERATION_MODEL_NAME,
    MODERATION_TIMEOUT_SECONDS,
)
from content_moderator import ContentModerator
from discord_helpers import (
    attachment_is_image,
    clean_message_content,
    fetch_channel_history_text,
    is_supported_document_attachment,
    is_supported_message_channel,
    load_image_attachments,
)
from document_service import DocumentService
from gemini_service import GeminiService
from memory_service import MemoryService
from ng_word_service import NgWordFilter
from paths import load_prompt_template, resolve_prompt_path, resolve_script_path
from prompting import build_full_prompt, build_memory_sections
from settings_loader import load_settings
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
gemini = GeminiService(state, genai)
memory = MemoryService(state, genai)
ng_filter = NgWordFilter()
moderator = ContentModerator(
    genai,
    ng_filter,
    model_name=MODERATION_MODEL_NAME,
    timeout=MODERATION_TIMEOUT_SECONDS,
)
documents = DocumentService(client, state, memory, gemini, usage, ng_filter, moderator)

register_commands(tree, state, usage, ng_filter)


@tasks.loop(minutes=MEMORY_MAINTENANCE_MINUTES)
async def memory_maintenance_loop() -> None:
    try:
        await memory.flush_if_needed()
    except Exception as error:
        print(f"バックグラウンド記憶処理エラー: {error}")


@memory_maintenance_loop.before_loop
async def before_memory_maintenance_loop() -> None:
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

    await tree.sync()


@client.event
async def on_message(message: discord.Message) -> None:
    if client.user is None:
        return
    if message.author == client.user or message.author.bot:
        return
    if state.current_respond_mode == "mention" and client.user not in message.mentions:
        return

    question = clean_message_content(message, client.user)
    if not question and not message.attachments:
        return

    if question and ng_filter.contains_ng_word(question):
        detected = ng_filter.find_ng_words(question)
        print(f"NGワード検出 (ユーザー入力): {detected}")
        await message.reply("⚠️ NGワードが含まれているため、メッセージを処理できません。")
        return

    # --- AI モデレーション（ユーザー入力） ---
    if question:
        is_safe, reason = await moderator.check(question, direction="ユーザー入力")
        if not is_safe:
            await message.reply(f"⚠️ 不適切な内容が検出されたため、メッセージを処理できません。")
            return

    channel = message.channel
    if not is_supported_message_channel(channel):
        return

    state.touch()

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

    response_text = await handle_chat_message(message, question)
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

        try:
            prompt = load_prompt_template(resolve_prompt_path(state.current_prompt_file))
        except FileNotFoundError:
            print(f"エラー: {state.current_prompt_file} が見つかりません。")
            await message.reply(f"エラー: プロンプトファイル({state.current_prompt_file})が見つかりませんでした。")
            return None

        full_prompt = build_full_prompt(
            prompt,
            build_memory_sections(memory_category, memory_context),
            history_text,
            question,
        )
        response_text, usage_info = await gemini.generate(
            full_prompt,
            image_objects,
            timeout=GEMINI_RESPONSE_TIMEOUT_SECONDS,
        )
        usage.log_snapshot("直接会話", question, f"画像添付: {len(image_objects)}枚", full_prompt, response_text, usage_info)

        if response_text is None:
            await message.reply("エラーが発生しました。（Gemini APIの呼び出しに失敗、または非対応の画像形式です）")
            return None

        if ng_filter.contains_ng_word(response_text):
            detected = ng_filter.find_ng_words(response_text)
            print(f"NGワード検出 (Bot応答): {detected}")
            response_text = ng_filter.mask_ng_words(response_text)

        await message.reply(response_text)
        await memory.append_pending_log(question, response_text)
        asyncio.create_task(memory.flush_if_needed())
        return response_text


def main() -> None:
    client.run(settings.TOKEN_DISCORD)


if __name__ == "__main__":
    main()
