import io
import re
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any, Callable, Sequence, TypeGuard

import discord
from PIL import Image

from persona_bot.config.constants import (
    DISCORD_MESSAGE_MAX_LENGTH,
    IMAGE_EXTENSIONS,
    MAX_IMAGE_SIZE_BYTES,
    SUMMARY_TIMEZONE,
    SUPPORTED_DOCUMENT_EXTENSIONS,
    WELCOME_MESSAGE_ENABLED,
)
from persona_bot.utils.timeutil import resolve_timezone
from persona_bot.core.background import run_in_thread

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


def describe_supported_attachments(*, documents: bool = True) -> str:
    """対応している添付の拡張子を、利用者向けの案内文として返す。

    案内に拡張子を直書きすると、受け付ける形式を変えたときに文言だけが古いまま残るため、
    判定に使っている IMAGE_EXTENSIONS / SUPPORTED_DOCUMENT_EXTENSIONS から組み立てる
    （集合は順序を持たないので、表示順を固定するために並べ替える）。
    """
    def join(extensions: set[str]) -> str:
        return " / ".join(sorted(extension.lstrip(".") for extension in extensions))

    text = f"画像（{join(IMAGE_EXTENSIONS)}）"
    if documents:
        text += f"と文書（{join(SUPPORTED_DOCUMENT_EXTENSIONS)}）"
    return text


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
        # 画像と文書などを区別して書く（すべて「画像」と書くと、PDF を添えた発言まで
        # モデルが画像付きと誤解して応答してしまう）。
        if any(attachment_is_image(attachment) for attachment in history_message.attachments):
            clean_content += " [画像添付あり]"
        if not all(attachment_is_image(attachment) for attachment in history_message.attachments):
            clean_content += " [添付ファイルあり]"
        lines.append(f"{role}: {clean_content}")
    return "\n".join(lines)


async def fetch_channel_history_text(
    channel: Any,
    reset_message_id: int | None,
    bot_user: Any,
    limit: int,
) -> str:
    """チャンネルの直近履歴を、プロンプトへ差し込める1つのテキストに整形する。

    履歴の取得に失敗した場合は例外を外へ出さず、空文字を返して会話を続行する
    （フェイルオープン）。この関数は on_message の主経路から呼ばれるため、
    例外を外へ漏らすと handle_chat_message → on_message と素通りして
    discord.py の既定 on_error がログに吐くだけになり、ユーザーには入力中
    インジケータが消えるだけで何も返らない。履歴が無くても会話は成立するので
    フェイルオープンにする。
    """
    # 常に新しい順で直近 limit 件を取得する。リセットポイント指定時に古い順で
    # 取得すると、リセット以降のメッセージが limit を超えた時点で
    # 最新の発言が履歴に入らなくなるため。
    after = discord.Object(id=reset_message_id) if reset_message_id else None
    history_messages: list[discord.Message] = []
    channel_id = getattr(channel, "id", "不明")
    try:
        async for history_message in channel.history(limit=limit, after=after, oldest_first=False):
            history_messages.append(history_message)
    # Forbidden は HTTPException の派生クラスなので、必ず先に捕まえること
    # （順序を逆にすると権限エラーが一時障害として扱われ、原因がログから消える）。
    except discord.Forbidden:
        print(f"履歴の取得権限がありません（チャンネルID: {channel_id}）。「メッセージ履歴を読む」権限を確認してください。履歴なしで応答します。")
        return ""
    except discord.HTTPException as error:
        print(f"履歴の取得に失敗しました（チャンネルID: {channel_id}）。履歴なしで応答します: {error}")
        return ""
    return build_history_text(list(reversed(history_messages)), bot_user)


def build_summary_source_text(
    messages: Sequence[discord.Message],
    bot_user: Any,
    tz: tzinfo | None = None,
) -> str:
    """議事録要約用に「発言者名つき」で会話ログを整形する。

    通常の履歴整形（build_history_text）は全員を「ユーザー/BOT」の2種類に潰すが、
    議事録では「誰が発言したか」が重要なため、実際の表示名を明示する。
    他のBOTの発言は除外し、自ボットの発言は会話の流れとして含める。

    Args:
        tz: 時刻表記に使うタイムゾーン。None なら SUMMARY_TIMEZONE を解決して使う。
            実行環境のローカル時刻（astimezone() の既定）に頼ると、UTC 運用の VPS で
            議事録の時刻だけが9時間ずれる（統計・記念日・リマインダーは
            Asia/Tokyo 固定のため、そこだけ基準が食い違う）。
    """
    # 解決はメッセージごとではなく1回だけ行う（1件ごとに ZoneInfo を引くと無駄）。
    # ZoneInfo を直接呼ばず resolve_timezone を通すこと（tzdata の無い環境では
    # 例外になるため、そこを UTC フォールバックで受けるのがこの関数の役割）。
    display_tz = tz if tz is not None else resolve_timezone(SUMMARY_TIMEZONE, purpose="会話要約の時刻表示")
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
        # created_at は UTC。表示用のタイムゾーン（既定は SUMMARY_TIMEZONE）へ明示的に変換する。
        timestamp = history_message.created_at.astimezone(display_tz).strftime("%m/%d %H:%M")
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


