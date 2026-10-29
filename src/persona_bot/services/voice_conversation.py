"""Gemini Live による音声会話（/join。#268 の Persona Voice Controller）。

`/join` でボイスチャンネルへ入ったボットと、参加者が声で会話する。1つのボイスチャンネルに
つき1つの Live セッション（`live_session.LiveSession`）を使い、次の部品をここでつなぐ。

- **同意（ConsentState）**: ボイスチャンネルの人間の参加者（ブラックリスト登録者を除く）
  全員が音声処理に同意するまで、入出力を一時停止する。途中参加者も同じ。記憶への保存・
  親密度の更新への同意は音声処理とは別で、初期はオフ。同意は接続している間だけメモリに持つ。
- **話者の管理（Active Speaker Manager）**: Discord の user_id で話者を見分け、Live へは
  同時に1人分の音声だけを送る（複数人の音声を混ぜない）。話し始め・話し終わりはローカルの
  VAD（音量としきい値）で判定し、`activity_start` / `activity_end` で Live へ伝える。
  ほかの人が話している間の「話したい」は発話希望として短時間だけ覚える（音声は溜めない）。
- **割り込み**: ボットが話している最中に参加者が話し始めたら（`LIVE_BARGE_IN_MS` 以上）、
  ボットの再生を止めて未再生の音声を捨て、新しい発言へ切り替える。受信は止めない。
- **モデレーション（並行検査＋短時間バッファ）**: 入力・出力の文字起こしをローカルの
  NG ワードで即座に、AI モデレーションで生成と並行して検査する。違反を見つけたら再生を
  止めて残りを捨てる（検出前に再生された部分は取り消せないことは仕様上許容する）。
  AI の判定不能は再生を止めないが、記憶・親密度の更新には使わない（条件付き fail-open）。
  同じ発言の違反は最大1回だけ数え、ボット自身の出力の違反は参加者に数えない。
- **記憶・親密度**: 個人の長期記憶・親密度は Live へ渡さない（共有の場で漏らさない）。
  記憶・親密度に同意した人の、入出力とも AI が安全と判定した発言だけを保存・反映する。

生の音声・録音・文字起こしの全文・会話ログは保存しない（処理中だけメモリに持つ）。
"""

from __future__ import annotations

import logging
import asyncio
from collections import deque
from dataclasses import dataclass, field
from statistics import quantiles
from time import monotonic
from typing import Any, Callable

import discord

from persona_bot.audio.live_audio import DISCORD_FRAME_BYTES, LivePlaybackSource, Upsampler, frame_rms
from persona_bot.audio.voice_receive import LiveVoiceClient, VoiceReceiver
from persona_bot.config.constants import (
    LIVE_BARGE_IN_MS,
    LIVE_CONSENT_PANEL_UPDATE_SECONDS,
    LIVE_INPUT_CHUNK_MS,
    LIVE_INPUT_QUEUE_MAX_CHUNKS,
    LIVE_INPUT_SAMPLE_RATE,
    LIVE_INPUT_TRANSCRIPT_MAX_WAIT_SECONDS,
    LIVE_INPUT_TRANSCRIPT_SETTLE_SECONDS,
    LIVE_MAX_CONCURRENT_SESSIONS,
    LIVE_MAX_UTTERANCE_SECONDS,
    LIVE_OUTPUT_BUFFER_MS,
    LIVE_PLAYBACK_STALL_SECONDS,
    LIVE_SPEAKER_QUEUE_MAX,
    LIVE_SPEAKER_REQUEST_TTL_SECONDS,
    LIVE_TICK_SECONDS,
    LIVE_TRANSCRIPT_MAX_CHARS,
    LIVE_VAD_ENERGY_THRESHOLD,
    LIVE_VAD_PREROLL_MS,
    LIVE_VAD_SILENCE_MS,
    LIVE_VAD_START_GAP_MS,
    LIVE_VAD_START_MS,
    BLACKLIST_MAX_VIOLATIONS,
)
from persona_bot.config.paths import load_persona_template
from persona_bot.config.personas import current_persona_catalog
from persona_bot.core.background import spawn, wait_for_cleanup
from persona_bot.services.content_moderator import VERDICT_SAFE, VERDICT_UNSAFE
from persona_bot.services.live_session import CLOSED_QUOTA, LiveOptions, LiveSession
from persona_bot.services.prompting import build_live_system_instruction
from persona_bot.utils.discord_helpers import can_bot_send_messages


logger = logging.getLogger(__name__)

