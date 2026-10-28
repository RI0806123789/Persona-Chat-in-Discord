"""モデルの分類値と保存値を共通の許容値へ正規化する。"""

import re
from typing import Any

from persona_bot.config.constants import MEMORY_ROUTE_CATEGORIES, REMINDER_RECURRENCE_VALUES


def normalize_extracted_text(value: Any, max_chars: int) -> str:
    """AIの抽出文を1行・長さ上限内に収め、メンションとして解釈させない。"""
    if not isinstance(value, str):
        return ""
    # 改行で後続のプロンプトの節を作らせず、通知先の文字列も命令として持ち越さない。
    text = re.sub(r"\s+", " ", value).strip().replace("@", "＠")
    return text[:max_chars].rstrip()


def normalize_memory_category(value: Any) -> str:
    """記憶カテゴリの空白・大小文字を吸収し、未知の値はnoneにする。"""
    category = str(value or "none").strip().lower()
    return category if category in MEMORY_ROUTE_CATEGORIES else "none"


def normalize_recurrence(value: Any) -> str:
    """繰り返し種別を検証し、未知の値・欠落はnone（単発）にする。"""
    recurrence = str(value or "none").strip().lower()
    return recurrence if recurrence in REMINDER_RECURRENCE_VALUES else "none"
