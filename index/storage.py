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


def _find_balanced_json_object(text: str) -> str | None:
    """テキスト中の最初の `{` から、波括弧の対応が取れる位置までを切り出す。

    文字列リテラル内の波括弧・エスケープを正しく無視するため、単純な
    正規表現（非貪欲マッチ）ではネストしたJSONを取りこぼす問題を回避する。
    """
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def extract_json_object(text: str) -> dict[str, Any] | None:
    if not text:
        return None

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    # Markdownのコードブロックを除去
    text = re.sub(r"^```(?:json)?\n", "", text.strip())
    text = re.sub(r"\n```$", "", text)

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    # 前後に説明文などが付いた場合のフォールバック:
    # 波括弧のバランスを取ってネストしたJSONオブジェクトを丸ごと抽出する。
    candidate = _find_balanced_json_object(text)
    if candidate is None:
        return None

    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        return None
    return None


def get_prompt_block_reason(response: Any) -> str | None:
    """Gemini response の prompt_feedback からブロック理由名を取得する。

    ブロックされていなければ None を返す。
    注意: block_reason は enum のため、値0 (BLOCK_REASON_UNSPECIFIED) でも
    truthy になる。int() に変換して比較する必要がある。
    """
    prompt_feedback = getattr(response, "prompt_feedback", None)
    if not prompt_feedback:
        return None
    block_reason = getattr(prompt_feedback, "block_reason", None)
    if block_reason is None:
        return None
    try:
        if int(block_reason) == 0:
            return None
    except (TypeError, ValueError):
        return None
    return getattr(block_reason, "name", str(block_reason))


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

