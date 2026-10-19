"""Geminiの本文へ混入した内部形式の判定と、本文を含めない診断の要約を架空の応答で検証する。"""

from contextlib import contextmanager
import logging
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from google.genai import types

from persona_bot.utils.storage import (
    contains_internal_response_text,
    describe_response_parts,
    safe_response_text,
)

# Issue #167 の再現に使った架空の応答（検索の呼び出し・思考・最終回答が通常の text に連続する）。
LEAKED_TEXT = (
    'tool_code\nprint(google_search.search(queries=["架空の天気"]))\n\n'
    "thought\n\nI should answer the fictional question in character.\n\n"
    "架空の最終回答です。"
)


@contextmanager
def quiet_sdk_warnings():
    """非テキストのパートを含む応答で SDK が出す警告を、版による有無に依存せず抑える。"""
    logging.disable(logging.WARNING)
    try:
        yield
    finally:
        logging.disable(logging.NOTSET)


def text_response(*parts: types.Part, finish_reason=types.FinishReason.STOP) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(candidates=[types.Candidate(
        finish_reason=finish_reason, content=types.Content(role="model", parts=list(parts)),
    )])


class InternalResponseTextTests(unittest.TestCase):
    def test_internal_formats_in_plain_text_are_detected(self) -> None:
        cases = {
            "検索の呼び出し＋思考＋最終回答": LEAKED_TEXT,
            "内部形式だけ": 'tool_code\nprint(google_search.search(queries=["架空の語"]))',
            "コードフェンス付きの呼び出し": "```tool_code\nprint(google_search.search(queries=['架空']))\n```\n架空の回答",
            "前置きの後の検索結果": "架空の前置きです。\n```tool_outputs\n[架空の検索結果]\n```\n架空の回答",
            "前置きの後の呼び出し": '調べてみるね！\ntool_code\nprint(google_search.search(queries=["架空"]))\n架空の回答',
            "見出しの無い呼び出し": 'print(google_search.search(queries=["架空"]))\n\n架空の回答',
            "先頭の思考": "thought\nThe user asks a fictional question.\n\n架空の回答です。",
            "空行とCRLFの後の思考": "\r\n  thought\r\nI should reply.\r\n架空の回答",
            "CRLFの呼び出し見出し": "架空の前置き\r\ntool_code\r\nprint(google_search.search(queries=['架空']))\r\n",
        }
        for name, text in cases.items():
            with self.subTest(name=name):
                self.assertTrue(contains_internal_response_text(text))

    def test_normal_answers_words_and_code_examples_are_kept(self) -> None:
        cases = {
            "日本語": "今日は架空の晴れだよ！\n散歩日和だね。",
            "英文の thought": "I thought about your question.\nHere is my fictional answer.",
            "文頭の大文字見出し": "Thought\nThis is a heading in a fictional answer.",
            "文中の単独行": "think の過去形はこれだよ。\nthought\n覚えておいてね。",
            "先頭の派生語": "thoughtful な回答を心がけるね。",
            "先頭の語＋記号": "thought: 架空のメモです。",
            "Pythonのコード例": '```python\nprint("架空の出力")\n```\nこれで表示できるよ。',
            "文中のインラインコード": "`tool_code` という語は架空の説明です。",
            "文中の関数名": "google_search.search という関数名は架空です。",
            "行頭以外の呼び出し": '例: print(google_search.search(queries=["架空"])) のように書きます。',
            "見出しを含む別の語": "my_tool_code\ntool_code_example\n架空の変数名です。",
        }
        for name, text in cases.items():
            with self.subTest(name=name):
                self.assertFalse(contains_internal_response_text(text))

    def test_structured_thought_and_code_parts_stay_out_of_public_text(self) -> None:
        # SDK が構造化して返す思考・コード・関数呼び出しは本文に含まれず、判定の対象にもならない。
        response = text_response(
            types.Part(text=LEAKED_TEXT, thought=True),
            types.Part(executable_code=types.ExecutableCode(
                code='print(google_search.search(queries=["架空"]))', language=types.Language.PYTHON,
            )),
            types.Part(function_call=types.FunctionCall(name="google_search", args={"query": "架空"})),
            types.Part(text="架空の最終回答です。"),
        )
        with quiet_sdk_warnings():
            text = safe_response_text(response)
        self.assertEqual(text, "架空の最終回答です。")
        self.assertFalse(contains_internal_response_text(text))

    def test_plain_text_leak_is_returned_by_sdk_and_detected(self) -> None:
        # 目印を失った内部形式は SDK の本文抽出では除けないため、本文の検証で見分ける。
        text = safe_response_text(text_response(types.Part(text=LEAKED_TEXT)))
        self.assertEqual(text, LEAKED_TEXT)
        self.assertTrue(contains_internal_response_text(text))


class ResponsePartsDescriptionTests(unittest.TestCase):
    def test_description_counts_part_kinds_without_text(self) -> None:
        response = text_response(
            types.Part(text="架空の内部思考", thought=True),
            types.Part(text=LEAKED_TEXT),
            types.Part(text="架空の続き"),
            types.Part(executable_code=types.ExecutableCode(code="print('架空のコード')", language=types.Language.PYTHON)),
            types.Part(function_call=types.FunctionCall(name="google_search", args={"query": "架空の検索語"})),
        )
        description = describe_response_parts(response)
        self.assertEqual(
            description,
            "終了理由: STOP、パート: thought×1、text×2、executable_code×1、function_call×1",
        )
        for secret in ("架空の内部思考", "架空の天気", "I should answer", "架空の最終回答", "架空のコード", "架空の検索語"):
            self.assertNotIn(secret, description)

    def test_description_handles_missing_candidates_parts_and_reason(self) -> None:
        self.assertEqual(describe_response_parts(types.GenerateContentResponse()), "候補なし")
        self.assertEqual(describe_response_parts(object()), "候補なし")
        empty = types.GenerateContentResponse(candidates=[types.Candidate()])
        self.assertEqual(describe_response_parts(empty), "終了理由: 不明、パート: なし")


if __name__ == "__main__":
    unittest.main()
