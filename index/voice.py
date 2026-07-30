"""ボイスチャンネルでの読み上げ（TTS）。

`/join` でボットがボイスチャンネルに接続している間だけ、テキストチャンネルへの
AI の返信を音声で読み上げる。接続していなければ黙って何もしない（no-op）。

**FFmpeg には依存しない。** gTTS が返すのは mp3 だが、`ffmpeg.exe` のような外部プロセスを
毎回起動する代わりに、miniaudio（C ライブラリを同梱した pip パッケージ。exe 不要）で
メモリ上のまま「48kHz / ステレオ / 16bit」の生 PCM へデコードし、`discord.PCMAudio` に
渡す。Discord のボイス送信はこの形式の PCM をそのまま受け取れるため、変換用の
外部バイナリも一時ファイルも要らない。CPU 1コア / メモリ 1GB 級の小さなサーバーでも
動かせるようにするための構成。
"""

import asyncio
import io
import re

import discord
import miniaudio
from gtts import gTTS

from background import spawn
from constants import (
    TTS_CHANNELS,
    TTS_LANG,
    TTS_MAX_CHARS,
    TTS_QUEUE_MAX,
    TTS_SAMPLE_RATE,
    TTS_TIMEOUT_SECONDS,
    TTS_TRUNCATION_NOTICE,
)

# 読み上げに不向きな要素を落とすためのパターン群。
# 音声で聞いて意味をなさないもの（URL・記号・コード）は読み上げる価値がないうえ、
# gTTS は URL を1文字ずつ読み上げてしまい非常に長くなるため必ず除去する。
_CODE_BLOCK_PATTERN = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_PATTERN = re.compile(r"`[^`]*`")
_URL_PATTERN = re.compile(r"https?://\S+")
# Discord のカスタム絵文字 <:name:123> / アニメ絵文字 <a:name:123>
_CUSTOM_EMOJI_PATTERN = re.compile(r"<a?:\w+:\d+>")
# メンション <@123> / <@!123> / <@&123>（ロール） / <#123>（チャンネル）
_MENTION_PATTERN = re.compile(r"<[@#][!&]?\d+>")
# Markdown の装飾記号と引用・見出し記号（読み上げでは雑音にしかならない）
_MARKDOWN_SYMBOL_PATTERN = re.compile(r"[*_~>#|`]")
_WHITESPACE_PATTERN = re.compile(r"\s+")


def build_speech_text(response_text: str) -> str:
    """返信テキストを読み上げ向けに整形する。

    テキストチャンネルには元の全文が既に投稿されているため、ここでは
    「声で聞いて分かる部分」だけを残す。長すぎる返信は途中で切り、
    ボイスチャンネルを何分も占有しないようにする。
    """
    text = _CODE_BLOCK_PATTERN.sub(" ", response_text)
    text = _INLINE_CODE_PATTERN.sub(" ", text)
    text = _URL_PATTERN.sub(" ", text)
    text = _CUSTOM_EMOJI_PATTERN.sub(" ", text)
    text = _MENTION_PATTERN.sub(" ", text)
    text = _MARKDOWN_SYMBOL_PATTERN.sub(" ", text)
    text = _WHITESPACE_PATTERN.sub(" ", text).strip()

    if len(text) > TTS_MAX_CHARS:
        text = text[:TTS_MAX_CHARS].rstrip() + TTS_TRUNCATION_NOTICE
    return text


def _synthesize_pcm(text: str) -> bytes:
    """テキストを Discord へそのまま送出できる生 PCM に変換する。

    gTTS のネットワーク呼び出しと miniaudio のデコードはどちらも同期処理のため、
    呼び出し側から `asyncio.to_thread` で別スレッドに逃がして使う
    （イベントループを止めるとボット全体の応答が固まるため）。
    """
    mp3_buffer = io.BytesIO()
    gTTS(text=text, lang=TTS_LANG).write_to_fp(mp3_buffer)

    # miniaudio 側で 48kHz / ステレオ / 16bit への変換まで済ませる。
    # gTTS の mp3 は 24kHz モノラルなので、この指定が実質リサンプリングを兼ねる。
    decoded = miniaudio.decode(
        mp3_buffer.getvalue(),
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=TTS_CHANNELS,
        sample_rate=TTS_SAMPLE_RATE,
    )
    return decoded.samples.tobytes()


