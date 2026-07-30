from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent

DEFAULT_PROMPT_FILE = "../prompts/prompt_default.txt"
DEFAULT_MODEL_NAME = "gemini-3.1-flash-lite"
DEFAULT_RESPOND_MODE = "mention"

# 設定はサーバー（ギルド）ごとに分離されるが、DM にはギルドIDが存在しない。
# DB（guild_settings / anniversary_posts テーブル）に DM の行を保存する際は、
# 実在しないギルドIDとしてこの値を使う（Discord のIDは正の整数のため 0 は衝突しない）。
DM_SETTINGS_GUILD_KEY = 0

MEMORY_DIR = PROJECT_ROOT / "memory"
# SQLite データベース本体。affinity / user_stats / queue_tasks / pending_log を格納する。
# profile.json / projects.json は LLM が構造を決める自由形式 JSON のためファイルのまま。
DATABASE_PATH = MEMORY_DIR / "bot.db"
PROFILE_MEMORY_PATH = MEMORY_DIR / "profile.json"
PROJECTS_MEMORY_PATH = MEMORY_DIR / "projects.json"

NG_DIR = PROJECT_ROOT / "ng"
NG_WORD_PATH = NG_DIR / "NG_WORD.txt"
NG_WORD_PRIVATE_PATH = NG_DIR / "NG_WORD_private.txt"
BLACKLIST_PATH = NG_DIR / "blacklist.json"
BLACKLIST_MAX_VIOLATIONS = 3

ROUTER_MODEL_NAME = "gemini-3.1-flash-lite"
ROUTER_TIMEOUT_SECONDS = 15.0
MEMORY_COMPACT_TIMEOUT_SECONDS = 30.0
# 記憶圧縮がこの回数連続で失敗したら pending_log を破棄して無限リトライを防ぐ
MEMORY_COMPACT_MAX_FAILURES = 3
# pending_log の Q/A 件数がこれ以上になったら記憶圧縮を実行する（ユーザー単位で判定）
MEMORY_FLUSH_THRESHOLD_ENTRIES = 25
MEMORY_FLUSH_IDLE_SECONDS = 60 * 60
MEMORY_MAINTENANCE_MINUTES = 10
# 1 回のフラッシュ処理で圧縮するユーザー数の上限。記憶はユーザー単位で個別に
# 圧縮する（他人の会話ログを同じ Gemini 呼び出しに混ぜない＝プライバシー保護）ため、
# アクティブ人数分の呼び出しが一度に走らないよう上限で区切る（レート制限対策）。
# 上限を超えた分は次回の見回りで順次処理される。
MEMORY_FLUSH_MAX_USERS_PER_RUN = 5

# 記憶ルーティングの分類カテゴリ（route_category とモデレーション統合判定の共通定義）。
MEMORY_ROUTE_CATEGORIES = ("profile", "projects", "none")

# ── ユーザー別記憶への移行用 ──
# 本ボットはユーザー別記憶の導入前、単一ユーザーによるテスト運用のため
# profile / projects を「全体共有の1スペース」で持っていた。その既存データを
# 失わないよう、移行時にここで指定したユーザーIDの記憶として引き継ぐ。
#
# 移行は既に完了しているため空文字にしてある（引き継ぎ元がもう存在しない）。
# 空文字のときは引き継ぎ処理自体が行われない。旧・全体共有形式のデータが
# 残っている環境から移行する場合のみ、その所有者の Discord ユーザーIDを入れること。
LEGACY_MEMORY_OWNER_ID = ""

HISTORY_LIMIT = 100
GEMINI_RESPONSE_TIMEOUT_SECONDS = 60.0
MODERATION_MODEL_NAME = "gemini-3.1-flash-lite"
MODERATION_TIMEOUT_SECONDS = 10.0

# 会話要約 (/summarize) 用
# 対象メッセージがこの件数を超えたら要約せず自動停止する（トークン超過・
# API 取得時間・レート制限の暴発を防ぐ安全弁）。
SUMMARY_MAX_MESSAGES = 1000
# 大量メッセージの要約は通常チャットより時間がかかるため長めに確保する。
SUMMARY_TIMEOUT_SECONDS = 120.0

