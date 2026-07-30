# CLAUDE.md

このファイルは、このリポジトリで作業する際の Claude Code (claude.ai/code) 向けガイダンスです。

## 概要

Google Gemini をバックエンドに持つ Discord ボット。ペルソナベースのテキストチャット、ボイスチャンネルでの TTS 読み上げ（`/join` 中のみ・FFmpeg 不要）、画像/文書のマルチモーダル入力、長期記憶、ユーザーごとの親密度/感情トラッキング、コンテンツモデレーション、三審制ブラックリスト、チャンネル会話の議事録要約、ユーザー別会話統計ダッシュボード、無会話チャンネルへの自発的な話題提供、記念日の自動お祝い、リマインダーを備える。ソースはすべて `index/` にある。ユーザー向け文字列・ログ出力（`print`）・コードコメントはすべて日本語。新規に追加するものも日本語で統一すること。

## ボットの起動

このリポジトリにはビルド手順・リンター設定・テストスイートは存在しない。開発は「実行して挙動を観察する」スタイル。

```bash
# 1. 依存関係のインストール (Python 3.10+)
pip install -r requirements.txt

# 2. シークレットの用意: index/config_private.py を作成する（gitignore 済み。config.py より優先される）
#    中身は API_KEY_GEMINI = "..." と TOKEN_DISCORD = "..."

# 3. Opus 共有ライブラリは /join のボイス TTS でのみ必要（FFmpeg はもう使わない）
#    Windows: 不要（discord.py が opus の DLL を同梱している）
#    Linux:   sudo apt install -y libopus0

# 4. index/ ディレクトリの中から実行する:
cd index
python bot.py     # 成功時に "ログイン成功: <bot>" と表示される
```

**必ず `index/` の中から実行すること。** モジュール群はパッケージ化されていないフラットな構成（`__init__.py` なし）で、互いをベア名でインポートし合う（`from app_state import BotState`）。プロンプトのパスも `index/` を基準に解決される（「パス解決」の項を参照）。

## アーキテクチャ

### 配線 (`bot.py`)

`bot.py` がコンポジションルート（合成の起点）。設定を読み込み、`genai` を構成し、`Database`（`database.py`、後述）を最初に初期化してから、各サービスのインスタンスを1つずつ生成して、DI フレームワークを使わず手作業で相互に注入する。共有される可変ランタイム状態はすべて単一の `BotState` データクラス（`app_state.py`）にまとめられ、必要なサービスに渡される。**ただしペルソナ・モデル・応答モードなど「サーバーごとに変わる設定」は `BotState` 直下ではなく `GuildSettings` に入っている**（「ギルド単位の設定」の項を参照）。`register_commands`（`commands.py`）がスラッシュコマンドを `CommandTree` に登録する。`on_ready` でコマンドを同期し、バックグラウンドタスク `memory_maintenance_loop` を開始し、文書ワーカーのコルーチンを起動する。

### ギルド単位の設定 (`app_state.py`)

同じボットを複数の Discord サーバーへ招待しても設定が混ざらないよう、可変設定は**サーバー（ギルド）ごとに分離**されている。

- **`GuildSettings`**: ペルソナ（`current_prompt_file`）・モデル（`current_model_name`）・応答モード（`current_respond_mode`）・グラウンディング（`grounding_enabled`）・会話リセット地点（`channel_reset_points`）・自発会話の設定と監視状態（`auto_topic_*`）・記念日の投稿記録（`anniversary_posted_dates`）を持つ。
- **`BotState.get_guild(guild_id)`**: そのサーバーの `GuildSettings` を返す。未登録なら初回アクセス時にデフォルト値で自動生成する。**`guild_id=None`（DM）は `BotState.dm_settings` を返す** — DM にはギルドが無いため、全 DM で1スロットを共有する。`iter_all_guild_settings()` は「全サーバー + DM」を横断する見回り処理用。
- **`BotState` に残すもの**: 最終活動時刻（`last_activity_timestamp`）とバックグラウンドタスクの起動フラグ類のみ（サーバーに依存しないプロセス全体の状態）。
- **永続化**: 設定値（ペルソナ・モデル・応答モード・グラウンディング・自発会話の ON/しきい値）と記念日の投稿記録は DB に保存され、起動時に `BotState.restore_from_database(db)`（`bot.py` の `Database` 初期化直後に1回）が復元する。**設定を変えたら `await guild_settings.persist()` を呼ぶこと** — UI の確定ボタンやトグルの直後がその場所。保存はフェイルオープン（失敗してもインメモリの変更は残る）。`get_guild()` が新しく作る `GuildSettings` には `attach()` で DB が引き継がれるので、呼び出し側は何もしなくてよい。
- **永続化しないもの**: `channel_reset_points` / `auto_topic_channels` / `auto_topic_posted_at` は実行時に自然に再構築される一時状態のため、意図的にインメモリのみ。ここを永続化したくなった場合はチャンネル単位の書き込みが毎メッセージ走る点に注意すること。
- **規約**: サービス層は `BotState` から設定を勝手に読まないこと。`bot.py` / `commands.py` が `state.get_guild(message.guild.id if message.guild else None)` で解決し、**`GuildSettings` かその中の値（例: `model_name`）を引数で渡す**。`GeminiService.generate()` / `summarize_document()` が `model_name` を必須引数に取るのはこのため（サービスが「どのギルドの設定か」を知らずに済む）。固定モデルを使う経路（`stats_service.py` のルーターなど）は `ROUTER_MODEL_NAME` を明示的に渡す。UI の各 View も `BotState` ではなく `GuildSettings` を受け取る。

