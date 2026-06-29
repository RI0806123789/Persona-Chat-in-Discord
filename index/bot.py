import discord
from discord import app_commands
import google.generativeai as genai
import os
import asyncio
from gtts import gTTS
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import io
from PIL import Image
from typing import Any
import config as config
import config_private as config

#利用結果を表示するグラフデータ用リスト
graph_data_input = []
graph_data_output = []
graph_data_total = []

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True 
client = discord.Client(intents=intents)
tree  = app_commands.CommandTree(client) 

# (A) 現在の設定を記憶する変数
current_prompt_file = "../prompts/prompt_default.txt"
current_model_name = "gemini-3.1-flash-lite"
current_respond_mode = "all" 

# (B) ペルソナの選択肢リスト
persona_choices_list = [
    app_commands.Choice(name="ツンデレ", value="../prompts/prompt_tundere.txt"),
    app_commands.Choice(name="ヤンデレ", value="../prompts/prompt_yandere.txt"),
    app_commands.Choice(name="メイド", value="../prompts/prompt_maid.txt"),
    app_commands.Choice(name="ロリ", value="../prompts/prompt_loli.txt"),
    app_commands.Choice(name ="ショタ", value = "../prompts/prompt_syota.txt"),
    app_commands.Choice(name="オジサン", value="../prompts/prompt_oji.txt"),
    app_commands.Choice(name="ギャル", value="../prompts/prompt_gyaru.txt"),
    app_commands.Choice(name="メスガキ", value="../prompts/prompt_mesugaki.txt"),
    app_commands.Choice(name="お姉さん", value="../prompts/prompt_ane.txt"),
    app_commands.Choice(name="標準", value="../prompts/prompt_default.txt"),
]

# (C) モデルの選択肢リスト
model_choices_list = [
    app_commands.Choice(name="Gemini 2.5 Flash", value="gemini-2.5-flash"),
    app_commands.Choice(name="Gemini 3.5 Flash", value="gemini-3.5-flash"),
    app_commands.Choice(name="Gemini 3.1 Flash Lite", value="gemini-3.1-flash-lite"),
]

# 応答モードの選択肢リスト
respond_mode_choices_list = [
    app_commands.Choice(name="すべての発言に反応", value="all"),
    app_commands.Choice(name="メンションのみに反応", value="mention"),
]

persona_display_names = {choice.value: choice.name for choice in persona_choices_list}
model_display_names = {choice.value: choice.name for choice in model_choices_list}
respond_mode_display_names = {choice.value: choice.name for choice in respond_mode_choices_list}

# (D) チャンネルごとの会話リセットポイントを記憶する辞書
#     キー: channel.id (int)  値: リセット時のメッセージID (int)
#     on_message ではこのID以降のメッセージのみ履歴として読み込む
channel_reset_points: dict[int, int] = {}

@client.event
async def on_ready():
    print(f'ログイン成功: {client.user}')
    await tree.sync() 

