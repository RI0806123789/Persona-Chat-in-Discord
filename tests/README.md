# 回帰テスト

仮想環境のPythonを使い、リポジトリルートで実行する。

```bash
python -B -m unittest discover -s tests -v
```

## フォルダ構成と個別実行

```text
tests/
├── config/       # 調整定数
├── core/         # DB・メッセージ処理
├── services/     # 会話・記憶・自律行動などの機能
├── views/        # Discordのパネル
├── utils/        # 保存・JSON解析・グラフなどの共通処理
├── deploy/       # 運用・移行スクリプト
├── integration/  # 起動配線・SDK移行・非同期I/O
└── helpers/      # テスト間で共有する補助処理
```

一括実行のコマンドは従来どおり。各フォルダの `__init__.py` により、Python 3.10／3.13の `unittest discover` がサブフォルダも検出する。分類だけ、モジュールだけ、ファイルの直接実行も選べる。

```bash
python -B -m unittest discover -s tests/services -v
python -B -m unittest tests.core.test_bot_reply_settings -v
python -B tests/core/test_bot_reply_settings.py -v
```

外部サービスはモック、DB・JSONは一時ディレクトリを使う。実際の設定・会話・記憶は読み込まない。メッセージ処理は `MessageHandler` を直接呼び、`bot.py` の起動処理やAST抽出は使わない。時刻集計の日本時間は標準ライブラリの固定オフセットで再現し、タイムゾーンDB欠落時のUTC代用も別途検証する。

Windows・Python 3.13で、整理前後に検出したテストの一覧が全288件一致し、従来の一括実行で全件が省略なしで成功した。リポジトリルートからの検出と `-t .` 指定でも同じ一覧になることを確認した。上の個別実行例は、分類別169件・モジュール指定9件・ファイル直接実行9件がそれぞれ成功した。移動した28ファイルは、プロジェクトの探索パスと共通補助のimportを除いてASTが一致し、全88ソースの構文・Python 3.10文法、依存関係の整合性も確認した。今回の整理ではLinux・Python 3.10での実行は未確認。

## 感情タグの小文字とBOM付きファイルの確認（#54・#55）

| Issue | テスト | 確認内容 |
| --- | --- | --- |
| #54 | `services/test_affinity_service.AffinityTagTests` | 小文字・大小混在・空白入りのタグを抽出・除去し、変化量を返す。大文字タグとタグのない本文は従来どおり |
| #55 | `services/test_ng_word_service` | BOM付きの公開／非公開NGファイルで1行目のコメントを除外し、1行目の語を検知・マスクする。BOMの有無だけが違う同じ語は1件にまとめる。検出結果は重複を除いた出現順 |
| #55 | `config/test_prompt_loading` | BOM付きのペルソナを先頭のU+FEFFなしで読み、BOMなし・文字列パス指定も従来どおり |

NGワードは一時ディレクトリの架空の語だけを使い、実際の `ng/` は読まない。

2026-10-16、Windows・Python 3.13で修正前のソースに対して追加11件を実行し、10件が失敗した（小文字・混在タグの4パターン、BOM付きNGファイル3件、検出順1件、BOM付きペルソナ2件）。BOMなしと大文字タグの確認は修正前から成功する。修正後は `PYTHONHASHSEED` を0〜3とランダムに変えて追加分がすべて成功し、全299テストが省略なしで成功した。依存関係・全ソースの構文・メッセージ分割・`compileall` も確認した。Linux・Python 3.10での実行と、実際のDiscord・Geminiでの再現は未確認。

## ボット本体のフォルダ整理時の確認

`integration/test_package_layout.py` は、移動後も全ペルソナの既存の相対パスと、`.env`・DB・記憶・NG関連ファイルの保存先が変わらないことを確認する。起動の確認ではソースだけを一時プロジェクトへコピーし、架空の `.env` と一時DBを使って、従来の `src/bot.py` を子プロセス内で実行する。Geminiクライアントの生成とDiscordの接続入口をモック化し、コマンド登録まで進むことと外部通信・DNSの試行がないことを確認する。

