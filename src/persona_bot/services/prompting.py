"""Gemini へ渡すプロンプト文面の置き場所。

プロンプトの組み立てはこのモジュールに集約する。ただしモデレーション用プロンプト
(`content_moderator.py`) だけは判定ロジックと密結合なので例外的にそちらへ残している。

プロンプト文面はモデル出力の「形式契約」そのもの（JSON のキー名や出力フォーマットの
指定）であり、1 文字変えるだけで呼び出し側の `extract_json_object` によるパースが
黙って失敗する。文面を変更するときは必ず呼び出し側のパース処理も合わせて確認すること。

このモジュールは他モジュールを一切 import しない（各サービスから import されるため、
サービス側を import し返すと循環参照になる）。定数を含め、必要な値はすべて引数で受け取る。
"""


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

    共有チャンネルの会話履歴から話題を1つ選ばせ、短い問いかけで会話を
    再開させる。誰への返信でもないため感情タグは明示的に禁止する。
    """
    return (
        persona_prompt
        + "\n\n--- タスク: 自発的な話題提供 ---\n"
        + "このチャンネルではしばらく誰も発言していません。あなたの方から自然に会話を切り出してください。\n"
        + "\n## 厳守する出力ルール\n"
        + "- 下の「過去の会話履歴」に出てきた話題を1つ選び、その続き・近況確認・関連する新しい切り口で話しかけること。\n"
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


def build_anniversary_prompt(persona_prompt: str, user_display_name: str, label: str, history_text: str) -> str:
    """記念日・誕生日の自動お祝いメッセージ生成プロンプトを組み立てる。

    自発的な会話（無会話への話題提供）と同じくチャンネル履歴のみを材料にし、
    個人の長期記憶（profile）はここでは渡さない（プライバシー保護のため、
    共有チャンネルには「今日が記念日である」という事実以上の個人情報を出さない）。
    """
    return (
        persona_prompt
        + "\n\n--- タスク: 記念日のお祝い ---\n"
        + f"今日は {user_display_name} さんの「{label}」です。あなたから、このチャンネルでお祝いの言葉をかけてください。\n"
        + "\n## 厳守する出力ルール\n"
        + f"- 冒頭で {user_display_name} さんに呼びかけ、「{label}」であることに触れてお祝いすること。\n"
        + "- 1〜3文程度の短いお祝いメッセージにすること。長い説明はしない。\n"
        + "- 過去の会話履歴に関連する話題があれば軽く絡めてもよいが、必須ではない。\n"
        + "- 感情タグ（[V:.., A:..] の形式）は絶対に付けないこと。\n"
        + "\n--- 過去の会話履歴（参考。無くてもよい） ---\n"
        + history_text
        + "\n--- 履歴ここまで ---\n"
        + "\n上記を踏まえて、お祝いメッセージの一言だけを出力してください。"
    )


# --- 自律行動（ボイス/ステージでの「配信」）用 ---
#
# 3つのプロンプト（第一声・独り言・コメント応答）に共通する出力ルール。
# いずれも読み上げ（TTS）を通ってボイスチャンネルで喋られるため、
# 「声で聞いて分かる短さ・書式」に揃える必要がある。文面を1か所にまとめて
# おかないと、片方だけ長さや禁止事項が緩んで喋り方がちぐはぐになる。
#
# ・長さ: constants.TTS_MAX_CHARS（既定200文字）を超えた分は読み上げで
#   打ち切られる。文字数そのものはここに書かず「2〜3文」で伝える
#   （このモジュールは constants を import しない規約のため）。
# ・記号: Markdown・箇条書き・URL は読み上げると雑音になる。
# ・感情タグ: 特定の相手への返信ではないので affinity は更新しない。
#   混ざるとそのまま声に出てしまうため明示的に禁止する。
_AUTONOMOUS_SPEECH_RULES = (
    "\n## 厳守する出力ルール（音声で読み上げられます）\n"
    "- 2〜3文の短い発言にすること。長い説明・列挙はしない。\n"
    "- 声に出して自然な話し言葉にすること。Markdown記法・箇条書き・見出し・URL・"
    "顔文字の羅列は使わない。\n"
    "- 絵文字は使わないか、使っても1つまでにすること。\n"
    "- 感情タグ（[V:.., A:..] の形式）は絶対に付けないこと。\n"
    "- 発言そのものだけを出力すること。ト書き・状況説明・自分の名前の前置きは書かない。\n"
)


def build_autonomous_opening_prompt(persona_prompt: str, channel_name: str, history_text: str) -> str:
    """自律行動の第一声（配信開始の挨拶と会話相手の募集）を組み立てる。

    ボットが自分からボイス/ステージチャンネルへ入った直後に1回だけ使う。
    「誰か話し相手が欲しい」という意図をペルソナの口調で伝えさせるのが目的
    （固定文だけだとキャラクターが死ぬため、看板の文言とは別に生成する）。
    """
    return (
        persona_prompt
        + "\n\n--- タスク: 自分から始めた通話の第一声 ---\n"
        + f"あなたは今、自分の意志でボイスチャンネル「{channel_name}」に入りました。"
        + "まだ誰も来ていないかもしれませんが、話し相手が欲しくて待っています。\n"
        + "配信を始める人のように、聞いている人へ向けた第一声を言ってください。\n"
        + "\n## この発言に必ず含めること\n"
        + "- 自分から来たこと（誰かに呼ばれたのではないこと）。\n"
        + "- 話し相手を待っていること、気軽に話しかけてほしいこと。\n"
        + _AUTONOMOUS_SPEECH_RULES
        + "\n--- このチャンネルの過去の会話（参考。無くてもよい） ---\n"
        + history_text
        + "\n--- 履歴ここまで ---\n"
        + "\n上記を踏まえて、第一声だけを出力してください。"
    )


def build_autonomous_monologue_prompt(persona_prompt: str, history_text: str, turn_count: int) -> str:
    """自律行動中の独り言を組み立てる。

    誰も反応していない時間帯でも配信を続けるための「間をつなぐ一言」。
    直前までの自分の発言は history_text に入っているため、同じ話を
    繰り返さないことを明示的に禁止する（同じ材料から生成するため、
    禁止しないと毎回ほぼ同じ独り言になる）。

    Args:
        turn_count: これまでに喋った独り言の回数。話し始めか、間が空いて
            きた頃かで話題の選び方を変えさせるために渡す。
    """
    return (
        persona_prompt
        + "\n\n--- タスク: 通話中の独り言 ---\n"
        + "あなたは今、自分から入ったボイスチャンネルで話し相手を待っています。"
        + f"これはこの通話で {turn_count + 1} 回目の独り言です。\n"
        + "誰も返事をしていない時間をつなぐように、ひとりごとを言ってください。\n"
        + "\n## 話題の選び方\n"
        + "- 下の会話履歴に出てきた話題の続き、今日の気分、ふと思ったこと、"
        + "誰かに聞いてみたいことなど、あなたが話したいことを自由に選ぶこと。\n"
        + "- **履歴にある自分の発言と同じ話題・同じ言い回しを繰り返さないこと。**"
        + "毎回ちがう切り口にすること。\n"
        + "- ときどきは、聞いている人が答えやすい問いかけで終えてもよい。\n"
        + _AUTONOMOUS_SPEECH_RULES
        + "\n--- このチャンネルの会話（直前までのあなたの発言を含む） ---\n"
        + history_text
        + "\n--- 履歴ここまで ---\n"
        + "\n上記を踏まえて、独り言だけを出力してください。"
    )


def build_autonomous_reply_prompt(
    persona_prompt: str,
    commenter_name: str,
    comment: str,
    history_text: str,
) -> str:
    """自律行動中に届いたコメントへの応答を組み立てる。

    リスナーは「傍観者」であり、1対1の会話ではなく配信のコメント欄という
    位置づけ（idea.md）。そのため配信者がコメントを拾うときの距離感で
    応答させる。長期記憶（profile）は渡さない —— 共有チャンネルへの配信で
    特定ユーザーの私的な記憶を読み上げると本人以外にも漏れるため
    （自発的な話題提供・記念日のお祝いと同じ方針）。
    """
    return (
        persona_prompt
        + "\n\n--- タスク: 配信中のコメントへの応答 ---\n"
        + "あなたは今、自分から入ったボイスチャンネルで話しています。"
        + "聞いている人からコメントが届きました。配信者がコメントを拾うように応答してください。\n"
        + "\n## 応答のしかた\n"
        + f"- コメントをくれた「{commenter_name}」さんに呼びかけてから応えること。\n"
        + "- コメントの内容にきちんと答えること。質問なら答え、感想なら受け止めて返すこと。\n"
        + "- 会話を続けたいので、必要に応じて聞き返してもよい。\n"
        + _AUTONOMOUS_SPEECH_RULES
        + "\n--- このチャンネルの会話 ---\n"
        + history_text
        + "\n--- 履歴ここまで ---\n"
        + f"\n--- 今回のコメント（{commenter_name} さん） ---\n"
        + comment
        + "\n\n上記のコメントへの応答だけを出力してください。"
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


def build_router_prompt(question: str) -> str:
    """長期記憶のカテゴリ（profile / projects / none）を判定させるプロンプトを組み立てる。

    呼び出し側は `extract_json_object` で `{"category": "..."}` をパースするため、
    出力形式の指定を書き換えないこと。
    """
    return (
        "以下の質問に答えるために、ユーザーのプロフィール情報(profile)、進行中のプロジェクト情報(projects)、"
        "または過去の情報は不要(none)のどれが適切ですか？ JSONで category だけを返してください。\n"
        f"質問: {question}\n"
        '出力形式: {"category": "profile|projects|none"}'
    )


def build_memory_compaction_prompt(
    pending_text: str,
    profile_text: str,
    projects_text: str,
    anniversary_key: str,
) -> str:
    """一時ログ（pending_log）を長期記憶へ圧縮統合させるプロンプトを組み立てる。

    Args:
        pending_text: 圧縮対象の一時ログを整形したテキスト。
        profile_text: 既存のプロフィール記憶（JSON 文字列）。
        projects_text: 既存のプロジェクト記憶（JSON 文字列）。文書要約
            （document_summaries）は呼び出し側が除いてから渡す。この指示の
            「不要な古い情報は削除」で要約が消えないようにするため。
        anniversary_key: 記念日を保存させる profile 内のキー名
            （呼び出し側が `constants.ANNIVERSARY_PROFILE_KEY` を渡す。
            このモジュールは constants を import しないため引数で受け取る）。

    出力は `{"profile": {...}, "projects": {...}}` 形式で、呼び出し側が
    `extract_json_object` でパースする。記念日を構造化リストで保存させる指示は
    記念日の自動お祝い機能の前提なので削らないこと。
    """
    return (
        "次の一時ログと既存の記憶JSONから、永続化すべき重要な事実だけを抽出し、"
        "profile と projects を整理したJSONを返してください。不要な古い情報は削除してください。"
        "出力は必ずJSONのみで、形式は {\"profile\": {...}, \"projects\": {...}} にしてください。\n\n"
        "【記念日・誕生日の扱い】一時ログの中でユーザーが誕生日や記念日など"
        "「毎年同じ月日に繰り返し祝いたいイベント」について話していた場合、"
        f'profile 内に "{anniversary_key}" というキーで次の形式のリストとして保存してください: '
        '[{"label": "呼び名(例: 誕生日)", "month": 1〜12の整数, "day": 1〜31の整数}]。'
        "既存の記念日エントリがあれば失わないようマージし（同じ label は上書き、それ以外は保持）、"
        "月日が確定できない情報や単発の出来事（例: 今日の予定）は記念日として保存しないでください。\n\n"
        f"--- 一時ログ (pending_log) ---\n{pending_text}\n\n"
        f"--- プロフィール記憶 (profile) ---\n{profile_text}\n\n"
        f"--- プロジェクト記憶 (projects) ---\n{projects_text}"
    )


def build_document_summary_prompt(document_name: str) -> str:
    """添付文書を構造化 JSON へ要約させるプロンプトを組み立てる。

    呼び出し側は `[prompt, uploaded_file]` の形でアップロード済みファイルと一緒に
    Gemini へ渡し、`summary` / `key_points` / `warnings` / `usage_notes` の各キーを
    読み出す。キー名を変えるとその読み出しが黙って空になる。
    """
    return (
        "このドキュメントの核となる要点とルールを、日本語で構造化して抽出してください。"
        "出力は必ずJSONのみで、次の形式にしてください: "
        '{"summary":"...","key_points":["..."],"warnings":["..."],"usage_notes":["..."]}'
        f"\nファイル名: {document_name}"
    )


def build_stats_analysis_prompt(samples: list[str], topic_count: int) -> str:
    """会話統計の「よく話す話題」と「ビッグファイブ性格特性」を推定させるプロンプトを組み立てる。

    Args:
        samples: そのユーザーの直近発言サンプル（1 件ずつ箇条書きへ整形する）。
        topic_count: 話題の最大件数（呼び出し側が `constants.STATS_TOPIC_COUNT` を渡す）。

    出力の `topics` / `big_five` / `personality` は描画側がそのままキー参照するため、
    キー名と構造を変えないこと。
    """
    joined = "\n".join(f"- {s}" for s in samples)
    return f"""以下は、あるユーザーが Discord でボットに送った発言の一覧です。
