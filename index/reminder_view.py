import discord
from discord import ui

from reminder_service import ReminderService
from view_parts import BackButton, HubFactory, PanelView


def _build_status_text(records: list[dict], service: ReminderService) -> str:
    """リマインダーパネルの表示テキストを組み立てる。"""
    if not records:
        return (
            "⏰ **リマインダーパネル** — 予定されているリマインダーはありません。\n"
            "※ 会話の中で「30分後にゴミ出しって知らせて」のように頼むと登録されます。\n"
            "※ 「毎朝7時に薬って知らせて」のように頼むと、繰り返しで登録されます。"
        )

    lines = ["⏰ **リマインダーパネル** — 予定されているリマインダーです。"]
    # Discord のセレクトは最大25件のため、一覧の表示もそれに揃える。
    for record in records[:25]:
        lines.append(f"・{service.describe(record)}")
    if len(records) > 25:
        lines.append(f"…ほか {len(records) - 25} 件")
    lines.append("※ 取り消したいものはセレクトメニューから選んでください。")
    # 繰り返しは通知しても消えず登録枠を占め続けるため、その点をここで案内する。
    lines.append("※ （毎日）などの繰り返しは、取り消すまでこの一覧に残り続けます。")
    return "\n".join(lines)


class ReminderCancelSelect(ui.Select):
    """取り消すリマインダーを選ぶセレクトメニュー。"""

    def __init__(self, records: list[dict], service: ReminderService) -> None:
        options = []
        # Discord のセレクトは最大25件まで
        for record in records[:25]:
            message = str(record.get("message", ""))
            options.append(
                discord.SelectOption(
                    # ラベルは100文字までのため、本文が長い場合は切り詰める
                    label=message[:100] or "（本文なし）",
                    value=str(record.get("reminder_id")),
                    description=service.describe(record)[:100],
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
