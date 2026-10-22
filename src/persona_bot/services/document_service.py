import asyncio
import hashlib
import codecs
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import discord

from persona_bot.services.affinity_service import strip_affinity_tag
from persona_bot.core.app_state import BotState
from persona_bot.core.background import run_in_thread, spawn, wait_for_cleanup
from persona_bot.config.constants import (
    DOCUMENT_MEMORY_CONTEXT_LIMIT,
    DOCUMENT_ONLY_QUESTION,
    DOCUMENT_QUEUE_MAX_ACTIVE_PER_USER,
    DOCUMENT_QUEUE_MAX_ACTIVE_TOTAL,
    DOCUMENT_WORKER_IDLE_SECONDS,
    GEMINI_RESPONSE_TIMEOUT_SECONDS,
    HISTORY_LIMIT,
    MAX_DOCUMENT_SIZE_BYTES,
    QUEUE_MAX_FINISHED_TASKS,
    TEXT_VALIDATION_PREVIEW_BYTES,
)
from persona_bot.services.content_moderator import ContentModerator, screen_bot_response_with_status
from persona_bot.core.database import Database
from persona_bot.utils.discord_helpers import (
    clean_message_content,
    fetch_channel_history_text,
    format_grounding_sources,
    is_supported_document_attachment,
    is_supported_message_channel,
    send_long_reply,
)
from persona_bot.utils.timeutil import utc_now_iso
from persona_bot.services.gemini_service import GeminiService
from persona_bot.services.memory_service import MemoryService
from persona_bot.services.ng_word_service import NgWordFilter
from persona_bot.config.paths import load_persona_template
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
        self._worker: asyncio.Task[Any] | None = None
        self._stopping = False

    def start_worker(self) -> None:
        """終了時に回収できるよう、このサービスのワーカーだけを保持する。"""
        if not self._stopping and (self._worker is None or self._worker.done()):
            self._worker = spawn(self.run_worker(), label="文書処理ワーカー")

    async def stop_worker(self) -> None:
        """Gemini・Discord・DBが有効な間に、中断とファイル回収を完了する。"""
        self._stopping = True
        await wait_for_cleanup(self._stop_worker(), label="文書ワーカーの終了")

    async def _stop_worker(self) -> None:
        worker = self._worker
        if worker is not None:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
            finally:
                self._worker = None

    async def enqueue_from_message(
        self,
        message: discord.Message,
        question: str,
        attachments: Sequence[discord.Attachment],
    ) -> str | None:
        """文書タスクを受け付けてタスクIDを返す。未処理件数の上限に達していれば追加せず None を返す。"""
        now = datetime.now(timezone.utc)
        # 取り出し順は ORDER BY created_at, task_id で決まり、task_id の先頭はチャンネルID。
        # 秒精度にすると同じ1秒に届いた別チャンネルの文書が到着順に処理されなくなるため、
        # この列だけはマイクロ秒まで残す（他の時刻は timeutil.utc_now_iso の秒精度）。
        now_iso = now.isoformat(timespec="microseconds")
        task_id = f"{message.channel.id}-{message.id}-{int(now.timestamp() * 1000)}"
        result = await self._enqueue(
            {
                "task_id": task_id,
                "status": "pending",
                "channel_id": message.channel.id,
                "message_id": message.id,
                "author_id": message.author.id,
                "question": question,
                "attachment_count": len(attachments),
                "created_at": now_iso,
                "updated_at": now_iso,
            }
        )
        if result != "accepted":
            scope = "利用者別" if result == "user_limit" else "全体"
            print(f"文書キュー: 処理待ちの上限（{scope}）に達したため、新しい文書を受け付けませんでした。")
            return None
        return task_id

    async def run_worker(self) -> None:
        # 起動時の復旧が完了してから通常処理へ進む。成功後は再実行しない。
        while not self._stopping:
            try:
                await self._recover_stuck_tasks()
            except Exception as error:
                print(f"文書ワーカー: 中断タスクの復旧に失敗しました。待機後に再試行します: {error}")
                await asyncio.sleep(DOCUMENT_WORKER_IDLE_SECONDS)
            else:
                break

        while not self._stopping:
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
        now_iso = utc_now_iso()
        recovered = await run_in_thread(self._db.queue_recover_stuck, now_iso)
        if recovered:
            print(f"文書ワーカー: 中断された processing タスク {recovered} 件を pending に戻しました。")

    async def _enqueue(self, task: dict[str, Any]) -> str:
        # 件数の確認と追加は DB 側で1つのトランザクションにまとめる（並行した受付でも上限を超えない）。
        return await run_in_thread(
            self._db.queue_try_enqueue, task,
            DOCUMENT_QUEUE_MAX_ACTIVE_PER_USER, DOCUMENT_QUEUE_MAX_ACTIVE_TOTAL,
        )

    async def _update_task(self, task_id: str, **updates: Any) -> None:
        now_iso = utc_now_iso()
        await run_in_thread(self._db.queue_update_task, task_id, updates, now_iso)
        # 完了/失敗タスクが際限なく溜まらないよう、終了時に古いものから削除する
        if updates.get("status") in _TERMINAL_STATUSES:
            try:
                await run_in_thread(self._db.queue_prune_finished, QUEUE_MAX_FINISHED_TASKS)
            except Exception as error:
                # 終端状態の保存は成功済み。掃除の失敗で二重返信やfailedへの転落を起こさない。
                print(f"文書ワーカー: 終了タスクの掃除に失敗しました: {error}")

    async def _claim_next_task(self) -> dict[str, Any] | None:
        now_iso = utc_now_iso()
        return await run_in_thread(self._db.queue_claim_next_pending, now_iso)

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
            author_id = str(task.get("author_id") or message.author.id)
            if author_id == "0":
                author_id = str(message.author.id)
            # 質問は、受付時に NG 判定と入力モデレーションを通した保存済みの値だけを使う（#203）。
            # 再取得した投稿の本文は、受付後に編集されていても誰も検査していないため生成へ渡さない。
            # 添付は編集で外すことしかできず差し替えられないので、再取得した一覧を使ってよい。
            question = str(task.get("question", "")).strip()
            if clean_message_content(message, self._client.user) != question:
                print("文書タスク: 受付後に本文が編集されたため、受付時に検査した質問で回答します。")
            question = question or DOCUMENT_ONLY_QUESTION

            # ペルソナ/モデル/グラウンディングはメッセージが投稿されたサーバー（DM ならDM共有設定）のものを使う。
            guild_id = message.guild.id if message.guild is not None else None
            guild_settings = self._state.get_guild(guild_id)
            response_settings = guild_settings.snapshot_response()

            summaries = [
                await self._process_attachment(attachment, author_id, response_settings.model_name)
                for attachment in attachments
            ]
            await self._memory.store_document_summaries(author_id, summaries)

            prompt_file = response_settings.prompt_file
            prompt = await load_persona_template(prompt_file)
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

            response_text, accepted = await screen_bot_response_with_status(
                response_text, self._ng_filter, self._moderator, log_label="文書応答"
            )

            # 参考リンクは送信用テキストにのみ追記する（長期記憶にはノイズになるため含めない）
            reply_text = response_text
            if accepted and grounding_sources:
                reply_text += format_grounding_sources(grounding_sources)

            # DiscordとDBは同じトランザクションにできない。送信開始を先に記録し、
            # 強制終了で配送結果が不明なら自動再送を止める。通常のキャンセルは送信・保存後に伝える。
            await self._update_task(task_id, status="sending")
            await wait_for_cleanup(
                self._send_and_complete(task_id, message, reply_text, grounding_sources, author_id,
                                        question, response_text, accepted),
                label="文書返信の送信と完了記録",
            )
        except Exception as error:
            if self._stopping:
                # 終了中の中断は processing のまま残し、次回起動時に復旧する。
                return
            print(f"文書タスク処理エラー: {error}")
            try:
                await self._update_task(
                    task_id,
                    status="failed",
                    finished_at=utc_now_iso(),
                    error=str(error),
                )
            except Exception as update_error:
                print(f"文書タスク: 失敗状態の保存に失敗しました: {type(update_error).__name__}")
            if message is not None:
                try:
                    await message.reply(f"エラーが発生しました。（文書処理に失敗しました）\n詳細: {error}")
                except Exception as reply_error:
                    print(f"文書タスク失敗通知エラー: {reply_error}")
            return

    async def _send_and_complete(
        self, task_id: str, message: discord.Message, reply_text: str, grounding_sources: list,
        author_id: str, question: str, response_text: str, accepted: bool,
    ) -> None:
        await send_long_reply(message, reply_text, suppress_embeds=bool(grounding_sources))
        # 回答は送信済み。完了記録を先に試み、保存の失敗を再送や二重のエラー返信へ変えない。
        try:
            await self._update_task(task_id, status="completed", finished_at=utc_now_iso())
        except Exception as error:
            print(f"文書タスク: 回答後の完了状態の保存に失敗しました: {type(error).__name__}")
        try:
            if accepted:
                await self._memory.append_pending_log(author_id, question, response_text)
                spawn(self._memory.flush_if_needed(), label="記憶圧縮（文書処理便乗）")
        except Exception as error:
            print(f"文書タスク: 回答後の記憶追記に失敗しました: {type(error).__name__}")

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
    ) -> dict[str, Any]:
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
            summary_data["document_hash"] = await run_in_thread(self._document_hash, temp_path)
            return summary_data
        finally:
            try:
                if uploaded_file is not None:
                    await wait_for_cleanup(
                        self._gemini.delete_file(uploaded_file), label="文書アップロードの削除",
                    )
            finally:
                # アップロード先の削除が失敗しても、ローカルの回収を実行する。
                if temp_path is not None:
                    self._remove_temp_file(temp_path)

    @staticmethod
    def _document_hash(path: Path) -> str:
        """ファイル内容だけの指紋を、同期ワーカーで算出する。"""
        return hashlib.sha256(path.read_bytes()).hexdigest()

    async def _download_to_temp_file(self, attachment: discord.Attachment, suffix: str) -> Path:
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        temp_path = Path(temp_file.name)
        downloaded = False
        try:
            temp_file.close()
            temp_bytes = await attachment.read()
            if len(temp_bytes) > MAX_DOCUMENT_SIZE_BYTES:
                raise ValueError(f"ファイルサイズが上限を超えています: {attachment.filename}")
            # 書き込み完了まで待ち、キャンセル時も回収後の再作成を防ぐ。
            await run_in_thread(temp_path.write_bytes, temp_bytes)
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
