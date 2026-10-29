
import logging
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


logger = logging.getLogger(__name__)


def load_json_file(path: Path, default_value: dict[str, Any]) -> dict[str, Any]:
    """未作成なら初期値、破損時は原本の退避に成功した場合だけ初期値を返す。

    手で直すファイル（projects.json・blacklist.json）は、メモ帳の「UTF-8 (BOM 付き)」で
    保存されうる。先頭の BOM は json.loads が拒否し、正しい中身ごと破損扱いで退避して
    記憶を空にしてしまうため、utf-8-sig で読んで BOM だけを吸収する（BOM 無しも同じ結果）。
    書き込みは従来どおり BOM 無しの UTF-8 のまま。
    """
    try:
        loaded = json.loads(path.read_text(encoding="utf-8-sig"))
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
        logger.error(f'JSON読み込みエラー (<PATH>): {reason} 退避できないため処理を中止します。', exc_info=True)
        raise
    logger.warning(f'JSON読み込みエラー (<PATH>): {reason} <PATH> へ退避し、既定値で再開します。')
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
                logger.error(f'JSON一時ファイルの削除に失敗しました (<PATH>)。', exc_info=True)


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


# SDK が thought=True やコード・関数呼び出しのパートとして返すものは response.text の時点で除かれる。
# 以下は、それらが目印を失って通常の text として返ったときの見出し。行全体がこの見出しだけの行
# （コードフェンスの開始記号つきを含む）に限り、文中に現れる同じ語には反応しない。
_INTERNAL_TOOL_LINE_PATTERN = re.compile(r"^[ \t]*(?:`{3,}[ \t]*)?(?:tool_code|tool_outputs)[ \t]*\r?$", re.MULTILINE)
# 見出しが欠けても、検索ツールの呼び出し文が行頭にあれば内部形式とみなす。
_INTERNAL_SEARCH_CALL_PATTERN = re.compile(r"^[ \t]*print\([ \t]*google_search\.", re.MULTILINE)
# 「thought」は一般の英文にも現れるため、本文の先頭行がこの語だけの場合に限る。
_INTERNAL_THOUGHT_HEAD_PATTERN = re.compile(r"\A\s*(?:`{3,}[ \t]*)?thought[ \t]*\r?(?:\n|\Z)")
# 原因の追跡用に数える応答パートの種類（SDK の Part の属性名）。
_RESPONSE_PART_KINDS = (
    "text", "executable_code", "code_execution_result", "function_call", "function_response",
    "inline_data", "file_data",
)


def contains_internal_response_text(text: str) -> bool:
    """検索ツールの呼び出しや思考の出力形式が、通常の本文へ紛れ込んでいるかを判定する。

    思考と最終回答の境目は本文から確実に切り分けられないため、呼び出し側は削らずに応答ごと扱う。
    一般の文章に現れる「thought」という語や、通常のコード例には反応しない。
    """
    return bool(
        _INTERNAL_THOUGHT_HEAD_PATTERN.match(text)
        or _INTERNAL_TOOL_LINE_PATTERN.search(text)
        or _INTERNAL_SEARCH_CALL_PATTERN.search(text)
    )


def describe_response_parts(response: Any) -> str:
    """原因の追跡用に、本文を含めず終了理由とパートの種類・件数だけを要約する。"""
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return "候補なし"
    candidate = candidates[0]
    reason = getattr(candidate, "finish_reason", None)
    reason_name = getattr(reason, "name", None) or ("不明" if reason is None else str(reason))
    counts: dict[str, int] = {}
    for part in getattr(getattr(candidate, "content", None), "parts", None) or []:
        for kind in _RESPONSE_PART_KINDS:
            if getattr(part, kind, None) is None:
                continue
            # 思考のパートも text を持つため、thought フラグで公開本文と分けて数える。
            label = "thought" if kind == "text" and getattr(part, "thought", None) else kind
            counts[label] = counts.get(label, 0) + 1
    parts = "、".join(f"{label}×{count}" for label, count in counts.items()) or "なし"
    return f"終了理由: {reason_name}、パート: {parts}"

