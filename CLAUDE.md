# CLAUDE.md

このファイルは、このリポジトリで作業する際の Claude Code (claude.ai/code) 向けガイダンスです。

## 概要

Google Gemini をバックエンドに持つ Discord ボット。ペルソナベースのテキストチャット、ボイスチャンネルでの TTS 読み上げ（`/join` 中のみ・FFmpeg 不要・edge-tts でペルソナごとに声が変わる）、画像/文書のマルチモーダル入力、長期記憶、ユーザーごとの親密度/感情トラッキング、コンテンツモデレーション、三審制ブラックリスト、チャンネル会話の議事録要約、ユーザー別会話統計ダッシュボード、無会話チャンネルへの自発的な話題提供、記念日の自動お祝い、リマインダー、返信への 👍👎 リアクションによるフィードバック収集、オンボーディング導線（サーバー参加時の案内と使い方パネル）を備える。ソースはすべて `index/` にある。ユーザー向け文字列・ログ出力（`print`）・コードコメントはすべて日本語。新規に追加するものも日本語で統一すること。

## ボットの起動

このリポジトリにはビルド手順・リンター設定・テストスイートは存在しない。開発は「実行して挙動を観察する」スタイル。

```bash
# 1. 依存関係のインストール (Python 3.10+)
pip install -r requirements.txt

# 2. シークレットの用意: プロジェクトルート直下（index/ の中ではない）に .env を作る。
#    cp .env.example .env してから API_KEY_GEMINI= と TOKEN_DISCORD= の右辺を実キーに書き換える。
#    .env は gitignore 済み。未設定のまま起動すると "キー未設定" を出して例外で停止する。

# 3. Opus 共有ライブラリは /join のボイス TTS でのみ必要（FFmpeg はもう使わない）
#    Windows: 不要（discord.py が opus の DLL を同梱している）
#    Linux:   sudo apt install -y libopus0
#
#    requirements.txt の discord.py は必ず [voice] 付きにすること。ボイス接続に必要な
#    davey（DAVE = Discord が 2026-03-02 に必須化した音声E2E暗号化）が入らず、
#    /join が RuntimeError('davey library needed in order to use voice') で失敗する。

# 4. index/ ディレクトリの中から実行する:
cd index
python bot.py     # 成功時に "ログイン成功: <bot>" と表示される
```

**必ず `index/` の中から実行すること。** モジュール群はパッケージ化されていないフラットな構成（`__init__.py` なし）で、互いをベア名でインポートし合う（`from app_state import BotState`）。プロンプトのパスも `index/` を基準に解決される（「パス解決」の項を参照）。

## サーバー運用 (`deploy/`)

本番は Oracle Cloud 上の Ubuntu（ユーザー `ubuntu`、`/home/ubuntu/Persona-Chat-in-Discord`）で **systemd のサービス名 `bot`** として常駐する。ユニットは `deploy/bot.service.example`（プレースホルダ入り・Git 管理対象）と `deploy/bot.service`（実値入り・gitignore 済み）の**2本立て**で、実パスやユーザー名を公開側に残さない。導入・運用コマンドは README の「7. 常時稼働の設定」にある。

- **`WorkingDirectory` は `index/`、`ExecStart` は `.venv/bin/python` の絶対パス**。systemd はシェルを介さないため `activate` は効かない。
- **`StartLimitIntervalSec=0`（再起動のレート制限を無効化）を戻さないこと**: 既定の「10秒で5回失敗したら諦める」だと一時的な回線障害でボットが二度と復帰せず、systemd を入れた目的（手動回復をなくす）が失われる。副作用として設定ミスでは永久に再起動ループになるため、`systemctl status bot` が `activating (auto-restart)` のままなら設定ミスを疑う運用が前提。なお **systemd 229 以降この指定は `[Unit]` に置く** — `[Service]` に書くと黙って無視される。
- **`ProtectSystem=strict` / `ProtectHome=yes` を足さないこと**: 実行中に `memory/`（SQLite DB・JSON）と `ng/` へ書き込むため、read-only エラーで記憶とモデレーションが黙って壊れる。入れるなら `ReadWritePaths=` で個別に開ける必要がある。
- **`WatchdogSec` は未導入**（依頼どおり、ハング型のフリーズが実際に観測されてから検討する）。`Restart=always` が拾えるのはプロセスが落ちるケースだけで、「生きているのに応答しない」状態は拾えない。導入にはアプリ側の `sd_notify(WATCHDOG=1)` 定期送信が必須で、送信が滞ると健全なプロセスまで殺す。
- **ユニットファイルは LF 改行必須**（`.gitattributes` で固定済み）。CRLF だと `ExecStart=` のパス末尾に `\r` が混入して起動に失敗し、原因が非常に分かりにくい。
- ログは journald（`journalctl -u bot -f`）。`print` の出力を即時反映させるため `PYTHONUNBUFFERED=1` を、日本語・絵文字で `UnicodeEncodeError` を起こさないため `PYTHONIOENCODING=utf-8` をユニット側で指定している。

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

SQLite（`memory/bot.db`、WAL モード）が構造化データの永続化先。SQL 文はすべて `database.py` に集約されている。テーブルは9つ: `affinity`（親密度・感情）、`user_stats`（会話統計）、`queue_tasks`（文書処理キュー）、`pending_log`（記憶圧縮前の一時ログ。`user_id` 列でユーザー別）、`profile`（プロフィール記憶。`(user_id, key)` の複合主キーでユーザー別。値は JSON 文字列）、`guild_settings`（サーバーごとの設定。`guild_id` が主キー。DM は `DM_SETTINGS_GUILD_KEY`＝`0`）、`anniversary_posts`（記念日のお祝い投稿記録。`(guild_id, user_id, label)` が主キー）、`reminders`（リマインダー。`status` は `pending`/`done`/`canceled`。見回り用に `(status, remind_at)` の索引つき。`recurrence` は `none`/`daily`/`weekly`/`monthly` で、繰り返しは通知後も `pending` のまま `remind_at` だけが進む）、`feedback`（返信への 👍👎 評価。`(message_id, user_id)` が主キーで押し替えは上書き。集計用に `(guild_id, created_at)` の索引つき）。`projects.json` は LLM が構造を自由に決めるため、テーブル化せず JSON ファイルのまま（ただし `{"_schema": "per_user_v1", "users": {user_id: {...}}}` の形でユーザー別に束ねる）。**`affinity`/`user_stats`/`profile`/`pending_log`/`projects.json` はすべて `user_id` で分離され、記憶は完全にユーザーごとに独立している**（他人の記憶が混ざらない＝プライバシー保護）。

