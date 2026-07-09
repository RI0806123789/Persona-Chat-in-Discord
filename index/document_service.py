import asyncio
import importlib
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import discord

from app_state import BotState
from constants import (
    DOCUMENT_WORKER_IDLE_SECONDS,
    GEMINI_RESPONSE_TIMEOUT_SECONDS,
    HISTORY_LIMIT,
    MAX_DOCUMENT_SIZE_BYTES,
    QUEUE_PATH,
)
from content_moderator import ContentModerator
from discord_helpers import clean_message_content, fetch_channel_history_text, format_grounding_sources, is_supported_document_attachment, send_long_reply
from gemini_service import GeminiService
from memory_service import MemoryService
from ng_word_service import NgWordFilter
from paths import load_prompt_template, resolve_prompt_path
from prompting import build_full_prompt, build_memory_sections
from storage import read_text_file, write_json_file
from usage_graph import UsageTracker


class DocumentService:
    def __init__(
        self,
        client: discord.Client,
        state: BotState,
        memory: MemoryService,
        gemini: GeminiService,
        usage: UsageTracker,
        ng_filter: NgWordFilter | None = None,
        moderator: ContentModerator | None = None,
    ) -> None:
        self._client = client
        self._state = state
        self._memory = memory
        self._gemini = gemini
        self._usage = usage
        self._ng_filter = ng_filter
        self._moderator = moderator
        self._queue_lock = asyncio.Lock()

    async def enqueue_from_message(
        self,
        message: discord.Message,
        question: str,
        attachments: Sequence[discord.Attachment],
    ) -> str:
        task_id = f"{message.channel.id}-{message.id}-{int(datetime.now(timezone.utc).timestamp() * 1000)}"
        await self._enqueue(
            {
                "task_id": task_id,
                "status": "pending",
                "channel_id": message.channel.id,
                "message_id": message.id,
                "author_id": message.author.id,
                "question": question,
                "attachment_count": len(attachments),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        return task_id

    async def run_worker(self) -> None:
        while True:
            task = await self._claim_next_task()
            if task is None:
                await asyncio.sleep(DOCUMENT_WORKER_IDLE_SECONDS)
                continue
            await self._process_task(task)

    async def _read_tasks(self) -> list[dict[str, Any]]:
        self._memory.ensure_storage()
        try:
            raw_text = await asyncio.to_thread(read_text_file, QUEUE_PATH)
            if not raw_text.strip():
                return []
            loaded = json.loads(raw_text)
            if isinstance(loaded, dict):
                tasks = loaded.get("tasks", [])
                return tasks if isinstance(tasks, list) else []
        except Exception as error:
            print(f"キュー読み込みエラー: {error}")
        return []

    async def _write_tasks(self, tasks: list[dict[str, Any]]) -> None:
        self._memory.ensure_storage()
        await asyncio.to_thread(write_json_file, QUEUE_PATH, {"tasks": tasks})

    async def _enqueue(self, task: dict[str, Any]) -> None:
        async with self._queue_lock:
            tasks = await self._read_tasks()
            tasks = [item for item in tasks if str(item.get("task_id")) != str(task.get("task_id"))]
            tasks.append(task)
            await self._write_tasks(tasks)

    async def _update_task(self, task_id: str, **updates: Any) -> None:
        async with self._queue_lock:
            tasks = await self._read_tasks()
            updated = False
            for item in tasks:
                if str(item.get("task_id")) == task_id:
                    item.update(updates)
                    item["updated_at"] = datetime.now(timezone.utc).isoformat()
                    updated = True
                    break
            if updated:
                await self._write_tasks(tasks)

    async def _claim_next_task(self) -> dict[str, Any] | None:
        async with self._queue_lock:
            tasks = await self._read_tasks()
            for item in tasks:
                if item.get("status") == "pending":
                    item["status"] = "processing"
                    item["updated_at"] = datetime.now(timezone.utc).isoformat()
                    await self._write_tasks(tasks)
                    return item
        return None

    async def _process_task(self, task: dict[str, Any]) -> None:
        task_id = str(task.get("task_id", ""))
        message: discord.Message | None = None

        try:
            message = await self._fetch_message_from_task(task)
            attachments = [
                attachment
                for attachment in message.attachments
                if is_supported_document_attachment(attachment)
            ]
            if not attachments:
                raise ValueError("対応する文書添付が見つかりませんでした。")

            question = clean_message_content(message, self._client.user)
            if not question:
                question = str(task.get("question", "")).strip() or "このドキュメントの要点を整理してください。"

            for attachment in attachments:
                await self._process_attachment(attachment)

            prompt = load_prompt_template(resolve_prompt_path(self._state.current_prompt_file))
            memory_category = await self._memory.route_category(question)
            memory_context = self._memory.build_context(memory_category)
            document_context = self._memory.build_document_memory_context()
            history_text = await fetch_channel_history_text(
                message.channel,
                self._state.channel_reset_points.get(message.channel.id),
                self._client.user,
                HISTORY_LIMIT,
            )
            full_prompt = build_full_prompt(
                prompt,
                build_memory_sections(memory_category, memory_context, document_context),
                history_text,
                question,
            )
            response_text, usage_info, grounding_sources = await self._gemini.generate(
                full_prompt,
                timeout=GEMINI_RESPONSE_TIMEOUT_SECONDS,
                grounding=self._state.grounding_enabled,
            )
            self._usage.log_snapshot(
                "文書タスク",
                question,
                f"文書添付: {len(attachments)}件",
                full_prompt,
                response_text,
                usage_info,
            )

            if response_text is None:
                raise ValueError("Gemini APIの呼び出しに失敗しました。")

            if self._ng_filter is not None and self._ng_filter.contains_ng_word(response_text):
                detected = self._ng_filter.find_ng_words(response_text)
                print(f"NGワード検出 (文書応答): {detected}")
                response_text = self._ng_filter.mask_ng_words(response_text)

            # --- AI モデレーション（文書処理） ---
            if self._moderator is not None:
                is_safe, reason = await self._moderator.check(response_text, direction="Bot応答")
                if not is_safe:
                    print(f"文書応答モデレーション: {reason}")
                    response_text = "申し訳ありませんが、適切な応答を生成できませんでした。"

            if grounding_sources:
                response_text += format_grounding_sources(grounding_sources)

            await send_long_reply(message, response_text, suppress_embeds=bool(grounding_sources))
            await self._memory.append_pending_log(question, response_text)
            asyncio.create_task(self._memory.flush_if_needed())

            await self._update_task(task_id, status="completed", finished_at=datetime.now(timezone.utc).isoformat())
        except Exception as error:
            print(f"文書タスク処理エラー: {error}")
            await self._update_task(
                task_id,
                status="failed",
                finished_at=datetime.now(timezone.utc).isoformat(),
                error=str(error),
            )
            if message is not None:
                try:
                    await message.reply(f"エラーが発生しました。（文書処理に失敗しました）\n詳細: {error}")
                except Exception as reply_error:
                    print(f"文書タスク失敗通知エラー: {reply_error}")

    async def _fetch_message_from_task(self, task: dict[str, Any]) -> discord.Message:
        channel_id = int(task.get("channel_id", 0))
        message_id = int(task.get("message_id", 0))
        channel = self._client.get_channel(channel_id)
        if channel is None:
            channel = await self._client.fetch_channel(channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel, discord.StageChannel)):
            raise ValueError("文書処理はメッセージ取得に対応したチャンネルでのみ実行できます。")
        return await channel.fetch_message(message_id)

    async def _process_attachment(
        self,
        attachment: discord.Attachment,
    ) -> None:
        if attachment.size > MAX_DOCUMENT_SIZE_BYTES:
            raise ValueError(f"ファイルサイズが上限を超えています: {attachment.filename}")

        suffix = Path(attachment.filename).suffix.lower()
        temp_path: Path | None = None
        uploaded_file: Any = None
        try:
            temp_path = await self._download_to_temp_file(attachment, suffix)
            await validate_document_file(temp_path, suffix)

            uploaded_file = await self._gemini.upload_file(temp_path)
            summary_data = await self._gemini.summarize_document(uploaded_file, attachment.filename)
            await self._memory.store_document_summary(summary_data)
        finally:
            if uploaded_file is not None:
                await self._gemini.delete_file(uploaded_file)
            if temp_path is not None and temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception as error:
                    print(f"一時ファイル削除エラー: {error}")

    async def _download_to_temp_file(self, attachment: discord.Attachment, suffix: str) -> Path:
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        temp_file.close()
        temp_path = Path(temp_file.name)
        temp_bytes = await attachment.read()
        await asyncio.to_thread(temp_path.write_bytes, temp_bytes)
        return temp_path


def validate_pdf_file(temp_path: Path) -> None:
    try:
        pypdf_module = importlib.import_module("pypdf")
    except ImportError:
        pypdf_module = importlib.import_module("PyPDF2")

    reader_class = getattr(pypdf_module, "PdfReader")
    reader = reader_class(str(temp_path))
    if reader.is_encrypted:
        raise ValueError("PDFが暗号化されています。")


def validate_text_file(temp_path: Path) -> None:
    with open(temp_path, "rb") as file:
        preview_bytes = file.read(4096)
    preview_bytes.decode("utf-8-sig")


async def validate_document_file(temp_path: Path, suffix: str) -> None:
    if suffix == ".pdf":
        await asyncio.to_thread(validate_pdf_file, temp_path)
    else:
        await asyncio.to_thread(validate_text_file, temp_path)
