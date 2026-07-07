import io
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


class UsageTracker:
    def __init__(self) -> None:
        self.input_tokens: list[int] = []
        self.output_tokens: list[int] = []
        self.total_tokens: list[int] = []

    def has_data(self) -> bool:
        return bool(self.total_tokens)

    def record(self, usage_info: Any) -> None:
        prompt_tokens = int(getattr(usage_info, "prompt_token_count", 0) or 0)
        response_tokens = int(getattr(usage_info, "candidates_token_count", 0) or 0)
        total_tokens = int(getattr(usage_info, "total_token_count", 0) or 0)
        self.input_tokens.append(prompt_tokens)
        self.output_tokens.append(response_tokens)
        self.total_tokens.append(total_tokens)

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

    def build_graph_buffer(self) -> io.BytesIO:
        figure, axis = plt.subplots(figsize=(10, 6))
        x = np.arange(1, len(self.total_tokens) + 1)
        matplotlib.rc("font", **{"family": "Yu Gothic"})
        axis.plot(x, self.input_tokens, label="入力トークン数", marker="o", color="red")
        axis.plot(x, self.output_tokens, label="出力トークン数", marker="o", color="blue")
        axis.plot(x, self.total_tokens, label="合計トークン数", marker="o", color="green")
        axis.set_xlabel("利用回数")
        axis.set_ylabel("トークン数")
        axis.set_title("Gemini API 利用結果のトークン数推移")
        axis.legend()
        axis.grid(True)

        buffer = io.BytesIO()
        figure.savefig(buffer, format="png")
        plt.close(figure)
        buffer.seek(0)
        return buffer

