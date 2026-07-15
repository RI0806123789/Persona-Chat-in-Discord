"""NGワードの読み込み・判定・マスク処理を提供するサービス。

ng/NG_WORD.txt と ng/NG_WORD_private.txt から1行ずつNGワードを読み込み、
ユーザーの入力やBotの応答に含まれていないかチェックする。
"""

import re
from pathlib import Path

from constants import NG_WORD_PATH, NG_WORD_PRIVATE_PATH


def _load_words_from_file(file_path: Path) -> list[str]:
    """ファイルからNGワードを1行ずつ読み込む。空行・コメント行(#)はスキップする。"""
    if not file_path.exists():
        return []
    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()
        return [
            line.strip()
            for line in lines
            if line.strip() and not line.strip().startswith("#")
        ]
    except Exception as error:
        print(f"NGワード読み込みエラー ({file_path.name}): {error}")
        return []


def load_ng_words() -> list[str]:
    """NG_WORD.txt と NG_WORD_private.txt の両方からNGワードを読み込み、統合して返す。"""
    public_words = _load_words_from_file(NG_WORD_PATH)
    private_words = _load_words_from_file(NG_WORD_PRIVATE_PATH)
    # 重複を除去しつつ順序を維持
    seen: set[str] = set()
    merged: list[str] = []
    for word in public_words + private_words:
        lower = word.lower()
        if lower not in seen:
            seen.add(lower)
            merged.append(word)
    return merged


def _build_pattern(ng_words: list[str]) -> re.Pattern[str] | None:
    """NGワードリストから大文字小文字を無視した正規表現パターンを構築する。

    `|` は左の選択肢から順にマッチするため、長い語を先に並べて最長一致を
    優先する（短い語が先だと「ワード」と「ワード表現」の両方がある場合に
    「ワード表現」が部分マスクされてしまう）。
    """
    if not ng_words:
        return None
    escaped = [re.escape(word) for word in sorted(ng_words, key=len, reverse=True)]
    return re.compile("|".join(escaped), re.IGNORECASE)


class NgWordFilter:
    """NGワードのフィルタリングを行うクラス。"""

    def __init__(self) -> None:
        self._ng_words: list[str] = []
        self._pattern: re.Pattern[str] | None = None
        self.reload()

    def reload(self) -> None:
        """NGワードリストをファイルから再読み込みする。"""
        self._ng_words = load_ng_words()
        self._pattern = _build_pattern(self._ng_words)
        if self._ng_words:
            print(f"NGワード: {len(self._ng_words)}件 読み込み済み")

    @property
    def word_count(self) -> int:
        return len(self._ng_words)

    @property
    def words(self) -> list[str]:
        """NGワードのリストを返す（ContentModerator 連携用）。"""
        return list(self._ng_words)

    def contains_ng_word(self, text: str) -> bool:
        """テキストにNGワードが含まれているかどうかを判定する。"""
        if self._pattern is None:
            return False
        return bool(self._pattern.search(text))

    def find_ng_words(self, text: str) -> list[str]:
        """テキスト中に含まれるNGワードの一覧を返す。"""
        if self._pattern is None:
            return []
        return list({match.group() for match in self._pattern.finditer(text)})

    def mask_ng_words(self, text: str) -> str:
        """テキスト中のNGワードを伏字(●)に置換して返す。"""
        if self._pattern is None:
            return text
        return self._pattern.sub(lambda m: "●" * len(m.group()), text)
