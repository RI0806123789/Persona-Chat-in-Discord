from types import ModuleType


def load_settings() -> ModuleType:
    try:
        import config_private as settings
    except ImportError:
        import config as settings
    return settings

