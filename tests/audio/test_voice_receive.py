"""音声受信（#268 の VoiceReceiveAdapter）を、discord.py と同じ暗号化で作ったパケットで確かめる。

★ discord.py を更新したら必ず回すこと。受信は discord.py の内部属性（`_connection`・
`secret_key`・`dave_session`）と音声 WebSocket のフックに依存している。
"""

import asyncio
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import nacl.secret
import nacl.utils
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord
from discord.voice_state import VoiceConnectionState
from persona_bot.audio import voice_receive
from persona_bot.audio.voice_receive import (
    OPUS_SILENCE_FRAME, LiveVoiceClient, VoiceReceiver, decrypt_transport, is_newer_sequence,
    parse_rtp_packet,
)

KEY = nacl.utils.random(nacl.secret.Aead.KEY_SIZE)
SSRC = 4321
USER_ID = 777


def encrypt_packet(payload: bytes, *, sequence: int = 1, extension: bytes | None = None,
                   csrcs: int = 0, nonce: int = 5) -> bytes:
    """discord.py の _encrypt_aead_xchacha20_poly1305_rtpsize と同じ形でパケットを作る。

    受信では拡張ヘッダー（4バイト）が追加認証データ、拡張の本体が暗号文の先頭に入る。
    """
    first = 0x80 | csrcs | (0x10 if extension is not None else 0)
    header = bytearray(12)
    header[0] = first
    header[1] = 0x78
    struct.pack_into(">HII", header, 2, sequence, 960 * sequence, SSRC)
    header += bytes(4 * csrcs)
    body = payload
    if extension is not None:
        header += struct.pack(">HH", 0xBEDE, len(extension) // 4)
        body = extension + payload
    nonce_bytes = bytearray(24)
    nonce_bytes[:4] = struct.pack(">I", nonce)
    box = nacl.secret.Aead(KEY)
    return bytes(header) + box.encrypt(body, bytes(header), bytes(nonce_bytes)).ciphertext + bytes(nonce_bytes[:4])


class FakeDecoder:
    def __init__(self) -> None:
        self.decoded: list[bytes] = []

    def decode(self, data: bytes, *, fec: bool) -> bytes:
        self.decoded.append(data)
        # 20ms・48kHz・ステレオの一定値（実際の Opus のデコードの代わり）
        return np.full(960 * 2, 1200, dtype=np.int16).tobytes()


class RtpTests(unittest.TestCase):
    def test_packet_without_extension_round_trips(self):
        packet = parse_rtp_packet(encrypt_packet(b"opus-frame"))
        self.assertEqual((packet.ssrc, packet.sequence), (SSRC, 1))
        self.assertEqual(decrypt_transport(packet, nacl.secret.Aead(KEY)), b"opus-frame")

    def test_extension_body_and_csrcs_are_removed(self):
        raw = encrypt_packet(b"opus-frame", extension=b"\x10\xff\x00\x00" * 2, csrcs=2)
        packet = parse_rtp_packet(raw)
        self.assertTrue(packet.has_extension)
        self.assertEqual(decrypt_transport(packet, nacl.secret.Aead(KEY)), b"opus-frame")

    def test_tampered_header_fails_authentication(self):
        raw = bytearray(encrypt_packet(b"opus-frame"))
        raw[8] ^= 0x01  # SSRC を書き換える
        packet = parse_rtp_packet(bytes(raw))
        with self.assertRaises(Exception):
            decrypt_transport(packet, nacl.secret.Aead(KEY))

    def test_rtcp_and_short_packets_are_ignored(self):
        rtcp = bytes([0x80, 200]) + bytes(30)
        self.assertIsNone(parse_rtp_packet(rtcp))
        self.assertIsNone(parse_rtp_packet(b"\x80\x78\x00"))
        self.assertIsNone(parse_rtp_packet(bytes([0x40, 0x78]) + bytes(30)))

    def test_sequence_comparison_handles_wraparound(self):
        self.assertTrue(is_newer_sequence(5, None))
        self.assertTrue(is_newer_sequence(6, 5))
        self.assertFalse(is_newer_sequence(5, 5))
        self.assertFalse(is_newer_sequence(4, 5))
        self.assertTrue(is_newer_sequence(1, 0xFFFF))
        self.assertFalse(is_newer_sequence(0xFFFF, 1))


class LiveVoiceClientTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self) -> LiveVoiceClient:
        loop = asyncio.get_running_loop()
        client = SimpleNamespace(_connection=SimpleNamespace(loop=loop))
        voice_client = LiveVoiceClient(client, SimpleNamespace(id=1, guild=SimpleNamespace(id=2)))
        self.addCleanup(voice_client._connection._socket_reader.stop)
        return voice_client

    async def test_connection_state_uses_the_speaking_hook(self):
        voice_client = self.make_client()
        self.assertIsInstance(voice_client._connection, VoiceConnectionState)
        self.assertEqual(voice_client._connection.hook, voice_client._handle_voice_ws_message)

    async def test_speaking_maps_ssrc_and_disconnect_forgets_it(self):
        voice_client = self.make_client()
        await voice_client._handle_voice_ws_message(None, {"op": 5, "d": {"user_id": "77", "ssrc": 9, "speaking": 1}})
        self.assertEqual(voice_client.user_id_for_ssrc(9), 77)
        await voice_client._handle_voice_ws_message(None, {"op": 13, "d": {"user_id": "77"}})
        self.assertIsNone(voice_client.user_id_for_ssrc(9))

    async def test_malformed_messages_are_ignored(self):
        voice_client = self.make_client()
        for message in ({"op": 5, "d": {"user_id": "名前", "ssrc": 1}}, {"op": 5, "d": None}, {"op": 2}):
            await voice_client._handle_voice_ws_message(None, message)
        self.assertIsNone(voice_client.user_id_for_ssrc(1))


