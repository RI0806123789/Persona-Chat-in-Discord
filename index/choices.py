from discord import app_commands

from constants import DEFAULT_MODEL_NAME


PERSONA_CHOICES = [
    app_commands.Choice(name="ツンデレ", value="../prompts/prompt_tsun.txt"),
    app_commands.Choice(name="ヤンデレ", value="../prompts/prompt_devotion.txt"),
    app_commands.Choice(name="メイド", value="../prompts/prompt_maid.txt"),
    app_commands.Choice(name="ロリ", value="../prompts/prompt_innocent_girl.txt"),
    app_commands.Choice(name="ショタ", value="../prompts/prompt_innocent_boy.txt"),
    app_commands.Choice(name="オジサン", value="../prompts/prompt_middle_aged.txt"),
    app_commands.Choice(name="ギャル", value="../prompts/prompt_gyaru.txt"),
    app_commands.Choice(name="メスガキ", value="../prompts/prompt_cheeky.txt"),
    app_commands.Choice(name="お姉さん", value="../prompts/prompt_ane.txt"),
    app_commands.Choice(name="はんなり", value="../prompts/prompt_kyoto.txt"),
    app_commands.Choice(name="クレーマー", value="../prompts/prompt_greed.txt"),
    app_commands.Choice(name="標準", value="../prompts/prompt_default.txt"),
]

MODEL_CHOICES = [
    app_commands.Choice(name="Gemini 2.5 Flash", value="gemini-2.5-flash"),
    app_commands.Choice(name="Gemini 3.5 Flash", value="gemini-3.5-flash"),
    app_commands.Choice(name="Gemini 3.1 Flash Lite", value=DEFAULT_MODEL_NAME),
]

RESPOND_MODE_CHOICES = [
    app_commands.Choice(name="すべての発言に反応", value="all"),
    app_commands.Choice(name="メンションのみに反応", value="mention"),
]

# /summarize の期間選択肢。value は「何時間さかのぼるか」を表す（1か月=30日=720時間）。
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



PERSONA_DISPLAY_NAMES = {choice.value: choice.name for choice in PERSONA_CHOICES}
MODEL_DISPLAY_NAMES = {choice.value: choice.name for choice in MODEL_CHOICES}
RESPOND_MODE_DISPLAY_NAMES = {choice.value: choice.name for choice in RESPOND_MODE_CHOICES}
AUTO_TOPIC_INTERVAL_DISPLAY_NAMES = {choice.value: choice.name for choice in AUTO_TOPIC_INTERVAL_CHOICES}


