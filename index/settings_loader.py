from typing import Any


def load_settings() -> Any:
    try:
        import config_private as settings
    except ImportError:
        import config as settings
    return settings

