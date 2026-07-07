import io
from pathlib import Path
from typing import Any, Sequence

import discord
from PIL import Image

from constants import SUPPORTED_DOCUMENT_EXTENSIONS

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def is_supported_message_channel(channel: Any) -> bool:
    return isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel, discord.StageChannel))


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
    return content.replace(f"<@{bot_user.id}>", "").strip()


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

