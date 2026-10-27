"""起動処理から分離したメッセージ処理。サービスは bot.py から注入する。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from persona_bot.services.affinity_service import extract_affinity_changes
from persona_bot.core.app_state import BotState, GuildSettings, ResponseSettings
from persona_bot.core.background import run_in_thread, spawn
from persona_bot.core.chat_admission import ChatAdmission
from persona_bot.config.constants import (
    AGENT_ROUTER_HISTORY_LIMIT, BLACKLIST_MAX_VIOLATIONS, GEMINI_RESPONSE_TIMEOUT_SECONDS,
    DOCUMENT_QUEUE_FULL_NOTICE, HISTORY_LIMIT, IMAGE_ONLY_QUESTION, MAX_DOCUMENT_SIZE_BYTES,
)
from persona_bot.config.personas import current_persona_catalog
from persona_bot.services.agent_plan import AgentPlan, requests_subagent
from persona_bot.services.agent_service import render_agent_line
from persona_bot.services.content_moderator import screen_bot_response_with_status
from persona_bot.utils.discord_helpers import (
    attachment_is_image, clean_message_content, describe_supported_attachments,
    fetch_channel_history_text, format_grounding_sources, is_supported_document_attachment,
    close_image_attachments, is_connected_voice_chat, is_supported_message_channel,
    load_image_attachments, send_long_reply,
)
from persona_bot.config.paths import load_persona_template
from persona_bot.services.prompting import (
    build_agent_declined_section, build_agent_report_section, build_full_prompt, build_memory_sections,
)
from persona_bot.audio.voice import play_tts_response
from persona_bot.views.agent_view import AgentProgress

if TYPE_CHECKING:
    from persona_bot.services.affinity_service import AffinityService
    from persona_bot.services.agent_service import AgentService
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
    agent: AgentService

    def __post_init__(self) -> None:
        self._chat_admission = ChatAdmission()

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
            await self._send_violation_notice(message, f"⚠️ {notice}")
            return
        if just_blocked:
            print(f"違反記録: {count}回目の違反でブラックリストに登録しました (user_id={user_id})")
            await self._send_violation_notice(
                message,
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
            await self._send_violation_notice(message, f"⚠️ {notice}（あと{remaining}回で応答を停止します）")

    @staticmethod
    async def _send_violation_notice(message: discord.Message, text: str) -> None:
        """違反の保存結果は保持し、通知の送信失敗をイベントへ漏らさない。"""
        try:
            await message.reply(text)
        except discord.DiscordException as error:
            print(f"違反通知: 送信に失敗しました: {type(error).__name__}")

    async def _reject_ng_input(self, message: discord.Message, question: str, notice: str) -> bool:
        """入力にNGワードがあれば違反として記録し、True を返す（Gemini は呼ばない）。"""
        if not question or not self.ng_filter.contains_ng_word(question):
            return False
        detected = self.ng_filter.find_ng_words(question)
        print(f"NGワード検出 (ユーザー入力): {detected}")
        await self.notify_violation(message, notice)
        return True

    async def _try_acquire_chat_slot(self, message: discord.Message, guild_id: int | None) -> bool:
        """受付判定と案内枠の記録を、最初の await より前に済ませる。"""
        notice = self._chat_admission.try_acquire(message.author.id, guild_id)
        if notice is None:
            return True
        if self._chat_admission.try_claim_notice(message.author.id):
            try:
                await message.reply(notice, allowed_mentions=discord.AllowedMentions.none())
            except Exception as error:
                print(f"通常会話: 見送り案内の送信に失敗しました: {type(error).__name__}")
        return False

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
        guild_settings = await self.state.get_guild_async(guild_id)
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
                # 画像確認中に配信が終わったら、通常の文書受付・画像失敗案内へ進める。
                if not accepts_normally or self.autonomous.is_comment_channel(message.channel.id, guild_id):
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

        normal_slot_acquired = False
        if claimed_comment_session is None:
            # 入口ではコメントでも、受付前に配信が終わった場合は通常の委任判定を使う。
            is_autonomous_comment = False
            if not accepts_normally:
                return
            normal_slot_acquired = await self._try_acquire_chat_slot(message, guild_id)
            if not normal_slot_acquired:
                return
        try:
            # 安全判定・記憶カテゴリ・リマインダー依頼を1回のAPI呼び出しで取得する。
            # memory_categoryは通常チャットの記憶参照へ、reminder_requestは添付の振り分け後の登録へ渡す。
            memory_category = "none"
            reminder_request: dict[str, str] | None = None
            agent_plan: AgentPlan | None = None
            if question:
                if not is_autonomous_comment and requests_subagent(question):
                    # サブエージェントの使用が明記された発言だけ、同じ1回の判定に委任の判定を足す（#170）。
                    # 「さっきの件」のような依頼を子へ渡せる形に整理させるため、直近の会話も渡す。
                    router_history = await fetch_channel_history_text(
                        message.channel,
                        guild_settings.channel_reset_points.get(message.channel.id),
                        self.client.user,
                        AGENT_ROUTER_HISTORY_LIMIT,
                    )
                    is_safe, _reason, memory_category, reminder_request, agent_plan = (
                        await self.moderator.check_route_and_plan(question, router_history)
                    )
                else:
                    is_safe, _reason, memory_category, reminder_request = await self.moderator.check_and_route(question)
                if not is_safe:
                    await self.notify_violation(message, "不適切な内容が検出されたため、メッセージを処理できません。")
                    return

            self.state.touch()

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

            # 配信終了後に通常会話へ移る場合も、通常の受付制限を通す。
            if not normal_slot_acquired:
                normal_slot_acquired = await self._try_acquire_chat_slot(message, guild_id)
                if not normal_slot_acquired:
                    return

            # 配信の再確認と通常枠の確保が済んだ発言だけ、話題提供の監視へ登録する。
            # 入力判定中に配信が終わった場合も対象にし、配信中のコメントとDMは登録しない。
            if not is_dm:
                self.auto_topic.watch_channel(message.channel.id, guild_id)

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

                # 未処理件数の上限で受け付けなかった文書には、受付完了を伝えない（#202）。
                if await self.documents.enqueue_from_message(message, question, document_attachments) is None:
                    await message.reply(DOCUMENT_QUEUE_FULL_NOTICE)
                    return
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
            if agent_plan is not None and agent_plan.delegate:
                response_text = await self._handle_agent_request(
                    message, question, memory_category, agent_plan, response_settings,
                )
            else:
                # サブエージェントを求められたが不要と判断した場合は、人格役がその旨を一言添えて直接答える。
                response_text = await self.handle_chat_message(
                    message, question, memory_category, response_settings=response_settings,
                    image_objects=image_objects,
                    delegation_section=build_agent_declined_section() if agent_plan is not None else "",
                )
            if response_text:
                # ボイス接続中なら、自律行動による参加中も読み上げる（未接続なら no-op）。
                # 設定が生成中に変わっても、今回の応答に使ったペルソナの声で読み上げる。
                await play_tts_response(message, response_text, response_settings.prompt_file)
        finally:
            if normal_slot_acquired:
                self._chat_admission.release(guild_id)

    async def _handle_agent_request(
        self,
        message: discord.Message,
        question: str,
        memory_category: str,
        plan: AgentPlan,
        response_settings: ResponseSettings,
    ) -> str | None:
        """子（サブエージェント）に作業させ、報告を人格役がペルソナの口調で言い直して返信する（#170）。

        進捗は定型文（案A）で1通のメッセージを書き換えて伝える。子の失敗・期限切れ・中止は
        定型文で伝えて終える（人格役の生成はしない）。返信の後処理（親密度・モデレーション・
        評価・記憶）は通常の会話と同じ handle_chat_message に任せる。
        """
        user_id = str(message.author.id)
        lines = current_persona_catalog().agent_lines(response_settings.prompt_file)
        if not self.agent.try_acquire(user_id):
            print(f"サブエージェント: 実行中の依頼があるため受け付けませんでした (user_id={user_id})。")
            try:
                await message.reply(render_agent_line(lines, "busy"), allowed_mentions=discord.AllowedMentions.none())
            except discord.DiscordException as error:
                print(f"サブエージェント: 受付できない旨を送れませんでした: {type(error).__name__}")
            return None
        try:
            # ロールと手順名は進捗メッセージへそのまま出るため、NG ワードを伏せておく。
            plan = AgentPlan(
                delegate=True,
                role=self.ng_filter.mask_with_log(plan.role, label="サブエージェントのロール"),
                task=plan.task,
                expected_output=plan.expected_output,
                steps=tuple(self.ng_filter.mask_with_log(step, label="サブエージェントの手順") for step in plan.steps),
            )
            print(f"サブエージェント: 委任します（ロール: {plan.role}、手順: {len(plan.steps)}）。")
            progress = AgentProgress(message, lines, plan)
            await progress.start()
            outcome, report = await progress.run(
                self.agent.run(plan, response_settings.model_name, progress.update)
            )
            await progress.finish(outcome)
            if report is None:
                return None
            return await self.handle_chat_message(
                message, question, memory_category, response_settings=response_settings,
                delegation_section=build_agent_report_section(plan.role, report.text),
                delegated_sources=report.sources,
            )
        finally:
            self.agent.release(user_id)

    async def handle_chat_message(
        self,
        message: discord.Message,
        question: str,
        memory_category: str = "none",
        *,
        response_settings: ResponseSettings | None = None,
        delegation_section: str = "",
        delegated_sources: list[dict[str, str]] | None = None,
        image_objects: list | None = None,
    ) -> str | None:
        """人格役として返信し、送った本文を返す（読み上げに使う）。

        Args:
            delegation_section: サブエージェントの報告・委任しなかった旨の差し込み（#170）。
            delegated_sources: サブエージェントが検索で参照した出典。渡したときは人格役は検索せず
                （報告の言い直しなので）、この出典を返信に添える。
            image_objects: 読込済み画像の借用。配信終了後の通常経路でも再読込せず、呼出元が閉じる。
        """
        guild_id = message.guild.id if message.guild is not None else None
        guild_settings = await self.state.get_guild_async(guild_id)
        # typing() の開始も待機を伴うため、その前に今回の値を確定する。
        if response_settings is None:
            response_settings = guild_settings.snapshot_response()
        async with message.channel.typing():
            user_id = str(message.author.id)
            owns_images = image_objects is None
            if owns_images:
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
                    affinity_prompt=self.affinity.build_dynamic_prompt(str(message.author.id)),
                    delegation_section=delegation_section,
                )
                delegated = delegated_sources is not None
                response_text, usage_info, grounding_sources, generated_model = await self.gemini.generate_with_metadata(
                    full_prompt,
                    response_settings.model_name,
                    image_objects,
                    timeout=GEMINI_RESPONSE_TIMEOUT_SECONDS,
                    grounding=response_settings.grounding_enabled and not delegated,
                )

                if response_text is None:
                    await message.reply("エラーが発生しました。（Gemini APIの呼び出しに失敗、または非対応の画像形式です）")
                    return None
                if delegated:
                    grounding_sources = delegated_sources

                self.usage.log_snapshot(
                    "サブエージェント経由" if delegated else "直接会話",
                    question, f"画像添付: {len(image_objects)}枚", full_prompt, response_text, usage_info,
                )

                # タグの抽出だけ先に行い、親密度の更新はモデレーション通過後に行う。
                response_text, emotion_changes = extract_affinity_changes(response_text)

                # タグを除去した結果、本文が空になった場合は空メッセージ送信を防ぐ
                # （モデルが感情タグだけを返したケース）。
                if not response_text.strip():
                    print("応答本文が空です（感情タグのみ、または空応答）。")
                    await message.reply("申し訳ありません、うまく応答を生成できませんでした。もう一度お試しください。")
                    return None

                response_text, accepted = await screen_bot_response_with_status(
                    response_text, self.ng_filter, self.moderator, log_label="Bot応答",
                )
                if accepted and emotion_changes is not None:
                    v_change, a_change = emotion_changes
                    self.affinity.update_emotion(user_id, v_change, a_change)
                    spawn(self.affinity.save_background(user_id), label="親密度の保存")

                # 参考リンクは送信用テキストにのみ追記する
                # （TTS 読み上げと長期記憶には URL の列挙はノイズになるため含めない）。
                reply_text = response_text
                if accepted and grounding_sources:
                    reply_text += format_grounding_sources(grounding_sources)

                # 送信の失敗は呼出元へ伝える（送れていない返信を読み上げない）。
                sent = await send_long_reply(message, reply_text, suppress_embeds=bool(grounding_sources))
                # ★ ここから先は送信済みの返信に付随する処理。どれが失敗しても、送った本文は
                # 呼出元の読み上げへ返す（#157）。再生成・再送はしない（二重返信になるため）。
                if accepted:
                    self._track_reply_for_feedback(sent, user_id, response_settings, generated_model)
                if accepted and await self._remember_reply(user_id, question, response_text):
                    spawn(self.memory.flush_if_needed(), label="記憶圧縮（チャット便乗）")
                return response_text
            finally:
                if owns_images:
                    close_image_attachments(image_objects)

    def _track_reply_for_feedback(
        self, sent: discord.Message, user_id: str, response_settings: ResponseSettings, generated_model: str,
    ) -> None:
        """送った返信を 👍👎 の評価対象として登録する（リアクション付与は裏で走る）。

        実際の生成モデルと今回のペルソナを渡し、待機中の設定変更を混ぜない。登録の失敗は
        評価を取りこぼすだけなので、ログに残して記憶・読み上げへ進む。
        """
        try:
            self.feedback.track_reply(
                sent,
                target_user_id=user_id,
                model_name=generated_model,
                prompt_file=response_settings.prompt_file,
            )
        except Exception as error:
            print(f"評価対象の登録に失敗しました（返信は送信済み）: {type(error).__name__}: {error}")

    async def _remember_reply(self, user_id: str, question: str, response_text: str) -> bool:
        """会話を記憶圧縮前の一時ログへ追記し、成功したかを返す。

        失敗しても返信は送信済みなので、例外を外へ出さずにログへ残す（成功扱いにはしない。
        記憶圧縮も起動しない）。キャンセルは Exception に含まれないため従来どおり伝わる。
        """
        try:
            await self.memory.append_pending_log(user_id, question, response_text)
        except Exception as error:
            print(
                f"会話記憶: 返信は送信済みですが、記憶への追記に失敗しました"
                f"（user_id={user_id}）: {type(error).__name__}: {error}"
            )
            return False
        return True