### `on_message` パイプライン (`bot.py`)

ボット以外のメッセージはすべて、決まった順序の関門を通過する。編集時はこの順序を守ること:

1. **応答モードのゲート** — `"mention"` モード（デフォルト）では、DM 以外でボットへのメンションが無いメッセージは無視される。
2. **ブラックリスト**（`blacklist_service`）— ブロック中のユーザーは黙って破棄される。
3. **NG ワードフィルタ**（`ng_word_service`）— クリーニング済みテキストへの正規表現マッチ → `notify_violation`。
4. **ユーザー入力の AI モデレーション＋記憶ルーティング＋リマインダー抽出**（`content_moderator.check_and_route`）→ 不適切なら `notify_violation`。安全なら記憶カテゴリ（`profile`/`projects`/`none`）と、リマインダー依頼なら日時＋本文も同時に得る。**対象テキストが同一なので3つの判定を1回の Gemini 呼び出しに統合して API コストを抑えている**（個別に呼べば3回）。戻り値は `(is_safe, reason, category, reminder)` の4つ。
5. **リマインダー**（`reminder_service.create_from_request`）— 依頼が抽出されていれば登録し、確認だけ返して以降の処理は行わない（同じ発言にチャット返信も返すと二重投稿になるため）。日時が過去・解釈不能などで登録を見送った場合は `None` が返るので、通常の会話へ流す。
6. **ルーティング**: 文書添付（`pdf/txt/csv/md`）は非同期の `DocumentService` ワーカーへキュー登録され、後で回答される（ワーカー側で別途 `route_category` する）。それ以外は `handle_chat_message` が同期的に実行される。同期チャット返信の後は、必ず `play_tts_response`（`voice.py`）が呼ばれて読み上げを試みる — ボットが `/join` でボイスチャンネルに接続中でない限り no-op（何もしない）。再生中に届いた返信はキューに積まれ、順番に読み上げられる。

`notify_violation` は違反を1回記録し、3回目でユーザーをブラックリスト登録する（`BLACKLIST_MAX_VIOLATIONS`）。NG ワードとモデレーションは、送信前に**ボット自身の応答に対しても再度**適用される（応答中の NG ワードは ● でマスクされ、不適切な応答は定型の謝罪文に差し替えられる）。

### プロンプト組み立て (`handle_chat_message` → `prompting.py`)

メッセージごとに、単一のフラットなプロンプト文字列を組み立てる。構成要素は、ペルソナのテンプレート + 親密度/感情ブロック + 選択された長期記憶 + 文書要約 + チャンネル履歴 + 現在の質問。Gemini 呼び出しは `(text, usage_metadata, grounding_sources)` を返す。

### データベース (`database.py`)

SQLite（`memory/bot.db`、WAL モード）が構造化データの永続化先。SQL 文はすべて `database.py` に集約されている。テーブルは7つ: `affinity`（親密度・感情）、`user_stats`（会話統計）、`queue_tasks`（文書処理キュー）、`pending_log`（記憶圧縮前の一時ログ。`user_id` 列でユーザー別）、`profile`（プロフィール記憶。`(user_id, key)` の複合主キーでユーザー別。値は JSON 文字列）、`guild_settings`（サーバーごとの設定。`guild_id` が主キー。DM は `DM_SETTINGS_GUILD_KEY`＝`0`）、`anniversary_posts`（記念日のお祝い投稿記録。`(guild_id, user_id, label)` が主キー）、`reminders`（リマインダー。`status` は `pending`/`done`/`canceled`。見回り用に `(status, remind_at)` の索引つき）。`projects.json` は LLM が構造を自由に決めるため、テーブル化せず JSON ファイルのまま（ただし `{"_schema": "per_user_v1", "users": {user_id: {...}}}` の形でユーザー別に束ねる）。**`affinity`/`user_stats`/`profile`/`pending_log`/`projects.json` はすべて `user_id` で分離され、記憶は完全にユーザーごとに独立している**（他人の記憶が混ざらない＝プライバシー保護）。

