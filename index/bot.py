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
from typing import Any
import config as config
import config_private as config

#利用結果を表示するグラフデータ用リスト
graph_data_input = []
graph_data_output = []
graph_data_total = []
# (インテントやクライアント定義は変更なし)
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True 
client = discord.Client(intents=intents)
tree  = app_commands.CommandTree(client) 

# --- ★ 修正点 1: 現在の設定用変数と、選択肢リストをグローバルに定義 ★ ---

# (A) 現在の設定を記憶する変数
current_prompt_file = "../prompts/prompt_default.txt"
current_model_name = "gemini-2.5-flash-lite"

# (B) ペルソナの選択肢リスト
##[;]で複数のファイルを認識可能
persona_choices_list = [
    app_commands.Choice(name="ツンデレ", value="../prompts/prompt_tundere.txt"),
    app_commands.Choice(name="ヤンデレ", value="../prompts/prompt_yandere.txt"),
    app_commands.Choice(name="メイド", value="../prompts/prompt_maid.txt"),
    app_commands.Choice(name="ロリ", value="../prompts/prompt_loli.txt"),
    app_commands.Choice(name ="ショタ", value = "../prompts/prompt_syota.txt"),
    app_commands.Choice(name="オジサン", value="../prompts/prompt_oji.txt"),
    app_commands.Choice(name="標準", value="../prompts/prompt_default.txt"),
]

# (C) モデルの選択肢リスト
model_choices_list = [
    app_commands.Choice(name="Gemini 2.5 Flash", value="gemini-2.5-flash"),
    app_commands.Choice(name="Gemini 2.5 Flash Lite", value="gemini-2.5-flash-lite"),
    app_commands.Choice(name="Gemini 2.5 Pro", value="gemini-2.5-pro"),
]

# (D) /status コマンドで表示名を逆引きするための辞書
persona_display_names = {choice.value: choice.name for choice in persona_choices_list}
model_display_names = {choice.value: choice.name for choice in model_choices_list}
# -----------------------------------------------------------------

@client.event
async def on_ready():
    print(f'ログイン成功: {client.user}')
    await tree.sync() 

# --- ★ 修正点 2: /set コマンド (グローバル変数リストを使用) ★ ---
@tree.command(name="set", description="AIのペルソナ（プロンプト）を変更します。")
@app_commands.choices(persona=persona_choices_list) # ★ グローバル変数を使用
@app_commands.describe(persona="使用したいペルソナを選択してください。")
async def set_prompt(interaction: discord.Interaction, persona: app_commands.Choice[str]):
    global current_prompt_file # グローバル変数を書き換える宣言
    
    script_dir = os.path.dirname(os.path.abspath(__file__))
    check_path = os.path.join(script_dir, persona.value)
    
    if os.path.exists(check_path):
        current_prompt_file = persona.value # 変数を更新
        await interaction.response.send_message(f"プロンプトを「{persona.name}」({persona.value}) に変更しました。")
    else:
        await interaction.response.send_message(f"エラー: ファイル「{persona.value}」が見つかりませんでした。\n現在の設定 ({current_prompt_file}) を維持します。")
# ---------------------------------------------------------

# --- ★ 修正点 3: /gemini コマンド (グローバル変数リストを使用) ★ ---
@tree.command(name="gemini", description="使用するGeminiモデルを選択します。")
@app_commands.choices(model=model_choices_list) # ★ グローバル変数を使用
@app_commands.describe(model="使用したいモデルを選択してください。")
async def set_gemini_model(interaction: discord.Interaction, model: app_commands.Choice[str]):
    global current_model_name # グローバル変数を書き換える宣言
    
    current_model_name = model.value # 変数を更新
    await interaction.response.send_message(f"使用するモデルを「{model.name}」({model.value}) に変更しました。")
# ---------------------------------------------------------

