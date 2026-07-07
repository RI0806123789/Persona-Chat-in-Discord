import asyncio
from pathlib import Path

import discord
from gtts import gTTS


async def play_tts_response(message: discord.Message, response_text: str | None, ffmpeg_path: Path) -> None:
    if not response_text or response_text.isspace():
        return

    voice_client = message.guild.voice_client if message.guild else None
    if not isinstance(voice_client, discord.VoiceClient):
        return
    if not voice_client.is_connected() or voice_client.is_playing():
        return

    tts_path = Path(f"temp_speech_{message.id}.mp3")
    try:
        tts = gTTS(text=response_text, lang="ja")
        await asyncio.to_thread(tts.save, str(tts_path))
        source = discord.FFmpegPCMAudio(str(tts_path), executable=str(ffmpeg_path))
        voice_client.play(source)
        while voice_client.is_playing():
            await asyncio.sleep(1)
    except Exception as error:
        print(f"gTTS APIエラー: {error}")
    finally:
        if tts_path.exists():
            tts_path.unlink()

