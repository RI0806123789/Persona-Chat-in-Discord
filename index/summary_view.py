import re
from datetime import datetime, timedelta, timezone

import discord
from discord import ui

from app_state import BotState
from choices import SUMMARY_PERIOD_CHOICES
from constants import SUMMARY_MAX_MESSAGES, SUMMARY_TIMEOUT_SECONDS
from content_moderator import ContentModerator, screen_bot_response
from discord_helpers import (
    build_summary_source_text,
    fetch_messages_since,
    is_supported_message_channel,
    split_message,
)
from gemini_service import GeminiService
from ng_word_service import NgWordFilter
from paths import load_prompt_template, resolve_prompt_path
from prompting import build_summary_prompt
from usage_graph import UsageTracker
from view_parts import BackButton, HubFactory

# 感情タグ [V:.., A:..] を除去するための正規表現（ペルソナが誤って付けた場合の保険）
_AFFINITY_TAG_PATTERN = r'\[V:\s*[+-]?\d+\s*,\s*A:\s*[+-]?\d+\s*\]'

# Markdown の見出し記号（行頭の # 〜 ######）を除去する正規表現。
# Discord は h4 以降（####）を描画せず「#### 見出し」がそのまま表示され見づらいため、
# モデルが指示に反して付けてしまった場合の保険としてプレーンテキスト化する。
_MD_HEADING_PATTERN = r'(?m)^[ \t]{0,3}#{1,6}[ \t]*'

# 期間選択のデフォルト（SUMMARY_PERIOD_CHOICES の「1日」）
_DEFAULT_PERIOD = next(c for c in SUMMARY_PERIOD_CHOICES if c.value == 24)


async def generate_channel_summary(
    channel: discord.abc.Messageable,
    bot_user: object,
    period: "discord.app_commands.Choice[int]",
    *,
    state: BotState,
    gemini: GeminiService,
    moderator: ContentModerator,
    ng_filter: NgWordFilter,
    usage: UsageTracker,
) -> tuple[bool, str]:
    """チャンネルの会話を議事録として要約する。

    戻り値 (is_summary, text):
        is_summary=True  → text は要約本体（ヘッダー付き）。表示範囲に応じて出し分ける。
        is_summary=False → text は通知・エラーメッセージ（パネルにそのまま表示する）。
    """
    after_time = datetime.now(timezone.utc) - timedelta(hours=period.value)

    # --- 履歴取得 ---
    try:
        messages = await fetch_messages_since(channel, after_time, SUMMARY_MAX_MESSAGES)
    except discord.Forbidden:
        return False, "⚠️ メッセージ履歴を読む権限がありません。ボットの権限設定を確認してください。"
    except Exception as error:
        print(f"要約: 履歴取得エラー: {error}")
        return False, "⚠️ 履歴の取得に失敗しました。"

    # --- 自動停止: 上限超過なら要約せず注意メッセージ ---
    if len(messages) > SUMMARY_MAX_MESSAGES:
        return False, (
            f"⚠️ 対象メッセージが多すぎます（上限 {SUMMARY_MAX_MESSAGES} 件）。"
            "より短い期間を選んで再実行してください。"
        )

    source_text = build_summary_source_text(messages, bot_user)
    line_count = len(source_text.splitlines()) if source_text.strip() else 0
    if line_count == 0:
        return False, f"この期間（{period.name}）には要約できる会話がありませんでした。"

    # --- ペルソナテンプレート読み込み（/status で選択中のペルソナ） ---
    try:
        persona_prompt = load_prompt_template(resolve_prompt_path(state.current_prompt_file))
    except FileNotFoundError:
        # ペルソナが見つからなくても議事録自体は作れるため、口調なしで続行する
        print(f"要約: プロンプトファイル({state.current_prompt_file})が見つかりません。口調なしで続行します。")
        persona_prompt = ""

    full_prompt = build_summary_prompt(persona_prompt, period.name, source_text)

    # モデルは gemini.generate が内部で state.current_model_name（/status の設定）を参照する。
    # グラウンディングは既存テキストの要約に不要なため無効。
    response_text, usage_info, _sources = await gemini.generate(
        full_prompt,
        images=None,
        timeout=SUMMARY_TIMEOUT_SECONDS,
        grounding=False,
    )

    if not response_text:
        return False, "⚠️ 要約の生成に失敗しました。時間をおいて再度お試しください。"

    # 感情タグが混じった場合の保険（ペルソナが誤って付けることがある）
    response_text = re.sub(_AFFINITY_TAG_PATTERN, "", response_text)
    # Markdown 見出し記号を除去してプレーンテキスト化（#### 等が Discord で見づらいため）
    response_text = re.sub(_MD_HEADING_PATTERN, "", response_text).strip()

    # Bot自身の応答としてNGワードマスク＋AIモデレーションを適用する（既存パイプラインと同様）
    response_text = await screen_bot_response(
        response_text, ng_filter, moderator, log_label="会話要約"
    )

    usage.log_snapshot(
        "会話要約", f"{period.name}の要約", f"{line_count}件", full_prompt, response_text, usage_info
    )

    header = f"📋 **会話要約（期間: {period.name} / 対象: {line_count}件）**\n\n"
    return True, header + response_text


