from pathlib import Path

from persona_bot.config.constants import BASE_DIR, BUILTIN_PERSONA_PROMPT


def resolve_script_path(relative_path: str) -> Path:
    return (BASE_DIR / relative_path).resolve()


def resolve_prompt_path(relative_path: str) -> Path:
    return resolve_script_path(relative_path)


def load_prompt_template(file_path: str | Path) -> str:
    # メモ帳などで「UTF-8 (BOM 付き)」保存されたペルソナも、先頭に U+FEFF を残さず読む
    return Path(file_path).read_text(encoding="utf-8-sig")



async def load_persona_template(prompt_file: str) -> str:
    """src基準のペルソナを別スレッドで読み、読込例外は呼出元へ返す。

    空の値はペルソナを1つも作っていない状態（personas.py）なので、ファイルを読まずに
    組み込みの標準プロンプトを返す。
    """
    if not prompt_file:
        return BUILTIN_PERSONA_PROMPT
    from persona_bot.core.background import run_in_thread

    return await run_in_thread(_load_persona_template, prompt_file)


def _load_persona_template(prompt_file: str) -> str:
    return load_prompt_template(resolve_prompt_path(prompt_file))
