import time
import math
import asyncio

from database import Database

HALF_LIFE = 1800.0  # 30 minutes in seconds

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
        except Exception as e:
            print(f"Failed to load affinity data: {e}")
            self.cache = {}

    def get_user_data(self, user_id: str) -> dict:
        data = self.cache.get(user_id, {
            "affinity": 50,
            "valence": 50,
            "arousal": 50,
            "last_interaction": time.time()
        }).copy()
        return self._apply_decay(data)

    def _apply_decay(self, data: dict) -> dict:
        current_time = time.time()
        last_time = data.get("last_interaction", current_time)
        elapsed = current_time - last_time
        
        if elapsed > 0:
            decay_factor = math.pow(0.5, elapsed / HALF_LIFE)
            data["valence"] = 50 + (data["valence"] - 50) * decay_factor
            data["arousal"] = 50 + (data["arousal"] - 50) * decay_factor
            data["valence"] = max(0, min(100, int(round(data["valence"]))))
            data["arousal"] = max(0, min(100, int(round(data["arousal"]))))
            
        return data

    def update_emotion(self, user_id: str, v_change: int, a_change: int) -> dict:
        data = self.get_user_data(user_id)
        data["valence"] += v_change
        data["arousal"] += a_change
        
        data["valence"] = max(0, min(100, data["valence"]))
        data["arousal"] = max(0, min(100, data["arousal"]))
        
        if data["valence"] > 70:
            data["affinity"] = min(100, data["affinity"] + 1)
        elif data["valence"] < 30:
            data["affinity"] = max(0, data["affinity"] - 1)
            
        data["last_interaction"] = time.time()
        self.cache[user_id] = data
        return data

    async def save_background(self):
        # 同一テーブルへの並行書き込みを避けるため、保存はロックで直列化する。
        async with self._save_lock:
            cache_copy = {user_id: data.copy() for user_id, data in self.cache.items()}
            try:
                await asyncio.to_thread(self._db.affinity_save_all, cache_copy)
            except Exception as e:
                print(f"Failed to save affinity data: {e}")

    def build_dynamic_prompt(self, user_id: str) -> str:
        data = self.get_user_data(user_id)
        affinity = data["affinity"]
        valence = data["valence"]
        arousal = data["arousal"]
        
        return f"""
【現在のあなたの状態】
・このユーザーへの親密度(Affinity): {affinity}/100
・現在の機嫌(Valence): {valence}/100
・現在のテンション(Arousal): {arousal}/100

上記を踏まえて返答を生成してください。
また、今回の返答によって生じた感情の変化量を、返答の末尾に必ず `[V:X, A:Y]` の形式で追記してください。
Xは機嫌の変動量、Yはテンションの変動量です。（例: `[V:+5, A:-10]` や `[V:0, A:+2]`）
"""