class SummaryPeriodSelect(ui.Select):
    """要約する期間を選ぶセレクトメニュー。"""

    def __init__(self, current_value: int) -> None:
        options = [
            discord.SelectOption(
                label=choice.name,
                # SelectOption の value は文字列のみ。int は str に変換して保持する。
                value=str(choice.value),
                default=(choice.value == current_value),
            )
            for choice in SUMMARY_PERIOD_CHOICES
        ]
        super().__init__(placeholder="🕒 要約する期間を選択…", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "SummaryView" = self.view  # type: ignore[assignment]
        selected = int(self.values[0])
        view.temp_period = next(c for c in SUMMARY_PERIOD_CHOICES if c.value == selected)
        view.clear_items()
        view._add_all_items()
        await interaction.response.edit_message(content=view.build_preview(), view=view)


class PrivateToggleButton(ui.Button):
    """要約結果の表示範囲（自分だけ / 全員）を切り替えるトグルボタン。"""

    def __init__(self, private: bool) -> None:
        label = "🔒 表示: 自分だけ" if private else "🌐 表示: 全員"
        style = discord.ButtonStyle.primary if private else discord.ButtonStyle.secondary
        super().__init__(label=label, style=style, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "SummaryView" = self.view  # type: ignore[assignment]
        view.temp_private = not view.temp_private
        view.clear_items()
        view._add_all_items()
        await interaction.response.edit_message(content=view.build_preview(), view=view)


class RunSummaryButton(ui.Button):
    """要約を実行するボタン。"""

    def __init__(self) -> None:
        super().__init__(label="要約実行", style=discord.ButtonStyle.success, emoji="📋", row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "SummaryView" = self.view  # type: ignore[assignment]

        channel = interaction.channel
        if not is_supported_message_channel(channel):
            await interaction.response.edit_message(
                content="このチャンネルでは要約を実行できません。", view=None
            )
            view.stop()
            return

        # パネル（エフェメラル）を「要約中」に更新してコンポーネント応答を返す。
        await interaction.response.edit_message(content="🔄 要約中...", view=None)
        # 以降は長時間処理になるため、View のタイムアウトとの競合を避けて先に停止する。
        view.stop()

        bot_user = interaction.client.user
        is_summary, text = await generate_channel_summary(
            channel,
            bot_user,
            view.temp_period,
            state=view.state,
            gemini=view.gemini,
            moderator=view.moderator,
            ng_filter=view.ng_filter,
            usage=view.usage,
        )

        # 通知・エラーはパネル（本人だけに見える）にそのまま表示して終了。
        if not is_summary:
            await interaction.edit_original_response(content=text)
            return

        chunks = split_message(text)
        if view.temp_private:
            # 結果を自分だけに: パネルを結果で上書きし、続きも本人だけの followup で送る。
            await interaction.edit_original_response(content=chunks[0])
            for chunk in chunks[1:]:
                await interaction.followup.send(chunk, ephemeral=True)
        else:
            # 結果を全員に: チャンネルへ投稿し、パネルは完了表示にする。
            for chunk in chunks:
                await channel.send(chunk)
            await interaction.edit_original_response(content="✅ 要約をチャンネルに投稿しました。")


class SummaryView(ui.View):
    """会話要約の設定パネル全体を表す View。

    期間セレクト・表示範囲トグル・要約実行/閉じるボタンを保持する。
    パネル自体はエフェメラル（本人だけに見える）で表示される想定。
    """

    def __init__(
        self,
        state: BotState,
        gemini: GeminiService,
        moderator: ContentModerator,
        ng_filter: NgWordFilter,
        usage: UsageTracker,
        *,
        make_hub: HubFactory | None = None,
        timeout: float = 120.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.state = state
        self.gemini = gemini
        self.moderator = moderator
        self.ng_filter = ng_filter
        self.usage = usage
        # 「戻る」ボタンが機能パネルを再生成するための生成関数
        self.make_hub = make_hub
        # タイムアウト時に UI を取り除くためのパネルメッセージ参照（送信後に設定される）
        self.message: discord.Message | None = None

        # 一時選択値
        self.temp_period = _DEFAULT_PERIOD
        self.temp_private: bool = False  # 結果を自分だけに表示するか（デフォルト: 全員）

        self._add_all_items()

    def _add_all_items(self) -> None:
        """UIパーツを View に追加する。選択・トグル更新時にも使用する。"""
        self.add_item(SummaryPeriodSelect(self.temp_period.value))
        self.add_item(PrivateToggleButton(self.temp_private))
        self.add_item(RunSummaryButton())
        self.add_item(BackButton(row=2))

    def build_preview(self) -> str:
        """現在の一時選択値からパネル表示テキストを組み立てる。"""
        visibility = "🔒 自分だけ" if self.temp_private else "🌐 全員に表示"
        return (
            "📋 **会話要約パネル** — 期間と表示範囲を選んで「要約実行」を押してください。\n"
            f"・**期間**: {self.temp_period.name}\n"
            f"・**表示範囲**: {visibility}"
        )

    async def on_timeout(self) -> None:
        """タイムアウト時に、操作できなくなった UI をメッセージから取り除く。"""
        if self.message is None:
            return
        try:
            await self.message.edit(
                content="⏱️ 要約パネルは時間切れになりました。再度 /summarize を実行してください。",
                view=None,
            )
        except discord.HTTPException as error:
            print(f"要約パネルのタイムアウト処理エラー: {error}")
