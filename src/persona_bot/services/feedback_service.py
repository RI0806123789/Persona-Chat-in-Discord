"""リアクション（👍/👎）によるフィードバック収集サービス。

ボットのチャット返信に 👍 と 👎 を自動で付け、押されたリアクションを
「どのモデル・どのペルソナの返信が良かったか」の記録として DB へ残す。
応答品質のチューニングとモデル選定の判断材料にするための付随機能なので、
**この機能の失敗でチャット本体を止めない**（付与できなくても会話は続く）。

■ 評価対象の覚え方（インメモリのレジストリ）
リアクションのイベント（on_raw_reaction_add）はチャンネル内のあらゆる
メッセージについて飛んでくるため、「どれがボットのチャット返信か」を
こちらで覚えておく必要がある。送信のたびに DB へ書くとチャットの主経路が
重くなるので、直近 FEEDBACK_TRACKED_MESSAGES_MAX 件だけをメモリに保持する
リングバッファ方式にしている（溢れた古い返信へのリアクションは無視される。
再起動でも消えるが、評価は「返信直後に押すもの」なので実用上は困らない）。

■ 数えないリアクション
  - ボット自身が付けた 👍👎（自動付与でイベントが発火するため。最大のノイズ源）
  - 他のボットが付けたリアクション（絵文字ボットなどの自動反応）
  - 👍👎 以外の絵文字
  - レジストリに無いメッセージ（＝ボットのチャット返信ではないもの）

■ 数えるリアクション
返信の宛先ではない第三者の評価も数える（応答品質はチャンネルの参加者全員が
判断できるため）。ただし「評価者（user_id）」と「返信の宛先（target_user_id）」は
別々に保存し、後から当事者だけで絞り込める余地を残す。

■ 付与の間引き（クールダウン）
👍👎 の付与は返信ごとに Discord への API 呼び出しを2回増やす。この経路は
チャンネル単位のレート制限が厳しく、待たされるとチャット全体の応答が遅くなる。
ただし詰まるのは**連投しているときだけ**なので、一律に「N 回に1回」で間引くと
痛くない場面のデータまで等しく捨てることになる。そこで
FEEDBACK_REACTION_COOLDOWN_SECONDS 以内に同じチャンネルへ付与していたら
今回は見送る、という形にした（単発の会話にはほぼ全件付き、バーストのときだけ減る）。

**間引くのは付与だけで、レジストリへの登録は全返信に対して行う。** 登録は
インメモリの辞書に1行書くだけでコストが無く、登録さえ残っていれば
「付与されなかった返信にユーザーが自分で 👍👎 を押した」場合も記録できるため
（間引いた分がそのままデータの損失にはならない）。
"""

import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import discord

from persona_bot.core.background import run_in_thread, spawn
from persona_bot.config.choices import MODEL_DISPLAY_NAMES, PERSONA_DISPLAY_NAMES
from persona_bot.config.constants import (
    FEEDBACK_ENABLED,
    FEEDBACK_MAX_STORED_ROWS,
    FEEDBACK_REACTION_BAD,
    FEEDBACK_REACTION_COOLDOWN_SECONDS,
    FEEDBACK_REACTION_GOOD,
    FEEDBACK_SUMMARY_MAX_ROWS,
    FEEDBACK_TRACKED_MESSAGES_MAX,
)
from persona_bot.core.database import Database


@dataclass
class _TrackedReply:
    """評価対象として覚えておくボット返信1件分の情報。

    リアクションが押された時点では「その返信をどのモデル・ペルソナで
    生成したか」を後から知る手段が無い（設定はいつでも変えられる）ため、
    送信した瞬間の値をここへ写し取っておく。
    """

    guild_id: int
    channel_id: int
    target_user_id: str
    model_name: str
    prompt_file: str