- **スキーマ変更の手順**: `SCHEMA_VERSION` を1つ上げて `_MIGRATIONS` にそのバージョンの SQL 文を追記する。適用済みバージョンは `PRAGMA user_version` に記録され、起動時に未適用分だけが実行される。`_CREATE_TABLE_STATEMENTS`（初版の形）は変更禁止 — 変更は必ず `_MIGRATIONS` 側で行う（実例: バージョン2 = `profile` テーブル追加、バージョン3 = `profile` の複合主キー化＋`pending_log.user_id` 追加、バージョン4 = `guild_settings` / `anniversary_posts` テーブル追加、バージョン5 = `reminders` テーブル追加、バージョン6 = `reminders` へ繰り返し列（`recurrence` / `recurrence_anchor`）追加、バージョン7 = `feedback` テーブル＋索引追加、バージョン8 = `user_stats` へ統計収集の停止列（`stats_opt_out`）追加）。**`SCHEMA_VERSION` は自分が追加した分ちょうどまでしか上げないこと** — 番号を飛ばすと、後から埋めた小さいバージョンは `user_version` が既に先へ進んでいるため永久に実行されない。
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

### ボイス / TTS (`voice.py`, `voice_dsp.py`)

`play_tts_response(message, response_text, prompt_file)` は、**ボットが `/join` でボイスチャンネルに接続している間だけ** AI の返信を読み上げる。未接続・DM・整形後に本文が残らない場合は黙って何もしない（no-op）。チャット本体は読み上げの成否に影響されないフェイルオープン設計。`prompt_file` は `bot.py` が `guild_settings.current_prompt_file` を渡す（サービス層に `BotState` を読ませない規約どおり、値だけを渡す）。

**音声エンジンは edge-tts（主）＋ gTTS（退避）の二段構え。** `_fetch_mp3` が主エンジンの **edge-tts**（Microsoft Edge の読み上げが使うニューラル音声。API キー不要）で合成し、例外または空応答なら **gTTS**（`lang=TTS_LANG`）へ退避する。edge-tts は公式に third-party 利用が認められた API ではなく過去に Microsoft 側の仕様変更で使えなくなった経緯があるため、**この退避経路を消さないこと**（消すと壊れた瞬間に読み上げ機能ごと止まる）。退避中は話者・高さ・速さを再現できずキャラ差が失われるが、音色（EQ）だけは掛かる。退避と復旧のログは `_edge_tts_failing` で**状態が変わったときだけ**出す（失敗が続く間、返信のたびにログが溢れないようにするため）。

**FFmpeg には依存しない。** どちらのエンジンも返す mp3（24kHz モノラル）を **miniaudio**（C ライブラリを同梱した pip の binary wheel。exe 不要）で**メモリ上のまま** `TTS_SAMPLE_RATE`（48kHz）/ `TTS_CHANNELS`（ステレオ）/ 16bit の生 PCM へデコードし、`discord.PCMAudio` に渡す。Discord のボイス送信はこの形式をそのまま受け取れるため、変換用の外部プロセスも一時ファイルも要らない（CPU 1コア・メモリ 1GB 級の VPS で動かすための構成）。**`discord.FFmpegPCMAudio` を使う実装に戻さないこと** — 返信のたびに別プロセスが起動し、小メモリ環境では OOM の原因になる。

- **非同期と別スレッドの使い分け**: `_synthesize_pcm` は **async 関数**。edge-tts は async ネイティブなのでイベントループ上でそのまま `await` する（通信待ちでループは塞がらない）。同期処理である gTTS の呼び出しと、デコード＋EQ の CPU 処理だけを `asyncio.to_thread` へ逃がす。全体を `asyncio.wait_for(_synthesize_pcm(...), timeout=TTS_TIMEOUT_SECONDS)` で包む。**edge-tts を `asyncio.to_thread` の中で `asyncio.run()` して呼ばないこと** — イベントループが入れ子になり動作しない。
- **読み上げキュー**: 再生中に届いた返信を捨てないよう、ギルドIDごとに `asyncio.Queue`（`maxsize=TTS_QUEUE_MAX`）を持つ。積むのは **`(テキスト, ボイスプロファイル)` の組**で、プロファイルは `play_tts_response` の時点で `resolve_profile` により確定させる（待っている間にペルソナを切り替えられても、積まれた分は依頼時のキャラクターの声で読み上げる。新規に合成する分から新プロファイルが反映されれば十分）。ワーカーは最初の依頼で起動し、キューが空になったら `_speech_queues` から自分を消して終了する（参加していないサーバー分のタスクを常駐させないため）。**キューが空だと判定してから登録を消すまでの間に `await` を挟まないこと** — 挟むとその隙に積まれた依頼が誰にも処理されず取り残される。キューが溢れた分は捨てる（古い返信を延々と喋り続けるより会話の現在地に追従する方が自然なため）。
- **再生の完了待ち**: `voice_client.play()` の `after` コールバックは discord.py の**再生スレッド**から呼ばれる。イベントループ側のオブジェクト（`asyncio.Event`）へは必ず `loop.call_soon_threadsafe` 経由で触ること。
- **接続は毎回見直す**: 読み上げ中に `/leave` されうるため、ワーカーはループのたびに `guild.voice_client` を取り直して接続状態を確認する。
- **読み上げテキストの整形** (`build_speech_text`): URL・コードブロック・インラインコード・カスタム絵文字・メンション・Markdown 記号を除去し、`TTS_MAX_CHARS` を超える分は `TTS_TRUNCATION_NOTICE` を付けて打ち切る。特に **URL は音声エンジンが1文字ずつ読み上げて極端に長くなる**ため必ず除去する。感情タグ `[V:X, A:Y]` は `bot.py` 側で `extract_affinity_changes` により除去済みのため、ここでは扱わない。
- 調整値は `constants.py` の `TTS_*` に集約する（インラインに書かない）。

**キャラクター別ボイスプロファイル**: 「声の高さ（`pitch`）」と「話す速さ（`rate`）」は **edge-tts のサーバー側パラメータ**が、「音色（`eq`）」だけを `voice_dsp.py` の numpy 製バイクアッド EQ が担う。**`scipy` / `librosa` / `pydub` を持ち込まないこと** — 前2つは 1GB 環境に対して重すぎ、`pydub` は内部で FFmpeg プロセスを起動するため上記の方針と矛盾する。

