import asyncio
from dataclasses import dataclass
import io
from typing import Any, Callable

import discord
from discord import ui

from persona_bot.config.constants import DISCORD_SELECT_LABEL_MAX_LENGTH, VIEW_TIMEOUT_SECONDS
from persona_bot.utils.discord_helpers import reject_blocked_interaction

# ハブ（機能パネル）を再生成する呼び出し可能オブジェクトの型。
# functions_view を直接 import すると循環参照になるため、呼び出し側から
# FunctionsView.clone のような生成関数を受け取る形にする。
HubFactory = Callable[[], Any]


class _PanelState:
    def __init__(self, view: "PanelView") -> None:
        self.current: PanelView | None = view
        self.generation = 0
        self.edit_lock = asyncio.Lock()


@dataclass(frozen=True)
class _PanelOperation:
    source: "PanelView"
    state: _PanelState
    generation: int

    def is_current(self) -> bool:
        # 元Viewが遷移で停止しても、受付済みの最新操作は次の遷移を完了できる。
        # 停止済みViewそのものの再表示は、編集時に別途拒否する。
        return self.generation == self.state.generation


def begin_panel_operation(view: ui.View | None, interaction: discord.Interaction) -> _PanelOperation | None:
    """DB待機より前に操作の世代を確保する。同じInteractionでは二重に進めない。"""
    if not isinstance(view, PanelView):
        return None
    extras = getattr(interaction, "extras", None)
    if not isinstance(extras, dict):
        extras = interaction.extras = {}
    previous = extras.get("persona_panel_operation")
    if isinstance(previous, _PanelOperation) and previous.source is view:
        return previous
    state = view._panel_state
    if state.current is not view or view.is_finished():
        operation = _PanelOperation(view, state, -1)
    else:
        state.generation += 1
        operation = _PanelOperation(view, state, state.generation)
    extras["persona_panel_operation"] = operation
    return operation


def select_option_text(text: Any, *, fallback: str = "") -> str:
    """選択肢の表示文を1行・上限以内にし、空なら代わりの表示名を使う。"""
    single_line = " ".join(("" if text is None else str(text)).split())
    return single_line[:DISCORD_SELECT_LABEL_MAX_LENGTH].rstrip() or fallback


def panel_operation_is_current(view: ui.View | None, interaction: discord.Interaction) -> bool:
    extras = getattr(interaction, "extras", {})
    operation = extras.get("persona_panel_operation") if isinstance(extras, dict) else None
    if not isinstance(operation, _PanelOperation):
        operation = begin_panel_operation(view, interaction)
    return operation is None or operation.is_current()


async def _edit_panel_message(
    interaction: discord.Interaction, *, content: str, view: ui.View | None, deferred: bool,
) -> None:
    if deferred or getattr(interaction.response, "is_done", lambda: not hasattr(interaction.response, "edit_message"))():
        await interaction.edit_original_response(content=content, view=view)
    else:
        await interaction.response.edit_message(content=content, view=view)


async def edit_panel_message(
    interaction: discord.Interaction, *, content: str, view: ui.View | None = None,
    source: ui.View | None = None, deferred: bool = False,
) -> bool:
    """同じパネルの編集を直列化し、後から始まった操作がある旧更新を破棄する。"""
    extras = getattr(interaction, "extras", {})
    operation = extras.get("persona_panel_operation") if isinstance(extras, dict) else None
    if not isinstance(operation, _PanelOperation) or (source is not None and operation.source is not source):
        operation = begin_panel_operation(source if source is not None else view, interaction)
    if operation is None:
        await _edit_panel_message(interaction, content=content, view=view, deferred=deferred)
        return True
    state = operation.state
    try:
        # 先行するHTTP編集が遅くても、ロック待ちでInteractionを失効させない。
        if state.edit_lock.locked() and not getattr(interaction.response, "is_done", lambda: False)():
            await interaction.response.defer(ephemeral=True)
        async with state.edit_lock:
            if not operation.is_current() or (view is not None and view.is_finished()):
                if view is not None and view is not state.current:
                    view.stop()
                if not getattr(interaction.response, "is_done", lambda: False)():
                    await interaction.response.defer(ephemeral=True)
                return False
            await _edit_panel_message(interaction, content=content, view=view, deferred=deferred)
            previous = state.current
            if previous is not view:
                state.current = view
                if isinstance(view, PanelView):
                    view._panel_state = state
                if previous is not None:
                    previous.stop()
            return True
    except BaseException:
        # ロック待ちの取り消しでも、不採用の遷移先を残さない。
        if view is not None and view is not state.current:
            view.stop()
        raise


