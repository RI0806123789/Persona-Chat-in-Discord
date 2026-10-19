"""Discord終了時に、共有するGeminiクライアントの接続とDBも閉じる。"""

import asyncio
from typing import Any, Awaitable, Callable

import discord

from persona_bot.core.background import TaskOwner, run_in_thread, spawn, task_scope, wait_for_task
from persona_bot.services.genai_client import close_genai_client


class PersonaClient(discord.Client):
    def __init__(self, *, genai_client: Any, database: Any = None, **options: Any) -> None:
        super().__init__(**options)
        self._genai_client = genai_client
        self._database = database
        self._shutdown_callbacks: list[Callable[[], Awaitable[None]]] = []
        self.background_tasks = TaskOwner()
        self._close_task: asyncio.Task[Any] | None = None

    async def start(self, token: str, *, reconnect: bool = True) -> None:
        # Discordが生成するコマンド・UI処理の子タスクにも所有者を引き継ぐ。
        with self.background_tasks.scope():
            await super().start(token, reconnect=reconnect)

    async def _run_event(self, coro, event_name, *args, **kwargs) -> None:
        await self.background_tasks.run(super()._run_event(coro, event_name, *args, **kwargs))

    def add_shutdown_callback(self, callback: Callable[[], Awaitable[None]]) -> None:
        """共有クライアントの切断前に終了させる処理を明示的に登録する。"""
        self._shutdown_callbacks.append(callback)

    async def close(self) -> None:
        self.background_tasks.release_current()
        if self._close_task is None:
            # 終了タスク自身を回収対象に入れると、自分自身の終了を待ってしまう。
            with task_scope(None):
                self._close_task = spawn(self._close_resources(), label="ボットクライアントの終了")
        await wait_for_task(self._close_task)

    async def _close_resources(self) -> None:
        with self.background_tasks.scope():
            await self._shutdown()

    async def _shutdown(self) -> None:
        try:
            await self.background_tasks.shutdown()
            for callback in self._shutdown_callbacks:
                try:
                    await callback()
                except (Exception, asyncio.CancelledError) as error:
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