- **どのキャラをどう鳴らすかは `constants.py` の `VOICE_PROFILES`**（キーは `current_prompt_file` の値、値は `voice` / `pitch` / `rate` / `eq`）。EQ の中身は `VOICE_EQ_PROFILES`。未登録のペルソナは `VOICE_PROFILE_FALLBACK` へ落ちる。調整はこの2つの辞書だけで完結させ、`voice_dsp.py` や `voice.py` にハードコードしないこと。
- **`pitch` / `rate` は edge-tts のネイティブ形式の文字列**（`"+10Hz"` / `"+5%"`。正規表現 `^[+-]\d+Hz$` / `^[+-]\d+%$` に一致しないと `Communicate` が `ValueError` を投げる）。**半音単位 `st` は使えない。**
- **「半音 → Hz」を計算で求めないこと**: 効き方が話者ごとに違い、Nanami は理論式の約1.5倍効き、Keita は約0.55倍しか効かないうえ **+6半音付近で飽和し、下方向はほとんど効かない**。`constants.py` に実測の較正表をコメントで置いてあるので、それを目安に Hz を直書きする。
- **`voice_dsp.py` に WSOLA・リサンプリングを戻さないこと**: 設計初期に gTTS の単一音声から高さと速さを作るため自前で持っていたが、edge-tts はピッチを変えても長さが変わらない（＝リサンプリングではなくフォルマントを保つ）ため不要になった。戻すと音質を落とすだけで得るものがない。
- **バイクアッドはブロック処理**: IIR は1つ前の**出力**を使う再帰式のため numpy でベクトル化できず Python のループになる。`_BIQUAD_BLOCK_SIZE` ごとに区切ってフィルタの状態を持ち越す（一気に `tolist()` すると 30 秒の音声で数十 MB の一時オブジェクトができる）。
- **音色を変えないペルソナは numpy を通さない**: `needs_equalizer` が偽なら miniaudio に 48kHz ステレオまで変換させる。この分岐を消さないこと（`flat` のペルソナの経路が無駄に重くなる）。EQ を掛ける場合はモノラル1ch分だけ処理し、`np.repeat(clipped, TTS_CHANNELS)` でのステレオ化は最後に回す（計算量が半分で済む）。
- **int16 のクリップは必須**: EQ のブーストでレンジを超えるため、`np.clip` してから `astype(np.int16)` する（超えたまま変換すると音が割れる）。
- **プロファイルは起動時に点検する**: `voice.py` の `_validate_profiles()` が import 時に `pitch` / `rate` の形式を確認して警告を出す。形式ミスは `Communicate` の `ValueError` 経由で**黙って gTTS 退避に落ち続ける**（音は出るのでキャラ差が消えたことに気づきにくい）ため、この点検を外さないこと。
- **フェイルセーフ**: EQ 名が未知・`eq` キー自体が無い・値が数値でない場合も音を壊さず加工なしへフォールバックする。

### スラッシュコマンド (`commands.py`)

すべてのコマンドは `register_commands` で登録され、`on_ready` で同期される。全一覧: `/functions`（機能ハブ。エフェメラル）、`/join` + `/leave`（ボイスチャンネル）。`/functions` はハブパネルを開き、ボタンで各機能へ分岐する: **⚙️ キャラ設定**（`SettingsView` に変身）、**📋 会話要約**（`SummaryView` に変身）、**📈 会話統計**（`StatsView` に変身。ユーザー別ダッシュボード。管理者はここから 👍 フィードバック集計も見られる）、**💬 自発会話**（`AutoTopicView` に変身。自発的な話題提供の ON/OFF・しきい値設定）、**⏰ リマインダー**（`ReminderView` に変身。本人の予定の一覧・取り消し）、**📊 グラフ**（利用トークングラフをその場で表示）、**🔄 NGワード再読込**、**🔍 Google検索**（グラウンディングのその場トグル。確定なしで即そのサーバーの `GuildSettings` に反映）、**❓ 使い方**（`HelpView` に変身。会話のしかたと全機能の説明。権限制限なし）、**🔒 統計プライバシー**（`StatsPrivacyView` に変身。本人の会話統計の収集停止・蓄積データ削除）、**🚫 ブロック解除**（**管理者専用**。ブロック中ユーザーの選択メニューから解除。管理者以外にはボタン自体を非表示）。旧コマンド（`/status`、`/summarize`、`/graph`、`/ng_reload`、`/unblock`、および `/ai`、`/set`、`/gemini`、`/setting`、`/reset`）は削除済み — 設定・要約・利用グラフ・NG再読込・ブロック解除は `/functions` ハブに、テキストチャットは mention モードの `on_message` パイプラインに統合された。

### キャラ設定 UI (`settings_view.py`, `commands.py`, `choices.py`)

`/functions` のハブ（`functions_view.py` の `FunctionsView`、エフェメラル）から **⚙️ キャラ設定** を選ぶと、同じメッセージが `SettingsView` に変身する（ペルソナ/モデル/応答モードのセレクト + 確定/戻る/リセットボタン、120秒タイムアウト）。一時的な選択値は View 上に保持され、**確定 (Confirm)** ボタンを押したときにのみ `GuildSettings`（`/functions` 実行時に `interaction.guild_id` から解決したもの）に書き込まれる — それまでは共有状態を一切変更しない。Google検索（グラウンディング）のトグルはハブ側にあり、こちらは押した瞬間に `GuildSettings` へ反映される（ハブは「その場で実行」系のため）。**各 View は `BotState` ではなく `GuildSettings` を受け取る** ため、設定変更の影響はそのサーバー内（DM の場合は全 DM）に閉じる。`choices.py` はペルソナ/モデル/モード、および要約期間（`SUMMARY_PERIOD_CHOICES`）の選択肢リストと表示名マップの唯一の情報源。

### 機能ハブ / 会話要約 (`functions_view.py`, `summary_view.py`, `stats_view.py`)

`/functions` はエフェメラルな `FunctionsView`（ハブ）を開く。Discord の View は最大5行のため設定と要約を1枚に合体できず、ボタンで選んだ機能のパネルへ**同じメッセージを差し替える**方式で統合している。⚙️キャラ設定→`SettingsView`、📋会話要約→`SummaryView`、📈会話統計→`StatsView`、💬自発会話→`AutoTopicView`、⏰リマインダー→`ReminderView`、❓使い方→`HelpView`、🔒統計プライバシー→`StatsPrivacyView`、🚫ブロック解除→`UnblockView`（管理者のみボタン表示）はメッセージを差し替え、📊グラフ・🔄NG再読込は別のエフェメラルメッセージで結果を返す。**ハブの5行（row 0〜4）は使い切っている**ので、以降の機能追加はハブではなくサブパネル側へ置くこと（row 3 = ❓使い方 / row 4 = 🔒統計プライバシー）。差し替えは `view_parts.swap_panel()` を使う（ハブの `stop()` 忘れを防ぐため。新規パネルは `ui.View` 直継承ではなく `PanelView` を継承する）。🔍Google検索はハブ上のトグルで、押した瞬間に `BotState.grounding_enabled` を反転する。各サブパネルは共通の **↩️ 戻る** ボタン（`view_parts.py` の `BackButton`）を持ち、`make_hub`（`FunctionsView.clone` を渡す）でハブを再生成して同じメッセージを戻す — `view_parts.py` が `functions_view.py` を import すると循環参照になるため、生成関数を渡す形にしている。**会話要約**は期間（1時間〜1か月）と表示範囲（自分だけ/全員）を選んで実行する。`generate_channel_summary` が対象期間のメッセージを `fetch_messages_since` で取得（`SUMMARY_MAX_MESSAGES` 件超過で自動停止）、`build_summary_source_text` で**発言者名つき**に整形し、`build_summary_prompt`（`prompting.py`）＋ **そのサーバーの `GuildSettings`** の選択モデル/ペルソナで議事録を生成する。生成結果は感情タグ除去・Markdown見出し除去・`screen_bot_response` を経て、表示範囲に応じてチャンネル投稿またはエフェメラル表示される。

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

