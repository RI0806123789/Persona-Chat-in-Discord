"""自律行動（ボットが自分からボイス/ステージチャンネルへ出て「配信」する）。

既存の自発的な会話（`auto_topic_service.py`）が「テキストチャンネルへ話題を
1つ投稿する」だけなのに対して、こちらは **ボットが自分の意志で通話に入り、
独り言を続けながら、聞きに来た人のコメントへ配信者のように応答する**。

    1. サーバーごとに「話したいゲージ」（0〜100）を持つ。無会話時間が伸びるほど
       増え、直近の会話が活発なサーバーほど早く増える（`compute_talk_desire`）。
    2. ゲージが `AUTONOMOUS_GAUGE_THRESHOLD` に達したら、ステージ優先で
       チャンネルを1つ選んで接続し、第一声（会話相手の募集）を投稿・発話する。
    3. 以後 `AUTONOMOUS_TALK_INTERVAL_MINUTES` ごとに独り言を続ける。
    4. そのチャンネル付属のチャットに届いた発言だけを「コメント」として拾い、
       配信者の距離感で応答する（`handle_comment`）。
    5. コメントが `AUTONOMOUS_IDLE_STOP_MINUTES` 途絶えたら自動終了する。

■ 設計上の約束（変更するときは理由まで含めて確認すること）

**ボットの自発発言に AI モデレーションは掛けない。** 独り言・第一声は
`_screen_speech()` で感情タグ除去と NG ワードのマスクだけを通す。
これは idea.md の明示的な方針（発言のたびに Gemini をもう1回呼ぶコストを
避ける）で、既存の `auto_topic_service._screen_auto_post` とは扱いが違う。
一方、**コメントへの応答は「ユーザーからのインプットへの応答」なので、
従来どおり `screen_bot_response` で NG マスクと AI モデレーションを通す。**

**長期記憶（profile / projects）は一切渡さない。** ボイス/ステージは共有の
場であり、特定ユーザーの私的な記憶を材料にすると本人以外にも漏れる。
材料はそのチャンネルの履歴だけにする（自発的な会話・記念日と同じ方針）。

**接続の唯一の情報源は `guild.voice_client`。** `AutonomousSession` に
VoiceClient を持たせない。持たせると、管理者にキックされた・回線が切れた
などで実態と食い違った参照が残り、切断済みのクライアントへ喋り続ける。

**セッションの状態は永続化しない。** ボイス接続自体が再起動をまたげない
ため、復元しても意味がない（`GuildSettings.autonomous_session` は
`auto_topic_channels` と同じ「実行時に自然に再構築される一時状態」）。
永続化するのは有効/無効（`autonomous_enabled`）だけ。
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import discord

from persona_bot.services.affinity_service import strip_affinity_tag
from persona_bot.core.app_state import AutonomousSession, BotState, GuildSettings, ResponseSettings

# パネルからの既存importも維持し、計算本体はDiscordを使わないモジュールへ置く。
from persona_bot.services.autonomous_metrics import (
    compute_talk_desire,
    describe_quiet_hours,
    is_quiet_hours,
    start_retry_wait_minutes,
    updated_activity_samples,
)
from persona_bot.services import autonomous_stage
from persona_bot.core.background import run_in_thread, spawn, wait_for_cleanup
from persona_bot.config.constants import (
    AUTONOMOUS_CLOSING_NOTICE,
    AUTONOMOUS_CLOSING_NOTICE_DISABLED,
    AUTONOMOUS_CLOSING_NOTICE_MOVED,
    AUTONOMOUS_CLOSING_NOTICE_QUIET,
    AUTONOMOUS_CLOSING_NOTICE_TIMEUP,
    AUTONOMOUS_COMMENT_COOLDOWN_SECONDS,
    AUTONOMOUS_CONNECT_TIMEOUT_SECONDS,
    AUTONOMOUS_GAUGE_THRESHOLD,
    AUTONOMOUS_HISTORY_LIMIT,
    AUTONOMOUS_IDLE_STOP_MINUTES,
    AUTONOMOUS_IMAGE_ONLY_COMMENT,
    AUTONOMOUS_OPENING_NOTICE,
    AUTONOMOUS_SESSION_MAX_MINUTES,
    AUTONOMOUS_TALK_INTERVAL_MINUTES,
    AUTONOMOUS_TIMEOUT_SECONDS,
)
from persona_bot.services.content_moderator import ContentModerator, screen_bot_response
from persona_bot.utils.discord_helpers import (
    fetch_channel_history_text,
    close_image_attachments,
    load_image_attachments,
    send_long_message,
    send_long_reply,
)
from persona_bot.services.gemini_service import GeminiService
from persona_bot.services.ng_word_service import NgWordFilter
from persona_bot.config.paths import load_prompt_template, resolve_prompt_path
from persona_bot.services.prompting import (
    build_autonomous_monologue_prompt,
    build_autonomous_opening_prompt,
    build_autonomous_reply_prompt,
)
from persona_bot.utils.usage_graph import UsageTracker
from persona_bot.audio.voice import speak_text

# ボットが自分から投稿する文面では通知を一切飛ばさない。独り言は誰かへの返信では
# ないため、メンションらしき文字列が混じっても通知爆撃にしないための保険。
# （コメントへの応答だけは reply でコメント主に通知する。）
_NO_MENTIONS = discord.AllowedMentions.none()


@dataclass
class _StartAttempt:
    """開始待機中の停止要求。接続そのものは保持せず、同時開始も抑止する。"""

    stopped: bool = False


class AutonomousService:
    """自律行動（ボイス/ステージでの配信）を管理するサービス。

    見回り（`run_pending_checks`）はサーバーを横断して次の3つを行う:
      ・配信していないサーバー … ゲージを見て、満タンなら配信を始める
      ・配信中のサーバー       … 停止条件の確認と、独り言の投下
      ・接続が失われたサーバー … 自分のステージ枠・接続・セッション記録の片付け

    コメントの受け取り（`mark_activity` / `is_comment_channel` /
    `handle_comment`）は `bot.py` の `on_message` から呼ばれる。
    """

    def __init__(
        self,
        client: discord.Client,
        state: BotState,
        gemini: GeminiService,
        moderator: ContentModerator,
        ng_filter: NgWordFilter,
        usage: UsageTracker,
    ) -> None:
        self._client = client
        self._state = state
        self._gemini = gemini
        self._moderator = moderator
        self._ng_filter = ng_filter
        self._usage = usage
        self._starting: dict[int, _StartAttempt] = {}

    # ==========================================================================
    # on_message から呼ばれる入口
    # ==========================================================================

    def mark_activity(self, channel_id: int, guild_id: int | None) -> None:
        """人間の発言を記録する（ゲージの基準時刻と、配信の無応答タイマーの更新）。

        引数の順番は `AutoTopicService.mark_activity` / `watch_channel` と**同じ
        (channel_id, guild_id) に揃えてある**。同じ `on_message` から並べて呼ぶ
        メソッドで順番が違うと、取り違えても型が同じ（int）ため静かに誤動作する。

        メンション判定より**前**に呼ぶこと。ボット宛てでない雑談も「このサーバーで
        会話が行われている」という観測であり、ゲージはそれを見て動くため。

        更新する範囲は2つで意味が違う:
          ・ゲージの基準（`autonomous_last_activity`）… サーバー内のどのチャンネルでも更新する。
          ・配信の無応答タイマー（`session.last_human_at`）… **配信中のチャンネルの
            発言だけ**で更新する。他のテキストチャンネルの発言をコメントとして
            認めない以上、それで配信を延命させるのも筋が通らないため
            （idea.md「他のテキストチャンネルからの受け付けはコメントとして認めない」）。

        DM はボイスチャンネルを持たないため対象外。
        """
        if guild_id is None:
            return

        guild_settings = self._state.get_guild(guild_id)
        now = datetime.now(timezone.utc)
        guild_settings.autonomous_last_activity = now

        guild_settings.autonomous_recent_activity = updated_activity_samples(
            guild_settings.autonomous_recent_activity, now=now,
        )

        session = guild_settings.autonomous_session
        if session is not None and session.channel_id == channel_id:
            session.last_human_at = now

    def is_comment_channel(self, channel_id: int, guild_id: int | None) -> bool:
        """そのチャンネルが「配信中のコメント欄」かどうかを返す。

        True のとき `bot.py` はメンション不要でその発言を拾い、
        `handle_comment` へ回す（傍観者は配信のチャットへ気軽に書き込むため、
        いちいちメンションを求めるのは配信の体裁に合わない）。

        引数の順番は `mark_activity` および `AutoTopicService` 側と同じ
        (channel_id, guild_id) に揃えてある。
        """
        if guild_id is None:
            return False
        session = self._state.get_guild(guild_id).autonomous_session
        return session is not None and session.channel_id == channel_id

    def claim_comment_slot(self, channel_id: int, guild_id: int | None) -> AutonomousSession | None:
        """入力モデレーション前に枠を確保し、その配信を予約の証明として返す。

        判定と時刻更新を待機なしで済ませ、後段では同じ配信の枠を再確保しない。
        """
        if guild_id is None:
            return None
        session = self._state.get_guild(guild_id).autonomous_session
        if session is None or session.channel_id != channel_id:
            return None
        return session if self._claim_comment_slot(session) else None

    async def handle_comment(
        self,
        message: discord.Message,
        question: str,
        guild_settings: GuildSettings,
        *,
        claimed_session: AutonomousSession | None = None,
        response_settings: ResponseSettings | None = None,
    ) -> str | None:
        """配信中のチャンネルに届いたコメントへ、配信者のように応答する。

        送信した本文を返す（`bot.py` がそれを読み上げに回す）。生成に失敗した
        場合は None を返し、呼び出し側は何もしない。

        通常のチャット（`handle_chat_message`）と違い、長期記憶・親密度・
        👍👎 の評価対象登録は行わない。リスナーは「傍観者」であって1対1の
        会話相手ではない、という idea.md の位置づけに合わせている
        （記憶を混ぜない理由はモジュール冒頭の説明を参照）。
        """
        session = guild_settings.autonomous_session
        if session is None:
            return None
        if claimed_session is not None:
            # 入力判定を待っている間に、別の配信へ差し替わった予約は使わない。
            if session is not claimed_session:
                return None
        elif not self._claim_comment_slot(session):
            # この関数を直接呼ぶ経路でも、従来どおり流量制限を適用する。
            return None

        if response_settings is None:
            response_settings = guild_settings.snapshot_response()
        channel = message.channel
        async with channel.typing():
            # 本文が無く画像だけのコメントは、そのまま渡すと「空のコメントへの返事」を
            # 生成してしまう。通常のチャット経路と同じように、代わりの文を置いたうえで
            # 画像そのものもモデルへ渡す（配信中だけ画像を見なくなるのを避ける）。
            images = await load_image_attachments(message.attachments)
            try:
                comment_text = question.strip()
                if not comment_text and images:
                    comment_text = AUTONOMOUS_IMAGE_ONLY_COMMENT
                if not comment_text:
                    return None

                persona_prompt = await run_in_thread(self._load_persona, response_settings)
                if persona_prompt is None:
                    return None

                history_text = await fetch_channel_history_text(
                    channel, guild_settings.channel_reset_points.get(channel.id),
                    self._client.user, AUTONOMOUS_HISTORY_LIMIT,
                )
                full_prompt = build_autonomous_reply_prompt(
                    persona_prompt,
                    message.author.display_name,
                    comment_text,
                    history_text,
                )
                # コメントは人からの問いかけなので、通常のチャットと同じく
                # そのサーバーの Google 検索設定に従う（独り言と違って調べ物がありうる）。
                response_text = await self._generate(
                    full_prompt,
                    response_settings,
                    label="コメント応答",
                    detail=f"{message.author.display_name} さんのコメント（画像{len(images)}枚）",
                    grounding=response_settings.grounding_enabled,
                    images=images or None,
                )
                if not response_text:
                    return None

                response_text = strip_affinity_tag(response_text)
                if not response_text.strip():
                    print("自律行動: コメント応答が感情タグのみだったため見送ります。")
                    return None

                # 「ユーザーからのインプットに対しては今まで通りフィルターを使う」（idea.md）
                # ため、自発発言と違いここでは AI モデレーションまで通す。
                response_text = await screen_bot_response(
                    response_text, self._ng_filter, self._moderator, log_label="自律行動のコメント応答"
                )

                # 生成を待っている間に配信が終わっている（/leave など）ことがある。
                # 終わった配信の「配信者としての応答」を後から投稿しないよう、
                # 独り言・第一声と同じく送信の直前で確かめる。
                if not self._is_current_session(guild_settings, session):
                    print("自律行動: 応答の生成中に配信が終了したため、この応答は見送ります。")
                    return None

                await send_long_reply(message, response_text)
                # 応答した直後に独り言が重なると会話がぶつかるため、発話時刻を更新しておく。
                session.last_spoke_at = datetime.now(timezone.utc)
                return response_text
            finally:
                close_image_attachments(images)

    def _claim_comment_slot(self, session: AutonomousSession) -> bool:
        """入力AI判定の前に受付枠を確保し、同時に受付時刻を記録する。

        配信中のチャンネルではメンション不要でコメントを拾うため、何もしないと
        「そのチャンネルの全メッセージ × (モデレーション + 応答生成)」で Gemini 呼び出しが
        発散する（通常のメンション必須モードでは、メンションが自然な流量制限だった）。
        直前の受付から `AUTONOMOUS_COMMENT_COOLDOWN_SECONDS` 未満のコメントは見送る。
        これは配信単位の制限で、同じユーザーの連投にも適用される。
        入力AIで拒否された場合も枠は消費するが、受付前のローカルNG判定では消費しない。

        **判定と時刻の記録を1つのメソッドにまとめてあるのは、間に `await` を
        挟ませないため。** 挟むと同時に届いた2件が両方とも通り、クールダウンが効かない。
        そのためこのメソッドは同期関数のままにすること。

        見送ったコメントも、無応答タイマー（`last_human_at`）は `mark_activity` が
        既に更新済みなので、配信が早く終わることはない。
        """
        if AUTONOMOUS_COMMENT_COOLDOWN_SECONDS <= 0:
            return True  # 制限なし（全コメントに応答する）

        now = datetime.now(timezone.utc)
        previous = session.last_comment_reply_at
        if previous is not None:
            elapsed = (now - previous).total_seconds()
            if elapsed < AUTONOMOUS_COMMENT_COOLDOWN_SECONDS:
                print(
                    f"自律行動: コメントが続いているため、この1件は見送ります"
                    f"（前回の応答から {elapsed:.1f} 秒）。"
                )
                return False

        session.last_comment_reply_at = now
        # 応答を作っている最中に見回りが独り言を投下すると、返事と独り言がぶつかる。
        # 生成に入る前から発話時刻も進めて、独り言の間隔判定に引っかからせる。
        session.last_spoke_at = now
        return True

    # ==========================================================================
    # 見回り（bot.py の autonomous_loop から1分ごとに呼ばれる）
    # ==========================================================================

    async def run_pending_checks(self) -> None:
        """全サーバーを見回り、配信の開始・継続・終了を判断する。

        1サーバーの失敗で他のサーバーの見回りを止めないよう、例外はここで受ける。
        DM（guild_id が None）はボイスチャンネルを持たないため対象外。
        """
        for guild_id, guild_settings in list(self._state.iter_all_guild_settings()):
            if guild_id is None:
                continue
            try:
                await self._check_guild(guild_id, guild_settings)
            except Exception as error:
                print(f"自律行動: 見回りエラー (guild={guild_id}): {type(error).__name__}: {error}")

    async def _check_guild(self, guild_id: int, guild_settings: GuildSettings) -> None:
        guild = self._client.get_guild(guild_id)
        if guild is None:
            # サーバーから追い出された・キャッシュから引けなくなった場合。
            # セッション記録を残したままにすると is_comment_channel が真を返し続け、
            # さらに自発会話の競合抑止（auto_topic_service）も効いたままになって、
            # そのサーバーの話題提供が二度と動かなくなる。ここで片付ける。
            if guild_settings.autonomous_session is not None:
                print(f"自律行動: サーバーを参照できないため配信の記録を片付けます（guild={guild_id}）。")
                self._clear_session(guild_settings)
            return

        # 配信中なら、続けるか終えるかの判断が先（機能を無効化された場合も
        # ここで拾って後片付けする必要があるため、enabled の判定より前に置く）。
        if guild_settings.autonomous_session is not None:
            await self._tend_session(guild, guild_settings)
            return

        if not guild_settings.autonomous_enabled:
            return
        if is_quiet_hours():
            return
        # /join で人が呼んだ接続を横取りしない（読み上げ用に呼ばれている最中かもしれない）。
        if guild.voice_client is not None:
            return
        # 直前の開始が失敗していれば、その待ち時間が明けるまで試さない。
        # 失敗してもゲージは満タンのままなので、この歯止めが無いと毎分ずっと
        # 接続→失敗→切断を繰り返す（_record_start_failure の説明を参照）。
        retry_at = guild_settings.autonomous_start_retry_at
        if retry_at is not None and datetime.now(timezone.utc) < retry_at:
            return
        if compute_talk_desire(guild_settings) < AUTONOMOUS_GAUGE_THRESHOLD:
            return

        await self._start_session(guild, guild_settings)

    async def _tend_session(self, guild: discord.Guild, guild_settings: GuildSettings) -> None:
        """配信中のサーバーの面倒を見る（停止条件の確認 → 独り言の投下）。"""
        session = guild_settings.autonomous_session
        if session is None:
            return

        # 実態（接続）が先。管理者に切断された・回線が切れた場合はセッションの
        # 記録だけが残るので、喋ろうとする前に片付ける。
        voice_client = guild.voice_client
        if not isinstance(voice_client, discord.VoiceClient) or not voice_client.is_connected():
            await self.stop_session(guild.id, reason="ボイス接続が失われた")
            return

        # 管理者にドラッグで別のチャンネルへ移されることがある。接続は生きたままなので
        # 上の判定では気づけず、放置すると「テキストは移動前のチャンネルへ投稿し、
        # 音声だけ移動後のチャンネルで流れる」というちぐはぐな状態になる
        # （コメントの受付口も移動前のままになる）。追従はしない —— セッションは
        # 開いたステージ枠を channel_id で覚えているため、行き先を差し替えると
        # 元の枠を閉じられなくなる。素直に畳んで、必要ならまた出直す。
        current_channel = getattr(voice_client, "channel", None)
        if current_channel is not None and current_channel.id != session.channel_id:
            await self.stop_session(
                guild.id, reason="別のチャンネルへ移動された", notice=AUTONOMOUS_CLOSING_NOTICE_MOVED
            )
            return

        now = datetime.now(timezone.utc)
        if not guild_settings.autonomous_enabled:
            # 管理者がパネルから止めた場合。パネルの確認文は操作した本人にしか
            # 見えない（エフェメラル）ため、聞いていた人向けにひと言残してから抜ける。
            await self.stop_session(
                guild.id, reason="設定が無効化されました", notice=AUTONOMOUS_CLOSING_NOTICE_DISABLED
            )
            return
        if is_quiet_hours():
            await self.stop_session(
                guild.id, reason="時間帯制限（深夜）", notice=AUTONOMOUS_CLOSING_NOTICE_QUIET
            )
            return
        if now - session.started_at >= timedelta(minutes=AUTONOMOUS_SESSION_MAX_MINUTES):
            await self.stop_session(
                guild.id, reason="最長時間に到達", notice=AUTONOMOUS_CLOSING_NOTICE_TIMEUP
            )
            return
        if now - session.last_human_at >= timedelta(minutes=AUTONOMOUS_IDLE_STOP_MINUTES):
            await self.stop_session(
                guild.id, reason="コメントが途絶えた", notice=AUTONOMOUS_CLOSING_NOTICE
            )
            return

        if session.busy:
            return
        if now - session.last_spoke_at < timedelta(minutes=AUTONOMOUS_TALK_INTERVAL_MINUTES):
            return

        # 生成と読み上げは数十秒かかりうる。見回りループの中で待つと他のサーバーの
        # 停止判定まで遅れるため、裏へ逃がす（多重起動は busy フラグで防ぐ）。
        session.busy = True
        spawn(self._speak_monologue(guild, guild_settings, session), label="自律行動の独り言")

    # ==========================================================================
    # 配信の開始・終了
    # ==========================================================================

    async def _start_session(self, guild: discord.Guild, guild_settings: GuildSettings) -> None:
        """開始要求を登録し、未登録セッションへの停止要求と多重開始を扱う。"""
        if guild.id in self._starting or guild_settings.autonomous_session is not None:
            return
        attempt = _StartAttempt()
        self._starting[guild.id] = attempt
        try:
            await self._connect_session(guild, guild_settings, attempt)
        finally:
            self._starting.pop(guild.id, None)

    async def _connect_session(
        self, guild: discord.Guild, guild_settings: GuildSettings, attempt: _StartAttempt,
    ) -> None:
        """チャンネルを1つ選んで接続し、配信を開始する。

        失敗した経路では必ず `_record_start_failure` を通すこと。ゲージは失敗しても
        満タンのままなので、記録しないと次の見回り（1分後）で同じ失敗を繰り返す。
        """
        channel = autonomous_stage.pick_channel(guild)
        if channel is None:
            # 出ていける先が無いサーバー（権限不足・ボイスチャンネルが無いなど）。
            # ここは API を1回も叩かず、チャンネル一覧の権限を見るだけなので
            # 待ち時間を入れない（チャンネルが作られたらすぐ出ていけるようにする）。
            return

        is_stage = isinstance(channel, discord.StageChannel)
        try:
            voice_client = await channel.connect(timeout=AUTONOMOUS_CONNECT_TIMEOUT_SECONDS)
        # Forbidden は HTTPException の派生クラスなので必ず先に捕まえる。
        except discord.Forbidden:
            print(f"自律行動: {channel.name} への接続を拒否されました（権限不足）。")
            self._record_start_failure(guild_settings)
            return
        except asyncio.TimeoutError:
            print(f"自律行動: {channel.name} への接続がタイムアウトしました。")
            self._record_start_failure(guild_settings)
            return
        except Exception as error:
            print(f"自律行動: {channel.name} への接続に失敗しました（{type(error).__name__}: {error}）。")
            self._record_start_failure(guild_settings)
            return

        # 接続待機中の停止・無効化・静音時間への突入や、接続の差し替えを取り直す。
        if not self._can_finish_start(guild, guild_settings, attempt, voice_client, channel.id):
            if voice_client is not None:
                await wait_for_cleanup(
                    autonomous_stage.disconnect(guild, expected_voice_client=voice_client),
                    label="自律行動の接続後の開始中止",
                )
            self._record_interrupted_start(guild_settings, attempt)
            return

        stage_instance_id = None
        if is_stage:
            try:
                stage_instance_id = await autonomous_stage.prepare_stage(guild, channel)
            except asyncio.CancelledError:
                if voice_client is not None:
                    await wait_for_cleanup(
                        autonomous_stage.disconnect(guild, expected_voice_client=voice_client),
                        label="自律行動の開始キャンセルの切断",
                    )
                raise
            except Exception as error:
                # 発言者になれないステージで喋っても誰にも聞こえないため、
                # 中途半端に居座らず接続ごと諦める。
                print(f"自律行動: ステージ({channel.name})の準備に失敗しました（{type(error).__name__}: {error}）。")
                if voice_client is not None:
                    await wait_for_cleanup(
                        autonomous_stage.disconnect(guild, expected_voice_client=voice_client),
                        label="自律行動の開始失敗の切断",
                    )
                self._record_start_failure(guild_settings)
                return

        now = datetime.now(timezone.utc)
        session = AutonomousSession(
            channel_id=channel.id,
            is_stage=is_stage,
            started_at=now,
            last_human_at=now,
            last_spoke_at=now,
            stage_started_by_bot=stage_instance_id is not None,
            stage_instance_id=stage_instance_id,
            # 第一声を作っている間に独り言が重ならないよう、最初から busy にしておく。
            busy=True,
        )
        # ステージ準備も待機を伴う。条件が変わっていれば、未登録の所有枠と元の接続を片付ける。
        if not self._can_finish_start(guild, guild_settings, attempt, voice_client, channel.id):
            await wait_for_cleanup(
                autonomous_stage.finish_session_cleanup(
                    guild.id, guild_settings, session, channel, guild, voice_client,
                ),
                label="自律行動の準備後の開始中止",
            )
            self._record_interrupted_start(guild_settings, attempt)
            return

        # ここまで来たら開始成功。失敗の記録は消して、次回は待たずに出られるようにする。
        guild_settings.autonomous_start_failures = 0
        guild_settings.autonomous_start_retry_at = None
        guild_settings.autonomous_session = session
        kind = "ステージ" if is_stage else "ボイスチャンネル"
        print(f"自律行動: {kind}「{channel.name}」で配信を開始しました（guild={guild.id}）。")

        spawn(self._speak_opening(guild, guild_settings, session, channel), label="自律行動の第一声")

    @staticmethod
    def _can_finish_start(
        guild: discord.Guild, guild_settings: GuildSettings, attempt: _StartAttempt,
        voice_client: discord.VoiceClient | None, channel_id: int,
    ) -> bool:
        """待機後も開始要求が有効で、元の接続・チャンネルを所有しているか。"""
        return bool(
            not attempt.stopped and guild_settings.autonomous_enabled and not is_quiet_hours()
            and guild_settings.autonomous_session is None
            and voice_client is not None and guild.voice_client is voice_client
            and voice_client.is_connected() and getattr(voice_client.channel, "id", None) == channel_id
        )

    def _record_interrupted_start(self, guild_settings: GuildSettings, attempt: _StartAttempt) -> None:
        """利用者の停止や時間帯制限は失敗にせず、接続異常では既存の再試行待ちを保つ。"""
        if (not attempt.stopped and guild_settings.autonomous_enabled and not is_quiet_hours()
                and guild_settings.autonomous_session is None):
            self._record_start_failure(guild_settings)

    def _record_start_failure(self, guild_settings: GuildSettings) -> None:
        """配信の開始に失敗したことを記録し、次に試すまでの待ち時間を設定する。

        **この歯止めが無いと、失敗したサーバーへ毎分ずっと接続を試み続ける。**
        「話したいゲージ」は時間が経つほど増えるだけで、開始に失敗しても下がらない
        ため、見回りのたびに同じ判定が通ってしまうため（実測で確認済み）。
        権限設定が中途半端なサーバーが1つあるだけで、1日1440回の接続と切断が走る。

        待ち時間は連続失敗のたびに延び、`AUTONOMOUS_START_RETRY_MAX_MULTIPLIER` 倍で
        頭打ちになる（既定で 30分 → 最長4時間）。ゲージ自体はリセットしない —
        リセットすると、権限を直したあともしきい値時間ぶん待たされてしまう。
        なお `/functions` のパネルで自律行動を確定すると、この待ち時間は即座に
        解除される（設定を直した直後に試せるようにするため）。
        """
        guild_settings.autonomous_start_failures += 1
        wait_minutes = start_retry_wait_minutes(guild_settings.autonomous_start_failures)
        guild_settings.autonomous_start_retry_at = datetime.now(timezone.utc) + timedelta(minutes=wait_minutes)
        print(
            f"自律行動: 配信の開始に失敗しました（連続 {guild_settings.autonomous_start_failures} 回目）。"
            f"次に試すのは約 {wait_minutes:.0f} 分後です。"
        )

    async def stop_session(
        self,
        guild_id: int,
        *,
        reason: str,
        notice: str | None = None,
    ) -> bool:
        """配信と開始待機を停止する。停止対象があれば True、無ければ False。

        `/leave`・`/join` からも呼ばれる（人の操作による強制終了）。

        **例外を外へ出さない。** 締めの投稿やステージ枠の後始末が失敗しても、
        ボイス接続の切断とセッション記録の破棄までは必ず進める（片付けが
        途中で止まると、接続だけが残って誰も止められなくなるため）。

        Args:
            reason: ログに残す終了理由。
            notice: 配信チャンネルへ残す締めの一言（None なら何も投稿しない）。
        """
        guild_settings = self._state.get_guild(guild_id)
        starting = self._starting.get(guild_id)
        if starting is not None:
            # connect()がまだ戻っていない場合にも、後着するセッションの登録を止める。
            starting.stopped = True
        session = guild_settings.autonomous_session
        if session is None:
            if starting is not None:
                self._clear_session(guild_settings)
                print(f"自律行動: 配信の開始を中止します（guild={guild_id} / 理由: {reason}）。")
                return True
            return False

        # 待機中に /join や別の配信へ接続が差し替わっても、その接続を切らない。
        # 接続の情報源は常に guild.voice_client とし、ここでは照合用の参照だけを控える。
        guild = self._client.get_guild(guild_id)
        voice_client = guild.voice_client if guild is not None else None
        # 先に記録を消す。ここから切断までの間に見回りが来ても、二重に停止処理を
        # 走らせない（同じステージ枠を2回消そうとするのを防ぐ）。
        self._clear_session(guild_settings)

        canceled = False
        channel = None
        try:
            channel = self._client.get_channel(session.channel_id)
            # 終了案内の送信だけは呼出元と一緒にキャンセルし、後片付けへ進む。
            if notice and channel is not None:
                try:
                    await send_long_message(channel, notice, allowed_mentions=_NO_MENTIONS)
                except discord.HTTPException as error:
                    print(f"自律行動: 締めの投稿に失敗しました（guild={guild_id}）: {error}")
                except Exception as error:
                    print(f"自律行動: 締めの投稿で予期せぬエラー（guild={guild_id}）: {type(error).__name__}: {error}")
        except asyncio.CancelledError:
            canceled = True
        finally:
            cleanup = spawn(
                autonomous_stage.finish_session_cleanup(guild_id, guild_settings, session, channel, guild, voice_client),
                label="自律行動の終了後片付け",
            )
            while True:
                try:
                    await asyncio.shield(cleanup)
                    break
                except asyncio.CancelledError:
                    if cleanup.cancelled():
                        raise
                    # キャンセルを繰り返されても、ステージ枠と元の接続を片付け終える。
                    canceled = True

        print(f"自律行動: 配信を終了しました（guild={guild_id} / 理由: {reason}）。")
        if canceled:
            raise asyncio.CancelledError
        return True

    def _clear_session(self, guild_settings: GuildSettings) -> None:
        """セッション記録を破棄し、「話したいゲージ」を空にする。

        ゲージを空に戻さないと、終了直後の見回りで満タンのまま再判定され、
        同じサーバーで配信が延々と再開してしまう。
        """
        guild_settings.autonomous_session = None
        guild_settings.autonomous_last_activity = datetime.now(timezone.utc)
        guild_settings.autonomous_recent_activity.clear()

    # ==========================================================================
    # 発話（第一声・独り言）
    # ==========================================================================

    async def _speak_opening(
        self,
        guild: discord.Guild,
        guild_settings: GuildSettings,
        session: AutonomousSession,
        channel: Any,
    ) -> None:
        """配信開始の第一声を投稿・発話する。

        生成に失敗しても「会話相手募集中…」の看板（`AUTONOMOUS_OPENING_NOTICE`）
        だけは必ず出す — 人を誘うことがこの機能の目的なので、AI の調子で
        案内そのものが消えては困るため。
        """
        response_settings = guild_settings.snapshot_response()
        try:
            body = AUTONOMOUS_OPENING_NOTICE
            speech = ""
            persona_prompt = await run_in_thread(self._load_persona, response_settings)
            if persona_prompt is not None:
                history_text = await fetch_channel_history_text(
                    channel, guild_settings.channel_reset_points.get(channel.id),
                    self._client.user, AUTONOMOUS_HISTORY_LIMIT,
                )
                full_prompt = build_autonomous_opening_prompt(
                    persona_prompt, getattr(channel, "name", ""), history_text
                )
                generated = await self._generate(
                    full_prompt, response_settings, label="第一声", detail=self._channel_label(channel)
                )
                speech = self._screen_speech(generated, "自律行動の第一声")
                if speech:
                    body = f"{AUTONOMOUS_OPENING_NOTICE}\n{speech}"

            if not self._is_current_session(guild_settings, session):
                return
            await send_long_message(channel, body, allowed_mentions=_NO_MENTIONS)
            # テキストの送信待機中にも /join・/leave・外部からの移動が起こりうる。
            if speech and self.can_speak_session(guild, guild_settings, session):
                await speak_text(
                    guild, speech, response_settings.prompt_file,
                    is_valid=lambda: self.can_speak_session(guild, guild_settings, session),
                )
        finally:
            session.busy = False
            # 失敗したときも時刻を進める。進めないと次の見回りで即座に再挑戦し、
            # 生成が壊れている間ずっと毎分 API を叩き続けることになる。
            session.last_spoke_at = datetime.now(timezone.utc)

    async def _speak_monologue(
        self,
        guild: discord.Guild,
        guild_settings: GuildSettings,
        session: AutonomousSession,
    ) -> None:
        """独り言を1つ生成して投稿・発話する。"""
        response_settings = guild_settings.snapshot_response()
        try:
            channel = self._client.get_channel(session.channel_id)
            if channel is None:
                return
            persona_prompt = await run_in_thread(self._load_persona, response_settings)
            if persona_prompt is None:
                return

            history_text = await fetch_channel_history_text(
                channel, guild_settings.channel_reset_points.get(channel.id),
                self._client.user, AUTONOMOUS_HISTORY_LIMIT,
            )
            full_prompt = build_autonomous_monologue_prompt(
                persona_prompt, history_text, session.turn_count
            )
            # 独り言に調べ物は要らないためグラウンディングは使わない（既定の False）。
            generated = await self._generate(
                full_prompt, response_settings, label="独り言", detail=self._channel_label(channel)
            )
            speech = self._screen_speech(generated, "自律行動の独り言")
            if not speech:
                return

            # 生成を待っている間に配信が終わっている（/leave など）ことがある。
            # 終わったチャンネルへ投稿しないよう、送信の直前で確かめる。
            if not self._is_current_session(guild_settings, session):
                return

            await send_long_message(channel, speech, allowed_mentions=_NO_MENTIONS)
            if not self.can_speak_session(guild, guild_settings, session):
                return
            await speak_text(
                guild, speech, response_settings.prompt_file,
                is_valid=lambda: self.can_speak_session(guild, guild_settings, session),
            )
            session.turn_count += 1
        finally:
            session.busy = False
            session.last_spoke_at = datetime.now(timezone.utc)

    # ==========================================================================
    # 補助
    # ==========================================================================

    @staticmethod
    def can_speak_session(
        guild: discord.Guild | None, guild_settings: GuildSettings,
        session: AutonomousSession | None,
    ) -> bool:
        """元の配信が続いており、接続先もその配信チャンネルのままか確かめる。"""
        if guild is None or session is None or guild_settings.autonomous_session is not session:
            return False
        voice_client = guild.voice_client
        return bool(
            voice_client is not None and voice_client.is_connected()
            and getattr(voice_client.channel, "id", None) == session.channel_id
        )

    @staticmethod
    def _is_current_session(guild_settings: GuildSettings, session: AutonomousSession) -> bool:
        """その配信がまだ生きているか（別の配信に差し替わっていないか）を確かめる。"""
        return guild_settings.autonomous_session is session

    def _screen_speech(self, text: str | None, label: str) -> str:
        """ボットの自発発言に掛ける後処理（感情タグの除去 → NG ワードのマスク）。

        **AI モデレーションは意図的に掛けない**（idea.md の方針。理由は
        モジュール冒頭の説明を参照）。NG ワードのマスクはローカルの正規表現
        マッチだけで済み、API 呼び出しも遅延も増えないため残している。
        """
        if not text:
            return ""
        cleaned = strip_affinity_tag(text)
        if not cleaned.strip():
            return ""
        return self._ng_filter.mask_with_log(cleaned, label=label)

    async def _generate(
        self,
        full_prompt: str,
        response_settings: ResponseSettings,
        *,
        label: str,
        detail: str,
        grounding: bool = False,
        images: list[Any] | None = None,
    ) -> str | None:
        """Gemini を呼び、生成結果を返す（失敗時は None）。利用ログもここで残す。

        `images` はコメントに添付された画像（自発発言では常に None）。
        """
        response_text, usage_info, _sources = await self._gemini.generate(
            full_prompt,
            response_settings.model_name,
            images=images,
            timeout=AUTONOMOUS_TIMEOUT_SECONDS,
            grounding=grounding,
        )
        if not response_text:
            print(f"自律行動: {label}の生成に失敗しました。")
            return None
        self._usage.log_snapshot(f"自律行動/{label}", detail, "自動実行", full_prompt, response_text, usage_info)
        return response_text

    def _load_persona(self, response_settings: ResponseSettings) -> str | None:
        """そのサーバーで選ばれているペルソナのプロンプトを読む（無ければ None）。"""
        try:
            return load_prompt_template(resolve_prompt_path(response_settings.prompt_file))
        except FileNotFoundError:
            print(f"自律行動: プロンプトファイル({response_settings.prompt_file})が見つかりません。")
            return None

    @staticmethod
    def _channel_label(channel: Any) -> str:
        return getattr(channel, "name", None) or f"channel {getattr(channel, 'id', '不明')}"