@client.event
async def on_message(message: discord.Message):
    global current_respond_mode

    # Pylanceの警告を消すための安全確認
    if client.user is None:
        return

    if message.author == client.user:
        return

    if message.author.bot:
        return

    if current_respond_mode == "mention":
        if client.user not in message.mentions:
            return

    question = message.content.replace(f'<@{client.user.id}>', '').strip()
    
    # テキストが空でも、画像が添付されていれば処理を続行できるように変更
    if not question and not message.attachments:
        return

    channel = message.channel
    if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel, discord.StageChannel)):
        return

    async with channel.typing():
        
        # 添付ファイル（画像）の取得とPillowオブジェクト化
        image_objects = []
        for attachment in message.attachments:
            if attachment.content_type and attachment.content_type.startswith('image/'):
                try:
                    image_bytes = await attachment.read()
                    img = Image.open(io.BytesIO(image_bytes))
                    if img.mode != 'RGB':
                        img = img.convert('RGB')
                    image_objects.append(img)
                except Exception as e:
                    print(f"画像読み込みエラー: {e}")

        # リセットポイントが設定されているか確認し、それ以降の履歴のみ取得する
        reset_message_id = channel_reset_points.get(channel.id)
        history_msgs = []
        if reset_message_id:
            # after を指定すると oldest_first=True 相当（昇順）で返るためそのまま使用
            async for msgs in channel.history(
                limit=100,
                after=discord.Object(id=reset_message_id),
                oldest_first=True
            ):
                history_msgs.append(msgs)
        else:
            # リセット未設定の場合は従来通り最新100件を取得して昇順に並べ直す
            async for msgs in channel.history(limit=100):
                history_msgs.append(msgs)
            history_msgs = list(reversed(history_msgs))

        history_text = ""
        for msg in history_msgs:
            role = "BOTの回答" if msg.author == client.user else "ユーザーの質問"
            clean_content = msg.content.replace(f'<@{client.user.id}>', '').strip()
            if msg.attachments:
                clean_content += " [画像添付あり]"
            history_text += f"{role}: {clean_content}\n"

        script_dir = os.path.dirname(os.path.abspath(__file__))    
        prompt_file_path = os.path.join(script_dir, current_prompt_file) 
        try:
            prompt = load_prompt_template(prompt_file_path)
        except FileNotFoundError:
            print(f"エラー: {current_prompt_file} が見つかりません。")
            await message.reply(f"エラー: プロンプトファイル({current_prompt_file})が見つかりませんでした。")
            return
        
        if not question:
            question = "この画像について説明・反応してください。"

        full_prompt = prompt + "\n--- 過去の会話履歴 ---\n" + history_text + "\n--- 今回の質問 ---\n" + question
        
        response_text, usage_info = await ask_gemini_async(full_prompt, image_objects) 
        
        len_fullprompt = len(full_prompt)
        print("----------------------------------------------------------------------------------------------------------------------------")
        print(f"User (直接会話)：{question} / 画像添付: {len(image_objects)}枚")
        print(f"Prompt text count：{len_fullprompt}")  
        if usage_info: 
            print(f"Prompt token count：{usage_info.prompt_token_count}")
            print(f"Response token count：{usage_info.candidates_token_count}")
            print(f"Total token count：{usage_info.total_token_count}")
            graph_data_input.append(usage_info.prompt_token_count)
            graph_data_output.append(usage_info.candidates_token_count)
            graph_data_total.append(usage_info.total_token_count)
        print(f"AI：{response_text}")
        print("----------------------------------------------------------------------------------------------------------------------------")

        if response_text is None:
            await message.reply("エラーが発生しました。（Gemini APIの呼び出しに失敗、または非対応の画像形式です）")
            return 

        await message.reply(response_text) 

    # gTTS による音声再生ロジック
    voice_client = message.guild.voice_client if message.guild else None
    if isinstance(voice_client, discord.VoiceClient):
        if voice_client.is_connected() and not voice_client.is_playing():
            tts_file = f"temp_speech_{message.id}.mp3" 
            try:
                if not response_text or response_text.isspace():
                    return 
                tts = gTTS(text=response_text, lang='ja')
                tts.save(tts_file)
                ffmpeg_exe_path = os.path.join(script_dir, "ffmpeg.exe")
                source = discord.FFmpegPCMAudio(tts_file, executable=ffmpeg_exe_path) 
                voice_client.play(source)
                while voice_client.is_playing():
                    await asyncio.sleep(1)
            except Exception as e:
                print(f"gTTS APIエラー: {e}")
            finally:
                if os.path.exists(tts_file):
                    os.remove(tts_file)

@tree.command(name="set", description="AIのペルソナ（プロンプト）を変更します。")
@app_commands.choices(persona=persona_choices_list)
@app_commands.describe(persona="使用したいペルソナを選択してください。")
async def set_prompt(interaction: discord.Interaction, persona: app_commands.Choice[str]):
    global current_prompt_file
    script_dir = os.path.dirname(os.path.abspath(__file__))
    check_path = os.path.join(script_dir, persona.value)
    
    if os.path.exists(check_path):
        current_prompt_file = persona.value
        await interaction.response.send_message(f"プロンプトを「{persona.name}」({persona.value}) に変更しました。")
    else:
        await interaction.response.send_message(f"エラー: ファイル「{persona.value}」が見つかりませんでした。\n現在の設定 ({current_prompt_file}) を維持します。")

@tree.command(name="gemini", description="使用するGeminiモデルを選択します。")
@app_commands.choices(model=model_choices_list) 
@app_commands.describe(model="使用したいモデルを選択してください。")
async def set_gemini_model(interaction: discord.Interaction, model: app_commands.Choice[str]):
    global current_model_name 
    current_model_name = model.value
    await interaction.response.send_message(f"使用するモデルを「{model.name}」({model.value}) に変更しました。")

