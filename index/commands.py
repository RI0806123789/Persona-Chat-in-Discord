import discord
from discord import app_commands

from app_state import BotState
from choices import (
    MODEL_CHOICES,
    MODEL_DISPLAY_NAMES,
    PERSONA_CHOICES,
    PERSONA_DISPLAY_NAMES,
    RESPOND_MODE_CHOICES,
    RESPOND_MODE_DISPLAY_NAMES,
)
from ng_word_service import NgWordFilter
from paths import resolve_prompt_path
from usage_graph import UsageTracker


def register_commands(tree: app_commands.CommandTree, state: BotState, usage: UsageTracker, ng_filter: NgWordFilter) -> None:
    @tree.command(name="set", description="AIのペルソナ（プロンプト）を変更します。")
    @app_commands.choices(persona=PERSONA_CHOICES)
    @app_commands.describe(persona="使用したいペルソナを選択してください。")
    async def set_prompt(interaction: discord.Interaction, persona: app_commands.Choice[str]) -> None:
        check_path = resolve_prompt_path(persona.value)

        if check_path.exists():
            state.current_prompt_file = persona.value
            await interaction.response.send_message(f"プロンプトを「{persona.name}」({persona.value}) に変更しました。")
        else:
            await interaction.response.send_message(
                f"エラー: ファイル「{persona.value}」が見つかりませんでした。\n"
                f"現在の設定 ({state.current_prompt_file}) を維持します。"
            )

    @tree.command(name="gemini", description="使用するGeminiモデルを選択します。")
    @app_commands.choices(model=MODEL_CHOICES)
    @app_commands.describe(model="使用したいモデルを選択してください。")
    async def set_gemini_model(interaction: discord.Interaction, model: app_commands.Choice[str]) -> None:
        state.current_model_name = model.value
        await interaction.response.send_message(f"使用するモデルを「{model.name}」({model.value}) に変更しました。")

    @tree.command(name="setting", description="ボットの応答モード（すべてかメンションのみか）を変更します。")
    @app_commands.choices(mode=RESPOND_MODE_CHOICES)
    @app_commands.describe(mode="応答モードを選択してください。")
    async def set_respond_mode(interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        state.current_respond_mode = mode.value
        await interaction.response.send_message(f"応答モードを「{mode.name}」に変更しました。")

    @tree.command(name="status", description="現在のペルソナ、モデル、応答モードの設定を表示します。")
    async def check_status(interaction: discord.Interaction) -> None:
        current_persona_name = PERSONA_DISPLAY_NAMES.get(
            state.current_prompt_file,
            f"不明 ({state.current_prompt_file})",
        )
        current_model_display_name = MODEL_DISPLAY_NAMES.get(
            state.current_model_name,
            f"不明 ({state.current_model_name})",
        )
        current_mode_name = RESPOND_MODE_DISPLAY_NAMES.get(
            state.current_respond_mode,
            f"不明 ({state.current_respond_mode})",
        )

        await interaction.response.send_message(
            f"現在の設定は以下の通りです：\n"
            f"・**ペルソナ**: {current_persona_name}\n"
            f"・**モデル**: {current_model_display_name}\n"
            f"・**応答モード**: {current_mode_name}\n"
            f"・**NGワード**: {ng_filter.word_count}件"
        )

    @tree.command(name="reset", description="このチャンネルの会話履歴をリセットして新しく始めます。")
    async def reset_chat(interaction: discord.Interaction) -> None:
        channel = interaction.channel
        channel_id = getattr(channel, "id", None)
        if channel_id is None:
            await interaction.response.send_message("エラー: チャンネルが取得できませんでした。")
            return

        await interaction.response.send_message("✅ 会話履歴をリセットしました。ここから新しい会話を始めます。")
        reset_msg = await interaction.original_response()
        state.channel_reset_points[int(channel_id)] = reset_msg.id

    @tree.command(name="graph", description="これまでの利用結果をグラフで表示します。")
    async def show_graph(interaction: discord.Interaction) -> None:
        if not usage.has_data():
            await interaction.response.send_message("まだ利用データがありません。")
            return

        buffer = usage.build_graph_buffer()
        try:
            picture = discord.File(buffer, filename="usage_graph.png")
            await interaction.response.send_message("これまでの利用結果のグラフです。", file=picture)
        except Exception as error:
            print(f"グラフ送信中に予期せぬエラー: {error}")
            try:
                await interaction.followup.send(f"エラー: グラフの送信に失敗しました。 ({error})")
            except Exception as followup_error:
                print(f"グラフ送信エラー通知に失敗しました: {followup_error}")
        finally:
            buffer.close()

    @tree.command(name="join", description="ボットがボイスチャンネルに参加します")
    async def join(interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member) or member.voice is None or member.voice.channel is None:
            await interaction.response.send_message("先にボイスチャンネルに参加してください。")
            return

        voice_channel = member.voice.channel
        await interaction.response.defer()
        if interaction.guild and interaction.guild.voice_client is not None:
            await interaction.guild.voice_client.disconnect(force=True)
        try:
            await voice_channel.connect()
            await interaction.followup.send(f"{voice_channel.name} に接続しました。")
        except Exception as error:
            print(f"接続エラー: {error}")
            await interaction.followup.send("接続に失敗しました。")

    @tree.command(name="leave", description="ボットがボイスチャンネルから退出します")
    async def leave(interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.guild.voice_client is None:
            await interaction.response.send_message("ボットはボイスチャンネルに参加していません。")
            return

        await interaction.response.defer()
        try:
            await interaction.guild.voice_client.disconnect(force=True)
            await interaction.followup.send("退出しました。")
        except Exception as error:
            print(f"切断エラー: {error}")
            await interaction.followup.send("退出に失敗しました。")

    @tree.command(name="ng_reload", description="NGワードリストを再読み込みします。")
    async def ng_reload(interaction: discord.Interaction) -> None:
        ng_filter.reload()
        await interaction.response.send_message(f"✅ NGワードリストを再読み込みしました。（{ng_filter.word_count}件）")

