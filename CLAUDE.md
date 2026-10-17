# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 概要

Google Gemini をバックエンドに持つ Discord ボット。ペルソナベースのテキストチャット、ボイスチャンネルでの TTS 読み上げ（ボイス接続中・FFmpeg 不要・edge-tts でペルソナごとに声が変わる）、画像/文書のマルチモーダル入力、長期記憶、ユーザーごとの親密度/感情トラッキング、コンテンツモデレーション、三審制ブラックリスト、チャンネル会話の議事録要約、ユーザー別会話統計ダッシュボード、無会話チャンネルへの自発的な話題提供、自律行動（ボットが自分からボイス/ステージチャンネルへ出て独り言を続け、コメントに配信者のように応答する）、記念日の自動お祝い、リマインダー、返信への 👍👎 リアクションによるフィードバック収集、オンボーディング導線（サーバー参加時の案内と使い方パネル）を備える。ボットのソースはすべて `src/` にある（開発者用ダッシュボードは別に `tools/console/`。後述）。ユーザー向け文字列・ログ出力（`print`）・コードコメントはすべて日本語。新規に追加するものも日本語で統一すること。

## 作業の進め方

### 確認を取ってから行うこと

次の操作は、**何をなぜ実行するかを伝えて承認を得てから**行ってください。

- ファイル・ディレクトリの削除
- `git push` / 履歴を書き換える操作（`rebase` / `reset --hard` / `commit --amend`）/ ブランチの削除。
  ただし、利用者が「作業完了を宣言します」と伝えた**後**に、末尾の「作業完了時の手順」に沿って行う作業ブランチの
  push と PR 作成は、その宣言が依頼にあたるので承認は要らない（宣言の前に push・PR 作成をしてはならない）
- **実行時データ**（`memory/` 配下の `bot.db`・`projects.json`、`ng/blacklist.json`）を消したり書き換えたりすること。
  会話の記憶・会話統計・リマインダー・違反記録が入っており、消すと取り戻せない
- **依存ライブラリの追加。** このプロジェクトは**ライブラリを足さない**判断を随所でしています
  （読み上げは FFmpeg を使わず miniaudio でメモリ上のままデコードし、音色の EQ は numpy だけで実装して
  `scipy` / `librosa` / `pydub` は使わない）。足す前に必ず相談すること

利用者から明示的に頼まれた操作には応じてかまいません（その場合も、危険な操作は内容を示して確認する）。

### git

- 作業は**トピックブランチ**で行い、`main` へ直接コミットしない。
  命名は `feat/...` / `fix/...`（例: `feat/issue-1-community-standards`）
- コミットメッセージは `feat:` / `fix:` + **日本語**の要約。本文には
  **「なぜそうしたか」と「★ 壊れやすい点」**を書く（既存のログが手本）
- **Codex が作成するコミットの Author / Committer は `Codex <codex@openai.com>` にする。**
  `git -c user.name=Codex -c user.email=codex@openai.com commit ...` で、そのコミットだけ名義を指定する。
  手動コミットの名義を保つため、共通の `git config user.name` / `user.email` は変更しない。
- **PR は、利用者が「作業完了を宣言します」と伝えた後にだけ出す。** それまではトピックブランチへのコミットまでに
  とどめ、push もしない。宣言を受けたら、末尾の「作業完了時の手順」に従い、**プライバシーチェックをしてから**
  PR を出す。既存ブランチの再利用でかまわない
- PR を出す前に、`CONTRIBUTING.md` の「完全ローカルで確認する」のコマンド（`pip check`・全ソースの構文確認・
  メッセージ分割の確認）を回す。回帰テストも標準の unittest で実行する（下記参照）。
  Discord・Gemini の実動作に関わる変更は検証用サーバーで確かめ、確かめられなかった範囲は PR に「未確認」と書く
- ★ **対応する issue があれば、PR 本文の末尾に `Closes #N` を書く**（複数なら 1 行ずつ）。
  本文中に `#N` と書くだけでは**参照リンクになるだけで、マージしても閉じません**
- PR・コミットにセッション URL を書かない
- README の更新履歴とリリースは、末尾の「バージョンとリリースの運用」（`AGENTS.md`）に従う

### 同時に走らせるエージェント

作業の複雑さに応じて分けてください。**同時に 3 体まで。**

- **同じ作業を複数のエージェント（Claude Code と Codex など）で並行させないこと。** 実際に、同じ依頼から
  Issue が重複して起票されたり、追加の push より先に PR がマージされたりした。Issue の起票・push・PR の編集の直前には、
  `gh issue list` / `gh pr view` で最新の状態を取り直す
- 共有の作業コピーを別のエージェントが触っている可能性があるときは、自分用の worktree（ブランチ）で作業する

## ボットの起動

ビルド工程・リンター設定は未整備。標準の unittest / IsolatedAsyncioTestCase による回帰テストと、Windows／Linux・Python 3.10／3.13で実行する `.github/workflows/tests.yml` がある。

**ローカル確認**（リポジトリルートで実行。`.env`・実際の DB・Discord・Gemini を使わない。`CONTRIBUTING.md` と同じもの）:

```bash
python -m pip check
# 外部通信・シークレット・実データ不要の回帰テスト
python -B -m unittest discover -s tests -v
# 1モジュールだけ／テスト名の一部に一致するものだけを実行する
python -B -m unittest tests.core.test_bot_reply_settings -v
python -B -m unittest discover -s tests -k test_last_slot_accepts_only_one_concurrent_request
# src/ と deploy/ の全 .py の構文確認（モジュールは起動しない）
python -B -c "from pathlib import Path; paths = sorted(Path('src').rglob('*.py')) + sorted(Path('deploy').glob('*.py')); [compile(p.read_text(encoding='utf-8-sig'), str(p), 'exec') for p in paths]; print('構文確認: OK')"
# 個別モジュールの動作確認の例（src を import パスに足す）
python -B -c "import sys; sys.path.insert(0, 'src'); from persona_bot.utils.discord_helpers import split_message; assert [len(p) for p in split_message('あ' * 4001)] == [2000, 2000, 1]; print('メッセージ分割: OK')"
```