def _decode_image_bytes(image_bytes: bytes) -> Image.Image:
    """画像の展開とRGB変換をワーカーで行い、入力を閉じた後も使える画像を返す。"""
    with Image.open(io.BytesIO(image_bytes)) as image:
        return image.convert("RGB")


async def load_image_attachments(attachments: Sequence[discord.Attachment]) -> list[Image.Image]:
    image_objects: list[Image.Image] = []
    try:
        for attachment in attachments:
            if not attachment_is_image(attachment):
                continue
            if attachment.size > MAX_IMAGE_SIZE_BYTES:
                print("画像読み込み: サイズ上限を超えているため見送ります。")
                continue
            try:
                image_bytes = await attachment.read()
                if len(image_bytes) > MAX_IMAGE_SIZE_BYTES:
                    print("画像読み込み: 取得した画像がサイズ上限を超えているため見送ります。")
                    continue
                image_objects.append(await run_in_thread(_decode_image_bytes, image_bytes))
            except Exception as error:
                print(f"画像読み込みエラー: {error}")
        return image_objects
    except BaseException:
        # 中断時に受取先がなくなる画像も回収する。処理中の画像はrun_in_threadが回収する。
        close_image_attachments(image_objects)
        raise


def close_image_attachments(images: Sequence[Any]) -> None:
    """呼出元が所有する画像をすべて回収し、元の例外やキャンセルを保つ。"""
    for image in images:
        try:
            close = getattr(image, "close", None)
            if callable(close):
                close()
        except Exception as error:
            print(f"画像の解放に失敗しました: {type(error).__name__}")


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
        text = text[split_pos + 1:]

    return chunks


async def _send_chunks(
    channel: Any,
    chunks: Sequence[str],
    *,
    suppress_embeds: bool,
    allowed_mentions: discord.AllowedMentions | None = None,
) -> None:
    """分割済みチャンクを順にチャンネルへ送信する（送信系ヘルパーの共通部分）。

    `allowed_mentions` は None のままなら Discord クライアントの既定に従う
    （＝これまでどおりの挙動）。`AllowedMentions.none()` を渡すと通知を一切
    飛ばさずに投稿できる。ボットが自分から投稿する文面（自律行動の独り言など）は
    誰かへの返信ではないため、通知を伴わせない用途で使う。
    """
    for chunk in chunks:
        await channel.send(chunk, suppress_embeds=suppress_embeds, allowed_mentions=allowed_mentions)


async def send_long_reply(
    message: discord.Message,
    text: str,
    *,
    suppress_embeds: bool = False,
) -> discord.Message | None:
    """2000 文字を超える応答を分割して送信する。

    最初のチャンクは元メッセージへの reply、残りは通常の send で送信する。
    suppress_embeds が True の場合、リンクの埋め込みプレビューを抑制する。

    戻り値は**1通目**（元メッセージへの reply）。フィードバックのリアクション
    付与など、送信したメッセージを後から参照する用途に使う。2通目以降は
    元の質問への紐付けが無い channel.send なので返さない
    （評価対象は「1つの応答」であり、分割は表示上の都合にすぎないため）。
    戻り値を使わない呼び出しはそのままでよい。
    """
    # split_message は空文字でも1要素を返すため chunks[0] は必ず存在する。
    chunks = split_message(text)
    sent = await message.reply(chunks[0], suppress_embeds=suppress_embeds)
    await _send_chunks(message.channel, chunks[1:], suppress_embeds=suppress_embeds)
    return sent


async def send_long_message(
    channel: Any,
    text: str,
    *,
    suppress_embeds: bool = False,
    allowed_mentions: discord.AllowedMentions | None = None,
) -> None:
    """2000 文字を超えるテキストを分割してチャンネルへ送信する（reply しない版）。

    自発的な会話・記念日のお祝い・リマインダー通知・会話要約・自律行動の
    独り言のように「特定のメッセージへの返信ではない投稿」で使う。
    以前は `for chunk in split_message(...): await channel.send(chunk)` が
    4か所へ手書きコピーされており、send_long_reply が持つ suppress_embeds の
    考慮がそちらには無かった。リンク抑制などを足すときに触る場所を
    1か所に保つため集約している。
    """
    await _send_chunks(
        channel,
        split_message(text),
        suppress_embeds=suppress_embeds,
        allowed_mentions=allowed_mentions,
    )


