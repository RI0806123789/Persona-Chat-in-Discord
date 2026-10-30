"""ボイスチャンネルの音声受信（#268 の VoiceReceiveAdapter）。

discord.py 本体は音声の受信に対応していないため、Gemini Live の音声会話で人の声を
聞き取る部分をここで組む。**依存は増やさない**（どれも discord.py[voice] が既に入れている）:

1. UDP の受信 …………… `VoiceConnectionState.add_socket_listener`（discord.py が IP の検出に
   使っている購読口）。コールバックは discord.py のソケット読み取りスレッドで呼ばれる。
2. 転送層の復号 ………… PyNaCl の `Aead`（`aead_xchacha20_poly1305_rtpsize`。discord.py が
   送信に使うのと同じ方式で、Discord は 2024-11 から他の方式を受け付けない）。
3. DAVE（E2E暗号化）の復号 … discord.py が保持する `dave_session.decrypt`（davey）。
   Discord は 2026-03-02 から全ボイスチャンネルで DAVE を必須化している。
4. Opus のデコード ……… discord.py 同梱の `discord.opus.Decoder`（48kHz・ステレオ）。
5. 話者の識別 …………… 音声 WebSocket の SPEAKING（op 5）が知らせる「SSRC → user_id」の対応。

★ discord.py の内部属性（`_connection`・`secret_key`・`dave_session`）と音声 WebSocket の
フックに依存する。discord.py の版を上げるときは `tests/audio/test_voice_receive.py` を
必ず回すこと（requirements.txt で版を固定しているのはこのため）。
受信の依存はこのモジュールに閉じ込めてあり、別の受信基盤へ置き換えるときもここだけを差し替える。

受信は再生（`VoiceClient.play` / `stop`）と独立している。`voice_guard` が再生を止めても
受信は止まらず、受信を止めても再生は止まらない。
"""

from __future__ import annotations

import logging
import asyncio
from dataclasses import dataclass
import struct
from typing import Any, Callable

import discord
from discord.voice_state import VoiceConnectionState

from persona_bot.audio.live_audio import Downsampler

try:
    import davey
except ImportError:  # pragma: no cover - discord.py[voice] が入れるため通常は通らない
    davey = None

try:
    import nacl.secret
except ImportError:  # pragma: no cover - 同上
    nacl = None


logger = logging.getLogger(__name__)

_RTP_HEADER_SIZE = 12
_RTP_VERSION = 2
_OPUS_PAYLOAD_TYPE = 0x78
_RTPSIZE_NONCE_SIZE = 4
_SUPPORTED_MODE = "aead_xchacha20_poly1305_rtpsize"
# Discord のクライアントが話し終わりに送る無音のフレーム。
OPUS_SILENCE_FRAME = b"\xf8\xff\xfe"
# 音声 WebSocket の op コード（discord.py の DiscordVoiceWebSocket と同じ値）。
_OP_SPEAKING = 5
_OP_CLIENT_DISCONNECT = 13
# 16bit の通番の比較で「古い」とみなす幅（ラップアラウンドを考慮する）。
_SEQUENCE_HALF_RANGE = 0x8000

# 1人分の音声を受け取るコールバック（user_id, 16kHz モノラル PCM）。イベントループ上で呼ばれる。
AudioSink = Callable[[int, bytes], None]


@dataclass(frozen=True)
class RtpPacket:
    """復号前の RTP パケットの、受信に要る部分。"""

    sequence: int
    timestamp: int
    ssrc: int
    has_extension: bool
    has_padding: bool
    # 転送層の暗号の追加認証データ（rtpsize では RTP ヘッダーと拡張ヘッダーの4バイト）。
    authenticated: bytes
    ciphertext: bytes
    nonce: bytes


