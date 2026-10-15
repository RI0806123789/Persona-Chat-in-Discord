import asyncio
import sys

import discord
from discord import app_commands
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

from app_state import BotState, ResponseSettings
from affinity_service import AffinityService, extract_affinity_changes
from auto_topic_service import AutoTopicService
from autonomous_service import AutonomousService
from background import make_guarded_loop, spawn
from blacklist_service import BlacklistService
from commands import register_commands
from constants import (
    ANNIVERSARY_CHECK_MINUTES,
    AUTO_TOPIC_CHECK_MINUTES,
    AUTONOMOUS_CHECK_MINUTES,
    BLACKLIST_MAX_VIOLATIONS,
    DATABASE_PATH,
    FEEDBACK_PRUNE_MINUTES,
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
    post_welcome_message,
    send_long_reply,
)
from document_service import DocumentService
from feedback_service import FeedbackService, set_default_service
from gemini_service import GeminiService
from help_view import build_welcome_text, resolve_bot_name
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
autonomous = AutonomousService(client, state, gemini, moderator, ng_filter, usage)
reminders = ReminderService(client, database, ng_filter)
feedback = FeedbackService(client, database)
# 集計パネル（会話統計 → 👍 フィードバック）は機能ハブ経由で生成されるため、
# UI 層がこのインスタンスを引けるよう既定として登録しておく。
set_default_service(feedback)

register_commands(tree, state, usage, ng_filter, blacklist, gemini, moderator, stats, reminders, autonomous)


# --- 定期実行の見回りループ ---
# どれも「間隔を指定 / 接続完了を待つ / 本体を try-except で包んでログを出す」の
# 3点セットなので、background.make_guarded_loop にまとめて作らせる
# （生成するだけで開始はしないため、start() は下の on_ready で行う）。
memory_maintenance_loop = make_guarded_loop(
    memory.flush_if_needed,
    label="バックグラウンド記憶処理エラー",
    client=client,
    minutes=MEMORY_MAINTENANCE_MINUTES,
)

auto_topic_loop = make_guarded_loop(
    auto_topic.run_pending_checks,
    label="自発的な会話の見回りエラー",
    client=client,
    minutes=AUTO_TOPIC_CHECK_MINUTES,
)

anniversary_loop = make_guarded_loop(
    auto_topic.run_anniversary_checks,
    label="記念日チェックエラー",
    client=client,
    minutes=ANNIVERSARY_CHECK_MINUTES,
)

# 自律行動（ボイス/ステージでの配信）の見回り。配信の開始判定だけでなく、
# 独り言の投下・無応答での退出まで面倒を見るため、他の見回りより短い間隔で回す
# （生成そのものは裏のタスクへ逃がすので、このループ自体はすぐ返る）。
autonomous_loop = make_guarded_loop(
    autonomous.run_pending_checks,
    label="自律行動の見回りエラー",
    client=client,
    minutes=AUTONOMOUS_CHECK_MINUTES,
)

# リマインダーは分単位の指定に応えるため、記念日（時間単位）よりずっと短い間隔で見回る。
reminder_loop = make_guarded_loop(
    reminders.run_due_checks,
    label="リマインダー確認エラー",
    client=client,
    seconds=REMINDER_CHECK_SECONDS,
)

# フィードバック記録の間引き。以前は評価が押されるたびに実行していたが、
# DB のロックを握って主経路の返信まで遅らせていたためここへ移した。
# tasks.loop は start() 直後に1回目を実行するので、起動時の間引きも兼ねる。
feedback_prune_loop = make_guarded_loop(
    feedback.prune_stored,
    label="フィードバック記録の整理エラー",
    client=client,
    minutes=FEEDBACK_PRUNE_MINUTES,
)


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

    if not state.autonomous_background_started:
        autonomous_loop.start()
        state.autonomous_background_started = True

    if not state.reminder_background_started:
        reminder_loop.start()
        state.reminder_background_started = True

    if not state.feedback_prune_background_started:
        feedback_prune_loop.start()
        state.feedback_prune_background_started = True

    if not state.commands_synced:
        await tree.sync()
        state.commands_synced = True


