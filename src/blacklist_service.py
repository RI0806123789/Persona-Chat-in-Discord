"""ブラックリスト管理サービス。

ng/blacklist.json を使って、不適切な発言を繰り返すユーザーを
三審制（3回違反でブロック）で管理する。
ユーザーの識別にはDiscordの固有ID（スノーフレークID）を使用する。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from constants import BLACKLIST_PATH, BLACKLIST_MAX_VIOLATIONS
from storage import load_json_file, write_json_file


class BlacklistService:
    """ユーザーの違反回数を記録し、閾値に達したらブロック状態にするサービス。"""

    def __init__(self, path: Path = BLACKLIST_PATH) -> None:
        self._path = path
        self._data: dict[str, Any] = {}
        self.reload()

    def reload(self) -> None:
        """ng/blacklist.json からデータを読み込む。"""
        self._data = load_json_file(self._path, {})

    def _save(self) -> None:
        """現在のデータを ng/blacklist.json に書き込む。"""
        write_json_file(self._path, self._data)

    # ------------------------------------------------------------------
    # 参照系
    # ------------------------------------------------------------------

    def is_blocked(self, user_id: int) -> bool:
        """指定ユーザーがブロック中かどうかを返す。"""
        record = self._data.get(str(user_id))
        if record is None:
            return False
        return bool(record.get("blocked", False))

    def get_violation_count(self, user_id: int) -> int:
        """指定ユーザーの現在の違反回数を返す。"""
        record = self._data.get(str(user_id))
        if record is None:
            return 0
        return int(record.get("count", 0))

    def get_all_blocked_users(self) -> list[dict[str, Any]]:
        """ブロック中の全ユーザー情報をリストで返す。"""
        blocked: list[dict[str, Any]] = []
        for uid, record in self._data.items():
            if record.get("blocked", False):
                blocked.append({"user_id": int(uid), **record})
        return blocked

    # ------------------------------------------------------------------
    # 更新系
    # ------------------------------------------------------------------

    def record_violation(self, user_id: int, user_name: str) -> tuple[int, bool]:
        """違反を1回記録し、(新しい違反回数, ブロックされたか) を返す。

        3回目の違反でブロック状態に移行する。
        """
        key = str(user_id)
        record = self._data.get(key, {"count": 0, "blocked": False})

        # 既にブロック済みの場合はカウントを増やさずに現在の状態を返す
        if record.get("blocked", False):
            return record.get("count", 0), False

        record["count"] = record.get("count", 0) + 1
        record["user_name"] = user_name

        just_blocked = False
        if record["count"] >= BLACKLIST_MAX_VIOLATIONS and not record.get("blocked", False):
            record["blocked"] = True
            record["blocked_at"] = datetime.now(timezone.utc).isoformat()
            just_blocked = True

        self._data[key] = record
        self._save()

        return record["count"], just_blocked

    def unblock(self, user_id: int) -> bool:
        """指定ユーザーのブロックを解除し、違反カウントをリセットする。

        Returns:
            True: ブロックが解除された。
            False: そのユーザーはブラックリストに存在しなかった。
        """
        key = str(user_id)
        if key not in self._data:
            return False

        del self._data[key]
        self._save()
        return True
