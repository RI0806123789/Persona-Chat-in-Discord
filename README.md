# Persona Chat in Discord

## 🛠️ 技術スタック
![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white) 

Google Gemini AI を活用した Discord ボットです。  
テキストチャット・ボイスチャット読み上げ・ペルソナ切り替え・利用状況グラフ表示などの機能を備えています。

---

## ✨ 機能一覧

| 機能 | 概要 |
|------|------|
| AI チャット | Gemini API を使ってテキストで会話 |
| 音声読み上げ | gTTS + FFmpeg でボイスチャンネルに返答を読み上げ |
| ペルソナ切り替え | ツンデレ / ヤンデレ / メイド / ロリ / オジサン / 標準 |
| モデル切り替え | Gemini 2.5 Flash / Flash Lite / Pro を動的に変更 |
| 利用グラフ | 入力・出力・合計トークン数の推移をグラフ表示 |
| ステータス確認 | 現在のペルソナとモデルを即確認 |

---

## 📁 ディレクトリ構成

```
project/
├── ffmpeg-master-latest-win64-gpl-shared/      # FFmpeg ダウンロード元（Git 管理対象外）
├── index/
│   ├── bot.py                  # ボット本体
│   ├── config.py               # APIキー設定テンプレート（Git 管理対象）
│   └── ffmpeg.exe              # FFmpeg 実行ファイル（別途ダウンロード）
├── prompts/                    # システムプロンプトファイル群
│   ├── prompt_default.txt
│   ├── prompt_tundere.txt
│   ├── prompt_yandere.txt
│   ├── prompt_maid.txt
│   ├── prompt_loli.txt
│   └── prompt_oji.txt
├── .gitignore                   
├── README.md                    
└── requirements.txt            # Python 依存パッケージ
```

---

## 🛠️ セットアップ

### 1. 事前準備

以下を用意してください。

- **Python 3.10 以上**
- **Discord Bot トークン**（[Discord Developer Portal](https://discord.com/developers/applications) で取得）
- **Gemini API キー**（[Google AI Studio](https://aistudio.google.com/app/apikey) で取得）
- **FFmpeg**（後述）

---

### 2. パッケージのインストール

```bash
pip install discord.py google-generativeai gTTS PyNaCl httpx matplotlib numpy
```

または `requirements.txt` を使用:

```bash
pip install -r requirements.txt
```

---

### 3. FFmpeg のセットアップ

ボイスチャンネルでの読み上げに FFmpeg が必要です。  
> ⚠️ FFmpeg のバイナリはサイズが大きいため Git 管理対象外です。必ず手動でダウンロードしてください。

1. [ffmpeg.org/download.html](https://ffmpeg.org/download.html) から Windows 用ビルドをダウンロード
2. `ffmpeg.exe` を `index/` フォルダ（`bot.py` と**同じフォルダ**）に配置

---

### 4. APIキーの設定

`index/config.py` に以下の内容を記述します。  

```python
# config.py
API_KEY_GEMINI = "YOUR_GEMINI_API_KEY"
TOKEN_DISCORD  = "YOUR_DISCORD_BOT_TOKEN"
```

---


### 5. ボットの起動

```bash
cd index
python bot.py
```

`ログイン成功: BotName#0000` と表示されれば起動完了です。

---

## 💬 コマンド一覧

### `/ai <question>`
現在の設定でAIと会話します。  
チャンネルの直近 100 件の発言を履歴として参照します。  
ボイスチャンネルに参加中の場合は、返答を自動で読み上げます。

```
/ai 今日の天気はどう？
```

---

### `/set <persona>`
AI のペルソナ（システムプロンプト）を切り替えます。

| 選択肢 | 説明 |
|--------|------|
| ツンデレ | ツンデレキャラ |
| ヤンデレ | ヤンデレキャラ |
| メイド | メイドキャラ |
| ロリ | ロリキャラ |
| オジサン | おじさんキャラ |
| 標準 | デフォルトプロンプト |

---

### `/gemini <model>`
使用する Gemini モデルを切り替えます。

| 選択肢 | モデルID |
|--------|----------|
| Gemini 2.5 Flash | `gemini-2.5-flash` |
| Gemini 2.5 Flash Lite | `gemini-2.5-flash-lite` |
| Gemini 2.5 Pro | `gemini-2.5-pro` |

---

### `/status`
現在のペルソナとモデルの設定を表示します。

---

### `/graph`
これまでの `/ai` 利用分のトークン数推移（入力・出力・合計）をグラフ画像で表示します。  
※ ボット再起動でデータはリセットされます。

---

### `/join`
コマンド実行者が参加中のボイスチャンネルにボットを接続します。

---

### `/leave`
ボットをボイスチャンネルから退出させます。

---

## ⚠️ 注意事項

- **`bot.py` を起動している IDE やターミナルを終了するとボットが停止します。** 運用時はサーバーやバックグラウンド実行を検討してください。
- グラフデータはメモリ上に保持されるため、ボット再起動でリセットされます。

---

## 📦 依存パッケージ

| パッケージ | 用途 |
|-----------|------|
| `discord.py` | Discord API クライアント |
| `google-generativeai` | Gemini API クライアント |
| `gTTS` | テキスト → 音声変換 |
| `PyNaCl` | Discord 音声暗号化 |
| `httpx` | HTTP 通信 |
| `matplotlib` | トークン数グラフ描画 |
| `numpy` | グラフ用数値処理 |

---

## 📖 参考文献

- [Discord Developer Portal](https://discord.com/developers/applications)
- [Google AI Studio](https://aistudio.google.com/app/apikey)
- [FFmpeg 公式サイト](https://ffmpeg.org/download.html)