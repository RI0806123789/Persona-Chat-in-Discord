import asyncio
import codecs
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import discord

from persona_bot.services.affinity_service import strip_affinity_tag
from persona_bot.core.app_state import BotState
from persona_bot.core.background import run_in_thread, spawn
from persona_bot.config.constants import (
    DOCUMENT_MEMORY_CONTEXT_LIMIT,
    DOCUMENT_WORKER_IDLE_SECONDS,
    GEMINI_RESPONSE_TIMEOUT_SECONDS,
    HISTORY_LIMIT,
    MAX_DOCUMENT_SIZE_BYTES,
    QUEUE_MAX_FINISHED_TASKS,
    TEXT_VALIDATION_PREVIEW_BYTES,
)
from persona_bot.services.content_moderator import ContentModerator, screen_bot_response
from persona_bot.core.database import Database
from persona_bot.utils.discord_helpers import (
    clean_message_content,
    fetch_channel_history_text,
    format_grounding_sources,
    is_supported_document_attachment,
    is_supported_message_channel,
    send_long_reply,
)
from persona_bot.services.gemini_service import GeminiService
from persona_bot.services.memory_service import MemoryService
from persona_bot.services.ng_word_service import NgWordFilter
from persona_bot.config.paths import load_prompt_template, resolve_prompt_path
from persona_bot.services.prompting import build_full_prompt, build_memory_sections
from persona_bot.utils.usage_graph import UsageTracker


# 処理が終わった（もう再処理しない）タスクのステータス
_TERMINAL_STATUSES = {"completed", "failed"}


