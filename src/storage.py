from copy import deepcopy
from datetime import datetime, timezone
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4


def read_text_file(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def load_json_file(path: Path, default_value: dict[str, Any]) -> dict[str, Any]:
    """未作成なら初期値、破損時は原本の退避に成功した場合だけ初期値を返す。"""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return deepcopy(default_value)
    except (json.JSONDecodeError, UnicodeDecodeError):
        reason = "JSONの形式または文字コードが不正です。"
    else:
        if isinstance(loaded, dict):
            return loaded
        reason = "JSONの最上位がオブジェクトではありません。"

    # 空ファイルも書き込み中断の可能性がある。証拠を残す前に初期値を返さない。
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = path.with_name(f"{path.name}.corrupt-{stamp}-{uuid4().hex}.bak")
    try:
        os.replace(path, backup_path)
    except OSError:
        print(f"JSON読み込みエラー ({path}): {reason} 退避できないため処理を中止します。")
        raise
    print(f"JSON読み込みエラー ({path}): {reason} {backup_path} へ退避し、既定値で再開します。")
    return deepcopy(default_value)


def write_json_file(path: Path, data: dict[str, Any]) -> None:
    """同じディレクトリで内容を同期してから置換し、保存中の失敗で原本を壊さない。"""
    serialized = json.dumps(data, ensure_ascii=False, indent=2)
    try:
        existing_mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        existing_mode = None

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            if existing_mode is not None:
                os.chmod(temporary_path, existing_mode)
            temporary_file.write(serialized)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        # Windowsでも置換できるよう、一時ファイルを閉じてから切り替える。
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                # 後始末の失敗で、書き込みが失敗した本来の例外を隠さない。
                print(f"JSON一時ファイルの削除に失敗しました ({temporary_path})。")


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

