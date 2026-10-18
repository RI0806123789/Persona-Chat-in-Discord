import io
from collections import deque
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from persona_bot.core.background import run_in_thread
from persona_bot.utils.graph_rendering import graph_font_context, save_figure_to_buffer, serialized_plot
from persona_bot.config.constants import MAX_USAGE_HISTORY


class UsageTracker:
    def __init__(self) -> None:
        # 長時間稼働でメモリと描画コストが際限なく増えないよう、
        # 直近 MAX_USAGE_HISTORY 件のみを保持するリングバッファにする。
        self.input_tokens: deque[int] = deque(maxlen=MAX_USAGE_HISTORY)
        self.output_tokens: deque[int] = deque(maxlen=MAX_USAGE_HISTORY)
        self.total_tokens: deque[int] = deque(maxlen=MAX_USAGE_HISTORY)

    def has_data(self) -> bool:
        return bool(self.total_tokens)

    @staticmethod
    def _read_token_count(usage_info: Any, attr_name: str, dict_key: str) -> int:
        """SDK オブジェクト（snake_case 属性）と REST API の dict（camelCase キー）の
        両形式から トークン数を取り出す。"""
        if isinstance(usage_info, dict):
            return int(usage_info.get(dict_key, 0) or 0)
        return int(getattr(usage_info, attr_name, 0) or 0)

    def record(self, usage_info: Any) -> None:
        self.input_tokens.append(self._read_token_count(usage_info, "prompt_token_count", "promptTokenCount"))
        self.output_tokens.append(self._read_token_count(usage_info, "candidates_token_count", "candidatesTokenCount"))
        self.total_tokens.append(self._read_token_count(usage_info, "total_token_count", "totalTokenCount"))

    def log_snapshot(
        self,
        context_label: str,
        question: str,
        attachment_summary: str,
        full_prompt: str,
        response_text: str | None,
        usage_info: Any | None,
    ) -> None:
        print("-" * 124)
        print(f"User ({context_label})：{question} / {attachment_summary}")
        print(f"Prompt text count：{len(full_prompt)}")
        if usage_info:
            self.record(usage_info)
            print(f"Prompt token count：{self.input_tokens[-1]}")
            print(f"Response token count：{self.output_tokens[-1]}")
            print(f"Total token count：{self.total_tokens[-1]}")
        print(f"AI：{response_text}")
        print("-" * 124)

    async def build_graph_buffer(self) -> io.BytesIO:
        # 3列を同じ時点でコピーする。描画待機中のrecordで系列の長さが食い違わない。
        snapshot = (list(self.input_tokens), list(self.output_tokens), list(self.total_tokens))
        return await run_in_thread(self._render_graph_buffer, *snapshot)

    @serialized_plot
    def _render_graph_buffer(self, input_tokens, output_tokens, total_tokens) -> io.BytesIO:
        with plt.rc_context(graph_font_context()):
            figure, axis = plt.subplots(figsize=(10, 6))
            x = np.arange(1, len(total_tokens) + 1)
            axis.plot(x, input_tokens, label="入力トークン数", marker="o", color="red")
            axis.plot(x, output_tokens, label="出力トークン数", marker="o", color="blue")
            axis.plot(x, total_tokens, label="合計トークン数", marker="o", color="green")
            axis.set_xlabel("利用回数")
            axis.set_ylabel("トークン数")
            axis.set_title("Gemini API 利用結果のトークン数推移")
            axis.legend()
            axis.grid(True)

            return save_figure_to_buffer(figure)
