from dataclasses import dataclass, field
from datetime import datetime, timezone

from constants import (
    DEFAULT_AUTO_TOPIC_INTERVAL_HOURS,
    DEFAULT_MODEL_NAME,
    DEFAULT_PROMPT_FILE,
    DEFAULT_RESPOND_MODE,
)


@dataclass
class BotState:
    current_prompt_file: str = DEFAULT_PROMPT_FILE
    current_model_name: str = DEFAULT_MODEL_NAME
    current_respond_mode: str = DEFAULT_RESPOND_MODE
    grounding_enabled: bool = True
    channel_reset_points: dict[int, int] = field(default_factory=dict)
    last_activity_timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    memory_background_started: bool = False
    document_worker_started: bool = False
    commands_synced: bool = False

    # --- 自発的な会話（インメモリ。再起動でリセットされる） ---
    # 機能の有効/無効と無会話しきい値（時間）。/functions → 💬 自発会話 で変更する。
    auto_topic_enabled: bool = False
    auto_topic_interval_hours: int = DEFAULT_AUTO_TOPIC_INTERVAL_HOURS
    # 監視対象チャンネル: ボットが会話に参加したチャンネル ID → 最後に人間の発言があった時刻
    auto_topic_channels: dict[int, datetime] = field(default_factory=dict)
    # チャンネル ID → 最後に自動で話題提供した時刻（沈黙が続く限り連投しないための記録）
    auto_topic_posted_at: dict[int, datetime] = field(default_factory=dict)
    auto_topic_background_started: bool = False

    def touch(self) -> None:
        self.last_activity_timestamp = datetime.now(timezone.utc)
