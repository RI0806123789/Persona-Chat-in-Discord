import io
from typing import Any, Callable

import discord
from discord import ui

from constants import VIEW_TIMEOUT_SECONDS

# ハブ（機能パネル）を再生成する呼び出し可能オブジェクトの型。
# functions_view を直接 import すると循環参照になるため、呼び出し側から
# FunctionsView.clone のような生成関数を受け取る形にする。
HubFactory = Callable[[], Any]


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
        self.message: discord.Message | None = None
        # タイムアウトしたときにユーザーへ表示する文言（パネルごとに変える）。
        self.timeout_notice: str = timeout_notice

    async def on_timeout(self) -> None:
        """タイムアウト時に、操作できなくなった UI をメッセージから取り除く。"""
        if self.message is None:
            # メッセージ参照が無い（送信前・差し替え済みなど）ときは何もしない。
            return
        try:
            await self.message.edit(content=self.timeout_notice, view=None)
        except discord.HTTPException as error:
            # 既に消えたメッセージ・権限切れなどで失敗しうるが、致命的ではないのでログのみ。
            print(f"パネル({type(self).__name__})のタイムアウト処理エラー: {error}")

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
            await interaction.response.edit_message(content="パネルを閉じました。", view=None)
            if view is not None:
                view.stop()
            return

        hub = make_hub()
        await interaction.response.edit_message(content=hub.build_preview(), view=hub)
        hub.message = interaction.message
        if view is not None:
            view.stop()


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
    await interaction.response.edit_message(content=content, view=panel)
    # タイムアウト時に UI を取り除けるよう、差し替え先へメッセージ参照を引き継ぐ。
    panel.message = interaction.message
    # ハブは役目を終えたので停止する。止めないとハブのタイムアウト処理が
    # 後からメッセージを誤って上書きする（同じメッセージを共有しているため、
    # 表示中のサブパネルが「機能パネルは時間切れです」に化けてしまう）。
    hub.stop()


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
            await sender(error_notice or f"エラー: グラフの送信に失敗しました。（{error}）", ephemeral=True)
    finally:
        # discord.Fileが差し替えたbuffer.closeを元へ戻してから閉じる。
        if picture is not None:
            picture.close()
        buffer.close()