テストは `tests/` 内で用途別に配置する。構成と個別実行の手順は [テストの案内](tests/README.md#フォルダ構成と個別実行) を参照する。CI は上記に加えて `python -m compileall -q src deploy tests` で `tests/` の構文も確かめ、`PYTHONIOENCODING=utf-8`・`MPLBACKEND=Agg` を設定する。Linux のジョブだけ `REQUIRE_JAPANESE_GRAPH_FONT=1` で日本語フォントの未導入を失敗にする（未指定の環境では字形テストを省略する）。

確認用のスクリプトから **`bot.py` を import しないこと**（トップレベルで設定の読み込みと DB の初期化が走る）。`persona_bot.core.message_handler.MessageHandler` は設定読込やDB初期化を実行せずimportできる。メッセージ処理のテストはサービスと外部境界をモックに差し替えてこのクラスを直接呼ぶ（AST抽出は不要。`tests/helpers/message_handler_helpers.make_message_handler` が、渡さなかった依存を `Mock` で埋めて組み立てる）。それ以外のモジュールも `sys.path.insert(0, 'src')` で import できる。外部サービスはモックに置き換え、DB を使う確認は一時ディレクトリと架空のデータで行う。

```bash
# 1. 依存関係のインストール (Python 3.10+)
pip install -r requirements.txt

# 2. シークレットの用意: プロジェクトルート直下（src/ の中ではない）に .env を作る。
#    cp .env.example .env してから API_KEY_GEMINI= と TOKEN_DISCORD= の右辺を実キーに書き換える。
#    .env は gitignore 済み。未設定のまま起動すると "キー未設定" を出して例外で停止する。

# 3. Opus 共有ライブラリはボイス TTS（/join・自律行動）で必要（FFmpeg はもう使わない）
#    Windows: 不要（discord.py が opus の DLL を同梱している）
#    Linux:   sudo apt install -y libopus0
#
#    requirements.txt の discord.py は必ず [voice] 付きにすること。ボイス接続に必要な
#    davey（DAVE = Discord が 2026-03-02 に必須化した音声E2E暗号化）が入らず、
#    /join が RuntimeError('davey library needed in order to use voice') で失敗する。

# 4. src/ ディレクトリの中から実行する:
cd src
python bot.py     # 成功時に "ログイン成功: <bot>" と表示される
```

起動は従来どおり **`src/` の中から `python bot.py`** とする。`bot.py` が配線を担当し、各モジュールは `persona_bot/` パッケージ内で用途別に分ける。内部のimportは `from persona_bot.core.app_state import BotState` のようにパッケージ名から指定する。各 `__init__.py` は説明だけを置き、サービスの生成や設定読込を行わない。プロンプトの相対パスは引き続き `src/` を基準に解決する（「パス解決」の項を参照）。

| フォルダ（`src/persona_bot/` 内） | 担当 |
| --- | --- |
| `config/` | 定数、選択肢、APIキー読込、パス解決 |
| `core/` | 状態、Discordクライアント、コマンド、メッセージ処理、バックグラウンド処理、SQLite |
| `services/` | Gemini、プロンプト、記憶、親密度、モデレーション、自発会話、自律行動、文書、統計、リマインダー、評価 |
| `views/` | 機能パネルと共通UI部品 |
| `audio/` | TTSと音声の加工 |
| `utils/` | Discord送信、JSON入出力、時刻、グラフ描画 |

## サーバー運用 (`deploy/`)

本番は Oracle Cloud 上の Ubuntu（ユーザー `ubuntu`、`/home/ubuntu/Persona-Chat-in-Discord`）で **systemd のサービス名 `bot`** として常駐する。ユニットは `deploy/bot.service.example`（プレースホルダ入り・Git 管理対象）と `deploy/bot.service`（実値入り・gitignore 済み）の**2本立て**で、実パスやユーザー名を公開側に残さない。導入・運用コマンドは README の「7. 常時稼働の設定」にある。

- **`WorkingDirectory` は `src/`、`ExecStart` は `.venv/bin/python` の絶対パス**。systemd はシェルを介さないため `activate` は効かない。
- **`StartLimitIntervalSec=0`（再起動のレート制限を無効化）を戻さないこと**: 既定の「10秒で5回失敗したら諦める」だと一時的な回線障害でボットが二度と復帰せず、systemd を入れた目的（手動回復をなくす）が失われる。副作用として設定ミスでは永久に再起動ループになるため、`systemctl status bot` が `activating (auto-restart)` のままなら設定ミスを疑う運用が前提。なお **systemd 229 以降この指定は `[Unit]` に置く** — `[Service]` に書くと黙って無視される。
- **`ProtectSystem=strict` / `ProtectHome=yes` を足さないこと**: 実行中に `memory/`（SQLite DB・JSON）と `ng/` へ書き込むため、read-only エラーで記憶とモデレーションが黙って壊れる。入れるなら `ReadWritePaths=` で個別に開ける必要がある。
- **`WatchdogSec` は未導入**（依頼どおり、ハング型のフリーズが実際に観測されてから検討する）。`Restart=always` が拾えるのはプロセスが落ちるケースだけで、「生きているのに応答しない」状態は拾えない。導入にはアプリ側の `sd_notify(WATCHDOG=1)` 定期送信が必須で、送信が滞ると健全なプロセスまで殺す。
- **ユニットファイルは LF 改行必須**（`.gitattributes` で固定済み）。CRLF だと `ExecStart=` のパス末尾に `\r` が混入して起動に失敗し、原因が非常に分かりにくい。
- ログは journald（`journalctl -u bot -f`）。`print` の出力を即時反映させるため `PYTHONUNBUFFERED=1` を、日本語・絵文字で `UnicodeEncodeError` を起こさないため `PYTHONIOENCODING=utf-8` をユニット側で指定している。

## 開発者用ダッシュボード (`tools/console/`)

SSH 経由でボットを見守り、サービス操作・デプロイを行う開発者用の画面（React + TypeScript の画面と、FastAPI + asyncssh のバックエンド）。ボットとは依存関係も起動も分けており、ボットの `requirements.txt` と CI には含めない。セットアップ・起動・設計は `tools/console/README.md` にある。

- **ボットのコード（`src/`）をダッシュボードのために変えないこと。** 値は SSH で読める既存の情報源（systemd・journald・`bot.db` の読み取り専用・サーバー上のトークンでの Discord API の読み取り）から集める。ボットの import もしない（手元の `database.py` の版も `ast` で読むだけ）。
- **実行できるコマンドは `backend/remote_commands.py` のホワイトリストだけ。** 画面から届いた文字列をコマンドに埋め込まない。ホスト鍵の検証を切らない（`known_hosts=None` にしない）。127.0.0.1 だけで待ち受け、専用ヘッダー・Host・Origin を確かめる。
- **デプロイは `backend/deploy.py` が管理する。** 接続先の `deploy_branch`（既定 `main`）を `origin` から取得して早送りし、`requirements.txt` が変わったときだけ設定した `python` で `pip install`・`pip check` を実行して再起動する。ブランチの不一致・Git 管理対象の未コミット変更・サーバーにだけあるコミットは事前確認で拒否する。更新後に失敗したらコードと更新した依存関係を戻し、再起動に進んでいた場合は前の版で再起動・起動確認を行う。`bot.db` は戻さない。同時実行は1件だけとし、最後の記録はメモリ上に保持する。
- **起動確認は「ログイン成功」の文言に依存する。** `src/bot.py` の `on_ready` が出す `ログイン成功: <ボット名>` を `backend/remote_commands.py` の `LOGIN_SUCCESS_TEXT` で探す（最大120秒）。再起動後の PID の変化・稼働状態・自動再起動の回数も確かめる。文言を変えたら判定側・`backend/tests/fake_ssh_server.py`・関連テストと説明をそろえる。ログイン後の各機能の実動作は別途確認する。
- **自動の確認で本番の接続先へつながないこと。** 模擬 SSH サーバーは `backend/tests/fake_ssh_server.py` の自動テスト専用とし、起動スクリプトや画面の接続先には出さない。SSH・デプロイのテストはループバックの空きポートと一時フォルダの鍵・known_hosts を使う。実際の接続先は `backend/profiles.json`（Git 管理外）に書く。
- **見本（`backend/profiles.example.json`・`examples/ssh_config.example`・`examples/sudoers.example`）の書き換える値は `YOUR_` で始める**（`.env.example` と同じ流儀）。`profiles.py` の `_PLACEHOLDER` がこの形の値が残った接続先を見分け、バックエンドは接続を断る（仮のホスト名を DNS が偶然引いて、知らないサーバーへつなぎに行かないように）。見本の値の書き方を変えたら、`_PLACEHOLDER`・3つの見本・`tests/test_profiles.py` をそろえて直すこと。実際の値は見本ではなく `backend/profiles.json`（Git 管理外）に書く。
- **依存は公開から2週間以上たった版に固定する**（`frontend/package.json` は完全一致。`.npmrc` の `save-exact`）。依存の追加は、ボットと同じく事前に相談する。
- **伏せ字モード（`frontend/src/privacy.ts`）:** 画面が知っているホスト名・ボット名・サーバー名などを集め、エラー文・ログ・コマンド履歴の中からも探して隠す。秘密になりうる値を新しく表示したら `collectSecrets` に足すこと。ログの会話は、ボットの `usage_graph.log_snapshot` の書式（`User (…)：` から `AI：` の応答と区切り線まで）で見分けているので、**この書式を変えたら `privacy.ts`・`backend/tests/fake_ssh_server.py`・`backend/remote_scripts/insights.py`（`User (` と `Total token count` を数える）も合わせて直す**。
- **動き:** CSS の `--spring-*` は `frontend/src/theme/motion.ts` がばねの物理計算から作り、最初の描画より前に書き込む（CSS に静的な値は無い）。跳ねない Standard が既定（利用者の好み）。
- **確認:** `tools/console/backend` で `.venv\Scripts\python.exe -B -m unittest discover -s tests`、`tools/console/frontend` で `npm run build`（`tsc --noEmit && vite build`）。

## アーキテクチャ

全体像の図は `docs/フロー図.drawio`（draw.io 形式。起動フロー・メッセージ受信〜応答・`on_message` パイプライン・自律行動の 4 ページ）にある。図はコードを書き写したものなので、構成を変えたら図も直すこと。

### 配線 (`bot.py`)

`bot.py` がコンポジションルート（合成の起点）。設定を読み込み、`genai_client.create_genai_client` で共有SDKクライアントを生成し、`Database`（`database.py`、後述）を最初に初期化してから（`PersonaClient` にも渡し、終了時に閉じさせるため Discord クライアントより先に作る）、各サービスのインスタンスを1つずつ生成して、DI フレームワークを使わず手作業で相互に注入する。共有される可変ランタイム状態はすべて単一の `BotState` データクラス（`app_state.py`）にまとめられ、必要なサービスに渡される。**ただしペルソナ・モデル・応答モードなど「サーバーごとに変わる設定」は `BotState` 直下ではなく `GuildSettings` に入っている**（「ギルド単位の設定」の項を参照）。`register_commands`（`commands.py`）がスラッシュコマンドを `CommandTree` に登録する。`on_ready` でコマンドを同期し、各種の見回りループ（`memory_maintenance_loop` / `auto_topic_loop` / `autonomous_loop` / `anniversary_loop` / `reminder_loop` / `feedback_prune_loop`。いずれも `background.make_guarded_loop` で作る）を開始し、文書ワーカーのコルーチンを起動する。ループの二重起動は `BotState` の `*_background_started` フラグで防ぐ（`on_ready` は再接続のたびに発火するため）。

### ギルド単位の設定 (`app_state.py`)

同じボットを複数の Discord サーバーへ招待しても設定が混ざらないよう、可変設定は**サーバー（ギルド）ごとに分離**されている。

- **`GuildSettings`**: ペルソナ（`current_prompt_file`）・モデル（`current_model_name`）・応答モード（`current_respond_mode`）・グラウンディング（`grounding_enabled`）・会話リセット地点（`channel_reset_points`）・自発会話の設定と監視状態（`auto_topic_*`）・自律行動の設定と配信状態（`autonomous_*`）・記念日の投稿記録（`anniversary_posted_dates`）を持つ。
- **`BotState.get_guild(guild_id)`**: そのサーバーの `GuildSettings` を返す。未登録なら初回アクセス時にデフォルト値で自動生成する。**`guild_id=None`（DM）は `BotState.dm_settings` を返す** — DM にはギルドが無いため、全 DM で1スロットを共有する。`iter_all_guild_settings()` は「全サーバー + DM」を横断する見回り処理用。
- **`BotState` に残すもの**: 最終活動時刻（`last_activity_timestamp`）とバックグラウンドタスクの起動フラグ類のみ（サーバーに依存しないプロセス全体の状態）。
- **永続化**: 設定値（ペルソナ・モデル・応答モード・グラウンディング・自発会話の ON/しきい値・自律行動の ON）と記念日の投稿記録は DB に保存され、起動時に `BotState.restore_from_database(db)`（`bot.py` の `Database` 初期化直後に1回）が復元する。**設定を変えたら `await guild_settings.persist()` を呼ぶこと** — UI の確定ボタンやトグルの直後がその場所。保存はフェイルオープン（失敗してもインメモリの変更は残る）。`persist()` は `_persist_lock` で直列化し、**保存する値はロックを取ってから読む**（保存は行の総入れ替えなので、並んだ2つの保存の書き込み順が入れ替わると古い値で上書きされる）。書き込みは `run_in_thread` で待つ（素の `asyncio.to_thread` に戻すと、キャンセルの瞬間にロックが外れて同じ上書きが起きる）。記念日の記録（`record_anniversary_post`）も `run_in_thread` で待つ。`get_guild()` が新しく作る `GuildSettings` には `attach()` で DB が引き継がれるので、呼び出し側は何もしなくてよい。`restore_from_database` は、復元より前に `get_guild()` で作られていた設定にも `attach()` する（つながないと、その設定の `persist()` が DB へ書かれない）。
- **永続化しないもの**: `channel_reset_points` / `auto_topic_channels` / `auto_topic_posted_at` / `autonomous_session` / `autonomous_last_activity` / `autonomous_recent_activity` は実行時に自然に再構築される一時状態のため、意図的にインメモリのみ。ここを永続化したくなった場合はチャンネル単位の書き込みが毎メッセージ走る点に注意すること（`autonomous_*` の実行時状態はそもそもボイス接続が再起動をまたげないため、復元しても意味がない）。
- **規約**: サービス層は `BotState` から設定を勝手に読まないこと。`bot.py` / `commands.py` が `state.get_guild(message.guild.id if message.guild else None)` で解決し、**`GuildSettings` かその中の値（例: `model_name`）を引数で渡す**。`GeminiService.generate()` / `summarize_document()` が `model_name` を必須引数に取るのはこのため（サービスが「どのギルドの設定か」を知らずに済む）。固定モデルを使う経路（`stats_service.py` のルーターなど）は `ROUTER_MODEL_NAME` を明示的に渡す。UI の各 View も `BotState` ではなく `GuildSettings` を受け取る。

### `on_message` パイプライン (`message_handler.py`)

`bot.py` のイベント関数は、配線時に生成した `MessageHandler.on_message` へ委譲する。このクラスが `notify_violation` と `handle_chat_message` も持ち、起動処理とは独立して同じ実装をテストできる。ボット以外のメッセージはすべて、決まった順序の関門を通過する。編集時はこの順序を守ること:

1. **応答モードのゲート** — `"mention"` モード（デフォルト）では、DM 以外でボットへのメンションが無いメッセージは無視される。**例外は自律行動の配信中のチャンネル**（`autonomous.is_comment_channel`）で、そこへの書き込みはメンション無しでも「コメント」として拾う。ゲートより前（ただし 2 のブラックリスト判定の後）に `auto_topic.mark_activity` と `autonomous.mark_activity` を呼び、ボット宛てでない発言も「会話が行われている」観測として記録する。
2. **ブラックリスト**（`blacklist_service`）— ブロック中のユーザーは黙って破棄される。**この判定だけは 1 より前、サーバー設定の解決と活動の観測よりも先に行う**（後に置くと、無視しているはずの発言で自発会話の沈黙タイマーが戻り、自律行動のゲージや無応答での退出まで左右される）。
3. **NG ワードフィルタ**（`ng_word_service`）— クリーニング済みテキストへの正規表現マッチ → `notify_violation`。
   **配信コメントの受付枠** — NG ワード判定の後、入力モデレーションの前に `autonomous.claim_comment_slot` で同期的に確保する。枠が無いコメントは API を呼ばず終了する。活動観測はゲート前に済んでいる。
4. **ユーザー入力の AI モデレーション＋記憶ルーティング＋リマインダー抽出**（`content_moderator.check_and_route`）→ 不適切なら `notify_violation`。安全なら記憶カテゴリ（`profile`/`projects`/`none`）と、リマインダー依頼なら日時＋本文も同時に得る。**対象テキストが同一なので3つの判定を1回の Gemini 呼び出しに統合して API コストを抑えている**（個別に呼べば3回）。戻り値は `(is_safe, reason, category, reminder)` の4つ。
5. **添付の振り分け** — 未対応の拡張子が混ざっていれば断って終了する（案内文は `discord_helpers.describe_supported_attachments` が `IMAGE_EXTENSIONS` / `SUPPORTED_DOCUMENT_EXTENSIONS` から組み立てる。拡張子を文言に直書きしない）。文書添付（`pdf/txt/csv/md`）はサイズ（`MAX_DOCUMENT_SIZE_BYTES`）を確かめてから非同期の `DocumentService` ワーカーへキュー登録し、受付の返信だけ返して終了する（後でワーカーが回答する。ワーカー側で別途 `route_category` する）。画像だけの添付は通常のチャットへ進む（本文が空なら `IMAGE_ONLY_QUESTION` を質問の代わりにする。画像を1枚も読み込めなかったときは Gemini を呼ばずに断る — 呼ぶと見えていない画像への反応を作り話で返す）。
6. **会話統計の記録とリマインダー** — `stats.record` を fire-and-forget で呼んでから、リマインダー依頼が抽出されていれば `reminder_service.create_from_request` で登録し、確認だけ返して以降の処理は行わない（同じ発言にチャット返信も返すと二重投稿になるため）。日時が過去・解釈不能などで登録を見送った場合は `None` が返るので、通常の会話へ流す。文書を添えた依頼は 5 で終わるため、リマインダーより文書処理が優先される。
7. **ルーティング** — 自律行動の配信中のチャンネルなら `autonomous.handle_comment`（配信者としての応答）、それ以外は `handle_chat_message` が同期的に実行される。配信中かどうかは**ここで取り直す**（4 の Gemini 呼び出しの間に配信が終わることがあるため）。同期チャット返信の後は、必ず `play_tts_response`（`voice.py`）が呼ばれて読み上げを試みる — ボットがボイスチャンネルに接続中でない限り no-op（何もしない）。再生中に届いた返信はキューに積まれ、順番に読み上げられる。

`notify_violation` は違反を1回記録し、3回目でユーザーをブラックリスト登録する（`BLACKLIST_MAX_VIOLATIONS`）。並行して届いた別の違反で既にブロック済みのとき（`record_violation` は回数を増やさず `just_blocked=False` を返す）は何も返信しない（「あと0回」の警告や二重の案内を出さない）。NG ワードとモデレーションは、送信前に**ボット自身の応答に対しても再度**適用される（応答中の NG ワードは ● でマスクされ、不適切な応答は定型の謝罪文に差し替えられる）。

### プロンプト組み立て (`MessageHandler.handle_chat_message` → `prompting.py`)

メッセージごとに、単一のフラットなプロンプト文字列を組み立てる。構成要素は、ペルソナのテンプレート + 親密度/感情ブロック + 選択された長期記憶 + 文書要約 + チャンネル履歴 + 現在の質問。履歴の添付は、画像なら `[画像添付あり]`、それ以外（文書など）は `[添付ファイルあり]` と区別して書く（`build_history_text`。すべて画像と書くと、PDF を添えた発言までモデルが画像付きと誤解する）。Gemini 呼び出しは `(text, usage_metadata, grounding_sources)` を返す。

### データベース (`database.py`)

スキーマ初期化では `BEGIN IMMEDIATE` で明示的にトランザクションを開始し、初版テーブルの作成・全未適用DDL・`PRAGMA user_version` を同時に確定する。DDLは接続のコンテキストマネージャーだけでは自動でトランザクションを開始しないため、この開始処理を外さないこと。途中失敗時は開始前のスキーマと版へ戻り、再起動で再試行する。移行成功のログはコミット後に出す。

SQLite（`memory/bot.db`、WAL モード）が構造化データの永続化先。SQL 文はすべて `database.py` に集約されている。テーブルは9つ: `affinity`（親密度・感情）、`user_stats`（会話統計）、`queue_tasks`（文書処理キュー）、`pending_log`（記憶圧縮前の一時ログ。`user_id` 列でユーザー別）、`profile`（プロフィール記憶。`(user_id, key)` の複合主キーでユーザー別。値は JSON 文字列）、`guild_settings`（サーバーごとの設定。`guild_id` が主キー。DM は `DM_SETTINGS_GUILD_KEY`＝`0`。**保存は `INSERT OR REPLACE` による行の総入れ替え**なので、列を足したら `guild_settings_load_all` / `guild_settings_save` / `GuildSettings.apply_persisted` / `GuildSettings.persist` の4か所を必ず揃えて直すこと — 1か所でも漏らすと設定を保存するたびにその項目が既定値へ黙って戻る）、`anniversary_posts`（記念日のお祝い投稿記録。`(guild_id, user_id, label)` が主キー）、`reminders`（リマインダー。`status` は `pending`/`done`/`canceled`。見回り用に `(status, remind_at)`、本人の登録時の上限確認・一覧・件数用に `(user_id, status, remind_at)` の索引つき。`recurrence` は `none`/`daily`/`weekly`/`monthly` で、繰り返しは通知後も `pending` のまま `remind_at` だけが進む）、`feedback`（返信への 👍👎 評価。`(message_id, user_id)` が主キーで押し替えは上書き。集計用に `(guild_id, created_at)` の索引つき）。`projects.json` は LLM が構造を自由に決めるため、テーブル化せず JSON ファイルのまま（ただし `{"_schema": "per_user_v1", "users": {user_id: {...}}}` の形でユーザー別に束ねる）。**`affinity`/`user_stats`/`profile`/`pending_log`/`projects.json` はすべて `user_id` で分離され、記憶は完全にユーザーごとに独立している**（他人の記憶が混ざらない＝プライバシー保護）。

- **スキーマ変更の手順**: `SCHEMA_VERSION` を1つ上げて `_MIGRATIONS` にそのバージョンの SQL 文を追記する。適用済みバージョンは `PRAGMA user_version` に記録され、起動時に未適用分だけが実行される。`_CREATE_TABLE_STATEMENTS`（初版の形）は変更禁止 — 変更は必ず `_MIGRATIONS` 側で行う（実例: バージョン2 = `profile` テーブル追加、バージョン3 = `profile` の複合主キー化＋`pending_log.user_id` 追加、バージョン4 = `guild_settings` / `anniversary_posts` テーブル追加、バージョン5 = `reminders` テーブル追加、バージョン6 = `reminders` へ繰り返し列（`recurrence` / `recurrence_anchor`）追加、バージョン7 = `feedback` テーブル＋索引追加、バージョン8 = `user_stats` へ統計収集の停止列（`stats_opt_out`）追加、バージョン9 = `guild_settings` へ自律行動の有効/無効列（`autonomous_enabled`）追加、バージョン10 = `reminders` へユーザー別の索引（`idx_reminders_user_status`）追加）。**`SCHEMA_VERSION` は自分が追加した分ちょうどまでしか上げないこと** — 番号を飛ばすと、後から埋めた小さいバージョンは `user_version` が既に先へ進んでいるため永久に実行されない。
- **ユーザー別記憶への移行（バージョン3）**: 導入前は単一ユーザーのテスト運用のため記憶を「全体共有の1スペース」で持っていた。移行時、旧・全体共有データ（`profile` 行・`pending_log` の未割当行・`projects.json` の中身）は失わないよう `LEGACY_MEMORY_OWNER_ID`（`constants.py`。旧・唯一の実利用者の Discord ユーザーID）の記憶として引き継ぐ。SQL では担えない「旧行を owner へ割り当てる」後始末は `_migrate_legacy_profile_rows`（冪等。スキーマ移行本体とは別トランザクション）が行う。引き継ぎ元が不要になれば `LEGACY_MEMORY_OWNER_ID` を `""` にしてよい。`""` のときは旧データを破棄する — 旧 `profile` 行に加え、`pending_log` の未割当行（`user_id = ''`）も起動時に削除し、旧 `pending_log.txt` は取り込まない（ファイルは `.imported.bak` として残る）。残すと `user_id=''` の「誰でもない利用者」として記憶圧縮の対象に数えられ、誰も読まない記憶のために Gemini を呼び続ける（`pending_distinct_user_ids` も念のため空の `user_id` を返さない）。
- **旧 JSON からの取り込み**: 初回起動時、旧ファイル（`affinity.json`/`user_stats.json`/`queue.json`/`pending_log.txt`）が残っていれば DB へ取り込み、`<元の名前>.imported.bak` に改名する（バックアップ兼二重取り込み防止）。JSON として読めない・最上位が object でない（`queue.json` は `tasks` が配列でない）ファイルは `_load_legacy_json_object` が例外にするので、取り込まず改名もせずに残して警告し、次回起動時に再試行する。**`_json_loads_or` で `{}` 扱いに戻さないこと** — 戻すと、取り込めなかったファイルが「取り込み済み」として改名され、誰も気づけない。
- **profile.json は手書き追加用の受け皿**: 毎起動時、`profile.json` に中身があれば `profile` テーブルへマージ（同名キーはファイル側が勝つ）し、取り込めた場合だけ `write_json_file` で原子的に `{}` に戻す。ユーザー別記憶に対応するため形式は `{ユーザーID: {キー: 値, ...}, ...}`（値が dict でない旧・全体共有形式は owner へ取り込む後方互換あり）。JSON として読めない場合や、旧形式の owner が未設定・全ユーザーの内容が空などで1件も取り込めない場合は、ファイルを残して警告する。DB保存やリセットの失敗時も原本を保持し、次回起動時に再試行する（手書きミスやI/O障害でデータを失わない）。
- **並行処理**: 各サービスが `run_in_thread`（＝別スレッド）から呼ぶため、接続は `check_same_thread=False` + 内部の `threading.Lock` で直列化。書き込みはトランザクションで原子的（旧 JSON 方式と違い、書き込み途中のクラッシュでデータが壊れない）。`close()` も同じロックを取り、実行中の読み書きが終わってから閉じる（何度呼んでもよい）。終了時は `PersonaClient.close()` が Discord → Gemini → DB の順に閉じる（DB を最後にするのは、終了処理中の保存が使うため）。

### JSONファイルの保存 (`storage.py`)

`write_json_file` は同じディレクトリの一時ファイルへUTF-8で書き、flush・fsync・close後に `os.replace` で差し替える。置換前の失敗では元のファイルを保ち、一時ファイルを回収する。既存ファイルの権限モードも引き継ぐ。`load_json_file` は未作成時のみそのまま既定値を返し、空ファイル・不正JSON・不正UTF-8・最上位がobject以外の場合は `<元名>.corrupt-<UTC日時>-<識別子>.bak` へ退避後、ログを出して既定値で再開する。退避失敗や読込のI/Oエラーは例外を伝播し、その後の既定値による上書きを防ぐ。破損前の内容を自動復元する機能はなく、復旧にはバックアップを確認する。実行時ファイルは従来どおり単一プロセスで扱う。

### 記憶システム (`memory_service.py`)

`build_contexts` は非同期の記憶取得入口。`io_lock` の下でワーカーへ処理を渡し、通常記憶と文書記憶を同じスナップショットから組み立てる。`projects.json` は1回の取得につき1回だけ読む。ファイルを手書きした場合も次回の取得で反映し、永続的なキャッシュは持たない。個別取得の `build_context` / `build_document_memory_context` もawaitする。文書要約保存・圧縮時のJSON読込と書込・プロンプトの主経路の読込もイベントループへ同期I/Oを持ち込まない。

カテゴリでルーティングされる長期記憶。**すべて `user_id` でユーザー別に分離される。** DB の `profile`/`pending_log` テーブルと、`projects.json`（`memory/` 配下の JSON。`users[user_id]` にユーザーごとのスライスを持つ）で構成される:
- **ルーティング**: 各質問を `profile` / `projects` / `none` に分類し、**そのユーザーの**スライスだけをプロンプトに読み込む。チャット主経路では `content_moderator.check_and_route` がモデレーションと同時に分類する（`route_category` は文書ワーカーなど主経路以外で使う）。`route_category` はあいさつ・相槌などの短い定型発言を正規表現（`_looks_memory_irrelevant`）で先に弾き、LLM を呼ばず `none` を即決して API を節約する。失敗時は `none` にフォールバック（安全側）。
- **書き込み経路**: 各返信の後、Q/A を発言者の `user_id` 付きで非同期に `pending_log` テーブルへ追記する。
- **コンパクション（圧縮）**: `flush_if_needed`（`MEMORY_MAINTENANCE_MINUTES` ごとのバックグラウンドループ。返信後にも便乗して発火する）は、`pending_log` を持つユーザーを列挙し、**ユーザーごとに個別に**圧縮する（他人の会話ログを同じ Gemini 呼び出しに混ぜない＝プライバシー保護）。各ユーザーは Q/A 件数のしきい値（`MEMORY_FLUSH_THRESHOLD_ENTRIES`）を超えるかボットがアイドルのときに、そのユーザーの `pending_log` を `profile` テーブル/`projects.json` の該当スライスへ圧縮統合する（他ユーザーのスライスには触れない）。1回のフラッシュで処理する人数は `MEMORY_FLUSH_MAX_USERS_PER_RUN` で上限を設け、一度に大量の API 呼び出しが走らないようにする（残りは次回の見回りで処理）。フェイルセーフ設計で、パース失敗時は pending ログを**保持**（リトライ）するが、3回連続失敗した場合 — またはセーフティフィルタによるブロック時 — は無限ループを避けるためそのユーザー分をクリアする。連続失敗回数はユーザー別に数え、ログをクリアしたとき（ブロック時も）は 0 に戻す（消したログの失敗を新しいログへ持ち越すと、一時エラー1回で上限に達して消えてしまう）。`io_lock` + `flush_lock` で保護されている。
- **文書要約は圧縮の対象外**: `projects.json` の `document_summaries` は圧縮プロンプトへ**渡さない**（プロンプトが「不要な古い情報は削除」と指示するため、渡すと LLM の判断ひとつで要約が消えたり別のキーへ書き換えられたりして戻せない）。保存時（`_save_compacted_projects`）は、書き込み直前に読み直した最新の一覧をそのまま引き継ぎ、LLM の出力に同名のキーがあっても捨てる。圧縮を待つ間に `store_document_summary` が追記した分も、この読み直しで自然に残る。要約の件数管理（`DOCUMENT_SUMMARY_LIMIT`）は `store_document_summary` だけが担う。

### 文書パイプライン (`document_service.py`)

文書の取得後、最初の添付処理より前に `snapshot_response()` を取得し、全添付の要約・プロンプト読込・最終生成で共用する。`build_contexts` の文書件数は `max(DOCUMENT_MEMORY_CONTEXT_LIMIT, len(attachments))` とし、同時添付が既定件数を超えても先頭の要約を落とさない。PDFは `with temp_path.open("rb")` のストリームを `PdfReader` へ渡し、検証の例外時も閉じる。検証ワーカーは `run_in_thread` で終了まで待ってからキャンセルを伝播し、Windowsで削除前にハンドルを解放する。終了タスクの状態保存と古いタスクの掃除は別に扱い、掃除の例外はログだけに留める（完了済みタスクを失敗へ戻したり、回答後にエラーを二重返信しない）。

添付ファイルはチャットをブロックしないよう別系統で処理される。`enqueue_from_message` がタスクを DB の `queue_tasks` テーブルに書き込み（`author_id` を含む）、単一のワーカーコルーチンが `pending` タスクを取得し、一時ファイルへダウンロードし、検証し、Gemini へアップロードし、構造化 JSON へ要約し、要約を**投稿者（`author_id`）の** `projects.json` スライスへ保存し、返信を生成し、その後アップロード済みファイルと一時ファイルを削除する。起動時、`processing` のまま止まっているタスク（処理中にボットがクラッシュしたもの）は `pending` へ戻される。完了済みタスクは `QUEUE_MAX_FINISHED_TASKS` 件に切り詰められる。タスクの取得（claim）は SELECT と UPDATE を同一トランザクションで行うため二重取得されない。PDF の検証に使う `pypdf` は `validate_pdf_file` の中で import する（モジュールの先頭へ戻さないこと — 未導入の環境で起動と回帰テストの検出まで止まる）。未導入なら PDF だけが日本語の `ValueError` で失敗し、PDF の回帰テストは省略される。

### Gemini 呼び出し (`gemini_service.py`, `genai_client.py`)

`google-genai` の共有 `Client` を、生成・モデレーション・記憶ルーティング・記憶圧縮・文書処理へ注入する。接続先の種類はGemini Developer APIを明示し、APIキーはSDKがヘッダーに載せる。独自REST経路は使わない。Discord側は `bot_client.PersonaClient` を使い、終了時に `close_genai_client` でSDKの非同期・同期接続と、factoryが所有するHTTPX接続を閉じる（その後に、渡された `Database` も閉じる）。SDKは渡されたHTTPX接続を閉じないため、共通終了処理を使う。

生成は `genai_client.generate_content` → `client.aio.models.generate_content` へ集約する。画像はPIL画像をSDKに渡し、検索は `types.Tool(google_search=types.GoogleSearch())` を指定する。検索非対応を示す400/404/422の場合だけ、設定が有効なら `GROUNDING_MODEL_NAME` で検索を試し、最後にユーザーの選択モデルで検索なしへ進む。タイムアウト・5xx・認証・利用枠・その他の通信エラーでは追加生成しない。サブモデルを使っても、最後の非検索生成には元の `model_name` を渡す。

`GeminiService.generate` は開始時にmonotonicな締切を1回だけ決め、各段に残り時間だけを渡す。共通入口は秒をSDKの `HttpOptions.timeout`（ミリ秒）へ変換し、`HttpRetryOptions(attempts=1)` で生成のSDK内部リトライを止め、`asyncio.wait_for` でも同じ時間を制限する。期限切れは `(None, None, [])`、利用者からのキャンセルは伝播する。timeout未指定の検索付き生成は `GROUNDING_REQUEST_TIMEOUT_SECONDS` を使う。記憶取得や生成後のモデレーションはこの生成期限の対象外。

アップロード・削除は `client.aio.files`、文書要約は共通の生成入口を使う。PDFは `application/pdf`、txt/csv/mdは `text/plain` を明示し、ファイル転送にも全体の期限を設ける。factoryは公開HTTPXクライアントを指定し、aiohttp経路のSDK独自再送を避ける。転送transportはuploadコマンドの応答に必須の状態ヘッダーがない場合、応答を閉じて明示的に失敗させる。これによりSDK内の同期sleep・再送を防ぎ、イベントループと期限管理を保つ。SDKのprivate実装は上書きしない。

Geminiのテキストは `storage.safe_response_text` から取得する。空候補・プロンプトや候補の安全ブロックでは本文を返さず、思考パートはSDKの本文取得で除外される。`get_prompt_block_reason` は新SDKの文字列enumと旧形式の数値enumを区別し、未指定をブロックと誤認しない。モデルのJSONは `storage.extract_json_object` でコードブロックを優先し、ネスト・文字列内の括弧を標準パーサーで処理する。不正候補の後も走査し、モデレーションは `required_keys=("safe",)` で説明中の別形式のJSON例を除外する。

### 親密度 / 感情 (`affinity_service.py`)

ユーザーごとの2次元感情（valence/arousal、30分の半減期で 50 へ減衰）に加えて、緩やかに変動する親密度。DB の `affinity` テーブルに永続化される。モデルには返信の末尾に `[V:X, A:Y]` タグを付けるよう指示しており、`handle_chat_message` がタグを抽出・除去し、親密度・感情を更新して対象ユーザー分の保存を開始する。本文はその後にモデレーション・送信される。保存は `asyncio.Lock` で直列化される。

通常の保存は `AffinityService.save_background(user_id)` → `Database.affinity_upsert(user_id, entry)` で対象1行だけを更新する。`affinity_save_all` は明示的な全件置換用で、返信ごとの保存には使わない。ロック内で対象の現在値だけをコピーし、キャンセル時も `run_in_thread` の完了までロックを保持する。親密度・機嫌・テンション・`last_interaction` の4項目を保存・復元し、半減期・中立値・上下限・親密度の増減しきい値は `AFFINITY_*` から参照する。

**感情タグを扱うコードはこのモジュールに集約すること。** `[V:X, A:Y]` の正規表現は `_AFFINITY_TAG_PATTERN` の1か所だけに置く（空白と大文字・小文字のゆらぎを許容する。`[v:+5, a:-10]` も抽出・除去する）。`extract_affinity_changes(text)` は `(本文, (機嫌の変化量, テンションの変化量))` を返し（タグが無ければ変化量は `None`、本文は無加工）、`strip_affinity_tag(text)` は除去だけを行う（自発的な会話・記念日のお祝い・会話要約のように、特定の相手への返信ではなく親密度を更新しない経路で使う）。読み上げ用の `strip_speech_tags(text)` はこれを再利用し、`_SPEECH_EMOTION_TAG_PATTERN` で感情名（happy / sad / angry / neutral / excited / calm / surprised / scared）と arousal / valence / affinity の数値タグも除去する。一般の角括弧付き本文は保持する。以前は同じ正規表現が `bot.py` / `auto_topic_service.py` / `summary_view.py` に3重定義されており、空白の許容などに差異が生まれるとタグがそのままユーザーへ露出する不具合に直結していた。

### ボイス / TTS (`voice.py`, `voice_dsp.py`)

`play_tts_response(message, response_text, prompt_file)` は、**ボットがボイスチャンネルに接続している間だけ**（`/join` で呼ばれたときと、自律行動で自分から入っているとき）AI の返信を読み上げる。未接続・DM・整形後に本文が残らない場合は黙って何もしない（no-op）。チャット本体は読み上げの成否に影響されないフェイルオープン設計。`prompt_file` は `MessageHandler` が今回の応答に使った `ResponseSettings.prompt_file` を渡す（サービス層に `BotState` を読ませない規約どおり、値だけを渡す）。

**元メッセージを持たないテキストは `speak_text(guild, text, prompt_file)` を使う。** `play_tts_response` はこれを `message.guild` 付きで呼ぶだけの薄い層で、実体（接続確認・整形・キュー投入）はすべて `speak_text` 側にある。自律行動の独り言のように「どの発言への返信でもない」読み上げはこちらを直接呼ぶこと（キュー・ワーカーを二重に実装しないため）。

自律配信の第一声・独り言・コメント応答では、両入口の `is_valid` にセッションと接続先の照合関数を渡す。キューには本文・ボイスプロファイルと一緒にこの関数も保持し、投入前・合成前・再生直前に確認する。合成中にVoiceClientが差し替わった場合も再生しない。通常チャットの読み上げではこの判定を省略できる。

**音声エンジンは edge-tts（主）＋ gTTS（退避）の二段構え。** `_fetch_mp3` が主エンジンの **edge-tts**（Microsoft Edge の読み上げが使うニューラル音声。API キー不要）で合成し、例外または空応答なら **gTTS**（`lang=TTS_LANG`）へ退避する。edge-tts は公式に third-party 利用が認められた API ではなく過去に Microsoft 側の仕様変更で使えなくなった経緯があるため、**この退避経路を消さないこと**（消すと壊れた瞬間に読み上げ機能ごと止まる）。退避中は話者・高さ・速さを再現できずキャラ差が失われるが、音色（EQ）だけは掛かる。退避と復旧のログは `_edge_tts_failing` で**状態が変わったときだけ**出す（失敗が続く間、返信のたびにログが溢れないようにするため）。

**FFmpeg には依存しない。** どちらのエンジンも返す mp3（24kHz モノラル）を **miniaudio**（C ライブラリを同梱した pip の binary wheel。exe 不要）で**メモリ上のまま** `TTS_SAMPLE_RATE`（48kHz）/ `TTS_CHANNELS`（ステレオ）/ 16bit の生 PCM へデコードし、`discord.PCMAudio` に渡す。Discord のボイス送信はこの形式をそのまま受け取れるため、変換用の外部プロセスも一時ファイルも要らない（CPU 1コア・メモリ 1GB 級の VPS で動かすための構成）。**`discord.FFmpegPCMAudio` を使う実装に戻さないこと** — 返信のたびに別プロセスが起動し、小メモリ環境では OOM の原因になる。

- **非同期と別スレッドの使い分け**: `_synthesize_pcm` は **async 関数**。edge-tts は async ネイティブなのでイベントループ上でそのまま `await` する（通信待ちでループは塞がらない）。同期処理である gTTS の呼び出しと、デコード＋EQ の CPU 処理だけを `run_in_thread` へ逃がす。全体を `asyncio.wait_for(_synthesize_pcm(...), timeout=TTS_TIMEOUT_SECONDS)` で包むが、同期処理中のキャンセルはワーカー完了を待って伝える。**edge-tts を `asyncio.to_thread` の中で `asyncio.run()` して呼ばないこと** — イベントループが入れ子になり動作しない。
- **読み上げキュー**: 再生中に届いた返信を捨てないよう、ギルドIDごとに `asyncio.Queue`（`maxsize=TTS_QUEUE_MAX`）を持つ。積むのは **`(テキスト, ボイスプロファイル, 任意の有効性判定)` の組**で、プロファイルは `play_tts_response` の時点で `resolve_profile` により確定させる（待っている間にペルソナを切り替えられても、積まれた分は依頼時のキャラクターの声で読み上げる。新規に合成する分から新プロファイルが反映されれば十分）。ワーカーは最初の依頼で起動し、キューが空になったら `_speech_queues` から自分を消して終了する（参加していないサーバー分のタスクを常駐させないため）。**キューが空だと判定してから登録を消すまでの間に `await` を挟まないこと** — 挟むとその隙に積まれた依頼が誰にも処理されず取り残される。キューが溢れた分は捨てる（古い返信を延々と喋り続けるより会話の現在地に追従する方が自然なため）。
- **再生の完了待ち**: `voice_client.play()` の `after` コールバックは discord.py の**再生スレッド**から呼ばれる。イベントループ側のオブジェクト（`asyncio.Event`）へは必ず `loop.call_soon_threadsafe` 経由で触ること。
- **接続は毎回見直す**: 読み上げ中に `/leave` されうるため、ワーカーはループのたびに `guild.voice_client` を取り直して接続状態を確認する。
- **読み上げテキストの整形** (`build_speech_text`): `affinity_service.strip_speech_tags` で感情・親密度の内部タグを除去してから、URL・コードブロック・インラインコード・カスタム絵文字・メンション・Markdown 記号を除去し、`TTS_MAX_CHARS` を超える分は `TTS_TRUNCATION_NOTICE` を付けて打ち切る。特に **URL は音声エンジンが1文字ずつ読み上げて極端に長くなる**ため必ず除去する。通常チャット以外の入口にも備えて音声側でタグを除去し、タグだけならキューへ積まない。
- 調整値は `constants.py` の `TTS_*` に集約する（インラインに書かない）。

**キャラクター別ボイスプロファイル**: 「声の高さ（`pitch`）」と「話す速さ（`rate`）」は **edge-tts のサーバー側パラメータ**が、「音色（`eq`）」だけを `voice_dsp.py` の numpy 製バイクアッド EQ が担う。**`scipy` / `librosa` / `pydub` を持ち込まないこと** — 前2つは 1GB 環境に対して重すぎ、`pydub` は内部で FFmpeg プロセスを起動するため上記の方針と矛盾する。

- **どのキャラをどう鳴らすかは `constants.py` の `VOICE_PROFILES`**（キーは `current_prompt_file` の値、値は `voice` / `pitch` / `rate` / `eq`）。EQ の中身は `VOICE_EQ_PROFILES`。未登録のペルソナは `VOICE_PROFILE_FALLBACK` へ落ちる。調整はこの2つの辞書だけで完結させ、`voice_dsp.py` や `voice.py` にハードコードしないこと。
- **`pitch` / `rate` は edge-tts のネイティブ形式の文字列**（`"+10Hz"` / `"+5%"`。正規表現 `^[+-]\d+Hz$` / `^[+-]\d+%$` に一致しないと `Communicate` が `ValueError` を投げる）。**半音単位 `st` は使えない。**
- **「半音 → Hz」を計算で求めないこと**: 効き方が話者ごとに違い、Nanami は理論式の約1.5倍効き、Keita は約0.55倍しか効かないうえ **+6半音付近で飽和し、下方向はほとんど効かない**。`constants.py` に実測の較正表をコメントで置いてあるので、それを目安に Hz を直書きする。
- **`voice_dsp.py` に WSOLA・リサンプリングを戻さないこと**: 設計初期に gTTS の単一音声から高さと速さを作るため自前で持っていたが、edge-tts はピッチを変えても長さが変わらない（＝リサンプリングではなくフォルマントを保つ）ため不要になった。戻すと音質を落とすだけで得るものがない。
- **バイクアッドはブロック処理**: IIR は1つ前の**出力**を使う再帰式のため numpy でベクトル化できず Python のループになる。`TTS_BIQUAD_BLOCK_SIZE` ごとに区切ってフィルタの状態を持ち越す（一気に `tolist()` すると 30 秒の音声で数十 MB の一時オブジェクトができる）。
- **音色を変えないペルソナは numpy を通さない**: `needs_equalizer` が偽なら miniaudio に 48kHz ステレオまで変換させる。この分岐を消さないこと（`flat` のペルソナの経路が無駄に重くなる）。EQ を掛ける場合はモノラル1ch分だけ処理し、`np.repeat(clipped, TTS_CHANNELS)` でのステレオ化は最後に回す（計算量が半分で済む）。
- **int16 のクリップは必須**: EQ のブーストでレンジを超えるため、`np.clip` してから `astype(np.int16)` する（超えたまま変換すると音が割れる）。
- **プロファイルは起動時に点検する**: `voice.py` の `_validate_profiles()` が import 時に `pitch` / `rate` の形式を確認して警告を出す。形式ミスは `Communicate` の `ValueError` 経由で**黙って gTTS 退避に落ち続ける**（音は出るのでキャラ差が消えたことに気づきにくい）ため、この点検を外さないこと。
- **フェイルセーフ**: EQ 名が未知・`eq` キー自体が無い場合は `DEFAULT_VOICE_EQ_NAME` の音色へフォールバックする（既定は加工なしの `flat`）。数値でない周波数・ゲイン・Qも `TTS_EQ_*` の既定値で処理する。NaN・無限大も同じく既定値へ落とす（`_as_float` が `math.isfinite` で弾く。通すとフィルタ係数が NaN になり、int16 変換で雑音になる）。`bit_crush` は `_bit_crush_bits` が1以上のときだけ有効とし、`needs_equalizer` の判定と `apply_equalizer` の適用で同じ関数を使う（判定だけ `bool()` にすると `"0"` や `-1` で numpy の経路へ無駄に回る）。

### スラッシュコマンド (`commands.py`)

`/functions`・`/join`・`/leave` は副作用の前に `reject_blocked_interaction` でブラックリストを確認する。拒否は本人だけに通知する。パネルも `PanelView.interaction_check` で毎回確認し、開いた後のブロックを反映する。`FunctionsView` が渡す `blocked_user_check` を `swap_panel` で引き継ぐこと。サブパネルへ渡し忘れると開いたままの画面から操作できてしまう。

`/join` はボットのMemberを取得できた場合、接続先の `permissions_for` で「接続」と「発言」の両方を確認する。不足している権限だけを案内し、defer・自律配信の停止・既存接続の切断より前に終了する。

すべてのコマンドは `register_commands` で登録され、`on_ready` で同期される。全一覧: `/functions`（機能ハブ。エフェメラル）、`/join` + `/leave`（ボイスチャンネル）。**`/join` と `/leave` はどちらも先に `autonomous.stop_session()` を呼ぶ** — 自律行動でボットが自分から通話に入っている可能性があり、素の `disconnect` だけで済ませるとボットが開いたステージ枠が「配信中」の看板を出したまま残る。`/leave` での強制退出は自律行動の緊急停止手段でもある（idea.md の要件）。`/join` は配信を止めた後も同じチャンネルへ接続中なら、切り直さずにその接続を使う（切り直すと読み上げ中の音声とキューが失われる）。接続の待ち時間は `VOICE_CONNECT_TIMEOUT_SECONDS`。`/functions` はハブパネルを開き、ボタンで各機能へ分岐する: **⚙️ キャラ設定**（`SettingsView` に変身）、**📋 会話要約**（`SummaryView` に変身）、**📈 会話統計**（`StatsView` に変身。ユーザー別ダッシュボード。管理者はここから 👍 フィードバック集計も見られる）、**💬 自発会話**（`AutoTopicView` に変身。自発的な話題提供と 🎙️ 自律行動の ON/OFF・しきい値設定。「ボットが自分から話し出す」系の入口はここに統一している）、**⏰ リマインダー**（`ReminderView` に変身。本人の予定の一覧・取り消し）、**📊 グラフ**（利用トークングラフをその場で表示）、**🔄 NGワード再読込**、**🔍 Google検索**（グラウンディングのその場トグル。確定なしで即そのサーバーの `GuildSettings` に反映）、**❓ 使い方**（`HelpView` に変身。会話のしかたと全機能の説明。権限制限なし）、**🔒 統計プライバシー**（`StatsPrivacyView` に変身。本人の会話統計の収集停止・蓄積データ削除）、**🚫 ブロック解除**（**管理者専用**。ブロック中ユーザーの選択メニューから解除。管理者以外にはボタン自体を非表示）。旧コマンド（`/status`、`/summarize`、`/graph`、`/ng_reload`、`/unblock`、および `/ai`、`/set`、`/gemini`、`/setting`、`/reset`）は削除済み — 設定・要約・利用グラフ・NG再読込・ブロック解除は `/functions` ハブに、テキストチャットは mention モードの `on_message` パイプラインに統合された。

### キャラ設定 UI (`settings_view.py`, `commands.py`, `choices.py`)

`/functions` のハブ（`functions_view.py` の `FunctionsView`、エフェメラル）から **⚙️ キャラ設定** を選ぶと、同じメッセージが `SettingsView` に変身する（ペルソナ/モデル/応答モードのセレクト + 確定/戻る/リセットボタン、120秒タイムアウト）。一時的な選択値は View 上に保持され、**確定 (Confirm)** ボタンを押したときにのみ `GuildSettings`（`/functions` 実行時に `interaction.guild_id` から解決したもの）に書き込まれる — それまでは共有状態を一切変更しない。Google検索（グラウンディング）のトグルはハブ側にあり、こちらは押した瞬間に `GuildSettings` へ反映される（ハブは「その場で実行」系のため）。**各 View は `BotState` ではなく `GuildSettings` を受け取る** ため、設定変更の影響はそのサーバー内（DM の場合は全 DM）に閉じる。`choices.py` はペルソナ/モデル/モード、および要約期間（`SUMMARY_PERIOD_CHOICES`）の選択肢リストと表示名マップの唯一の情報源。

### 機能ハブ / 会話要約 (`functions_view.py`, `summary_view.py`, `stats_view.py`)

サブパネルへの遷移とハブ復帰では、古いViewを停止して `await interaction.original_response()` の `InteractionMessage` を新しいViewへ渡す。通常の `interaction.message` を保持すると、エフェメラルUIのタイムアウト編集がチャンネル経由になり404で失敗する。

`/functions` はエフェメラルな `FunctionsView`（ハブ）を開く。Discord の View は最大5行のため設定と要約を1枚に合体できず、ボタンで選んだ機能のパネルへ**同じメッセージを差し替える**方式で統合している。⚙️キャラ設定→`SettingsView`、📋会話要約→`SummaryView`、📈会話統計→`StatsView`、💬自発会話→`AutoTopicView`、⏰リマインダー→`ReminderView`、❓使い方→`HelpView`、🔒統計プライバシー→`StatsPrivacyView`、🚫ブロック解除→`UnblockView`（管理者のみボタン表示）はメッセージを差し替え、📊グラフ・🔄NG再読込は別のエフェメラルメッセージで結果を返す。**ハブの5行（row 0〜4）は使い切っている**ので、以降の機能追加はハブではなくサブパネル側へ置くこと（row 3 = ❓使い方 / row 4 = 🔒統計プライバシー）。差し替えは `view_parts.swap_panel()` を使う（ハブの `stop()` 忘れを防ぐため。新規パネルは `ui.View` 直継承ではなく `PanelView` を継承する）。🔍Google検索はハブ上のトグルで、押した瞬間にそのサーバーの `GuildSettings.grounding_enabled` を反転して保存する。各サブパネルは共通の **↩️ 戻る** ボタン（`view_parts.py` の `BackButton`）を持ち、`make_hub`（`FunctionsView.clone` を渡す）でハブを再生成して同じメッセージを戻す — `view_parts.py` が `functions_view.py` を import すると循環参照になるため、生成関数を渡す形にしている。**会話要約**は期間（1時間〜1か月）と表示範囲（自分だけ/全員）を選んで実行する。`generate_channel_summary` が対象期間のメッセージを `fetch_messages_since` で取得（`SUMMARY_MAX_MESSAGES` 件超過で自動停止）、`build_summary_source_text` で**発言者名つき**に整形し、`build_summary_prompt`（`prompting.py`）＋ **そのサーバーの `GuildSettings`** の選択モデル/ペルソナで議事録を生成する。生成結果は感情タグ除去・Markdown見出し除去・`screen_bot_response` を経て、表示範囲に応じてチャンネル投稿またはエフェメラル表示される。`RunSummaryButton` は生成から送信までを `try` で囲み、パネルを「要約中...」のまま残さない。全員表示の投稿の失敗は、権限不足（`Forbidden`。権限設定の確認か「自分だけ」での表示を案内）と途中での失敗（`HTTPException`。一部が投稿されている可能性を案内）を個別に伝え、それ以外の想定外の例外も定型の案内へ差し替える（例外の文面は内部の情報を含みうるので見せない）。「自分だけ」で1通目を表示済みなら、それを上書きせず followup で伝える。Gemini の通信エラー・タイムアウトは `GeminiService.generate` が `None` に変えるため、`generate_channel_summary` が案内文を返す。

### 自発的な会話 (`auto_topic_service.py`, `auto_topic_view.py`)

自動投稿は生成前に `_can_send` でチャンネル単位の閲覧・送信権限を確認する。スレッドでは `send_messages_in_threads` を使い、DM・ボットのMember未取得時も生成を見送る。事前の権限不足は記録せず、復旧後の見回りで再試行する。生成中の権限変更による `Forbidden` は話題提供の処理済み時刻を記録し、次に人間が発言するまでAPIの再実行を抑える。話題提供は `AllowedMentions.none()`、記念日は対象Memberだけを許可し、全体・ロール・他ユーザーへの通知を抑止する。

一定時間会話が無いテキストチャンネルへ、過去の話題をもとにボットから話題提供する機能。デフォルトは**無効**で、`/functions` → **💬 自発会話** の `AutoTopicView`（間隔セレクト + ON/OFF トグル2つ + 確定/戻る。確定まで `GuildSettings` を変更しない）から有効化する。**設定・監視状態はサーバーごとに独立している。**しきい値は `AUTO_TOPIC_INTERVAL_CHOICES`（1/3/6/12/24時間、`choices.py`）から選ぶ。

**`AutoTopicView` は自律行動（次項）の ON/OFF も兼ねる。** 「ボットが自分から話し出す」系の入口をここ1枚に統一しているため、トグルは 💬 自発的な会話 と 🎙️ 自律行動 の2つある（入口を分けると、どちらを切ったのか分からなくなる。ハブの row 0〜4 が埋まっていて新しいボタンを足せないという事情もある）。しきい値も両者で共用する（自発会話では「何時間黙っていたら話題を投げるか」、自律行動では「何時間で話したいゲージが 100% になるか」）。

- **監視対象**: ボットが会話に参加した（`on_message` パイプラインを通過した）ギルドチャンネルが `watch_channel` で自動登録される。**DM は対象外**。最終活動時刻は、メンション判定より**前**に呼ばれる `mark_activity` により「ボット宛てでない人間の発言」でも更新される（会話が活発なチャンネルに割り込まないため）。
- **発火条件**: `auto_topic_loop`（`AUTO_TOPIC_CHECK_MINUTES` ごとの見回り）が「無会話時間 ≥ しきい値」かつ「前回の自動投稿より後に人間の発言がある」チャンネルにのみ投稿する。つまり一度話題提供したら、誰かが発言するまで同じチャンネルには再投稿しない（沈黙中の連投防止）。生成失敗時は記録を残さず、次回の見回りで自然にリトライされる。
- **話題生成**: 現在のペルソナ + チャンネル履歴（`AUTO_TOPIC_HISTORY_LIMIT` 件）から `build_auto_topic_prompt`（`prompting.py`）で組み立て、グラウンディング無効で生成する。**長期記憶（`profile`）は使わない** — 記憶はユーザー別に分離されており、特定ユーザーの個人記憶を共有チャンネルへの話題提供に使うとその人の私的情報が他の参加者へ漏れるため（プライバシー保護）。材料は全員が既に見ているチャンネル履歴だけにする。感情タグは除去のみ（相手が特定できないため親密度は更新しない）。NG ワードマスクは適用するが、モデレーションで不適切と判定された場合は謝罪文に差し替えず**投稿自体を見送る**（唐突な謝罪の投稿を防ぐ）。
- **状態の保存範囲**: 有効/無効としきい値は DB（`guild_settings` テーブル）に永続化され再起動後も維持される。監視チャンネル（`auto_topic_channels`）と投稿記録（`auto_topic_posted_at`）はインメモリのみで、再起動後はそのチャンネルでボットが再び会話に参加した時点から監視が始まる。
- **自律行動との競合抑止**: `_run_pending_checks_for_guild` は、そのサーバーで自律行動の配信が進行中（`GuildSettings.autonomous_session is not None`）なら**何もせずに戻る**。両方が有効なサーバーでは、しきい値を共用する都合で「無会話が続いた瞬間」に両方が同時に発火しうる。ボイスで独り言を始めながらテキストにも話しかけると、どちらに応えればよいのか分からずうるさいだけになるため、既に自分から喋っている間は話題提供の側を黙らせる。この判定を外さないこと。

### 自律行動（ボイス/ステージでの「配信」） (`autonomous_service.py`, `autonomous_metrics.py`, `autonomous_stage.py`, `auto_topic_view.py`)

`AutonomousService` は見回り・コメント処理・発話・停止の統括を担当する。ゲージ・活動窓・深夜判定・開始失敗の待ち時間はDiscordに依存しない `autonomous_metrics.py`、チャンネル選択・発言権・所有ステージ枠の終了・接続の切断は `autonomous_stage.py` に分離している。`compute_talk_desire` / `describe_quiet_hours` は既存のパネルとの互換性のためサービスからも再公開する。終了処理のタスク生成とキャンセル伝播はサービス、枠IDと接続の参照照合はステージヘルパーの責務とする。

開始待機はサービス内の `_starting` にサーバーごとの `_StartAttempt` として保持する。接続やステージ準備を待つ間の `stop_session` はこの要求も中止にし、セッションが未登録でも `True` を返す。`/leave` は接続の有無にかかわらず停止処理を呼ぶ。開始処理は接続後と準備後に、停止要求・有効設定・静音時間・元のVoiceClientとの一致・接続先を再確認し、成立しなければ所有枠と元の接続を片付ける。待機が完了するまでは同じサーバーの二重開始を抑止し、`finally` で開始要求を破棄する。この要求は永続化しない。

第一声・独り言・コメント応答の履歴取得には、通常チャットと同じ `guild_settings.channel_reset_points.get(channel.id)` を渡す。自律配信だけリセット前の文脈を読み続けないよう、3経路を揃える。

**ボットが自分の意志でボイス/ステージチャンネルへ入り、独り言を続けながら、聞きに来た人のコメントへ配信者のように応答する機能。** 自発的な会話が「テキストへ話題を1つ投稿する」だけなのに対し、こちらは通話に居座って会話の場そのものを作る。デフォルトは**無効**で、`/functions` → **💬 自発会話** の 🎙️ 自律行動 トグルから有効化する（前項参照）。ver0.1.7(BETA) の機能。この節で「idea.md」と書いているのは、この機能の要件をまとめた利用者のメモのこと。**リポジトリには含まれていない**ので探さなくてよい（参照している要件は、この節にすべて書き写してある）。

- **話したいゲージ**（`compute_talk_desire`。モジュール関数なのでパネル表示からも呼べる）: 0〜100。基礎値は無会話時間に比例し、`auto_topic_interval_hours` で 100 に届く。そこへ「直近 `AUTONOMOUS_ACTIVITY_WINDOW_MINUTES` の人間の発言数 × `AUTONOMOUS_ACTIVITY_BONUS_PER_MESSAGE`」を `AUTONOMOUS_ACTIVITY_BONUS_MAX` を上限に加点する（人が集まっているサーバーほど早く話したくなる）。**人間の発言をまだ一度も観測していないサーバーでは 0 を返す** — 招待されただけのサーバーや再起動直後に、いきなりボイスへ入り込まないための歯止め。観測は `on_message` から呼ばれる `mark_activity` が行う。
- **【重要】開始に失敗したら必ず `_record_start_failure` を通すこと**: ゲージは開始に失敗しても下がらない（時間が経つほど増えるだけ）ため、記録しないと次の見回り＝1分後に同じ判定が通り、**接続→失敗→切断を永久に繰り返す**（権限設定が中途半端なサーバーが1つあるだけで1日1440回）。待ち時間は `AUTONOMOUS_START_RETRY_COOLDOWN_MINUTES × 連続失敗回数`（`AUTONOMOUS_START_RETRY_MAX_MULTIPLIER` 倍で頭打ち。既定 30分〜最長4時間）。**ゲージ自体はリセットしない** — リセットすると権限を直したあともしきい値時間ぶん待たされる。代わりに `/functions` のパネルで確定するとこの待ち時間が即座に解除される。開始に成功したら `autonomous_start_failures` / `autonomous_start_retry_at` を消すこと。行き先が無い（`autonomous_stage.pick_channel` が None）場合だけは API を1回も叩いていないので待ち時間を入れない。
- **発火と行き先**: `autonomous_loop`（`AUTONOMOUS_CHECK_MINUTES` = 1分ごと）がゲージ ≥ `AUTONOMOUS_GAUGE_THRESHOLD` のサーバーで `_start_session` を呼ぶ。行き先は**ステージ優先**で、発言者になれるステージが無ければボイスチャンネルから選ぶ（候補が複数あれば `random.choice`）。権限は必ず `permissions_for(guild.me)` で**チャンネル単位に**確認する（`/join` と同じ作法）。ステージは「参加しただけでは観客（suppress=True）で音声を流せない」ため `mute_members` が必須、ステージ枠が無ければ自分で開くので `manage_channels` / `move_members` も要る。条件を満たさないチャンネルは候補から外す（入っても喋れないため）。
- **ステージ枠**: `create_instance` のトピックは `AUTONOMOUS_STAGE_TOPIC`（「会話相手募集中…」）。チャンネル一覧に出るので、これが人を誘う看板になる。**`send_start_notification=True` にしないこと** — サーバー全体へ @everyone 相当の通知が飛び、ボットが配信を始めるたびに通知爆撃になる。**自分で開いた枠だけを終了時に閉じる**（`stage_started_by_bot` と作成応答から得た `stage_instance_id`）。枠のIDが一致しなければ削除しない。他人が開いた枠に相乗りした場合は閉じない。
- **発話**: 第一声（`build_autonomous_opening_prompt`）→ `AUTONOMOUS_TALK_INTERVAL_MINUTES` ごとの独り言（`build_autonomous_monologue_prompt`）。どちらもチャンネル付属のチャットへ投稿し、同じ本文を `voice.speak_text` で読み上げる。第一声・独り言・コメント応答は開始時の `snapshot_response()` をプロンプト読込・生成・TTSで共用し、途中の設定変更は次の応答から反映する。投稿直前にセッションを照合し、投稿後の音声投入直前には `can_speak_session` で同じセッション・接続中・同じチャンネルを確認する。キューと合成の待機後も音声側で照合する。コメント応答では `MessageHandler` がスナップショットを渡し、返された本文を同じ条件で読み上げる。生成が失敗しても `last_spoke_at` は `finally` で必ず進める（進めないと、生成が壊れている間ずっと毎分 API を叩き続ける）。第一声は生成に失敗しても `AUTONOMOUS_OPENING_NOTICE`（募集の看板）だけは必ず投稿する。
- **開始準備のキャンセル**: `prepare_stage` のボイス状態待機・発言者への変更がキャンセルされた場合も、自分で開いた枠だけを削除し、`_start_session` が開始時のVoiceClient参照と照合して切断する。後片付けは `background.wait_for_cleanup` の専用タスクで完了まで待ち、繰り返しのキャンセルを受けても終了後に伝播する。枠削除の例外で切断を省略せず、借りた枠・差し替わった枠や接続には触らない。
- **【重要】自発発言に AI モデレーションは掛けない**: 独り言・第一声は `_screen_speech()` で感情タグ除去と NG ワードのマスクだけを通す。これは idea.md の明示的な方針（発言のたびに Gemini をもう1回呼ぶコストを避ける）で、`auto_topic_service._screen_auto_post` とは扱いが違う。**一方、コメントへの応答は「ユーザーからのインプットへの応答」なので従来どおり `screen_bot_response` で NG マスクと AI モデレーションを通す。** この非対称を崩さないこと。
- **コメントの受け付け範囲**: `is_comment_channel` が真になるのは**配信中のそのチャンネル**だけ。`MessageHandler.on_message` はこのときメンション判定を飛ばして `handle_comment` へ回す（傍観者は配信のチャットに気軽に書き込むもので、メンションを求めるのは配信の体裁に合わない）。他のテキストチャンネルからの発言はコメントとして認めない。ブラックリスト・NG ワード・入力モデレーションの関門は従来どおり通る。配信中のチャンネルは `auto_topic.watch_channel` に**登録しない**（登録すると配信終了後もそこがテキスト話題提供の対象として残り続ける）。
- **【重要】コメントには入力モデレーション前の流量制限（`claim_comment_slot`）が要る**: メンション不要で拾う以上、何もしないと「そのチャンネルの全メッセージ ×（モデレーション＋応答生成）」で Gemini 呼び出しが発散する（通常のメンション必須モードでは、メンションが自然な流量制限になっていた）。基準は返信完了ではなく直前の受付時刻で、配信単位のため同じユーザーの連投も制限する。クールダウン未満のコメントは入力AI・生成・出力AIを省略し、見送った入力のAI違反は記録しない。AIで拒否した受付も枠を保持し、受付前のローカルNGだけで拒否した入力は枠を消費しない。活動観測とローカルNG判定は維持する。**判定と時刻の記録を1メソッドにまとめてあるのは間に `await` を挟ませないため**（挟むと同時に届いた2件が両方通る）。基準に `last_spoke_at` を流用しないこと — 独り言でも更新されるため、**いちばん拾いたい「独り言に反応した最初のコメント」を捨ててしまう**。専用の `session.last_comment_reply_at` を使う。`MessageHandler` は入力モデレーション前に確保したセッションを `handle_comment(..., claimed_session=...)` へ渡し、同一セッションなら二重に枠を取らない。配信が入れ替わった場合、その予約は使わない。予約なしの直接呼び出しだけは内部の `_claim_comment_slot` で判定する。
- **コメント応答が通常チャットと違う点**: 長期記憶（`profile`/`projects`）を渡さず、親密度を更新せず、👍👎 の評価対象にも登録しない。記憶を渡さないのは、共有の場である配信で特定ユーザーの私的な記憶を読み上げると本人以外へ漏れるため（自発的な会話・記念日と同じ方針）。親密度と評価を外すのは、リスナーが「傍観者」であって1対1の会話相手ではないという idea.md の位置づけによる。**画像は通常チャットと同じく扱う** — 本文が空で画像だけのコメントは `AUTONOMOUS_IMAGE_ONLY_COMMENT` を本文の代わりに置き、`load_image_attachments` の結果を `generate` へ渡す（置き換えないと「空のコメントへの返事」を生成し、配信中だけマルチモーダルが死ぬ）。
- **配信中かどうかは、モデレーションの後にもう一度確かめる**: `on_message` の入口の判定と `handle_comment` を呼ぶ地点の間には Gemini 呼び出し（モデレーション）が挟まるため、その間に配信が終わりうる。`MessageHandler` は routing で `is_comment_channel` を取り直し、配信が終わっていて**かつ通常経路の条件（DM / 全発言モード / メンションあり）を満たすなら `handle_chat_message` へ流す**。古い判定のまま `handle_comment` を呼ぶと、セッションが無いので None が返り、メンション付きの発言まで黙って捨てられる。`handle_comment` 側も送信の直前に `_is_current_session` で確かめる（終わった配信へ後から応答を投稿しないため）。
- **`mark_activity` / `is_comment_channel` の引数順は `(channel_id, guild_id)`**（`AutoTopicService.mark_activity` / `watch_channel` と同じ）。同じ `on_message` から並べて呼ぶメソッドで順番が違うと、取り違えても型が同じ（int）ため静かに誤動作する。順番を変えないこと。
- **停止条件**（見回りが毎分確認する）: ①コメントが `AUTONOMOUS_IDLE_STOP_MINUTES`（30分）途絶えた ②深夜の時間帯制限に入った ③`AUTONOMOUS_SESSION_MAX_MINUTES` を超えた ④パネルで無効化された ⑤**別のチャンネルへ移動させられた** ⑥ボイス接続が外部から切れた。接続喪失時も `stop_session` へ進め、所有ステージ枠・ボイス接続・記録をまとめて片付ける。サーバーを参照できない場合だけは記録を片付ける。①〜⑤は締めの一言を投稿してから退出する（④の文言も省かないこと — パネルの確認文はエフェメラルで操作した本人にしか見えず、聞いていた人からはボットが黙って消えたようにしか見えない）。`/leave` と `/join` からも `stop_session` を呼ぶ（`/leave` での強制退出は idea.md の要件）。**`stop_session` は例外を外へ出さない** — 締めの投稿やステージ枠の後始末が失敗しても、切断とセッション記録の破棄までは必ず進める（片付けが途中で止まると接続だけが残って誰も止められなくなる）。そのため後片付けは `discord.HTTPException` だけでなく**想定外の例外もログにして飲み込む**。終了案内や後片付けの途中でキャンセルされても、専用async taskで所有枠と接続の終了を完了してからキャンセルを伝播する。
- **停止中の接続差し替え**: 停止開始時のVoiceClient参照だけを切断対象とし、待機中に `/join` や新配信へ差し替わった接続は切らない。同じVoiceClientが自動再接続した場合はforce切断する。枠キャッシュが無いときは取得し、所有IDを照合して閉じる。
- **チャンネル移動は「追従」ではなく「終了」**: 管理者にドラッグで別のチャンネルへ移されると接続は生きたままなので、接続の有無だけでは気づけない。`_tend_session` は `voice_client.channel.id` と `session.channel_id` を突き合わせ、食い違ったら配信を畳む。追従しないのは、セッションが開いたステージ枠を `channel_id` で覚えているため、行き先を差し替えると元の枠を閉じられなくなるから。
- **セッションの取り残しを防ぐ**: `_check_guild` は `guild` が引けないとき（サーバーから追い出された・キャッシュ未取得）も、セッション記録が残っていれば片付ける。残すと `is_comment_channel` が真を返し続け、さらに自発会話の競合抑止も効いたままになって**そのサーバーの話題提供が二度と動かなくなる**。
- **時間帯制限**: `AUTONOMOUS_QUIET_HOURS_START`〜`END`（既定 00:00〜05:59、`AUTONOMOUS_TIMEZONE` 基準）は開始せず、配信中でも退出する。タイムゾーンは**モジュール読み込み時に1回だけ**解決し（`autonomous_metrics.QUIET_HOURS_TZ`）、サービスもパネル表示も同じものを使う（`resolve_timezone` は失敗時に警告を出すため、パネルを開くたびに呼ぶとログが溢れる）。**画面へ出す時間帯は `describe_quiet_hours()` で作ること** — 定数から直接組み立てると、tzdata の無い環境で UTC へフォールバックしたときに「実際は UTC の 00:00〜05:59（＝日本時間の 09:00〜14:59）なのに Asia/Tokyo と名乗る」食い違いが起きる。ゲージは無会話時間で増えるため、誰も居ない深夜ほど早く満タンになる — 寝ている間にボイスチャンネルで喋り続けないための歯止め（idea.md の制限）。開始 > 終了（例: 22〜6）の日またぎ指定にも対応している。
- **接続の唯一の情報源は `guild.voice_client`**: `AutonomousSession` に VoiceClient を持たせないこと。持たせると、管理者にキックされた・回線が切れたときに実態と食い違った参照が残り、切断済みのクライアントへ喋り続ける。**`/join` で人が呼んだ接続があるサーバーでは配信を始めない**（読み上げ用に呼ばれている最中かもしれないため）。
- **多重発話の防止**: 生成と読み上げは数十秒かかるため見回りループの中で待たず `background.spawn` で裏へ逃がす。二重起動は `session.busy` で防ぐ（`_tend_session` が spawn の**前**に立て、`_speak_monologue` が `finally` で下ろす）。`handle_comment` は生成に入る前にも `last_spoke_at` を進め、応答を作っている間に独り言がぶつからないようにする。生成を待っている間に配信が終わっていることがあるため、送信の直前に `_is_current_session` で確かめる。
- **状態の保存範囲**: 永続化するのは有効/無効（`guild_settings.autonomous_enabled`、スキーマ バージョン9）だけ。セッション・ゲージ・活動記録・開始失敗の待ち時間はインメモリのみ（ボイス接続自体が再起動をまたげないため、復元しても意味がない）。
- 調整値は `constants.py` の `AUTONOMOUS_*` に集約する。

### 記念日・誕生日の自動お祝い (`auto_topic_service.py`, `memory_service.py`)

会話の中で出てきた誕生日・記念日を長期記憶に覚えておき、当日にボットからお祝いを投稿する機能。専用の UI は無く、記憶と自発会話の枠組みに相乗りしている。

- **記憶側**: 記憶圧縮（`memory_service._compact_memory`）のプロンプトが、月日が確定できる記念日を `profile` の `ANNIVERSARY_PROFILE_KEY`（= `"anniversaries"`）キーへ `[{"label": "誕生日", "month": 5, "day": 1}]` の構造化リストで保存する。既存エントリはマージ（同じ `label` は上書き）し、単発の予定は保存しない。ユーザーが `memory/profile.json` に手書きしても登録できる。
- **お祝い側**: `run_anniversary_checks()` が `ANNIVERSARY_CHECK_MINUTES`（既定60分）ごとの `anniversary_loop`（`bot.py`）から呼ばれる。`Database.profile_load_all_for_key()` で全ユーザーの記念日を一括取得し、`ANNIVERSARY_TIMEZONE`（`Asia/Tokyo`）基準の「今日」と月日が一致するユーザーを、そのユーザーが所属する各サーバーの**自発会話の監視チャンネル**でお祝いする。投稿先は最終活動が新しい順に試し、送信できる最初のチャンネルを使う（`_pick_anniversary_channel`。削除済みのチャンネルは監視から外す。この選別では Gemini を呼ばない）。送信できるチャンネルが無ければ記録せず、権限が戻れば同日中の見回りで投稿する。生成後に送信を拒否された（`discord.Forbidden`）場合は、自発的な会話と同じくその日を処理済みとして記録し、毎時の再生成を止める（生成文はそのチャンネルの履歴から作ったため、別のチャンネルへ流用しない）。プロンプトは `build_anniversary_prompt`（`prompting.py`）。
- **二重投稿の防止**: `GuildSettings.anniversary_posted_dates`（`"{user_id}:{label}"` → `"YYYY-MM-DD"`）で同一サーバー・同一記念日の同日再投稿を防ぐ。記録は `record_anniversary_post()` がメモリと `anniversary_posts` テーブルの両方へ書き、起動時に復元されるため**当日に再起動しても再投稿しない**。主キーが `(guild_id, user_id, label)` の upsert なので毎年お祝いしても行は増えない。
- **フェイルセーフ**: `anniversaries` は LLM が書き込む自由記述寄りの記憶のため、型や形式が期待どおりでないエントリは静かにスキップする（例外で見回りを止めない）。
- **プライバシーと安全性**: 自発的な会話と同じ方針。材料はチャンネル履歴のみで**個人の `profile` は渡さない**（「今日が記念日」以上の私的情報を共有チャンネルへ出さない）。DM は対象外。NG ワードはマスクし、モデレーションで不適切と判定されたら謝罪文に差し替えず**投稿自体を見送る**。

### リマインダー (`reminder_service.py`, `reminder_view.py`)

会話から登録し、指定時刻に**依頼されたチャンネル**でメンション付きに通知する。専用コマンドは無い。

- **登録経路**: `content_moderator.check_and_route` がモデレーションと同じ1回の呼び出しで日時＋本文＋**繰り返し種別**（`repeat`）を抽出する（**リマインダーのために Gemini を追加で呼ばないこと** — 判定を足すならこのプロンプトに相乗りさせる）。相対表現を解釈させるため、プロンプトには現在日時（曜日つき、`REMINDER_TIMEZONE` 基準）を埋め込む。`_build_combined_system_prompt` の JSON 出力例は3つ（安全時・リマインダー時・不適切時）あるので、形式を変えるときは**全部そろえて**直すこと（1つだけ直すとモデルが形式を揺らす）。
- **検証は `ReminderService` 側**: 日時の解釈（複数フォーマットを順に試す）、過去・`REMINDER_MAX_DAYS_AHEAD` より先の拒否、`REMINDER_MAX_PENDING_PER_USER` の上限、本文の切り詰め。繰り返しでも判定するのは**初回の日時**。**登録を見送るときは `None` を返す**（呼び出し側は通常の会話へ流す）。上限超過だけは意図が明確なので案内文を返す。
- **通知は生成しない**: 正確さが最優先なので Gemini を呼ばず、`⏰ @ユーザー リマインダーです: 本文` のテンプレートで組み立てる（繰り返しは `🔁 次回は …` を添える）。NG ワードのマスクだけ適用する（登録時にモデレーション済みのため再判定しない）。繰り返しは同じ本文が定期的に公開チャンネルへ出るため、マスクは**毎回**通すこと。通知を飛ばしてよいのは依頼者本人だけなので、`allowed_mentions` で本人のユーザーだけを許可する（本文の `@everyone`・`@here`・ロール・他人へのメンションは AI モデレーションを通過するため、送信側で止める。記念日のお祝いと同じ制御）。
- **二重通知の防止**: `Database.reminder_claim_due()` が SELECT と UPDATE を同一トランザクションで行い、期限到来分をクレームしてから返す（`queue_tasks` と同じクレーム方式）。送信自体が失敗しても `pending` に戻さない — 重複通知より「時間外れの再通知」の方が困るため。
- **繰り返し（`none`/`daily`/`weekly`/`monthly`）**: 新しい行を作らず、**同じ行の `remind_at` を次回時刻へ進めて `pending` のまま残す**（`reminder_id` が変わらないのでパネルからの取り消しが直感どおりに効き、`done` 行で保持枠を食い潰さない）。単発は従来どおり `done`。次回時刻の計算は `reminder_service.next_occurrence()`（暦だけを扱う純粋関数）が担い、`reminder_claim_due` へ**同期関数として渡してクレームと同一トランザクションで実行する** — 2ステップに分けると間でクラッシュしたときに繰り返しが失われる。
  - **渡す計算関数から `Database` のメソッドを呼ばないこと**（`self._lock` は再入不可の `threading.Lock`。同じスレッドから取り直すとデッドロックする）。`async def` にするのも不可（`await` されずコルーチンが返る）。
  - **停止明けは「未来になるまで反復」して追い付く**。1回分だけ進めると追い付くまで見回り（60秒）のたびに通知が飛ぶ。歯止めは `REMINDER_RECURRENCE_MAX_CATCHUP_STEPS`（到達したら `canceled` にして止める）。次回時刻を作れなかった繰り返しは、`None` が返った場合も計算が例外で落ちた場合も `canceled` にする（例外だけ `done` にすると正常に完了したように見え、途絶えた原因を追えない）。単発は常に `done`。過ぎた回はまとめて通知しない（通知は最大1回、時刻は未来へ）。
  - `_is_stale` が見るのは**進める前の（本来の）予定時刻**。そのためクレームが返すレコードの `remind_at` は進める前の値のままにし、進めた後の時刻は `next_remind_at` に入れて返す。stale で今回を見送っても次回時刻は計算済みなので**系列は途切れない**（長期停止明けは翌回の定刻から再開する）。
  - **月次は `recurrence_anchor`（初回時刻）を固定**したまま `calendar.monthrange` で末日へ丸める（`timedelta(days=30)` は使わない）。アンカーを毎回更新すると 1/31→2/28→**3/28** とずれる。正しくは 1/31→2/28→3/31。
  - 日時の加算は `REMINDER_TIMEZONE` のローカル時刻で行ってから UTC へ戻す（DST のあるタイムゾーンへ変えても壊れないため）。許容値・表示名・追い付き上限は `constants.py` の `REMINDER_RECURRENCE_*` に集約する。
- **時刻の扱い**: 保存は UTC の ISO 8601 で**秒精度に統一**する（期限判定を文字列比較で行うため、桁数が混ざると比較が壊れる）。表示だけ `REMINDER_TIMEZONE` に変換する（`_format_local`）。日時の書式に `strftime` の `%-m` / `%#m` は使わないこと（プラットフォーム依存）。
- **パネルの文字数**: 一覧はヘッダー・注記・改行を含め `REMINDER_PANEL_MAX_LENGTH` 以内に収め、入りきらない行は「…ほか N 件」と表示する。省略は本文だけに適用し、通常の上限20件すべてを取り消し用セレクトに残す。セレクトの `label` / `description` は `reminder_view._to_option_text` で改行・連続空白を半角空白1つにまとめてから切り詰める（本文は会話から登録されるため改行を含みうる。改行入りの選択肢は Discord に拒否され、パネルごと開けなくなる）。
- **権限**: `/functions` → **⏰ リマインダー** で一覧・取り消し。表示は本人の分だけで、`reminder_cancel` は SQL の条件にも `user_id` を含めるため他人のものは操作できない。

### ユーザー別会話統計 (`stats_service.py`, `stats_view.py`, `stats_privacy_view.py`)

`RunStatsButton` の説明文は `split_message` で分割し、最初を元パネル、続きを本人だけのfollowupへ送る。画像のFile・バッファは引き続き `finally` で回収する。`FeedbackButton` は権限・サーバー・サービスの確認後、集計より前に本人限定の応答をdeferし、結果を分割してfollowupへ送る。集計後も管理者権限を再確認し、失敗は本人だけへ案内する。

`RunStatsButton` は生成とFile作成も `try` の対象にし、生成失敗時は「集計中」の元パネルをエラー表示へ更新する。成功・失敗・キャンセルのいずれでも `discord.File.close()` の後にバッファを閉じ、Fileの作成途中で失敗した場合も作成済みの分を回収する。他人の統計を表示する場合は生成前と送信直前に管理者権限を再確認し、対象ユーザー選択とブロック解除のSelectも操作時に再確認する。本人の統計は引き続き管理者権限なしで表示できる。

`/functions` の **📈 会話統計** から開く、ユーザー別の会話ダッシュボード。**すべてエフェメラル（非公開）で表示される。** 集計対象は「ボットへの発言のみ」で、`on_message` パイプラインの添付の振り分けの後（リマインダー判定の前。配信中のコメントも含む）に `stats.record(...)` を fire-and-forget で呼んで蓄積する（失敗してもチャット本体は止めないフェイルオープン）。データは DB の `user_stats` テーブルに永続化され、`asyncio.Lock` で保存を直列化する（単一プロセス前提）。

- **集計データ**: ユーザーごとに累計発言回数・時間帯別ヒストグラム（`hourly[24]`、`STATS_TIMEZONE`＝`Asia/Tokyo` の「時」に変換）・直近発言サンプル（`STATS_RECENT_SAMPLES_MAX` 件のリングバッファ）を記録する。
- **話題・性格の LLM 解析** (`ensure_analysis`): 直近サンプルを Gemini に渡し、「よく話す話題」と「ビッグファイブ性格特性」を JSON で推定する。結果は `STATS_ANALYSIS_TTL_HOURS` の間キャッシュし、TTL 内なら再計算しない。LLM 呼び出し・パース失敗時は既存キャッシュにフォールバックする。LLM の出力は保存前に型と有限値へ正規化する（`normalize_topics` / `normalize_big_five`）。描画関数も同じ関数と `normalize_hourly` を通すこと — 以前の版が型を確かめずに保存したキャッシュが DB に残っていても、描画が例外で落ち続けないようにするため。性格推定は `STATS_PERSONALITY_MIN_SAMPLES` 件未満のとき「データ不足」として描画をスキップし、性格推定だけを見るときは解析自体を呼ばない。件数不足で解析した結果には性格推定を保存せず（`sample_count` を記録する）、件数が揃ったら TTL 内でも1回だけ解析し直す（`_lacks_personality`）。
- **項目別グラフ** (matplotlib、`usage_graph.py` と同じ日本語フォント作法): 時間帯＝棒グラフ / 話題＝重み付き横棒 / 性格＝ビッグファイブのレーダーチャート。項目セレクトで「全部 / 時間帯 / 話題 / 性格推定」を切り替えられる。
- **権限**: 一般ユーザーは**自分の統計のみ**閲覧できる（対象は `viewer_id` に固定）。**管理者のみ**、ユーザー選択メニューで他メンバーの統計を見られ、発言回数ランキング（全体比較）ボタンも使える。権限判定は `interaction.user.guild_permissions.administrator` によるサーバー側判定で、`RankingButton` 側でも二重にチェックする。
- **再起動でリセットされない**: DB の `user_stats` テーブルに永続化される。調整値（TTL・上限・タイムアウト・タイムゾーン）は `constants.py` の `STATS_*` に集約。
- **収集の停止（opt-out）** (`stats_privacy_view.py`): 本人が `/functions` → **🔒 統計プライバシー** から統計収集そのものを止められる。設定はユーザー単位で**全サーバー横断**（統計データ自体が `user_id` 単位でサーバーをまたぐため）、`user_stats.stats_opt_out` 列（スキーマ バージョン8）に永続化される。既定値は `STATS_OPT_OUT_DEFAULT`（`False` ＝従来どおり収集する。`True` にすると「原則収集しない」オプトイン運用になる）。
  - **判定は `StatsService` の内部で行う**（`record()` の冒頭＝エントリ生成より前、と `ensure_analysis()` の入口）。呼び出し側（`MessageHandler`）ではチェックしない — `record` の呼び出し箇所が増えたときのチェック漏れを防ぎ、fire-and-forget の主経路に `await` を足さないため。判定は `is_opted_out()` の**キャッシュ参照のみ**で DB へは行かない。
  - **通常更新は `Database.stats_save_user` による対象ユーザー1行の UPSERT**。発言・解析・収集設定・削除は `_save_lock` でキャッシュ更新から保存まで直列化し、対象の入れ子の値だけをコピーする。`stats_save_all` は明示的な全件置換専用で、通常更新には使わない。`user_stats` に列を足すときは `stats_load_all` と `_stats_upsert_user` の全列を揃え、`stats_opt_out` を含む「保存→読み込み→保存」の往復を実測すること。
  - **停止と削除は別操作**: 停止（opt-out）は以降の収集を止めるだけで既存データは残る。🗑️ 蓄積データを削除（2回押しの確認つき）が集計値・発言サンプル・解析キャッシュを消す。**削除で行ごと消さないこと**（`del cache[user_id]` は禁止 — opt-out の設定まで消える）。
  - **他人からは見えない**: opt-out 中のユーザーは管理者のユーザー選択（`get_user_options`）と発言回数ランキング（`build_ranking_graph`）から除外される。他人が opt-out 中のユーザーを指定した場合は、既存の「◯◯ さんの会話データはまだありません。」と**同一の文言**を返す（専用の文言を出すと停止していること自体が露見する）。明示的な注記 `🔕 現在、統計収集を停止しています。` は**本人が自分のダッシュボードを見たときだけ**。`build_dashboard(user_id, item, viewer_id=None)` の `viewer_id` は「誰が見ているか」で、**省略時は本人扱い**。呼び出し元の `stats_view.RunStatsButton` は `viewer_id=view.viewer_id` を渡している（省略すると、管理者が他人の統計を開いたときに相手の opt-out の注記まで見えてしまう）。
  - **対象は統計だけ**: 長期記憶（`profile` / `projects` / `pending_log`）は opt-out の対象外。パネルの説明文でその旨を明示している（誤解させると透明性の目的を損なうため、文言を削らないこと）。
  - **フェイルオープンの例外**: 設定の保存・データ削除に失敗したときは、UI に「保存できませんでした」「削除できませんでした」と伝える（`save_background(user_id)`・`set_opt_out`・`clear_user_data` が成否を `bool` で返す）。プライバシー操作の失敗は黙って成功に見せない。解析の LLM 待機中はロックを離すが、削除・収集設定変更の世代を確認して古い結果を書き戻さない。解析失敗時も現在のキャッシュを参照し、待機中に消した解析を返さない。

### Discordの共通処理

自発会話・記念日・リマインダー・公開要約の長文投稿は `discord_helpers.send_long_message` に送信オプションごと渡す。`send_long_reply` の「元発言へ最初の返信を返す」契約は別に維持する。管理者の判定は `discord_helpers.is_administrator` を使う。

利用トークンとランキングの画像送信は `view_parts.send_graph_ephemeral` に集約する。defer後は `followup=True` を渡し、本人限定で送る。画像バッファの所有権も渡し、成功・失敗・キャンセルのいずれでも `discord.File.close()` の後にバッファを閉じる。Bot応答・自発会話・自律配信・リマインダーの出力マスクは `NgWordFilter.mask_with_log` を使い、文脈別のログと後続のモデレーションを維持する。

### グラフ描画と同期I/Oの待機

自発会話・記念日・自律配信・会話要約のペルソナ読込も `run_in_thread` で待つ。設定確定の存在確認とNGワード再読込はDiscordへ応答をdeferしてからワーカーで行う。NGワードは単語と正規表現を準備してからスナップショットを差し替え、読込途中の状態を公開しない。画像は `MAX_IMAGE_SIZE_BYTES` を取得前と取得後に確認し、デコードとRGB変換をワーカーへ渡す。中断時は取得済み画像も回収する。

モデルのJSON抽出には用途ごとの `required_keys` を指定する（モデレーションは `safe`、文書要約は `summary`、記憶ルーティングは `category`、圧縮は `profile` / `projects`、統計は `topics` / `big_five` / `personality`）。記憶カテゴリと繰り返し種別の正規化は `utils/normalization.py` に集約する。`reminder_service.normalize_recurrence` は同じ関数を再公開している。

フォント候補は `constants.GRAPH_FONT_FAMILY` を唯一の情報源とし、`graph_rendering.graph_font_context()` が導入済み候補と汎用フォントだけを順序どおり返す。利用量・時間帯・話題・性格・ランキングの全描画でこの設定を `plt.rc_context` に渡し、グローバルな設定は変更しない。Linuxでは `fonts-noto-cjk` 等の日本語フォントをOSへ導入する必要がある。導入・キャッシュ再生成の手順はREADME、字形と5種類のPNG確認は `tests/utils/test_graph_fonts.py` を参照する。LinuxのCIはフォントを導入し、未解決ならテストを失敗させる。

統計の `build_dashboard` / `build_ranking_graph` と利用量の `build_graph_buffer` は、データをイベントループでコピーしてから描画をワーカーへ渡す。matplotlibはスレッド間で共有状態を持つため、全描画関数を `graph_rendering.serialized_plot` の共通ロックで直列化する（Aggバックエンドを維持）。UIは描画前に応答をdeferし、完了後に本人だけのfollowupで返す。描画中の収集設定変更・削除は更新世代でも照合し、古い画像を破棄する。中断時の画像バッファ・Figureも回収する。

`background.run_in_thread` は `asyncio.to_thread` の完了を待つ入口。キャンセルしてもスレッド自体は止まらないため、worker完了まで呼出元のロックを保持してからキャンセルを伝播する。記憶・違反記録・会話統計・サーバー設定・記念日の記録の保存はこの入口を使い、後着した保存が削除や新しい更新を上書きしないようにする（ロックと組み合わせて初めて順序が守られる。ロックの無い呼び出しを置き換えるだけでは並行した保存の順序は守れない）。起動時の記憶ディレクトリ準備も `MemoryService.io_lock` の下で実行する。

キャンセル時に受取先の無くなった結果は、`close` が呼び出せる場合だけ回収する。属性取得・回収の例外は型名だけをログに出し、キャンセルを優先して伝播する。正常に返す結果の所有権は呼出元に渡し、この入口では閉じない。

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

- **ヘルプとウェルカムの文言を2か所に書かないこと。** 両者は説明する内容が大きく重なるため、共通部分（メンションでの会話・添付・リマインダー・読み上げ・自律行動・評価・記憶・統計）は `help_view.py` のモジュール内定数を唯一の情報源にし、`build_help_text()` / `build_welcome_text()` の両方がそれを組み立てて使う。片方だけ直すと「ウェルカムには書いてあるのにヘルプには無い」という食い違いが生まれ、案内としての信頼を失う。**機能を足したり既定値を変えたりしたら、ここの文言も必ず合わせて直すこと**（本文が実装と食い違わないことが最優先）。長い日本語テキストのため `constants.py` には置かない（`constants.py` は調整値の置き場という既存規約を守る）。
- **ボット名はハードコードせず `resolve_bot_name(client.user)` で解決する**（別名のボットで同じコードを動かすとメンション例だけが嘘になるため）。名前から `@` を落とすのは、`@everyone` という名前のボットが作られた場合への保険。
- **本文は Discord の1メッセージ上限に収める**: `_fit_to_discord_limit` が超過分を切り詰めて警告ログを出す。エフェメラルの差し替え（`edit_message`）は1通しか置き換えられないため `split_message` は使えない（View を持てるメッセージは1通だけ）。切り詰めは安全弁であり、発動したら文言を減らすこと。
- **使い方パネル**: `HelpView` は `PanelView` を継承し、部品は ↩️ 戻る（row 0）だけ。表示のみで副作用が無いため権限制限は設けない（DM でも使える）。差し替えは `view_parts.swap_panel()`。
- **ウェルカム投稿の配線は薄く保つ**: `on_guild_join` は「本文を組み立てて `discord_helpers.post_welcome_message` へ渡す」だけで、判断ロジックは各モジュール側にある。イベントハンドラの本体は `try/except Exception` で包む（案内の失敗でイベント処理を落とさない）。必要な `guilds` インテントは `Intents.default()` に含まれるため **`bot.py` の intents は触らない**。
- **投稿先の決め方**（`pick_announcement_channel`）: ① `guild.system_channel` → ② `guild.text_channels` を順に走査して最初に送信できるチャンネル → ③ 見つからなければ投稿しない（`print` のログのみ）。権限は必ず `channel.permissions_for(guild.me)` で**チャンネル単位に**、閲覧（`view_channel`）と送信（`send_messages`）の両方を確認する（サーバー全体のロールで許可されていてもチャンネル個別で拒否されうる。`/join` の接続権限チェックや自発会話と同じ作法。discord.py は閲覧できないチャンネルの送信権限も落とすが、その内部の挙動には頼らない）。「送ってみて失敗したら次」の例外頼みにしないこと（無駄に API を叩き、権限エラーとネットワーク障害を区別できない）。`guild.channels` は使わない（カテゴリやボイスが混ざる）。`guild.me` が `None`（キャッシュ未取得）なら権限を判定できないので諦める。
- **連投防止**: `BotState.welcomed_guild_ids`（**インメモリのみ**。`auto_topic_channels` と同じ「実行時に自然に再構築される一時状態」）。`on_guild_join` は実際に招待されたときにしか発火せず再起動・再接続では発火しないため、プロセスをまたぐ重複投稿は原理的に起きない（この集合は同一プロセス内でイベントが重複した場合の保険）。**投稿済みフラグは送信を待つ前に立てること** — 送信後に立てると、その待ち時間に入ってきた2件目が二重投稿になる。DB へは持たせない（`guild_settings` に列を足すと `guild_settings_load_all` / `guild_settings_save` / `apply_persisted` / `persist` の4か所すべてを揃えて直す必要があり、1か所でも漏れると他の設定が既定値へ戻る）。
- **ウェルカム本文に `@everyone` / `@here` / ロールメンションを絶対に書かないこと**（公開チャンネルへの投稿なので通知爆撃になる。招待→キック→再招待を繰り返されると毎回投稿される）。送信側でも `discord.AllowedMentions.none()` を付けて二重に防いでいる。個人情報・記憶・統計も載せない（参加時点でそもそもデータが無い）。
- **フェイルオープン**: `post_welcome_message` は例外を外へ出さず、投稿できたかを `bool` で返す（`discord.Forbidden` → `discord.HTTPException` の順で捕まえる）。案内が出せなくてもボット自体は普通に使える。`WELCOME_MESSAGE_ENABLED`（`constants.py`）で投稿だけを止められ、止めても ❓ 使い方 からいつでも読める。既に招待済みのサーバーへは投稿されない（仕様として許容）。

## 規約と落とし穴

- **パス解決**（`persona_bot/config/constants.py`, `persona_bot/config/paths.py`）: `BASE_DIR = src/`（`constants.py` の `Path(__file__).resolve().parents[2]`）、`PROJECT_ROOT` はその親。ペルソナファイルは `"../prompts/prompt_tsun.txt"` のような値（`src/` 基準）として保存・選択され、`resolve_prompt_path` で解決される。`memory/`・`ng/`・`prompts/` はプロジェクトルート直下にある。ファイルの配置を変えたら `tests/integration/test_package_layout.py`（全ペルソナの相対パスと `.env`・DB・記憶・NG の保存先が変わらないことを確かめる）を回す。
- **設定（APIキー）**: `settings_loader.load_settings()` はプロジェクトルート直下の `.env`（パスは `constants.ENV_FILE_PATH`。**絶対パスで解決すること** — ボットは `src/` から起動するため、カレントディレクトリ基準の相対探索では見つけられない）を `python-dotenv` で読み込み、`.API_KEY_GEMINI` / `.TOKEN_DISCORD` を持つ `Settings` データクラスを返す。旧 `config.py` / `config_private.py` は ver0.1.6 で削除済み。
  - **`load_dotenv(override=False)` を変えないこと**: OS の環境変数が `.env` より優先される。systemd の `EnvironmentFile=` や Docker の `--env-file` へ移行できるようにするための挙動で、逆にするとサーバーに残った古い `.env` が黙って優先され「直したはずのキーが反映されない」事故になる。
  - **未設定なら起動を止める**: ファイル無し・変数無し・空文字・`.env.example` のプレースホルダのまま、のいずれでも `キー未設定` を出力して `SettingsError` を送出する。`load_settings()` は `bot.py` のトップレベルで呼ばれるためボットは起動しない。キーが無いまま起動すると「Discord へは接続できるが Gemini の呼び出しだけ全滅する」という切り分けの難しい状態になるため、あえて失敗させている。
  - **`python-dotenv` が未インストールでも例外にしないこと**: 警告を出して `os.environ` だけで続行する（環境変数でキーを渡す運用ならこのパッケージは不要なため）。
  - `.env.example` のプレースホルダ文字列を変えたら `settings_loader._PLACEHOLDER_VALUES` と `deploy/migrate_config_to_env.py` の `PLACEHOLDER_VALUES` も**必ず対で直す**こと（片方だけ直すと書き換え忘れの検出が効かなくなる）。
  - **移行用**: `deploy/migrate_config_to_env.py` が旧 `config_private.py` / `config.py` から `.env` を生成する（ver0.1.5 以前から更新した環境向け。ボット本体からは呼ばれない一度きりのスクリプト）。旧ファイルは `ast` で構文解析するだけで **import しない** — import は中身を実行するため、値を読むだけの用途で実行する必要が無い。
- **調整値は `constants.py` に置く** — モデル名、タイムアウト、しきい値、上限値。新規もインラインではなくここに追加すること。
- **fire-and-forget は `background.spawn(coro, label=...)` を使う（`asyncio.create_task` を直接呼ばない）**: asyncio のイベントループは実行中タスクを**弱参照**でしか保持しないため、`create_task` の戻り値を捨てるとタスクが実行途中でガベージコレクトされて消えることがある（Python 公式ドキュメントが明記している既知の落とし穴）。`spawn` は完了までタスクへの強参照を保持し、完了時に自動で取り除く。加えてタスク内の例外を `label` 付きでログに出す — `create_task` に投げっぱなしだと例外は回収時まで報告されず、そのまま失われることもある。特に `documents.run_worker()` のような常駐コルーチンでは、消えると添付ファイルの処理が二度と走らなくなる。
- **タイムゾーン名を表示・プロンプトに載せるときは `timeutil.timezone_label(self._tz)` を使う（`REMINDER_TIMEZONE` などの定数をそのまま書かない）**: `resolve_timezone` は tzdata の無い環境で UTC へフォールバックするため、定数を表示すると「実際は UTC の時刻なのに Asia/Tokyo と名乗る」食い違いが起きる。リマインダーの統合判定プロンプトでは、この食い違いがそのまま9時間ずれた日時での登録に直結する。
- **モデレーションはフェイルオープン**: タイムアウト/例外時、`content_moderator.check` は *safe* を返してボットを動かし続ける。Gemini のプロンプトブロックは *unsafe* として扱う。この挙動を維持すること — 一時的な API エラーでユーザーをブロックしないこと。**`check` / `check_and_route` の本体で例外を外へ漏らさないこと**: これらは `on_message` の主経路から呼ばれるため、抜けた例外はそのメッセージの処理ごと落とす。準備処理（タイムゾーンの解決など）を `try` の外に置くと、環境依存の失敗が全メッセージの無応答に直結する（実際に `ZoneInfo` の組み立てで起きた）。
- **タイムゾーンは `timeutil.resolve_timezone(name, purpose=...)` で解決する**: `ZoneInfo` を直接呼ばないこと。タイムゾーンデータベースの無い環境（`tzdata` を含まないスリムな Linux イメージなど）では例外になるため、この関数が UTC へのフォールバックと警告ログをまとめて引き受ける。**Windows はこの状態が既定**で、OS にタイムゾーンDBが無く `tzdata` も `requirements.txt` に含まれないため、Windows の仮想環境では `Asia/Tokyo` を解決できず UTC で動く（テスト実行時に出る「タイムゾーン(Asia/Tokyo)の読み込みに失敗しました」はこれで、テストの失敗ではない）。タイムゾーン名そのもの（`STATS_TIMEZONE` / `ANNIVERSARY_TIMEZONE` / `REMINDER_TIMEZONE`）は用途ごとに `constants.py` で個別に調整できる形を保つ。
- **再起動でリセットされる状態**（インメモリのみ）: 利用/トークン履歴（`usage_graph`、500件のリングバッファ）、`channel_reset_points`（`/functions` → ⚙️キャラ設定パネルの会話リセットのアンカー）、自発的な会話の**監視状態**（`auto_topic_channels` / `auto_topic_posted_at`）、自律行動の**配信状態と話したいゲージ**（`autonomous_session` / `autonomous_last_activity` / `autonomous_recent_activity`。ボイス接続自体が再起動をまたげないため復元しない）、フィードバックの**評価対象レジストリ**と**付与クールダウンの記録**（`FeedbackService._tracked` / `_last_reaction_at`。記録済みの評価そのものは DB に残る）、ウェルカム投稿済みのサーバーID（`welcomed_guild_ids`。`on_guild_join` は招待時にしか発火しないため、消えても再投稿は起きない）。一方、`GuildSettings` の**設定値**（ペルソナ/モデル/応答モード/グラウンディング/自発会話の ON・しきい値/自律行動の ON）・記念日の投稿記録・リマインダー・フィードバックの記録は DB に永続化され、再起動しても失われない。永続化される状態は `memory/bot.db`（SQLite: affinity / user_stats / queue_tasks / pending_log / profile / guild_settings / anniversary_posts / reminders / feedback）、`memory/projects.json`、`ng/blacklist.json`。`memory/profile.json` は手書き追加用の受け皿で、起動時に DB へ吸収され `{}` に戻る。
- **単一プロセス前提**: DB は `database.py` 内部の `threading.Lock` + トランザクションで、`projects.json` は `MemoryService.io_lock`（`asyncio.Lock`）で保護される。`blacklist.json` の違反登録・解除は非同期ロックで直列化し、別スレッドで原子的保存した後にキャッシュのスナップショットを差し替える（`profile.json` は起動時に DB へ吸収されるだけ）。保存前に `ng/` を作る（初回の違反記録で保存できるように）。手で直すこともあるファイルなので、ユーザーごとの値が dict でない・回数が数値でない記録は「記録なし」として扱い、`is_blocked` から例外を出さない（`on_message` の最初に呼ばれるため）。プロセス間ロックは存在しない。DB は WAL モードのため、稼働中でも `sqlite3` CLI 等からの**読み取り**は安全（外部からの書き込みはしないこと）。
- **ログ出力とエンコーディング**: Windows の既定コンソールは cp932 で、絵文字を `print` すると `UnicodeEncodeError` でそこの処理が止まる。`bot.py` の冒頭で標準出力を UTF-8（`errors="replace"`）へ再設定しているので通常は問題ないが、**`bot.py` を経由しない単体スクリプトから同じモジュールを動かすときは `PYTHONIOENCODING=utf-8` を付けること**。
- **Discord の制限**: 2000文字を超える返信は `send_long_reply`/`split_message` を通す。`send_long_reply` は**1通目（元メッセージへの `reply`）**を返し、フィードバックのリアクション付与がそれを使う（戻り値を最後のチャンクに変えない。戻り値を無視する呼び出しはそのままでよい）。グラウンディング返信はリンクの埋め込みプレビューを抑制する。
- **`ng/NG_WORD_private.txt`** は gitignore されており、読み込み時に `NG_WORD.txt` の上にマージされる。`/functions` の 🔄 NGワード再読込 で再起動なしに再読み込みできる。
- **手書きのテキストは `utf-8-sig` で読む**: NGワード（`ng_word_service._load_words_from_file`）とペルソナ（`paths.load_prompt_template`）は、メモ帳の「UTF-8 (BOM 付き)」保存でも先頭に U+FEFF を残さない。`utf-8` に戻さないこと — BOM は `strip()` で消えないため、1行目のコメントがNGワードとして登録され、1行目のNGワードは検知できなくなる。`find_ng_words` は `dict.fromkeys` で重複を除き、文中に最初に現れた順を保つ（`set` だと順序が実行ごとに変わる）。

## 作業完了時の手順（PR 作成とプライバシーチェック）

このリポジトリは **Public** で、PR・コミット・Issue に書いた内容はそのまま公開される（後から消しても、キャッシュやフォークに残りうる）。

**PR は、利用者が「作業完了を宣言します」と伝えた後にだけ作る。** エージェントが自分の判断で作業を終えたときは、トピックブランチへのコミットまでにとどめ、**push も PR 作成もしない**。エージェント自身は「作業完了を宣言します」という言い回しを使わないこと（利用者の合図と紛れるため）。ファイルを変更しないタスク（質問への回答・調査だけ）では PR は作らない。

宣言を受けた後の push と PR 作成は、その宣言が依頼にあたるので都度の承認は要らない（`main` へ直接 push しないこと）。

### 宣言を受ける前（作業中）

1. トピックブランチで作業し、コミットする。コミットの前にも下の項目でプライバシーチェックを行い、個人情報を最初から履歴に入れない。
2. 作業が終わったら、ブランチ名・コミット・確認した内容を報告し、「PR を出してよければ『作業完了を宣言します』と伝えてください」と添えて、**利用者の宣言を待つ**。

### 宣言を受けた後（この順番を守る）

1. **最新の状態を取り直す**（`git fetch`、`gh pr list` / `gh pr view`）。別のエージェントや利用者が、すでに PR をマージしていたり `main` を進めていたりすることがある。対象のブランチの PR がマージ済みなら、`main` から切り直したブランチへコミットを移す。
2. **プライバシーチェック**を行う（公開前の最終確認）。
   - 対象: このブランチの差分すべて（`git diff origin/main...HEAD`）、コミットメッセージ（`git log origin/main..HEAD`）、これから書く PR のタイトルと本文、PR に添付する画像。
   - 見つけた個人情報は**削除するか、プレースホルダーに置き換える**（下の表）。
3. 必要な修正をコミットする。まだ push していないコミットに個人情報が入っていた場合は、後から消すコミットを足すのではなく、push する前にそのコミット自体を書き直して（`git commit --amend` など）履歴に残さない。履歴の書き換えに承認が要る運用なら、書き直す前に利用者へ確認する。
4. トピックブランチを push し、`gh pr create` で PR を作る（既存の PR があれば、そのブランチへ push して本文を更新する）。
5. PR 本文に `## プライバシーチェック` 節を設け、確認した範囲と結果を書く（置き換えた項目の**種類と件数**だけ。**置き換える前の値そのものは書かない**）。
6. PR の URL・確認結果・プライバシーチェックの結果を報告する。

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
