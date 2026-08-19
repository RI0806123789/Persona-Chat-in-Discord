"""リマインダーサービス。

「30分後にゴミ出しって知らせて」のような依頼を会話から受け取り、指定時刻に
依頼されたチャンネルへ通知する。

日時の解釈（相対表現→絶対日時）は content_moderator の統合判定が
モデレーションと同じ1回の Gemini 呼び出しの中で済ませており、このサービスは
その結果の検証・保存・通知だけを担う。通知文は Gemini を呼ばずテンプレートで
組み立てる — リマインダーは文面の魅力より「正確に届くこと」が重要なため。

繰り返し（毎日/毎週/毎月）にも対応する。繰り返しは新しい行を作らず、
**同じ行の remind_at を次回時刻へ進めて pending のまま残す**方式にしている
（reminder_id が変わらないので、パネルからの取り消しが直感どおりに効く）。
次回時刻の計算は next_occurrence()（暦だけを扱う純粋関数）に切り出してあり、
DB のクレームと同一トランザクションの中から呼ばれる。
"""

import asyncio
import calendar
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import discord

from constants import (
    REMINDER_MAX_DAYS_AHEAD,
    REMINDER_MAX_FINISHED,
    REMINDER_MAX_MESSAGE_LENGTH,
    REMINDER_MAX_PENDING_PER_USER,
    REMINDER_RECURRENCE_LABELS,
    REMINDER_RECURRENCE_MAX_CATCHUP_STEPS,
    REMINDER_RECURRENCE_VALUES,
    REMINDER_STALE_HOURS,
    REMINDER_TIMEZONE,
)
from database import Database
from discord_helpers import split_message
from ng_word_service import NgWordFilter
from timeutil import format_weekday, resolve_timezone

# LLM が返す日時のゆらぎを吸収するため、複数の形式を順に試す。
_AT_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y/%m/%d %H:%M",
)


def _utc_now() -> datetime:
    """秒精度の現在時刻（UTC）。保存する時刻はすべてこの精度に揃える。

    remind_at の期限判定は ISO 8601 文字列の比較で行うため、桁数が混ざらないよう
    マイクロ秒を落として形式を統一する。
    """
    return datetime.now(timezone.utc).replace(microsecond=0)


def _format_local(iso_utc: str, tz: Any) -> str:
    """保存されている UTC の ISO 8601 文字列を、表示用のローカル日時表記に直す。"""
    try:
        dt = datetime.fromisoformat(iso_utc)
    except ValueError:
        return iso_utc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(tz)
    # strftime の %-m / %#m はプラットフォーム依存のため、数値は自前で埋め込む。
    # 曜日名は timeutil の唯一のテーブルから取る（登録時にプロンプトへ埋める
    # 曜日表記と同じ情報源にして、表記が食い違わないようにするため）。
    return (
        f"{local.year}年{local.month}月{local.day}日"
        f"（{format_weekday(local)}）{local:%H:%M}"
    )


def normalize_recurrence(value: Any) -> str:
    """繰り返し種別を検証して正規化する。未知の値・欠落は none（単発）に倒す。

    LLM の抽出結果と DB の値の両方を同じ関数で通し、許容値の唯一の情報源を
    REMINDER_RECURRENCE_VALUES に保つ（安全側＝従来どおりの単発へフォールバック）。
    """
    recurrence = str(value or "none").strip().lower()
    return recurrence if recurrence in REMINDER_RECURRENCE_VALUES else "none"


def recurrence_label(value: Any) -> str:
    """繰り返し種別の日本語表示名を返す（単発・未知は空文字）。"""
    return REMINDER_RECURRENCE_LABELS.get(normalize_recurrence(value), "")


# ---- 次回時刻の計算（暦だけを扱う純粋関数。DB もネットワークも触らない） --------


