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

過去の `bot.py` 向け比較だけは、その時点のAST抽出用テスト補助を使った。#7の表示用MockではWindowsのパス区切りを `Path.as_posix()` で正規化し、設定不一致そのものを確認した。現在のテストは通常のimportによる実装を検証する。

DB新規初期化・初期化の再実行・サーバーとDM設定の保存復元・副作用なしのimportは、それぞれ `test_database.py` と `test_message_handler_import.py` で検証する。Discord・Geminiの実接続、ボイス出力、電源断・プロセス強制終了はこのローカル検証に含まない。

#13は先行16テスト中15件、#14は先行5テスト中4件の停止検出と一括取得入口未実装の1件、#15は先行15テスト中17失敗・1エラー（subTestを含む）を修正前に確認した。最終テストには、保存キャンセルと後続削除の競合、描画中の収集停止・削除・再記録、応答保留と画像の後始末、実際のPNG並行生成も含む。

同日のローカル単回計測では、実PNG3枚（時間帯・ランキング・利用量）の並行生成が合計0.742秒、その間の5ms間隔のheartbeatは80回・最大間隔21.1msだった。架空の一時データによる参考値であり、本番VPSの処理時間は未測定。
