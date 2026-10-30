"""サーバー退出時の実行中セッションとメモリ上の設定を回収する。"""

import logging
from typing import Any

from persona_bot.core.app_state import BotState
from persona_bot.services.autonomous_service import AutonomousService
from persona_bot.services.voice_guard import VoiceGuard


logger = logging.getLogger(__name__)


async def remove_guild_state(
    guild_id: int, state: BotState, autonomous: AutonomousService, voice_guard: VoiceGuard,
    voice_conversation: Any = None,
) -> None:
    """停止処理が設定を参照し終わってからキャッシュを外す。永続化データは保持する。"""
    try:
        await autonomous.stop_session(guild_id, reason="サーバーからの退出")
    except Exception as error:
        logger.error(f'サーバー退出時の配信停止に失敗しました: {type(error).__name__}', exc_info=error)
    finally:
        try:
            # 音声会話（#268）の受信・Live の接続・同意パネルも、そのサーバー分を片付ける。
            if voice_conversation is not None:
                await voice_conversation.stop(guild_id, reason="サーバーからの退出")
        except Exception as error:
            logger.error(f'サーバー退出時の音声会話の停止に失敗しました: {type(error).__name__}', exc_info=error)
        finally:
            voice_guard.release(guild_id)
            state.forget_guild(guild_id)
            state.welcomed_guild_ids.discard(guild_id)
