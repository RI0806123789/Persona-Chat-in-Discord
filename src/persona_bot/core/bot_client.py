"""Discord終了時に、共有するGeminiクライアントの接続とDBも閉じる。"""

from typing import Any, Awaitable, Callable

import discord

from persona_bot.core.background import run_in_thread, wait_for_cleanup
from persona_bot.services.genai_client import close_genai_client


class PersonaClient(discord.Client):
    def __init__(self, *, genai_client: Any, database: Any = None, **options: Any) -> None:
        super().__init__(**options)
        self._genai_client = genai_client
        self._database = database
        self._shutdown_callbacks: list[Callable[[], Awaitable[None]]] = []

    def add_shutdown_callback(self, callback: Callable[[], Awaitable[None]]) -> None:
        """共有クライアントの切断前に終了させる処理を明示的に登録する。"""
        self._shutdown_callbacks.append(callback)

    async def close(self) -> None:
        await wait_for_cleanup(self._close_resources(), label="ボットクライアントの終了")

    async def _close_resources(self) -> None:
        try:
            for callback in self._shutdown_callbacks:
                try:
                    await callback()
                except Exception as error:
                    print(f"終了前のバックグラウンド処理の回収に失敗しました: {type(error).__name__}")
        finally:
            try:
                await super().close()
            finally:
                try:
                    await close_genai_client(self._genai_client)
                finally:
                    # DB は共有クライアントと文書処理の終了後、最後に閉じる。
                    if self._database is not None:
                        await run_in_thread(self._database.close)