- **登録経路**: `content_moderator.check_and_route` がモデレーションと同じ1回の呼び出しで日時＋本文＋**繰り返し種別**（`repeat`）を抽出する（**リマインダーのために Gemini を追加で呼ばないこと** — 判定を足すならこのプロンプトに相乗りさせる）。相対表現を解釈させるため、プロンプトには現在日時（曜日つき、`REMINDER_TIMEZONE` 基準）を埋め込む。`_build_combined_system_prompt` の JSON 出力例は3つ（安全時・リマインダー時・不適切時）あるので、形式を変えるときは**全部そろえて**直すこと（1つだけ直すとモデルが形式を揺らす）。
- **検証は `ReminderService` 側**: 日時の解釈（複数フォーマットを順に試す）、過去・`REMINDER_MAX_DAYS_AHEAD` より先の拒否、`REMINDER_MAX_PENDING_PER_USER` の上限、本文の切り詰め。繰り返しでも判定するのは**初回の日時**。**登録を見送るときは `None` を返す**（呼び出し側は通常の会話へ流す）。上限超過だけは意図が明確なので案内文を返す。
- **通知は生成しない**: 正確さが最優先なので Gemini を呼ばず、`⏰ @ユーザー リマインダーです: 本文` のテンプレートで組み立てる（繰り返しは `🔁 次回は …` を添える）。NG ワードのマスクだけ適用する（登録時にモデレーション済みのため再判定しない）。繰り返しは同じ本文が定期的に公開チャンネルへ出るため、マスクは**毎回**通すこと。
- **二重通知の防止**: `Database.reminder_claim_due()` が SELECT と UPDATE を同一トランザクションで行い、期限到来分をクレームしてから返す（`queue_tasks` と同じクレーム方式）。送信自体が失敗しても `pending` に戻さない — 重複通知より「時間外れの再通知」の方が困るため。
- **繰り返し（`none`/`daily`/`weekly`/`monthly`）**: 新しい行を作らず、**同じ行の `remind_at` を次回時刻へ進めて `pending` のまま残す**（`reminder_id` が変わらないのでパネルからの取り消しが直感どおりに効き、`done` 行で保持枠を食い潰さない）。単発は従来どおり `done`。次回時刻の計算は `reminder_service.next_occurrence()`（暦だけを扱う純粋関数）が担い、`reminder_claim_due` へ**同期関数として渡してクレームと同一トランザクションで実行する** — 2ステップに分けると間でクラッシュしたときに繰り返しが失われる。
  - **渡す計算関数から `Database` のメソッドを呼ばないこと**（`self._lock` は再入不可の `threading.Lock`。同じスレッドから取り直すとデッドロックする）。`async def` にするのも不可（`await` されずコルーチンが返る）。
  - **停止明けは「未来になるまで反復」して追い付く**。1回分だけ進めると追い付くまで見回り（60秒）のたびに通知が飛ぶ。歯止めは `REMINDER_RECURRENCE_MAX_CATCHUP_STEPS`（到達したら `canceled` にして止める）。過ぎた回はまとめて通知しない（通知は最大1回、時刻は未来へ）。
  - `_is_stale` が見るのは**進める前の（本来の）予定時刻**。そのためクレームが返すレコードの `remind_at` は進める前の値のままにし、進めた後の時刻は `next_remind_at` に入れて返す。stale で今回を見送っても次回時刻は計算済みなので**系列は途切れない**（長期停止明けは翌回の定刻から再開する）。
  - **月次は `recurrence_anchor`（初回時刻）を固定**したまま `calendar.monthrange` で末日へ丸める（`timedelta(days=30)` は使わない）。アンカーを毎回更新すると 1/31→2/28→**3/28** とずれる。正しくは 1/31→2/28→3/31。
  - 日時の加算は `REMINDER_TIMEZONE` のローカル時刻で行ってから UTC へ戻す（DST のあるタイムゾーンへ変えても壊れないため）。許容値・表示名・追い付き上限は `constants.py` の `REMINDER_RECURRENCE_*` に集約する。
- **時刻の扱い**: 保存は UTC の ISO 8601 で**秒精度に統一**する（期限判定を文字列比較で行うため、桁数が混ざると比較が壊れる）。表示だけ `REMINDER_TIMEZONE` に変換する（`format_local`）。日時の書式に `strftime` の `%-m` / `%#m` は使わないこと（プラットフォーム依存）。
- **権限**: `/functions` → **⏰ リマインダー** で一覧・取り消し。表示は本人の分だけで、`reminder_cancel` は SQL の条件にも `user_id` を含めるため他人のものは操作できない。

### ユーザー別会話統計 (`stats_service.py`, `stats_view.py`, `stats_privacy_view.py`)

`/functions` の **📈 会話統計** から開く、ユーザー別の会話ダッシュボード。**すべてエフェメラル（非公開）で表示される。** 集計対象は「ボットへの発言のみ」で、`on_message` パイプラインの `handle_chat_message` 直前に `stats.record(...)` を fire-and-forget で呼んで蓄積する（失敗してもチャット本体は止めないフェイルオープン）。データは DB の `user_stats` テーブルに永続化され、`asyncio.Lock` で保存を直列化する（単一プロセス前提）。

