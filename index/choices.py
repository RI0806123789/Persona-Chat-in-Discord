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



PERSONA_DISPLAY_NAMES = {choice.value: choice.name for choice in PERSONA_CHOICES}
MODEL_DISPLAY_NAMES = {choice.value: choice.name for choice in MODEL_CHOICES}
RESPOND_MODE_DISPLAY_NAMES = {choice.value: choice.name for choice in RESPOND_MODE_CHOICES}


