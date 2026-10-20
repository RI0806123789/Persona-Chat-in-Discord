"""起動処理から分離したメッセージ処理。サービスは bot.py から注入する。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from persona_bot.services.affinity_service import extract_affinity_changes
from persona_bot.core.app_state import BotState, GuildSettings, ResponseSettings
from persona_bot.core.background import run_in_thread, spawn
from persona_bot.config.constants import (
    BLACKLIST_MAX_VIOLATIONS, GEMINI_RESPONSE_TIMEOUT_SECONDS,
    HISTORY_LIMIT, IMAGE_ONLY_QUESTION, MAX_DOCUMENT_SIZE_BYTES,
)
from persona_bot.services.content_moderator import screen_bot_response
from persona_bot.utils.discord_helpers import (
    attachment_is_image, clean_message_content, describe_supported_attachments,
    fetch_channel_history_text, format_grounding_sources, is_supported_document_attachment,
    close_image_attachments, is_connected_voice_chat, is_supported_message_channel,
    load_image_attachments, send_long_reply,
)
from persona_bot.config.paths import load_persona_template
from persona_bot.services.prompting import build_full_prompt, build_memory_sections
from persona_bot.audio.voice import play_tts_response

if TYPE_CHECKING:
    from persona_bot.services.affinity_service import AffinityService
    from persona_bot.services.auto_topic_service import AutoTopicService
    from persona_bot.services.autonomous_service import AutonomousService
    from persona_bot.services.blacklist_service import BlacklistService
    from persona_bot.services.content_moderator import ContentModerator
    from persona_bot.services.document_service import DocumentService
    from persona_bot.services.feedback_service import FeedbackService
    from persona_bot.services.gemini_service import GeminiService
    from persona_bot.services.memory_service import MemoryService
    from persona_bot.services.ng_word_service import NgWordFilter
    from persona_bot.services.reminder_service import ReminderService
    from persona_bot.services.stats_service import StatsService
    from persona_bot.utils.usage_graph import UsageTracker


@dataclass
class MessageHandler:
    """受信時の関門と応答処理を、明示的に渡されたサービスで実行する。"""

    client: discord.Client
    state: BotState
    auto_topic: AutoTopicService
    autonomous: AutonomousService
    blacklist: BlacklistService
    ng_filter: NgWordFilter
    moderator: ContentModerator
    stats: StatsService
    reminders: ReminderService
    documents: DocumentService
    memory: MemoryService
    affinity: AffinityService
    gemini: GeminiService
    usage: UsageTracker
    feedback: FeedbackService

    async def notify_violation(self, message: discord.Message, notice: str) -> None:
        """違反を1回記録し、ブロック状況に応じた警告をユーザーに返信する。

        記録の成否はログにも残す。「NGワード検出」のログは記録より前に出るため、
        それだけでは違反が数えられたのか分からない（#173）。
        """
        user_id = message.author.id
        try:
            count, just_blocked = await self.blacklist.record_violation(user_id, str(message.author))
        except Exception as error:
            # ★ 保存できなかった違反は回数に数えない。BlacklistService は保存に成功してから
            # メモリ上の記録を差し替えるため、ここで失敗した違反はどこにも残っていない。
            # 「あと N 回」は事実と食い違うので出さず、発言を処理しないことだけを伝える。
            # 再試行もしない（同じ障害で失敗し続ける保存を、発言のたびに積み増さないため）。
            print(
                f"違反記録: 保存に失敗したため、今回の違反は回数に数えていません "
                f"(user_id={user_id}): {type(error).__name__}: {error}"
            )
            await message.reply(f"⚠️ {notice}")
            return
        if just_blocked:
            print(f"違反記録: {count}回目の違反でブラックリストに登録しました (user_id={user_id})")
            await message.reply(
                f"🚫 違反が{BLACKLIST_MAX_VIOLATIONS}回に達したため、ブラックリストに登録されました。"
                "以降のメッセージは無視されます。"
            )
        elif count >= BLACKLIST_MAX_VIOLATIONS:
            # 並行して届いた別の違反で既にブロック済み（record_violation は回数を増やさず
            # just_blocked=False を返す）。そちらが 🚫 の案内を返しているため、「あと0回」の
            # ような食い違った警告も二重の案内も出さない（ブロック中の発言は黙って破棄する）。
            print(f"違反記録: 既にブロック済みのため警告を省略します (user_id={message.author.id})")
        else:
            print(f"違反記録: {count}/{BLACKLIST_MAX_VIOLATIONS} 回目を記録しました (user_id={user_id})")
            remaining = BLACKLIST_MAX_VIOLATIONS - count
            await message.reply(f"⚠️ {notice}（あと{remaining}回で応答を停止します）")

    async def _reject_ng_input(self, message: discord.Message, question: str, notice: str) -> bool:
        """入力にNGワードがあれば違反として記録し、True を返す（Gemini は呼ばない）。"""
        if not question or not self.ng_filter.contains_ng_word(question):
            return False
        detected = self.ng_filter.find_ng_words(question)
        print(f"NGワード検出 (ユーザー入力): {detected}")
        await self.notify_violation(message, notice)
        return True

    async def on_message(self, message: discord.Message) -> None:
        if self.client.user is None:
            return
        if message.author == self.client.user or message.author.bot:
            return
        # ブロック中のユーザーは、活動の観測より前に黙って破棄する。後に置くと、無視している
        # はずの発言で自発会話の沈黙タイマーが戻り、自律配信の話したいゲージや無応答での
        # 退出まで左右されてしまう。
        if self.blacklist.is_blocked(message.author.id):
            return
        # 返信も履歴取得もできないチャンネル種別は、NG判定やモデレーション（Gemini 呼び出し）に
        # 進む前にここで打ち切る。後段に置くと、結局返信できない発言のために API を1回
        # 無駄打ちし、違反警告の message.reply() も失敗してしまう。
        channel = message.channel
        if not is_supported_message_channel(channel):
            return

        is_dm = isinstance(message.channel, discord.DMChannel)
        # DM はギルドに属さないため guild_id=None として「全DM共有」の設定スロットへ収束させる。
        guild_id = message.guild.id if message.guild is not None else None
        guild_settings = self.state.get_guild(guild_id)
        self.auto_topic.mark_activity(message.channel.id, guild_id)
        # 自律行動の「話したいゲージ」も、ボット宛てでない発言を含めて観測する
        # （このサーバーで会話が行われているかどうかがゲージの材料のため）。
        # 引数の順番は auto_topic 側と同じ (channel_id, guild_id) に揃えてある。
        self.autonomous.mark_activity(message.channel.id, guild_id)

        # 通常のチャット経路が受け付ける条件（応答モードのゲート）。
        # ルーティングでも同じ条件を使うため、判定を1か所にまとめて持ち回す。
        accepts_normally = (
            is_dm
            or guild_settings.current_respond_mode != "mention"
            or self.client.user in message.mentions
        )
        # 配信中のボイス/ステージチャンネル付属のチャットへの書き込みは「コメント」として
        # 扱い、メンションが無くても拾う（傍観者は配信のチャットに気軽に書き込むもので、
        # いちいちメンションを求めるのは配信の体裁に合わないため）。他のテキストチャンネル
        # からの発言はコメントとして認めない（idea.md）ので、判定はチャンネル一致で行う。
        is_autonomous_comment = self.autonomous.is_comment_channel(message.channel.id, guild_id)
        if not accepts_normally and not is_autonomous_comment:
            # 返信しない発言でも、ボットがいま接続しているボイス/ステージの付属チャットだけは
            # ローカルのNGワード判定にかける（#172）。「返信する条件」と「NG判定の対象」を分け、
            # ボットが居合わせている通話のチャットを見守る。AI判定は呼ばない（宛先でない
            # 発言ごとに Gemini を呼ぶとコストが発散する）。他のチャンネルへは広げない。
            if is_connected_voice_chat(message.guild, message.channel.id):
                await self._reject_ng_input(
                    message, clean_message_content(message, self.client.user), "NGワードが含まれています。",
                )
            return

        question = clean_message_content(message, self.client.user)
        if not question and not message.attachments:
            return

        if await self._reject_ng_input(message, question, "NGワードが含まれているため、メッセージを処理できません。"):
            return

        # 本文の無いコメントはデコードまで確認し、使えない添付で受付枠を消費しない。
        image_objects = None
        if is_autonomous_comment and not question:
            image_objects = await load_image_attachments(message.attachments)
        try:
            if image_objects is not None and not image_objects:
                return
            await self._process_accepted_message(
                message, question, guild_settings, accepts_normally=accepts_normally,
                is_autonomous_comment=is_autonomous_comment, image_objects=image_objects,
            )
        finally:
            if image_objects is not None:
                close_image_attachments(image_objects)

    async def _process_accepted_message(
        self, message: discord.Message, question: str, guild_settings: GuildSettings, *,
        accepts_normally: bool, is_autonomous_comment: bool, image_objects: list | None,
    ) -> None:
        guild_id = message.guild.id if message.guild is not None else None
        is_dm = isinstance(message.channel, discord.DMChannel)
        # コメントの受付枠はGeminiを呼ぶ前に、同期的に確保する。
        # 見送る分も活動観測は上の mark_activity で済ませている。
        claimed_comment_session = None
        if is_autonomous_comment and self.autonomous.is_comment_channel(message.channel.id, guild_id):
            claimed_comment_session = self.autonomous.claim_comment_slot(message.channel.id, guild_id)
            if claimed_comment_session is None:
                return

        # 安全判定・記憶カテゴリ・リマインダー依頼を1回のAPI呼び出しで取得する。
        # memory_categoryは通常チャットの記憶参照へ、reminder_requestは添付の振り分け後の登録へ渡す。
        memory_category = "none"
        reminder_request: dict[str, str] | None = None
        if question:
            is_safe, _reason, memory_category, reminder_request = await self.moderator.check_and_route(question)
            if not is_safe:
                await self.notify_violation(message, "不適切な内容が検出されたため、メッセージを処理できません。")
                return

        self.state.touch()

        # （DM は「テキストチャンネルへの話題提供」の対象外とする）。
        # 配信中のコメント欄も対象外にする。ここで登録してしまうと、配信が終わった後も
        # そのボイスチャンネルのチャットが自発的な話題提供の監視対象として残り続け、
        # 「配信のコメント欄」だった場所へ延々とテキストの話題が投稿される。
        if not is_dm and not is_autonomous_comment:
            self.auto_topic.watch_channel(message.channel.id, guild_id)

        # モデレーション後の配信状態を取り直し、通常チャットの副作用より先に振り分ける。
        if self.autonomous.is_comment_channel(message.channel.id, guild_id):
            comment_session = guild_settings.autonomous_session
            response_settings = guild_settings.snapshot_response()
            response_text = await self.autonomous.handle_comment(
                message, question, guild_settings, claimed_session=claimed_comment_session,
                response_settings=response_settings, image_objects=image_objects,
            )
            if response_text and self.autonomous.can_speak_session(message.guild, guild_settings, comment_session):
                await play_tts_response(
                    message, response_text, response_settings.prompt_file,
                    is_valid=lambda: self.autonomous.can_speak_session(message.guild, guild_settings, comment_session),
                )
            return
        if not accepts_normally:
            print("自律行動: 配信の終了と入れ違いに届いた発言のため、応答しません。")
            return

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
            await message.reply(f"未対応の添付ファイルが含まれています。{describe_supported_attachments()}に対応しています。")
            return

        if document_attachments:
            for attachment in document_attachments:
                if attachment.size > MAX_DOCUMENT_SIZE_BYTES:
                    await message.reply(f"ファイルサイズが上限({MAX_DOCUMENT_SIZE_BYTES // (1024 * 1024)}MB)を超えています。")
                    return

            await self.documents.enqueue_from_message(message, question, document_attachments)
            await message.reply("受付が完了しました。順番に処理しています…")
            return

        # --- 会話統計の記録（ボットへの発言のみを集計。失敗してもチャット本体は止めない） ---
        spawn(
            self.stats.record(
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
            confirmation = await self.reminders.create_from_request(message, reminder_request)
            if confirmation:
                await message.reply(confirmation)
                return

        response_settings = guild_settings.snapshot_response()
        response_text = await self.handle_chat_message(
            message, question, memory_category, response_settings=response_settings
        )
        if response_text:
            # ボイス接続中なら、自律行動による参加中も読み上げる（未接続なら no-op）。
            # 設定が生成中に変わっても、今回の応答に使ったペルソナの声で読み上げる。
            await play_tts_response(message, response_text, response_settings.prompt_file)

    async def handle_chat_message(
        self,
        message: discord.Message,
        question: str,
        memory_category: str = "none",
        *,
        response_settings: ResponseSettings | None = None,
    ) -> str | None:
        guild_id = message.guild.id if message.guild is not None else None
        guild_settings = self.state.get_guild(guild_id)
        # typing() の開始も待機を伴うため、その前に今回の値を確定する。
        if response_settings is None:
            response_settings = guild_settings.snapshot_response()
        async with message.channel.typing():
            user_id = str(message.author.id)
            image_objects = await load_image_attachments(message.attachments)
            try:
                if not question:
                    if not image_objects:
                        # 画像だけの発言で、どの画像も読み込めなかった。質問も画像も無いまま
                        # Gemini を呼ぶと、見えていない画像への反応を作り話で返してしまうため断る。
                        await message.reply(
                            "画像を読み込めませんでした。"
                            f"{describe_supported_attachments(documents=False)}の画像か確認して、もう一度送ってください。"
                        )
                        return None
                    question = IMAGE_ONLY_QUESTION

                history_text = await fetch_channel_history_text(
                    message.channel,
                    guild_settings.channel_reset_points.get(message.channel.id),
                    self.client.user,
                    HISTORY_LIMIT,
                )
                # 記憶カテゴリは on_message のモデレーション統合判定で既に決まっている
                # （追加の Gemini 呼び出しはしない）。ユーザー別の記憶スペースを読み込む。
                memory_context, document_context = await self.memory.build_contexts(memory_category, user_id)

                try:
                    prompt = await load_persona_template(response_settings.prompt_file)
                except FileNotFoundError:
                    print(f"エラー: {response_settings.prompt_file} が見つかりません。")
                    await message.reply(f"エラー: プロンプトファイル({response_settings.prompt_file})が見つかりませんでした。")
                    return None

                full_prompt = build_full_prompt(
                    prompt,
                    build_memory_sections(memory_category, memory_context, document_context),
                    history_text,
                    question,
                    affinity_prompt=self.affinity.build_dynamic_prompt(str(message.author.id))
                )
                response_text, usage_info, grounding_sources = await self.gemini.generate(
                    full_prompt,
                    response_settings.model_name,
                    image_objects,
                    timeout=GEMINI_RESPONSE_TIMEOUT_SECONDS,
                    grounding=response_settings.grounding_enabled,
                )

                if response_text is None:
                    await message.reply("エラーが発生しました。（Gemini APIの呼び出しに失敗、または非対応の画像形式です）")
                    return None

                self.usage.log_snapshot("直接会話", question, f"画像添付: {len(image_objects)}枚", full_prompt, response_text, usage_info)

                # --- タグ抽出とAffinity更新 ---
                response_text, emotion_changes = extract_affinity_changes(response_text)
                if emotion_changes is not None:
                    v_change, a_change = emotion_changes
                    self.affinity.update_emotion(str(message.author.id), v_change, a_change)
                    spawn(self.affinity.save_background(user_id), label="親密度の保存")

                # タグを除去した結果、本文が空になった場合は空メッセージ送信を防ぐ
                # （モデルが感情タグだけを返したケース）。
                if not response_text.strip():
                    print("応答本文が空です（感情タグのみ、または空応答）。")
                    await message.reply("申し訳ありません、うまく応答を生成できませんでした。もう一度お試しください。")
                    return None

                response_text = await screen_bot_response(response_text, self.ng_filter, self.moderator, log_label="Bot応答")

                # 参考リンクは送信用テキストにのみ追記する
                # （TTS 読み上げと長期記憶には URL の列挙はノイズになるため含めない）。
                reply_text = response_text
                if grounding_sources:
                    reply_text += format_grounding_sources(grounding_sources)

                sent = await send_long_reply(message, reply_text, suppress_embeds=bool(grounding_sources))
                # 送った返信を 👍👎 の評価対象として登録する（リアクション付与は裏で走る）。
                # 生成中・送信中に設定が変わっても、今回の生成に使った値を渡す。
                self.feedback.track_reply(
                    sent,
                    target_user_id=user_id,
                    model_name=response_settings.model_name,
                    prompt_file=response_settings.prompt_file,
                )
                await self.memory.append_pending_log(user_id, question, response_text)
                spawn(self.memory.flush_if_needed(), label="記憶圧縮（チャット便乗）")
                return response_text
            finally:
                close_image_attachments(image_objects)