この発言だけを根拠に、(1) よく話す話題 と (2) ビッグファイブ性格特性の推定 を行ってください。

発言一覧:
---
{joined}
---

出力は次の JSON のみ。前後に説明文やコードフェンスを付けないでください。
{{
  "topics": [{{"label": "話題名(短く)", "weight": 0〜1の重要度}}],
  "big_five": {{
    "openness": 0〜100の整数,
    "conscientiousness": 0〜100の整数,
    "extraversion": 0〜100の整数,
    "agreeableness": 0〜100の整数,
    "neuroticism": 0〜100の整数
  }},
  "personality": "性格の総評（推定である旨を含め日本語で1〜2文）"
}}

topics は重要度の高い順に最大 {topic_count} 件まで。
発言が少ない場合でも無理に断定せず、控えめに推定してください。"""


def build_affinity_prompt(affinity: int, valence: int, arousal: int) -> str:
    """親密度・感情の現在値をプロンプトへ差し込むブロックを組み立てる。

    末尾に `[V:X, A:Y]` 形式の感情タグを付けるようモデルへ指示する部分を含む。
    このタグは `affinity_service.extract_affinity_changes` が抽出・除去する前提なので、
    タグの書式を変えるときは同モジュールの正規表現も合わせて直すこと。

    Args:
        affinity: このユーザーへの親密度（0〜100）。
        valence: 現在の機嫌（0〜100）。
        arousal: 現在のテンション（0〜100）。
    """
    return f"""
【現在のあなたの状態】
・このユーザーへの親密度(Affinity): {affinity}/100
・現在の機嫌(Valence): {valence}/100
・現在のテンション(Arousal): {arousal}/100

上記を踏まえて返答を生成してください。
また、今回の返答によって生じた感情の変化量を、返答の末尾に必ず `[V:X, A:Y]` の形式で追記してください。
Xは機嫌の変動量、Yはテンションの変動量です。（例: `[V:+5, A:-10]` や `[V:0, A:+2]`）
"""

