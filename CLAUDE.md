# CLAUDE.md

このファイルは、このリポジトリで作業する際の Claude Code (claude.ai/code) 向けガイダンスです。

## 概要

Google Gemini をバックエンドに持つ Discord ボット。ペルソナベースのテキストチャット、ボイスチャンネルでの TTS 読み上げ、画像/文書のマルチモーダル入力、長期記憶、ユーザーごとの親密度/感情トラッキング、コンテンツモデレーション、三審制ブラックリスト、チャンネル会話の議事録要約、ユーザー別会話統計ダッシュボード、無会話チャンネルへの自発的な話題提供を備える。ソースはすべて `index/` にある。ユーザー向け文字列・ログ出力（`print`）・コードコメントはすべて日本語。新規に追加するものも日本語で統一すること。

## ボットの起動

このリポジトリにはビルド手順・リンター設定・テストスイートは存在しない。開発は「実行して挙動を観察する」スタイル。

```bash
# 1. 依存関係のインストール (Python 3.10+)
pip install -r requirements.txt

# 2. シークレットの用意: index/config_private.py を作成する（gitignore 済み。config.py より優先される）
#    中身は API_KEY_GEMINI = "..." と TOKEN_DISCORD = "..."

# 3. FFmpeg バイナリ (ffmpeg.exe/ffplay.exe/ffprobe.exe + *.dll) は index/ の中に置く必要がある
#    — これらは gitignore されており、別途ダウンロードする。/join のボイス TTS でのみ必要。

# 4. index/ ディレクトリの中から実行する:
cd index
python bot.py     # 成功時に "ログイン成功: <bot>" と表示される
```

**必ず `index/` の中から実行すること。** モジュール群はパッケージ化されていないフラットな構成（`__init__.py` なし）で、互いをベア名でインポートし合う（`from app_state import BotState`）。プロンプトのパスも `index/` を基準に解決される（「パス解決」の項を参照）。

## アーキテクチャ

### 配線 (`bot.py`)

`bot.py` がコンポジションルート（合成の起点）。設定を読み込み、`genai` を構成し、`Database`（`database.py`、後述）を最初に初期化してから、各サービスのインスタンスを1つずつ生成して、DI フレームワークを使わず手作業で相互に注入する。共有される可変ランタイム状態はすべて単一の `BotState` データクラス（`app_state.py`）にまとめられ、必要なサービスに渡される。`register_commands`（`commands.py`）がスラッシュコマンドを `CommandTree` に登録する。`on_ready` でコマンドを同期し、バックグラウンドタスク `memory_maintenance_loop` を開始し、文書ワーカーのコルーチンを起動する。

### `on_message` パイプライン (`bot.py`)

ボット以外のメッセージはすべて、決まった順序の関門を通過する。編集時はこの順序を守ること:

1. **応答モードのゲート** — `"mention"` モード（デフォルト）では、DM 以外でボットへのメンションが無いメッセージは無視される。
2. **ブラックリスト**（`blacklist_service`）— ブロック中のユーザーは黙って破棄される。
3. **NG ワードフィルタ**（`ng_word_service`）— クリーニング済みテキストへの正規表現マッチ → `notify_violation`。
4. **ユーザー入力の AI モデレーション**（`content_moderator`）→ `notify_violation`。
5. **ルーティング**: 文書添付（`pdf/txt/csv/md`）は非同期の `DocumentService` ワーカーへキュー登録され、後で回答される。それ以外は `handle_chat_message` が同期的に実行される。同期チャット返信の後は、必ず `play_tts_response`（`voice.py`）が呼ばれて読み上げを試みる — ボットがボイスチャンネルに接続中かつアイドルでない限り no-op（何もしない）。

`notify_violation` は違反を1回記録し、3回目でユーザーをブラックリスト登録する（`BLACKLIST_MAX_VIOLATIONS`）。NG ワードとモデレーションは、送信前に**ボット自身の応答に対しても再度**適用される（応答中の NG ワードは ● でマスクされ、不適切な応答は定型の謝罪文に差し替えられる）。

### プロンプト組み立て (`handle_chat_message` → `prompting.py`)

