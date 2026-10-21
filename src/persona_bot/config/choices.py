from discord import app_commands

from persona_bot.config.constants import DEFAULT_MODEL_NAME, DEFAULT_RESPOND_MODE


# ペルソナの選択肢は固定の一覧を持たず、prompts/ にあるファイルから作る（personas.py）。
# キャラ別のプロンプトは利用者が各自の環境で作り、Git では管理しないため。

MODEL_CHOICES = [
    app_commands.Choice(name="Gemini 2.5 Flash", value="gemini-2.5-flash"),
    app_commands.Choice(name="Gemini 3.5 Flash", value="gemini-3.5-flash"),
    app_commands.Choice(name="Gemini 3.5 Flash Lite", value=DEFAULT_MODEL_NAME),
]

RESPOND_MODE_CHOICES = [
    app_commands.Choice(name="すべての発言に反応", value="all"),
    app_commands.Choice(name="メンションのみに反応", value=DEFAULT_RESPOND_MODE),
]

# /functions → 📋 会話要約 の期間選択肢。value は「何時間さかのぼるか」を表す（1か月=30日=720時間）。
SUMMARY_PERIOD_CHOICES = [
    app_commands.Choice(name="1時間", value=1),
    app_commands.Choice(name="12時間", value=12),
    app_commands.Choice(name="1日", value=24),
    app_commands.Choice(name="1週間", value=168),
    app_commands.Choice(name="1か月", value=720),
]

# 自発的な会話の無会話しきい値の選択肢。value は「何時間会話が無ければ話題提供するか」。
AUTO_TOPIC_INTERVAL_CHOICES = [
    app_commands.Choice(name="1時間", value=1),
    app_commands.Choice(name="3時間", value=3),
    app_commands.Choice(name="6時間", value=6),
    app_commands.Choice(name="12時間", value=12),
    app_commands.Choice(name="24時間", value=24),
]



MODEL_DISPLAY_NAMES = {choice.value: choice.name for choice in MODEL_CHOICES}
RESPOND_MODE_DISPLAY_NAMES = {choice.value: choice.name for choice in RESPOND_MODE_CHOICES}
AUTO_TOPIC_INTERVAL_DISPLAY_NAMES = {choice.value: choice.name for choice in AUTO_TOPIC_INTERVAL_CHOICES}