MAX_DOCUMENT_SIZE_BYTES = 5 * 1024 * 1024
SUPPORTED_DOCUMENT_EXTENSIONS = {".pdf", ".txt", ".csv", ".md"}
DOCUMENT_UPLOAD_TIMEOUT_SECONDS = 60.0
DOCUMENT_SUMMARY_TIMEOUT_SECONDS = 60.0
DOCUMENT_SUMMARY_LIMIT = 12
DOCUMENT_WORKER_IDLE_SECONDS = 5
QUEUE_MAX_FINISHED_TASKS = 50
MAX_GROUNDING_SOURCES = 5
GROUNDING_MODEL_NAME = "gemini-2.5-flash"
# グラウンディング失敗時に GROUNDING_MODEL_NAME でもう一度リトライするか。
# True: REST 失敗 → サブモデルで再試行 → それでもダメなら SDK（最悪3回呼び出し）。
# False: REST 失敗 → 即 SDK フォールバック（最悪2回）。API 呼び出し回数を抑えたい
# 場合は False にする。ただし選択モデルが REST グラウンディング非対応のとき、
# 検索結果の質が下がる（グラウンディング無しへ早く落ちる）ことがある。
GROUNDING_SUBMODEL_RETRY_ENABLED = True

# UsageTracker が保持するトークン履歴の上限（リングバッファ）
MAX_USAGE_HISTORY = 500

# 自発的な会話 (/functions → 💬 自発会話) 用
# 機能を有効化した直後のデフォルト間隔（時間）。選択肢は choices.py の
# AUTO_TOPIC_INTERVAL_CHOICES（1/3/6/12/24時間）を唯一の情報源とする。
DEFAULT_AUTO_TOPIC_INTERVAL_HOURS = 6
# 無会話チェックを行うバックグラウンドループの周期（分）。
# 間隔そのものではなく「間隔を超えたかどうかの見回り」の頻度。
AUTO_TOPIC_CHECK_MINUTES = 10
# 話題生成の Gemini 呼び出しタイムアウト（秒）。
AUTO_TOPIC_TIMEOUT_SECONDS = 60.0
# 話題生成プロンプトに含めるチャンネル履歴の件数上限。
AUTO_TOPIC_HISTORY_LIMIT = 50

# 記念日・誕生日の自動お祝い用
# profile 記憶の特別キー名。値は [{"label": "誕生日", "month": 1〜12, "day": 1〜31}, ...] の形式。
ANNIVERSARY_PROFILE_KEY = "anniversaries"
# 「今日が記念日かどうか」の見回り間隔（分）。記念日はその日1回届けば十分なため、
# 自発的な会話(AUTO_TOPIC_CHECK_MINUTES)ほど高頻度にする必要はない。
ANNIVERSARY_CHECK_MINUTES = 60
# お祝いメッセージ生成の Gemini 呼び出しタイムアウト（秒）。
ANNIVERSARY_TIMEOUT_SECONDS = 60.0
# お祝いメッセージ生成プロンプトに含めるチャンネル履歴の件数上限。
ANNIVERSARY_HISTORY_LIMIT = 30
# 記念日の判定に使うタイムゾーン（STATS_TIMEZONE と同じ地域基準で「今日」を判定する）。
ANNIVERSARY_TIMEZONE = "Asia/Tokyo"