メッセージごとに、単一のフラットなプロンプト文字列を組み立てる。構成要素は、ペルソナのテンプレート + 親密度/感情ブロック + 選択された長期記憶 + 文書要約 + チャンネル履歴 + 現在の質問。Gemini 呼び出しは `(text, usage_metadata, grounding_sources)` を返す。

### データベース (`database.py`)

SQLite（`memory/bot.db`、WAL モード）が構造化データの永続化先。SQL 文はすべて `database.py` に集約されている。テーブルは5つ: `affinity`（親密度・感情）、`user_stats`（会話統計）、`queue_tasks`（文書処理キュー）、`pending_log`（記憶圧縮前の一時ログ）、`profile`（プロフィール記憶。トップレベルキーごとに1行、値は JSON 文字列）。`projects.json` は LLM が構造を自由に決めるため、テーブル化せず JSON ファイルのまま。

- **スキーマ変更の手順**: `SCHEMA_VERSION` を1つ上げて `_MIGRATIONS` にそのバージョンの SQL 文を追記する。適用済みバージョンは `PRAGMA user_version` に記録され、起動時に未適用分だけが実行される。`_CREATE_TABLE_STATEMENTS`（初版の形）は変更禁止 — 変更は必ず `_MIGRATIONS` 側で行う（実例: バージョン2 = `profile` テーブル追加）。
- **旧 JSON からの取り込み**: 初回起動時、旧ファイル（`affinity.json`/`user_stats.json`/`queue.json`/`pending_log.txt`）が残っていれば DB へ取り込み、`<元の名前>.imported.bak` に改名する（バックアップ兼二重取り込み防止）。
- **profile.json は手書き追加用の受け皿**: 毎起動時、`profile.json` に中身があれば `profile` テーブルへマージ（同名キーはファイル側が勝つ）し、ファイルを `{}` に戻す。JSON として読めない場合はファイルを残して警告のみ（手書きミスでデータを失わない）。
- **並行処理**: 各サービスが `asyncio.to_thread`（＝別スレッド）から呼ぶため、接続は `check_same_thread=False` + 内部の `threading.Lock` で直列化。書き込みはトランザクションで原子的（旧 JSON 方式と違い、書き込み途中のクラッシュでデータが壊れない）。

### 記憶システム (`memory_service.py`)

カテゴリでルーティングされる長期記憶。DB の `profile`/`pending_log` テーブルと、`projects.json`（`memory/` 配下の JSON）で構成される:
- **ルーティング**: 安価なルーターモデルが各質問を `profile` / `projects` / `none` に分類し、そのスライスだけをプロンプトに読み込む。失敗時は `none` にフォールバック（安全側）。
- **書き込み経路**: 各返信の後、Q/A を非同期で `pending_log` テーブルに追記する。
- **コンパクション（圧縮）**: `flush_if_needed`（`MEMORY_MAINTENANCE_MINUTES` ごとのバックグラウンドループ。返信後にも便乗して発火する）は、Q/A 件数のしきい値（`MEMORY_FLUSH_THRESHOLD_ENTRIES`）を超えるかボットがアイドルのときに `pending_log` を `profile` テーブル/`projects.json` へ圧縮統合する。フェイルセーフ設計で、パース失敗時は pending ログを**保持**（リトライ）するが、3回連続失敗した場合 — またはセーフティフィルタによるブロック時 — は無限ループを避けるためクリアする。`io_lock` + `flush_lock` で保護されている。

### 文書パイプライン (`document_service.py`)

添付ファイルはチャットをブロックしないよう別系統で処理される。`enqueue_from_message` がタスクを DB の `queue_tasks` テーブルに書き込み、単一のワーカーコルーチンが `pending` タスクを取得し、一時ファイルへダウンロードし、検証し、Gemini へアップロードし、構造化 JSON へ要約し、要約を `projects.json` へ保存し、返信を生成し、その後アップロード済みファイルと一時ファイルを削除する。起動時、`processing` のまま止まっているタスク（処理中にボットがクラッシュしたもの）は `pending` へ戻される。完了済みタスクは `QUEUE_MAX_FINISHED_TASKS` 件に切り詰められる。タスクの取得（claim）は SELECT と UPDATE を同一トランザクションで行うため二重取得されない。

### Gemini 呼び出し (`gemini_service.py`)

