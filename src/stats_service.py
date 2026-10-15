import asyncio
import io
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from constants import (
    ROUTER_MODEL_NAME,
    STATS_ANALYSIS_TIMEOUT_SECONDS,
    STATS_ANALYSIS_TTL_HOURS,
    STATS_MAX_USERS_IN_SELECT,
    STATS_OPT_OUT_DEFAULT,
    STATS_PERSONALITY_MIN_SAMPLES,
    STATS_RECENT_SAMPLES_MAX,
    STATS_TIMEZONE,
    STATS_TOPIC_COUNT,
)
from background import run_in_thread
from graph_rendering import graph_font_context, serialized_plot
from database import Database
from prompting import build_stats_analysis_prompt
from storage import extract_json_object
from timeutil import resolve_timezone, timezone_label

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

    本人が収集そのものを停止できる（opt-out）。停止判定は record() /
    ensure_analysis() の内部で行い、呼び出し側には判断させない
    （呼び出し箇所が増えたときのチェック漏れを防ぐため）。
    """

    def __init__(self, db: Database, gemini: Any) -> None:
        self._db = db
        self.gemini = gemini
        # { user_id: { user_name, total_count, hourly[24], first_seen, last_seen,
        #              recent_samples[], analysis{ topics, big_five, personality, computed_at },
        #              opt_out } }
        try:
            self.cache: dict[str, dict] = db.stats_load_all()
        except Exception as error:
            print(f"会話統計の読み込みに失敗: {error}")
            self.cache = {}
        self._save_lock = asyncio.Lock()
        # 削除・収集設定の変更より前に開始した解析結果を書き戻さないための世代。
        self._analysis_versions: dict[str, int] = {}
        self._tz = resolve_timezone(STATS_TIMEZONE, purpose="会話統計の時間帯集計")

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
            # 収集停止（opt-out）の既定値。定数から引くことで、運用方針を
            # 「原則収集しない（オプトイン）」へ切り替えられるようにしている。
            "opt_out": STATS_OPT_OUT_DEFAULT,
        }

    @staticmethod
    def _entry_opted_out(entry: dict) -> bool:
        """エントリが収集停止中かを返す（キーが無い旧形式は既定値で扱う）。"""
        return bool(entry.get("opt_out", STATS_OPT_OUT_DEFAULT))

    def _to_local_hour(self, when: datetime) -> int:
        """発言時刻（UTC aware 想定）を集計用タイムゾーンの「時」に変換する。"""
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when.astimezone(self._tz).hour

    async def record(self, user_id: str, user_name: str, text: str, when: datetime) -> bool:
        """1件を集計・保存し、成否を返す。失敗してもチャット本体は止めない。"""
        try:
            # 更新から保存までを直列化し、古いスナップショットによる上書きを防ぐ。
            async with self._save_lock:
                if self.is_opted_out(user_id):
                    return True
                entry = self.cache.get(user_id)
                if entry is None:
                    entry = self._new_entry(user_name)
                    self.cache[user_id] = entry
                entry["user_name"] = user_name
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
                    if len(samples) > STATS_RECENT_SAMPLES_MAX:
                        del samples[: len(samples) - STATS_RECENT_SAMPLES_MAX]
                return await self._save_user_locked(user_id)
        except Exception as error:
            print(f"会話統計の記録に失敗: {error}")
            return False

    async def save_background(self, user_id: str) -> bool:
        """対象ユーザーの現在値だけを保存し、成否を返す。"""
        async with self._save_lock:
            return await self._save_user_locked(user_id)

    async def _save_user_locked(self, user_id: str) -> bool:
        """_save_lock 保持中に使う。入れ子の可変値も保存用にコピーする。"""
        entry = self.cache.get(user_id)
        if entry is None:
            return True
        try:
            await run_in_thread(self._db.stats_save_user, user_id, deepcopy(entry))
        except Exception as error:
            print(f"会話統計の保存に失敗: {error}")
            return False
        return True

    # ---- 収集停止（opt-out） ----------------------------------------------

    def is_opted_out(self, user_id: str) -> bool:
        """そのユーザーが統計収集を停止しているかを返す。

        on_message の主経路（record）から毎回呼ばれるため、DB へは行かず
        キャッシュだけを見る。一度も発言していないユーザーはエントリが無いので、
        既定値 STATS_OPT_OUT_DEFAULT を返す（True にすればオプトイン運用になる）。
        """
        entry = self.cache.get(user_id)
        if entry is None:
            return STATS_OPT_OUT_DEFAULT
        return self._entry_opted_out(entry)

    async def set_opt_out(self, user_id: str, user_name: str, opted_out: bool) -> bool:
        """統計収集の停止/再開を設定する。保存できたかを返す。

        一度も発言していないユーザーはエントリが無いため、その場で作ってから
        フラグを立てる（「発言してからでないと停止できない」を避けるため）。
        """
        async with self._save_lock:
            entry = self.cache.get(user_id)
            if entry is None:
                entry = self._new_entry(user_name)
                self.cache[user_id] = entry
            elif user_name:
                entry["user_name"] = user_name
            entry["opt_out"] = bool(opted_out)
            self._analysis_versions[user_id] = self._analysis_versions.get(user_id, 0) + 1
            return await self._save_user_locked(user_id)

    async def clear_user_data(self, user_id: str) -> bool:
        """そのユーザーの蓄積データ（集計値・発言サンプル・解析キャッシュ）を消す。

        **行ごとは消さない。** del self.cache[user_id] にすると収集停止の設定まで
        一緒に消えてしまうため、フィールドだけをリセットして opt_out は残す。
        保存できたかを返す（プライバシー操作の失敗は隠さずユーザーへ伝える）。
        """
        async with self._save_lock:
            entry = self.cache.get(user_id)
            if entry is None:
                return True
            now_iso = datetime.now(timezone.utc).isoformat()
            entry["total_count"] = 0
            entry["hourly"] = [0] * 24
            entry["recent_samples"] = []
            entry["analysis"] = None
            entry["first_seen"] = now_iso
            entry["last_seen"] = now_iso
            self._analysis_versions[user_id] = self._analysis_versions.get(user_id, 0) + 1
            return await self._save_user_locked(user_id)

    # ---- 参照ヘルパー -----------------------------------------------------

    def has_data(self) -> bool:
        return any(int(d.get("total_count", 0)) > 0 for d in self.cache.values())

    def display_name(self, user_id: str) -> str:
        entry = self.cache.get(user_id)
        if entry and entry.get("user_name"):
            return entry["user_name"]
        return str(user_id)

    def get_user_options(self) -> list[tuple[str, str, int]]:
        """管理者向けユーザー選択メニュー用に (user_id, 表示名, 発言回数) を回数の多い順で返す。

        収集停止中のユーザーは載せない（「収集を止めたい」という意思表示には
        「他人に見られたくない」が含まれると解釈する）。まだ発言していない人も
        同じく載らないため、外形上は両者を区別できない＝停止したこと自体は漏れない。
        """
        items = [
            (uid, data.get("user_name") or uid, int(data.get("total_count", 0)))
            for uid, data in self.cache.items()
            if int(data.get("total_count", 0)) > 0 and not self._entry_opted_out(data)
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

    async def ensure_analysis(self, user_id: str, force: bool = False) -> dict | None:
        """話題・性格推定の解析結果を返す。TTL 内ならキャッシュ、なければ LLM で再計算する。

        LLM 呼び出しやパースに失敗した場合は、既存のキャッシュ（あれば）にフォールバックする。
        """
        async with self._save_lock:
            entry = self.cache.get(user_id)
            if not entry:
                return None
            cached = deepcopy(entry.get("analysis"))
            if self._entry_opted_out(entry):
                return cached
            samples = list(entry.get("recent_samples") or [])
            if not samples:
                return cached
            if not force and cached and self._is_fresh(cached.get("computed_at")):
                return cached
            version = self._analysis_versions.get(user_id, 0)

        # プロンプト文面は prompting.py に集約している（STATS_TOPIC_COUNT のような
        # 定数は prompting が constants を import しない規約のため引数で渡す）。
        prompt = build_stats_analysis_prompt(samples, STATS_TOPIC_COUNT)
        parsed = None
        try:
            # 話題・性格推定はユーザーが選択中のペルソナ/モデルとは無関係な内部解析
            # タスクのため、記憶ルーティング等と同じ固定モデル（ROUTER_MODEL_NAME）を使う。
            text, _usage, _sources = await self.gemini.generate(
                prompt,
                ROUTER_MODEL_NAME,
                images=None,
                timeout=STATS_ANALYSIS_TIMEOUT_SECONDS,
                grounding=False,
            )
            parsed = extract_json_object(text or "")
            if not parsed:
                print("会話統計の解析結果を JSON として解釈できませんでした。")
        except Exception as error:
            print(f"会話統計の解析呼び出しに失敗: {error}")

        analysis = {
            "topics": parsed.get("topics") or [],
            "big_five": parsed.get("big_five") or {},
            "personality": parsed.get("personality") or "",
            "computed_at": datetime.now(timezone.utc).isoformat(),
        } if parsed else None
        async with self._save_lock:
            entry = self.cache.get(user_id)
            if entry is None:
                return None
            # LLM待機中の削除・停止・再開をまたいだ結果は反映しない。
            if (analysis is None or version != self._analysis_versions.get(user_id, 0)
                    or self._entry_opted_out(entry)):
                return deepcopy(entry.get("analysis"))
            previous = entry.get("analysis")
            entry["analysis"] = analysis
            if not await self._save_user_locked(user_id):
                entry["analysis"] = previous
                return previous
        return analysis

    # ---- グラフ生成 -------------------------------------------------------

    @staticmethod
    def _save_figure(figure: "plt.Figure") -> io.BytesIO:
        buffer = io.BytesIO()
        try:
            figure.savefig(buffer, format="png", bbox_inches="tight")
            buffer.seek(0)
            return buffer
        except BaseException:
            buffer.close()
            raise
        finally:
            plt.close(figure)

    @serialized_plot
    def build_hourly_graph(self, entry: dict) -> io.BytesIO:
        """時間帯別の発言回数ヒストグラム（24時間）。"""
        hourly = entry.get("hourly") or [0] * 24
        with plt.rc_context(graph_font_context()):
            figure, axis = plt.subplots(figsize=(10, 5))
            x = np.arange(24)
            axis.bar(x, hourly, color="tab:blue")
            axis.set_xticks(x)
            # 集計に実際に使った tz を表示する（定数のままだと、tzdata が無く
            # UTC へフォールバックした環境で「Asia/Tokyo」と偽って表示してしまう）。
            axis.set_xlabel(f"時刻（{timezone_label(self._tz)}）")
            axis.set_ylabel("発言回数")
            axis.set_title("よく話す時間帯")
            axis.grid(True, axis="y", alpha=0.3)
            return self._save_figure(figure)

    @serialized_plot
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
        with plt.rc_context(graph_font_context()):
            figure, axis = plt.subplots(figsize=(10, max(3, len(labels) * 0.6)))
            axis.barh(range(len(labels)), weights, color="tab:green")
            axis.set_yticks(range(len(labels)))
            axis.set_yticklabels(labels)
            axis.set_xlabel("重要度")
            axis.set_title("よく話す話題")
            axis.grid(True, axis="x", alpha=0.3)
            return self._save_figure(figure)

    @serialized_plot
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
        with plt.rc_context(graph_font_context()):
            figure, axis = plt.subplots(figsize=(6, 6), subplot_kw={"polar": True})
            axis.plot(angles_closed, values_closed, color="tab:purple", marker="o")
            axis.fill(angles_closed, values_closed, color="tab:purple", alpha=0.25)
            axis.set_xticks(angles)
            axis.set_xticklabels(labels)
            axis.set_ylim(0, 100)
            axis.set_title("性格推定（ビッグファイブ）", pad=20)
            return self._save_figure(figure)

    async def build_ranking_graph(self, top_n: int = 15) -> io.BytesIO | None:
        """発言回数のユーザー別ランキング横棒グラフ（管理者用の全体比較）。

        収集停止中のユーザーは、データが残っていてもランキングに出さない
        （get_user_options と同じ理由）。
        """
        snapshot = {
            uid: (data.get("user_name") or uid, int(data.get("total_count", 0)))
            for uid, data in self.cache.items()
            if int(data.get("total_count", 0)) > 0 and not self._entry_opted_out(data)
        }
        if not snapshot:
            return None
        versions = {uid: self._analysis_versions.get(uid, 0) for uid in snapshot}
        graph = await run_in_thread(self._render_ranking_graph, list(snapshot.values()), top_n)
        # 描画中に停止・削除したユーザーの情報を、管理者へ後から送信しない。
        if any(uid not in self.cache or self._entry_opted_out(self.cache[uid])
               or not self.cache[uid].get("total_count")
               or versions[uid] != self._analysis_versions.get(uid, 0) for uid in snapshot):
            graph.close()
            return None
        return graph

    @serialized_plot
    def _render_ranking_graph(self, items: list[tuple[str, int]], top_n: int) -> io.BytesIO:
        items.sort(key=lambda t: t[1], reverse=True)
        items = items[:top_n][::-1]  # 横棒は下から上へ並ぶため反転
        names = [i[0] for i in items]
        counts = [i[1] for i in items]
        with plt.rc_context(graph_font_context()):
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
        self, user_id: str, item: str, viewer_id: str | None = None
    ) -> tuple[str, list[tuple[str, io.BytesIO]]]:
        """指定ユーザー・指定項目のダッシュボード（説明文＋グラフ画像群）を組み立てる。

        戻り値 (caption, images):
            caption … エフェメラルに表示する説明テキスト
            images  … (ファイル名, PNG バッファ) のリスト。呼び出し側が File 化して送る。

        viewer_id は「誰が見ているか」。収集停止中のユーザーのデータは、本人には
        注記つきで見せるが、他人（管理者）には「まだありません」と同じ文言を返して
        停止していること自体を露見させない。省略時（None）は本人とみなす。
        UIからは必ずviewer_idを渡す。描画中の削除や収集設定変更も世代で検出する。
        """
        entry = deepcopy(self.cache.get(user_id))
        version = self._analysis_versions.get(user_id, 0)
        name = self.display_name(user_id)
        opted_out = bool(entry) and self._entry_opted_out(entry)
        # 他人が停止中のユーザーを指定した場合は、データの有無にかかわらず
        # 「まだありません」と同一の文言を返す（専用の文言を出すと停止が露見する）。
        if opted_out and viewer_id is not None and viewer_id != user_id:
            return (f"{name} さんの会話データはまだありません。", [])
        if not entry or int(entry.get("total_count", 0)) == 0:
            return (f"{name} さんの会話データはまだありません。", [])

        images: list[tuple[str, io.BytesIO]] = []
        try:
            lines = [
                f"📈 **会話統計 — {name} さん**",
                f"・累計発言回数: {int(entry.get('total_count', 0))} 回",
            ]
            if opted_out:
                # 本人向けの注記。停止後も残っているデータを見ていることが分かるようにする。
                lines.insert(1, "🔕 現在、統計収集を停止しています。（表示中のデータは停止前の記録です）")

            want_time = item in ("all", "time")
            want_topic = item in ("all", "topic")
            want_personality = item in ("all", "personality")

            if want_time:
                images.append(("stats_hourly.png", await run_in_thread(self.build_hourly_graph, entry)))

            analysis = None
            if want_topic or want_personality:
                analysis = await self.ensure_analysis(user_id)

            if want_topic:
                topic_graph = await run_in_thread(self.build_topic_graph, deepcopy(analysis.get("topics", []))) if analysis else None
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
                    radar = await run_in_thread(self.build_bigfive_radar, deepcopy(analysis.get("big_five", {}))) if analysis else None
                    if radar is not None:
                        images.append(("stats_personality.png", radar))
                        summary = analysis.get("personality") if analysis else ""
                        if summary:
                            lines.append(f"・性格推定: {summary}")
                        lines.append("※ これは発言からの推定であり、確定的な性格診断ではありません。")
                    else:
                        lines.append("・性格推定: 解析に失敗しました。時間をおいて再度お試しください。")

            current = self.cache.get(user_id)
            if (not current or not current.get("total_count")
                    or version != self._analysis_versions.get(user_id, 0)
                    or (viewer_id is not None and viewer_id != user_id and self._entry_opted_out(current))):
                for _, buffer in images:
                    buffer.close()
                return (f"{name} さんの会話データはまだありません。", [])
            return ("\n".join(lines), images)
        except BaseException:
            for _, buffer in images:
                buffer.close()
            raise