def _utc_now_iso() -> str:
    """記録時刻（UTC・秒精度の ISO 8601）。他テーブルと桁数を揃える。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _rating_of(emoji: Any) -> int | None:
    """リアクションの絵文字を評価値（👍=1 / 👎=-1）に直す。対象外なら None。

    payload.emoji は PartialEmoji なので、カスタム絵文字と取り違えないよう
    name ではなく str() で比較する（カスタム絵文字は "<:name:id>" になる）。
    """
    text = str(emoji)
    if text == FEEDBACK_REACTION_GOOD:
        return 1
    if text == FEEDBACK_REACTION_BAD:
        return -1
    return None


def _format_satisfaction(good: int, bad: int) -> str:
    """満足度（👍 が全体に占める割合）を表示用の文字列にする。"""
    total = good + bad
    if total <= 0:
        return "—"
    return f"{round(good * 100 / total)}%"


class FeedbackService:
    """ボット返信への 👍/👎 の付与・記録・集計を担うサービス。"""

    def __init__(self, client: discord.Client, db: Database) -> None:
        self._client = client
        self._db = db
        # メッセージID → 評価対象の情報。古い順に捨てられるよう OrderedDict を使う。
        self._tracked: "OrderedDict[int, _TrackedReply]" = OrderedDict()
        # チャンネルID → 最後に 👍👎 を付与した時刻（time.monotonic 基準）。
        # 付与を間引くクールダウンの判定だけに使う。単調増加する時計を使うのは、
        # システム時刻が変更されてもクールダウンが飛んだり固まったりしないため。
        # 期限切れの項目は判定のたびに捨てるので、規模は「直近クールダウン時間内に
        # ボットが返信したチャンネル数」で自然に頭打ちになる（上限の指定は不要）。
        self._last_reaction_at: dict[int, float] = {}

    # ---- 評価対象の登録とリアクションの付与 ------------------------------------

    def track_reply(
        self,
        sent: discord.Message | None,
        *,
        target_user_id: str,
        model_name: str,
        prompt_file: str,
    ) -> None:
        """ボットのチャット返信を評価対象として登録し、必要なら 👍👎 を付ける。

        **同期メソッドであることが重要。** レジストリへの登録だけは呼び出しと
        同時に済ませ、通信を伴うリアクション付与のみをバックグラウンドへ逃がす
        （登録まで後回しにすると、付与が終わる前に人間が押したリアクションを
        取りこぼしうる）。チャットの主経路から呼ばれるため、ここでは待たない。

        **登録は必ず全返信に対して行い、間引くのは付与だけ**（モジュールの
        docstring「付与の間引き」を参照）。ここで登録を飛ばすと、付与されなかった
        返信にユーザーが自分で 👍👎 を押しても記録できなくなる。

        Args:
            sent: 送信済みの返信メッセージ（send_long_reply の戻り値＝1通目）。
                None のときは何もしない。
            target_user_id: その返信の宛先（会話相手）の Discord ユーザーID。
            model_name: 返信の生成に使ったモデル名。
            prompt_file: 返信時のペルソナ（プロンプトファイル）。
        """
        if not FEEDBACK_ENABLED or sent is None:
            return

        channel_id = int(sent.channel.id)
        self._tracked[sent.id] = _TrackedReply(
            guild_id=sent.guild.id if sent.guild is not None else 0,
            channel_id=channel_id,
            target_user_id=str(target_user_id),
            model_name=str(model_name),
            prompt_file=str(prompt_file),
        )
        # 上限を超えた分は古いものから捨てる（放っておくとメモリを食い続けるため）。
        while len(self._tracked) > FEEDBACK_TRACKED_MESSAGES_MAX:
            self._tracked.popitem(last=False)

        if not self._claim_reaction_slot(channel_id):
            return
        spawn(self._add_reactions(sent), label="フィードバック用リアクションの付与")

    def _claim_reaction_slot(self, channel_id: int) -> bool:
        """このチャンネルへ今 👍👎 を付けてよいかを判定し、付ける場合は記録する。

        判定と記録を1つのメソッドにまとめてあるのは、**間に await を挟ませない**
        ため（「付けてよい」と判断してから記録するまでに他の返信が割り込むと、
        同時に2件が通ってクールダウンが効かなくなる）。呼び出し側も同期のまま。

        ついでに期限切れの項目を捨てる。期限切れの項目は二度と付与を止めない
        （時刻の比較で必ず通る）ので、持ち続けても意味がないぶん残すとメモリを
        食い続ける。捨てることで辞書の規模が「直近クールダウン時間内に返信した
        チャンネル数」で頭打ちになる。

        Returns:
            付与してよければ True（このとき付与済みとして時刻を記録する）。
            クールダウン中なら False。
        """
        if FEEDBACK_REACTION_COOLDOWN_SECONDS <= 0:
            # 間引きを無効にした設定。従来どおり全返信へ付与する
            # （記録も残さない ＝ 無駄な辞書を育てない）。
            return True

        now = time.monotonic()
        deadline = now - FEEDBACK_REACTION_COOLDOWN_SECONDS
        self._last_reaction_at = {
            key: stamp for key, stamp in self._last_reaction_at.items() if stamp > deadline
        }
        if channel_id in self._last_reaction_at:
            return False

        # 付与そのものは裏で走り、権限不足などで失敗しうる。それでもここで
        # 時刻を記録するのは意図どおり — 付けられないチャンネルで返信のたびに
        # 失敗する API 呼び出しを繰り返さずに済む。
        self._last_reaction_at[channel_id] = now
        return True

    async def _add_reactions(self, sent: discord.Message) -> None:
        """返信へ 👍 → 👎 の順にリアクションを付ける（失敗してもログのみ）。

        並列に投げると表示順が入れ替わるため、必ず順番に await する。
        リアクションを付けられなくてもチャット自体は成立しているので、
        ここでの失敗は成功扱いにする（フェイルオープン）。
        """
        try:
            await sent.add_reaction(FEEDBACK_REACTION_GOOD)
            await sent.add_reaction(FEEDBACK_REACTION_BAD)
        # Forbidden は HTTPException の派生クラスなので、必ず先に捕まえること
        # （順序を逆にすると権限エラーが一時障害として扱われ、原因がログから消える）。
        except discord.Forbidden:
            print(
                f"フィードバック: リアクションを付ける権限がありません"
                f"（チャンネルID: {sent.channel.id}）。「リアクションの追加」権限を確認してください。"
            )
        except discord.HTTPException as error:
            print(f"フィードバック: リアクションの付与に失敗しました: {error}")

    # ---- リアクションイベントの処理 --------------------------------------------

    async def handle_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """👍/👎 が押されたときに評価を記録する（on_raw_reaction_add から呼ぶ）。

        メッセージがキャッシュに載っていなくても発火する raw イベントを使う
        （on_reaction_add はキャッシュ上のメッセージにしか反応しないため、
        少し前の返信への評価を取りこぼす）。
        イベントハンドラから例外を漏らすと discord.py の既定 on_error が
        ログを吐くだけで原因が埋もれるので、全体を try で包む。
        """
        try:
            entry = self._resolve_vote_target(payload)
            if entry is None:
                return
            rating = _rating_of(payload.emoji)
            if rating is None:
                return
            # ギルドのリアクションでは member が来る（DM では None）。
            # 他のボットの自動リアクションはノイズなので数えない。
            member = payload.member
            if member is not None and member.bot:
                return

            await run_in_thread(
                self._db.feedback_record,
                message_id=int(payload.message_id),
                user_id=str(payload.user_id),
                guild_id=entry.guild_id,
                channel_id=entry.channel_id,
                target_user_id=entry.target_user_id,
                model_name=entry.model_name,
                prompt_file=entry.prompt_file,
                rating=rating,
                created_at=_utc_now_iso(),
            )
            # 古い記録の間引きはここでは行わない（prune_stored の docstring を参照）。
        except Exception as error:
            print(f"フィードバック: 評価の記録に失敗しました: {error}")

    async def prune_stored(self) -> None:
        """古いフィードバック記録を間引く（bot.py の定期ループから呼ぶ）。

        以前は評価が押されるたびに handle_reaction_add の中で実行していたが、
        feedback_prune は索引の無い created_at で全行を並べ替えるうえ、
        database.py のプロセス唯一のロックを握る。その間はチャットの主経路が
        使う記憶・親密度・統計の読み書きまで待たされ、返信そのものが遅くなっていた。

        保持件数は「多少超過してもよい」性質の上限なので、主経路から切り離して
        定期実行にする。失敗しても記録の追加には影響しないためログのみで続行する
        （次回の見回りで自然に再試行される）。
        """
        if not FEEDBACK_ENABLED:
            return
        try:
            await run_in_thread(self._db.feedback_prune, FEEDBACK_MAX_STORED_ROWS)
        except Exception as error:
            print(f"フィードバック: 古い記録の整理に失敗しました: {error}")

    async def handle_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        """👍/👎 が外されたときに記録を削除する（on_raw_reaction_remove から呼ぶ）。

        押し間違えを取り消せるようにするための処理。対象外の絵文字を外した
        だけで評価が消えないよう、ここでも絵文字を確認してから削除する。
        なお削除イベントには member が付かない（Discord が送らない）ため、
        ボット判定は payload.user_id の照合だけで行う。
        """
        try:
            if self._resolve_vote_target(payload) is None:
                return
            rating = _rating_of(payload.emoji)
            if rating is None:
                return

            # 外した絵文字に対応する評価だけを消す。👍👎 の両方を押している
            # ユーザーが片方だけ外したときに、残っている側の記録まで
            # 消えてしまわないようにするため（feedback_delete の注記を参照）。
            await run_in_thread(
                self._db.feedback_delete,
                int(payload.message_id),
                str(payload.user_id),
                rating,
            )
        except Exception as error:
            print(f"フィードバック: 評価の取り消しに失敗しました: {error}")

    def _resolve_vote_target(
        self, payload: discord.RawReactionActionEvent
    ) -> _TrackedReply | None:
        """そのリアクションが「数えてよいボット返信」宛かを判定して情報を返す。

        該当しない場合は None を返す。**レジストリに無いメッセージはログも
        出さずに黙って無視する** — チャンネル内の他人のメッセージへの
        リアクションでも毎回イベントが飛ぶため、ログを出すと溢れてしまう。
        """
        if not FEEDBACK_ENABLED:
            return None
        bot_user = self._client.user
        if bot_user is None:
            # 起動直後などログイン前は自分のIDが分からず、自己投票を弾けない。
            return None
        if payload.user_id == bot_user.id:
            # ボット自身が付けた初期リアクション。数えると起動直後から自己投票で汚れる。
            return None
        return self._tracked.get(int(payload.message_id))

    # ---- 集計（/functions → 📈 会話統計 → 👍 フィードバック） -------------------

    async def build_summary_text(self, guild_id: int) -> str:
        """指定サーバーのフィードバック集計を、そのまま表示できる文章にする。

        個人が特定されると評価しづらくなるため、**「誰が👎したか」は出さない**
        （集計値だけを返す）。閲覧は管理者専用・エフェメラルを前提とする。
        """
        try:
            rows = await run_in_thread(self._db.feedback_summary, int(guild_id))
        except Exception as error:
            print(f"フィードバック: 集計の取得に失敗しました: {error}")
            return "フィードバックの集計に失敗しました。"

        if not rows:
            return "まだフィードバックがありません。"

        total_good = sum(row["good"] for row in rows)
        total_bad = sum(row["bad"] for row in rows)
        lines = [
            "👍 **フィードバック集計**（このサーバー分）",
            f"全体: 👍 {total_good} / 👎 {total_bad}"
            f"（満足度 {_format_satisfaction(total_good, total_bad)}）",
            "",
            "**モデル別・ペルソナ別の内訳**",
        ]
        for row in rows[:FEEDBACK_SUMMARY_MAX_ROWS]:
            # 表示名は choices.py を唯一の情報源にする（未登録の値は素のまま出す）。
            model = MODEL_DISPLAY_NAMES.get(row["model_name"], row["model_name"]) or "（不明）"
            persona = PERSONA_DISPLAY_NAMES.get(row["prompt_file"], row["prompt_file"]) or "（不明）"
            lines.append(
                f"・{model} / {persona} … 👍 {row['good']} / 👎 {row['bad']}"
                f"（満足度 {_format_satisfaction(row['good'], row['bad'])}）"
            )
        hidden = len(rows) - FEEDBACK_SUMMARY_MAX_ROWS
        if hidden > 0:
            lines.append(f"…ほか {hidden} 通りの組み合わせは省略しました。")
        lines.append("※ 誰がどちらを押したかは表示しません（評価しづらくならないようにするため）。")
        return "\n".join(lines)


# ---- 既定インスタンスの受け渡し ------------------------------------------------
# 依存関係は bot.py が手作業で注入するのが本リポジトリの流儀だが、
# StatsView（フィードバック集計ボタンの置き場）は機能ハブ（functions_view.py）が
# 生成しており、そこへ引数を足すとハブのコンストラクタ・clone()・register_commands
# まで芋づるで書き換えることになる。フィードバックは付随機能なので、その連鎖を
# 避けて「bot.py が起動時に1回だけ登録した既定インスタンスを UI 層が引く」形にする。
# 単一プロセス前提（ボットは1プロセスで1つの FeedbackService しか持たない）。
_default_service: "FeedbackService | None" = None


def set_default_service(service: "FeedbackService | None") -> None:
    """既定の FeedbackService を登録する（bot.py が起動時に1回だけ呼ぶ）。"""
    global _default_service
    _default_service = service


def get_default_service() -> "FeedbackService | None":
    """登録済みの既定 FeedbackService を返す（未登録なら None）。"""
    return _default_service
