"""Discord終了時に、共有するGeminiクライアントの接続も閉じる。"""

from typing import Any

import discord

from persona_bot.services.genai_client import close_genai_client


class PersonaClient(discord.Client):
    def __init__(self, *, genai_client: Any, **options: Any) -> None:
        super().__init__(**options)
        self._genai_client = genai_client

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            await close_genai_client(self._genai_client)