def _to_local_naive(iso_utc: str, tz: Any) -> datetime | None:
    """UTC の ISO 8601 文字列を、tz における「素朴な（tzinfo 無しの）ローカル日時」にする。

    日付の加算をローカルの壁時計で行うため、いったん tzinfo を外す。UTC のまま
    timedelta を足すと、DST のあるタイムゾーンへ設定を変えた瞬間に時刻がずれる。
    """
    try:
        dt = datetime.fromisoformat(str(iso_utc))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).replace(tzinfo=None)


def _from_local_naive(local_naive: datetime, tz: Any) -> str:
    """ローカルの素朴な日時を、保存用の UTC・秒精度 ISO 8601 文字列へ戻す。

    期限判定は ISO 文字列の比較で行うため、_utc_now() と同じくマイクロ秒を落として
    桁数を揃える（桁数が混ざると比較が壊れる）。
    """
    utc = local_naive.replace(tzinfo=tz).astimezone(timezone.utc).replace(microsecond=0)
    return utc.isoformat()


def _monthly_occurrence(anchor_local: datetime, months: int) -> datetime:
    """アンカーから months か月後の予定日時（ローカル）を返す。

    対象日が存在しない月（例: 1/31 の翌月）はその月の末日へ丸める。
    **アンカーは常に初回時刻のまま**で、丸めた日付から次を計算しないため、
    1/31 → 2/28 → 3/31 と元の日付へ戻る（timedelta(days=30) では実現できない）。
    """
    total = (anchor_local.year * 12 + (anchor_local.month - 1)) + months
    year, month = divmod(total, 12)
    month += 1
    # calendar.monthrange(年, 月)[1] はその月の日数（閏年の2月は29）。
    day = min(anchor_local.day, calendar.monthrange(year, month)[1])
    return anchor_local.replace(year=year, month=month, day=day)


def _month_distance(anchor_local: datetime, current_local: datetime) -> int:
    """アンカーから見て current が何か月後かを返す（日付の丸めの影響を受けない）。"""
    return (current_local.year - anchor_local.year) * 12 + (current_local.month - anchor_local.month)


def next_occurrence(
    *,
    remind_at: str,
    recurrence: str,
    anchor: str,
    after: datetime,
    tz: Any,
    max_steps: int = REMINDER_RECURRENCE_MAX_CATCHUP_STEPS,
) -> str | None:
    """繰り返しリマインダーの次回通知時刻（UTC・秒精度の ISO 8601）を返す。

    ボット停止などで複数回分を過ぎていた場合は、**after より後になるまで繰り返し
    進める**（1回分だけ進めると、追い付くまで見回りのたびに通知が飛んでしまう）。
    過ぎた回をまとめて通知することはしない — 通知は最大1回、時刻は未来へ。

    Args:
        remind_at: 今回通知した（＝進める前の）予定時刻。UTC の ISO 8601。
        recurrence: 'daily' / 'weekly' / 'monthly'。'none' や未知の値は None を返す。
        anchor: 初回の予定時刻（UTC の ISO 8601）。毎月の日付を元へ戻すために使う。
                空なら remind_at をアンカーとみなす（移行前の行などへの保険）。
        after: この日時より後になるまで進める（通常は現在時刻。tz 付きでも無しでもよい）。
        tz: 日付の加算を行う基準タイムゾーン（REMINDER_TIMEZONE を解決したもの）。
        max_steps: 追い付きの最大反復回数。到達したら None を返して系列を止める。

    Returns:
        次回の通知時刻。繰り返さない場合・計算できない場合は None。
    """
    kind = normalize_recurrence(recurrence)
    if kind == "none":
        return None

    current_local = _to_local_naive(remind_at, tz)
    if current_local is None:
        print(f"リマインダー: 繰り返しの基準日時を読めませんでした: {remind_at!r}")
        return None

    # アンカーが読めない行は今回の予定時刻を基準にする（繰り返しを止めない）。
    anchor_local = _to_local_naive(anchor, tz) or current_local

    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)
    after_local = after.astimezone(tz).replace(tzinfo=None)

    if kind == "monthly":
        # 反復の起点はアンカーではなく「今回の予定時刻の月」にする。
        # アンカーから数え直すと、何年も続いた繰り返しで反復回数が上限に届いてしまう。
        months = _month_distance(anchor_local, current_local)
        for _ in range(max_steps):
            months += 1
            candidate = _monthly_occurrence(anchor_local, months)
            if candidate > after_local:
                return _from_local_naive(candidate, tz)
    else:
        delta = timedelta(days=1) if kind == "daily" else timedelta(days=7)
        candidate = current_local
        for _ in range(max_steps):
            candidate += delta
            if candidate > after_local:
                return _from_local_naive(candidate, tz)

    # ここに来るのは「進めても未来に届かない」＝計算が前進していない不具合のとき。
    # 無限ループになるより、系列を止めて気づけるようにする。
    print(
        f"リマインダー: 次回時刻の計算が {max_steps} 回で追い付きませんでした"
        f" (種別={kind}, 予定={remind_at})"
    )
    return None


