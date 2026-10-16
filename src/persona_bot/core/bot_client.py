"""Discord終了時に、共有するGeminiクライアントの接続とDBも閉じる。"""

from typing import Any

import discord

from persona_bot.core.background import run_in_thread
from persona_bot.services.genai_client import close_genai_client


class PersonaClient(discord.Client):
    def __init__(self, *, genai_client: Any, database: Any = None, **options: Any) -> None:
        super().__init__(**options)
        self._genai_client = genai_client
        self._database = database

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            try:
                await close_genai_client(self._genai_client)
            finally:
                # DB は Discord と Gemini を閉じた後、最後に閉じる（終了処理中の保存が使うため）。
                # Database.close はロックを取って実行中の読み書きを待つので、別スレッドで待つ。
                if self._database is not None:
                    await run_in_thread(self._database.close)