def parse_rtp_packet(data: bytes) -> RtpPacket | None:
    """Opus の RTP パケットなら分解して返す。RTCP・その他のパケットは None。"""
    if len(data) < _RTP_HEADER_SIZE + _RTPSIZE_NONCE_SIZE:
        return None
    first, second = data[0], data[1]
    if first >> 6 != _RTP_VERSION or second & 0x7F != _OPUS_PAYLOAD_TYPE:
        # RTCP（送信者レポートなど）は 2 バイト目が 200〜204 で、ここで除かれる。
        return None
    csrc_count = first & 0x0F
    has_extension = bool(first & 0x10)
    header_size = _RTP_HEADER_SIZE + 4 * csrc_count
    authenticated_size = header_size + (4 if has_extension else 0)
    if len(data) < authenticated_size + _RTPSIZE_NONCE_SIZE:
        return None
    sequence, timestamp, ssrc = struct.unpack_from(">HII", data, 2)
    return RtpPacket(
        sequence=sequence,
        timestamp=timestamp,
        ssrc=ssrc,
        has_extension=has_extension,
        has_padding=bool(first & 0x20),
        authenticated=bytes(data[:authenticated_size]),
        ciphertext=bytes(data[authenticated_size:-_RTPSIZE_NONCE_SIZE]),
        nonce=bytes(data[-_RTPSIZE_NONCE_SIZE:]),
    )


def decrypt_transport(packet: RtpPacket, box: Any) -> bytes:
    """転送層の暗号を外し、RTP 拡張の本体とパディングを取り除いた Opus（または DAVE 暗号文）を返す。

    rtpsize では、拡張ヘッダー（4バイト）は追加認証データ、拡張の本体は暗号文の先頭に入る。
    """
    nonce = packet.nonce + bytes(nacl.secret.Aead.NONCE_SIZE - _RTPSIZE_NONCE_SIZE)
    plaintext = box.decrypt(packet.ciphertext, packet.authenticated, nonce)
    if packet.has_extension:
        (extension_words,) = struct.unpack_from(">H", packet.authenticated, len(packet.authenticated) - 2)
        plaintext = plaintext[extension_words * 4:]
    if packet.has_padding and plaintext:
        plaintext = plaintext[: len(plaintext) - plaintext[-1]]
    return plaintext


def is_newer_sequence(sequence: int, last: int | None) -> bool:
    """16bit の通番が前回より新しいか（遅れて届いた古いパケットを捨てるため）。"""
    if last is None:
        return True
    difference = (sequence - last) & 0xFFFF
    return 0 < difference < _SEQUENCE_HALF_RANGE


class _UserDecoder:
    """1人分の Opus デコーダと、16kHz モノラルへの変換の状態。"""

    def __init__(self, decoder: Any) -> None:
        self.decoder = decoder
        self.downsampler = Downsampler()
        self.last_sequence: int | None = None


class LiveVoiceClient(discord.VoiceClient):
    """音声 WebSocket の SPEAKING を見て、SSRC と話者の対応を覚える VoiceClient。

    `/join` はこのクラスで接続する（受信に必要な情報は接続の最初から流れてくるため、
    後から差し替えられない）。再生は通常の VoiceClient と同じ。
    """

    def __init__(self, client: discord.Client, channel: discord.abc.Connectable) -> None:
        self._ssrc_users: dict[int, int] = {}
        super().__init__(client, channel)

    def create_connection_state(self) -> VoiceConnectionState:
        return VoiceConnectionState(self, hook=self._handle_voice_ws_message)

    async def _handle_voice_ws_message(self, _ws: Any, message: dict[str, Any]) -> None:
        """音声 WebSocket のフック。discord.py が処理した後に、全メッセージで呼ばれる。"""
        try:
            op = message.get("op")
            data = message.get("d") or {}
            if op == _OP_SPEAKING and "ssrc" in data and "user_id" in data:
                self._ssrc_users[int(data["ssrc"])] = int(data["user_id"])
            elif op == _OP_CLIENT_DISCONNECT and "user_id" in data:
                user_id = int(data["user_id"])
                for ssrc in [key for key, value in self._ssrc_users.items() if value == user_id]:
                    self._ssrc_users.pop(ssrc, None)
        except (TypeError, ValueError):
            # 形式の違うメッセージで接続処理を落とさない（話者が分からない音声は捨てるだけ）。
            logger.debug("音声受信: 話者の対応を読めないメッセージを無視しました。", exc_info=True)

    def user_id_for_ssrc(self, ssrc: int) -> int | None:
        return self._ssrc_users.get(ssrc)