class DocumentService:
    def __init__(
        self,
        client: discord.Client,
        state: BotState,
        memory: MemoryService,
        gemini: GeminiService,
        usage: UsageTracker,
        db: Database,
        ng_filter: NgWordFilter | None = None,
        moderator: ContentModerator | None = None,
    ) -> None:
        self._client = client
        self._state = state
        self._memory = memory
        self._gemini = gemini
        self._usage = usage
        self._db = db
        self._ng_filter = ng_filter
        self._moderator = moderator

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
        try:
            await self._recover_stuck_tasks()
        except Exception as error:
            print(f"文書ワーカー: 中断タスクの復旧に失敗しました: {error}")

        while True:
            # キューの読み書きで予期しない例外（一時的な DB ロック等）が
            # 発生してもワーカーが停止しないよう、ループ全体を保護してリトライする。
            try:
                task = await self._claim_next_task()
                if task is None:
                    await asyncio.sleep(DOCUMENT_WORKER_IDLE_SECONDS)
                    continue
                await self._process_task(task)
            except Exception as error:
                print(f"文書ワーカー: 予期しないエラー: {error}")
                await asyncio.sleep(DOCUMENT_WORKER_IDLE_SECONDS)

    async def _recover_stuck_tasks(self) -> None:
        """再起動前に "processing" のまま中断されたタスクを "pending" に戻す。

        これがないと、処理中に Bot が落ちたタスクは二度と拾われず放置される。
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        recovered = await asyncio.to_thread(self._db.queue_recover_stuck, now_iso)
        if recovered:
            print(f"文書ワーカー: 中断された processing タスク {recovered} 件を pending に戻しました。")

    async def _enqueue(self, task: dict[str, Any]) -> None:
        await asyncio.to_thread(self._db.queue_upsert_task, task)

    async def _update_task(self, task_id: str, **updates: Any) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        await asyncio.to_thread(self._db.queue_update_task, task_id, updates, now_iso)
        # 完了/失敗タスクが際限なく溜まらないよう、終了時に古いものから削除する
        if updates.get("status") in _TERMINAL_STATUSES:
            try:
                await asyncio.to_thread(self._db.queue_prune_finished, QUEUE_MAX_FINISHED_TASKS)
            except Exception as error:
                # 終端状態の保存は成功済み。掃除の失敗で二重返信やfailedへの転落を起こさない。
                print(f"文書ワーカー: 終了タスクの掃除に失敗しました: {error}")

    async def _claim_next_task(self) -> dict[str, Any] | None:
        now_iso = datetime.now(timezone.utc).isoformat()
        return await asyncio.to_thread(self._db.queue_claim_next_pending, now_iso)

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

            # 文書タスクは投稿者の記憶スペースに紐付けて処理する（ユーザー別記憶）。
            author_id = str(task.get("author_id", "") or "")
            question = clean_message_content(message, self._client.user)
            if not question:
                question = str(task.get("question", "")).strip() or "このドキュメントの要点を整理してください。"

            # ペルソナ/モデル/グラウンディングはメッセージが投稿されたサーバー（DM ならDM共有設定）のものを使う。
            guild_id = message.guild.id if message.guild is not None else None
            guild_settings = self._state.get_guild(guild_id)
            response_settings = guild_settings.snapshot_response()

            for attachment in attachments:
                await self._process_attachment(attachment, author_id, response_settings.model_name)

            prompt_file = response_settings.prompt_file
            prompt = await run_in_thread(lambda: load_prompt_template(resolve_prompt_path(prompt_file)))
            memory_category = await self._memory.route_category(question)
            memory_context, document_context = await self._memory.build_contexts(
                memory_category, author_id, limit=max(DOCUMENT_MEMORY_CONTEXT_LIMIT, len(attachments)),
            )
            history_text = await fetch_channel_history_text(
                message.channel,
                guild_settings.channel_reset_points.get(message.channel.id),
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
                response_settings.model_name,
                timeout=GEMINI_RESPONSE_TIMEOUT_SECONDS,
                grounding=response_settings.grounding_enabled,
            )

            if not response_text:
                raise ValueError("Gemini APIの呼び出しに失敗しました。")

            # 文書経路では親密度を更新せず、共通関数で感情タグだけを除去する。
            # タグのみの応答は、空本文や参考リンクだけを送らず生成失敗として扱う。
            response_text = strip_affinity_tag(response_text)
            if not response_text:
                raise ValueError("Gemini APIの応答に本文がありませんでした。")

            self._usage.log_snapshot(
                "文書タスク",
                question,
                f"文書添付: {len(attachments)}件",
                full_prompt,
                response_text,
                usage_info,
            )

            response_text = await screen_bot_response(
                response_text, self._ng_filter, self._moderator, log_label="文書応答"
            )

            # 参考リンクは送信用テキストにのみ追記する（長期記憶にはノイズになるため含めない）
            reply_text = response_text
            if grounding_sources:
                reply_text += format_grounding_sources(grounding_sources)

            await send_long_reply(message, reply_text, suppress_embeds=bool(grounding_sources))
            await self._memory.append_pending_log(author_id, question, response_text)
            spawn(self._memory.flush_if_needed(), label="記憶圧縮（文書処理便乗）")

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
        # on_message 側で受け付けるチャンネル種別（DM 含む）と判定を揃える
        if not is_supported_message_channel(channel):
            raise ValueError("文書処理はメッセージ取得に対応したチャンネルでのみ実行できます。")
        return await channel.fetch_message(message_id)

    async def _process_attachment(
        self,
        attachment: discord.Attachment,
        author_id: str,
        model_name: str,
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
            summary_data = await self._gemini.summarize_document(uploaded_file, attachment.filename, model_name)
            # 文書要約は投稿者の記憶スペースへ保存する（ユーザー別記憶）。
            await self._memory.store_document_summary(author_id, summary_data)
        finally:
            try:
                if uploaded_file is not None:
                    await self._gemini.delete_file(uploaded_file)
            finally:
                # アップロード先の削除が失敗しても、ローカルの回収を実行する。
                if temp_path is not None:
                    self._remove_temp_file(temp_path)

    async def _download_to_temp_file(self, attachment: discord.Attachment, suffix: str) -> Path:
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        temp_path = Path(temp_file.name)
        downloaded = False
        try:
            temp_file.close()
            temp_bytes = await attachment.read()
            write_task = spawn(
                asyncio.to_thread(temp_path.write_bytes, temp_bytes), label="文書の一時ファイル書き込み"
            )
            try:
                await asyncio.shield(write_task)
            except asyncio.CancelledError:
                # to_thread の書き込みはキャンセルしても止まらない。
                # 完了前に削除すると、遅れて write_bytes がファイルを再作成し得る。
                while not write_task.done():
                    try:
                        await asyncio.shield(write_task)
                    except asyncio.CancelledError:
                        # 停止要求が重なっても、書き込みの終了までは回収を待つ。
                        continue
                    except Exception:
                        # キャンセルを優先し、書き込みエラーは spawn のログに残す。
                        break
                raise
            downloaded = True
            return temp_path
        finally:
            # 正常に返した時点で呼び出し側へ所有権を渡す。それまではここで回収する。
            if not downloaded:
                self._remove_temp_file(temp_path)

    @staticmethod
    def _remove_temp_file(temp_path: Path) -> None:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError as error:
            print(f"一時ファイル削除エラー: {error}")


def validate_pdf_file(temp_path: Path) -> None:
    # pypdf は PDF の検証にしか使わないため、ここで読み込む。モジュールの先頭で import すると、
    # pypdf の無い環境では起動・通常のチャット・回帰テストの検出まで ImportError で止まる。
    try:
        from pypdf import PdfReader
    except ImportError as error:
        # 失敗の詳細はユーザーへの返信にも載るため、英語の例外ではなく原因が分かる文にする
        raise ValueError("PDFの検証に必要な pypdf がインストールされていません。") from error
    with temp_path.open("rb") as file:
        reader = PdfReader(file)
        if reader.is_encrypted:
            raise ValueError("PDFが暗号化されています。")


def validate_text_file(temp_path: Path) -> None:
    with open(temp_path, "rb") as file:
        preview_bytes = file.read(TEXT_VALIDATION_PREVIEW_BYTES)
    # プレビューの末尾がマルチバイト文字（日本語は3バイト）の途中になり得るため、
    # 途中で切れた場合は末尾の不完全な文字を許容してデコード検証する。
    # 単純な decode() だと正常な日本語ファイルが境界位置次第で拒否される。
    truncated = len(preview_bytes) >= TEXT_VALIDATION_PREVIEW_BYTES
    decoder = codecs.getincrementaldecoder("utf-8-sig")()
    decoder.decode(preview_bytes, final=not truncated)


async def validate_document_file(temp_path: Path, suffix: str) -> None:
    if suffix == ".pdf":
        await run_in_thread(validate_pdf_file, temp_path)
    else:
        await run_in_thread(validate_text_file, temp_path)
