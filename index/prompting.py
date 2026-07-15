def build_memory_sections(memory_category: str, memory_context: str, document_context: str = "") -> str:
    sections: list[str] = []
    if memory_context:
        sections.append(f"\n--- 選択された長期記憶 ({memory_category}) ---\n{memory_context}")
    if document_context:
        sections.append(f"\n--- 圧縮されたドキュメント要約 ---\n{document_context}")
    return "".join(sections)


def build_full_prompt(prompt: str, memory_sections: str, history_text: str, question: str, affinity_prompt: str = "") -> str:
    return (
        prompt
        + "\n"
        + affinity_prompt
        + memory_sections
        + "\n--- 過去の会話履歴 ---\n"
        + history_text
        + "\n--- 今回の質問 ---\n"
        + question
    )


def build_auto_topic_prompt(persona_prompt: str, memory_sections: str, history_text: str) -> str:
    """無会話チャンネルへの自発的な話題提供プロンプトを組み立てる。

    過去の会話履歴・長期記憶から話題を1つ選ばせ、短い問いかけで会話を
    再開させる。誰への返信でもないため感情タグは明示的に禁止する。
    """
    return (
        persona_prompt
        + "\n\n--- タスク: 自発的な話題提供 ---\n"
        + "このチャンネルではしばらく誰も発言していません。あなたの方から自然に会話を切り出してください。\n"
        + "\n## 厳守する出力ルール\n"
        + "- 下の「過去の会話履歴」や「長期記憶」に出てきた話題を1つ選び、その続き・近況確認・関連する新しい切り口で話しかけること。\n"
        + "- 過去の話題がどうしても見つからない場合のみ、相手が答えやすい軽い雑談を振ること。\n"
        + "- 過去の発言をそのまま繰り返さないこと。\n"
        + "- 1〜3文程度の短い話しかけにすること。長い説明や要約はしない。\n"
        + "- 相手が返事しやすいよう、問いかけで締めくくること。\n"
        + "- 感情タグ（[V:.., A:..] の形式）は絶対に付けないこと。\n"
        + memory_sections
        + "\n--- 過去の会話履歴 ---\n"
        + history_text
        + "\n--- 会話履歴ここまで ---\n"
        + "\n上記を踏まえて、あなたからの話しかけの一言だけを出力してください。"
    )


def build_summary_prompt(persona_prompt: str, period_label: str, source_text: str) -> str:
    """会話ログを議事録として要約するためのプロンプトを組み立てる。

    正確さ（誰が何を言ったか）を最優先しつつ、冒頭と締めだけペルソナの口調を
    残す構成。感情タグの付与は明示的に禁止する。
    """
    return (
        persona_prompt
        + "\n\n--- タスク: 会話の議事録作成 ---\n"
        + f"以下は Discord チャンネルでの直近「{period_label}」の会話ログです。"
        + "これを議事録として要約してください。\n"
        + "\n## 厳守する出力ルール\n"
        + "- 正確さを最優先する。「誰が」発言したかを絶対に取り違えないこと。発言者名はログのまま使う。\n"
        + "- 話題ごとに整理し、時系列の流れがわかるようにする。\n"
        + "- 決定事項・TODO・未解決の論点があれば明記する。該当が無ければ「なし」と書く。\n"
        + "- 冒頭と締めの一言だけ、あなたのペルソナの口調で書く。議事録本編は正確で読みやすい中立的な文体にする。\n"
        + "- 感情タグ（[V:.., A:..] の形式）は絶対に付けないこと。\n"
        + "- 会話に無い情報を創作しないこと。発言が少ない場合は無理に内容を膨らませない。\n"
        + "- Markdown 記法は使わないこと。特に見出しの「#」「##」「###」「####」や「**強調**」は Discord で見づらくなるため禁止。\n"
        + "- 構造はプレーンテキストの記号で表す。大見出しは行頭に「■」、箇条書きは行頭に「・」を使う。\n"
        + "\n## 出力フォーマット（この形を厳守。Markdown記号は使わない）\n"
        + "（ここにペルソナ口調の導入を1〜2行）\n"
        + "\n■ 1. 〈話題の見出し〉\n"
        + "・〈発言者〉: 要点\n"
        + "\n■ 2. 〈話題の見出し〉\n"
        + "・…\n"
        + "\n■ 決定事項\n"
        + "・…（無ければ「なし」）\n"
        + "\n■ TODO\n"
        + "・〈担当者〉: …（無ければ「なし」）\n"
        + "\n■ 保留・未解決\n"
        + "・…（無ければ「なし」）\n"
        + "\n（ここにペルソナ口調の締めを1〜2行）\n"
        + "\n--- 会話ログ ---\n"
        + source_text
        + "\n--- 会話ログここまで ---\n"
        + "\n上記を議事録として要約してください。"
    )