- **集計データ**: ユーザーごとに累計発言回数・時間帯別ヒストグラム（`hourly[24]`、`STATS_TIMEZONE`＝`Asia/Tokyo` の「時」に変換）・直近発言サンプル（`STATS_RECENT_SAMPLES_MAX` 件のリングバッファ）を記録する。
- **話題・性格の LLM 解析** (`ensure_analysis`): 直近サンプルを Gemini に渡し、「よく話す話題」と「ビッグファイブ性格特性」を JSON で推定する。結果は `STATS_ANALYSIS_TTL_HOURS` の間キャッシュし、TTL 内なら再計算しない。LLM 呼び出し・パース失敗時は既存キャッシュにフォールバックする。性格推定は `STATS_PERSONALITY_MIN_SAMPLES` 件未満のとき「データ不足」として描画をスキップする。
- **項目別グラフ** (matplotlib、`usage_graph.py` と同じ日本語フォント作法): 時間帯＝棒グラフ / 話題＝重み付き横棒 / 性格＝ビッグファイブのレーダーチャート。項目セレクトで「全部 / 時間帯 / 話題 / 性格推定」を切り替えられる。
- **権限**: 一般ユーザーは**自分の統計のみ**閲覧できる（対象は `viewer_id` に固定）。**管理者のみ**、ユーザー選択メニューで他メンバーの統計を見られ、発言回数ランキング（全体比較）ボタンも使える。権限判定は `interaction.user.guild_permissions.administrator` によるサーバー側判定で、`RankingButton` 側でも二重にチェックする。
- **再起動でリセットされない**: `user_stats.json`（`memory/` 配下）に永続化される。調整値（TTL・上限・タイムアウト・タイムゾーン）は `constants.py` の `STATS_*` に集約。
- **収集の停止（opt-out）** (`stats_privacy_view.py`): 本人が `/functions` → **🔒 統計プライバシー** から統計収集そのものを止められる。設定はユーザー単位で**全サーバー横断**（統計データ自体が `user_id` 単位でサーバーをまたぐため）、`user_stats.stats_opt_out` 列（スキーマ バージョン8）に永続化される。既定値は `STATS_OPT_OUT_DEFAULT`（`False` ＝従来どおり収集する。`True` にすると「原則収集しない」オプトイン運用になる）。
  - **判定は `StatsService` の内部で行う**（`record()` の冒頭＝エントリ生成より前、と `ensure_analysis()` の入口）。呼び出し側（`bot.py`）ではチェックしない — `record` の呼び出し箇所が増えたときのチェック漏れを防ぎ、fire-and-forget の主経路に `await` を足さないため。判定は `is_opted_out()` の**キャッシュ参照のみ**で DB へは行かない。
  - **【最重要】`Database.stats_save_all` は DELETE → 全行 INSERT の総入れ替え**。INSERT の列に `stats_opt_out` を書き漏らすと、**誰かが発言するたびに全員の opt-out が既定値へ黙って戻る**。`user_stats` に列を足すときは `stats_load_all` と対で直し、必ず「保存→読み込み→保存」の往復を実測すること。
  - **停止と削除は別操作**: 停止（opt-out）は以降の収集を止めるだけで既存データは残る。🗑️ 蓄積データを削除（2回押しの確認つき）が集計値・発言サンプル・解析キャッシュを消す。**削除で行ごと消さないこと**（`del cache[user_id]` は禁止 — opt-out の設定まで消える）。
  - **他人からは見えない**: opt-out 中のユーザーは管理者のユーザー選択（`get_user_options`）と発言回数ランキング（`build_ranking_graph`）から除外される。他人が opt-out 中のユーザーを指定した場合は、既存の「◯◯ さんの会話データはまだありません。」と**同一の文言**を返す（専用の文言を出すと停止していること自体が露見する）。明示的な注記 `🔕 現在、統計収集を停止しています。` は**本人が自分のダッシュボードを見たときだけ**。`build_dashboard(user_id, item, viewer_id=None)` の `viewer_id` は「誰が見ているか」で、**省略時は本人扱い**（現在の呼び出し元 `stats_view.RunStatsButton` が渡さないため）。`stats_view.py` を次に触るときに `viewer_id=view.viewer_id` を渡すと、その経路も厳密になる。
  - **対象は統計だけ**: 長期記憶（`profile` / `projects` / `pending_log`）は opt-out の対象外。パネルの説明文でその旨を明示している（誤解させると透明性の目的を損なうため、文言を削らないこと）。
  - **フェイルオープンの例外**: 設定の保存・データ削除に失敗したときは、UI に「保存できませんでした」「削除できませんでした」と伝える（`save_background()` が成否を `bool` で返す）。プライバシー操作の失敗は黙って成功に見せない。

### リアクションによるフィードバック収集 (`feedback_service.py`, `stats_view.py`)

ボットのチャット返信に 👍👎 を自動で付け、押されたリアクションを「どのモデル・どのペルソナの返信が良かったか」の記録として `feedback` テーブルへ残す。目的は応答品質のチューニングとモデル選定の判断材料で、**ユーザーへの返信は一切しない**（会話の邪魔をしない）。機能全体は `FEEDBACK_ENABLED` で止められる。

- **対象は `handle_chat_message` のチャット返信だけ**。文書処理の返信・自発的な会話・記念日・リマインダー通知には付けない（評価したいのは「会話としての応答品質」であり、定型通知を混ぜると集計が濁るため）。分割送信された長文は **1通目（`message.reply` したもの）にだけ**付ける。そのために `send_long_reply` は1通目の `discord.Message` を返す（**戻り値を「最後のチャンク」に変えないこと** — 2通目以降は元の質問への紐付けが無い `channel.send`）。
- **評価対象はインメモリのレジストリで覚える**: `FeedbackService.track_reply()` が `OrderedDict`（`FEEDBACK_TRACKED_MESSAGES_MAX` 件のリングバッファ）へ「メッセージID → サーバー/チャンネル/宛先/モデル/ペルソナ」を記録する。**このメソッドは同期**で、登録だけを即座に済ませ、通信を伴うリアクション付与は `background.spawn` で裏へ逃がす（登録まで後回しにすると、付与が終わる前に押されたリアクションを取りこぼす）。永続化しないのは、返信のたびに DB へ書くとチャットの主経路が重くなるため（再起動で消えるが、評価は返信直後に押すものなので実用上困らない）。
- **👍👎 の付与はクールダウンで間引くが、レジストリへの登録は全返信に対して行う**: 付与は返信ごとに Discord への API 呼び出しを2回増やし、この経路はチャンネル単位のレート制限が厳しいため、詰まるとチャット全体の応答が遅くなる（実測で確認済み）。`_claim_reaction_slot()` が「同じチャンネルへ直近 `FEEDBACK_REACTION_COOLDOWN_SECONDS` 以内に付与していたら見送る」判定を行う。**一律に「N 回に1回」で間引かないこと** — レート制限が痛むのは連投しているときだけなので、固定比率だと痛くない場面のデータまで等しく捨てる。**登録の側を間引かないこと**も同様に重要で、登録さえ残っていれば付与されなかった返信にユーザーが自分で 👍👎 を押した分も記録できる（間引きがそのままデータの損失にならない）。集計は「モデル×ペルソナ」で最大36通りに分割される（ペルソナ12 × モデル3）ため、票の総数を削る変更は内訳集計を成立させなくする点に注意。判定と時刻の記録を `_claim_reaction_slot()` の1メソッドにまとめてあるのは**間に `await` を挟ませない**ため（挟むと同時に2件通ってクールダウンが効かない）。
- **`feedback_prune` を評価が押されるたびに呼ばないこと**: `feedback_prune` は索引の無い `created_at` で全行を並べ替えるうえ、`database.py` の**プロセス唯一のロック**を握る。その間はチャットの主経路が使う記憶・親密度・統計の読み書きまで待たされ、返信そのものが遅くなる。`FeedbackService.prune_stored()` を `bot.py` の `feedback_prune_loop`（`FEEDBACK_PRUNE_MINUTES` ごと。`tasks.loop` は `start()` 直後に1回目が走るので起動時の間引きも兼ねる）から呼ぶ形にしてある。保持件数は「多少超過してもよい」性質の上限なので、主経路の速さを優先する。
- **数えないリアクション**: ボット自身が付けた初期リアクション（`payload.user_id == client.user.id` で最初に弾く。これを忘れると起動直後から自己投票でデータが汚れる）、他のボット（`payload.member.bot`。**DM では `member` が `None`** なので無条件に参照しないこと）、👍👎 以外の絵文字（`str(payload.emoji)` で比較する。`PartialEmoji` なのでカスタム絵文字と取り違えないため）、レジストリに無いメッセージ（**ログも出さずに黙って無視** — 他人のメッセージへのリアクションでも毎回イベントが飛ぶため）。
- **第三者の評価は数える**: 応答品質はチャンネルの参加者全員が判断できるため。ただし「評価者（`user_id`）」と「返信の宛先（`target_user_id`）」を別々に保存し、後から当事者だけで絞り込める余地を残す。`target_user_id` は集計表示には使わない（表示に使うなら別途プライバシー検討が必要）。
- **押し替えと取り消し**: 主キーが `(message_id, user_id)` なので 👍→👎 は `INSERT OR REPLACE` で上書きされ、行は増えない。`on_raw_reaction_remove` では**絵文字を確認してから**削除する（対象外の絵文字を外しただけで評価が消えないようにするため）。イベント（`on_raw_reaction_add` / `on_raw_reaction_remove`）は `on_reaction_add` ではなく **raw 版**を使う — 前者はメッセージがキャッシュに載っている間しか発火せず、少し前の返信への評価を取りこぼす。必要な `reactions` インテントは `Intents.default()` に含まれるため **`bot.py` の intents は触らない**。
- **フェイルオープン**: リアクション付与の失敗（`discord.Forbidden` → `discord.HTTPException` の順で捕まえる）も記録の失敗もログだけでチャットは成功扱い。イベントハンドラの本体は全体を `try/except Exception` で包む（漏らすと discord.py の既定 `on_error` がログを吐くだけで原因が埋もれる）。
- **閲覧は管理者専用・エフェメラル**: `/functions` → 📈 会話統計 → **👍 フィードバック**（`stats_view.py` の `FeedbackButton`、row 2）。権限は `discord_helpers.is_administrator` で判定し、**コールバック内でも再判定する**（`RankingButton` と同じ二重チェック）。集計は `interaction.guild_id` のサーバー分だけで、**DM 分（`guild_id=0`）は誰にも見せない**。表示は全体の 👍/👎/満足度とモデル別・ペルソナ別の内訳（最大 `FEEDBACK_SUMMARY_MAX_ROWS` 行）で、**「誰が👎したか」は出さない**（個人が特定されると評価しづらくなるため）。
- **`FeedbackService` の受け渡し**: `StatsView` は機能ハブ（`functions_view.py`）が生成するため、依存を引数で通すとハブのコンストラクタ・`clone()`・`register_commands` まで芋づるで変わる。付随機能でその連鎖を起こさないよう、`bot.py` が起動時に `feedback_service.set_default_service()` で登録した既定インスタンスを `StatsView` が引く（コンストラクタの `feedback` 引数で明示的に渡すこともでき、渡された場合はそちらが優先される）。
- 記録は `FEEDBACK_MAX_STORED_ROWS` 件で古いものから捨てる（`feedback_prune`。呼ぶのは定期ループだけ）。調整値は `constants.py` の `FEEDBACK_*` に集約する。
- **案内の文言も合わせて直すこと**: 付与を間引く以上、「返信に必ず 👍👎 が付く」という案内は嘘になる。`help_view.py` の `_FEEDBACK_LINE`（❓使い方とウェルカム投稿の共通の情報源）に「一部の返信にしか付かないが、自分で押しても記録される」旨を書いてある。付与の方針を変えたらここも必ず対で直す。

