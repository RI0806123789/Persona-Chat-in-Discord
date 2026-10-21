"""Geminiを使ったAIベースのコンテンツモデレーションサービス。

ルールベースのNGワードフィルタを補完し、スペース挿入・記号混入・
カタカナ⇔ひらがな変換・伏字・隠語などの回避テクニックにも対応する。
APIエラーやタイムアウト時は安全側に倒して通過させる（fail-open）。
"""

import asyncio
from datetime import datetime
from typing import Any

from persona_bot.config.constants import (
    MODERATION_MODEL_NAME, MODERATION_NG_WORD_SAMPLE_LIMIT, MODERATION_TIMEOUT_SECONDS,
    REMINDER_TIMEZONE,
)
from persona_bot.services.ng_word_service import NgWordFilter
from persona_bot.services.genai_client import generate_content
from persona_bot.utils.storage import extract_json_object, get_prompt_block_reason, safe_response_text
from persona_bot.utils.timeutil import format_weekday, resolve_timezone, timezone_label
from persona_bot.utils.normalization import normalize_memory_category, normalize_recurrence


# ---------------------------------------------------------------------------
# モデレーション用プロンプト
# ---------------------------------------------------------------------------

# 判定基準の共通部分（単独モデレーションと、ルーティング統合判定の両方で使う）。
_MODERATION_CRITERIA = (
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
)

_SYSTEM_PROMPT = (
    _MODERATION_CRITERIA
    + "\n## 出力形式\n"
    + "必ず以下のJSON **のみ** を返してください。説明文は不要です。\n"
    + '{"safe": true}\n'
    + "または\n"
    + '{"safe": false, "reason": "簡潔な判定理由"}\n'
)

