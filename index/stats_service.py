import asyncio
import io
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from constants import (
    STATS_ANALYSIS_TIMEOUT_SECONDS,
    STATS_ANALYSIS_TTL_HOURS,
    STATS_MAX_USERS_IN_SELECT,
    STATS_PERSONALITY_MIN_SAMPLES,
    STATS_RECENT_SAMPLES_MAX,
    STATS_TIMEZONE,
    STATS_TOPIC_COUNT,
)
from database import Database
from storage import extract_json_object

# matplotlib の日本語フォント指定（usage_graph.py と同じ作法）
_FONT_CONTEXT = {"font.family": ["Yu Gothic", "Meiryo", "sans-serif"]}

# ビッグファイブの軸（英語キー → 日本語表示名）。レーダーチャートの軸ラベルに使う。
BIG_FIVE_LABELS = {
    "openness": "開放性",
    "conscientiousness": "誠実性",
    "extraversion": "外向性",
    "agreeableness": "協調性",
    "neuroticism": "神経症傾向",
}


class StatsService:
    """ユーザー別の会話統計（発言回数・時間帯・話題・性格推定）を集計・可視化する。

    集計対象は「ボットへの発言のみ」。データは SQLite（memory/bot.db の
    user_stats テーブル）に永続化する。保存は asyncio.Lock で直列化する
    （単一プロセス前提）。
    """

    def __init__(self, db: Database, gemini: Any) -> None:
        self._db = db
        self.gemini = gemini
        # { user_id: { user_name, total_count, hourly[24], first_seen, last_seen,
        #              recent_samples[], analysis{ topics, big_five, personality, computed_at } } }
        try:
            self.cache: dict[str, dict] = db.stats_load_all()
        except Exception as error:
            print(f"会話統計の読み込みに失敗: {error}")
            self.cache = {}
        self._save_lock = asyncio.Lock()
        try:
            self._tz = ZoneInfo(STATS_TIMEZONE)
        except Exception as error:
            # タイムゾーンデータが無い環境では UTC にフォールバックする
            print(f"タイムゾーン({STATS_TIMEZONE})の読み込みに失敗、UTCで集計します: {error}")
            self._tz = timezone.utc

    # ---- 記録 -------------------------------------------------------------

    @staticmethod
    def _new_entry(user_name: str) -> dict:
        now_iso = datetime.now(timezone.utc).isoformat()
        return {
            "user_name": user_name,
            "total_count": 0,
            "hourly": [0] * 24,
            "first_seen": now_iso,
            "last_seen": now_iso,
            "recent_samples": [],
            "analysis": None,
        }

    def _to_local_hour(self, when: datetime) -> int:
        """発言時刻（UTC aware 想定）を集計用タイムゾーンの「時」に変換する。"""
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when.astimezone(self._tz).hour

    async def record(self, user_id: str, user_name: str, text: str, when: datetime) -> None:
        """1件の発言を集計に反映する。失敗してもチャット本体は止めない（フェイルオープン）。"""
        try:
            entry = self.cache.get(user_id)
            if entry is None:
                entry = self._new_entry(user_name)
                self.cache[user_id] = entry

            entry["user_name"] = user_name  # 表示名は最新に更新
            entry["total_count"] = int(entry.get("total_count", 0)) + 1

            hourly = entry.get("hourly")
            if not isinstance(hourly, list) or len(hourly) != 24:
                hourly = [0] * 24
                entry["hourly"] = hourly
            hourly[self._to_local_hour(when)] += 1

            entry["last_seen"] = datetime.now(timezone.utc).isoformat()

            text = (text or "").strip()
            if text:
                samples = entry.get("recent_samples")
                if not isinstance(samples, list):
                    samples = []
                    entry["recent_samples"] = samples
                samples.append(text)
                # 上限を超えたぶんは古い発言から捨てる
                if len(samples) > STATS_RECENT_SAMPLES_MAX:
                    del samples[: len(samples) - STATS_RECENT_SAMPLES_MAX]

            await self.save_background()
        except Exception as error:
            print(f"会話統計の記録に失敗: {error}")

    async def save_background(self) -> None:
        # 同一テーブルへの並行書き込みを避けるため、保存はロックで直列化する。
        async with self._save_lock:
            cache_copy = {user_id: data.copy() for user_id, data in self.cache.items()}
            try:
                await asyncio.to_thread(self._db.stats_save_all, cache_copy)
            except Exception as error:
                print(f"会話統計の保存に失敗: {error}")

    # ---- 参照ヘルパー -----------------------------------------------------

    def has_data(self) -> bool:
        return any(int(d.get("total_count", 0)) > 0 for d in self.cache.values())

    def display_name(self, user_id: str) -> str:
        entry = self.cache.get(user_id)
        if entry and entry.get("user_name"):
            return entry["user_name"]
        return str(user_id)

    def get_user_options(self) -> list[tuple[str, str, int]]:
        """管理者向けユーザー選択メニュー用に (user_id, 表示名, 発言回数) を回数の多い順で返す。"""
        items = [
            (uid, data.get("user_name") or uid, int(data.get("total_count", 0)))
            for uid, data in self.cache.items()
            if int(data.get("total_count", 0)) > 0
        ]
        items.sort(key=lambda t: t[2], reverse=True)
        return items[:STATS_MAX_USERS_IN_SELECT]

    # ---- 話題・性格推定の LLM 解析 ---------------------------------------

    def _is_fresh(self, computed_at: str | None) -> bool:
        """解析キャッシュが TTL 内かどうかを判定する。"""
        if not computed_at:
            return False
        try:
            when = datetime.fromisoformat(computed_at)
        except ValueError:
            return False
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        elapsed_hours = (datetime.now(timezone.utc) - when).total_seconds() / 3600.0
        return elapsed_hours < STATS_ANALYSIS_TTL_HOURS

    def _build_analysis_prompt(self, samples: list[str]) -> str:
        joined = "\n".join(f"- {s}" for s in samples)
        return f"""以下は、あるユーザーが Discord でボットに送った発言の一覧です。
この発言だけを根拠に、(1) よく話す話題 と (2) ビッグファイブ性格特性の推定 を行ってください。

発言一覧:
---
{joined}
---

出力は次の JSON のみ。前後に説明文やコードフェンスを付けないでください。
{{
  "topics": [{{"label": "話題名(短く)", "weight": 0〜1の重要度}}],
  "big_five": {{
    "openness": 0〜100の整数,
    "conscientiousness": 0〜100の整数,
    "extraversion": 0〜100の整数,
    "agreeableness": 0〜100の整数,
    "neuroticism": 0〜100の整数
  }},
  "personality": "性格の総評（推定である旨を含め日本語で1〜2文）"
}}

topics は重要度の高い順に最大 {STATS_TOPIC_COUNT} 件まで。
発言が少ない場合でも無理に断定せず、控えめに推定してください。"""

    async def ensure_analysis(self, user_id: str, force: bool = False) -> dict | None:
        """話題・性格推定の解析結果を返す。TTL 内ならキャッシュ、なければ LLM で再計算する。

        LLM 呼び出しやパースに失敗した場合は、既存のキャッシュ（あれば）にフォールバックする。
        """
        entry = self.cache.get(user_id)
        if not entry:
            return None
        samples = entry.get("recent_samples") or []
        cached = entry.get("analysis")
        if not samples:
            return cached
        if not force and cached and self._is_fresh(cached.get("computed_at")):
            return cached

        prompt = self._build_analysis_prompt(samples)
        try:
            text, _usage, _sources = await self.gemini.generate(
                prompt,
                images=None,
                timeout=STATS_ANALYSIS_TIMEOUT_SECONDS,
                grounding=False,
            )
        except Exception as error:
            print(f"会話統計の解析呼び出しに失敗: {error}")
            return cached

        parsed = extract_json_object(text or "")
        if not parsed:
            print("会話統計の解析結果を JSON として解釈できませんでした。")
            return cached

        analysis = {
            "topics": parsed.get("topics") or [],
            "big_five": parsed.get("big_five") or {},
            "personality": parsed.get("personality") or "",
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }
        entry["analysis"] = analysis
        await self.save_background()
        return analysis

    # ---- グラフ生成 -------------------------------------------------------

    @staticmethod
    def _save_figure(figure: "plt.Figure") -> io.BytesIO:
        buffer = io.BytesIO()
        figure.savefig(buffer, format="png", bbox_inches="tight")
        plt.close(figure)
        buffer.seek(0)
        return buffer

    def build_hourly_graph(self, entry: dict) -> io.BytesIO:
        """時間帯別の発言回数ヒストグラム（24時間）。"""
        hourly = entry.get("hourly") or [0] * 24
        with plt.rc_context(_FONT_CONTEXT):
            figure, axis = plt.subplots(figsize=(10, 5))
            x = np.arange(24)
            axis.bar(x, hourly, color="tab:blue")
            axis.set_xticks(x)
            axis.set_xlabel(f"時刻（{STATS_TIMEZONE}）")
            axis.set_ylabel("発言回数")
            axis.set_title("よく話す時間帯")
            axis.grid(True, axis="y", alpha=0.3)
            return self._save_figure(figure)

    def build_topic_graph(self, topics: list[dict]) -> io.BytesIO | None:
        """話題（トピック）の重み付き横棒グラフ。"""
        cleaned = [
            (str(t.get("label", "")).strip(), float(t.get("weight", 0) or 0))
            for t in topics
            if str(t.get("label", "")).strip()
        ]
        if not cleaned:
            return None
        cleaned.sort(key=lambda t: t[1], reverse=True)
        cleaned = cleaned[:STATS_TOPIC_COUNT][::-1]  # 横棒は下から上へ並ぶため反転
        labels = [c[0] for c in cleaned]
        weights = [c[1] for c in cleaned]
        with plt.rc_context(_FONT_CONTEXT):
            figure, axis = plt.subplots(figsize=(10, max(3, len(labels) * 0.6)))
            axis.barh(range(len(labels)), weights, color="tab:green")
            axis.set_yticks(range(len(labels)))
            axis.set_yticklabels(labels)
            axis.set_xlabel("重要度")
            axis.set_title("よく話す話題")
            axis.grid(True, axis="x", alpha=0.3)
            return self._save_figure(figure)

    def build_bigfive_radar(self, big_five: dict) -> io.BytesIO | None:
        """ビッグファイブ性格推定のレーダーチャート（5軸・0〜100）。"""
        keys = list(BIG_FIVE_LABELS.keys())
        values = [float(big_five.get(k, 0) or 0) for k in keys]
        if not any(values):
            return None
        labels = [BIG_FIVE_LABELS[k] for k in keys]
        angles = np.linspace(0, 2 * np.pi, len(keys), endpoint=False).tolist()
        # レーダーを閉じるため先頭要素を末尾に足す
        values_closed = values + values[:1]
        angles_closed = angles + angles[:1]
        with plt.rc_context(_FONT_CONTEXT):
            figure, axis = plt.subplots(figsize=(6, 6), subplot_kw={"polar": True})
            axis.plot(angles_closed, values_closed, color="tab:purple", marker="o")
            axis.fill(angles_closed, values_closed, color="tab:purple", alpha=0.25)
            axis.set_xticks(angles)
            axis.set_xticklabels(labels)
            axis.set_ylim(0, 100)
            axis.set_title("性格推定（ビッグファイブ）", pad=20)
            return self._save_figure(figure)

    def build_ranking_graph(self, top_n: int = 15) -> io.BytesIO | None:
        """発言回数のユーザー別ランキング横棒グラフ（管理者用の全体比較）。"""
        items = [
            (data.get("user_name") or uid, int(data.get("total_count", 0)))
            for uid, data in self.cache.items()
            if int(data.get("total_count", 0)) > 0
        ]
        if not items:
            return None
        items.sort(key=lambda t: t[1], reverse=True)
        items = items[:top_n][::-1]  # 横棒は下から上へ並ぶため反転
        names = [i[0] for i in items]
        counts = [i[1] for i in items]
        with plt.rc_context(_FONT_CONTEXT):
            figure, axis = plt.subplots(figsize=(10, max(3, len(names) * 0.5)))
            axis.barh(range(len(names)), counts, color="tab:orange")
            axis.set_yticks(range(len(names)))
            axis.set_yticklabels(names)
            axis.set_xlabel("発言回数")
            axis.set_title("発言回数ランキング")
            axis.grid(True, axis="x", alpha=0.3)
            return self._save_figure(figure)

    # ---- ダッシュボード組み立て ------------------------------------------

    async def build_dashboard(
        self, user_id: str, item: str
    ) -> tuple[str, list[tuple[str, io.BytesIO]]]:
        """指定ユーザー・指定項目のダッシュボード（説明文＋グラフ画像群）を組み立てる。

        戻り値 (caption, images):
            caption … エフェメラルに表示する説明テキスト
            images  … (ファイル名, PNG バッファ) のリスト。呼び出し側が File 化して送る。
        """
        entry = self.cache.get(user_id)
        name = self.display_name(user_id)
        if not entry or int(entry.get("total_count", 0)) == 0:
            return (f"{name} さんの会話データはまだありません。", [])

        images: list[tuple[str, io.BytesIO]] = []
        lines = [
            f"📈 **会話統計 — {name} さん**",
            f"・累計発言回数: {int(entry.get('total_count', 0))} 回",
        ]

        want_time = item in ("all", "time")
        want_topic = item in ("all", "topic")
        want_personality = item in ("all", "personality")

        if want_time:
            images.append(("stats_hourly.png", self.build_hourly_graph(entry)))

        analysis = None
        if want_topic or want_personality:
            analysis = await self.ensure_analysis(user_id)

        if want_topic:
            topic_graph = self.build_topic_graph(analysis.get("topics", [])) if analysis else None
            if topic_graph is not None:
                images.append(("stats_topics.png", topic_graph))
            else:
                lines.append("・話題: 解析できるデータがありませんでした。")

        if want_personality:
            samples = entry.get("recent_samples") or []
            if len(samples) < STATS_PERSONALITY_MIN_SAMPLES:
                lines.append(
                    f"・性格推定: データ不足のため表示できません"
                    f"（発言 {len(samples)} 件 / 必要 {STATS_PERSONALITY_MIN_SAMPLES} 件）。"
                )
            else:
                radar = self.build_bigfive_radar(analysis.get("big_five", {})) if analysis else None
                if radar is not None:
                    images.append(("stats_personality.png", radar))
                    summary = analysis.get("personality") if analysis else ""
                    if summary:
                        lines.append(f"・性格推定: {summary}")
                    lines.append("※ これは発言からの推定であり、確定的な性格診断ではありません。")
                else:
                    lines.append("・性格推定: 解析に失敗しました。時間をおいて再度お試しください。")

        return ("\n".join(lines), images)
