def build_memory_sections(memory_category: str, memory_context: str, document_context: str = "") -> str:
    sections: list[str] = []
    if memory_context:
        sections.append(f"\n--- 選択された長期記憶 ({memory_category}) ---\n{memory_context}")
    if document_context:
        sections.append(f"\n--- 圧縮されたドキュメント要約 ---\n{document_context}")
    return "".join(sections)


def build_full_prompt(prompt: str, memory_sections: str, history_text: str, question: str) -> str:
    return (
        prompt
        + memory_sections
        + "\n--- 過去の会話履歴 ---\n"
        + history_text
        + "\n--- 今回の質問 ---\n"
        + question
    )