# 16kHz・モノラル・16bit の1ミリ秒あたりのバイト数。
_INPUT_BYTES_PER_MS = LIVE_INPUT_SAMPLE_RATE * 2 // 1000
_INPUT_CHUNK_BYTES = _INPUT_BYTES_PER_MS * LIVE_INPUT_CHUNK_MS
# 再生を始めるまでに溜める応答音声（48kHz・ステレオ）のバイト数。
_OUTPUT_BUFFER_BYTES = DISCORD_FRAME_BYTES * max(1, LIVE_OUTPUT_BUFFER_MS // 20)
_PLAYBACK_STALL_FRAMES = int(LIVE_PLAYBACK_STALL_SECONDS * 50)
_PREROLL_FRAMES = max(1, LIVE_VAD_PREROLL_MS // 20)

_NO_MENTIONS = discord.AllowedMentions.none()

# 利用者に見せる案内（接続の結果・終了の理由）。
NOTICE_LIVE_UNAVAILABLE = (
    "⚠️ 音声会話（Gemini Live）に接続できなかったため、ボイスチャンネルから退出しました。"
    "しばらく待ってからもう一度 /join してください。"
)
NOTICE_LIVE_QUOTA = (
    "⚠️ 音声会話（Gemini Live）の利用枠の上限に達したため、ボイスチャンネルから退出しました。"
    "時間をおいてからもう一度 /join してください。"
)
NOTICE_LIVE_FAILED = (
    "⚠️ 音声会話（Gemini Live）との接続が切れ、つなぎ直せなかったため退出しました。"
    "もう一度 /join してください。"
)
NOTICE_LIVE_CAPACITY = (
    f"⚠️ 同時に音声会話できるボイスチャンネルの上限（{LIVE_MAX_CONCURRENT_SESSIONS}か所）に達しています。"
    "ほかのボイスチャンネルでの会話が終わってから、もう一度 /join してください。"
)
NOTICE_CONSENT_PANEL_FAILED = (
    "⚠️ 同意パネルを投稿できなかったため、音声会話を始めずに退出しました。"
    "ボイスチャンネルのチャットか /join を実行したチャンネルで、ボットに"
    "「チャンネルを見る」「メッセージを送信」の権限があるか確認してください。"
)
_NOTICE_INPUT_VIOLATION = "不適切な発言が検出されたため、応答を停止しました。"


class ConsentPanelError(Exception):
    """同意パネルを出せない。同意を集められないため、音声会話を始めない。"""


@dataclass
class ConsentState:
    """接続中だけ保持する同意（user_id の集合）。記憶・親密度は音声処理とは別に数える。"""

    voice: set[int] = field(default_factory=set)
    memory: set[int] = field(default_factory=set)
    affinity: set[int] = field(default_factory=set)


@dataclass
class _Participant:
    """1人分のローカル VAD の状態。プリロールは話し始めの判定前の音声（語頭の欠け防止）。"""

    preroll: deque[bytes] = field(default_factory=lambda: deque(maxlen=_PREROLL_FRAMES))
    voiced_ms: float = 0.0
    last_voiced_at: float = float("-inf")


@dataclass
class _Utterance:
    """参加者の1回の発言と、それに対するボットの返答（1ターン分）。"""

    turn_id: int
    user_id: int
    started_at: float
    ended_at: float | None = None
    input_text: str = ""
    input_updated_at: float = float("-inf")
    output_text: str = ""
    input_verdict: str | None = None
    output_verdict: str | None = None
    emotion: tuple[int, int] | None = None
    # 返答を捨てた（違反・割り込み・一時停止）。以後の音声は再生しない。
    discarded: bool = False
    violation_recorded: bool = False
    output_ng: bool = False
    response_started: bool = False
    response_complete: bool = False
    first_audio_at: float | None = None
    input_task: asyncio.Task[Any] | None = None
    # 入力の文字起こしが出そろった（サーバーの finished・返答の終わり・割り込みの通知・時間切れ）。
    transcript_closed: bool = False
    # 帰属を確定できない文字起こしを捨てた。足りない文字起こしは記憶に使わない。
    transcript_incomplete: bool = False


class VoiceConversation:
    """1つのボイスチャンネルの音声会話（Persona Voice Controller）。"""

    def __init__(
        self,
        service: VoiceConversationService,
        guild: discord.Guild,
        voice_client: LiveVoiceClient,
        channel_id: int,
        notice_channel: Any,
        options: LiveOptions,
        *,
        now: Callable[[], float] = monotonic,
    ) -> None:
        self._service = service
        self.guild = guild
        self.voice_client = voice_client
        self.channel_id = channel_id
        self.notice_channel = notice_channel
        self._now = now
        self.consent = ConsentState()
        self._live = LiveSession(
            service.genai_client, options,
            on_message=self._on_live_message,
            on_reconnected=self._on_live_reconnected,
            on_closed=self._on_live_closed,
            connect=service.live_connect,
        )
        self._receiver: VoiceReceiver | None = None
        self._participants: dict[int, _Participant] = {}
        self._reservations: deque[tuple[int, float]] = deque()
        self._active_user: int | None = None
        self._speaking = False
        self._current: _Utterance | None = None
        self._turn_seq = 0
        self._awaiting_interrupt_ack = False
        self._interrupt_ack_deadline = float("-inf")
        self._playback: LivePlaybackSource | None = None
        self._playback_owner: _Utterance | None = None
        self._playback_started = False
        self._upsampler = Upsampler()
        self._send_queue: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue()
        # いまの Live の接続（世代）で activity_start を送ったか。送っていなければ None。
        self._open_generation: int | None = None
        self._pending_chunk = bytearray()
        self._queue_full_warned = False
        self._gate_open = False
        self._waiting_count = 0
        # 入力の文字起こしの帰属先。発話権（_current）とは別に持つ — 前の人の文字起こしが
        # 遅れて届いても、次に話し始めた人の発言として扱わない（違反・記憶を取り違えない）。
        self._transcript_owner: _Utterance | None = None
        # 前の人（_transcript_blocked）の文字起こしが閉じないうちに次の人が話し始めた間は、
        # 届いた断片をどちらにも帰属させずに捨てる。閉じたら _transcript_next が帰属先になる。
        self._transcript_blocked: _Utterance | None = None
        self._transcript_next: _Utterance | None = None
        self._transcript_blocked_since = float("-inf")
        # 割り込まれた発言。サーバーの割り込みの通知で、その文字起こしを閉じる。
        self._interrupted: _Utterance | None = None
        self._stopped = False
        self._end_requested = False
        self._tasks: list[asyncio.Task[Any]] = []
        self._latencies: list[float] = []
        self.panel_message: Any = None
        self.panel_view: Any = None
        self._panel_update_task: asyncio.Task[Any] | None = None
        self._panel_updated_at = float("-inf")

    # ==================================================================
    # 開始・終了
    # ==================================================================

    @property
    def stopped(self) -> bool:
        return self._stopped

    async def start(self) -> None:
        """Live へ接続し、受信と見回りを始める。接続できなければ LiveUnavailableError。"""
        await self._live.start()
        self._receiver = self._service.receiver_factory(self.voice_client, self._on_audio)
        self._receiver.start()
        self._tasks.append(spawn(self._run_sender(), label="音声会話の送信"))
        self._tasks.append(spawn(self._run_ticks(), label="音声会話の見回り"))
        self._update_gate()

    async def stop(self) -> None:
        """受信・再生・Live をまとめて止める（何度呼んでもよい）。切断はしない。"""
        if self._stopped:
            return
        self._stopped = True
        current = asyncio.current_task()
        for task in self._tasks:
            if task is not current:
                task.cancel()
        try:
            if self._receiver is not None:
                self._receiver.stop()
        except Exception:
            logger.warning("音声会話: 受信の停止に失敗しました。", exc_info=True)
        self._abort_playback()
        self._clear_send_queue()
        self._participants.clear()
        self._reservations.clear()
        await wait_for_cleanup(self._live.close(), label="音声会話の Live の切断")
        await asyncio.gather(*(task for task in self._tasks if task is not current), return_exceptions=True)
        if self._panel_update_task is not None and self._panel_update_task is not current:
            self._panel_update_task.cancel()
        self._log_latency()
        await self._finish_panel()

    def _log_latency(self) -> None:
        if not self._latencies:
            return
        ordered = sorted(self._latencies)
        if len(ordered) >= 2:
            cuts = quantiles(ordered, n=20, method="inclusive")
            p50, p95 = cuts[9], cuts[18]
        else:
            p50 = p95 = ordered[0]
        logger.info(
            "音声会話: 発話終了から初回音声までの遅延 P50=%.2f秒 / P95=%.2f秒（%d回）",
            p50, p95, len(ordered),
        )

    # ==================================================================
    # 同意（同意パネル・参加者の出入りから呼ばれる）
    # ==================================================================

    def grant_voice(self, user_id: int) -> None:
        self.consent.voice.add(user_id)
        self._after_consent_change()

    def withdraw(self, user_id: int) -> None:
        """音声処理への同意を撤回する。記憶・親密度への同意も一緒に外す。"""
        had_consent = user_id in self.consent.voice
        self.consent.voice.discard(user_id)
        self.consent.memory.discard(user_id)
        self.consent.affinity.discard(user_id)
        if had_consent:
            # 撤回した人の発言が残った文脈を使い続けない。撤回した人がボイスチャンネルに
            # いない（ゲートが開いたまま）場合もあるため、開閉を待たずに新しいセッションへ張り替える。
            self._live.forget_context()
            spawn(self._live.restart_fresh(), label="音声会話の文脈の破棄")
        if self._active_user == user_id:
            self._end_activity(discard=True)
        self._participants.pop(user_id, None)
        self._remove_reservation(user_id)
        self._after_consent_change()

    def toggle_memory(self, user_id: int) -> bool:
        return self._toggle(self.consent.memory, user_id)

    def toggle_affinity(self, user_id: int) -> bool:
        return self._toggle(self.consent.affinity, user_id)

    def _toggle(self, target: set[int], user_id: int) -> bool:
        if user_id in target:
            target.discard(user_id)
        else:
            target.add(user_id)
        self._schedule_panel_update()
        return user_id in target

    def _after_consent_change(self) -> None:
        self._update_gate()
        self._schedule_panel_update()

    def handle_member_update(self, member: Any, before: Any, after: Any) -> None:
        """参加者の出入り。抜けた人の発話権・発話希望・プリロールを無効にする。"""
        before_id = getattr(getattr(before, "channel", None), "id", None)
        after_id = getattr(getattr(after, "channel", None), "id", None)
        if before_id == after_id:
            return
        if before_id == self.channel_id:
            if self._active_user == member.id:
                self._end_activity(discard=True)
            self._participants.pop(member.id, None)
            self._remove_reservation(member.id)
        self._update_gate()
        self._schedule_panel_update()

    # ==================================================================
    # 開閉（全員の同意がそろっているか）
    # ==================================================================

    def _eligible_members(self) -> list[Any]:
        channel = getattr(self.voice_client, "channel", None)
        if channel is None or getattr(channel, "id", None) != self.channel_id:
            # 別のチャンネルへ移されている間（voice_guard が戻すまで）は誰の声も扱わない。
            return []
        return [
            member for member in getattr(channel, "members", [])
            if not member.bot and not self._service.is_blocked(member.id)
        ]

    def _update_gate(self) -> None:
        if self._stopped:
            return
        eligible = self._eligible_members()
        waiting = [member for member in eligible if member.id not in self.consent.voice]
        is_open = bool(eligible) and not waiting
        self._waiting_count = len(waiting)
        if is_open == self._gate_open:
            return
        self._gate_open = is_open
        if is_open:
            logger.info("音声会話: 参加者全員の同意がそろったため、会話を再開します。")
        else:
            logger.info("音声会話: 同意していない参加者がいるため、会話を一時停止します。")
            self._pause()
        self._schedule_panel_update()

    def _pause(self) -> None:
        if self._speaking:
            self._end_activity(discard=True)
        if self._current is not None:
            self._discard_response(self._current, "一時停止")
        self._abort_playback()
        self._clear_send_queue()
        self._reservations.clear()
        for participant in self._participants.values():
            participant.preroll.clear()
            participant.voiced_ms = 0.0

    @property
    def is_open(self) -> bool:
        return self._gate_open

    @property
    def waiting_count(self) -> int:
        return self._waiting_count

    # ==================================================================
    # 受信した音声（イベントループ上で呼ばれる）
    # ==================================================================

    def _on_audio(self, user_id: int, pcm: bytes) -> None:
        if self._stopped or not self._gate_open or not self._live.connected:
            return
        if user_id not in self.consent.voice or self._service.is_blocked(user_id):
            # 未同意・ブラックリスト登録者の声は Live へ送らずに捨てる。
            return
        now = self._now()
        participant = self._participants.setdefault(user_id, _Participant())
        voiced = frame_rms(pcm) >= LIVE_VAD_ENERGY_THRESHOLD
        if self._speaking and self._active_user == user_id:
            self._queue_audio(pcm)
            if voiced:
                participant.last_voiced_at = now
            return

        participant.preroll.append(pcm)
        if now - participant.last_voiced_at > LIVE_VAD_START_GAP_MS / 1000:
            participant.voiced_ms = 0.0
        if not voiced:
            return
        participant.voiced_ms += len(pcm) / _INPUT_BYTES_PER_MS
        participant.last_voiced_at = now
        threshold = LIVE_BARGE_IN_MS if self._bot_speaking() else LIVE_VAD_START_MS
        if participant.voiced_ms < threshold:
            return
        if self._speaking or not self._floor_available_for(user_id, now):
            self._request_floor(user_id, now)
            return
        self._begin_activity(user_id, participant, now)

    def _bot_speaking(self) -> bool:
        """ボットが返答中か（生成中・再生中）。話し始めのしきい値を割り込み用に上げる。"""
        if self._playback is not None and not self._playback.aborted:
            return True
        current = self._current
        if current is None or current.ended_at is None or current.response_complete or current.discarded:
            return False
        if current.response_started:
            return True
        # 返答がまだ届かない間も返答待ちとみなす。ただし返答が来ないまま時間が過ぎたら
        # 待つのをやめる（いつまでも話し始めのしきい値を割り込み用のままにしない）。
        return self._now() - current.ended_at < LIVE_PLAYBACK_STALL_SECONDS

    def _begin_activity(self, user_id: int, participant: _Participant, now: float) -> None:
        if self._bot_speaking():
            logger.info("音声会話: 返答中の割り込みを検出したため、再生を止めます。")
            previous = self._current
            if previous is not None:
                if previous.response_started and not previous.response_complete:
                    # 生成中の返答の残りが届いても、新しい発言の返答と取り違えない。
                    # サーバーが割り込みを知らせないまま終わっても、待ち続けないよう期限を設ける。
                    self._awaiting_interrupt_ack = True
                    self._interrupt_ack_deadline = now + LIVE_PLAYBACK_STALL_SECONDS
                    self._interrupted = previous
                self._discard_response(previous, "割り込み")
            self._abort_playback()
        self._turn_seq += 1
        self._current = _Utterance(self._turn_seq, user_id, now)
        self._assign_transcript_owner(self._current, now)
        self._active_user = user_id
        self._speaking = True
        self._remove_reservation(user_id)
        self._enqueue("start")
        for frame in participant.preroll:
            self._queue_audio(frame)
        participant.preroll.clear()
        participant.voiced_ms = 0.0

    # --- 入力の文字起こしの帰属（発話権とは別に管理する） ---------------------------

    def _assign_transcript_owner(self, utterance: _Utterance, now: float) -> None:
        """話し始めた発言を、入力の文字起こしの帰属先にする（前の人の分が閉じていれば）。"""
        if self._transcript_blocked is not None:
            # 帰属を確定できない間に、さらに次の人が話し始めた。間の人の文字起こしは
            # 誰の分とも確定できないまま捨てる。
            if self._transcript_next is not None:
                self._transcript_next.transcript_incomplete = True
            self._transcript_next = utterance
            return
        owner = self._transcript_owner
        if owner is None or owner.transcript_closed:
            self._transcript_owner = utterance
            return
        # 前の人の文字起こしがまだ届きうる。閉じるまでの断片は、前の人の残りか次の人の
        # 冒頭か区別できないため、どちらにも帰属させない。
        self._transcript_blocked = owner
        self._transcript_next = utterance
        self._transcript_owner = None
        self._transcript_blocked_since = now

    def _close_transcript(self, utterance: _Utterance | None) -> None:
        if utterance is None:
            return
        utterance.transcript_closed = True
        if utterance is self._transcript_blocked or utterance is self._transcript_next:
            # 前の人の分が閉じた（または次の人の分まで出そろった）。以後の断片は次の人の分。
            self._transcript_blocked = None
            self._transcript_owner, self._transcript_next = self._transcript_next, None

    def _reset_transcripts(self) -> None:
        """接続の張り替えで、届きうる文字起こしが無くなったときに帰属の状態を戻す。"""
        self._close_transcript(self._transcript_blocked)
        self._close_transcript(self._transcript_owner)
        self._transcript_owner = self._transcript_blocked = self._transcript_next = None
        self._interrupted = None

    def _end_activity(self, *, discard: bool = False) -> None:
        if not self._speaking:
            return
        utterance = self._current
        self._flush_chunk()
        self._enqueue("end")
        self._speaking = False
        self._active_user = None
        now = self._now()
        if utterance is not None:
            utterance.ended_at = now
            if discard:
                utterance.discarded = True
            else:
                utterance.input_task = spawn(self._judge_input(utterance), label="音声会話の入力判定")
        # 待っていた人に、少しの間だけ優先して話す機会を渡す。
        self._reservations = deque(
            (user_id, now + LIVE_SPEAKER_REQUEST_TTL_SECONDS) for user_id, _ in self._reservations
        )

    def _request_floor(self, user_id: int, now: float) -> None:
        if any(queued == user_id for queued, _ in self._reservations):
            return
        if len(self._reservations) >= LIVE_SPEAKER_QUEUE_MAX:
            return
        self._reservations.append((user_id, now + LIVE_SPEAKER_REQUEST_TTL_SECONDS))

    def _remove_reservation(self, user_id: int) -> None:
        self._reservations = deque(item for item in self._reservations if item[0] != user_id)

    def _floor_available_for(self, user_id: int, now: float) -> bool:
        while self._reservations and self._reservations[0][1] < now:
            self._reservations.popleft()
        return not self._reservations or self._reservations[0][0] == user_id

    # ==================================================================
    # Live への送信（発話の区切りと音声の順序を保つため、1本の待ち行列で送る）
    # ==================================================================

    def _enqueue(self, kind: str, payload: bytes = b"") -> None:
        self._send_queue.put_nowait((kind, payload))

    def _queue_audio(self, pcm: bytes) -> None:
        self._pending_chunk.extend(pcm)
        if len(self._pending_chunk) >= _INPUT_CHUNK_BYTES:
            self._flush_chunk()

    def _flush_chunk(self) -> None:
        if not self._pending_chunk:
            return
        chunk = bytes(self._pending_chunk)
        self._pending_chunk.clear()
        if self._send_queue.qsize() >= LIVE_INPUT_QUEUE_MAX_CHUNKS:
            if not self._queue_full_warned:
                self._queue_full_warned = True
                logger.warning("音声会話: Live への送信が詰まっているため、音声の一部を捨てました。")
            return
        self._enqueue("audio", chunk)

    def _clear_send_queue(self) -> None:
        """送信待ちの音声だけを捨てる。

        発話の区切り（start / end）は残す — 送信済みの start に対応する end まで消すと、
        Live 側で発話が開いたままになる。対応の取れない区切りは送信側が捨てる。
        """
        self._pending_chunk.clear()
        markers = []
        while not self._send_queue.empty():
            item = self._send_queue.get_nowait()
            if item[0] != "audio":
                markers.append(item)
        for item in markers:
            self._send_queue.put_nowait(item)

    async def _run_sender(self) -> None:
        while not self._stopped:
            kind, payload = await self._send_queue.get()
            generation = self._live.generation
            if kind == "start":
                if await self._live.activity_start():
                    self._open_generation = generation
            elif self._open_generation != generation:
                # 再接続した新しい接続へ、対応する start の無い音声・end を送らない。
                continue
            elif kind == "audio":
                await self._live.send_audio(payload)
            else:
                await self._live.activity_end()
                self._open_generation = None

    # ==================================================================
    # 見回り（話し終わりの判定・接続の確認）
    # ==================================================================

    async def _run_ticks(self) -> None:
        while not self._stopped:
            await asyncio.sleep(LIVE_TICK_SECONDS)
            self.tick()

    def tick(self) -> None:
        if self._stopped:
            return
        guild_voice = getattr(self.guild, "voice_client", None)
        if guild_voice is not self.voice_client or not self.voice_client.is_connected():
            # /leave 以外の切断（voice_guard の退出・外部からの切断）。
            if not self._end_requested:
                self._end_requested = True
                spawn(
                    self._service.end(self.guild.id, reason="ボイス接続が切れた", disconnect=False, conversation=self),
                    label="音声会話の終了（切断）",
                )
            return
        self._update_gate()
        if (self._transcript_blocked is not None
                and self._now() - self._transcript_blocked_since >= LIVE_PLAYBACK_STALL_SECONDS):
            # 前の人の文字起こしが閉じる合図が来ないまま時間が過ぎた。以後の断片は次の人の分とみなす
            # （次の人の冒頭は捨てた可能性があるため、その発言は記憶に使わない）。
            self._close_transcript(self._transcript_blocked)
        if self._speaking and self._active_user is not None:
            participant = self._participants.get(self._active_user)
            now = self._now()
            started = self._current.started_at if self._current is not None else now
            silent = participant is None or now - participant.last_voiced_at >= LIVE_VAD_SILENCE_MS / 1000
            if silent or now - started >= LIVE_MAX_UTTERANCE_SECONDS:
                self._end_activity()

    # ==================================================================
    # Live からの応答
    # ==================================================================

    async def _on_live_message(self, message: Any) -> None:
        if self._stopped:
            return
        usage = getattr(message, "usage_metadata", None)
        if usage:
            self._service.usage.log_tokens("音声会話", usage)
        content = getattr(message, "server_content", None)
        if content is None:
            return
        transcription = getattr(content, "input_transcription", None)
        if transcription is not None:
            self._on_input_transcript(transcription.text or "", bool(getattr(transcription, "finished", None)))
        if getattr(content, "interrupted", None):
            self._awaiting_interrupt_ack = False
            self._abort_playback()
            # 割り込まれた発言は、生成が始まった時点で入力を処理し終えている。
            self._close_transcript(self._interrupted)
            self._interrupted = None
        model_turn = getattr(content, "model_turn", None)
        if model_turn is not None:
            for part in model_turn.parts or []:
                inline = getattr(part, "inline_data", None)
                if inline is not None and inline.data:
                    self._on_model_audio(inline.data)
        transcription = getattr(content, "output_transcription", None)
        if transcription is not None and transcription.text:
            self._on_output_transcript(transcription.text)
        if getattr(content, "generation_complete", None) or getattr(content, "turn_complete", None):
            self._on_generation_complete()
        if getattr(content, "turn_complete", None):
            self._on_turn_complete()

    def _accepting_response(self) -> _Utterance | None:
        """いま届いた応答を受け取る発言。受け取らない（捨てる）なら None。"""
        if self._awaiting_interrupt_ack and self._now() >= self._interrupt_ack_deadline:
            self._awaiting_interrupt_ack = False
        if self._awaiting_interrupt_ack or self._speaking or not self._gate_open:
            return None
        current = self._current
        if current is None or current.ended_at is None or current.discarded:
            return None
        return current

    def _on_input_transcript(self, text: str, finished: bool) -> None:
        utterance = self._transcript_owner
        if utterance is None:
            blocked = self._transcript_blocked
            if blocked is None:
                return
            if text:
                # 前の人の残りか次の人の冒頭か区別できない断片は捨てる（別の人へ帰属させない）。
                blocked.transcript_incomplete = True
                if self._transcript_next is not None:
                    self._transcript_next.transcript_incomplete = True
                logger.debug("音声会話: 話者を確定できない文字起こしを捨てました。")
            if finished:
                self._close_transcript(blocked)
            return
        if text:
            utterance.input_text = (utterance.input_text + text)[:LIVE_TRANSCRIPT_MAX_CHARS]
            utterance.input_updated_at = self._now()
            if self._service.ng_filter.contains_ng_word(utterance.input_text):
                self._reject_input(utterance, "入力の NG ワード")
        if finished:
            self._close_transcript(utterance)

    def _on_model_audio(self, data: bytes) -> None:
        utterance = self._accepting_response()
        if utterance is None:
            return
        if utterance.first_audio_at is None:
            utterance.first_audio_at = self._now()
        utterance.response_started = True
        if self._playback is None or self._playback_owner is not utterance:
            self._abort_playback()
            self._playback = LivePlaybackSource(stall_frames=_PLAYBACK_STALL_FRAMES)
            self._playback_owner = utterance
            self._playback_started = False
            self._upsampler = Upsampler()
        self._playback.feed(self._upsampler.process(data))
        if not self._playback_started and self._playback.buffered_bytes >= _OUTPUT_BUFFER_BYTES:
            self._start_playback()

    def _on_output_transcript(self, text: str) -> None:
        utterance = self._accepting_response()
        if utterance is None:
            return
        utterance.response_started = True
        utterance.output_text = (utterance.output_text + text)[:LIVE_TRANSCRIPT_MAX_CHARS]
        if self._service.ng_filter.contains_ng_word(utterance.output_text):
            # ボット自身の出力の違反は、直前の参加者に数えない（再生を止めるだけ）。
            utterance.output_ng = True
            self._discard_response(utterance, "出力の NG ワード")

    def _on_generation_complete(self) -> None:
        if self._playback is None or self._playback_owner is not self._current:
            return
        self._playback.finish()
        if not self._playback_started:
            # バッファに満たない短い返答も、ここで流し始める。
            self._start_playback()

    def _on_turn_complete(self) -> None:
        if self._awaiting_interrupt_ack:
            # 割り込まれた古い返答の終わり。新しい発言の返答はこの後に届く。
            self._awaiting_interrupt_ack = False
            self._close_transcript(self._interrupted)
            self._interrupted = None
            return
        utterance = self._current
        if utterance is None or utterance.ended_at is None or not utterance.response_started:
            return
        # 返答が終わった時点で、その発言の入力はサーバーが処理し終えている。
        self._close_transcript(utterance)
        if utterance.response_complete:
            return
        utterance.response_complete = True
        spawn(self._judge_output(utterance), label="音声会話の出力判定")

    def _on_live_reconnected(self) -> None:
        # 新しい接続は、切れる前の発話の区切りを知らない。途中の発言と返答は捨てる。
        if self._speaking:
            self._end_activity(discard=True)
        if self._current is not None:
            self._discard_response(self._current, "Live の再接続")
        # 古い接続の文字起こしはもう届かない。
        self._reset_transcripts()
        self._awaiting_interrupt_ack = False
        self._clear_send_queue()

    async def _on_live_closed(self, reason: str) -> None:
        notice = NOTICE_LIVE_QUOTA if reason == CLOSED_QUOTA else NOTICE_LIVE_FAILED
        # 受信ループの中から自分を止めないよう、終了は別タスクで行う。
        spawn(
            self._service.end(self.guild.id, reason="Live の終了", notice=notice, disconnect=True, conversation=self),
            label="音声会話の終了（Live）",
        )

    # ==================================================================
    # 再生
    # ==================================================================

    def _start_playback(self) -> None:
        source = self._playback
        if source is None or self._playback_started:
            return
        self._playback_started = True
        owner = self._playback_owner
        if owner is not None and owner.ended_at is not None:
            # 性能目標（発話終了から初回音声の再生まで3秒以内）の計測点。終了時に P50/P95 を出す。
            self._latencies.append(self._now() - owner.ended_at)
        loop = asyncio.get_running_loop()

        def after_playing(error: Exception | None) -> None:
            # discord.py の再生スレッドから呼ばれる。
            if error:
                logger.error("音声会話: 再生エラー。", exc_info=error)
            loop.call_soon_threadsafe(self._on_playback_finished, source)

        try:
            if self.voice_client.is_playing() or self.voice_client.is_paused():
                self.voice_client.stop()
            self.voice_client.play(source, after=after_playing)
        except Exception as error:
            logger.error("音声会話: 再生を開始できませんでした。", exc_info=error)
            source.abort()
            self._on_playback_finished(source)

    def _on_playback_finished(self, source: LivePlaybackSource) -> None:
        if self._playback is source:
            self._playback = None
            self._playback_owner = None
            self._playback_started = False

    def _abort_playback(self) -> None:
        source = self._playback
        if source is None:
            return
        source.abort()
        self._playback = None
        self._playback_owner = None
        started, self._playback_started = self._playback_started, False
        if started:
            try:
                if self.voice_client.is_playing() or self.voice_client.is_paused():
                    self.voice_client.stop()
            except Exception:
                logger.debug("音声会話: 再生の停止に失敗しました。", exc_info=True)

    def _discard_response(self, utterance: _Utterance, reason: str) -> None:
        if not utterance.discarded:
            logger.info("音声会話: 返答を破棄しました（%s）。", reason)
        utterance.discarded = True
        if self._playback_owner is utterance:
            self._abort_playback()

    # ==================================================================
    # モデレーション・記憶・親密度
    # ==================================================================

    def _reject_input(self, utterance: _Utterance, reason: str) -> None:
        self._discard_response(utterance, reason)
        if utterance.violation_recorded:
            return
        # 同じ発言の違反は1回だけ数える（文字起こしは少しずつ届くため）。
        utterance.violation_recorded = True
        spawn(self._record_violation(utterance.user_id), label="音声会話の違反記録")

    async def _record_violation(self, user_id: int) -> None:
        member = self.guild.get_member(user_id)
        name = str(member) if member is not None else str(user_id)
        try:
            count, just_blocked = await self._service.blacklist.record_violation(user_id, name)
        except Exception as error:
            # 保存できなかった違反は回数に数えない（message_handler.notify_violation と同じ扱い）。
            logger.error("音声会話: 違反の保存に失敗したため、今回の違反は回数に数えていません。", exc_info=error)
            await self._notify(f"⚠️ {_NOTICE_INPUT_VIOLATION}", member)
            return
        if just_blocked:
            logger.info("音声会話: %d回目の違反でブラックリストに登録しました。", count)
            await self._notify(
                f"🚫 違反が{BLACKLIST_MAX_VIOLATIONS}回に達したため、ブラックリストに登録されました。"
                "以降の音声は処理されません。",
                member,
            )
            self._update_gate()
        elif count >= BLACKLIST_MAX_VIOLATIONS:
            logger.warning("音声会話: 既にブロック済みのため警告を省略します。")
        else:
            logger.info("音声会話: 違反を記録しました（%d/%d 回目）。", count, BLACKLIST_MAX_VIOLATIONS)
            remaining = BLACKLIST_MAX_VIOLATIONS - count
            await self._notify(f"⚠️ {_NOTICE_INPUT_VIOLATION}（あと{remaining}回で応答を停止します）", member)

    async def _judge_input(self, utterance: _Utterance) -> None:
        """話し終わった発言の文字起こしが出そろうのを待って、AI モデレーションに掛ける。"""
        waited = 0.0
        while waited < LIVE_INPUT_TRANSCRIPT_MAX_WAIT_SECONDS:
            quiet = self._now() - utterance.input_updated_at
            if utterance.transcript_closed or (utterance.input_text and quiet >= LIVE_INPUT_TRANSCRIPT_SETTLE_SECONDS):
                break
            await asyncio.sleep(LIVE_TICK_SECONDS)
            waited += LIVE_TICK_SECONDS
        verdict = await self._service.moderator.judge_voice_input(utterance.input_text)
        utterance.input_verdict = verdict
        if verdict == VERDICT_UNSAFE and not self._stopped:
            self._reject_input(utterance, "入力の AI モデレーション")

    async def _judge_output(self, utterance: _Utterance) -> None:
        """返答を AI モデレーションに掛け（感情の推定を相乗り）、条件を満たせば記憶・親密度へ反映する。"""
        verdict, emotion = await self._service.moderator.judge_voice_turn(
            utterance.input_text, utterance.output_text,
        )
        utterance.output_verdict = verdict
        utterance.emotion = emotion
        if verdict == VERDICT_UNSAFE:
            # 返答の生成は再生より速いため、判定の時点で残っている未再生の分を止められる。
            self._discard_response(utterance, "出力の AI モデレーション")
        if utterance.input_task is not None:
            await asyncio.gather(utterance.input_task, return_exceptions=True)
        if not self._stopped:
            await self._remember(utterance)

    async def _remember(self, utterance: _Utterance) -> None:
        if (
            utterance.discarded or utterance.output_ng or utterance.violation_recorded
            or utterance.transcript_incomplete
            or utterance.input_verdict != VERDICT_SAFE or utterance.output_verdict != VERDICT_SAFE
        ):
            # 判定不能・違反・割り込み・文字起こしが欠けた発言は、記憶・親密度の更新に使わない。
            return
        user_id = utterance.user_id
        if user_id not in self.consent.voice or self._service.is_blocked(user_id):
            return
        key = str(user_id)
        if user_id in self.consent.memory:
            try:
                await self._service.memory.append_pending_log(key, utterance.input_text, utterance.output_text)
            except Exception as error:
                logger.error("音声会話: 記憶への追記に失敗しました。", exc_info=error)
        if user_id in self.consent.affinity and utterance.emotion is not None:
            try:
                self._service.affinity.update_emotion(key, *utterance.emotion)
                spawn(self._service.affinity.save_background(key), label="音声会話の親密度の保存")
            except Exception as error:
                logger.error("音声会話: 親密度の更新に失敗しました。", exc_info=error)

    # ==================================================================
    # 通知・同意パネル
    # ==================================================================

    async def _notify(self, text: str, member: Any = None) -> None:
        channel = self.notice_channel
        if channel is None:
            return
        # 通知してよいのは対象の本人だけ（AllowedMentions の既定は @everyone・ロールも許可するため明示する）。
        mentions = (
            discord.AllowedMentions(everyone=False, roles=False, users=[member], replied_user=False)
            if member is not None else _NO_MENTIONS
        )
        content = f"{member.mention} {text}" if member is not None else text
        try:
            await channel.send(content, allowed_mentions=mentions)
        except discord.DiscordException as error:
            logger.error("音声会話: 案内を送信できませんでした。", exc_info=error)

    def _schedule_panel_update(self) -> None:
        if self.panel_message is None or self._stopped:
            return
        task = self._panel_update_task
        if task is not None and not task.done():
            return
        self._panel_update_task = spawn(self._update_panel_later(), label="音声会話の同意パネルの更新")

    async def _update_panel_later(self) -> None:
        delay = self._panel_updated_at + LIVE_CONSENT_PANEL_UPDATE_SECONDS - self._now()
        if delay > 0:
            await asyncio.sleep(delay)
        if self._stopped:
            return
        self._panel_updated_at = self._now()
        await self._edit_panel()

    async def _edit_panel(self, *, view: Any = ...) -> None:
        message = self.panel_message
        if message is None:
            return
        from persona_bot.views.voice_consent_view import build_consent_panel_text

        try:
            if view is ...:
                await message.edit(content=build_consent_panel_text(self), allowed_mentions=_NO_MENTIONS)
            else:
                await message.edit(content=build_consent_panel_text(self), view=view, allowed_mentions=_NO_MENTIONS)
        except discord.DiscordException as error:
            logger.warning("音声会話: 同意パネルを更新できませんでした。", exc_info=error)

    async def _finish_panel(self) -> None:
        if self.panel_view is not None:
            self.panel_view.stop()
        await wait_for_cleanup(self._edit_panel(view=None), label="音声会話の同意パネルの終了")


class VoiceConversationService:
    """サーバーごとの音声会話を管理する（同時実行の上限・開始・終了・参加者の出入り）。"""

    def __init__(
        self,
        client: discord.Client,
        genai_client: Any,
        *,
        moderator: Any,
        ng_filter: Any,
        blacklist: Any,
        memory: Any,
        affinity: Any,
        usage: Any,
        voice_guard: Any = None,
        live_connect: Callable[..., Any] | None = None,
        receiver_factory: Callable[[LiveVoiceClient, Callable[[int, bytes], None]], Any] | None = None,
    ) -> None:
        self._client = client
        self.genai_client = genai_client
        self.moderator = moderator
        self.ng_filter = ng_filter
        self.blacklist = blacklist
        self.memory = memory
        self.affinity = affinity
        self.usage = usage
        self.voice_guard = voice_guard
        self.live_connect = live_connect
        self.receiver_factory = receiver_factory or (lambda voice_client, sink: VoiceReceiver(voice_client, sink))
        self._sessions: dict[int, VoiceConversation] = {}
        self._reserved: set[int] = set()

    def is_blocked(self, user_id: int) -> bool:
        return self.blacklist.is_blocked(user_id)

    def get(self, guild_id: int) -> VoiceConversation | None:
        return self._sessions.get(guild_id)

    def reserve(self, guild_id: int) -> bool:
        """開始の枠を確保する。上限なら False（判定と確保の間に await を挟まない）。"""
        if guild_id in self._sessions or guild_id in self._reserved:
            return True
        if len(self._sessions) + len(self._reserved) >= LIVE_MAX_CONCURRENT_SESSIONS:
            return False
        self._reserved.add(guild_id)
        return True

    def release_reservation(self, guild_id: int) -> None:
        self._reserved.discard(guild_id)

    async def start(
        self,
        guild: discord.Guild,
        voice_client: LiveVoiceClient,
        voice_channel: Any,
        response_settings: Any,
        *,
        fallback_channel: Any = None,
    ) -> VoiceConversation:
        """音声会話を始める。先に reserve() で枠を確保しておくこと。

        Raises:
            LiveUnavailableError: Live へ接続できなかった。
            ConsentPanelError: 同意パネルを出せなかった（同意を集められないので始めない）。
        """
        try:
            existing = self._sessions.get(guild.id)
            if existing is not None:
                await existing.stop()
                self._sessions.pop(guild.id, None)
            persona_prompt = await load_persona_template(response_settings.prompt_file)
            voice_name = str(current_persona_catalog().voice_profile(response_settings.prompt_file).get("live_voice") or "")
            options = LiveOptions(
                system_instruction=build_live_system_instruction(
                    persona_prompt, grounding=response_settings.grounding_enabled,
                ),
                voice_name=voice_name,
                grounding=response_settings.grounding_enabled,
            )
            notice_channel = (
                voice_channel if can_bot_send_messages(voice_channel, guild.me) else fallback_channel
            )
            conversation = VoiceConversation(
                self, guild, voice_client, voice_channel.id, notice_channel, options,
            )
            try:
                await conversation.start()
                # 同意パネルを出せなければ誰も同意できず、Live の接続と同時実行の枠だけが残る。
                # パネルの投稿までを開始の成功の条件にする（登録はその後）。
                await self._post_panel(conversation)
            except BaseException:
                await wait_for_cleanup(conversation.stop(), label="音声会話の開始失敗の後片付け")
                raise
            self._sessions[guild.id] = conversation
        finally:
            self._reserved.discard(guild.id)
        logger.info("音声会話: 開始しました。")
        return conversation

    async def _post_panel(self, conversation: VoiceConversation) -> None:
        from persona_bot.views.voice_consent_view import VoiceConsentView, build_consent_panel_text

        channel = conversation.notice_channel
        if channel is None:
            logger.warning("音声会話: 同意パネルを出せるチャンネルがありません。")
            raise ConsentPanelError
        view = VoiceConsentView(conversation, is_blocked=self.is_blocked)
        try:
            conversation.panel_message = await channel.send(
                build_consent_panel_text(conversation), view=view, allowed_mentions=_NO_MENTIONS,
            )
        except discord.DiscordException as error:
            view.stop()
            logger.error("音声会話: 同意パネルを送信できませんでした。", exc_info=error)
            raise ConsentPanelError from error
        conversation.panel_view = view

    async def stop(self, guild_id: int, *, reason: str) -> bool:
        """そのサーバーの音声会話を止める（切断は呼び出し側）。止めたら True。"""
        conversation = self._sessions.pop(guild_id, None)
        self._reserved.discard(guild_id)
        if conversation is None:
            return False
        logger.info("音声会話: 終了します（%s）。", reason)
        await wait_for_cleanup(conversation.stop(), label="音声会話の終了")
        return True

    async def end(
        self, guild_id: int, *, reason: str, notice: str | None = None, disconnect: bool,
        conversation: VoiceConversation | None = None,
    ) -> None:
        """会話側の事情（Live の終了・接続の喪失）で止める。必要なら案内して退出する。"""
        current = self._sessions.get(guild_id)
        if conversation is not None and current is not conversation:
            # 既に /leave・/join で入れ替わった会話には触らない。
            return
        if current is None:
            return
        voice_client = current.voice_client
        await self.stop(guild_id, reason=reason)
        if guild_id in self._sessions or guild_id in self._reserved:
            # 終了を待つ間に /join が同じ接続で新しい会話を始めた（始めようとしている）。
            # 新しい会話の接続には触らず、退出の案内も出さない。
            return
        if notice:
            await current._notify(notice)
        guild = current.guild
        if disconnect and guild.voice_client is voice_client and voice_client.is_connected():
            if self.voice_guard is not None:
                self.voice_guard.release(guild_id)
            try:
                await wait_for_cleanup(voice_client.disconnect(force=True), label="音声会話の終了時の切断")
            except Exception:
                logger.warning("音声会話: 終了時の切断に失敗しました。", exc_info=True)

    async def stop_all(self) -> None:
        for guild_id in list(self._sessions):
            await self.stop(guild_id, reason="ボットの終了")

    def handle_voice_state_update(self, member: Any, before: Any, after: Any) -> None:
        """参加者の出入りを、そのサーバーの音声会話へ伝える（同期関数。届いた順に処理する）。"""
        guild = getattr(member, "guild", None)
        conversation = self._sessions.get(getattr(guild, "id", None))
        if conversation is None:
            return
        bot_user = self._client.user
        if bot_user is not None and member.id == bot_user.id:
            # ボット自身の移動・切断は見回り（tick）と voice_guard が扱う。
            return
        conversation.handle_member_update(member, before, after)