def pick_announcement_channel(guild: discord.Guild) -> Any:
    """サーバー全体へのお知らせを投稿できるテキストチャンネルを1つ選ぶ。

    優先順位:
      1. システムチャンネル（サーバー側で「お知らせ用」に指定されている場所）
      2. `guild.text_channels` を順に見て、最初に送信できるチャンネル
      3. どこにも送れなければ None（呼び出し側は投稿を諦める）

    走査に `guild.text_channels` を使うのは、`guild.channels` だとカテゴリや
    ボイスチャンネルまで混ざるため（position 順に並んだテキストチャンネル
    だけが欲しい）。

    権限は必ず `channel.permissions_for(guild.me)` で**チャンネル単位に**
    確認する。サーバー全体のロールで許可されていても、チャンネル個別の設定で
    拒否されていることがあるため（`/join` の接続権限チェックと同じ作法）。
    「送ってみて失敗したら次」という例外頼みにすると、投稿できないチャンネルの
    数だけ無駄に API を叩くうえ、権限エラーとネットワーク障害を区別できない。
    """
    bot_member = guild.me
    if bot_member is None:
        # キャッシュ未取得などで自分自身の Member が引けないときは権限を判定できない。
        return None

    system_channel = guild.system_channel
    if system_channel is not None and can_bot_send_messages(system_channel, bot_member):
        return system_channel

    for channel in guild.text_channels:
        if can_bot_send_messages(channel, bot_member):
            return channel
    return None


async def post_welcome_message(
    guild: discord.Guild,
    body: str,
    posted_guild_ids: set[int],
) -> bool:
    """サーバー参加時の案内を、そのサーバーへ1回だけ投稿する。

    投稿できたら True、投稿しなかった（無効化されている・投稿済み・投稿先が
    無い・送信に失敗した）場合は False を返す。**例外は外へ出さない** —
    招待そのものは成功させたいので、案内の失敗でイベント処理を落とさない。

    Args:
        guild: 招待されたサーバー。
        body: 投稿する本文（`help_view.build_welcome_text` の戻り値）。
        posted_guild_ids: 投稿済みサーバーIDの集合（`BotState.welcomed_guild_ids`）。
            この関数が投稿の直前に書き込む。

    メンションは `AllowedMentions.none()` で完全に無効化する。本文には
    `@everyone` を書かない方針だが、ボット名を差し込む都合で万一 `@` を
    含む文字列が入っても通知爆撃にならないよう、送信側でも止めておく。
    """
    if not WELCOME_MESSAGE_ENABLED:
        return False
    if guild.id in posted_guild_ids:
        # 同一プロセス内でイベントが重複して届いた場合の保険（連投防止）。
        return False

    channel = pick_announcement_channel(guild)
    if channel is None:
        print(f"ウェルカムメッセージを投稿できるチャンネルが見つかりませんでした（guild={guild.id}）。")
        return False

    # 送信を待つ前に「投稿済み」を立てる。ここから送信までの間に await を
    # 挟まないため、イベントが重複しても2件目は上の判定で確実に弾かれる
    # （送信後に立てると、送信の待ち時間に入ってきた2件目が二重投稿になる）。
    posted_guild_ids.add(guild.id)

    try:
        await channel.send(body, allowed_mentions=discord.AllowedMentions.none())
    # Forbidden は HTTPException の派生クラスなので、必ず先に捕まえること。
    except discord.Forbidden:
        print(f"ウェルカムメッセージの送信を拒否されました（guild={guild.id} / channel={channel.id}）。")
        return False
    except discord.HTTPException as error:
        print(f"ウェルカムメッセージの送信に失敗しました（guild={guild.id}）: {error}")
        return False

    print(f"ウェルカムメッセージを投稿しました（guild={guild.id} / channel={channel.id}）。")
    return True


def is_administrator(user: Any) -> bool:
    """そのユーザーがサーバーの管理者権限を持つかを判定する。

    この判定は「他人の会話統計の閲覧可否」「ブロック解除の可否」という
    セキュリティ境界であり、条件を1か所に固定するためここへ集約している
    （各所へコピーすると、片方だけ条件が緩んだときに気づけない）。

    DM では discord.Member ではなく discord.User が渡るため False を返す
    （＝DM では管理者機能を使えない）。DM にはサーバーが無く、
    guild_permissions で権限を確認できないため。
    """
    return isinstance(user, discord.Member) and user.guild_permissions.administrator


async def reject_blocked_interaction(
    interaction: discord.Interaction, is_blocked: Callable[[int], bool],
) -> bool:
    """ブロック中なら本人だけに拒否を通知し、処理を中止するためTrueを返す。"""
    if not is_blocked(interaction.user.id):
        return False
    await interaction.response.send_message(
        "利用が制限されているため、この操作は実行できません。", ephemeral=True,
    )
    return True


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



def can_bot_send_messages(channel: Any, member: Any) -> bool:
    """閲覧と、そのチャンネル種別に対応した送信権限を確認する。"""
    if member is None:
        return False
    permissions = channel.permissions_for(member)
    send = permissions.send_messages_in_threads if isinstance(channel, discord.Thread) else permissions.send_messages
    return bool(permissions.view_channel and send)


def channel_display_name(channel: Any) -> str:
    """名前のないチャンネルも識別可能な表示名にする。"""
    return getattr(channel, "name", None) or f"channel {channel.id}"