@tree.command(name="setting", description="ボットの応答モード（すべてかメンションのみか）を変更します。")
@app_commands.choices(mode=respond_mode_choices_list)
@app_commands.describe(mode="応答モードを選択してください。")
async def set_respond_mode(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    global current_respond_mode
    current_respond_mode = mode.value
    await interaction.response.send_message(f"応答モードを「{mode.name}」に変更しました。")

@tree.command(name="status", description="現在のペルソナ、モデル、応答モードの設定を表示します。")
async def check_status(interaction: discord.Interaction):
    global current_prompt_file, current_model_name, current_respond_mode
    global persona_display_names, model_display_names, respond_mode_display_names

    current_persona_name = persona_display_names.get(current_prompt_file, f"不明 ({current_prompt_file})")
    current_model_display_name = model_display_names.get(current_model_name, f"不明 ({current_model_name})")
    current_mode_name = respond_mode_display_names.get(current_respond_mode, f"不明 ({current_respond_mode})")
    
    await interaction.response.send_message(
        f"現在の設定は以下の通りです：\n"
        f"・**ペルソナ**: {current_persona_name}\n"
        f"・**モデル**: {current_model_display_name}\n"
        f"・**応答モード**: {current_mode_name}"
    )

@tree.command(name="reset", description="このチャンネルの会話履歴をリセットして新しく始めます。")
async def reset_chat(interaction: discord.Interaction):
    channel = interaction.channel
    if channel is None:
        await interaction.response.send_message("エラー: チャンネルが取得できませんでした。")
        return

    # リセット完了メッセージを送信
    await interaction.response.send_message("✅ 会話履歴をリセットしました。ここから新しい会話を始めます。")

    # 送信したメッセージのIDをリセットポイントとして記録する
    # 以降の on_message ではこのID以降のメッセージのみ履歴として読み込む
    reset_msg = await interaction.original_response()
    channel_reset_points[channel.id] = reset_msg.id

@tree.command(name="graph", description="これまでの利用結果をグラフで表示します。")
async def show_graph(interaction: discord.Interaction):
    if not graph_data_total:
        await interaction.response.send_message("まだ利用データがありません。")
        return

    plt.figure(figsize=(10, 6))
    x = np.arange(1, len(graph_data_total) + 1)    
    matplotlib.use('tkagg')
    matplotlib.rc('font', **{'family':'Yu Gothic'})
    plt.plot(x, graph_data_input, label='入力トークン数', marker='o', color = "red", )
    plt.plot(x, graph_data_output, label='出力トークン数', marker='o', color = "blue", )
    plt.plot(x, graph_data_total, label='合計トークン数', marker='o', color = "green", )
    plt.xlabel('利用回数')
    plt.ylabel('トークン数')
    plt.title('Gemini API 利用結果のトークン数推移')
    plt.legend()
    plt.grid(True)
    
    buffer = io.BytesIO()
    plt.savefig(buffer, format='png')
    plt.close() 

    buffer.seek(0)
    try:
        picture = discord.File(buffer, filename="usage_graph.png")
        await interaction.response.send_message("これまでの利用結果のグラフです。", file=picture)
    except Exception as e:
        print(f"グラフ送信中に予期せぬエラー: {e}")
        try:
            await interaction.followup.send(f"エラー: グラフの送信に失敗しました。 ({e})")
        except:
            pass
    finally:
        buffer.close()

@tree.command(name="join", description="ボットがボイスチャンネルに参加します")
async def join(interaction: discord.Interaction):
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
    except Exception as e:
        print(f"接続エラー: {e}")
        await interaction.followup.send("接続に失敗しました。")

@tree.command(name="leave", description="ボットがボイスチャンネルから退出します")
async def leave(interaction: discord.Interaction):
    if interaction.guild is None or interaction.guild.voice_client is None:
        await interaction.response.send_message("ボットはボイスチャンネルに参加していません。")
        return
    await interaction.response.defer()
    try:
        await interaction.guild.voice_client.disconnect(force=True)
        await interaction.followup.send("退出しました。")
    except Exception as e:
        print(f"切断エラー: {e}")
        await interaction.followup.send("退出に失敗しました。")

# ★ 修正1: [reportPrivateImportUsage] を無視するコメントを追加
genai.configure(api_key=config.API_KEY_GEMINI)  # type: ignore[reportPrivateImportUsage]

# ★ 修正2: imagesの型ヒントを list | None に変更（Noneの許容）
async def ask_gemini_async(question: str, images: list | None = None) -> tuple[str | None, Any | None]: 
    try:
        # ★ 修正3: [reportPrivateImportUsage] を無視するコメントを追加
        model = genai.GenerativeModel(current_model_name)  # type: ignore[reportPrivateImportUsage]
        
        content = [question]
        
        if images:
            content.extend(images)
            
        response = await model.generate_content_async(content)
        usage_info = response.usage_metadata
        return response.text , usage_info
    except Exception as e:
        print(f"Error occurred while asking Gemini: {e}") 
        return None , None

def load_prompt_template(file_path: str) -> str:
    with open(file_path, 'r', encoding='utf-8') as file:
        return file.read()

client.run(config.TOKEN_DISCORD)