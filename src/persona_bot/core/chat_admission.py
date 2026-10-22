"""通常会話の受付数と同時実行数を、待機処理を作らず制限する。"""

from collections import OrderedDict, deque
import time

from persona_bot.config.constants import (
    CHAT_MAX_CONCURRENT_GLOBAL, CHAT_MAX_CONCURRENT_PER_GUILD, CHAT_MAX_REQUESTS_PER_USER,
    CHAT_RATE_WINDOW_SECONDS, CHAT_RATE_MAX_USERS,
)


class ChatAdmission:
    """単一イベントループ上で、判定と確保・返却の間に await を挟まない。"""

    def __init__(self) -> None:
        self._accepted: OrderedDict[int, deque[float]] = OrderedDict()
        self._running_by_guild: dict[int | None, int] = {}
        self._running = 0

    def try_acquire(self, user_id: int, guild_id: int | None) -> bool:
        now = time.monotonic()
        cutoff = now - CHAT_RATE_WINDOW_SECONDS
        # 最後の受付が古い順。期間内の記録は追い出さず、上限の迂回を防ぐ。
        while self._accepted:
            oldest = next(iter(self._accepted.values()))
            if oldest[-1] > cutoff:
                break
            self._accepted.popitem(last=False)
        accepted = self._accepted.get(user_id)
        if accepted is not None:
            while accepted and accepted[0] <= cutoff:
                accepted.popleft()
            if len(accepted) >= CHAT_MAX_REQUESTS_PER_USER:
                return False
        elif len(self._accepted) >= CHAT_RATE_MAX_USERS:
            return False
        guild_running = self._running_by_guild.get(guild_id, 0)
        if self._running >= CHAT_MAX_CONCURRENT_GLOBAL or guild_running >= CHAT_MAX_CONCURRENT_PER_GUILD:
            return False

        if accepted is None:
            accepted = deque()
            self._accepted[user_id] = accepted
        accepted.append(now)
        self._accepted.move_to_end(user_id)
        self._running_by_guild[guild_id] = guild_running + 1
        self._running += 1
        return True

    def release(self, guild_id: int | None) -> None:
        """処理中の枠だけを返し、期間内の受付数は失敗・中断でも戻さない。"""
        remaining = self._running_by_guild[guild_id] - 1
        if remaining:
            self._running_by_guild[guild_id] = remaining
        else:
            del self._running_by_guild[guild_id]
        self._running -= 1
