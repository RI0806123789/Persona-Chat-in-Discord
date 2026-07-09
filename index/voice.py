import asyncio
from pathlib import Path

import discord
from gtts import gTTS


import tempfile

async def play_tts_response(message: discord.Message, response_text: str | None, ffmpeg_path: Path) -> None:
    if not response_text or response_text.isspace():
        return

    voice_client = message.guild.voice_client if message.guild else None
    if not isinstance(voice_client, discord.VoiceClient):
        return
    if not voice_client.is_connected() or voice_client.is_playing():
        return

    tts_path = Path(tempfile.gettempdir()) / f"temp_speech_{message.id}.mp3"
    try:
        tts = gTTS(text=response_text, lang="ja")
        await asyncio.to_thread(tts.save, str(tts_path))

        def after_playing(error: Exception | None) -> None:
            if error:
                print(f"音声再生エラー: {error}")
            try:
                if tts_path.exists():
                    tts_path.unlink()
            except Exception as delete_error:
                print(f"TTS一時ファイル削除エラー: {delete_error}")

        source = discord.FFmpegPCMAudio(str(tts_path), executable=str(ffmpeg_path))
        voice_client.play(source, after=after_playing)
    except Exception as error:
        print(f"gTTS APIエラー: {error}")
        if tts_path.exists():
            try:
                tts_path.unlink()
            except Exception:
                pass