### オンボーディング導線 (`help_view.py`, `discord_helpers.py`, `bot.py`)

「メンションするだけで会話できる」ことも「機能が `/functions` に集約されている」ことも、招待しただけでは誰も気づけない。そこで最初の一歩を案内する入口を2つ用意している — `/functions` → **❓ 使い方**（`HelpView`）と、サーバー参加時のウェルカム投稿（`bot.py` の `on_guild_join`）。

- **ヘルプとウェルカムの文言を2か所に書かないこと。** 両者は説明する内容が大きく重なるため、共通部分（メンションでの会話・添付・リマインダー・読み上げ・評価・記憶・統計）は `help_view.py` のモジュール内定数を唯一の情報源にし、`build_help_text()` / `build_welcome_text()` の両方がそれを組み立てて使う。片方だけ直すと「ウェルカムには書いてあるのにヘルプには無い」という食い違いが生まれ、案内としての信頼を失う。**機能を足したり既定値を変えたりしたら、ここの文言も必ず合わせて直すこと**（本文が実装と食い違わないことが最優先）。長い日本語テキストのため `constants.py` には置かない（`constants.py` は調整値の置き場という既存規約を守る）。
- **ボット名はハードコードせず `resolve_bot_name(client.user)` で解決する**（別名のボットで同じコードを動かすとメンション例だけが嘘になるため）。名前から `@` を落とすのは、`@everyone` という名前のボットが作られた場合への保険。
- **本文は Discord の1メッセージ上限に収める**: `_fit_to_discord_limit` が超過分を切り詰めて警告ログを出す。エフェメラルの差し替え（`edit_message`）は1通しか置き換えられないため `split_message` は使えない（View を持てるメッセージは1通だけ）。切り詰めは安全弁であり、発動したら文言を減らすこと。
- **使い方パネル**: `HelpView` は `PanelView` を継承し、部品は ↩️ 戻る（row 0）だけ。表示のみで副作用が無いため権限制限は設けない（DM でも使える）。差し替えは `view_parts.swap_panel()`。
- **ウェルカム投稿の配線は薄く保つ**: `on_guild_join` は「本文を組み立てて `discord_helpers.post_welcome_message` へ渡す」だけで、判断ロジックは各モジュール側にある。イベントハンドラの本体は `try/except Exception` で包む（案内の失敗でイベント処理を落とさない）。必要な `guilds` インテントは `Intents.default()` に含まれるため **`bot.py` の intents は触らない**。
- **投稿先の決め方**（`pick_announcement_channel`）: ① `guild.system_channel` → ② `guild.text_channels` を順に走査して最初に送信できるチャンネル → ③ 見つからなければ投稿しない（`print` のログのみ）。権限は必ず `channel.permissions_for(guild.me)` で**チャンネル単位に**確認する（サーバー全体のロールで許可されていてもチャンネル個別で拒否されうる。`/join` の接続権限チェックと同じ作法）。「送ってみて失敗したら次」の例外頼みにしないこと（無駄に API を叩き、権限エラーとネットワーク障害を区別できない）。`guild.channels` は使わない（カテゴリやボイスが混ざる）。`guild.me` が `None`（キャッシュ未取得）なら権限を判定できないので諦める。
- **連投防止**: `BotState.welcomed_guild_ids`（**インメモリのみ**。`auto_topic_channels` と同じ「実行時に自然に再構築される一時状態」）。`on_guild_join` は実際に招待されたときにしか発火せず再起動・再接続では発火しないため、プロセスをまたぐ重複投稿は原理的に起きない（この集合は同一プロセス内でイベントが重複した場合の保険）。**投稿済みフラグは送信を待つ前に立てること** — 送信後に立てると、その待ち時間に入ってきた2件目が二重投稿になる。DB へは持たせない（`guild_settings` に列を足すと `guild_settings_load_all` / `guild_settings_save` / `apply_persisted` / `persist` の4か所すべてを揃えて直す必要があり、1か所でも漏れると他の設定が既定値へ戻る）。
- **ウェルカム本文に `@everyone` / `@here` / ロールメンションを絶対に書かないこと**（公開チャンネルへの投稿なので通知爆撃になる。招待→キック→再招待を繰り返されると毎回投稿される）。送信側でも `discord.AllowedMentions.none()` を付けて二重に防いでいる。個人情報・記憶・統計も載せない（参加時点でそもそもデータが無い）。
- **フェイルオープン**: `post_welcome_message` は例外を外へ出さず、投稿できたかを `bool` で返す（`discord.Forbidden` → `discord.HTTPException` の順で捕まえる）。案内が出せなくてもボット自体は普通に使える。`WELCOME_MESSAGE_ENABLED`（`constants.py`）で投稿だけを止められ、止めても ❓ 使い方 からいつでも読める。既に招待済みのサーバーへは投稿されない（仕様として許容）。

