import re
from datetime import datetime, timedelta, timezone
from typing import Any

import discord

from app_state import BotState
from constants import AUTO_TOPIC_HISTORY_LIMIT, AUTO_TOPIC_TIMEOUT_SECONDS
from content_moderator import ContentModerator
from discord_helpers import fetch_channel_history_text, split_message
from gemini_service import GeminiService
from memory_service import MemoryService
from ng_word_service import NgWordFilter
from paths import load_prompt_template, resolve_prompt_path
from prompting import build_auto_topic_prompt, build_memory_sections
from usage_graph import UsageTracker

# 感情タグ [V:.., A:..] を除去するための正規表現（誰への返信でもないため付いたら除去する）
_AFFINITY_TAG_PATTERN = r'\[V:\s*[+-]?\d+\s*,\s*A:\s*[+-]?\d+\s*\]'


class AutoTopicService:
    """自発的な会話（無会話チャンネルへの話題提供）を管理するサービス。

    ボットが会話に参加したチャンネルを監視対象として登録し、設定した時間
    会話が無ければ、過去の話題をもとに Gemini で話しかけを生成して投稿する。
    一度話題提供したら、その後に人間の発言があるまでは再投稿しない
    （誰も反応しないまま同じチャンネルへ延々と投稿し続けるのを防ぐ）。
    監視状態はインメモリのみで、再起動でリセットされる。
    """

    def __init__(
        self,
        client: discord.Client,
        state: BotState,
        memory: MemoryService,
        gemini: GeminiService,
        moderator: ContentModerator,
        ng_filter: NgWordFilter,
        usage: UsageTracker,
    ) -> None:
        self._client = client
        self._state = state
        self._memory = memory
        self._gemini = gemini
        self._moderator = moderator
        self._ng_filter = ng_filter
        self._usage = usage

    def watch_channel(self, channel_id: int) -> None:
        """チャンネルを監視対象に登録し、最終活動時刻を今に更新する。

        ボットが会話に参加した（＝話しかけられた）チャンネルだけを対象にする。
        """
        self._state.auto_topic_channels[channel_id] = datetime.now(timezone.utc)

    def mark_activity(self, channel_id: int) -> None:
        """監視対象チャンネルの最終活動時刻を更新する。未登録なら何もしない。

        ボット宛てかどうかに関係なく「チャンネルで会話が行われている」ことを
        記録するため、on_message の早い段階（メンション判定より前）から呼ばれる。
        """
        if channel_id in self._state.auto_topic_channels:
            self._state.auto_topic_channels[channel_id] = datetime.now(timezone.utc)

    async def run_pending_checks(self) -> None:
        """全監視チャンネルを見回り、無会話しきい値を超えたものへ話題提供する。"""
        if not self._state.auto_topic_enabled:
            return

        now = datetime.now(timezone.utc)
        interval = timedelta(hours=self._state.auto_topic_interval_hours)

        # ループ中に辞書を更新する可能性があるためスナップショットを走査する
        for channel_id, last_activity in list(self._state.auto_topic_channels.items()):
            if now - last_activity < interval:
                continue

            # 前回の話題提供より後に人間の発言が無ければ見送る（沈黙中の連投防止）
            posted_at = self._state.auto_topic_posted_at.get(channel_id)
            if posted_at is not None and posted_at >= last_activity:
                continue

            channel = self._client.get_channel(channel_id)
            if channel is None:
                # チャンネルが削除された・アクセスできなくなった場合は監視から外す
                print(f"自発的な会話: チャンネル {channel_id} が見つからないため監視を解除します。")
                self._state.auto_topic_channels.pop(channel_id, None)
                self._state.auto_topic_posted_at.pop(channel_id, None)
                continue

            try:
                done = await self._post_topic(channel)
            except Exception as error:
                print(f"自発的な会話: 話題提供エラー (channel={channel_id}): {error}")
                continue

            if done:
                # 完了時のみ記録する。一時的な失敗（False）は次回の見回りで自然にリトライされる。
                self._state.auto_topic_posted_at[channel_id] = datetime.now(timezone.utc)

    async def _post_topic(self, channel: Any) -> bool:
        """過去の話題をもとに話しかけを生成し、チャンネルへ投稿する。

        戻り値は「この沈黙期間の処理を完了したか」。True なら次に人間の発言が
        あるまで再実行されない（投稿できた場合と、モデレーション見送りのように
        リトライしても同じ結果になる場合）。False は一時的な失敗で、次回の
        見回りでリトライされる。
        """
        try:
            persona_prompt = load_prompt_template(resolve_prompt_path(self._state.current_prompt_file))
        except FileNotFoundError:
            print(f"自発的な会話: プロンプトファイル({self._state.current_prompt_file})が見つかりません。")
            return False

        history_text = await fetch_channel_history_text(
            channel,
            self._state.channel_reset_points.get(channel.id),
            self._client.user,
            AUTO_TOPIC_HISTORY_LIMIT,
        )
        # 話題の材料はチャンネル履歴＋プロフィール記憶（ユーザーの興味・近況）。
        # 質問が無いためルーターは使わず、profile を固定で読み込む。
        memory_context = self._memory.build_context("profile")
        full_prompt = build_auto_topic_prompt(
            persona_prompt,
            build_memory_sections("profile", memory_context),
            history_text,
        )

        # 話題提供に Google 検索は不要なためグラウンディングは無効で呼び出す
        response_text, usage_info, _sources = await self._gemini.generate(
            full_prompt,
            images=None,
            timeout=AUTO_TOPIC_TIMEOUT_SECONDS,
            grounding=False,
        )

        if not response_text:
            print("自発的な会話: 話題の生成に失敗しました。次回の見回りで再試行します。")
            return False

        # 感情タグが混じった場合の保険（誰への返信でもないため親密度は更新しない）
        response_text = re.sub(_AFFINITY_TAG_PATTERN, "", response_text).strip()
        if not response_text:
            print("自発的な会話: 生成結果が空でした。")
            return False

        # NG ワードは既存パイプラインと同様に ● でマスクする
        if self._ng_filter.contains_ng_word(response_text):
            detected = self._ng_filter.find_ng_words(response_text)
            print(f"NGワード検出 (自発的な会話): {detected}")
            response_text = self._ng_filter.mask_ng_words(response_text)

        # 不適切な話題は、チャット応答と違い謝罪文へ差し替えて投稿する意味が無い
        # （誰も話していないのに唐突な謝罪になる）ため、投稿自体を見送る。
        # 同じ材料からの再生成はほぼ同じ結果になるため、リトライせず完了扱いにする。
        is_safe, reason = await self._moderator.check(response_text, direction="Bot応答")
        if not is_safe:
            print(f"自発的な会話: モデレーションにより投稿を見送りました ({reason})")
            return True

        channel_label = getattr(channel, "name", None) or f"channel {channel.id}"
        self._usage.log_snapshot(
            "自発的な会話", f"{channel_label} への話題提供", "自動実行", full_prompt, response_text, usage_info
        )

        for chunk in split_message(response_text):
            await channel.send(chunk)
        return True
