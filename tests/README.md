# 回帰テスト

仮想環境のPythonを使い、リポジトリルートで実行する。

```bash
python -B -m unittest discover -s tests -v
```

外部サービスはモック、DB・JSONは一時ディレクトリを使う。実際の設定・会話・記憶は読み込まない。メッセージ処理は `MessageHandler` を直接呼び、`bot.py` の起動処理やAST抽出は使わない。時刻集計の日本時間は標準ライブラリの固定オフセットで再現し、タイムゾーンDB欠落時のUTC代用も別途検証する。

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

#17は#8との重複としてクローズ済み。`test_autonomous_comments.py` の既存11件と、クールダウン中のローカルNG判定を検証する追加1件を再実行し、機能の追加修正は不要と確認した。

過去の `bot.py` 向け比較だけは、その時点のAST抽出用テスト補助を使った。#7の表示用MockではWindowsのパス区切りを `Path.as_posix()` で正規化し、設定不一致そのものを確認した。現在のテストは通常のimportによる実装を検証する。

DB新規初期化・初期化の再実行・サーバーとDM設定の保存復元・副作用なしのimportは、それぞれ `test_database.py` と `test_message_handler_import.py` で検証する。Discord・Geminiの実接続、ボイス出力、電源断・プロセス強制終了はこのローカル検証に含まない。

#13は先行16テスト中15件、#14は先行5テスト中4件の停止検出と一括取得入口未実装の1件、#15は先行15テスト中17失敗・1エラー（subTestを含む）を修正前に確認した。最終テストには、保存キャンセルと後続削除の競合、描画中の収集停止・削除・再記録、応答保留と画像の後始末、実際のPNG並行生成も含む。

同日のローカル単回計測では、実PNG3枚（時間帯・ランキング・利用量）の並行生成が合計0.742秒、その間の5ms間隔のheartbeatは80回・最大間隔21.1msだった。架空の一時データによる参考値であり、本番VPSの処理時間は未測定。

## 日本語フォントの検証

`test_graph_fonts.py` はWindows／Linuxそれぞれのフォント一覧を模擬し、共通候補の優先順・未導入候補の除外・汎用フォントへのフォールバックを検証する。実際の日本語フォントがあれば、全5種類のグラフを共有設定で描画し、PNG・日本語字形の欠落警告・設定の復元を確認する。Linux系候補が導入済みなら優先して検証に使う。

修正前は5テストで2失敗・3エラー、修正後はすべて成功した。日本語フォント未導入の開発環境では字形テストを省略し、`REQUIRE_JAPANESE_GRAPH_FONT=1` を指定した場合は失敗させる。LinuxのCIは `fonts-noto-cjk` 導入後、この指定で省略なしの検証を行う。導入・再起動の手順はREADMEを参照する。

## 親密度保存と調整定数の検証

`test_affinity_service.py` は一時DBと架空ユーザーで、1人・100人・1000人のいずれでも1件の更新に対してSQL書込が1文・変更行が1行であることを確認する。DBトリガーで削除・他ユーザーの挿入や更新を拒否し、同値の再挿入も見逃さない。DB再起動時の4項目の復元、並行更新、保存キャンセル後の順序、保存失敗後の再試行も含む。先行12テストは修正前に12失敗・3エラー（subTest込み）。その後、旧JSON取り込みと全件保存の既定値も確認する1テストを加え、最終13テストが成功した。

`test_tuning_constants.py` は起動前に定数を変更した状態を再現し、文書プレビューのUTF-8境界・文書記憶の参照件数・ランキング人数・EQ既定値と上下限・既定EQ名・PCMクリップを検証する。参照件数が0以下なら文書要約を載せず、小さいブロックでもIIRの状態が途切れないことも確認する。先行7テストは修正前に6失敗・1エラー（subTest込み）、修正後は全件成功した。外部サービスには接続しない。

`constants.py` の定義と `src/*.py` の参照をASTで再監査し、外部にも内部にも参照されない定数は0件と確認した。外部から直接参照されない `PROJECT_ROOT` / `NG_DIR` / `EDGE_TTS_VOICE_MALE` は定数定義内で使われる。`STATS_HOURLY_BUCKETS` は1時間ごとの24要素という保存形式を表し、今回の対応では区間数変更や既存データの再区分は行っていない。

## 共通ヘルパーの検証

`test_shared_helpers.py` の18テストで、長文の分割順と送信オプション、記念日・リマインダーのメンション、要約の公開／本人限定経路、管理者権限の再確認、出力マスクと文脈別ログを検証する。グラフ送信は初回応答とdefer後のfollowupの両方を確認し、送信・画像構築の失敗やキャンセルでもバッファを回収する。利用量・ランキングのボタンからの呼出も含む。

修正前の `6852d84` のソースだけを一時展開して代表3テストを実行し、グラフ送信ヘルパーのバッファ未回収、自発会話・記念日での送信ヘルパー未使用、出力マスクヘルパー未使用の失敗を確認した。修正後は全18件が成功した。

#18〜#20の追加後、Windows・Python 3.13で日本語フォント必須の指定を有効にし、全199テストが省略なしで成功した。依存関係・全Pythonソースの構文・Python 3.10文法・図のXML・メッセージ分割も確認した。Linux、Python 3.10の実行、Discord・Geminiの実接続は未確認。