Windows・Python 3.13の仮想環境で、既存286件と追加2件の全288テストが省略なしで成功した。依存関係、全79ソースの構文とPython 3.10文法、メッセージ分割、構成図のXML、差分も確認した。移動前後の全43ソースは、importとパス基準の調整を除いてASTが一致した。Linuxでの実行、Python 3.10での実行、実際のDiscord・Gemini・ボイス接続は未確認。

## 自動投稿・移行・パネル・JSONと自律行動の確認（#39〜#46）

| Issue | 確認内容 |
| --- | --- |
| #39 | 独自RESTは既にSDKへ移行済み。本番と同じfactory・SDKのHTTP境界をモック化し、検索の成功・接続障害・タイムアウト・HTTP例外・400/503でキーがヘッダーだけに入り、URLとログへ出ないことを確認 |
| #40 | 自発話題は通知を抑止し、記念日は対象者だけを通知可能に制限。長文の全チャンクにも同じ制限が渡ることを確認 |
| #41 | 実DBへ移行途中・版更新直前の失敗を注入。子プロセスをALTER直後に終了した場合も、DDL・版が戻り、既存データを保って再起動時に移行を再試行できることを確認 |
| #42 | サブパネル遷移・ハブ復帰後に通常メッセージの編集を禁止し、Webhookのメッセージで時間切れ表示とUI除去が行われることを確認 |
| #43 | 閲覧・送信権限不足、ボットMember未取得、DMで履歴・生成・判定・送信を省略。スレッド権限、権限復旧後の投稿、生成後のForbiddenの再生成抑制、一時障害の再試行を確認 |
| #44 | 不正・閉じていない先行候補、ネスト、文字列内の括弧・エスケープ、コードブロック、別形式のJSON例、整数桁数上限を確認。両モデレーション入口が後続のunsafe判定を使うことと、解析不能時の既存動作も確認 |
| #45 | 実際に呼ばれる `reminder_add` へBarrierを置き、並行した2件の到達・戻りID・登録件数を検証。サーバー/DMと繰り返し予定を混ぜた場合も最後の1枠に1件だけ成功することを確認 |
| #46 | 計算モジュールをDiscord・設定モジュールなしでimportし、ゲージ・活動窓・深夜帯の境界を検証。チャンネルの権限・ステージ優先・枠の作成/後片付けを確認し、既存のキャンセル・接続差し替え保護の回帰も実行 |

2026-10-16、Windows・Python 3.13の専用仮想環境へ `requirements.txt` の指定版を導入して全286テストが成功した。省略・実通信の試行はともに0件。HTTPX・aiohttpの実通信と外部DNSを遮断し、試行件数も確認した。依存関係・全Pythonソースの構文・Python 3.10文法・メッセージ分割・図のXML・差分の確認も成功。Linux・Python 3.10での実行と、実際のDiscord・Gemini・ボイス接続はローカルでは未確認。

## 修正前後の確認記録

2026-10-15、Windows・Python 3.13で確認。過去のソースだけを一時ディレクトリへ展開し、同じ代表テストを修正前後に適用した。実行時ファイルはコピーしていない。下表はいずれも問題に対応するアサーションが修正前に失敗し、修正後に成功した。