- **スキーマ変更の手順**: `SCHEMA_VERSION` を1つ上げて `_MIGRATIONS` にそのバージョンの SQL 文を追記する。適用済みバージョンは `PRAGMA user_version` に記録され、起動時に未適用分だけが実行される。`_CREATE_TABLE_STATEMENTS`（初版の形）は変更禁止 — 変更は必ず `_MIGRATIONS` 側で行う（実例: バージョン2 = `profile` テーブル追加、バージョン3 = `profile` の複合主キー化＋`pending_log.user_id` 追加、バージョン4 = `guild_settings` / `anniversary_posts` テーブル追加、バージョン5 = `reminders` テーブル追加）。
- **ユーザー別記憶への移行（バージョン3）**: 導入前は単一ユーザーのテスト運用のため記憶を「全体共有の1スペース」で持っていた。移行時、旧・全体共有データ（`profile` 行・`pending_log` の未割当行・`projects.json` の中身）は失わないよう `LEGACY_MEMORY_OWNER_ID`（`constants.py`。旧・唯一の実利用者の Discord ユーザーID）の記憶として引き継ぐ。SQL では担えない「旧行を owner へ割り当てる」後始末は `_migrate_legacy_profile_rows`（冪等。スキーマ移行本体とは別トランザクション）が行う。引き継ぎ元が不要になれば `LEGACY_MEMORY_OWNER_ID` を `""` にしてよい。
- **旧 JSON からの取り込み**: 初回起動時、旧ファイル（`affinity.json`/`user_stats.json`/`queue.json`/`pending_log.txt`）が残っていれば DB へ取り込み、`<元の名前>.imported.bak` に改名する（バックアップ兼二重取り込み防止）。
- **profile.json は手書き追加用の受け皿**: 毎起動時、`profile.json` に中身があれば `profile` テーブルへマージ（同名キーはファイル側が勝つ）し、ファイルを `{}` に戻す。ユーザー別記憶に対応するため形式は `{ユーザーID: {キー: 値, ...}, ...}`（値が dict でない旧・全体共有形式は owner へ取り込む後方互換あり）。JSON として読めない場合はファイルを残して警告のみ（手書きミスでデータを失わない）。
- **並行処理**: 各サービスが `asyncio.to_thread`（＝別スレッド）から呼ぶため、接続は `check_same_thread=False` + 内部の `threading.Lock` で直列化。書き込みはトランザクションで原子的（旧 JSON 方式と違い、書き込み途中のクラッシュでデータが壊れない）。

### 記憶システム (`memory_service.py`)

カテゴリでルーティングされる長期記憶。**すべて `user_id` でユーザー別に分離される。** DB の `profile`/`pending_log` テーブルと、`projects.json`（`memory/` 配下の JSON。`users[user_id]` にユーザーごとのスライスを持つ）で構成される:
- **ルーティング**: 各質問を `profile` / `projects` / `none` に分類し、**そのユーザーの**スライスだけをプロンプトに読み込む。チャット主経路では `content_moderator.check_and_route` がモデレーションと同時に分類する（`route_category` は文書ワーカーなど主経路以外で使う）。`route_category` はあいさつ・相槌などの短い定型発言を正規表現（`_looks_memory_irrelevant`）で先に弾き、LLM を呼ばず `none` を即決して API を節約する。失敗時は `none` にフォールバック（安全側）。
- **書き込み経路**: 各返信の後、Q/A を発言者の `user_id` 付きで非同期に `pending_log` テーブルへ追記する。
- **コンパクション（圧縮）**: `flush_if_needed`（`MEMORY_MAINTENANCE_MINUTES` ごとのバックグラウンドループ。返信後にも便乗して発火する）は、`pending_log` を持つユーザーを列挙し、**ユーザーごとに個別に**圧縮する（他人の会話ログを同じ Gemini 呼び出しに混ぜない＝プライバシー保護）。各ユーザーは Q/A 件数のしきい値（`MEMORY_FLUSH_THRESHOLD_ENTRIES`）を超えるかボットがアイドルのときに、そのユーザーの `pending_log` を `profile` テーブル/`projects.json` の該当スライスへ圧縮統合する（他ユーザーのスライスには触れない）。1回のフラッシュで処理する人数は `MEMORY_FLUSH_MAX_USERS_PER_RUN` で上限を設け、一度に大量の API 呼び出しが走らないようにする（残りは次回の見回りで処理）。フェイルセーフ設計で、パース失敗時は pending ログを**保持**（リトライ）するが、3回連続失敗した場合 — またはセーフティフィルタによるブロック時 — は無限ループを避けるためそのユーザー分をクリアする。連続失敗回数はユーザー別に数える。`io_lock` + `flush_lock` で保護されている。

