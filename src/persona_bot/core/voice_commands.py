"""参加・退出の受付順を記録し、同じサーバーの接続変更を直列化する。"""

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import discord


@dataclass
class _GuildOperations:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    latest: object | None = None
    pending: int = 0


async def skip_superseded_voice_operation(
    interaction: discord.Interaction, is_current: Callable[[], bool],
) -> bool:
    """後続の操作が受け付けられていれば、古い操作の接続変更を見送る。"""
    if is_current():
        return False
    await interaction.followup.send("後から受け付けた参加・退出操作を優先しました。")
    return True


class VoiceCommandCoordinator:
    """応答待ちから操作完了まで、サーバー単位でロックと操作世代を共有する。"""

    def __init__(self) -> None:
        self._guilds: dict[int | None, _GuildOperations] = {}

    async def run(
        self, interaction: discord.Interaction,
        change: Callable[[Callable[[], bool]], Awaitable[None]],
    ) -> None:
        guild_id = interaction.guild.id if interaction.guild is not None else None
        operations = self._guilds.setdefault(guild_id, _GuildOperations())
        token = object()
        # deferより前に同期的に記録する。応答待ちの古い参加も後続の退出から識別できる。
        operations.latest = token
        operations.pending += 1

        def is_current() -> bool:
            return operations.latest is token

        try:
            await interaction.response.defer()
            async with operations.lock:
                if not await skip_superseded_voice_operation(interaction, is_current):
                    await change(is_current)
        finally:
            operations.pending -= 1
            # 応答待ちも含めて最後の操作が終わるまで、ロックと世代を共有する。
            if operations.pending == 0:
                self._guilds.pop(guild_id, None)
