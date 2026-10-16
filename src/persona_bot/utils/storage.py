from copy import deepcopy
from collections.abc import Iterator
from datetime import datetime, timezone
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4


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


def _iter_json_objects(text: str) -> Iterator[dict[str, Any]]:
    """不正な候補の後も走査し、文字列内の括弧やネストは標準パーサーで扱う。"""
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            parsed, end = decoder.raw_decode(text, start)
        except (ValueError, RecursionError):
            start = text.find("{", start + 1)
            continue
        if isinstance(parsed, dict):
            yield parsed
        # 正常なオブジェクトの内部を別のトップレベル候補として採用しない。
        start = text.find("{", end)


def extract_json_object(text: str, *, required_keys: tuple[str, ...] = ()) -> dict[str, Any] | None:
    """コードブロックを優先し、必要なキーを持つ最初のJSONオブジェクトを返す。"""
    if not text:
        return None

    # 説明文のJSON例より、明示された出力ブロックを先に確認する。
    blocks = re.finditer(r"```(?:json)?[ \t]*\r?\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    for block in blocks:
        for parsed in _iter_json_objects(block.group(1)):
            if all(key in parsed for key in required_keys):
                return parsed
    for parsed in _iter_json_objects(text):
        if all(key in parsed for key in required_keys):
            return parsed
    return None


def get_prompt_block_reason(response: Any) -> str | None:
    """Gemini response の prompt_feedback からブロック理由名を取得する。

    ブロックされていなければ None を返す。
    新SDKの文字列enumと、既存の数値enumのどちらでも未指定とブロックを区別する。
    """
    prompt_feedback = getattr(response, "prompt_feedback", None)
    if not prompt_feedback:
        return None
    block_reason = getattr(prompt_feedback, "block_reason", None)
    if block_reason is None:
        return None
    value = getattr(block_reason, "value", block_reason)
    if value in (None, 0, "0", "", "BLOCK_REASON_UNSPECIFIED", "BLOCKED_REASON_UNSPECIFIED"):
        return None
    return getattr(block_reason, "name", str(value))


def safe_response_text(response: Any) -> str | None:
    """Gemini の response から安全にテキストを取得する。

    新SDKの空応答・安全フィルタと、例外を投げる旧形式のプロパティを吸収する。
    プロンプトや応答が安全フィルタでブロックされた場合は本文を返さない。
    """
    try:
        if get_prompt_block_reason(response) is not None:
            return None
        candidates = getattr(response, "candidates", None)
        if candidates:
            reason = getattr(candidates[0], "finish_reason", None)
            reason_name = getattr(reason, "name", str(reason))
            if reason_name in {
                "SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII",
                "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT", "IMAGE_RECITATION",
            }:
                return None
        text = response.text
        return text if text else None
    except (ValueError, IndexError, AttributeError):
        return None