@client.event
async def on_guild_join(guild: discord.Guild) -> None:
    """新しいサーバーへ招待されたときに、使い方の案内を1回だけ投稿する。

    「メンションしないと反応しない」ことも「機能が /functions に集約されている」
    ことも、招待しただけでは誰も気づけない。最初の一歩をここで案内する。

    投稿先の選定・連投防止・送信は discord_helpers.post_welcome_message が
    まとめて担い、本文は help_view が組み立てる（ヘルプと同じ文言を使うため）。
    ここでの try/except は、本文の組み立てなど想定外の失敗まで含めて
    案内の失敗でイベント処理を落とさないための最後の受け皿
    （案内が出せなくてもボット自体は普通に使えるのでフェイルオープンにする）。
    必要な guilds インテントは Intents.default() に含まれるため設定変更は不要。
    """
    try:
        welcome_text = build_welcome_text(resolve_bot_name(client.user))
        await post_welcome_message(guild, welcome_text, state.welcomed_guild_ids)
    except Exception as error:
        print(f"ウェルカムメッセージの処理エラー（guild={guild.id}）: {error}")


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
    # 自律行動の「話したいゲージ」も、ボット宛てでない発言を含めて観測する
    # （このサーバーで会話が行われているかどうかがゲージの材料のため）。
    # 引数の順番は auto_topic 側と同じ (channel_id, guild_id) に揃えてある。
    autonomous.mark_activity(message.channel.id, guild_id)

    # 通常のチャット経路が受け付ける条件（応答モードのゲート）。
    # ルーティングでも同じ条件を使うため、判定を1か所にまとめて持ち回す。
    accepts_normally = (
        is_dm
        or guild_settings.current_respond_mode != "mention"
        or client.user in message.mentions
    )
    # 配信中のボイス/ステージチャンネル付属のチャットへの書き込みは「コメント」として
    # 扱い、メンションが無くても拾う（傍観者は配信のチャットに気軽に書き込むもので、
    # いちいちメンションを求めるのは配信の体裁に合わないため）。他のテキストチャンネル
    # からの発言はコメントとして認めない（idea.md）ので、判定はチャンネル一致で行う。
    is_autonomous_comment = autonomous.is_comment_channel(message.channel.id, guild_id)
    if not accepts_normally and not is_autonomous_comment:
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
    # 配信中のコメント欄も対象外にする。ここで登録してしまうと、配信が終わった後も
    # そのボイスチャンネルのチャットが自発的な話題提供の監視対象として残り続け、
    # 「配信のコメント欄」だった場所へ延々とテキストの話題が投稿される。
    if not is_dm and not is_autonomous_comment:
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

    # 配信中のコメントは、通常のチャットではなく配信者としての応答へ回す。
    # 長期記憶・親密度・👍👎 の登録を行わない点が handle_chat_message と違う
    # （理由は autonomous_service のモジュール冒頭の説明を参照）。
    #
    # **ここで配信中かどうかを取り直すこと。** 入口で調べてからモデレーション
    # （Gemini 呼び出し）を挟んでいる間に配信が終わっていることがある。古い判定の
    # まま handle_comment を呼ぶと、セッションが無いので None が返り、通常経路にも
    # 回らないまま発言が黙って捨てられる（メンション付きの発言まで無視されてしまう）。
    if autonomous.is_comment_channel(message.channel.id, guild_id):
        response_text = await autonomous.handle_comment(message, question, guild_settings)
        tts_prompt_file = guild_settings.current_prompt_file
    elif accepts_normally:
        # 配信が終わっていても、通常のチャットとして受け付けられる発言なら普通に答える。
        response_settings = guild_settings.snapshot_response()
        response_text = await handle_chat_message(
            message, question, memory_category, response_settings=response_settings
        )
        tts_prompt_file = response_settings.prompt_file
    else:
        # 配信の終了と入れ違いに届いた、メンションの無い発言。配信が続いていれば
        # コメントとして拾えたが、終わった今は通常経路の条件も満たさないので何もしない。
        print("自律行動: 配信の終了と入れ違いに届いた発言のため、応答しません。")
        return
    if response_text:
        # ボットが /join でボイスチャンネルに接続中のときだけ読み上げる（それ以外は no-op）。
        # 設定が生成中に変わっても、今回の応答に使ったペルソナの声で読み上げる。
        await play_tts_response(message, response_text, tts_prompt_file)


async def handle_chat_message(
    message: discord.Message,
    question: str,
    memory_category: str = "none",
    *,
    response_settings: ResponseSettings | None = None,
) -> str | None:
    guild_id = message.guild.id if message.guild is not None else None
    guild_settings = state.get_guild(guild_id)
    # typing() の開始も待機を伴うため、その前に今回の値を確定する。
    if response_settings is None:
        response_settings = guild_settings.snapshot_response()
    async with message.channel.typing():
        memory.ensure_storage()

        user_id = str(message.author.id)
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
            prompt = load_prompt_template(resolve_prompt_path(response_settings.prompt_file))
        except FileNotFoundError:
            print(f"エラー: {response_settings.prompt_file} が見つかりません。")
            await message.reply(f"エラー: プロンプトファイル({response_settings.prompt_file})が見つかりませんでした。")
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
            response_settings.model_name,
            image_objects,
            timeout=GEMINI_RESPONSE_TIMEOUT_SECONDS,
            grounding=response_settings.grounding_enabled,
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

        sent = await send_long_reply(message, reply_text, suppress_embeds=bool(grounding_sources))
        # 送った返信を 👍👎 の評価対象として登録する（リアクション付与は裏で走る）。
        # 生成中・送信中に設定が変わっても、今回の生成に使った値を渡す。
        feedback.track_reply(
            sent,
            target_user_id=user_id,
            model_name=response_settings.model_name,
            prompt_file=response_settings.prompt_file,
        )
        await memory.append_pending_log(user_id, question, response_text)
        spawn(memory.flush_if_needed(), label="記憶圧縮（チャット便乗）")
        return response_text


@client.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent) -> None:
    """ボット返信への 👍/👎 を評価として記録する。

    on_reaction_add ではなく raw 版を使う（前者はメッセージがキャッシュに
    載っている間しか発火せず、少し前の返信への評価を取りこぼすため）。
    このイベントはボット自身が付けた初期リアクションでも発火するが、
    その除外はサービス側（_resolve_vote_target）が行う。
    必要な reactions インテントは Intents.default() に含まれるため設定変更は不要。
    """
    await feedback.handle_reaction_add(payload)


@client.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent) -> None:
    """👍/👎 が外されたときに、その評価の記録を取り消す。"""
    await feedback.handle_reaction_remove(payload)


def main() -> None:
    client.run(settings.TOKEN_DISCORD)


if __name__ == "__main__":
    main()