## 規約と落とし穴

- **パス解決**（`constants.py`, `paths.py`）: `BASE_DIR = index/`、`PROJECT_ROOT` はその親。ペルソナファイルは `"../prompts/prompt_tsun.txt"` のような値（`index/` 基準）として保存・選択され、`resolve_prompt_path` で解決される。`memory/`・`ng/`・`prompts/` はプロジェクトルート直下にある。
- **設定（APIキー）**: `settings_loader.load_settings()` はプロジェクトルート直下の `.env`（パスは `constants.ENV_FILE_PATH`。**絶対パスで解決すること** — ボットは `index/` から起動するため、カレントディレクトリ基準の相対探索では見つけられない）を `python-dotenv` で読み込み、`.API_KEY_GEMINI` / `.TOKEN_DISCORD` を持つ `Settings` データクラスを返す。旧 `config.py` / `config_private.py` は ver0.1.6 で削除済み。
  - **`load_dotenv(override=False)` を変えないこと**: OS の環境変数が `.env` より優先される。systemd の `EnvironmentFile=` や Docker の `--env-file` へ移行できるようにするための挙動で、逆にするとサーバーに残った古い `.env` が黙って優先され「直したはずのキーが反映されない」事故になる。
  - **未設定なら起動を止める**: ファイル無し・変数無し・空文字・`.env.example` のプレースホルダのまま、のいずれでも `キー未設定` を出力して `SettingsError` を送出する。`load_settings()` は `bot.py` のトップレベルで呼ばれるためボットは起動しない。キーが無いまま起動すると「Discord へは接続できるが Gemini の呼び出しだけ全滅する」という切り分けの難しい状態になるため、あえて失敗させている。
  - **`python-dotenv` が未インストールでも例外にしないこと**: 警告を出して `os.environ` だけで続行する（環境変数でキーを渡す運用ならこのパッケージは不要なため）。
  - `.env.example` のプレースホルダ文字列を変えたら `settings_loader._PLACEHOLDER_VALUES` と `deploy/migrate_config_to_env.py` の `PLACEHOLDER_VALUES` も**必ず対で直す**こと（片方だけ直すと書き換え忘れの検出が効かなくなる）。
  - **移行用**: `deploy/migrate_config_to_env.py` が旧 `config_private.py` / `config.py` から `.env` を生成する（ver0.1.5 以前から更新した環境向け。ボット本体からは呼ばれない一度きりのスクリプト）。旧ファイルは `ast` で構文解析するだけで **import しない** — import は中身を実行するため、値を読むだけの用途で実行する必要が無い。
- **調整値は `constants.py` に置く** — モデル名、タイムアウト、しきい値、上限値。新規もインラインではなくここに追加すること。
- **fire-and-forget は `background.spawn(coro, label=...)` を使う（`asyncio.create_task` を直接呼ばない）**: asyncio のイベントループは実行中タスクを**弱参照**でしか保持しないため、`create_task` の戻り値を捨てるとタスクが実行途中でガベージコレクトされて消えることがある（Python 公式ドキュメントが明記している既知の落とし穴）。`spawn` は完了までタスクへの強参照を保持し、完了時に自動で取り除く。加えてタスク内の例外を `label` 付きでログに出す — `create_task` に投げっぱなしだと例外は回収時まで報告されず、そのまま失われることもある。特に `documents.run_worker()` のような常駐コルーチンでは、消えると添付ファイルの処理が二度と走らなくなる。
- **タイムゾーン名を表示・プロンプトに載せるときは `timeutil.timezone_label(self._tz)` を使う（`REMINDER_TIMEZONE` などの定数をそのまま書かない）**: `resolve_timezone` は tzdata の無い環境で UTC へフォールバックするため、定数を表示すると「実際は UTC の時刻なのに Asia/Tokyo と名乗る」食い違いが起きる。リマインダーの統合判定プロンプトでは、この食い違いがそのまま9時間ずれた日時での登録に直結する。
- **モデレーションはフェイルオープン**: タイムアウト/例外時、`content_moderator.check` は *safe* を返してボットを動かし続ける。Gemini のプロンプトブロックは *unsafe* として扱う。この挙動を維持すること — 一時的な API エラーでユーザーをブロックしないこと。**`check` / `check_and_route` の本体で例外を外へ漏らさないこと**: これらは `on_message` の主経路から呼ばれるため、抜けた例外はそのメッセージの処理ごと落とす。準備処理（タイムゾーンの解決など）を `try` の外に置くと、環境依存の失敗が全メッセージの無応答に直結する（実際に `ZoneInfo` の組み立てで起きた）。
- **タイムゾーンは `timeutil.resolve_timezone(name, purpose=...)` で解決する**: `ZoneInfo` を直接呼ばないこと。タイムゾーンデータベースの無い環境（`tzdata` を含まないスリムな Linux イメージなど）では例外になるため、この関数が UTC へのフォールバックと警告ログをまとめて引き受ける。タイムゾーン名そのもの（`STATS_TIMEZONE` / `ANNIVERSARY_TIMEZONE` / `REMINDER_TIMEZONE`）は用途ごとに `constants.py` で個別に調整できる形を保つ。
- **再起動でリセットされる状態**（インメモリのみ）: 利用/トークン履歴（`usage_graph`、500件のリングバッファ）、`channel_reset_points`（`/functions` → ⚙️キャラ設定パネルの会話リセットのアンカー）、自発的な会話の**監視状態**（`auto_topic_channels` / `auto_topic_posted_at`）、フィードバックの**評価対象レジストリ**と**付与クールダウンの記録**（`FeedbackService._tracked` / `_last_reaction_at`。記録済みの評価そのものは DB に残る）、ウェルカム投稿済みのサーバーID（`welcomed_guild_ids`。`on_guild_join` は招待時にしか発火しないため、消えても再投稿は起きない）。一方、`GuildSettings` の**設定値**（ペルソナ/モデル/応答モード/グラウンディング/自発会話の ON・しきい値）・記念日の投稿記録・リマインダー・フィードバックの記録は DB に永続化され、再起動しても失われない。永続化される状態は `memory/bot.db`（SQLite: affinity / user_stats / queue_tasks / pending_log / profile / guild_settings / anniversary_posts / reminders / feedback）、`memory/projects.json`、`ng/blacklist.json`。`memory/profile.json` は手書き追加用の受け皿で、起動時に DB へ吸収され `{}` に戻る。
- **単一プロセス前提**: DB は `database.py` 内部の `threading.Lock` + トランザクションで、JSON ファイル（profile/projects/blacklist）はプロセス内 `asyncio.Lock` で保護される。プロセス間ロックは存在しない。DB は WAL モードのため、稼働中でも `sqlite3` CLI 等からの**読み取り**は安全（外部からの書き込みはしないこと）。
- **ログ出力とエンコーディング**: Windows の既定コンソールは cp932 で、絵文字を `print` すると `UnicodeEncodeError` でそこの処理が止まる。`bot.py` の冒頭で標準出力を UTF-8（`errors="replace"`）へ再設定しているので通常は問題ないが、**`bot.py` を経由しない単体スクリプトから同じモジュールを動かすときは `PYTHONIOENCODING=utf-8` を付けること**。
- **Discord の制限**: 2000文字を超える返信は `send_long_reply`/`split_message` を通す。`send_long_reply` は**1通目（元メッセージへの `reply`）**を返し、フィードバックのリアクション付与がそれを使う（戻り値を最後のチャンクに変えない。戻り値を無視する呼び出しはそのままでよい）。グラウンディング返信はリンクの埋め込みプレビューを抑制する。
- **`ng/NG_WORD_private.txt`** は gitignore されており、読み込み時に `NG_WORD.txt` の上にマージされる。`/functions` の 🔄 NGワード再読込 で再起動なしに再読み込みできる。

