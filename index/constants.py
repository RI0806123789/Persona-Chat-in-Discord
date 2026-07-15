from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent

DEFAULT_PROMPT_FILE = "../prompts/prompt_default.txt"
DEFAULT_MODEL_NAME = "gemini-3.1-flash-lite"
DEFAULT_RESPOND_MODE = "mention"

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
# pending_log の Q/A 件数がこれ以上になったら記憶圧縮を実行する
MEMORY_FLUSH_THRESHOLD_ENTRIES = 25
MEMORY_FLUSH_IDLE_SECONDS = 60 * 60
MEMORY_MAINTENANCE_MINUTES = 10

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