class ReceiverTests(unittest.IsolatedAsyncioTestCase):
    def make_receiver(self, *, dave_session=None, dave_version=0):
        self.connection = SimpleNamespace(
            mode="aead_xchacha20_poly1305_rtpsize", secret_key=list(KEY),
            dave_session=dave_session, dave_protocol_version=dave_version,
            add_socket_listener=Mock(), remove_socket_listener=Mock(),
        )
        voice_client = SimpleNamespace(_connection=self.connection, user_id_for_ssrc={SSRC: USER_ID}.get)
        self.decoders: list[FakeDecoder] = []

        def factory():
            decoder = FakeDecoder()
            self.decoders.append(decoder)
            return decoder

        self.received: list[tuple[int, bytes]] = []
        receiver = VoiceReceiver(
            voice_client, lambda user_id, pcm: self.received.append((user_id, pcm)), decoder_factory=factory,
        )
        return receiver

    async def test_decrypted_frame_is_delivered_as_16khz_mono(self):
        receiver = self.make_receiver()
        receiver.start()
        self.connection.add_socket_listener.assert_called_once_with(receiver._on_packet)
        receiver._on_packet(encrypt_packet(b"opus"))
        await asyncio.sleep(0)
        self.assertEqual(len(self.received), 1)
        user_id, pcm = self.received[0]
        self.assertEqual(user_id, USER_ID)
        self.assertEqual(len(pcm), 320 * 2)  # 20ms・16kHz・モノラル
        self.assertEqual(self.decoders[0].decoded, [b"opus"])
        receiver.stop()
        self.connection.remove_socket_listener.assert_called_once_with(receiver._on_packet)

    async def test_unknown_speaker_old_sequence_and_silence_are_dropped(self):
        receiver = self.make_receiver()
        receiver.start()
        unknown = bytearray(encrypt_packet(b"opus"))
        receiver._voice_client = SimpleNamespace(_connection=self.connection, user_id_for_ssrc={}.get)
        receiver._on_packet(bytes(unknown))
        receiver._voice_client = SimpleNamespace(_connection=self.connection, user_id_for_ssrc={SSRC: USER_ID}.get)
        receiver._on_packet(encrypt_packet(b"opus", sequence=10))
        receiver._on_packet(encrypt_packet(b"old", sequence=9))
        receiver._on_packet(encrypt_packet(OPUS_SILENCE_FRAME, sequence=11))
        await asyncio.sleep(0)
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.decoders[0].decoded, [b"opus"])

    async def test_broken_packet_is_counted_without_raising(self):
        receiver = self.make_receiver()
        receiver.start()
        raw = bytearray(encrypt_packet(b"opus"))
        raw[-6] ^= 0xFF  # 暗号文を壊す
        receiver._on_packet(bytes(raw))
        await asyncio.sleep(0)
        self.assertEqual(self.received, [])
        self.assertEqual(receiver.dropped_packets, 1)

    async def test_unsupported_mode_is_dropped(self):
        receiver = self.make_receiver()
        self.connection.mode = "xsalsa20_poly1305"
        receiver.start()
        with self.assertLogs("persona_bot.audio.voice_receive", level="WARNING"):
            receiver._on_packet(encrypt_packet(b"opus"))
        await asyncio.sleep(0)
        self.assertEqual(self.received, [])

    async def test_dave_layer_is_decrypted_with_the_speaker_id(self):
        session = SimpleNamespace(
            ready=True, decrypt=Mock(return_value=b"plain-opus"), can_passthrough=Mock(return_value=False),
        )
        receiver = self.make_receiver(dave_session=session, dave_version=1)
        receiver.start()
        receiver._on_packet(encrypt_packet(b"dave-cipher"))
        await asyncio.sleep(0)
        session.decrypt.assert_called_once_with(USER_ID, voice_receive.davey.MediaType.audio, b"dave-cipher")
        self.assertEqual(self.decoders[0].decoded, [b"plain-opus"])

    async def test_undecryptable_dave_frame_is_dropped_unless_passthrough(self):
        for passthrough in (False, True):
            with self.subTest(passthrough=passthrough):
                session = SimpleNamespace(
                    ready=True, decrypt=Mock(side_effect=RuntimeError("架空の復号失敗")),
                    can_passthrough=Mock(return_value=passthrough),
                )
                receiver = self.make_receiver(dave_session=session, dave_version=1)
                receiver.start()
                receiver._on_packet(encrypt_packet(b"frame"))
                await asyncio.sleep(0)
                self.assertEqual(len(self.received), 1 if passthrough else 0)

    async def test_packets_after_stop_are_not_delivered(self):
        receiver = self.make_receiver()
        receiver.start()
        receiver._on_packet(encrypt_packet(b"opus"))
        receiver.stop()
        await asyncio.sleep(0)
        self.assertEqual(self.received, [])


class DiscordCompatibilityTests(unittest.TestCase):
    def test_discord_py_still_exposes_the_receive_entry_points(self):
        """受信が依存する discord.py の内部の入口が残っているか（版の更新で消えたら検出する）。"""
        self.assertTrue(hasattr(VoiceConnectionState, "add_socket_listener"))
        self.assertTrue(hasattr(VoiceConnectionState, "remove_socket_listener"))
        self.assertIn("aead_xchacha20_poly1305_rtpsize", discord.VoiceClient.supported_modes)
        self.assertTrue(hasattr(discord.VoiceClient, "create_connection_state"))
        self.assertTrue(hasattr(discord.opus, "Decoder"))


if __name__ == "__main__":
    unittest.main()
