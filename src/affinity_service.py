import time
import math
import asyncio
import re

from background import run_in_thread
from constants import (
    AFFINITY_HALF_LIFE_SECONDS,
    AFFINITY_LOWER_THRESHOLD,
    AFFINITY_MAX,
    AFFINITY_MIN,
    AFFINITY_NEUTRAL,
    AFFINITY_RAISE_THRESHOLD,
)
from database import Database
from prompting import build_affinity_prompt

# モデルが返答の末尾に付ける感情タグ `[V:X, A:Y]`。
# `[V: +5, A: -10]` のように空白を挟むことがあるため各要素の前後に \s* を許容する
# （これを緩めないとタグが除去されずユーザーへそのまま露出する）。
# チャット返信・自発的な会話・会話要約のいずれでも同じ形を扱うため、
# 正規表現とその操作はここに集約している。
_AFFINITY_TAG_PATTERN = re.compile(r"\[V:\s*([+-]?\d+)\s*,\s*A:\s*([+-]?\d+)\s*\]")


def extract_affinity_changes(text: str) -> tuple[str, tuple[int, int] | None]:
    """感情タグを取り除いた本文と、その変化量を返す。

    Returns:
        (本文, (機嫌の変化量, テンションの変化量))。タグが無ければ変化量は None を返し、
        本文は元のまま（余計な strip をしない）。
    """
    match = _AFFINITY_TAG_PATTERN.search(text)
    if match is None:
        return text, None
    cleaned = _AFFINITY_TAG_PATTERN.sub("", text).strip()
    return cleaned, (int(match.group(1)), int(match.group(2)))


def strip_affinity_tag(text: str) -> str:
    """感情タグだけを取り除く（変化量を使わない経路用）。

    自発的な会話・記念日のお祝い・会話要約は特定の相手への返信ではないため
    親密度を更新しない。ペルソナが指示に反してタグを付けた場合の保険として使う。
    """
    return _AFFINITY_TAG_PATTERN.sub("", text).strip()


class AffinityService:
    def __init__(self, db: Database):
        self._db = db
        self.cache: dict[str, dict] = {}
        self._save_lock = asyncio.Lock()
        self._load()

    def _load(self):
        # 起動時に DB の affinity テーブルからキャッシュへ読み込む
        try:
            self.cache = self._db.affinity_load_all()
        except Exception as error:
            print(f"親密度データの読み込みに失敗しました: {error}")
            self.cache = {}

    def get_user_data(self, user_id: str) -> dict:
        data = self.cache.get(user_id, {
            "affinity": AFFINITY_NEUTRAL,
            "valence": AFFINITY_NEUTRAL,
            "arousal": AFFINITY_NEUTRAL,
            "last_interaction": time.time()
        }).copy()
        return self._apply_decay(data)

    def _apply_decay(self, data: dict) -> dict:
        current_time = time.time()
        last_time = data.get("last_interaction", current_time)
        elapsed = current_time - last_time

        if elapsed > 0:
            decay_factor = math.pow(0.5, elapsed / AFFINITY_HALF_LIFE_SECONDS)
            data["valence"] = AFFINITY_NEUTRAL + (data["valence"] - AFFINITY_NEUTRAL) * decay_factor
            data["arousal"] = AFFINITY_NEUTRAL + (data["arousal"] - AFFINITY_NEUTRAL) * decay_factor
            data["valence"] = max(AFFINITY_MIN, min(AFFINITY_MAX, int(round(data["valence"]))))
            data["arousal"] = max(AFFINITY_MIN, min(AFFINITY_MAX, int(round(data["arousal"]))))

        return data

    def update_emotion(self, user_id: str, v_change: int, a_change: int) -> dict:
        data = self.get_user_data(user_id)
        data["valence"] += v_change
        data["arousal"] += a_change

        data["valence"] = max(AFFINITY_MIN, min(AFFINITY_MAX, data["valence"]))
        data["arousal"] = max(AFFINITY_MIN, min(AFFINITY_MAX, data["arousal"]))

        if data["valence"] > AFFINITY_RAISE_THRESHOLD:
            data["affinity"] = min(AFFINITY_MAX, data["affinity"] + 1)
        elif data["valence"] < AFFINITY_LOWER_THRESHOLD:
            data["affinity"] = max(AFFINITY_MIN, data["affinity"] - 1)

        data["last_interaction"] = time.time()
        self.cache[user_id] = data
        return data

    async def save_background(self, user_id: str) -> None:
        """対象ユーザーだけを保存し、後続保存が古い値に巻き戻されないよう直列化する。"""
        async with self._save_lock:
            data = self.cache.get(user_id)
            if data is None:
                return
            snapshot = data.copy()
            try:
                # キャンセルしてもworker終了までロックを保ち、後続の保存を追い越させない。
                await run_in_thread(self._db.affinity_upsert, user_id, snapshot)
            except Exception as error:
                print(f"親密度データの保存に失敗しました: {error}")

    def build_dynamic_prompt(self, user_id: str) -> str:
        # 文面そのものは prompting.py に集約している（ここは現在値を渡すだけ）。
        data = self.get_user_data(user_id)
        return build_affinity_prompt(data["affinity"], data["valence"], data["arousal"])
