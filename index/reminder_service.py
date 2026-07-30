"""リマインダーサービス。

「30分後にゴミ出しって知らせて」のような依頼を会話から受け取り、指定時刻に
依頼されたチャンネルへ通知する。

日時の解釈（相対表現→絶対日時）は content_moderator の統合判定が
モデレーションと同じ1回の Gemini 呼び出しの中で済ませており、このサービスは
その結果の検証・保存・通知だけを担う。通知文は Gemini を呼ばずテンプレートで
組み立てる — リマインダーは文面の魅力より「正確に届くこと」が重要なため。
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import discord

from constants import (
    REMINDER_MAX_DAYS_AHEAD,
    REMINDER_MAX_FINISHED,
    REMINDER_MAX_MESSAGE_LENGTH,
    REMINDER_MAX_PENDING_PER_USER,
    REMINDER_STALE_HOURS,
    REMINDER_TIMEZONE,
)
from database import Database
from discord_helpers import split_message
from ng_word_service import NgWordFilter
from timeutil import resolve_timezone

# LLM が返す日時のゆらぎを吸収するため、複数の形式を順に試す。
_AT_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y/%m/%d %H:%M",
)

_WEEKDAY_NAMES = ("月", "火", "水", "木", "金", "土", "日")


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
    return (
        f"{local.year}年{local.month}月{local.day}日"
        f"（{_WEEKDAY_NAMES[local.weekday()]}）{local:%H:%M}"
    )


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
            )
        except Exception as error:
            print(f"リマインダー: 登録に失敗しました: {error}")
            return "⏰ リマインダーの登録に失敗しました。もう一度お試しください。"

        when = _format_local(remind_at.isoformat(), self._tz)
        return f"⏰ **{when}** に「{body}」をお知らせします。"

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
        """リマインダー1件を「日時 — 本文」の1行表記にする（一覧表示用）。"""
        return f"{_format_local(record.get('remind_at', ''), self._tz)} — {record.get('message', '')}"

    # ---- 通知（バックグラウンドの見回りから呼ばれる） --------------------------

    async def run_due_checks(self) -> None:
        """期限が来たリマインダーを通知する。REMINDER_CHECK_SECONDS ごとに呼ばれる。"""
        now = _utc_now()
        try:
            due = await asyncio.to_thread(self._db.reminder_claim_due, now.isoformat())
        except Exception as error:
            print(f"リマインダー: 期限の確認に失敗しました: {error}")
            return

        if not due:
            return

        # 停止中に溜まった古い通知は送らない（起動直後に何件も流れるのを防ぐ）。
        # claim 済みなので、送らなくても再通知はされない。
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
        for chunk in split_message(body):
            await channel.send(chunk)
