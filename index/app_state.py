from dataclasses import dataclass, field
from datetime import datetime, timezone

from constants import DEFAULT_MODEL_NAME, DEFAULT_PROMPT_FILE, DEFAULT_RESPOND_MODE


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

    def touch(self) -> None:
        self.last_activity_timestamp = datetime.now(timezone.utc)