# --- ★ 修正点 4: /status コマンド (新規追加) ★ ---
@tree.command(name="status", description="現在のペルソナとモデル設定を表示します。")
async def check_status(interaction: discord.Interaction):
    # グローバル変数を読み込む
    global current_prompt_file, current_model_name
    global persona_display_names, model_display_names

    # 辞書を使って、ファイル名/モデルIDから表示名（"ツンデレ"など）を取得
    current_persona_name = persona_display_names.get(current_prompt_file, f"不明 ({current_prompt_file})")
    current_model_display_name = model_display_names.get(current_model_name, f"不明 ({current_model_name})")
    
    await interaction.response.send_message(
        f"現在の設定は以下の通りです：\n"
        f"・**ペルソナ**: {current_persona_name}\n"
        f"・**モデル**: {current_model_display_name}"
    )
# ----/graphコマンド　これまでの利用結果を表示---
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
    # 1. メモリ上にバイナリデータ用のバッファを作成
    buffer = io.BytesIO()
    
    # 2. グラフを物理ファイルではなく、メモリバッファに PNG 形式で保存
    plt.savefig(buffer, format='png')
    plt.close() # Matplotlib のリソースを解放

    # 3. バッファのポインタを先頭 (0バイト目) に戻す (重要)
    #    (savefig が完了した時点ではポインタは末尾にあるため)
    buffer.seek(0)

    try:
        # 4. バッファを discord.File に渡す
        #    filename="usage_graph.png" は Discord 上で表示されるファイル名
        picture = discord.File(buffer, filename="usage_graph.png")
        
        # 5. 応答を送信
        await interaction.response.send_message("これまでの利用結果のグラフです。", file=picture)
        
    except Exception as e:
        print(f"グラフ送信中に予期せぬエラー: {e}")
        try:
            # send_message が失敗した場合
            await interaction.followup.send(f"エラー: グラフの送信に失敗しました。 ({e})")
        except:
            pass # エラー通知の失敗は無視
    finally:
        # 6. メモリバッファを閉じてリソースを解放
        buffer.close()
        print("グラフ用のメモリバッファを解放しました。")
    
    # 物理ファイルを作成していないため、os.remove と sleep 処理はすべて不要
# ---------------------------------------------------------