### 文書パイプライン (`document_service.py`)

添付ファイルはチャットをブロックしないよう別系統で処理される。`enqueue_from_message` がタスクを DB の `queue_tasks` テーブルに書き込み（`author_id` を含む）、単一のワーカーコルーチンが `pending` タスクを取得し、一時ファイルへダウンロードし、検証し、Gemini へアップロードし、構造化 JSON へ要約し、要約を**投稿者（`author_id`）の** `projects.json` スライスへ保存し、返信を生成し、その後アップロード済みファイルと一時ファイルを削除する。起動時、`processing` のまま止まっているタスク（処理中にボットがクラッシュしたもの）は `pending` へ戻される。完了済みタスクは `QUEUE_MAX_FINISHED_TASKS` 件に切り詰められる。タスクの取得（claim）は SELECT と UPDATE を同一トランザクションで行うため二重取得されない。

### Gemini 呼び出し (`gemini_service.py`)

`generate(...)` により2つの生成経路が選択される:
- **グラウンディング ON + 画像なし** → Generative Language API への直接 REST 呼び出しで `google_search` ツールを使い、`_parse_grounding_response` でパースする。非 200/例外の場合は `GROUNDING_MODEL_NAME` で一度リトライし、その後 SDK へフォールバックする。このサブモデルでのリトライは `GROUNDING_SUBMODEL_RETRY_ENABLED`（`constants.py`）で切り替え可能。`False` にすると失敗時の呼び出しを1回減らせる（即 SDK フォールバック）が、REST グラウンディング非対応モデルでは検索結果の質が下がりうる。
- **それ以外** → SDK の `generate_content_async`（この経路は画像処理と SDK 側のグラウンディングフォールバックも担う）。

`_generate_with_grounding` では、**ユーザーが選択したモデル（`model_name`）をリトライ用モデルで上書きしないこと**。上書きすると、サブモデルでのリトライも失敗したときの最終 SDK フォールバックが選択モデルではなく `GROUNDING_MODEL_NAME` で実行され、ユーザーの設定とは別モデルの回答が黙って返る。リトライ中に実際に使うモデルは `effective_model` として分けて持つ。

