"""管理者の再確認（最新の Member の取得）を試すための、架空の Member を作る。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord


def make_member(*, admin: bool, fresh_admin: bool | None = None, user_id: int = 11) -> Mock:
    """ボタンを押した時点の Member（写し）と、取り直すと返る別インスタンスの Member を作る。

    `member.fresh` が `guild.fetch_member` の返す最新の Member。長い処理の間にロールが外れた状況は、
    押した時点の `member` ではなく `member.fresh.guild_permissions.administrator = False` で再現する
    （実際の discord.py でも、押した時点の Member は後から更新されないため）。
    """
    fresh = Mock(spec=discord.Member, id=user_id)
    fresh.guild_permissions = SimpleNamespace(administrator=admin if fresh_admin is None else fresh_admin)
    member = Mock(spec=discord.Member, id=user_id)
    member.guild_permissions = SimpleNamespace(administrator=admin)
    member.guild = SimpleNamespace(fetch_member=AsyncMock(return_value=fresh))
    member.fresh = fresh
    return member