# --- ★ 修正点 5: /ai コマンド (説明文を固定に変更) ★ ---
@tree.command(name="ai", description="現在の設定でAIと会話します。")
@app_commands.describe(question="AIへの質問内容")
async def command(interaction: discord.Interaction, question: str):
    # (★ 以降の関数の中身は変更ありません)
    channel = interaction.channel
    if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel, discord.StageChannel)):
        await interaction.response.send_message("このコマンドはテキストチャンネル・スレッド・ボイスチャンネルでのみ使用できます。")
        return

    history_msgs = []
    async for msgs in channel.history(limit=100):
        history_msgs.append(msgs)
    history_msgs = list(reversed(history_msgs))
    history_text = ""
    for msg in history_msgs:
        role = "BOTの回答" if msg.author == client.user else "ユーザーの質問"
        history_text += f"{role}: {msg.content}\n"

    await interaction.response.defer() 
    
    script_dir = os.path.dirname(os.path.abspath(__file__))    
    
    prompt_file_path = os.path.join(script_dir, current_prompt_file) 

    try:
        prompt = load_prompt_template(prompt_file_path)
    except FileNotFoundError:
        print(f"エラー: {current_prompt_file} が見つかりません。")
        await interaction.followup.send(f"エラー: プロンプトファイル({current_prompt_file})が見つかりませんでした。")
        return
    
    full_prompt = prompt + "\n--- 過去の会話履歴 ---\n" + history_text + "\n--- 今回の質問 ---\n" + question #prompt = charafile history_text = chatlog question= userinput
    response_text, usage_info = await ask_gemini_async(full_prompt) 
    len_fullprompt = len(full_prompt)
    print("----------------------------------------------------------------------------------------------------------------------------")
    print(f"User：{question}")
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
        await interaction.followup.send("エラーが発生しました。（Gemini APIの呼び出しに失敗）")
        return 

    await interaction.followup.send(response_text) 
    
    # --- (gTTS による音声再生ロジックは変更なし) ---
    voice_client = interaction.guild.voice_client if interaction.guild else None
    if isinstance(voice_client, discord.VoiceClient):
        if voice_client.is_connected() and not voice_client.is_playing():
            tts_file = f"temp_speech_{interaction.id}.mp3" 
            try:
                if not response_text or response_text.isspace():
                    print("エラー: 再生するテキストが空です。") 
                    return 
                tts = gTTS(text=response_text, lang='ja')
                tts.save(tts_file)
                file_size = os.path.getsize(tts_file)
                print(f"一時ファイル {tts_file} を作成しました。(サイズ: {file_size} バイト)")
                ffmpeg_exe_path = os.path.join(script_dir, "ffmpeg.exe")
                source = discord.FFmpegPCMAudio(tts_file, executable=ffmpeg_exe_path) 
                print("再生を開始します...")
                voice_client.play(source)
                while voice_client.is_playing():
                    await asyncio.sleep(1)
                print("再生が終了しました。") 
            except Exception as e:
                print(f"gTTS APIエラー: {e}")
            finally:
                if os.path.exists(tts_file):
                    os.remove(tts_file)
                    print(f"一時ファイル {tts_file} を削除しました。")
        elif not voice_client.is_connected():
            print("デバッグ: ボットがVCに接続していません。")
        elif voice_client.is_playing():
            print("デバッグ: ボットは現在再生中です。")
    else:
        print("デバッグ: ボットがVCに接続していません。")
    print(f"--- Tts 処理終了 ---")
    
# (/join, /leave コマンドは変更なし)
@tree.command(name="join", description="ボットがボイスチャンネルに参加します")
async def join(interaction: discord.Interaction):
    member = interaction.user
    if not isinstance(member, discord.Member) or member.voice is None or member.voice.channel is None:
        await interaction.response.send_message("先にボイスチャンネルに参加してください。")
        return

    voice_channel = member.voice.channel

    if interaction.guild and interaction.guild.voice_client is not None:
        await interaction.guild.voice_client.disconnect(force=True)
    try:
        await voice_channel.connect()
        await interaction.response.send_message(f"{voice_channel.name} に接続しました。")
    except Exception as e:
        print(f"接続エラー: {e}")
        await interaction.response.send_message("接続に失敗しました。")

@tree.command(name="leave", description="ボTットがボイスチャンネルから退出します")
async def leave(interaction: discord.Interaction):
    if interaction.guild is None or interaction.guild.voice_client is None:
        await interaction.response.send_message("ボットはボイスチャンネルに参加していません。")
        return
    try:
        await interaction.guild.voice_client.disconnect(force=True)
        await interaction.response.send_message("退出しました。")
    except Exception as e:
        print(f"切断エラー: {e}")
        await interaction.response.send_message("退出に失敗しました。")

# (genai.configure は変更なし)
genai.configure(api_key=config.API_KEY_GEMINI)  # type: ignore[reportPrivateImportUsage]

# (ask_gemini_async は変更なし)
async def ask_gemini_async(question: str) -> tuple[str | None, Any | None]: 
    try:
        model = genai.GenerativeModel(current_model_name)  # type: ignore[reportPrivateImportUsage]
        response = await model.generate_content_async(question)
        # ★ トークン数情報を取得
        usage_info = response.usage_metadata
        return response.text , usage_info
    except Exception as e:
        print(f"Error occurred while asking Gemini: {e}") 
        return None , None

# (load_prompt_template, client.run は変更なし)
def load_prompt_template(file_path: str) -> str:
    with open(file_path, 'r', encoding='utf-8') as file:
        return file.read()

client.run(config.TOKEN_DISCORD)