async def bind_panel_message(
    interaction: discord.Interaction, panel: ui.View, *, previous_message: Any = None,
) -> None:
    """編集後の参照取得に失敗しても、直前のWebhook参照で時間切れ処理を保つ。"""
    panel.message = previous_message
    try:
        panel.message = await interaction.original_response()
    except Exception as error:
        print(f"パネル({type(panel).__name__})の応答参照の取得に失敗しました: {error}")


class PanelView(ui.View):
    """/functions のハブと各サブパネルに共通する土台となる View。

    各パネルにコピーされていた次の定型をここへ集約する:
      ・タイムアウト時に UI を取り除いて時間切れ表示へ差し替える処理（on_timeout）
      ・パネルメッセージの参照（message）
      ・操作待ち時間のマジックナンバー（constants.VIEW_TIMEOUT_SECONDS）

    サブクラスは UI パーツの追加を _add_all_items に実装し、__init__ の
    最後で一度だけ自分で呼ぶ（基底では呼ばない — サブクラス固有の属性が
    まだ初期化されていない段階で呼ぶと AttributeError になるため）。
    """

    def __init__(self, *, timeout: float = VIEW_TIMEOUT_SECONDS, timeout_notice: str) -> None:
        super().__init__(timeout=timeout)
        # タイムアウト時に UI を取り除くためのパネルメッセージ参照（送信後に設定される）。
        self.message: discord.InteractionMessage | None = None
        # タイムアウトしたときにユーザーへ表示する文言（パネルごとに変える）。
        self.timeout_notice: str = timeout_notice
        self.blocked_user_check: Callable[[int], bool] | None = None
        self._panel_state = _PanelState(self)

    def stop(self) -> None:
        if self._panel_state.current is self:
            self._panel_state.current = None
            self._panel_state.generation += 1
        super().stop()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """パネルを開いた後にブロックされた利用者も、操作のたびに拒否する。"""
        if self.is_finished() or self._panel_state.current is not self:
            return False
        if self.blocked_user_check is not None and await reject_blocked_interaction(interaction, self.blocked_user_check):
            return False
        # グラフ等の別メッセージへの操作では、保留中のパネル更新を失効させない。
        # 画面を更新するコールバックだけが、待機前に操作世代を確保する。
        return not self.is_finished() and self._panel_state.current is self

    async def on_timeout(self) -> None:
        """タイムアウト時に、操作できなくなった UI をメッセージから取り除く。"""
        state = self._panel_state
        if state.current is not self:
            return
        if self.message is None:
            # メッセージ参照が無い（送信前・差し替え済みなど）ときは何もしない。
            self.stop()
            return
        state.generation += 1
        generation = state.generation
        async with state.edit_lock:
            if state.current is not self or state.generation != generation:
                return
            try:
                await self.message.edit(content=self.timeout_notice, view=None)
            except discord.HTTPException as error:
                print(f"パネル({type(self).__name__})のタイムアウト処理エラー: {error}")
            finally:
                self.stop()

    def refresh_items(self) -> None:
        """UI パーツを組み直す（セレクト選択やトグル操作のあとに呼ぶ）。

        Select/Button のコールバックから `view._add_all_items()` とプライベート
        メソッドを直接呼んでいた8か所を、公開 API 1本に置き換えるためのもの。
        別クラスから他クラスの非公開メソッドを触る形を無くし、組み直しの手順
        （クリア → 再追加）をこの1か所に閉じ込める。
        """
        self.clear_items()
        self._add_all_items()

    def _add_all_items(self) -> None:
        """UI パーツを View に追加する。サブクラスが実装する。

        パーツを組み直さないパネルでも、refresh_items が呼ばれたときに
        黙って何も起きない状態を避けるため、基底では例外にしている。
        """
        raise NotImplementedError(
            f"{type(self).__name__} は _add_all_items() を実装していません。"
        )


