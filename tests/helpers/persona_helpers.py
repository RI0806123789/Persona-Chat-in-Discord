"""テスト用の架空のペルソナ一覧に差し替える。"""

from pathlib import Path

from persona_bot.config import personas
from persona_bot.config.constants import VOICE_PROFILE_FALLBACK

# 応答設定の取り違えを確かめるテストが使う、2つの架空のペルソナの値。
FICTIONAL_PERSONAS = ("../prompts/prompt_default.txt", "../prompts/prompt_maid.txt")


def install_persona_catalog(test_case, *values, default=personas.FOLLOW_DEFAULT_PERSONA, voices=None):
    """指定した値だけを持つ一覧に差し替え、テスト後に元の一覧へ戻す。

    実際の prompts/ は読まない（利用者のキャラ別ファイルを検証に使わないため）。
    値を省略すると FICTIONAL_PERSONAS を使う。
    """
    voices = voices or {}
    previous = personas.current_persona_catalog()
    catalog = personas.PersonaCatalog(
        tuple(
            personas.Persona(value, Path(value).stem, voices.get(value, VOICE_PROFILE_FALLBACK))
            for value in (values or FICTIONAL_PERSONAS)
        ),
        default,
    )
    personas.set_persona_catalog(catalog)
    test_case.addCleanup(personas.set_persona_catalog, previous)
    return catalog


def install_empty_persona_catalog(test_case):
    """ペルソナを1つも作っていない状態（テンプレートだけを取得した直後）にする。"""
    previous = personas.current_persona_catalog()
    personas.set_persona_catalog(personas.PersonaCatalog())
    test_case.addCleanup(personas.set_persona_catalog, previous)
