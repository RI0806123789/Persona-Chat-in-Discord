"""記念日の保存とお祝いで共用する形式の検証。"""

from datetime import date
from typing import Any


def parse_anniversary(entry: Any) -> tuple[str, int, int] | None:
    """有効なラベルと月日を返す。不正な要素は新規保存・お祝いの対象にしない。"""
    if not isinstance(entry, dict):
        return None
    label = entry.get("label")
    month = entry.get("month")
    day = entry.get("day")
    if not isinstance(label, str) or not label.strip() or type(month) is not int or type(day) is not int:
        return None
    try:
        # 年を持たない記念日のため、うるう年で検証して2月29日も受け付ける。
        date(2000, month, day)
    except (ValueError, OverflowError):
        return None
    return label.strip(), month, day