class VoiceReceiver:
    """1つの接続の受信を管理する。`start()` で購読を始め、`stop()` で外す。

    パケットの処理（復号・デコード・16kHz への変換）はソケットの読み取りスレッドで行い、
    結果だけを `loop.call_soon_threadsafe` でイベントループへ渡す（イベントループを
    1パケットごとの CPU 処理で塞がないため）。1パケットの失敗は捨てて次へ進む。
    """

    def __init__(
        self,
        voice_client: LiveVoiceClient,
        sink: AudioSink,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
        decoder_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._voice_client = voice_client
        self._sink = sink
        self._loop = loop or asyncio.get_running_loop()
        self._decoder_factory = decoder_factory or discord.opus.Decoder
        self._decoders: dict[int, _UserDecoder] = {}
        self._box: Any = None
        self._box_key: bytes | None = None
        self._running = False
        self._warned_mode = False
        self._dropped = 0

    @property
    def dropped_packets(self) -> int:
        """復号・デコードできずに捨てたパケット数（診断用）。"""
        return self._dropped

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._voice_client._connection.add_socket_listener(self._on_packet)

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        try:
            self._voice_client._connection.remove_socket_listener(self._on_packet)
        finally:
            self._decoders.clear()

    # --- ソケットの読み取りスレッド ------------------------------------------------

    def _on_packet(self, data: bytes) -> None:
        if not self._running:
            return
        try:
            result = self._decode_packet(data)
        except Exception:
            # 1パケットの失敗で読み取りスレッドのログを埋めない（件数だけ数える）。
            self._dropped += 1
            logger.debug("音声受信: パケットを処理できなかったため捨てました。", exc_info=True)
            return
        if result is None:
            return
        user_id, pcm = result
        try:
            self._loop.call_soon_threadsafe(self._deliver, user_id, pcm)
        except RuntimeError:
            # イベントループが閉じた後（終了処理中）に届いたパケット。
            return

    def _deliver(self, user_id: int, pcm: bytes) -> None:
        if self._running:
            self._sink(user_id, pcm)

    def _decode_packet(self, data: bytes) -> tuple[int, bytes] | None:
        packet = parse_rtp_packet(data)
        if packet is None:
            return None
        connection = self._voice_client._connection
        if connection.mode != _SUPPORTED_MODE:
            if not self._warned_mode:
                self._warned_mode = True
                logger.warning("音声受信: 未対応の暗号方式で接続されたため、音声を受信できません。")
            return None
        user_id = self._voice_client.user_id_for_ssrc(packet.ssrc)
        if user_id is None:
            # SPEAKING の通知より先に届いた音声。話者が分からないので捨てる。
            return None

        state = self._decoders.get(packet.ssrc)
        if state is not None and not is_newer_sequence(packet.sequence, state.last_sequence):
            return None

        opus = decrypt_transport(packet, self._transport_box(connection))
        opus = self._decrypt_dave(connection, user_id, opus)
        if opus is None:
            self._dropped += 1
            return None
        if state is None:
            state = self._decoders[packet.ssrc] = _UserDecoder(self._decoder_factory())
        state.last_sequence = packet.sequence
        if opus == OPUS_SILENCE_FRAME:
            return None
        pcm = state.decoder.decode(opus, fec=False)
        converted = state.downsampler.process(pcm)
        return (user_id, converted) if converted else None

    def _transport_box(self, connection: Any) -> Any:
        key = bytes(connection.secret_key)
        if self._box is None or self._box_key != key:
            # 再接続で鍵が変わったら作り直す。
            self._box = nacl.secret.Aead(key)
            self._box_key = key
        return self._box

    @staticmethod
    def _decrypt_dave(connection: Any, user_id: int, payload: bytes) -> bytes | None:
        """DAVE の暗号を外す。DAVE を使っていない接続ならそのまま返す。

        MLS のエポックの切り替わりの前後では一時的に復号できないパケットが届く。
        そのときは捨てる（パススルー中の相手なら暗号化されていないのでそのまま使う）。
        """
        session = getattr(connection, "dave_session", None)
        if session is None or getattr(connection, "dave_protocol_version", 0) == 0 or davey is None:
            return payload
        try:
            if session.ready:
                decrypted = session.decrypt(user_id, davey.MediaType.audio, payload)
                if decrypted:
                    return bytes(decrypted)
        except Exception:
            logger.debug("音声受信: DAVE の復号に失敗しました。", exc_info=True)
        try:
            if session.can_passthrough(user_id):
                return payload
        except Exception:
            logger.debug("音声受信: DAVE のパススルー状態を確認できませんでした。", exc_info=True)
        return None