async def _play_pcm(voice_client: discord.VoiceClient, pcm: bytes) -> None:
    """PCM を再生し、再生が終わる（または失敗する）まで待つ。"""
    loop = asyncio.get_running_loop()
    finished = asyncio.Event()

    def after_playing(error: Exception | None) -> None:
        # このコールバックは discord.py の再生スレッドから呼ばれるため、
        # イベントループ側のオブジェクトへは call_soon_threadsafe 経由で触る。
        if error:
            print(f"読み上げの再生エラー: {error}")
        loop.call_soon_threadsafe(finished.set)

    voice_client.play(discord.PCMAudio(io.BytesIO(pcm)), after=after_playing)
    await finished.wait()


# ギルドIDごとの読み上げ待ちキュー。再生中に届いた返信を捨てずに順番に読むために持つ。
# ワーカーは最初の依頼で起動し、キューが空になったら自分を片付けて終了するので、
# 参加していないサーバーの分までタスクが常駐することはない。
_speech_queues: dict[int, asyncio.Queue[str]] = {}


async def _run_speech_worker(guild: discord.Guild) -> None:
    """1サーバー分の読み上げキューを順に処理する。"""
    queue = _speech_queues[guild.id]
    try:
        while True:
            try:
                text = queue.get_nowait()
            except asyncio.QueueEmpty:
                # ここから return までに await を挟まないこと。挟むと、キューが空だと
                # 判断した後・登録を消す前に新しい依頼が積まれ、その分が
                # 誰にも処理されないまま取り残される。
                _speech_queues.pop(guild.id, None)
                return

            # 読み上げている間に /leave された場合に備え、毎回その時点の接続を見る。
            voice_client = guild.voice_client
            if not isinstance(voice_client, discord.VoiceClient) or not voice_client.is_connected():
                _speech_queues.pop(guild.id, None)
                return

            try:
                pcm = await asyncio.wait_for(
                    asyncio.to_thread(_synthesize_pcm, text),
                    timeout=TTS_TIMEOUT_SECONDS,
                )
                await _play_pcm(voice_client, pcm)
            except asyncio.TimeoutError:
                print(f"TTS: 音声合成が {TTS_TIMEOUT_SECONDS} 秒で完了しなかったため、この返信の読み上げを見送ります。")
            except Exception as error:
                # 読み上げはチャット本体の付随機能なので、失敗しても次の返信へ進む。
                print(f"TTS エラー ({type(error).__name__}): {error}")
    finally:
        # 予期しない例外で抜けた場合も登録を残さない（次の返信で再起動できるように）。
        _speech_queues.pop(guild.id, None)


async def play_tts_response(message: discord.Message, response_text: str | None) -> None:
    """AI の返信をボイスチャンネルで読み上げる。

    ボットが `/join` でボイスチャンネルに接続しているときだけ動作し、
    それ以外は何もせずに戻る（テキストチャットは読み上げの成否に影響されない）。
    """
    if not response_text or response_text.isspace():
        return

    # DM にはギルドが無く、ボットが参加できるボイスチャンネルも存在しない。
    guild = message.guild
    if guild is None:
        return

    voice_client = guild.voice_client
    if not isinstance(voice_client, discord.VoiceClient) or not voice_client.is_connected():
        return

    speech_text = build_speech_text(response_text)
    if not speech_text:
        # 装飾やURLだけの返信を除去した結果、読み上げる中身が残らなかった場合。
        return

    queue = _speech_queues.get(guild.id)
    if queue is None:
        queue = asyncio.Queue(maxsize=TTS_QUEUE_MAX)
        _speech_queues[guild.id] = queue
        queue.put_nowait(speech_text)
        spawn(_run_speech_worker(guild), label="ボイス読み上げ")
        return

    try:
        queue.put_nowait(speech_text)
    except asyncio.QueueFull:
        # 読み上げが追いつかないほど返信が続いた場合。古い返信を延々と喋り続けるより、
        # 遅れた分は捨てて会話の現在地に追従する方が自然なため、この返信は見送る。
        print("TTS: 読み上げの待ち行列が上限に達したため、この返信の読み上げを見送ります。")