## 作業完了時の手順（PR 作成とプライバシーチェック）

このリポジトリは **Public** で、PR・コミット・Issue に書いた内容はそのまま公開される（後から消しても、キャッシュやフォークに残りうる）。ファイルを変更するタスクを終えるときは、**「作業完了を宣言します」と報告する前に**、毎回次の手順を行うこと。ファイルを変更しないタスク（質問への回答・調査だけ）では PR は作らない。

この手順での push と PR 作成は利用者があらかじめ指示したものなので、都度の承認は要らない（`main` へ直接 push しないこと）。

### 手順（この順番を守る）

1. **プライバシーチェック**を行う。公開した後では手遅れなので、push と PR 作成より**前**に済ませること。
   - 対象: このブランチの差分すべて（`git diff origin/main...HEAD`）、コミットメッセージ（`git log origin/main..HEAD`）、これから書く PR のタイトルと本文、PR に添付する画像。
   - 見つけた個人情報は**削除するか、プレースホルダーに置き換える**（下の表）。
2. 修正をコミットする。まだ push していないコミットに個人情報が入っていた場合は、後から消すコミットを足すのではなく、push する前にそのコミット自体を書き直して（`git commit --amend` など）履歴に残さない。履歴の書き換えに承認が要る運用なら、書き直す前に利用者へ確認する。
3. トピックブランチを push し、`gh pr create` で PR を作る。
4. PR 本文に `## プライバシーチェック` 節を設け、確認した範囲と結果を書く（置き換えた項目の**種類と件数**だけ。**置き換える前の値そのものは書かない**）。
5. 「作業完了を宣言します」と報告し、PR の URL を添える。

### 確認する項目と置き換え先

| 種類 | 紛れ込みやすい場所 | 置き換え先の例 |
|---|---|---|
| 氏名・住所・電話番号・メールアドレス・生年月日 | 動作確認用の文、コメント、PR 本文の例文 | `<NAME>` / `<ADDRESS>` / `<PHONE>` / `<EMAIL>` |
| 学籍番号・所属・車のナンバーなど個人を特定できる情報 | 同上 | `<STUDENT_ID>` などの種類名 |
| Discord のユーザー・サーバー・チャンネル・メッセージの ID（17〜20 桁の数字） | ログの貼り付け、テストデータ、`LEGACY_MEMORY_OWNER_ID` | `<DISCORD_USER_ID>` / `<GUILD_ID>` / `<CHANNEL_ID>` |
| API キー・トークン | `.env` の値、ログ、デバッグ出力 | `.env.example` と同じ `YOUR_GEMINI_API_KEY` / `YOUR_DISCORD_BOT_TOKEN` |
| ローカルの絶対パス（OS のユーザー名を含む） | `C:\Users\<名前>\...` / `/home/<名前>/...` | `<PROJECT_ROOT>` やリポジトリ基準の相対パス |
| サーバーの実パス・ユーザー名・IP アドレス・ホスト名 | `deploy/bot.service` の値 | `deploy/bot.service.example` と同じプレースホルダ |
| 利用者の会話・記憶・違反記録 | `memory/`・`chatlog/`・`ng/blacklist.json`・`ng/NG_WORD_private.txt` 由来の文 | 削除（例が必要なら架空の文に差し替える） |

機械的な確認の例（ヒットした行は目で確かめ、誤検出なら残してよい）:

```bash
P='[0-9]{17,20}|0[0-9]{1,4}-[0-9]{1,4}-[0-9]{4}|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|[A-Za-z]:[\\/]Users[\\/]|/home/|AIza[0-9A-Za-z_-]{20,}'
git diff origin/main...HEAD | grep -nE "$P"            # 差分
git log origin/main..HEAD --format=%B | grep -nE "$P"  # コミットメッセージ
git diff --name-only origin/main...HEAD                 # .env / deploy/bot.service / memory/ などが混ざっていないか
```

★ Windows のパス区切りは `[\\/]` のように**角括弧の中で**書くこと。`C:\\Users\\` のようにバックスラッシュを重ねて書くと、Windows の Git Bash では `\\` が `\` に潰れて grep に渡り、`Trailing backslash` でパターン全体が壊れる（エラーに気づかないと、**何も検出されないまま「問題なし」に見える**）。パターンを変えたら、架空の電話番号・メールアドレス・ID・パス・キーを並べたテキストで全部ヒットすることを確かめる。

### 注意

- **gitignore 済みのファイル**（`.env`・`deploy/bot.service`・`memory/`・`chatlog/`・`ng/blacklist.json`・`ng/NG_WORD_private.txt` など）を `git add -f` で追加しないこと。
- 公開を意図して置かれている既存の情報（`SECURITY.md` の報告先など）は変更しない。公開してよいか判断できない値を見つけたら、PR を作る前に利用者へ確認する。
- `.env.example` のプレースホルダ文字列は変えないこと（変えるなら `settings_loader._PLACEHOLDER_VALUES` と `deploy/migrate_config_to_env.py` も対で直す。「規約と落とし穴」の設定の項を参照）。
- **既に push 済みのコミットに個人情報が見つかった場合**は、force-push で履歴を書き換えず、作業を止めて利用者に報告する（履歴の書き換えやキーの再発行など、利用者の判断が要るため）。
- Issue・PR へのコメントなど、リポジトリへ公開で書き込む文章にも同じチェックを適用する。

## バージョンとリリースの運用

機能追加のブランチ運用、README への `ver0.x.x(BETA)` の記録、Releases の登録と BETA の解除の方針は、Codex など他のエージェントと共有するため `AGENTS.md` に書いてある（次の行で読み込まれる）。方針を変えるときは `AGENTS.md` だけを直し、ここへ同じ内容を書き写さないこと。

@AGENTS.md