# リマインダー（会話から自動登録し、時刻になったら依頼されたチャンネルへ通知する）用
# 期限が来たリマインダーを探す見回りの間隔（秒）。分単位の指定に応えるため、
# 記念日（時間単位）よりずっと短い間隔で確認する。
REMINDER_CHECK_SECONDS = 60
# 「30分後」「明日の朝」などの相対表現を絶対日時に直すときの基準タイムゾーン。
# 保存は UTC で行い、ユーザーへの表示だけこのタイムゾーンに変換する。
REMINDER_TIMEZONE = "Asia/Tokyo"
# 1ユーザーが同時に持てる未通知リマインダーの上限（登録し放題にしない）。
REMINDER_MAX_PENDING_PER_USER = 20
# これより先の日時は受け付けない。日時の解釈は LLM に任せるため、
# 誤解釈で何十年も先に登録されてしまうのを防ぐ歯止め。
REMINDER_MAX_DAYS_AHEAD = 365
# リマインド本文の最大文字数（超過分は切り詰める）。
REMINDER_MAX_MESSAGE_LENGTH = 200
# 通知済み・取り消し済みレコードの保持件数。queue_tasks と同様に古い分から捨てる。
REMINDER_MAX_FINISHED = 100
# 通知予定をこの時間より大きく過ぎたリマインダーは、通知せず処理済みにする。
# ボットを長時間停止していた場合に、溜まっていた通知が起動直後へ一斉に流れるのを防ぐ
# （何時間も前の「10分後に休憩」を今さら知らされても困るため）。
REMINDER_STALE_HOURS = 24

# ユーザー別会話統計 (/functions → 📈 会話統計) 用
# 集計データは DATABASE_PATH (bot.db) の user_stats テーブルに永続化する。
# ボットへの発言のみを集計する。
# 話題／性格推定のため各ユーザーごとに保持する直近発言サンプルの上限件数。
# プライバシー配慮のため上限を設け、超過した古い発言から捨てる。
STATS_RECENT_SAMPLES_MAX = 50
# 話題／性格推定の LLM 解析結果をキャッシュする時間（時間）。
# これを超えると次回表示時に再計算する（毎回の課金を避ける）。
STATS_ANALYSIS_TTL_HOURS = 24
# 抽出する話題（トピック）の最大件数。
STATS_TOPIC_COUNT = 8
# 性格推定に必要な最小サンプル数。これ未満は「データ不足」として推定しない
# （少数サンプルへの過剰一般化を防ぐ）。
STATS_PERSONALITY_MIN_SAMPLES = 10
# ユーザー選択セレクトに載せる最大人数（Discord のセレクトは最大25件）。
STATS_MAX_USERS_IN_SELECT = 25
# 統計解析（話題／性格推定）の LLM 呼び出しタイムアウト（秒）。
STATS_ANALYSIS_TIMEOUT_SECONDS = 30.0
# 時間帯ヒストグラムの集計に使うタイムゾーン（発言時刻はこの地域時刻に変換して集計）。
STATS_TIMEZONE = "Asia/Tokyo"

# ボイスチャンネルの読み上げ (/join 中のみ動作) 用
# gTTS に渡す言語コード。
TTS_LANG = "ja"
# 読み上げる最大文字数。超過分は切り捨てて末尾に TTS_TRUNCATION_NOTICE を付ける。
# 長文の返信をすべて読み上げると数分間ボイスチャンネルを占有してしまうため、
# 「声で聞くのに適した長さ」で区切る（テキスト側には全文が投稿済み）。
TTS_MAX_CHARS = 200
# 文字数上限で切り詰めたときに末尾へ添える案内。
TTS_TRUNCATION_NOTICE = "以下省略します。"
# 再生待ちキューに積める最大件数。再生中に届いた返信はここに積まれ、順番に読み上げる。
# 上限を超えた分は捨てる（古い返信をいつまでも喋り続けないための歯止め）。
TTS_QUEUE_MAX = 5
# Discord のボイス送信が要求する PCM フォーマット。discord.PCMAudio は
# 「48kHz / 16bit / ステレオ」の生 PCM しか受け付けないため、
# miniaudio でのデコード時にこの形式へ変換する（ffmpeg は使わない）。
TTS_SAMPLE_RATE = 48000
TTS_CHANNELS = 2
# 音声合成（gTTS のネットワーク呼び出し＋デコード）のタイムアウト（秒）。
# 読み上げはチャット本体の付随機能なので、詰まったら諦めて次へ進む。
TTS_TIMEOUT_SECONDS = 30.0