Gemini のテキストは必ず `storage.safe_response_text` 経由で読むこと — `response.text` はプロパティで、候補がブロック（`PROHIBITED_CONTENT`）されると**例外を投げる**ため、単なる `getattr` では防げない。モデルの JSON は `storage.extract_json_object` でパースすること（``` フェンスを除去し、ネストしたオブジェクトにも耐える波括弧バランス抽出を行う）。

### 親密度 / 感情 (`affinity_service.py`)

ユーザーごとの2次元感情（valence/arousal、30分の半減期で 50 へ減衰）に加えて、緩やかに変動する親密度。DB の `affinity` テーブルに永続化される。モデルには返信の末尾に `[V:X, A:Y]` タグを付けるよう指示しており、`handle_chat_message` がそのタグを抽出・除去してから送信し、その後に親密度を更新する。保存は `asyncio.Lock` で直列化される。

**感情タグを扱うコードはこのモジュールに集約すること。** タグの正規表現は `_AFFINITY_TAG_PATTERN` の1か所だけに置き、外からは次の2つの関数を使う — `extract_affinity_changes(text)` は `(本文, (機嫌の変化量, テンションの変化量))` を返し（タグが無ければ変化量は `None`、本文は無加工）、`strip_affinity_tag(text)` は除去だけを行う（自発的な会話・記念日のお祝い・会話要約のように、特定の相手への返信ではなく親密度を更新しない経路で使う）。以前は同じ正規表現が `bot.py` / `auto_topic_service.py` / `summary_view.py` に3重定義されており、空白の許容などに差異が生まれるとタグがそのままユーザーへ露出する不具合に直結していた。

### ボイス / TTS (`voice.py`)

`play_tts_response(message, response_text)` は、**ボットが `/join` でボイスチャンネルに接続している間だけ** AI の返信を読み上げる。未接続・DM・整形後に本文が残らない場合は黙って何もしない（no-op）。チャット本体は読み上げの成否に影響されないフェイルオープン設計。

**FFmpeg には依存しない。** gTTS（`lang=TTS_LANG`、API キー不要）が返す mp3 を **miniaudio**（C ライブラリを同梱した pip の binary wheel。exe 不要）で**メモリ上のまま** `TTS_SAMPLE_RATE`（48kHz）/ `TTS_CHANNELS`（ステレオ）/ 16bit の生 PCM へデコードし、`discord.PCMAudio` に渡す。Discord のボイス送信はこの形式をそのまま受け取れるため、変換用の外部プロセスも一時ファイルも要らない（CPU 1コア・メモリ 1GB 級の VPS で動かすための構成）。**`discord.FFmpegPCMAudio` を使う実装に戻さないこと** — 返信のたびに別プロセスが起動し、小メモリ環境では OOM の原因になる。

- **合成は必ず別スレッドで**: gTTS のネットワーク呼び出しと miniaudio のデコードはどちらも同期処理のため、`asyncio.to_thread` + `asyncio.wait_for(timeout=TTS_TIMEOUT_SECONDS)` で実行する。イベントループ上で直接呼ぶとボット全体の応答が固まる。
- **読み上げキュー**: 再生中に届いた返信を捨てないよう、ギルドIDごとに `asyncio.Queue`（`maxsize=TTS_QUEUE_MAX`）を持つ。ワーカーは最初の依頼で起動し、キューが空になったら `_speech_queues` から自分を消して終了する（参加していないサーバー分のタスクを常駐させないため）。**キューが空だと判定してから登録を消すまでの間に `await` を挟まないこと** — 挟むとその隙に積まれた依頼が誰にも処理されず取り残される。キューが溢れた分は捨てる（古い返信を延々と喋り続けるより会話の現在地に追従する方が自然なため）。
- **再生の完了待ち**: `voice_client.play()` の `after` コールバックは discord.py の**再生スレッド**から呼ばれる。イベントループ側のオブジェクト（`asyncio.Event`）へは必ず `loop.call_soon_threadsafe` 経由で触ること。
- **接続は毎回見直す**: 読み上げ中に `/leave` されうるため、ワーカーはループのたびに `guild.voice_client` を取り直して接続状態を確認する。
- **読み上げテキストの整形** (`build_speech_text`): URL・コードブロック・インラインコード・カスタム絵文字・メンション・Markdown 記号を除去し、`TTS_MAX_CHARS` を超える分は `TTS_TRUNCATION_NOTICE` を付けて打ち切る。特に **URL は gTTS が1文字ずつ読み上げて極端に長くなる**ため必ず除去する。感情タグ `[V:X, A:Y]` は `bot.py` 側で `extract_affinity_changes` により除去済みのため、ここでは扱わない。
- 調整値は `constants.py` の `TTS_*` に集約する（インラインに書かない）。

### スラッシュコマンド (`commands.py`)

すべてのコマンドは `register_commands` で登録され、`on_ready` で同期される。全一覧: `/functions`（機能ハブ。エフェメラル）、`/join` + `/leave`（ボイスチャンネル）。`/functions` はハブパネルを開き、ボタンで各機能へ分岐する: **⚙️ キャラ設定**（`SettingsView` に変身）、**📋 会話要約**（`SummaryView` に変身）、**📈 会話統計**（`StatsView` に変身。ユーザー別ダッシュボード）、**💬 自発会話**（`AutoTopicView` に変身。自発的な話題提供の ON/OFF・しきい値設定）、**⏰ リマインダー**（`ReminderView` に変身。本人の予定の一覧・取り消し）、**📊 グラフ**（利用トークングラフをその場で表示）、**🔄 NGワード再読込**、**🔍 Google検索**（グラウンディングのその場トグル。確定なしで即そのサーバーの `GuildSettings` に反映）、**🚫 ブロック解除**（**管理者専用**。ブロック中ユーザーの選択メニューから解除。管理者以外にはボタン自体を非表示）。旧コマンド（`/status`、`/summarize`、`/graph`、`/ng_reload`、`/unblock`、および `/ai`、`/set`、`/gemini`、`/setting`、`/reset`）は削除済み — 設定・要約・利用グラフ・NG再読込・ブロック解除は `/functions` ハブに、テキストチャットは mention モードの `on_message` パイプラインに統合された。

### キャラ設定 UI (`settings_view.py`, `commands.py`, `choices.py`)

`/functions` のハブ（`functions_view.py` の `FunctionsView`、エフェメラル）から **⚙️ キャラ設定** を選ぶと、同じメッセージが `SettingsView` に変身する（ペルソナ/モデル/応答モードのセレクト + 確定/戻る/リセットボタン、120秒タイムアウト）。一時的な選択値は View 上に保持され、**確定 (Confirm)** ボタンを押したときにのみ `GuildSettings`（`/functions` 実行時に `interaction.guild_id` から解決したもの）に書き込まれる — それまでは共有状態を一切変更しない。Google検索（グラウンディング）のトグルはハブ側にあり、こちらは押した瞬間に `GuildSettings` へ反映される（ハブは「その場で実行」系のため）。**各 View は `BotState` ではなく `GuildSettings` を受け取る** ため、設定変更の影響はそのサーバー内（DM の場合は全 DM）に閉じる。`choices.py` はペルソナ/モデル/モード、および要約期間（`SUMMARY_PERIOD_CHOICES`）の選択肢リストと表示名マップの唯一の情報源。

### 機能ハブ / 会話要約 (`functions_view.py`, `summary_view.py`, `stats_view.py`)

`/functions` はエフェメラルな `FunctionsView`（ハブ）を開く。Discord の View は最大5行のため設定と要約を1枚に合体できず、ボタンで選んだ機能のパネルへ**同じメッセージを差し替える**方式で統合している。⚙️キャラ設定→`SettingsView`、📋会話要約→`SummaryView`、📈会話統計→`StatsView`、💬自発会話→`AutoTopicView`、⏰リマインダー→`ReminderView`、🚫ブロック解除→`UnblockView`（管理者のみボタン表示）はメッセージを差し替え、📊グラフ・🔄NG再読込は別のエフェメラルメッセージで結果を返す。🔍Google検索はハブ上のトグルで、押した瞬間に `BotState.grounding_enabled` を反転する。各サブパネルは共通の **↩️ 戻る** ボタン（`view_parts.py` の `BackButton`）を持ち、`make_hub`（`FunctionsView.clone` を渡す）でハブを再生成して同じメッセージを戻す — `view_parts.py` が `functions_view.py` を import すると循環参照になるため、生成関数を渡す形にしている。**会話要約**は期間（1時間〜1か月）と表示範囲（自分だけ/全員）を選んで実行する。`generate_channel_summary` が対象期間のメッセージを `fetch_messages_since` で取得（`SUMMARY_MAX_MESSAGES` 件超過で自動停止）、`build_summary_source_text` で**発言者名つき**に整形し、`build_summary_prompt`（`prompting.py`）＋ **そのサーバーの `GuildSettings`** の選択モデル/ペルソナで議事録を生成する。生成結果は感情タグ除去・Markdown見出し除去・`screen_bot_response` を経て、表示範囲に応じてチャンネル投稿またはエフェメラル表示される。

### 自発的な会話 (`auto_topic_service.py`, `auto_topic_view.py`)

一定時間会話が無いテキストチャンネルへ、過去の話題をもとにボットから話題提供する機能。デフォルトは**無効**で、`/functions` → **💬 自発会話** の `AutoTopicView`（間隔セレクト + ON/OFF トグル + 確定/キャンセル。確定まで `GuildSettings` を変更しない）から有効化する。**設定・監視状態はサーバーごとに独立している。**しきい値は `AUTO_TOPIC_INTERVAL_CHOICES`（1/3/6/12/24時間、`choices.py`）から選ぶ。

- **監視対象**: ボットが会話に参加した（`on_message` パイプラインを通過した）ギルドチャンネルが `watch_channel` で自動登録される。**DM は対象外**。最終活動時刻は、メンション判定より**前**に呼ばれる `mark_activity` により「ボット宛てでない人間の発言」でも更新される（会話が活発なチャンネルに割り込まないため）。
- **発火条件**: `auto_topic_loop`（`AUTO_TOPIC_CHECK_MINUTES` ごとの見回り）が「無会話時間 ≥ しきい値」かつ「前回の自動投稿より後に人間の発言がある」チャンネルにのみ投稿する。つまり一度話題提供したら、誰かが発言するまで同じチャンネルには再投稿しない（沈黙中の連投防止）。生成失敗時は記録を残さず、次回の見回りで自然にリトライされる。
- **話題生成**: 現在のペルソナ + チャンネル履歴（`AUTO_TOPIC_HISTORY_LIMIT` 件）から `build_auto_topic_prompt`（`prompting.py`）で組み立て、グラウンディング無効で生成する。**長期記憶（`profile`）は使わない** — 記憶はユーザー別に分離されており、特定ユーザーの個人記憶を共有チャンネルへの話題提供に使うとその人の私的情報が他の参加者へ漏れるため（プライバシー保護）。材料は全員が既に見ているチャンネル履歴だけにする。感情タグは除去のみ（相手が特定できないため親密度は更新しない）。NG ワードマスクは適用するが、モデレーションで不適切と判定された場合は謝罪文に差し替えず**投稿自体を見送る**（唐突な謝罪の投稿を防ぐ）。
- **状態の保存範囲**: 有効/無効としきい値は DB（`guild_settings` テーブル）に永続化され再起動後も維持される。監視チャンネル（`auto_topic_channels`）と投稿記録（`auto_topic_posted_at`）はインメモリのみで、再起動後はそのチャンネルでボットが再び会話に参加した時点から監視が始まる。

### 記念日・誕生日の自動お祝い (`auto_topic_service.py`, `memory_service.py`)

会話の中で出てきた誕生日・記念日を長期記憶に覚えておき、当日にボットからお祝いを投稿する機能。専用の UI は無く、記憶と自発会話の枠組みに相乗りしている。

- **記憶側**: 記憶圧縮（`memory_service._compact_memory`）のプロンプトが、月日が確定できる記念日を `profile` の `ANNIVERSARY_PROFILE_KEY`（= `"anniversaries"`）キーへ `[{"label": "誕生日", "month": 5, "day": 1}]` の構造化リストで保存する。既存エントリはマージ（同じ `label` は上書き）し、単発の予定は保存しない。ユーザーが `memory/profile.json` に手書きしても登録できる。
- **お祝い側**: `run_anniversary_checks()` が `ANNIVERSARY_CHECK_MINUTES`（既定60分）ごとの `anniversary_loop`（`bot.py`）から呼ばれる。`Database.profile_load_all_for_key()` で全ユーザーの記念日を一括取得し、`ANNIVERSARY_TIMEZONE`（`Asia/Tokyo`）基準の「今日」と月日が一致するユーザーを、そのユーザーが所属する各サーバーの**自発会話の監視チャンネル**（最終活動が最も新しいもの）でお祝いする。プロンプトは `build_anniversary_prompt`（`prompting.py`）。
- **二重投稿の防止**: `GuildSettings.anniversary_posted_dates`（`"{user_id}:{label}"` → `"YYYY-MM-DD"`）で同一サーバー・同一記念日の同日再投稿を防ぐ。記録は `record_anniversary_post()` がメモリと `anniversary_posts` テーブルの両方へ書き、起動時に復元されるため**当日に再起動しても再投稿しない**。主キーが `(guild_id, user_id, label)` の upsert なので毎年お祝いしても行は増えない。
- **フェイルセーフ**: `anniversaries` は LLM が書き込む自由記述寄りの記憶のため、型や形式が期待どおりでないエントリは静かにスキップする（例外で見回りを止めない）。
- **プライバシーと安全性**: 自発的な会話と同じ方針。材料はチャンネル履歴のみで**個人の `profile` は渡さない**（「今日が記念日」以上の私的情報を共有チャンネルへ出さない）。DM は対象外。NG ワードはマスクし、モデレーションで不適切と判定されたら謝罪文に差し替えず**投稿自体を見送る**。

### リマインダー (`reminder_service.py`, `reminder_view.py`)

会話から登録し、指定時刻に**依頼されたチャンネル**でメンション付きに通知する。専用コマンドは無い。

- **登録経路**: `content_moderator.check_and_route` がモデレーションと同じ1回の呼び出しで日時＋本文を抽出する（**リマインダーのために Gemini を追加で呼ばないこと** — 判定を足すならこのプロンプトに相乗りさせる）。相対表現を解釈させるため、プロンプトには現在日時（曜日つき、`REMINDER_TIMEZONE` 基準）を埋め込む。
- **検証は `ReminderService` 側**: 日時の解釈（複数フォーマットを順に試す）、過去・`REMINDER_MAX_DAYS_AHEAD` より先の拒否、`REMINDER_MAX_PENDING_PER_USER` の上限、本文の切り詰め。**登録を見送るときは `None` を返す**（呼び出し側は通常の会話へ流す）。上限超過だけは意図が明確なので案内文を返す。
- **通知は生成しない**: 正確さが最優先なので Gemini を呼ばず、`⏰ @ユーザー リマインダーです: 本文` のテンプレートで組み立てる。NG ワードのマスクだけ適用する（登録時にモデレーション済みのため再判定しない）。
- **二重通知の防止**: `Database.reminder_claim_due()` が SELECT と UPDATE を同一トランザクションで行い、期限到来分を `done` にしてから返す（`queue_tasks` と同じクレーム方式）。送信自体が失敗しても `pending` に戻さない — 重複通知より「時間外れの再通知」の方が困るため。
- **時刻の扱い**: 保存は UTC の ISO 8601 で**秒精度に統一**する（期限判定を文字列比較で行うため、桁数が混ざると比較が壊れる）。表示だけ `REMINDER_TIMEZONE` に変換する（`format_local`）。日時の書式に `strftime` の `%-m` / `%#m` は使わないこと（プラットフォーム依存）。
- **権限**: `/functions` → **⏰ リマインダー** で一覧・取り消し。表示は本人の分だけで、`reminder_cancel` は SQL の条件にも `user_id` を含めるため他人のものは操作できない。

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
- **fire-and-forget は `background.spawn(coro, label=...)` を使う（`asyncio.create_task` を直接呼ばない）**: asyncio のイベントループは実行中タスクを**弱参照**でしか保持しないため、`create_task` の戻り値を捨てるとタスクが実行途中でガベージコレクトされて消えることがある（Python 公式ドキュメントが明記している既知の落とし穴）。`spawn` は完了までタスクへの強参照を保持し、完了時に自動で取り除く。加えてタスク内の例外を `label` 付きでログに出す — `create_task` に投げっぱなしだと例外は回収時まで報告されず、そのまま失われることもある。特に `documents.run_worker()` のような常駐コルーチンでは、消えると添付ファイルの処理が二度と走らなくなる。
- **タイムゾーン名を表示・プロンプトに載せるときは `timeutil.timezone_label(self._tz)` を使う（`REMINDER_TIMEZONE` などの定数をそのまま書かない）**: `resolve_timezone` は tzdata の無い環境で UTC へフォールバックするため、定数を表示すると「実際は UTC の時刻なのに Asia/Tokyo と名乗る」食い違いが起きる。リマインダーの統合判定プロンプトでは、この食い違いがそのまま9時間ずれた日時での登録に直結する。
- **モデレーションはフェイルオープン**: タイムアウト/例外時、`content_moderator.check` は *safe* を返してボットを動かし続ける。Gemini のプロンプトブロックは *unsafe* として扱う。この挙動を維持すること — 一時的な API エラーでユーザーをブロックしないこと。**`check` / `check_and_route` の本体で例外を外へ漏らさないこと**: これらは `on_message` の主経路から呼ばれるため、抜けた例外はそのメッセージの処理ごと落とす。準備処理（タイムゾーンの解決など）を `try` の外に置くと、環境依存の失敗が全メッセージの無応答に直結する（実際に `ZoneInfo` の組み立てで起きた）。
- **タイムゾーンは `timeutil.resolve_timezone(name, purpose=...)` で解決する**: `ZoneInfo` を直接呼ばないこと。タイムゾーンデータベースの無い環境（`tzdata` を含まないスリムな Linux イメージなど）では例外になるため、この関数が UTC へのフォールバックと警告ログをまとめて引き受ける。タイムゾーン名そのもの（`STATS_TIMEZONE` / `ANNIVERSARY_TIMEZONE` / `REMINDER_TIMEZONE`）は用途ごとに `constants.py` で個別に調整できる形を保つ。
- **再起動でリセットされる状態**（インメモリのみ）: 利用/トークン履歴（`usage_graph`、500件のリングバッファ）、`channel_reset_points`（`/functions` → ⚙️キャラ設定パネルの会話リセットのアンカー）、自発的な会話の**監視状態**（`auto_topic_channels` / `auto_topic_posted_at`）。一方、`GuildSettings` の**設定値**（ペルソナ/モデル/応答モード/グラウンディング/自発会話の ON・しきい値）・記念日の投稿記録・リマインダーは DB に永続化され、再起動しても失われない。永続化される状態は `memory/bot.db`（SQLite: affinity / user_stats / queue_tasks / pending_log / profile）、`memory/projects.json`、`ng/blacklist.json`。`memory/profile.json` は手書き追加用の受け皿で、起動時に DB へ吸収され `{}` に戻る。
- **単一プロセス前提**: DB は `database.py` 内部の `threading.Lock` + トランザクションで、JSON ファイル（profile/projects/blacklist）はプロセス内 `asyncio.Lock` で保護される。プロセス間ロックは存在しない。DB は WAL モードのため、稼働中でも `sqlite3` CLI 等からの**読み取り**は安全（外部からの書き込みはしないこと）。
- **ログ出力とエンコーディング**: Windows の既定コンソールは cp932 で、絵文字を `print` すると `UnicodeEncodeError` でそこの処理が止まる。`bot.py` の冒頭で標準出力を UTF-8（`errors="replace"`）へ再設定しているので通常は問題ないが、**`bot.py` を経由しない単体スクリプトから同じモジュールを動かすときは `PYTHONIOENCODING=utf-8` を付けること**。
- **Discord の制限**: 2000文字を超える返信は `send_long_reply`/`split_message` を通す。グラウンディング返信はリンクの埋め込みプレビューを抑制する。
- **`ng/NG_WORD_private.txt`** は gitignore されており、読み込み時に `NG_WORD.txt` の上にマージされる。`/functions` の 🔄 NGワード再読込 で再起動なしに再読み込みできる。