class ReminderService:
    """リマインダーの登録・一覧・取り消しと、期限到来分の通知を行うサービス。

    保存先は DB の reminders テーブル（再起動しても消えない）。通知先は
    「依頼されたチャンネル」で、そこに依頼者をメンションして投稿する。
    """

    def __init__(self, client: discord.Client, db: Database, ng_filter: NgWordFilter) -> None:
        self._client = client
        self._db = db
        self._ng_filter = ng_filter
        self._tz = resolve_timezone(REMINDER_TIMEZONE, purpose="リマインダーの表示・日時解釈")

    # ---- 登録 -----------------------------------------------------------------

    async def create_from_request(
        self, message: discord.Message, request: dict[str, str]
    ) -> str | None:
        """会話から抽出されたリマインダー依頼を登録し、ユーザーへ返す確認文を返す。

        request は {"at": 日時, "message": 本文, "repeat": 繰り返し種別} の形
        （すべて文字列。"repeat" は欠けていてもよく、その場合は単発として扱う）。
        繰り返しであっても検証は**初回の日時**に対して行う（過去・遠すぎる日時の
        拒否も、本文の切り詰めも、単発とまったく同じ扱い）。

        Returns:
            登録できた場合は確認メッセージ、上限に達している場合はその案内。
            日時が解釈できない・過去・遠すぎるなど「リマインダーとして扱えない」
            場合は None を返す — 呼び出し側は通常のチャット応答へ流すこと
            （依頼ではない発言を LLM が誤って拾った可能性があるため、
            エラーを返すより普通に会話を続ける方が自然）。
        """
        remind_at = self._parse_at(request.get("at", ""))
        if remind_at is None:
            return None

        now = _utc_now()
        if remind_at <= now:
            print(f"リマインダー: 過去の日時のため登録しません ({request.get('at')})")
            return None
        if remind_at > now + timedelta(days=REMINDER_MAX_DAYS_AHEAD):
            print(f"リマインダー: 遠すぎる日時のため登録しません ({request.get('at')})")
            return None

        body = request.get("message", "").strip()
        if not body:
            return None
        if len(body) > REMINDER_MAX_MESSAGE_LENGTH:
            body = body[:REMINDER_MAX_MESSAGE_LENGTH] + "…"

        user_id = str(message.author.id)
        try:
            pending_count = await asyncio.to_thread(self._db.reminder_count_pending, user_id)
        except Exception as error:
            print(f"リマインダー: 登録件数の確認に失敗しました: {error}")
            return None

        if pending_count >= REMINDER_MAX_PENDING_PER_USER:
            return (
                f"⏰ リマインダーは1人あたり {REMINDER_MAX_PENDING_PER_USER} 件までです。"
                "`/functions` → ⏰ リマインダー から不要なものを取り消してください。"
            )

        # 抽出できない・未知の繰り返し種別は none（単発）に倒す。
        recurrence = normalize_recurrence(request.get("repeat"))

        guild_id = message.guild.id if message.guild is not None else 0
        try:
            await asyncio.to_thread(
                self._db.reminder_add,
                user_id=user_id,
                user_name=message.author.display_name,
                guild_id=guild_id,
                channel_id=message.channel.id,
                message=body,
                remind_at=remind_at.isoformat(),
                created_at=now.isoformat(),
                recurrence=recurrence,
                # アンカーは初回時刻で固定する（毎月の日付を元へ戻すための基準）。
                # 単発は使わないので空文字にしておく。
                recurrence_anchor=remind_at.isoformat() if recurrence != "none" else "",
            )
        except Exception as error:
            print(f"リマインダー: 登録に失敗しました: {error}")
            return "⏰ リマインダーの登録に失敗しました。もう一度お試しください。"

        when = _format_local(remind_at.isoformat(), self._tz)
        label = recurrence_label(recurrence)
        repeat_note = f"（以後 {label} くり返します）" if label else ""
        return f"⏰ **{when}** に「{body}」をお知らせします。{repeat_note}"

    def _parse_at(self, at_text: str) -> datetime | None:
        """LLM が返した日時文字列を UTC の datetime に変換する。解釈できなければ None。

        文字列にタイムゾーンが無い場合は REMINDER_TIMEZONE のローカル時刻とみなす
        （プロンプトでその基準の現在日時を渡しているため）。
        """
        text = (at_text or "").strip()
        if not text:
            return None

        for fmt in _AT_FORMATS:
            try:
                naive = datetime.strptime(text, fmt)
            except ValueError:
                continue
            return naive.replace(tzinfo=self._tz).astimezone(timezone.utc).replace(microsecond=0)

        # 上の形式で読めない場合、タイムゾーン付き ISO 8601 の可能性を試す。
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            print(f"リマインダー: 日時を解釈できませんでした: {text!r}")
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=self._tz)
        return parsed.astimezone(timezone.utc).replace(microsecond=0)

    # ---- 一覧・取り消し（/functions の ⏰ リマインダーパネルから使う） ----------

    async def list_pending(self, user_id: str) -> list[dict[str, Any]]:
        """指定ユーザーの未通知リマインダーを、通知が近い順で返す。"""
        try:
            return await asyncio.to_thread(self._db.reminder_list_pending, str(user_id))
        except Exception as error:
            print(f"リマインダー: 一覧の取得に失敗しました: {error}")
            return []

    async def cancel(self, reminder_id: int, user_id: str) -> dict[str, Any] | None:
        """リマインダーを取り消す。取り消せた場合はその内容を返す（他人のものは不可）。"""
        try:
            return await asyncio.to_thread(self._db.reminder_cancel, int(reminder_id), str(user_id))
        except Exception as error:
            print(f"リマインダー: 取り消しに失敗しました: {error}")
            return None

    def describe(self, record: dict[str, Any]) -> str:
        """リマインダー1件を「日時 — 本文（繰り返し）」の1行表記にする（一覧表示用）。"""
        label = recurrence_label(record.get("recurrence"))
        suffix = f"（{label}）" if label else ""
        return (
            f"{_format_local(record.get('remind_at', ''), self._tz)}"
            f" — {record.get('message', '')}{suffix}"
        )

    # ---- 通知（バックグラウンドの見回りから呼ばれる） --------------------------

    def _build_next_at(self, now: datetime) -> Callable[[dict[str, Any]], str | None]:
        """reminder_claim_due へ渡す「次回時刻を返す関数」を作る。

        **必ず同期関数を返すこと。** この関数は DB のクレームと同一トランザクション
        （＝ Database の threading.Lock を保持したまま）呼ばれるため、
        async def にすると await されずコルーチンが返って壊れる。
        同じ理由で、**中から Database のメソッドを呼んではいけない**
        （再入不可のロックを同じスレッドから取り直してデッドロックする）。
        next_occurrence は暦の計算だけを行う純粋関数なのでこの制約を満たす。
        """

        def next_at(record: dict[str, Any]) -> str | None:
            return next_occurrence(
                remind_at=str(record.get("remind_at", "")),
                recurrence=str(record.get("recurrence", "none") or "none"),
                anchor=str(record.get("recurrence_anchor", "") or ""),
                after=now,
                tz=self._tz,
            )

        return next_at

    async def run_due_checks(self) -> None:
        """期限が来たリマインダーを通知する。REMINDER_CHECK_SECONDS ごとに呼ばれる。"""
        now = _utc_now()
        try:
            due = await asyncio.to_thread(
                self._db.reminder_claim_due, now.isoformat(), self._build_next_at(now)
            )
        except Exception as error:
            print(f"リマインダー: 期限の確認に失敗しました: {error}")
            return

        if not due:
            return

        # 停止中に溜まった古い通知は送らない（起動直後に何件も流れるのを防ぐ）。
        # claim 済みなので、送らなくても再通知はされない。
        # 繰り返しの場合も次回時刻はクレーム時に進み終えているため、ここで今回分を
        # 見送っても系列は途切れない（＝長期停止明けは翌回の定刻から自然に再開する）。
        # 判定に使う record["remind_at"] は「進める前の予定時刻」であることが前提。
        stale_before = now - timedelta(hours=REMINDER_STALE_HOURS)

        for record in due:
            if self._is_stale(record, stale_before):
                print(
                    f"リマインダー: 予定時刻を大きく過ぎているため通知を見送りました"
                    f" (id={record.get('reminder_id')}, 予定={record.get('remind_at')})"
                )
                continue
            try:
                await self._notify(record)
            except Exception as error:
                print(f"リマインダー: 通知エラー (id={record.get('reminder_id')}): {error}")

        # 通知済みレコードが際限なく溜まらないよう、古いものから捨てる。
        try:
            await asyncio.to_thread(self._db.reminder_prune_finished, REMINDER_MAX_FINISHED)
        except Exception as error:
            print(f"リマインダー: 古い記録の掃除に失敗しました: {error}")

    @staticmethod
    def _is_stale(record: dict[str, Any], stale_before: datetime) -> bool:
        """通知予定が古すぎる（stale_before より前）かどうかを返す。

        日時が読めない壊れたレコードは古い扱いにしない（通知して気づけるようにする）。
        """
        try:
            remind_at = datetime.fromisoformat(str(record.get("remind_at", "")))
        except ValueError:
            return False
        if remind_at.tzinfo is None:
            remind_at = remind_at.replace(tzinfo=timezone.utc)
        return remind_at < stale_before

    async def _notify(self, record: dict[str, Any]) -> None:
        """1件分の通知を、依頼されたチャンネルへ投稿する。"""
        channel_id = int(record["channel_id"])
        channel = self._client.get_channel(channel_id)
        if channel is None:
            # 起動直後などキャッシュに無い場合があるため、API からの取得も試す。
            try:
                channel = await self._client.fetch_channel(channel_id)
            except Exception as error:
                print(f"リマインダー: チャンネル({channel_id})を取得できませんでした: {error}")
                return

        # カテゴリなど投稿できない種類のチャンネルが返ることがあるため確認する
        if not hasattr(channel, "send"):
            print(f"リマインダー: チャンネル({channel_id})へは投稿できません。")
            return

        text = str(record.get("message", ""))
        # 本文はユーザーの発言そのものなので、投稿前に NG ワードをマスクする
        # （登録時に AI モデレーションは通過済みのため、ここでは再判定しない）。
        if self._ng_filter.contains_ng_word(text):
            detected = self._ng_filter.find_ng_words(text)
            print(f"NGワード検出 (リマインダー): {detected}")
            text = self._ng_filter.mask_ng_words(text)

        body = f"⏰ <@{record['user_id']}> リマインダーです: **{text}**"
        # 繰り返しはクレーム時に次回時刻まで進んでいるので、その予定を添えて
        # 「まだ続くこと」と「止めたいときの目安」を伝える（単発には付けない）。
        next_at = record.get("next_remind_at")
        if next_at:
            body += f"\n🔁 次回は {_format_local(str(next_at), self._tz)} にお知らせします。"
        for chunk in split_message(body):
            await channel.send(chunk)
