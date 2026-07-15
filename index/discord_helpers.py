import io
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence, TypeGuard

import discord
from PIL import Image

from constants import SUPPORTED_DOCUMENT_EXTENSIONS

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
DISCORD_MESSAGE_MAX_LENGTH = 2000

# ボットが会話・履歴取得に対応しているチャンネル種別
SupportedMessageChannel = (
    discord.TextChannel | discord.Thread | discord.VoiceChannel | discord.StageChannel | discord.DMChannel
)


def is_supported_message_channel(channel: Any) -> TypeGuard[SupportedMessageChannel]:
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
    # 常に新しい順で直近 limit 件を取得する。リセットポイント指定時に古い順で
    # 取得すると、リセット以降のメッセージが limit を超えた時点で
    # 最新の発言が履歴に入らなくなるため。
    after = discord.Object(id=reset_message_id) if reset_message_id else None
    history_messages: list[discord.Message] = []
    async for history_message in channel.history(limit=limit, after=after, oldest_first=False):
        history_messages.append(history_message)
    return build_history_text(list(reversed(history_messages)), bot_user)


def build_summary_source_text(messages: Sequence[discord.Message], bot_user: Any) -> str:
    """議事録要約用に「発言者名つき」で会話ログを整形する。

    通常の履歴整形（build_history_text）は全員を「ユーザー/BOT」の2種類に潰すが、
    議事録では「誰が発言したか」が重要なため、実際の表示名を明示する。
    他のBOTの発言は除外し、自ボットの発言は会話の流れとして含める。
    """
    lines: list[str] = []
    for history_message in messages:
        # 他のBOTの発言は議事録に含めない（人間の会話に集中する）。
        # 自ボット自身の発言は文脈が繋がるように含める。
        if history_message.author.bot and history_message.author != bot_user:
            continue
        content = clean_message_content(history_message, bot_user)
        if history_message.attachments:
            content += " [添付ファイルあり]"
        # スタンプや埋め込みのみで本文が空の発言はノイズになるためスキップ
        if not content.strip():
            continue
        speaker = "（このBOT）" if history_message.author == bot_user else history_message.author.display_name
        # created_at は UTC。astimezone() で実行環境のローカル時刻（日本ならJST）に変換する。
        timestamp = history_message.created_at.astimezone().strftime("%m/%d %H:%M")
        lines.append(f"[{timestamp}] {speaker}: {content}")
    return "\n".join(lines)


async def fetch_messages_since(
    channel: Any,
    after_time: datetime,
    max_messages: int,
) -> list[discord.Message]:
    """指定時刻以降のメッセージを古い順で取得する。

    上限超過を判定できるよう、あえて max_messages + 1 件まで取得する。
    戻り値の件数が max_messages を超えていたら「多すぎ」と判断できる。
    """
    collected: list[discord.Message] = []
    async for history_message in channel.history(limit=max_messages + 1, after=after_time, oldest_first=True):
        collected.append(history_message)
    return collected


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
            # 区切り文字が見つからない場合は強制カット（切った文字も残す）
            chunks.append(text[:limit])
            text = text[limit:]
            continue

        # split_pos の文字は区切り文字(改行またはスペース)なので次チャンクからは除外する
        chunks.append(text[:split_pos])
        text = text[split_pos + 1:].lstrip("\n ")

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
    """グラウンディングソースをコンパクトなテキストに整形する。

    グラウンディングの URI はリダイレクト用の無機質なアドレスのため、
    タイトルがあれば `[タイトル](URL)` 形式でリンク先が分かるように表示する。
    """
    if not sources:
        return ""
    lines: list[str] = []
    for source in sources:
        uri = source.get("uri", "")
        # Markdown リンクの構文を壊さないよう、タイトル中の角括弧は全角に置換する
        title = source.get("title", "").replace("[", "［").replace("]", "］")
        if title:
            lines.append(f"・ [{title}](<{uri}>)")
        else:
            lines.append(f"・ <{uri}>")
    links = "\n".join(lines)
    return f"\n\n🔍 **参考リンク:**\n{links}"

