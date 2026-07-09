import io
import re
from pathlib import Path
from typing import Any, Sequence

import discord
from PIL import Image

from constants import SUPPORTED_DOCUMENT_EXTENSIONS

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
DISCORD_MESSAGE_MAX_LENGTH = 2000


def is_supported_message_channel(channel: Any) -> bool:
    return isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel, discord.StageChannel, discord.DMChannel))


def is_supported_document_attachment(attachment: discord.Attachment) -> bool:
    return Path(attachment.filename).suffix.lower() in SUPPORTED_DOCUMENT_EXTENSIONS


def attachment_is_image(attachment: discord.Attachment) -> bool:
    content_type = attachment.content_type or ""
    suffix = Path(attachment.filename).suffix.lower()
    return content_type.startswith("image/") or suffix in IMAGE_EXTENSIONS


def clean_message_content(message: discord.Message, bot_user: Any) -> str:
    content = message.content.strip()
    if bot_user is None:
        return content
    return re.sub(rf"<@!?{bot_user.id}>", "", content).strip()


def build_history_text(messages: Sequence[discord.Message], bot_user: Any) -> str:
    lines: list[str] = []
    for history_message in messages:
        role = "BOTの回答" if history_message.author == bot_user else "ユーザーの質問"
        clean_content = clean_message_content(history_message, bot_user)
        if history_message.attachments:
            clean_content += " [画像添付あり]"
        lines.append(f"{role}: {clean_content}")
    return "\n".join(lines)


async def fetch_channel_history_text(
    channel: Any,
    reset_message_id: int | None,
    bot_user: Any,
    limit: int,
) -> str:
    history_messages: list[discord.Message] = []
    if reset_message_id:
        async for history_message in channel.history(
            limit=limit,
            after=discord.Object(id=reset_message_id),
            oldest_first=True,
        ):
            history_messages.append(history_message)
    else:
        async for history_message in channel.history(limit=limit):
            history_messages.append(history_message)
        history_messages = list(reversed(history_messages))
    return build_history_text(history_messages, bot_user)


async def load_image_attachments(attachments: Sequence[discord.Attachment]) -> list[Image.Image]:
    image_objects: list[Image.Image] = []
    for attachment in attachments:
        if not attachment_is_image(attachment):
            continue
        try:
            image_bytes = await attachment.read()
            with Image.open(io.BytesIO(image_bytes)) as image:
                if image.mode != "RGB":
                    image = image.convert("RGB")
                image_objects.append(image.copy())
        except Exception as error:
            print(f"画像読み込みエラー: {error}")
    return image_objects


def split_message(text: str, limit: int = DISCORD_MESSAGE_MAX_LENGTH) -> list[str]:
    """長いテキストを Discord の文字数制限内に分割する。

    改行の位置で分割し、それでも収まらない場合は強制的にカットする。
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break

        # 制限内で最後の改行を探す
        split_pos = text.rfind("\n", 0, limit)
        if split_pos <= 0:
            # 改行が見つからない場合はスペースで分割を試みる
            split_pos = text.rfind(" ", 0, limit)
        if split_pos <= 0:
            # それでも見つからない場合は強制カット
            split_pos = limit

        chunks.append(text[:split_pos])
        text = text[split_pos:].lstrip("\n")

    return chunks


async def send_long_reply(
    message: discord.Message,
    text: str,
    *,
    suppress_embeds: bool = False,
) -> None:
    """2000 文字を超える応答を分割して送信する。

    最初のチャンクは元メッセージへの reply、残りは通常の send で送信する。
    suppress_embeds が True の場合、リンクの埋め込みプレビューを抑制する。
    """
    chunks = split_message(text)
    for i, chunk in enumerate(chunks):
        if i == 0:
            await message.reply(chunk, suppress_embeds=suppress_embeds)
        else:
            await message.channel.send(chunk, suppress_embeds=suppress_embeds)


def format_grounding_sources(sources: list[dict[str, str]]) -> str:
    """グラウンディングソースをコンパクトなテキストに整形する。"""
    if not sources:
        return ""
    links = "\n".join(f"・ <{s['uri']}>" for s in sources)
    return f"\n\n🔍 **参考リンク:**\n{links}"

