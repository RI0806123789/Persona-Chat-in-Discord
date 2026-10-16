from pathlib import Path

from persona_bot.config.constants import BASE_DIR


def resolve_script_path(relative_path: str) -> Path:
    return (BASE_DIR / relative_path).resolve()


def resolve_prompt_path(relative_path: str) -> Path:
    return resolve_script_path(relative_path)


def load_prompt_template(file_path: str | Path) -> str:
    # メモ帳などで「UTF-8 (BOM 付き)」保存されたペルソナも、先頭に U+FEFF を残さず読む
    return Path(file_path).read_text(encoding="utf-8-sig")