| Issue | 代表テスト | 修正前 → 修正後 | 検出した問題 |
| --- | --- | --- | --- |
| #2 | `test_memory_service.DocumentSummaryCompactionTests.test_addition_at_limit_is_preserved` | `08717c9` → `790b4f7` | 圧縮中に追加した要約の消失 |
| #3 | `test_document_service.DocumentTemporaryFileTests.test_read_failure_removes_temporary_file` | `08717c9` → `790b4f7` | 添付読込失敗後の一時ファイル残留 |
| #4 | `test_document_service.DocumentResponseTests.test_affinity_tag_is_removed_before_screening_and_storage` | `302c7c1` → `46abda2` | 文書応答の感情タグ残留 |
| #5 | `test_settings_view.ConversationResetTests.test_guild_reset_excludes_messages_before_button_press` | `302c7c1` → `46abda2` | リセット前の履歴混入 |
| #6 | `test_reminder_service.ReminderRegistrationTests.test_last_slot_accepts_only_one_concurrent_request` | `302c7c1` → `22571ac` | 残り1枠への同時二重登録 |
| #7 | `test_bot_reply_settings.ReplySettingsTests.test_generation_change_uses_old_settings_then_next_reply_uses_new` | `302c7c1` → `22571ac` | 生成と評価に使うモデル・ペルソナの不一致 |
| #8 | `test_autonomous_comments.AutonomousCommentAdmissionTests.test_five_comments_make_only_one_input_and_output_check` | `cb46a7b` → `6e9cdd9` | 連続5コメントで入力判定も5回実行 |
| #9 | `test_stats_service.UserStatsPersistenceTests.test_write_count_is_constant_for_one_hundred_and_thousand_users` | `cb46a7b` → `6e9cdd9` | ユーザー数に比例してDB書込が増加 |
| #11 | `test_storage.JsonStorageTests` | `033cd92` → `c200681` | 書込中断で原本が変化し、破損時の退避がない |
| #12 | `test_reminder_view.ReminderStatusTextTests.test_long_lists_fit_limit_and_report_every_omitted_record` | `033cd92` → `c200681` | 長文の予定一覧が1900文字を超過 |
| #13 | `test_autonomous_session_cleanup` | `c200681` → 追加修正 | 接続喪失時の残留・停止中に差し替わった接続や枠の誤操作 |
| #14 | `test_event_loop_io` | `c200681` → 追加修正 | DB・JSON・描画待機中にイベントループが停止 |
| #15 | `test_gemini_service.GeminiGenerationTests` | `c200681` → 追加修正 | フォールバックが期限をリセットし、障害時にも追加呼出 |
| #16 | `test_graph_fonts` | `ea0f29f` → 追加修正 | Linux候補の欠落と、共有設定を使わない全5種類のグラフ |
| #18 | `test_affinity_service` | `6852d84` → 追加修正 | 親密度保存が全件削除・再挿入され、書込量がユーザー数に比例 |
| #19 | `test_tuning_constants` / `test_affinity_service.AffinityTuningTests` | `6852d84` → 追加修正 | 文書・記憶・統計・音声・親密度の定数変更が利用側へ反映されない |
| #20 | `test_shared_helpers` | `6852d84` → 追加修正 | 共通送信・マスク処理が使われず、グラフ送信ヘルパーでバッファが閉じない |

#17は初回確認時に#8との重複としてクローズされており、既存11件とローカルNG判定の追加1件で、対応済みの動作を確認した。再オープン後は下記の追加検証を行った。

過去の `bot.py` 向け比較だけは、その時点のAST抽出用テスト補助を使った。#7の表示用MockではWindowsのパス区切りを `Path.as_posix()` で正規化し、設定不一致そのものを確認した。現在のテストは通常のimportによる実装を検証する。

DB新規初期化・初期化の再実行・サーバーとDM設定の保存復元・副作用なしのimportは、それぞれ `core/test_database.py` と `core/test_message_handler_import.py` で検証する。Discord・Geminiの実接続、ボイス出力、電源断・プロセス強制終了はこのローカル検証に含まない。

#13は先行16テスト中15件、#14は先行5テスト中4件の停止検出と一括取得入口未実装の1件、#15は先行15テスト中17失敗・1エラー（subTestを含む）を修正前に確認した。最終テストには、保存キャンセルと後続削除の競合、描画中の収集停止・削除・再記録、応答保留と画像の後始末、実際のPNG並行生成も含む。