class BackButton(ui.Button):
    """機能パネル（ハブ）へ戻るボタン。各サブパネル共通。

    親 View が保持する make_hub でハブパネルを再生成し、同じメッセージを
    差し替える。make_hub が無い場合は従来どおりパネルを閉じる。
    """

    def __init__(self, *, row: int) -> None:
        super().__init__(label="戻る", style=discord.ButtonStyle.secondary, emoji="↩️", row=row)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        make_hub: HubFactory | None = getattr(view, "make_hub", None)
        if make_hub is None:
            # エフェメラルメッセージは delete() できないため、内容を編集して閉じる。
            edited = await edit_panel_message(interaction, content="パネルを閉じました。", view=None, source=view)
            if edited and view is not None:
                view.stop()
            return

        hub = make_hub()
        hub.blocked_user_check = getattr(view, "blocked_user_check", None)
        if not await edit_panel_message(interaction, content=hub.build_preview(), view=hub, source=view):
            return
        if view is not None:
            view.stop()
        # エフェメラルはチャンネル経由では編集できないためWebhookの参照を保持する。
        await bind_panel_message(interaction, hub, previous_message=getattr(view, "message", None))


async def swap_panel(
    interaction: discord.Interaction,
    hub: ui.View,
    panel: PanelView,
    content: str,
) -> None:
    """ハブが開いている（エフェメラルな）メッセージを、サブパネルへ差し替える。

    Discord の View は最大5行までのため機能を1枚に合体できない。そこで
    「同じメッセージの中身だけを入れ替える」方式で各パネルを行き来する。

    引数:
        interaction: ハブのボタンが受け取った Interaction。
        hub:         差し替え元のハブ View（差し替え後に停止する）。
        panel:       差し替え先のサブパネル。
        content:     差し替え後のメッセージ本文（通常は panel.build_preview()）。
    """
    panel.blocked_user_check = getattr(hub, "blocked_user_check", None)
    if not await edit_panel_message(interaction, content=content, view=panel, source=hub):
        return
    # ハブは役目を終えたので停止する。止めないとハブのタイムアウト処理が
    # 後からメッセージを誤って上書きする（同じメッセージを共有しているため、
    # 表示中のサブパネルが「機能パネルは時間切れです」に化けてしまう）。
    hub.stop()
    await bind_panel_message(interaction, panel, previous_message=getattr(hub, "message", None))


async def send_graph_ephemeral(
    interaction: discord.Interaction,
    buffer: io.BytesIO,
    filename: str,
    caption: str,
    *,
    followup: bool = False,
    error_log: str | None = None,
    error_notice: str | None = None,
) -> None:
    """グラフ画像を、本人だけに見えるエフェメラルの新規メッセージとして送る。

    パネル自体は残したまま結果だけを別メッセージで返す用途（📊 グラフ /
    📊 全体比較）に使う。送信に失敗した場合はエラー文だけを返し、例外は
    呼び出し側へ伝播させない（パネル操作を止めないため）。

    **buffer の所有権はこの関数にある。** 呼び出し側は buffer を渡したら
    close してはならない — 成否にかかわらずこの関数が必ず close する。
    （元の GraphButton / RankingButton が、送信直後の finally で自分の
    buffer を閉じていた挙動をそのまま引き継いだもの。discord.File は
    ファイルオブジェクトを渡した場合その所有権を持たず閉じてくれない。）

    引数:
        interaction: ボタンが受け取った Interaction。
        buffer:      PNG 画像を保持する BytesIO。この関数が閉じる。
        filename:    Discord 上でのファイル名（例: "usage_graph.png"）。
        caption:     画像に添えるメッセージ本文。
        followup:    defer済みの場合はTrueにし、followupで送信する。
        error_log:   送信失敗ログの文脈名。省略時はファイル名を含む。
        error_notice: 送信失敗時の案内文。省略時は例外の内容を添える。
    """
    picture = None
    try:
        picture = discord.File(buffer, filename=filename)
        sender = interaction.followup.send if followup else interaction.response.send_message
        await sender(caption, file=picture, ephemeral=True)
    except Exception as error:
        print(f"{error_log or f'グラフ送信中に予期せぬエラー ({filename})'}: {error}")
        # 送信が始まる前に失敗した場合のみ、同じ Interaction でエラーを返せる。
        if followup or not interaction.response.is_done():
            sender = interaction.followup.send if followup else interaction.response.send_message
            try:
                await sender(error_notice or f"エラー: グラフの送信に失敗しました。（{error}）", ephemeral=True)
            except Exception as notice_error:
                print(f"グラフ送信失敗の案内も送れませんでした ({filename}): {notice_error}")
    finally:
        # discord.Fileが差し替えたbuffer.closeを元へ戻してから閉じる。
        if picture is not None:
            picture.close()
        buffer.close()
