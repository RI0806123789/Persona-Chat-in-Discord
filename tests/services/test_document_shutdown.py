"""文書ワーカーだけを回収してから共有接続を閉じ、中断タスクを再実行できる。"""

import asyncio
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord
from persona_bot.core import bot_client
from persona_bot.core.app_state import BotState
from persona_bot.core.background import spawn
from persona_bot.core.bot_client import PersonaClient
from persona_bot.core.database import Database
from persona_bot.services.document_service import DocumentService


class DocumentShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_cleanup_precedes_client_close_and_interrupted_task_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bot.db"
            db = Database(path)
            self.addCleanup(db.close)
            db.initialize()
            order = []
            summarizing, deleting, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

            async def summarize(*args):
                summarizing.set()
                await asyncio.Event().wait()

            async def delete(*args):
                deleting.set()
                await release.wait()
                order.append("delete")

            client = PersonaClient(genai_client=object(), database=db, intents=discord.Intents.none())
            service = DocumentService(
                client, BotState(), SimpleNamespace(store_document_summary=AsyncMock()),
                SimpleNamespace(upload_file=AsyncMock(return_value=SimpleNamespace(name="files/fictional")),
                                summarize_document=AsyncMock(side_effect=summarize), delete_file=AsyncMock(side_effect=delete)),
                SimpleNamespace(log_snapshot=Mock()), db,
            )
            client.add_shutdown_callback(service.stop_worker)
            message = SimpleNamespace(
                id=20, content="架空の質問", guild=SimpleNamespace(id=1), channel=SimpleNamespace(id=10),
                author=SimpleNamespace(id=11), reply=AsyncMock(),
                attachments=[SimpleNamespace(filename="fictional.txt", size=10, read=AsyncMock(return_value=b"fictional text"))],
            )
            service._fetch_message_from_task = AsyncMock(return_value=message)
            await service.enqueue_from_message(message, "架空の質問", message.attachments)
            original_close = db.close

            def close_db():
                order.append("db")
                original_close()

            unrelated = spawn(asyncio.Event().wait(), label="終了対象外のタスクの確認")
            closing = None
            with patch.object(discord.Client, "close", new=AsyncMock(side_effect=lambda: order.append("discord"))), patch.object(
                bot_client, "close_genai_client", new=AsyncMock(side_effect=lambda *_: order.append("gemini")),
            ), patch.object(db, "close", side_effect=close_db):
                try:
                    with client.background_tasks.scope():
                        service.start_worker()
                    worker = service._worker
                    service.start_worker()
                    self.assertIs(service._worker, worker)
                    await asyncio.wait_for(summarizing.wait(), 3)
                    closing = spawn(client.close(), label="文書処理中のクライアント終了の確認")
                    await asyncio.wait_for(deleting.wait(), 3)
                    for _ in range(2):
                        closing.cancel()
                        await asyncio.sleep(0)
                        self.assertFalse(closing.done())
                        self.assertEqual(order, [])
                    self.assertFalse(unrelated.done())
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await closing
                finally:
                    release.set()
                    if closing is not None:
                        await asyncio.gather(closing, return_exceptions=True)
                    await service.stop_worker()
                    unrelated.cancel()
                    await asyncio.gather(unrelated, return_exceptions=True)
            self.assertEqual(order, ["delete", "discord", "gemini", "db"])
            self.assertIsNone(service._worker)
            message.reply.assert_not_awaited()
            connection = sqlite3.connect(path)
            try:
                self.assertEqual(connection.execute("SELECT status FROM queue_tasks").fetchone()[0], "processing")
            finally:
                connection.close()
            restarted = Database(path)
            try:
                self.assertEqual(restarted.queue_recover_stuck("fictional-restart"), 1)
                self.assertEqual(restarted.queue_claim_next_pending("fictional-restart")["status"], "processing")
            finally:
                restarted.close()


if __name__ == "__main__":
    unittest.main()
