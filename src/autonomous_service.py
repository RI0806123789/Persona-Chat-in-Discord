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
import random
from datetime import datetime, timedelta, timezone
from typing import Any

import discord

from affinity_service import strip_affinity_tag
from app_state import AutonomousSession, BotState, GuildSettings
from background import spawn
from constants import (
    AUTONOMOUS_ACTIVITY_BONUS_MAX,
    AUTONOMOUS_ACTIVITY_BONUS_PER_MESSAGE,
    AUTONOMOUS_ACTIVITY_SAMPLES_MAX,
    AUTONOMOUS_ACTIVITY_WINDOW_MINUTES,
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
    AUTONOMOUS_QUIET_HOURS_END,
    AUTONOMOUS_QUIET_HOURS_START,
    AUTONOMOUS_SESSION_MAX_MINUTES,
    AUTONOMOUS_STAGE_TOPIC,
    AUTONOMOUS_START_RETRY_COOLDOWN_MINUTES,
    AUTONOMOUS_START_RETRY_MAX_MULTIPLIER,
    AUTONOMOUS_TALK_INTERVAL_MINUTES,
    AUTONOMOUS_TIMEOUT_SECONDS,
    AUTONOMOUS_TIMEZONE,
    AUTONOMOUS_VOICE_STATE_RETRIES,
    AUTONOMOUS_VOICE_STATE_WAIT_SECONDS,
)
from content_moderator import ContentModerator, screen_bot_response
from discord_helpers import (
    fetch_channel_history_text,
    load_image_attachments,
    send_long_message,
    send_long_reply,
)
from gemini_service import GeminiService
from ng_word_service import NgWordFilter
from paths import load_prompt_template, resolve_prompt_path
from prompting import (
    build_autonomous_monologue_prompt,
    build_autonomous_opening_prompt,
    build_autonomous_reply_prompt,
)
from timeutil import resolve_timezone, timezone_label
from usage_graph import UsageTracker
from voice import speak_text

# ボットが自分から投稿する文面では通知を一切飛ばさない。独り言は誰かへの返信では
# ないため、メンションらしき文字列が混じっても通知爆撃にしないための保険。
# （コメントへの応答だけは reply でコメント主に通知する。）
_NO_MENTIONS = discord.AllowedMentions.none()

# ボイス/ステージのどちらでも受け取れるチャンネル型。
VocalChannel = discord.VoiceChannel | discord.StageChannel

# 深夜の抑制に使うタイムゾーン。**import 時に1回だけ解決する。**
# resolve_timezone は失敗時に警告を出すため、パネル表示のたびに呼ぶとログが溢れる。
# サービスのインスタンスからもパネル表示（describe_quiet_hours）からも同じものを使う。
_QUIET_HOURS_TZ = resolve_timezone(AUTONOMOUS_TIMEZONE, purpose="自律行動の時間帯制限")


def describe_quiet_hours() -> str:
    """自律行動を止める時間帯の表示文字列（例: "00:00〜05:59（Asia/Tokyo）"）。

    抑制が無効（開始と終了が同じ値）なら空文字を返す。

    **タイムゾーン名は定数ではなく `timezone_label` で作ること。**
    `resolve_timezone` は tzdata の無い環境で UTC へフォールバックするため、
    定数をそのまま表示すると「実際は UTC の 00:00〜05:59（＝日本時間の
    09:00〜14:59）なのに Asia/Tokyo と名乗る」という食い違いが起きる。
    """
    if AUTONOMOUS_QUIET_HOURS_START == AUTONOMOUS_QUIET_HOURS_END:
        return ""
    end_hour = (AUTONOMOUS_QUIET_HOURS_END - 1) % 24
    return (
        f"{AUTONOMOUS_QUIET_HOURS_START:02d}:00〜{end_hour:02d}:59"
        f"（{timezone_label(_QUIET_HOURS_TZ)}）"
    )