`generate(...)` により2つの生成経路が選択される:
- **グラウンディング ON + 画像なし** → Generative Language API への直接 REST 呼び出しで `google_search` ツールを使い、`_parse_grounding_response` でパースする。非 200/例外の場合は `GROUNDING_MODEL_NAME` で一度リトライし、その後 SDK へフォールバックする。
- **それ以外** → SDK の `generate_content_async`（この経路は画像処理と SDK 側のグラウンディングフォールバックも担う）。

Gemini のテキストは必ず `storage.safe_response_text` 経由で読むこと — `response.text` はプロパティで、候補がブロック（`PROHIBITED_CONTENT`）されると**例外を投げる**ため、単なる `getattr` では防げない。モデルの JSON は `storage.extract_json_object` でパースすること（``` フェンスを除去し、ネストしたオブジェクトにも耐える波括弧バランス抽出を行う）。

### 親密度 / 感情 (`affinity_service.py`)

ユーザーごとの2次元感情（valence/arousal、30分の半減期で 50 へ減衰）に加えて、緩やかに変動する親密度。DB の `affinity` テーブルに永続化される。モデルには返信の末尾に `[V:X, A:Y]` タグを付けるよう指示しており、`handle_chat_message` が正規表現でそのタグを抽出・除去してから送信し、その後に親密度を更新する。保存は `asyncio.Lock` で直列化される。

### ボイス / TTS (`voice.py`)

`play_tts_response` は返信を **gTTS**（`lang="ja"`、API キー不要）で音声合成し、mp3 を **OS の一時ディレクトリ**（`index/` ではない）へ書き出し、`index/` にある `ffmpeg.exe`（`resolve_script_path` で解決）を使って `discord.FFmpegPCMAudio` 経由で再生する。`discord.VoiceClient` が接続中かつ再生中でない場合を除いて早期リターンするため、ボイスチャンネル外では黙って何もしない。一時ファイルは再生終了時の `after` コールバックで削除される。キューは無く、再生中に来た返信の読み上げは単にスキップされる。

### スラッシュコマンド (`commands.py`)

すべてのコマンドは `register_commands` で登録され、`on_ready` で同期される。全一覧: `/functions`（機能ハブ。エフェメラル）、`/join` + `/leave`（ボイスチャンネル）。`/functions` はハブパネルを開き、ボタンで各機能へ分岐する: **⚙️ キャラ設定**（`SettingsView` に変身）、**📋 会話要約**（`SummaryView` に変身）、**📈 会話統計**（`StatsView` に変身。ユーザー別ダッシュボード）、**💬 自発会話**（`AutoTopicView` に変身。自発的な話題提供の ON/OFF・しきい値設定）、**📊 グラフ**（利用トークングラフをその場で表示）、**🔄 NGワード再読込**、**🔍 Google検索**（グラウンディングのその場トグル。確定なしで即 `BotState` に反映）、**🚫 ブロック解除**（**管理者専用**。ブロック中ユーザーの選択メニューから解除。管理者以外にはボタン自体を非表示）。旧コマンド（`/status`、`/summarize`、`/graph`、`/ng_reload`、`/unblock`、および `/ai`、`/set`、`/gemini`、`/setting`、`/reset`）は削除済み — 設定・要約・利用グラフ・NG再読込・ブロック解除は `/functions` ハブに、テキストチャットは mention モードの `on_message` パイプラインに統合された。

### キャラ設定 UI (`settings_view.py`, `commands.py`, `choices.py`)

`/functions` のハブ（`functions_view.py` の `FunctionsView`、エフェメラル）から **⚙️ キャラ設定** を選ぶと、同じメッセージが `SettingsView` に変身する（ペルソナ/モデル/応答モードのセレクト + 確定/戻る/リセットボタン、120秒タイムアウト）。一時的な選択値は View 上に保持され、**確定 (Confirm)** ボタンを押したときにのみ `BotState` に書き込まれる — それまではグローバル状態を一切変更しない。Google検索（グラウンディング）のトグルはハブ側にあり、こちらは押した瞬間に `BotState` へ反映される（ハブは「その場で実行」系のため）。`choices.py` はペルソナ/モデル/モード、および要約期間（`SUMMARY_PERIOD_CHOICES`）の選択肢リストと表示名マップの唯一の情報源。

### 機能ハブ / 会話要約 (`functions_view.py`, `summary_view.py`, `stats_view.py`)

`/functions` はエフェメラルな `FunctionsView`（ハブ）を開く。Discord の View は最大5行のため設定と要約を1枚に合体できず、ボタンで選んだ機能のパネルへ**同じメッセージを差し替える**方式で統合している。⚙️キャラ設定→`SettingsView`、📋会話要約→`SummaryView`、📈会話統計→`StatsView`、💬自発会話→`AutoTopicView`、🚫ブロック解除→`UnblockView`（管理者のみボタン表示）はメッセージを差し替え、📊グラフ・🔄NG再読込は別のエフェメラルメッセージで結果を返す。🔍Google検索はハブ上のトグルで、押した瞬間に `BotState.grounding_enabled` を反転する。各サブパネルは共通の **↩️ 戻る** ボタン（`view_parts.py` の `BackButton`）を持ち、`make_hub`（`FunctionsView.clone` を渡す）でハブを再生成して同じメッセージを戻す — `view_parts.py` が `functions_view.py` を import すると循環参照になるため、生成関数を渡す形にしている。**会話要約**は期間（1時間〜1か月）と表示範囲（自分だけ/全員）を選んで実行する。`generate_channel_summary` が対象期間のメッセージを `fetch_messages_since` で取得（`SUMMARY_MAX_MESSAGES` 件超過で自動停止）、`build_summary_source_text` で**発言者名つき**に整形し、`build_summary_prompt`（`prompting.py`）＋ `BotState` の選択モデル/ペルソナで議事録を生成する。生成結果は感情タグ除去・Markdown見出し除去・`screen_bot_response` を経て、表示範囲に応じてチャンネル投稿またはエフェメラル表示される。

### 自発的な会話 (`auto_topic_service.py`, `auto_topic_view.py`)

一定時間会話が無いテキストチャンネルへ、過去の話題をもとにボットから話題提供する機能。デフォルトは**無効**で、`/functions` → **💬 自発会話** の `AutoTopicView`（間隔セレクト + ON/OFF トグル + 確定/キャンセル。確定まで `BotState` を変更しない）から有効化する。しきい値は `AUTO_TOPIC_INTERVAL_CHOICES`（1/3/6/12/24時間、`choices.py`）から選ぶ。

- **監視対象**: ボットが会話に参加した（`on_message` パイプラインを通過した）ギルドチャンネルが `watch_channel` で自動登録される。**DM は対象外**。最終活動時刻は、メンション判定より**前**に呼ばれる `mark_activity` により「ボット宛てでない人間の発言」でも更新される（会話が活発なチャンネルに割り込まないため）。
- **発火条件**: `auto_topic_loop`（`AUTO_TOPIC_CHECK_MINUTES` ごとの見回り）が「無会話時間 ≥ しきい値」かつ「前回の自動投稿より後に人間の発言がある」チャンネルにのみ投稿する。つまり一度話題提供したら、誰かが発言するまで同じチャンネルには再投稿しない（沈黙中の連投防止）。生成失敗時は記録を残さず、次回の見回りで自然にリトライされる。
- **話題生成**: 現在のペルソナ + プロフィール記憶（ルーターは使わず `profile` 固定）+ チャンネル履歴（`AUTO_TOPIC_HISTORY_LIMIT` 件）から `build_auto_topic_prompt`（`prompting.py`）で組み立て、グラウンディング無効で生成する。感情タグは除去のみ（相手が特定できないため親密度は更新しない）。NG ワードマスクは適用するが、モデレーションで不適切と判定された場合は謝罪文に差し替えず**投稿自体を見送る**（唐突な謝罪の投稿を防ぐ）。
- **状態はインメモリのみ**（`BotState` の `auto_topic_*` フィールド）: 有効/無効・しきい値・監視チャンネル・投稿記録はすべて再起動でリセットされる。

### ユーザー別会話統計 (`stats_service.py`, `stats_view.py`)

`/functions` の **📈 会話統計** から開く、ユーザー別の会話ダッシュボード。**すべてエフェメラル（非公開）で表示される。** 集計対象は「ボットへの発言のみ」で、`on_message` パイプラインの `handle_chat_message` 直前に `stats.record(...)` を fire-and-forget で呼んで蓄積する（失敗してもチャット本体は止めないフェイルオープン）。データは DB の `user_stats` テーブルに永続化され、`asyncio.Lock` で保存を直列化する（単一プロセス前提）。

- **集計データ**: ユーザーごとに累計発言回数・時間帯別ヒストグラム（`hourly[24]`、`STATS_TIMEZONE`＝`Asia/Tokyo` の「時」に変換）・直近発言サンプル（`STATS_RECENT_SAMPLES_MAX` 件のリングバッファ）を記録する。
- **話題・性格の LLM 解析** (`ensure_analysis`): 直近サンプルを Gemini に渡し、「よく話す話題」と「ビッグファイブ性格特性」を JSON で推定する。結果は `STATS_ANALYSIS_TTL_HOURS` の間キャッシュし、TTL 内なら再計算しない。LLM 呼び出し・パース失敗時は既存キャッシュにフォールバックする。性格推定は `STATS_PERSONALITY_MIN_SAMPLES` 件未満のとき「データ不足」として描画をスキップする。
- **項目別グラフ** (matplotlib、`usage_graph.py` と同じ日本語フォント作法): 時間帯＝棒グラフ / 話題＝重み付き横棒 / 性格＝ビッグファイブのレーダーチャート。項目セレクトで「全部 / 時間帯 / 話題 / 性格推定」を切り替えられる。
- **権限**: 一般ユーザーは**自分の統計のみ**閲覧できる（対象は `viewer_id` に固定）。**管理者のみ**、ユーザー選択メニューで他メンバーの統計を見られ、発言回数ランキング（全体比較）ボタンも使える。権限判定は `interaction.user.guild_permissions.administrator` によるサーバー側判定で、`RankingButton` 側でも二重にチェックする。
- **再起動でリセットされない**: `user_stats.json`（`memory/` 配下）に永続化される。調整値（TTL・上限・タイムアウト・タイムゾーン）は `constants.py` の `STATS_*` に集約。

## 規約と落とし穴

- **パス解決**（`constants.py`, `paths.py`）: `BASE_DIR = index/`、`PROJECT_ROOT` はその親。ペルソナファイルは `"../prompts/prompt_tsun.txt"` のような値（`index/` 基準）として保存・選択され、`resolve_prompt_path` で解決される。`memory/`・`ng/`・`prompts/` はプロジェクトルート直下にある。
- **設定**: `settings_loader.load_settings()` は `config_private` があればそれを、なければ `config`（コミット済みのテンプレート）をインポートする。実際のキーを `config.py` に置かないこと。
- **調整値は `constants.py` に置く** — モデル名、タイムアウト、しきい値、上限値。新規もインラインではなくここに追加すること。
- **モデレーションはフェイルオープン**: タイムアウト/例外時、`content_moderator.check` は *safe* を返してボットを動かし続ける。Gemini のプロンプトブロックは *unsafe* として扱う。この挙動を維持すること — 一時的な API エラーでユーザーをブロックしないこと。
- **再起動でリセットされる状態**（インメモリのみ）: 利用/トークン履歴（`usage_graph`、500件のリングバッファ）、`channel_reset_points`（`/functions` → ⚙️キャラ設定パネルの会話リセットのアンカー）、自発的な会話の設定・監視状態（`auto_topic_*`）。永続化される状態は `memory/bot.db`（SQLite: affinity / user_stats / queue_tasks / pending_log / profile）、`memory/projects.json`、`ng/blacklist.json`。`memory/profile.json` は手書き追加用の受け皿で、起動時に DB へ吸収され `{}` に戻る。
- **単一プロセス前提**: DB は `database.py` 内部の `threading.Lock` + トランザクションで、JSON ファイル（profile/projects/blacklist）はプロセス内 `asyncio.Lock` で保護される。プロセス間ロックは存在しない。DB は WAL モードのため、稼働中でも `sqlite3` CLI 等からの**読み取り**は安全（外部からの書き込みはしないこと）。
- **Discord の制限**: 2000文字を超える返信は `send_long_reply`/`split_message` を通す。グラウンディング返信はリンクの埋め込みプレビューを抑制する。
- **`ng/NG_WORD_private.txt`** は gitignore されており、読み込み時に `NG_WORD.txt` の上にマージされる。`/functions` の 🔄 NGワード再読込 で再起動なしに再読み込みできる。
