"""NGワードの読み込み・判定・マスク処理を提供するサービス。

ng/NG_WORD.txt と ng/NG_WORD_private.txt から1行ずつNGワードを読み込み、
ユーザーの入力やBotの応答に含まれていないかチェックする。
"""

import re
from pathlib import Path

from persona_bot.config.constants import NG_WORD_PATH, NG_WORD_PRIVATE_PATH


def _load_words_from_file(file_path: Path) -> list[str]:
    """ファイルからNGワードを1行ずつ読み込む。空行・コメント行(#)はスキップする。

    メモ帳などで「UTF-8 (BOM 付き)」保存されたファイルも読めるよう utf-8-sig で読む。
    BOM (U+FEFF) は strip() でも消えないため、残すと1行目のコメントがNGワードとして
    登録されたり、1行目のNGワードが検知できなくなったりする。
    """
    if not file_path.exists():
        return []
    try:
        lines = file_path.read_text(encoding="utf-8-sig").splitlines()
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
        self._snapshot: tuple[list[str], re.Pattern[str] | None] = ([], None)
        self.reload()

    def reload(self) -> None:
        """NGワードリストをファイルから再読み込みする。"""
        words = load_ng_words()
        pattern = _build_pattern(words)
        # ワーカーで準備してから一度に公開し、再読込中も従来の判定を維持する。
        self._snapshot = (words, pattern)
        if words:
            print(f"NGワード: {len(words)}件 読み込み済み")

    @property
    def word_count(self) -> int:
        return len(self._snapshot[0])

    @property
    def words(self) -> list[str]:
        """NGワードのリストを返す（ContentModerator 連携用）。"""
        return list(self._snapshot[0])

    def contains_ng_word(self, text: str) -> bool:
        """テキストにNGワードが含まれているかどうかを判定する。"""
        pattern = self._snapshot[1]
        if pattern is None:
            return False
        return bool(pattern.search(text))

    def find_ng_words(self, text: str) -> list[str]:
        """テキスト中に含まれるNGワードの一覧を、重複を除いて最初に現れた順で返す。

        set で重複を除くと順序が実行ごとに変わり、ログの比較やテストが不安定になるため
        dict.fromkeys で出現順を保つ。
        """
        pattern = self._snapshot[1]
        if pattern is None:
            return []
        return list(dict.fromkeys(match.group() for match in pattern.finditer(text)))

    def mask_ng_words(self, text: str) -> str:
        """テキスト中のNGワードを伏字(●)に置換して返す。"""
        pattern = self._snapshot[1]
        if pattern is None:
            return text
        return pattern.sub(lambda m: "●" * len(m.group()), text)

    def mask_with_log(self, text: str, *, label: str) -> str:
        """NGワードが含まれていればログに出したうえでマスクし、無ければ元のテキストを返す。

        マスク仕様を変えるときに1か所で済ませるために集約している。以前、感情タグの
        正規表現が3ファイルに重複して差異が生まれ、タグがユーザーへ露出する不具合に
        つながった前例があるため、同じ再発を避ける。

        Args:
            text: 判定対象のテキスト（ユーザー入力・Bot応答・リマインダー本文など）。
            label: ログに出す文脈名（例: "Bot応答" / "自発的な会話" / "リマインダー"）。
                どの経路で検出されたのか追えるよう、呼び出し側で必ず指定すること。

        Returns:
            NGワードを含む場合はマスク済みのテキスト。含まない場合は加工しない元のテキスト。
        """
        if not self.contains_ng_word(text):
            return text
        detected = self.find_ng_words(text)
        print(f"NGワード検出 ({label}): {detected}")
        return self.mask_ng_words(text)
