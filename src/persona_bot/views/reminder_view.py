import re

import discord
from discord import ui

from persona_bot.config.constants import DISCORD_SELECT_LABEL_MAX_LENGTH, DISCORD_SELECT_MAX_OPTIONS, REMINDER_PANEL_MAX_LENGTH
from persona_bot.services.reminder_service import ReminderService
from persona_bot.views.view_parts import BackButton, HubFactory, PanelView

# 改行・タブ・連続した空白（全角空白を含む）をまとめて1つの半角空白にするための正規表現
_WHITESPACE_RUN = re.compile(r"\s+")


def _to_option_text(text: str) -> str:
    """セレクトの選択肢に載せる文字列を、1行かつ上限の文字数以内にする。

    リマインダーの本文は会話から登録されるため改行を含みうる。選択肢の
    label / description に改行が入っていると Discord がパネルごと受け付けず、
    改行入りの予定を1件登録しただけで一覧も取り消しも開けなくなる。
    改行を除いてから切り詰め、切った位置に残った空白も落とす。
    """
    single_line = _WHITESPACE_RUN.sub(" ", text).strip()
    return single_line[:DISCORD_SELECT_LABEL_MAX_LENGTH].rstrip()


def _build_status_text(records: list[dict], service: ReminderService) -> str:
    """リマインダーパネルの表示テキストを組み立てる。"""
    if not records:
        return (
            "⏰ **リマインダーパネル** — 予定されているリマインダーはありません。\n"
            "※ 会話の中で「30分後にゴミ出しって知らせて」のように頼むと登録されます。\n"
            "※ 「毎朝7時に薬って知らせて」のように頼むと、繰り返しで登録されます。"
        )

    header = "⏰ **リマインダーパネル** — 予定されているリマインダーです。"
    # 本文の省略は表示だけに適用し、取り消し用の選択肢は減らさない。
    lines = [
        f"・{service.describe(record)}"
        for record in records[:DISCORD_SELECT_MAX_OPTIONS]
    ]
    # 繰り返しは通知しても消えず登録枠を占め続けるため、その点をここで案内する。
    notes = [
        "※ 取り消したいものはセレクトメニューから選んでください。",
        "※ （毎日）などの繰り返しは、取り消すまでこの一覧に残り続けます。",
    ]
    while True:
        omitted = len(records) - len(lines)
        notice = [f"…ほか {omitted} 件"] if omitted else []
        text = "\n".join([header, *lines, *notice, *notes])
        # 注記・省略件数・改行も含めて上限を確認し、日時や本文の途中で切らない。
        if len(text) <= REMINDER_PANEL_MAX_LENGTH or not lines:
            return text
        lines.pop()


class ReminderCancelSelect(ui.Select):
    """取り消すリマインダーを選ぶセレクトメニュー。"""

    def __init__(self, records: list[dict], service: ReminderService) -> None:
        options = []
        # Discord のセレクトは最大25件まで
        for record in records[:DISCORD_SELECT_MAX_OPTIONS]:
            options.append(
                discord.SelectOption(
                    # ラベルは100文字までのため、本文が長い場合は切り詰める。
                    # description も本文を含むので、同じく1行にしてから切り詰める
                    label=_to_option_text(str(record.get("message", ""))) or "（本文なし）",
                    value=str(record.get("reminder_id")),
                    description=_to_option_text(service.describe(record)),
                )
            )
        super().__init__(placeholder="🗑️ 取り消すリマインダーを選択…", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "ReminderView" = self.view  # type: ignore[assignment]
        reminder_id = int(self.values[0])

        # 取り消しは SQL 側でも user_id で絞るため、他人のものは取り消せない
        canceled = await view.service.cancel(reminder_id, view.viewer_id)
        if canceled is None:
            await interaction.response.edit_message(
                content="このリマインダーは既に通知済みか、取り消されています。", view=None
            )
            view.stop()
            return

        await interaction.response.edit_message(
            content=f"🗑️ リマインダーを取り消しました。\n・{view.service.describe(canceled)}",
            view=None,
        )
        view.stop()


class ReminderView(PanelView):
    """リマインダーの一覧・取り消しパネル。

    表示するのは実行者本人のリマインダーだけ（内容に予定などの私的情報が入るため）。
    パネル自体はエフェメラル（本人だけに見える）で表示される想定。
    """

    def __init__(
        self,
        service: ReminderService,
        records: list[dict],
        viewer_id: str,
        *,
        make_hub: HubFactory | None = None,
    ) -> None:
        super().__init__(
            timeout_notice=(
                "⏱️ リマインダーパネルは時間切れになりました。"
                "再度 /functions を実行してください。"
            )
        )
        self.service = service
        self.records = records
        self.viewer_id = viewer_id
        # 「戻る」ボタンが機能パネルを再生成するための生成関数
        self.make_hub = make_hub

        self._add_all_items()

    def _add_all_items(self) -> None:
        """UI パーツを View に追加する。"""
        # 1件も無いときはセレクトを出さない（空の選択肢は Discord 側でエラーになる）
        if self.records:
            self.add_item(ReminderCancelSelect(self.records, self.service))
        self.add_item(BackButton(row=1))

    def build_preview(self) -> str:
        """現在のリマインダー一覧からパネル表示テキストを組み立てる。"""
        return _build_status_text(self.records, self.service)
