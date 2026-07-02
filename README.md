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
| ペルソナ切り替え | ツンデレ / ヤンデレ / メイド / ロリ / ショタ / オジサン / ギャル / メスガキ / お姉さん / 標準 |
| モデル切り替え | Gemini 2.5 Flash / 3.5 Flash / 3.1 Flash Lite を動的に変更 |
| 利用グラフ | 入力・出力・合計トークン数の推移をグラフ表示 |
| ステータス確認 | 現在のペルソナとモデル・応答モードを即確認 |
| **コマンドいらずの会話** 🆕 | `/ai` コマンド不要で発言に直接反応。全発言 or メンションのみを切り替え可能 |
| **マルチモーダル対応** 🆕 | チャットへの画像添付を Gemini に送信して解析・返答。テキストなしの画像のみも対応 |
| **会話リセット** 🆕 | チャンネルごとに会話履歴をリセットし、文脈を切り替えて新しい会話を開始 |

---

## 📁 ディレクトリ構成

```
Persona Chat in Discord/
├── ffmpeg-master-latest-win64-gpl-shared/      # FFmpeg ダウンロード元（Git 管理対象外）
├── index/
│   ├── __pycache__/                            # Python キャッシュ（Git 管理対象外）
│   ├── avcodec-62.dll                          # FFmpeg 共有ライブラリ（Git 管理対象外）
│   ├── avdevice-62.dll
│   ├── avfilter-11.dll
│   ├── avformat-62.dll
│   ├── avutil-60.dll
│   ├── swresample-6.dll
│   ├── swscale-9.dll
│   ├── bot.py                                  # ボット本体
│   ├── config.py                               # APIキー テンプレート（Git 管理対象）
│   ├── ffmpeg.exe                              # FFmpeg 実行ファイル（Git 管理対象外）
│   ├── ffplay.exe
│   └── ffprobe.exe
├── prompts/                                    # システムプロンプトファイル群
│   ├── 00_prompt_format.txt                    # プロンプトフォーマット定義
│   ├── prompt_default.txt
│   ├── prompt_tundere.txt
│   ├── prompt_yandere.txt
│   ├── prompt_maid.txt
│   ├── prompt_lori.txt
│   ├── prompt_syota.txt
│   ├── prompt_oji.txt
│   ├── prompt_gyaru.txt
│   ├── prompt_mesugaki.txt
│   └── prompt_ane.txt
├── .gitignore
├── README.md
└── requirements.txt                            # Python 依存パッケージ
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
pip install discord.py google-generativeai gTTS PyNaCl httpx matplotlib numpy Pillow
```

または `requirements.txt` を使用:

```bash
pip install -r requirements.txt
```

---

### 3. FFmpeg のセットアップ

ボイスチャンネルでの読み上げに FFmpeg が必要です。  
> ⚠️ FFmpeg のバイナリ・DLL はサイズが大きいため Git 管理対象外です。必ず手動でダウンロードしてください。

1. [ffmpeg.org/download.html](https://ffmpeg.org/download.html) から Windows 用ビルド（shared版）をダウンロード
2. `ffmpeg.exe` / `ffplay.exe` / `ffprobe.exe` と各 `.dll` ファイルを `index/` フォルダ（`bot.py` と**同じフォルダ**）に配置

---

### 4. APIキーの設定

`index/config.py` を以下の内容を記述してください:

```python
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
| ショタ | ショタキャラ |
| オジサン | おじさんキャラ |
| ギャル | ギャルキャラ |
| メスガキ | メスガキキャラ |
| お姉さん | お姉さんキャラ |
| 標準 | デフォルトプロンプト |

---

### `/gemini <model>`
使用する Gemini モデルを切り替えます。

| 選択肢 | モデルID |
|--------|----------|
| Gemini 2.5 Flash | `gemini-2.5-flash` |
| Gemini 3.5 Flash | `gemini-3.5-flash` |
| Gemini 3.1 Flash Lite | `gemini-3.1-flash-lite` |

> デフォルトは **Gemini 3.1 Flash Lite** です。

---

### `/status`
現在のペルソナ・モデル・応答モードの設定を表示します。

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

### `/setting <mode>` 🆕
ボットの応答モードを切り替えます。

| 選択肢 | 説明 |
|--------|------|
| すべての発言に反応 | チャンネルの全メッセージに自動で返答します（デフォルト） |
| メンションのみに反応 | ボットへのメンション（@Bot）があった場合のみ返答します |

> コマンド不要で会話したい場合は「すべての発言に反応」、特定の呼びかけにだけ反応させたい場合は「メンションのみ」を選択してください。

---

### `/reset` 🆕
このチャンネルの会話履歴をリセットして新しい会話を開始します。  
リセット以前の発言は以降の返答に影響しなくなります。

```
/reset
→ ✅ 会話履歴をリセットしました。ここから新しい会話を始めます。
```

> ※ リセットポイントはチャンネルごとに独立して管理されます。

---

## 📋 更新履歴

### ver0.1.1 — 2026/06/29

#### コマンドいらずの会話
`/ai` コマンドなしで、チャンネルへの通常の発言に直接反応するようになりました。  
`/setting` コマンドで「すべての発言に反応」と「メンションのみに反応」の2つのモードを切り替えられます。

- **すべての発言に反応**（デフォルト）: チャンネルのメッセージすべてに返答
- **メンションのみに反応**: `@Bot` でメンションしたときだけ返答


#### マルチモーダル対応
メッセージに画像を添付すると、テキストと合わせて Gemini に送信され、画像の内容を踏まえた返答が得られます。

- テキストなしで画像のみ添付した場合は、自動的に「この画像について説明・反応してください。」というプロンプトで処理されます
- 複数枚の画像も一度に送信可能です
- 対応形式: JPEG・PNG などの一般的な画像形式（`image/*` 全般）

#### 会話リセット
`/reset` コマンドでチャンネルの会話履歴をリセットできます。  
リセット後は、それ以前の発言を参照せずに新しい文脈で会話を始めます。  
リセットポイントはチャンネルごとに独立しており、ボット再起動でクリアされます。

---

### ver0.1.0 — 2026/06/23

- 初回リリース
- AI チャット（`/ai`）、音声読み上げ（`/join` / `/leave`）、ペルソナ切り替え（`/set`）、モデル切り替え（`/gemini`）、利用グラフ（`/graph`）、ステータス確認（`/status`）

---

## ⚠️ 注意事項

- **`bot.py` を起動している IDE やターミナルを終了するとボットが停止します。** 運用時はサーバーやバックグラウンド実行を検討してください。
- グラフデータはメモリ上に保持されるため、ボット再起動でリセットされます。
- ソースコード、プロンプトファイルは、すべてGoogle Geminiによって生成されたものです。

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
| `Pillow` | 添付画像の読み込み・変換（マルチモーダル対応） |

---

## 📖 参考文献

- [Discord Developer Portal](https://discord.com/developers/applications)
- [Google AI Studio](https://aistudio.google.com/app/apikey)
- [FFmpeg 公式サイト](https://ffmpeg.org/download.html)