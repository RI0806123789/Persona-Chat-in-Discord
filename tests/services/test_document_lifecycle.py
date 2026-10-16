"""文書タスクの終端状態・設定固定・添付要約とPDFストリームの回収を検証する。"""

import asyncio
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

try:
    from pypdf import PdfWriter
except ImportError:
    PdfWriter = None
from persona_bot.core.app_state import BotState
from persona_bot.core.database import Database
from persona_bot.services import document_service, memory_service
from persona_bot.services.document_service import DocumentService, validate_pdf_file
from persona_bot.services.memory_service import MemoryService


class DocumentLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.db = Database(root / "bot.db")
        self.db.initialize()
        self.addCleanup(self.db.close)
        self.state = BotState()
        self.settings = self.state.get_guild(1)
        self.settings.current_prompt_file = "../prompts/prompt_default.txt"
        self.settings.current_model_name = "old-model"
        self.settings.grounding_enabled = True
        self.memory = MemoryService(self.state, None, self.db)
        paths = patch.multiple(memory_service, MEMORY_DIR=root,
                               PROFILE_MEMORY_PATH=root / "profile.json", PROJECTS_MEMORY_PATH=root / "projects.json")
        paths.start()
        self.addCleanup(paths.stop)
        self.memory.route_category = AsyncMock(return_value="none")
        self.memory.flush_if_needed = AsyncMock()
        self.gemini = SimpleNamespace(generate=AsyncMock(return_value=("架空の回答", None, [])))
        self.service = DocumentService(
            SimpleNamespace(user=SimpleNamespace(id=99)), self.state, self.memory,
            self.gemini, SimpleNamespace(log_snapshot=Mock()), self.db,
        )
        self.message = SimpleNamespace(
            content="架空の質問", guild=SimpleNamespace(id=1), channel=SimpleNamespace(id=10),
            attachments=[SimpleNamespace(filename=f"fictional-{i}.txt") for i in range(4)], reply=AsyncMock(),
        )
        self.service._fetch_message_from_task = AsyncMock(return_value=self.message)
        self.models = []

        async def attachment(item, user_id, model):
            self.models.append(model)
            await self.memory.store_document_summary(user_id, {"document_name": item.filename, "summary": f"架空の要約 {item.filename}"})

        self.service._process_attachment = AsyncMock(side_effect=attachment)
        self.attachment = attachment
        self.sent = AsyncMock()
        self.loader = Mock(return_value="架空のペルソナ")
        for name, replacement in (
            ("load_prompt_template", self.loader), ("fetch_channel_history_text", AsyncMock(return_value="")),
            ("send_long_reply", self.sent), ("spawn", lambda coro, **kwargs: coro.close()),
        ):
            patcher = patch.object(document_service, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.task = {"task_id": "fictional-task", "status": "pending", "author_id": "11"}
        self.db.queue_upsert_task(self.task)

    async def test_prune_failure_after_answer_does_not_fail_task_or_reply_twice(self) -> None:
        with patch.object(self.db, "queue_prune_finished", side_effect=RuntimeError("架空の掃除エラー")):
            await self.service._process_task(self.task)
        self.sent.assert_awaited_once()
        self.message.reply.assert_not_awaited()
        # 公開の取得APIがないため、状態だけを一時DBから確認する。
        with self.db._lock:
            row = self.db._conn.execute("SELECT status FROM queue_tasks WHERE task_id = ?", (self.task["task_id"],)).fetchone()
        self.assertEqual(row["status"], "completed")

    async def test_all_four_attachment_summaries_are_present_in_final_prompt(self) -> None:
        await self.service._process_task(self.task)
        prompt = self.gemini.generate.await_args.args[0]
        for attachment in self.message.attachments:
            self.assertIn(f"架空の要約 {attachment.filename}", prompt)

    async def test_setting_change_during_first_attachment_keeps_initial_settings(self) -> None:
        async def change(item, user_id, model):
            await self.attachment(item, user_id, model)
            self.settings.current_model_name = "new-model"
            self.settings.current_prompt_file = "../prompts/prompt_maid.txt"
            self.settings.grounding_enabled = False
            await asyncio.sleep(0)

        self.service._process_attachment.side_effect = change
        await self.service._process_task(self.task)
        self.assertEqual(self.models, ["old-model"] * 4)
        self.assertEqual(self.loader.call_args.args[0].name, "prompt_default.txt")
        self.assertEqual(self.gemini.generate.await_args.args[1], "old-model")
        self.assertTrue(self.gemini.generate.await_args.kwargs["grounding"])


class PdfStreamTests(unittest.TestCase):
    def test_pdf_reader_receives_stream_and_it_is_closed_on_every_exit(self) -> None:
        for outcome in ("success", "encrypted", "error"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "fictional.pdf"
                path.write_bytes(b"pdf")
                streams = []

                def reader(stream):
                    self.assertTrue(hasattr(stream, "read"))
                    streams.append(stream)
                    if outcome == "error":
                        raise ValueError("架空のPDF検証エラー")
                    return SimpleNamespace(is_encrypted=outcome == "encrypted")

                with patch.dict(sys.modules, {"pypdf": SimpleNamespace(PdfReader=reader)}):
                    if outcome == "success":
                        validate_pdf_file(path)
                    else:
                        with self.assertRaises(ValueError):
                            validate_pdf_file(path)
                self.assertTrue(streams[0].closed)
                path.unlink()

    @unittest.skipIf(PdfWriter is None, "pypdf が未導入のため、実際のPDFの検証は省略します。")
    def test_real_valid_and_encrypted_pdfs_can_be_removed_after_validation(self) -> None:
        for encrypted in (False, True):
            with self.subTest(encrypted=encrypted), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "fictional.pdf"
                writer = PdfWriter()
                writer.add_blank_page(width=72, height=72)
                if encrypted:
                    writer.encrypt("fictional-password")
                with path.open("wb") as stream:
                    writer.write(stream)
                if encrypted:
                    with self.assertRaises(ValueError):
                        validate_pdf_file(path)
                else:
                    validate_pdf_file(path)
                path.unlink()
                self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