同日のローカル単回計測では、実PNG3枚（時間帯・ランキング・利用量）の並行生成が合計0.742秒、その間の5ms間隔のheartbeatは80回・最大間隔21.1msだった。架空の一時データによる参考値であり、本番VPSの処理時間は未測定。

## 日本語フォントの検証

`utils/test_graph_fonts.py` はWindows／Linuxそれぞれのフォント一覧を模擬し、共通候補の優先順・未導入候補の除外・汎用フォントへのフォールバックを検証する。実際の日本語フォントがあれば、全5種類のグラフを共有設定で描画し、PNG・日本語字形の欠落警告・設定の復元を確認する。Linux系候補が導入済みなら優先して検証に使う。

修正前は5テストで2失敗・3エラー、修正後はすべて成功した。日本語フォント未導入の開発環境では字形テストを省略し、`REQUIRE_JAPANESE_GRAPH_FONT=1` を指定した場合は失敗させる。LinuxのCIは `fonts-noto-cjk` 導入後、この指定で省略なしの検証を行う。導入・再起動の手順はREADMEを参照する。

## 親密度保存と調整定数の検証

`services/test_affinity_service.py` は一時DBと架空ユーザーで、1人・100人・1000人のいずれでも1件の更新に対してSQL書込が1文・変更行が1行であることを確認する。DBトリガーで削除・他ユーザーの挿入や更新を拒否し、同値の再挿入も見逃さない。DB再起動時の4項目の復元、並行更新、保存キャンセル後の順序、保存失敗後の再試行も含む。先行12テストは修正前に12失敗・3エラー（subTest込み）。その後、旧JSON取り込みと全件保存の既定値も確認する1テストを加え、最終13テストが成功した。

`config/test_tuning_constants.py` は起動前に定数を変更した状態を再現し、文書プレビューのUTF-8境界・文書記憶の参照件数・ランキング人数・EQ既定値と上下限・既定EQ名・PCMクリップを検証する。参照件数が0以下なら文書要約を載せず、小さいブロックでもIIRの状態が途切れないことも確認する。先行7テストは修正前に6失敗・1エラー（subTest込み）、修正後は全件成功した。外部サービスには接続しない。

`constants.py` の定義と `src/*.py` の参照をASTで再監査し、外部にも内部にも参照されない定数は0件と確認した。外部から直接参照されない `PROJECT_ROOT` / `NG_DIR` / `EDGE_TTS_VOICE_MALE` は定数定義内で使われる。`STATS_HOURLY_BUCKETS` は1時間ごとの24要素という保存形式を表し、今回の対応では区間数変更や既存データの再区分は行っていない。

## 共通ヘルパーの検証

`utils/test_shared_helpers.py` の18テストで、長文の分割順と送信オプション、記念日・リマインダーのメンション、要約の公開／本人限定経路、管理者権限の再確認、出力マスクと文脈別ログを検証する。グラフ送信は初回応答とdefer後のfollowupの両方を確認し、送信・画像構築の失敗やキャンセルでもバッファを回収する。利用量・ランキングのボタンからの呼出も含む。

修正前の `6852d84` のソースだけを一時展開して代表3テストを実行し、グラフ送信ヘルパーのバッファ未回収、自発会話・記念日での送信ヘルパー未使用、出力マスクヘルパー未使用の失敗を確認した。修正後は全18件が成功した。

#18〜#20の追加後、Windows・Python 3.13で日本語フォント必須の指定を有効にし、全199テストが省略なしで成功した。依存関係・全Pythonソースの構文・Python 3.10文法・図のXML・メッセージ分割も確認した。Linux、Python 3.10の実行、Discord・Geminiの実接続は未確認。

## Gemini SDKとPDFの移行検証（#21）

