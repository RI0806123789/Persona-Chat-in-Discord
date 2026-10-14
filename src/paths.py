from pathlib import Path

from constants import BASE_DIR


def resolve_script_path(relative_path: str) -> Path:
    return (BASE_DIR / relative_path).resolve()


def resolve_prompt_path(relative_path: str) -> Path:
    return resolve_script_path(relative_path)


def load_prompt_template(file_path: str | Path) -> str:
    return Path(file_path).read_text(encoding="utf-8")

