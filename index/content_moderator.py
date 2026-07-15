"""Geminiを使ったAIベースのコンテンツモデレーションサービス。

ルールベースのNGワードフィルタを補完し、スペース挿入・記号混入・
カタカナ⇔ひらがな変換・伏字・隠語などの回避テクニックにも対応する。
APIエラーやタイムアウト時は安全側に倒して通過させる（fail-open）。
"""

import asyncio
from typing import Any

from ng_word_service import NgWordFilter
from storage import extract_json_object, get_prompt_block_reason


# ---------------------------------------------------------------------------
# モデレーション用プロンプト
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "あなたはDiscord Botのコンテンツモデレーターです。"
    "与えられたテキストが不適切かどうかを厳密に判定してください。\n"
    "\n## 判定基準\n"
    "- NGワードリストに含まれる語句、およびその **変形表現**\n"
    "  （スペース挿入、記号混入、全角半角変換、カタカナ⇔ひらがな変換、"
    "  ローマ字化、伏字、当て字、隠語、略語、顔文字に紛れ込ませるなど）\n"
    "- 暴力的・攻撃的・脅迫的な表現\n"
    "- 差別的・侮蔑的な表現（人種、性別、障害、出自など）\n"
    "- 性的に露骨な表現\n"
    "- 個人情報（住所、電話番号、本名など）の露出\n"
    "\n## 重要な注意\n"
    "- 日常会話や軽いジョーク、ペルソナに基づくロールプレイは **safe** と判定してください。\n"
    "- 文脈上問題のない医学用語・学術用語は **safe** と判定してください。\n"
    "- 迷った場合は **safe** と判定してください（過剰ブロックを避ける）。\n"
    "\n## 出力形式\n"
    "必ず以下のJSON **のみ** を返してください。説明文は不要です。\n"
    '{"safe": true}\n'
    "または\n"
    '{"safe": false, "reason": "簡潔な判定理由"}\n'
)


def _normalize_safe_flag(value: Any) -> bool:
    """モデルが返す "safe" フィールドを真偽値に正規化する。

    JSON上は bool が期待されるが、"false" / "no" / 0 などで返るケースがある。
    判定できない値は安全側（True）に倒す。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"false", "no", "0", "unsafe", "ng"}:
            return False
        if normalized in {"true", "yes", "1", "safe", "ok"}:
            return True
    return True


def _build_prompt(text: str, direction: str, ng_words: list[str]) -> str:
    """モデレーション用のプロンプトを組み立てる。"""
    ng_section = ""
    if ng_words:
        # プロンプトが長くなりすぎないよう先頭100語まで
        sample = ng_words[:100]
        ng_section = (
            "\n## 参考: NGワードリスト\n"
            + ", ".join(sample)
            + ("\n（他にもあります）" if len(ng_words) > 100 else "")
            + "\n"
        )

    return (
        _SYSTEM_PROMPT
        + ng_section
        + f"\n## 判定対象（{direction}）\n"
        + text
    )


# ---------------------------------------------------------------------------
# ContentModerator クラス
# ---------------------------------------------------------------------------

class ContentModerator:
    """Gemini flash-lite を使ってテキストの安全性をAI判定するモデレーター。"""

    def __init__(
        self,
        genai_module: Any,
        ng_filter: NgWordFilter,
        *,
        model_name: str = "gemini-3.1-flash-lite",
        timeout: float = 10.0,
    ) -> None:
        self._genai = genai_module
        self._ng_filter = ng_filter
        self._model_name = model_name
        self._timeout = timeout

    async def check(
        self,
        text: str,
        *,
        direction: str = "ユーザー入力",
    ) -> tuple[bool, str]:
        """テキストの安全性をAIで判定する。

        Returns:
            (is_safe, reason) のタプル。
            is_safe が True なら安全、False なら不適切。
            reason は不適切と判定された場合の理由。
        """
        if not text or not text.strip():
            return True, ""

        # Bot応答は既にNGワードフィルタ済みのため、モデレーションプロンプトに
        # NGワードを含めない。含めるとNGワード自体がGeminiの安全フィルタを
        # 発動させ、安全な応答まで誤ブロックされる。
        ng_words = self._ng_filter.words if direction != "Bot応答" else []
        prompt = _build_prompt(text, direction, ng_words)

        try:
            model = self._genai.GenerativeModel(self._model_name)
            response = await asyncio.wait_for(
                model.generate_content_async(
                    prompt,
                    generation_config={
                        "temperature": 0,
                        "response_mime_type": "application/json",
                    },
                ),
                timeout=self._timeout,
            )

            # Gemini がプロンプト自体をブロックした場合
            # → コンテンツが不適切だと Gemini が判断したので unsafe 扱い
            reason_name = get_prompt_block_reason(response)
            if reason_name is not None:
                print(f"[モデレーション] プロンプトブロック ({direction}): {reason_name}")
                return False, f"安全フィルタによりブロック ({reason_name})"

            # response.text はプロパティなので getattr のデフォルト値が
            # 使われず例外が発生するケースがある（candidates が空の場合など）
            try:
                result_text = response.text or ""
            except (ValueError, IndexError):
                # candidates が空 = Gemini が応答を生成できなかった
                # プロンプトにブロック理由がある場合は上で処理済みなので、
                # ここに来るのは原因不明のケース → unsafe 扱い
                print(f"[モデレーション] テキスト取得失敗 ({direction}) — unsafe 扱い")
                return False, "AIモデレーション応答の取得に失敗"

            parsed = extract_json_object(result_text)

            if parsed is None:
                print(f"[モデレーション] 応答パース失敗 — 通過扱い: {result_text!r}")
                return True, ""

            # {"safe": "false"} のように文字列で返るケースに備えて明示的に正規化する。
            # bool("false") は True になるため、単純な bool() では誤判定する。
            is_safe = _normalize_safe_flag(parsed.get("safe", True))
            reason = str(parsed.get("reason", "")) if not is_safe else ""

            if not is_safe:
                print(f"[モデレーション] 不適切検出 ({direction}): {reason}")

            return is_safe, reason

        except asyncio.TimeoutError:
            print(f"[モデレーション] タイムアウト ({direction}) — 通過扱い")
            return True, ""
        except Exception as error:
            print(f"[モデレーション] エラー ({direction}): {error}")
            return True, ""


async def screen_bot_response(
    response_text: str,
    ng_filter: NgWordFilter | None,
    moderator: "ContentModerator | None",
    *,
    log_label: str = "Bot応答",
) -> str:
    """Bot が生成した応答に NG ワードマスクと AI モデレーションを適用する。

    NG ワードは ● でマスクし、モデレーションで不適切と判定された場合は
    定型の謝罪文に差し替える。チャット応答と文書応答で共通の後処理。
    """
    if ng_filter is not None and ng_filter.contains_ng_word(response_text):
        detected = ng_filter.find_ng_words(response_text)
        print(f"NGワード検出 ({log_label}): {detected}")
        response_text = ng_filter.mask_ng_words(response_text)

    if moderator is not None:
        is_safe, reason = await moderator.check(response_text, direction="Bot応答")
        if not is_safe:
            print(f"{log_label}モデレーション: {reason}")
            response_text = "申し訳ありませんが、適切な応答を生成できませんでした。"

    return response_text