# ルーティング統合判定用プロンプト。モデレーションと同時に「回答に必要な長期記憶の
# カテゴリ」と「リマインダー依頼かどうか」も1回の呼び出しで判定させる
# （3つの判定を1回の Gemini 呼び出しにまとめて API コストを抑える）。
# category の値は memory_service.route_category と揃えること（MEMORY_ROUTE_CATEGORIES）。
def _build_combined_system_prompt(now_text: str, timezone_label: str) -> str:
    """モデレーション＋記憶ルーティング＋リマインダー抽出の統合判定プロンプトを組み立てる。

    相対的な日時表現（「30分後」「明日の朝」）を絶対日時へ直すには基準時刻が要るため、
    現在日時をプロンプトに埋め込む（この部分だけ呼び出しごとに変わる）。
    """
    return (
        _MODERATION_CRITERIA
        + "\n## 追加タスク1: 記憶カテゴリの分類\n"
        + "同じテキストについて、返答に必要な長期記憶のカテゴリも判定してください。\n"
        + "- profile: ユーザー個人のプロフィール（居住地・趣味・性格・近況など）が必要\n"
        + "- projects: 進行中のプロジェクト・学習・制作物などの情報が必要\n"
        + "- none: 過去の記憶は不要（あいさつ・相槌・その場で完結する質問など）\n"
        + "\n## 追加タスク2: リマインダー依頼の抽出\n"
        + f"現在日時は {now_text}（{timezone_label}）です。\n"
        + "テキストが「指定した時刻に知らせてほしい」という依頼であれば、日時と内容を抽出してください。\n"
        + "- 「30分後」「明日の朝」などの相対表現は、上記の現在日時を基準に絶対日時へ直すこと。\n"
        + "- 通知の要望が読み取れる場合（「リマインドして」「知らせて」「教えて」など）のみ抽出すること。\n"
        + "  単なる予定の報告（例: 「明日は10時から会議だ」）は依頼ではないので null にすること。\n"
        + "- 依頼だが日時が特定できない場合も null にすること（推測で日時を作らない）。\n"
        + '- "at" は "YYYY-MM-DD HH:MM"（24時間表記）の形式にすること。\n'
        + '- "message" は通知に載せる短い内容だけにすること（依頼文をそのまま入れない）。\n'
        + '- "repeat" は繰り返しの種類。1回だけなら "none"、「毎日」「毎朝」なら "daily"、'
        + '「毎週◯曜」なら "weekly"、「毎月◯日」なら "monthly"。'
        + '判断できなければ "none" にすること。\n'
        + '- 繰り返しの依頼でも "at" は必須（1回目の日時）。時刻が特定できなければ null にすること。\n'
        + "\n## 出力形式\n"
        + "必ず以下のJSON **のみ** を返してください。説明文は不要です。\n"
        + '{"safe": true, "category": "profile|projects|none", "reminder": null}\n'
        + "リマインダー依頼の場合は reminder を次の形にしてください。\n"
        + '{"safe": true, "category": "none", "reminder": {"at": "2026-01-31 09:00", "message": "ゴミ出し", "repeat": "none"}}\n'
        + "不適切と判定した場合は次の形にしてください。\n"
        + '{"safe": false, "reason": "簡潔な判定理由", "category": "none", "reminder": null}\n'
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


def _build_prompt(
    text: str,
    direction: str,
    ng_words: list[str],
    system_prompt: str = _SYSTEM_PROMPT,
) -> str:
    """モデレーション用のプロンプトを組み立てる。"""
    ng_section = ""
    if ng_words:
        # プロンプトが長くなりすぎないようサンプル数を制限する。
        sample = ng_words[:MODERATION_NG_WORD_SAMPLE_LIMIT]
        ng_section = (
            "\n## 参考: NGワードリスト\n"
            + ", ".join(sample)
            + ("\n（他にもあります）" if len(ng_words) > MODERATION_NG_WORD_SAMPLE_LIMIT else "")
            + "\n"
        )

    return (
        system_prompt
        + ng_section
        + f"\n## 判定対象（{direction}）\n"
        + text
    )


def _normalize_reminder(value: Any) -> dict[str, str] | None:
    """モデルが返す reminder を検証して正規化する。形式が違えば None（依頼なし扱い）。

    日時が実在するか・未来かの判定は ReminderService 側で行う（ここは形の確認だけ）。
    repeat（繰り返し種別）は許容値リストで検証し、外れた値・欠落は "none"（単発）に
    倒す — 繰り返しの解釈を誤って延々と通知し続けるより、1回で終わる方が安全なため。
    """
    if not isinstance(value, dict):
        return None
    at = value.get("at")
    message = value.get("message")
    if not isinstance(at, str) or not isinstance(message, str):
        return None
    at = at.strip()
    message = message.strip()
    if not at or not message:
        return None
    repeat = normalize_recurrence(value.get("repeat"))
    return {"at": at, "message": message, "repeat": repeat}


# ---------------------------------------------------------------------------
# ContentModerator クラス
# ---------------------------------------------------------------------------

class ContentModerator:
    """Gemini flash-lite を使ってテキストの安全性をAI判定するモデレーター。"""

    def __init__(
        self,
        genai_client: Any,
        ng_filter: NgWordFilter,
        *,
        model_name: str = MODERATION_MODEL_NAME,
        timeout: float = MODERATION_TIMEOUT_SECONDS,
    ) -> None:
        self._genai_client = genai_client
        self._ng_filter = ng_filter
        self._model_name = model_name
        self._timeout = timeout
        # リマインダーの相対表現（「30分後」など）を絶対日時へ直させるための基準時刻に使う。
        # ここで一度だけ解決しておく — メッセージごとに組み立てると、タイムゾーンデータが
        # 無い環境では毎回例外になり、fail-open のはずのモデレーションが
        # on_message ごと落として全メッセージを無応答にしてしまう。
        self._tz = resolve_timezone(REMINDER_TIMEZONE, purpose="リマインダーの日時解釈")

    async def _judge(self, prompt: str, direction: str) -> tuple[bool, str, dict[str, Any] | None]:
        """モデレーション判定を Gemini に1回問い合わせ、結果を正規化して返す。

        check() と check_and_route() の共通部分（API 呼び出し・プロンプトブロック判定・
        テキスト取得・JSON パース・safe フラグの正規化）をここに集約する。
        追加項目（記憶カテゴリ・リマインダー）の取り出しは呼び出し側が parsed から行う。

        Returns:
            (is_safe, reason, parsed) のタプル。parsed は JSON を読めなかった場合 None。

        **この関数から例外を外へ漏らさないこと。** on_message の主経路から呼ばれるため、
        抜けた例外はそのメッセージの処理ごと落として無応答にしてしまう。
        """
        try:
            response = await generate_content(
                self._genai_client, model=self._model_name, contents=prompt,
                timeout=self._timeout, temperature=0, response_mime_type="application/json",
            )

            # Gemini がプロンプト自体をブロックした場合
            # → コンテンツが不適切だと Gemini が判断したので unsafe 扱い
            reason_name = get_prompt_block_reason(response)
            if reason_name is not None:
                print(f"[モデレーション] プロンプトブロック ({direction}): {reason_name}")
                return False, f"安全フィルタによりブロック ({reason_name})", None

            # SDKの応答差異や空候補を各呼出側で処理せず、共通入口で安全に読む。
            result_text = safe_response_text(response)
            if result_text is None:
                # 明示的なプロンプトブロックは上で判定済み。本文の欠落だけでは違反としない。
                print(f"[モデレーション] テキスト取得失敗 ({direction}) — 通過扱い")
                return True, "", None

            parsed = extract_json_object(result_text, required_keys=("safe",))
            if parsed is None:
                # パース失敗は通過扱い（fail-open）。
                print(f"[モデレーション] 応答パース失敗 — 通過扱い: {result_text!r}")
                return True, "", None

            # {"safe": "false"} のように文字列で返るケースに備えて明示的に正規化する。
            # bool("false") は True になるため、単純な bool() では誤判定する。
            is_safe = _normalize_safe_flag(parsed.get("safe", True))
            raw_reason = parsed.get("reason")
            reason = raw_reason.strip() if not is_safe and isinstance(raw_reason, str) else ""

            if not is_safe:
                print(f"[モデレーション] 不適切検出 ({direction}): {reason}")

            return is_safe, reason, parsed

        except asyncio.TimeoutError:
            print(f"[モデレーション] タイムアウト ({direction}) — 通過扱い")
            return True, "", None
        except Exception as error:
            print(f"[モデレーション] エラー ({direction}): {error}")
            return True, "", None

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

        is_safe, reason, _parsed = await self._judge(prompt, direction)
        return is_safe, reason

    async def check_and_route(self, text: str) -> tuple[bool, str, str, dict[str, str] | None]:
        """ユーザー入力のモデレーション・記憶ルーティング・リマインダー抽出を1回で行う。

        チャットの主経路（メンションのたびに走る）で、本来3回になるはずの Gemini 呼び出し
        （入力モデレーション＋記憶ルーティング＋リマインダー判定）を1回に統合して
        API コストを抑える。

        Returns:
            (is_safe, reason, category, reminder) のタプル。
            category は "profile" / "projects" / "none" のいずれか。
            reminder はリマインダー依頼なら
            {"at": "YYYY-MM-DD HH:MM", "message": ..., "repeat": "none|daily|weekly|monthly"}、
            依頼でなければ None。
            タイムアウトや例外時は安全側かつ記憶不要側に倒し (True, "", "none", None) を返す。
        """
        if not text or not text.strip():
            return True, "", "none", None

        # 「30分後」などの相対表現を解釈させるため、基準となる現在日時を渡す。
        # 曜日名（「来週の月曜」などの解釈を助ける）は timeutil の唯一のテーブルから取る
        # — ここで独自に持つと、リマインダーの表示側と表記が食い違う。
        now = datetime.now(self._tz)
        now_text = f"{now:%Y-%m-%d %H:%M}（{format_weekday(now)}曜日）"

        # ユーザー入力の判定なので、単独モデレーションと同様に NG ワードを参考提示する。
        prompt = _build_prompt(
            text,
            "ユーザー入力",
            self._ng_filter.words,
            # 基準タイムゾーン名は定数ではなく「実際に解決できた tz」から作る。
            # tzdata の無い環境では self._tz が UTC へフォールバックしており、
            # 定数をそのまま名乗ると now_text（UTC の時刻）と食い違って
            # 9時間ずれた日時でリマインダーが登録されてしまう。
            system_prompt=_build_combined_system_prompt(now_text, timezone_label(self._tz)),
        )

        is_safe, reason, parsed = await self._judge(prompt, "ユーザー入力")
        if parsed is None:
            # JSON が読めなかった場合。安全判定は _judge の結果に従い、
            # 記憶カテゴリとリマインダーは「無し」に倒す。
            return is_safe, reason, "none", None

        category = normalize_memory_category(parsed.get("category", "none"))
        # 不適切と判定した入力からはリマインダーを作らない（そのまま通知文になるため）。
        reminder = _normalize_reminder(parsed.get("reminder")) if is_safe else None
        return is_safe, reason, category, reminder


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
    text, _accepted = await screen_bot_response_with_status(
        response_text, ng_filter, moderator, log_label=log_label,
    )
    return text


async def screen_bot_response_with_status(
    response_text: str,
    ng_filter: NgWordFilter | None,
    moderator: "ContentModerator | None",
    *,
    log_label: str = "Bot応答",
) -> tuple[str, bool]:
    """表示本文と判定の成否を返し、謝罪文の記憶保存や親密度更新を防ぐ。"""
    if ng_filter is not None:
        response_text = ng_filter.mask_with_log(response_text, label=log_label)

    if moderator is not None:
        is_safe, reason = await moderator.check(response_text, direction="Bot応答")
        if not is_safe:
            print(f"{log_label}モデレーション: {reason}")
            return "申し訳ありませんが、適切な応答を生成できませんでした。", False

    return response_text, True