2026-10-16、Windows・Python 3.13の新しい仮想環境へ固定した直接依存を導入し、旧 `google-generativeai`・`PyPDF2` が入っていないことと `pip check` の成功を確認した。`services/test_genai_client.py` / `services/test_gemini_service.py` / `integration/test_sdk_migration.py` で、実際の `google-genai` の型・シリアライズと `httpx.MockTransport` を使い、通常生成・Google検索・PIL画像・ファイル転送／要約／削除・モデレーション・記憶ルーティング／圧縮・安全ブロック・利用トークン集計を確認する。PDFは `pypdf` で作った架空の平文・暗号化PDFと破損データを検証する。

検索の全段で同じ締切を共有し、非対応4xx以外の障害では追加生成せず、ユーザーのキャンセルを伝播する。SDKの既定aiohttp経路にある接続時の再送を避けるため、factoryはHTTPXの公開設定を使う。upload応答の必須状態ヘッダーが欠けた場合も、SDK内の同期sleepや再送へ進まず、応答を閉じて失敗する。ファイル転送の期限による中断、SDK終了の失敗時や重複終了時の所有HTTPX回収も確認する。

移行前の比較テストでは、モック漏れにより空のAPIキーと架空の質問で1回のリクエストが送信され、403で拒否された。実キー・会話・記憶は使っていない。以後は通信禁止ガードを加えた。最終全体テストもHTTPX・aiohttpの実通信と外部DNSを遮断し、失敗が呼出側で吸収されても通信の試行件数を別途確認する。

最終確認は全235テストが省略なしで成功し、実通信の試行は0件だった。日本語フォント必須の指定も有効にし、全Pythonソースの構文・Python 3.10文法・図のXML・CIのYAML・メッセージ分割・差分を確認した。

直接依存の固定は推移依存まで含む完全なロックではない。Windows／Linux・Python 3.10／3.13のCIは導入結果を `pip freeze --all` の成果物へ記録する。今回のローカル検証ではLinux・Python 3.10の実行と、有効な認証を用いたDiscord・Gemini・音声合成サービスでの正常動作は未確認。

## 実装説明の照合（#22）

文書添付→統計・リマインダー→ルーティングの順序、検索設定の `GuildSettings` 所属、統計のSQLite永続化、`viewer_id` の受け渡し、違反回数の定数参照は先行修正で整合済みと確認した。残るテーブル一覧・スキーマ移行例・曜日表・ボイス接続中の読み上げ・記憶カテゴリの利用・親密度更新の順序を実装に合わせた。コメント／docstringだけを変えた6ソースは、docstringを除くASTが修正前と一致することを確認した。SDK移行で変わる配線・呼出経路はCLAUDEと図も更新した。

## 配信コメントの受付枠の再確認（#17）

2026-10-16、再オープンされた#17を `c0cf91d` の実装と照合した。ローカルNG判定→同期受付→入力AIの順序は#8の修正としてmainへ反映済みだったため、処理ロジックの追加修正は行っていない。

既存12件に次の3件を追加した。

- AIで不適切と判定した入力の連投でも入力判定は1回に抑え、受付枠を返さない。期限後の安全な入力は受け付ける。
- ローカルNG判定だけで拒否した入力はAPIも枠も使わず、直後の安全な入力を受け付ける。
- 直前の受付時刻からクールダウンの1マイクロ秒前は見送り、ちょうど境界に達した入力を受け付ける。

受付の基準を「応答後」ではなく「入力AI前の受付時刻」と明記し、同じユーザーにも適用されることと、見送った入力のAI違反を記録しないことをREADME・CLAUDE・コメントへ揃えた。

コメント受付の15件を含む全238テストが成功し、省略・実通信の試行はともに0件だった。依存関係・構文・Python 3.10文法・差分も確認し、変更した2ソースの実装部分はdocstringを除いたASTが変更前と一致した。Discord・Geminiを使った実環境での再現は行っていない。