def compute_talk_desire(guild_settings: GuildSettings, *, now: datetime | None = None) -> float:
    """そのサーバーの「話したいゲージ」（0〜100）を計算する。

    基礎値は無会話時間に比例して増え、`auto_topic_interval_hours` で 100 に届く
    （自発会話のしきい値をそのまま「ゲージが満タンになるまでの時間」として
    共用する。設定項目を増やさず、パネル1枚で完結させるため）。
    そこへ「直近の会話の活発さ」を加点する — 人が集まっているサーバーほど
    早く話したくなる、という `idea.md` の「直近の会話状況から値を決める」部分。

    **人間の発言をまだ一度も観測していないサーバーでは 0 を返す。** これにより
    「招待されただけで誰とも話していないサーバー」や「再起動直後」に、いきなり
    ボイスチャンネルへ入り込むことがない（観測は `mark_activity` が行う）。

    パネル表示（`auto_topic_view`）からも呼べるよう、サービスのインスタンスに
    依存しないモジュール関数にしてある。
    """
    now = now if now is not None else datetime.now(timezone.utc)
    last_activity = guild_settings.autonomous_last_activity
    if last_activity is None:
        return 0.0

    full_minutes = max(1.0, guild_settings.auto_topic_interval_hours * 60.0)
    silence_minutes = max(0.0, (now - last_activity).total_seconds() / 60.0)
    base = 100.0 * silence_minutes / full_minutes

    window_start = now - timedelta(minutes=AUTONOMOUS_ACTIVITY_WINDOW_MINUTES)
    recent_count = sum(1 for at in guild_settings.autonomous_recent_activity if at >= window_start)
    bonus = min(AUTONOMOUS_ACTIVITY_BONUS_MAX, recent_count * AUTONOMOUS_ACTIVITY_BONUS_PER_MESSAGE)

    return max(0.0, min(100.0, base + bonus))


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
        # 解決はモジュール読み込み時に1回だけ（describe_quiet_hours と同じものを使う）。
        self._tz = _QUIET_HOURS_TZ

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

        # 時間窓の外へ出た分を落としてから追加する（放っておくと際限なく伸びる）。
        window_start = now - timedelta(minutes=AUTONOMOUS_ACTIVITY_WINDOW_MINUTES)
        samples = [at for at in guild_settings.autonomous_recent_activity if at >= window_start]
        samples.append(now)
        # 極端に発言が多いサーバーでもメモリを食わないよう件数でも頭打ちにする。
        guild_settings.autonomous_recent_activity = samples[-AUTONOMOUS_ACTIVITY_SAMPLES_MAX:]

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

        channel = message.channel
        async with channel.typing():
            persona_prompt = self._load_persona(guild_settings)
            if persona_prompt is None:
                return None

            # 本文が無く画像だけのコメントは、そのまま渡すと「空のコメントへの返事」を
            # 生成してしまう。通常のチャット経路と同じように、代わりの文を置いたうえで
            # 画像そのものもモデルへ渡す（配信中だけ画像を見なくなるのを避ける）。
            images = await load_image_attachments(message.attachments)
            comment_text = question.strip()
            if not comment_text and images:
                comment_text = AUTONOMOUS_IMAGE_ONLY_COMMENT
            if not comment_text:
                return None

            history_text = await fetch_channel_history_text(
                channel, None, self._client.user, AUTONOMOUS_HISTORY_LIMIT
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
                guild_settings,
                label="コメント応答",
                detail=f"{message.author.display_name} さんのコメント（画像{len(images)}枚）",
                grounding=guild_settings.grounding_enabled,
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
        if self._is_quiet_hours():
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
        if self._is_quiet_hours():
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
        """チャンネルを1つ選んで接続し、配信を開始する。

        失敗した経路では必ず `_record_start_failure` を通すこと。ゲージは失敗しても
        満タンのままなので、記録しないと次の見回り（1分後）で同じ失敗を繰り返す。
        """
        channel = self._pick_channel(guild)
        if channel is None:
            # 出ていける先が無いサーバー（権限不足・ボイスチャンネルが無いなど）。
            # ここは API を1回も叩かず、チャンネル一覧の権限を見るだけなので
            # 待ち時間を入れない（チャンネルが作られたらすぐ出ていけるようにする）。
            return

        is_stage = isinstance(channel, discord.StageChannel)
        try:
            await channel.connect(timeout=AUTONOMOUS_CONNECT_TIMEOUT_SECONDS)
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

        stage_instance_id = None
        if is_stage:
            try:
                stage_instance_id = await self._prepare_stage(guild, channel)
            except Exception as error:
                # 発言者になれないステージで喋っても誰にも聞こえないため、
                # 中途半端に居座らず接続ごと諦める。
                print(f"自律行動: ステージ({channel.name})の準備に失敗しました（{type(error).__name__}: {error}）。")
                await self._disconnect(guild)
                self._record_start_failure(guild_settings)
                return

        # ここまで来たら開始成功。失敗の記録は消して、次回は待たずに出られるようにする。
        guild_settings.autonomous_start_failures = 0
        guild_settings.autonomous_start_retry_at = None

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
        guild_settings.autonomous_session = session
        kind = "ステージ" if is_stage else "ボイスチャンネル"
        print(f"自律行動: {kind}「{channel.name}」で配信を開始しました（guild={guild.id}）。")

        spawn(self._speak_opening(guild, guild_settings, session, channel), label="自律行動の第一声")

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
        multiplier = min(guild_settings.autonomous_start_failures, AUTONOMOUS_START_RETRY_MAX_MULTIPLIER)
        wait_minutes = AUTONOMOUS_START_RETRY_COOLDOWN_MINUTES * multiplier
        guild_settings.autonomous_start_retry_at = datetime.now(timezone.utc) + timedelta(minutes=wait_minutes)
        print(
            f"自律行動: 配信の開始に失敗しました（連続 {guild_settings.autonomous_start_failures} 回目）。"
            f"次に試すのは約 {wait_minutes:.0f} 分後です。"
        )

    async def _prepare_stage(self, guild: discord.Guild, channel: discord.StageChannel) -> int | None:
        """ステージで発言できる状態にし、自分で開いた枠のIDを返す。

        ステージチャンネルは参加しただけでは「観客」（suppress=True）のままで
        音声を流せない。自分の抑制を解除するには mute_members 権限が要る。
        ステージ枠（StageInstance）がまだ無ければ自分で開く — その看板（topic）が
        チャンネル一覧に出るので、「会話相手募集中…」を人に見せる手段にもなる。
        他の人が開いた枠に相乗りした場合は None を返し、終了時にも閉じない。
        """
        created_instance = None
        if channel.instance is None:
            created_instance = await channel.create_instance(
                topic=AUTONOMOUS_STAGE_TOPIC,
                privacy_level=discord.PrivacyLevel.guild_only,
                # **send_start_notification を True にしないこと。** True にすると
                # Discord が「ステージが始まりました」という @everyone 相当の通知を
                # サーバー全体へ送る。ボットが自分の判断で何度も配信を始める機能で
                # これを有効にすると、そのたびに全員へ通知が飛ぶ（通知爆撃になる）。
                send_start_notification=False,
                reason="ボットの自律行動による配信開始",
            )

        try:
            # connect() 直後は自分のボイス状態がまだキャッシュに乗っておらず、
            # Member.edit(suppress=False) が channel_id を載せられずに失敗しうる。
            # 見えるまで少しだけ待ってから呼ぶ。
            member = guild.me
            for _ in range(AUTONOMOUS_VOICE_STATE_RETRIES):
                if member is not None and member.voice is not None and member.voice.channel is not None:
                    break
                await asyncio.sleep(AUTONOMOUS_VOICE_STATE_WAIT_SECONDS)
                member = guild.me
            # 待っても見えなかった場合はここで諦める。そのまま edit すると
            # channel_id を載せられないリクエストになり、API 側のエラーメッセージが
            # 「権限不足」などに化けて原因が追えなくなる。
            if member is None or member.voice is None or member.voice.channel is None:
                raise RuntimeError(
                    "ボイス状態を取得できず、ステージの発言者になれませんでした"
                    f"（{AUTONOMOUS_VOICE_STATE_RETRIES} 回待機）。"
                )
            await member.edit(suppress=False)
        except Exception:
            # 枠を開いた後で発言者になれなかった場合、そのまま抜けると「会話相手
            # 募集中…」の看板を出したステージが誰も居ないまま開きっぱなしになる。
            # 自分で開いた枠だけは閉じてから、失敗を呼び出し元へ伝える。
            if created_instance is not None:
                instance = channel.instance
                if instance is None or instance.id == created_instance.id:
                    try:
                        await created_instance.delete(reason="ボットの自律行動: 発言者になれなかったため中止")
                    except discord.HTTPException as error:
                        print(f"自律行動: 中止時のステージ枠の終了に失敗しました（{channel.name}）: {error}")
            raise

        return created_instance.id if created_instance is not None else None

    async def stop_session(
        self,
        guild_id: int,
        *,
        reason: str,
        notice: str | None = None,
    ) -> bool:
        """進行中の配信を終了する。終了したら True、そもそも配信していなければ False。

        `/leave`・`/join` からも呼ばれる（人の操作による強制終了）。

        **例外を外へ出さない。** 締めの投稿やステージ枠の後始末が失敗しても、
        ボイス接続の切断とセッション記録の破棄までは必ず進める（片付けが
        途中で止まると、接続だけが残って誰も止められなくなるため）。

        Args:
            reason: ログに残す終了理由。
            notice: 配信チャンネルへ残す締めの一言（None なら何も投稿しない）。
        """
        guild_settings = self._state.get_guild(guild_id)
        session = guild_settings.autonomous_session
        if session is None:
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
                self._finish_session_cleanup(guild_id, guild_settings, session, channel, guild, voice_client),
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

    async def _finish_session_cleanup(
        self, guild_id: int, guild_settings: GuildSettings, session: AutonomousSession,
        channel: Any, guild: discord.Guild | None, voice_client: discord.VoiceProtocol | None,
    ) -> None:
        """呼出元のキャンセルから独立して、所有枠の終了と元の接続の切断を完了する。"""
        try:
            await self._close_owned_stage(guild_id, guild_settings, session, channel)
        finally:
            # 新しい配信に同じVoiceClientが引き継がれた場合も、その配信を優先する。
            if guild is not None and voice_client is not None and guild_settings.autonomous_session is None:
                await self._disconnect(guild, expected_voice_client=voice_client)

    async def _close_owned_stage(
        self, guild_id: int, guild_settings: GuildSettings,
        session: AutonomousSession, channel: Any,
    ) -> None:
        """自分が開いた枠だけを閉じる。待機中に始まった別の配信には触らない。"""
        if (not session.stage_started_by_bot or session.stage_instance_id is None
                or not isinstance(channel, discord.StageChannel)
                or guild_settings.autonomous_session is not None):
            return
        try:
            instance = channel.instance
            if instance is None:
                # 作成直後やキック後はキャッシュに枠が無くても、Discord上には残りうる。
                instance = await channel.fetch_instance()
            if (guild_settings.autonomous_session is None
                    and instance.id == session.stage_instance_id):
                await instance.delete(reason="ボットの自律行動による配信終了")
        except discord.HTTPException as error:
            print(f"自律行動: ステージ枠の終了に失敗しました（guild={guild_id}）: {error}")
        except Exception as error:
            print(f"自律行動: ステージ枠の終了で予期せぬエラー（guild={guild_id}）: {type(error).__name__}: {error}")

    def _clear_session(self, guild_settings: GuildSettings) -> None:
        """セッション記録を破棄し、「話したいゲージ」を空にする。

        ゲージを空に戻さないと、終了直後の見回りで満タンのまま再判定され、
        同じサーバーで配信が延々と再開してしまう。
        """
        guild_settings.autonomous_session = None
        guild_settings.autonomous_last_activity = datetime.now(timezone.utc)
        guild_settings.autonomous_recent_activity.clear()

    async def _disconnect(
        self, guild: discord.Guild, *, expected_voice_client: discord.VoiceProtocol | None = None,
    ) -> None:
        """ボイス接続を切る（既に切れていても例外を外へ出さない）。"""
        voice_client = guild.voice_client
        if voice_client is None or (expected_voice_client is not None and voice_client is not expected_voice_client):
            return
        try:
            await voice_client.disconnect(force=True)
        except Exception as error:
            print(f"自律行動: ボイス切断に失敗しました（guild={guild.id}）: {error}")

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
        try:
            body = AUTONOMOUS_OPENING_NOTICE
            speech = ""
            persona_prompt = self._load_persona(guild_settings)
            if persona_prompt is not None:
                history_text = await fetch_channel_history_text(
                    channel, None, self._client.user, AUTONOMOUS_HISTORY_LIMIT
                )
                full_prompt = build_autonomous_opening_prompt(
                    persona_prompt, getattr(channel, "name", ""), history_text
                )
                generated = await self._generate(
                    full_prompt, guild_settings, label="第一声", detail=self._channel_label(channel)
                )
                speech = self._screen_speech(generated, "自律行動の第一声")
                if speech:
                    body = f"{AUTONOMOUS_OPENING_NOTICE}\n{speech}"

            if not self._is_current_session(guild_settings, session):
                return
            await send_long_message(channel, body, allowed_mentions=_NO_MENTIONS)
            if speech:
                await speak_text(guild, speech, guild_settings.current_prompt_file)
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
        try:
            channel = self._client.get_channel(session.channel_id)
            if channel is None:
                return
            persona_prompt = self._load_persona(guild_settings)
            if persona_prompt is None:
                return

            history_text = await fetch_channel_history_text(
                channel, None, self._client.user, AUTONOMOUS_HISTORY_LIMIT
            )
            full_prompt = build_autonomous_monologue_prompt(
                persona_prompt, history_text, session.turn_count
            )
            # 独り言に調べ物は要らないためグラウンディングは使わない（既定の False）。
            generated = await self._generate(
                full_prompt, guild_settings, label="独り言", detail=self._channel_label(channel)
            )
            speech = self._screen_speech(generated, "自律行動の独り言")
            if not speech:
                return

            # 生成を待っている間に配信が終わっている（/leave など）ことがある。
            # 終わったチャンネルへ投稿しないよう、送信の直前で確かめる。
            if not self._is_current_session(guild_settings, session):
                return

            await send_long_message(channel, speech, allowed_mentions=_NO_MENTIONS)
            await speak_text(guild, speech, guild_settings.current_prompt_file)
            session.turn_count += 1
        finally:
            session.busy = False
            session.last_spoke_at = datetime.now(timezone.utc)

    # ==========================================================================
    # 補助
    # ==========================================================================

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
        guild_settings: GuildSettings,
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
            guild_settings.current_model_name,
            images=images,
            timeout=AUTONOMOUS_TIMEOUT_SECONDS,
            grounding=grounding,
        )
        if not response_text:
            print(f"自律行動: {label}の生成に失敗しました。")
            return None
        self._usage.log_snapshot(f"自律行動/{label}", detail, "自動実行", full_prompt, response_text, usage_info)
        return response_text

    def _load_persona(self, guild_settings: GuildSettings) -> str | None:
        """そのサーバーで選ばれているペルソナのプロンプトを読む（無ければ None）。"""
        try:
            return load_prompt_template(resolve_prompt_path(guild_settings.current_prompt_file))
        except FileNotFoundError:
            print(f"自律行動: プロンプトファイル({guild_settings.current_prompt_file})が見つかりません。")
            return None

    @staticmethod
    def _channel_label(channel: Any) -> str:
        return getattr(channel, "name", None) or f"channel {getattr(channel, 'id', '不明')}"

    def _is_quiet_hours(self) -> bool:
        """いま自律行動を禁止する時間帯（既定 00:00〜05:59）かどうかを返す。

        ゲージは無会話時間で増えるため、誰も居ない深夜ほど早く満タンになる。
        寝ている間にボイスチャンネルで喋り続けないための歯止め（idea.md の制限）。
        開始 > 終了（例: 22時〜6時）のように日をまたぐ指定にも対応する。
        """
        start = AUTONOMOUS_QUIET_HOURS_START
        end = AUTONOMOUS_QUIET_HOURS_END
        if start == end:
            return False  # 同じ値なら制限なしとして扱う
        hour = datetime.now(self._tz).hour
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def _pick_channel(self, guild: discord.Guild) -> VocalChannel | None:
        """出ていく先のチャンネルを1つ選ぶ。ステージを優先し、無ければボイスから選ぶ。

        候補が複数あればランダムに選ぶ（毎回同じ場所へ出ると単調になるため）。

        権限は必ず `permissions_for(guild.me)` で**チャンネル単位に**確認する。
        サーバー全体のロールで許可されていても、チャンネル個別の設定で拒否
        されていることがあるため（`/join` の接続権限チェックと同じ作法）。
        「入ってみて失敗したら次」にしないのは、無駄に API を叩くうえ、
        権限エラーとネットワーク障害を区別できなくなるため。
        """
        bot_member = guild.me
        if bot_member is None:
            # キャッシュ未取得などで自分自身の Member が引けないときは権限を判定できない。
            return None

        stages = [c for c in guild.stage_channels if self._can_use_stage(c, bot_member)]
        if stages:
            return random.choice(stages)
        voices = [c for c in guild.voice_channels if self._can_use_voice(c, bot_member)]
        if voices:
            return random.choice(voices)
        return None

    @staticmethod
    def _can_use_voice(channel: discord.VoiceChannel, bot_member: discord.Member) -> bool:
        """そのボイスチャンネルで配信できるか。

        独り言はチャンネル付属のチャットにも投稿し、そこがコメントの受付口にも
        なるため、接続・発話だけでなく閲覧と送信の権限も要る。
        """
        permissions = channel.permissions_for(bot_member)
        return bool(
            permissions.view_channel
            and permissions.connect
            and permissions.speak
            and permissions.send_messages
        )

    @staticmethod
    def _can_use_stage(channel: discord.StageChannel, bot_member: discord.Member) -> bool:
        """そのステージチャンネルで発言者として配信できるか。

        ステージは参加しただけでは観客（suppress=True）で音声を流せず、
        自分の抑制を解除するには mute_members が要る。さらにステージ枠が
        まだ無ければ自分で開く必要があり、それにはステージモデレーターの
        3権限（manage_channels / mute_members / move_members）が要る。
        条件を満たさないステージは候補から外す（入っても喋れないため）。
        """
        permissions = channel.permissions_for(bot_member)
        if not (permissions.view_channel and permissions.connect and permissions.send_messages):
            return False
        if not permissions.mute_members:
            return False
        if channel.instance is None and not (permissions.manage_channels and permissions.move_members):
            return False
        return True
