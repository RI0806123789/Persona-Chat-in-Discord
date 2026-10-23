"""サブエージェントの進捗メッセージと中止ボタン（#170）。

子が作業している間は、依頼への返信として進捗メッセージを1通出し、手順ごとに書き換える。
文面はペルソナの定型文（案A。LLM を呼ばない）で、`agent_service.render_agent_line` が組み立てる。

- 書き換えは `AGENT_PROGRESS_MIN_INTERVAL_SECONDS` より短い間隔では行わない（Discord のレート制限）。
  終了時の書き換えだけは間隔に関わらず必ず行う。
- 進捗の送信・書き換えの失敗はログに残すだけで、子の作業は止めない（フェイルオープン）。
- 中止ボタンは依頼した本人だけが押せる。全体の期限（`AGENT_TOTAL_TIMEOUT_SECONDS`）もここで管理する。

機能パネル（`PanelView`）ではなく、チャンネルに出す1通のメッセージに付ける部品なので
`ui.View` を直接継承する（パネルの差し替え・操作世代の仕組みは使わない）。
"""

from __future__ import annotations

import asyncio
from time import monotonic
from typing import Any, Coroutine, Mapping

import discord
from discord import ui

from persona_bot.config.constants import AGENT_PROGRESS_MIN_INTERVAL_SECONDS, AGENT_TOTAL_TIMEOUT_SECONDS
from persona_bot.core.background import spawn, wait_for_cleanup
from persona_bot.services.agent_plan import AgentPlan
from persona_bot.services.agent_service import AgentReport, render_agent_line


class AgentCancelView(ui.View):
    """進捗メッセージに付ける中止ボタン。押せるのは依頼した本人だけ。"""

    def __init__(self, requester_id: int, cancel_event: asyncio.Event) -> None:
        # 作業の期限より少し長く待つ（期限で終わったときは呼び出し側がボタンを外す）。
        super().__init__(timeout=AGENT_TOTAL_TIMEOUT_SECONDS + 60)
        self.requester_id = requester_id
        self.cancel_event = cancel_event

    @ui.button(label="中止", emoji="⏹️", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: ui.Button) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("依頼した人だけが中止できます。", ephemeral=True)
            return
        # 進捗メッセージの書き換えは作業側が行う（ここで書き換えると終了時の書き換えと競合する）。
        self.cancel_event.set()
        await interaction.response.defer()


class AgentProgress:
    """依頼1件分の進捗メッセージ。start → update（手順ごと）→ finish の順に使う。"""

    def __init__(self, message: discord.Message, lines: Mapping[str, str], plan: AgentPlan) -> None:
        self._source = message
        self._lines = lines
        self._plan = plan
        self._cancel_event = asyncio.Event()
        self._view = AgentCancelView(message.author.id, self._cancel_event)
        self._message: discord.Message | None = None
        self._last_edit = 0.0
        self._header = render_agent_line(lines, "start", role=plan.role, total=len(plan.steps))

    def _line(self, key: str, *, index: int = 0, step: str = "") -> str:
        return render_agent_line(
            self._lines, key, role=self._plan.role, step=step, current=index + 1, total=len(self._plan.steps),
        )

    async def start(self) -> None:
        """依頼への返信として進捗メッセージを出す（失敗しても作業は続ける）。"""
        first_step = self._plan.steps[0]
        content = f"{self._header}\n{self._line('progress', step=first_step)}"
        try:
            self._message = await self._source.reply(
                content, view=self._view, allowed_mentions=discord.AllowedMentions.none(),
            )
            self._last_edit = monotonic()
        except discord.DiscordException as error:
            print(f"サブエージェント: 進捗メッセージを送れませんでした: {type(error).__name__}")
            self._view.stop()

    async def update(self, index: int, step: str) -> None:
        """手順の開始を表示する。最初の手順は start で表示済み。間隔が短ければ見送る。"""
        if index == 0 or self._message is None:
            return
        if monotonic() - self._last_edit < AGENT_PROGRESS_MIN_INTERVAL_SECONDS:
            return
        await self._edit(f"{self._header}\n{self._line('progress', index=index, step=step)}", view=self._view)

    async def finish(self, key: str) -> None:
        """終了の定型文（done / failed / canceled）に書き換え、中止ボタンを外す。"""
        self._view.stop()
        if self._message is None:
            # 進捗メッセージを出せなかった場合も、失敗と中止だけは伝える（完了は人格役の返信で分かる）。
            if key != "done":
                try:
                    await self._source.reply(self._line(key), allowed_mentions=discord.AllowedMentions.none())
                except discord.DiscordException as error:
                    print(f"サブエージェント: 終了の案内を送れませんでした: {type(error).__name__}")
            return
        await self._edit(f"{self._header}\n{self._line(key)}", view=None)

    async def _edit(self, content: str, *, view: ui.View | None) -> None:
        try:
            await self._message.edit(content=content, view=view)
            self._last_edit = monotonic()
        except discord.DiscordException as error:
            print(f"サブエージェント: 進捗メッセージを更新できませんでした: {type(error).__name__}")

    async def run(self, work: Coroutine[Any, Any, AgentReport | None]) -> tuple[str, AgentReport | None]:
        """子の作業を、全体の期限と中止ボタンを見ながら待つ。

        Returns:
            ("done", 報告) / ("failed", None) / ("canceled", None)。
            呼び出し側のキャンセル（ボットの終了など）はそのまま伝える。
        """
        worker = spawn(work, label="サブエージェントの作業")
        canceled = spawn(self._cancel_event.wait(), label="サブエージェントの中止待ち")
        try:
            await asyncio.wait(
                {worker, canceled}, timeout=AGENT_TOTAL_TIMEOUT_SECONDS, return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            # 中止後の後片付けが報告を返しても、正常に完了した作業と取り違えない。
            worker_finished = worker.done()
            cancel_requested = self._cancel_event.is_set()
            for task in (worker, canceled):
                if not task.done():
                    task.cancel()

            async def collect_children() -> None:
                # 子の例外も回収し、片方の失敗で他方の終了待ちを打ち切らない。
                await asyncio.gather(worker, canceled, return_exceptions=True)

            # 実行枠の解放前に両方を回収する。追加のキャンセルも回収後に伝える。
            await wait_for_cleanup(collect_children(), label="サブエージェントの終了待ち")
        if worker_finished and not worker.cancelled():
            error = worker.exception()
            if error is not None:
                print(f"サブエージェント: 作業中にエラーが発生しました: {type(error).__name__}: {error}")
                return "failed", None
            report = worker.result()
            return ("done", report) if report is not None else ("failed", None)
        if cancel_requested:
            print("サブエージェント: 依頼した本人が中止しました。")
            return "canceled", None
        print(f"サブエージェント: 期限（{AGENT_TOTAL_TIMEOUT_SECONDS:.0f}秒）に達したため中止しました。")
        return "failed", None
