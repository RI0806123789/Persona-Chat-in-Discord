import json
import re
from pathlib import Path
from typing import Any


def read_text_file(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def load_json_file(path: Path, default_value: dict[str, Any]) -> dict[str, Any]:
    try:
        raw_text = read_text_file(path).strip()
        if not raw_text:
            return default_value.copy()
        loaded = json.loads(raw_text)
        if isinstance(loaded, dict):
            return loaded
    except Exception as error:
        print(f"JSON読み込みエラー ({path}): {error}")
    return default_value.copy()


def write_json_file(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_file(path: Path) -> None:
    path.write_text("", encoding="utf-8")


def append_text_to_file(path: Path, text: str) -> None:
    with open(path, "a", encoding="utf-8") as file:
        file.write(text)


def extract_json_object(text: str) -> dict[str, Any] | None:
    if not text:
        return None

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None

    try:
        parsed = json.loads(match.group(0))
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        return None
    return None


def safe_response_text(response: Any) -> str | None:
    """Gemini の response から安全にテキストを取得する。

    response.text はプロパティとして実装されており、candidates が空の場合
    （PROHIBITED_CONTENT 等でブロックされた場合）に例外を投げる。
    getattr(response, "text", default) ではプロパティの例外を防げないため、
    try/except で囲む必要がある。
    """
    try:
        text = response.text
        return text if text else None
    except (ValueError, IndexError, AttributeError):
        return None
