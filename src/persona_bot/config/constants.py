from pathlib import Path

# モジュールの配置に関係なく、既存のペルソナ相対パスは src/ を基準に保つ。
BASE_DIR = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BASE_DIR.parent

DEFAULT_PROMPT_FILE = "../prompts/prompt_default.txt"
DEFAULT_MODEL_NAME = "gemini-3.1-flash-lite"
DEFAULT_RESPOND_MODE = "mention"

# 設定はサーバー（ギルド）ごとに分離されるが、DM にはギルドIDが存在しない。
# DB（guild_settings / anniversary_posts テーブル）に DM の行を保存する際は、
# 実在しないギルドIDとしてこの値を使う（Discord のIDは正の整数のため 0 は衝突しない）。
DM_SETTINGS_GUILD_KEY = 0

# APIキー（Gemini / Discord）を書いた .env ファイル。プロジェクトルート直下に置く。
# ボットは src/ の中から起動するため、カレントディレクトリ基準の相対探索では
# 見つけられない。settings_loader.py はこの絶対パスを load_dotenv() へ明示的に渡す。
# .gitignore で除外済み（見本は Git 管理された .env.example）。
ENV_FILE_PATH = PROJECT_ROOT / ".env"

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

# 親密度 / 感情（affinity_service.py）用
# valence（機嫌）/ arousal（テンション）が中立値へ戻るまでの半減期（秒）。
# 30 分で「今の感情の揺れ」が半分に薄まる。
AFFINITY_HALF_LIFE_SECONDS = 1800.0
# 親密度・機嫌・テンションの中立値（初期値であり、減衰の収束先でもある）。
AFFINITY_NEUTRAL = 50
# 機嫌がこの値より高い返答では親密度を 1 上げる。
AFFINITY_RAISE_THRESHOLD = 70
# 機嫌がこの値より低い返答では親密度を 1 下げる。
AFFINITY_LOWER_THRESHOLD = 30
# 親密度・機嫌・テンションが取りうる範囲（この外へは丸める）。
AFFINITY_MIN = 0
AFFINITY_MAX = 100

HISTORY_LIMIT = 100
GEMINI_RESPONSE_TIMEOUT_SECONDS = 60.0
MODERATION_MODEL_NAME = "gemini-3.1-flash-lite"
MODERATION_TIMEOUT_SECONDS = 10.0
# モデレーションプロンプトに「参考」として載せる NG ワードの最大件数。
# 全件を載せるとプロンプトが肥大化してトークンを浪費するため先頭からこの件数だけ渡す。
# UI 側の DISCORD_SELECT_LABEL_MAX_LENGTH と数値は同じだが意味がまったく違うので別名にする。
MODERATION_NG_WORD_SAMPLE_LIMIT = 100

# 会話要約 (/functions → 📋 会話要約) 用
# 対象メッセージがこの件数を超えたら要約せず自動停止する（トークン超過・
# API 取得時間・レート制限の暴発を防ぐ安全弁）。
SUMMARY_MAX_MESSAGES = 1000
# 大量メッセージの要約は通常チャットより時間がかかるため長めに確保する。
SUMMARY_TIMEOUT_SECONDS = 120.0
# 要約へ渡す会話ログの時刻表記に使うタイムゾーン。
# 実行環境のローカル時刻（astimezone() の既定）に頼ると、海外リージョンの VPS では
# 議事録の時刻が日本の利用者の感覚とずれるため、用途ごとに明示して固定する
# （STATS_TIMEZONE / ANNIVERSARY_TIMEZONE / REMINDER_TIMEZONE と同じ方針）。
SUMMARY_TIMEZONE = "Asia/Tokyo"

MAX_DOCUMENT_SIZE_BYTES = 5 * 1024 * 1024
SUPPORTED_DOCUMENT_EXTENSIONS = {".pdf", ".txt", ".csv", ".md"}
# マルチモーダル入力として受け付ける画像の拡張子（Content-Type が空の添付の判定用）。
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
# Discord の1メッセージあたりの文字数上限。これを超える返信は split_message で分割する。
DISCORD_MESSAGE_MAX_LENGTH = 2000
DOCUMENT_UPLOAD_TIMEOUT_SECONDS = 60.0
DOCUMENT_SUMMARY_TIMEOUT_SECONDS = 60.0
DOCUMENT_SUMMARY_LIMIT = 12
# プロンプトへ差し込む文書要約の件数（新しいものから何件を文脈に載せるか）。
DOCUMENT_MEMORY_CONTEXT_LIMIT = 3
DOCUMENT_WORKER_IDLE_SECONDS = 5
# テキスト系文書（txt/csv/md）の妥当性検証で先頭から読むバイト数。
# 全体をデコードすると大きなファイルで無駄が大きいため、先頭だけを見て
# 「UTF-8 として読めるか」を確認する。
TEXT_VALIDATION_PREVIEW_BYTES = 4096
QUEUE_MAX_FINISHED_TASKS = 50
MAX_GROUNDING_SOURCES = 5
GROUNDING_MODEL_NAME = "gemini-2.5-flash"
# timeout未指定時の検索付き生成全体の期限（秒）。REST・SDKの全段で共有する。
GROUNDING_REQUEST_TIMEOUT_SECONDS = 60.0
# 検索ツール非対応を示す4xxの場合だけ、GROUNDING_MODEL_NAMEで再試行するか。
# True: 選択モデルREST → サブモデルREST → 選択モデルSDKの順で非対応を回避する。
# False: サブモデルを省いてSDKへ進む。期限切れ・5xx・認証・利用枠エラーは再試行しない。
GROUNDING_SUBMODEL_RETRY_ENABLED = True

# UsageTracker が保持するトークン履歴の上限（リングバッファ）
MAX_USAGE_HISTORY = 500

# matplotlib で日本語ラベルを描くためのフォント候補（先頭から見つかったものを使う）。
# 利用トークングラフと会話統計グラフで同じ見た目にするため、ここを唯一の情報源にする。
GRAPH_FONT_FAMILY = [
    "Yu Gothic", "Meiryo", "Noto Sans CJK JP", "Noto Sans JP", "IPAexGothic", "sans-serif",
]

# ── UI（View / セレクトメニュー）共通 ──
# 各パネル（View）の操作待ち時間（秒）。これを過ぎると UI を取り除いて時間切れ表示にする。
VIEW_TIMEOUT_SECONDS = 120.0
# Discord のセレクトメニューに載せられる選択肢の最大数（プラットフォームの制限）。
DISCORD_SELECT_MAX_OPTIONS = 25
# Discord のセレクト選択肢の label / description の最大文字数（プラットフォームの制限）。
DISCORD_SELECT_LABEL_MAX_LENGTH = 100
# リマインダーパネルの本文をここで打ち切る。Discord の 2000 文字制限に対し、
# 打ち切り案内の追記分を見込んで余裕を持たせた長さ。
REMINDER_PANEL_MAX_LENGTH = 1900

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

# --- 自律行動（ボイス/ステージへ自分から出て「配信」する） ---
# ボットが自分からボイス/ステージチャンネルへ入り、独り言を続けながら、
# そのチャンネル付属のチャットに届いたコメントへ配信者のように応答する機能。
# 有効/無効は /functions → 💬 自発会話 パネルの 🎙️ 自律行動 トグル（サーバーごと）。
#
# 「話したいゲージ」の満点までの時間は自発会話と同じ auto_topic_interval_hours を
# 共用する（設定項目を増やさず、パネル1枚で完結させるため）。

# 進行中の配信の面倒を見る見回りの間隔（分）。独り言の間隔・無応答での停止判定を
# この周期で確認するため、AUTONOMOUS_TALK_INTERVAL_MINUTES より細かくすること。
AUTONOMOUS_CHECK_MINUTES = 1.0
# 深夜の抑制と「話したいゲージ」の判定に使うタイムゾーン。
AUTONOMOUS_TIMEZONE = "Asia/Tokyo"
# 自律行動を禁止する時間帯（この時刻以上・未満。0〜6 なら 00:00〜05:59 が禁止）。
# ゲージは無会話時間で増えるため、誰も居ない深夜ほど早く満タンになってしまう。
# 「寝ている間にボイスチャンネルで喋り続ける」のを防ぐための歯止め。
AUTONOMOUS_QUIET_HOURS_START = 0
AUTONOMOUS_QUIET_HOURS_END = 6
# 「話したいゲージ」がこの値に達したら配信を始める（ゲージは 0〜100）。
AUTONOMOUS_GAUGE_THRESHOLD = 100.0
# ゲージの加点（直近の会話の活発さ）を数える時間窓（分）。
AUTONOMOUS_ACTIVITY_WINDOW_MINUTES = 180
# 時間窓内の人間の発言1件あたりの加点。人が話しているサーバーほど早く出たくなる。
AUTONOMOUS_ACTIVITY_BONUS_PER_MESSAGE = 2.0
# 加点の上限。基礎値（無会話時間）を加点だけで押し切らないための頭打ち。
AUTONOMOUS_ACTIVITY_BONUS_MAX = 30.0
# 加点のために覚えておく発言時刻の最大件数（サーバーごと。メモリの歯止め）。
AUTONOMOUS_ACTIVITY_SAMPLES_MAX = 100
# 独り言の間隔（分）。配信中はこの間隔でボットが自分から喋る。
AUTONOMOUS_TALK_INTERVAL_MINUTES = 3.0
# コメントを受け付けてから、次のコメントを受け付けるまでの待ち時間（秒）。
# 配信中のチャンネルではメンション不要でコメントを拾うため、何もしないと
# 「そのチャンネルの全メッセージ × (モデレーション + 応答生成)」で Gemini 呼び出しが
# 発散する（通常のメンション必須モードでは、メンションが自然な流量制限になっていた）。
# 入力AIの前に配信単位で枠を確保し、同じユーザーの連投にも適用する。
# 入力AIで拒否された場合も受付時刻は保持する。ローカルNG判定は枠の確保より前に行う。
# 0 以下にすると制限なし（全コメントに応答する）。
AUTONOMOUS_COMMENT_COOLDOWN_SECONDS = 10.0
# 本文が無く画像だけのコメントを受け取ったときに、本文の代わりとしてモデルへ渡す文。
# 通常のチャット経路が空の質問を「この画像について説明・反応してください。」へ
# 置き換えているのと同じ役割（置き換えないと、空のコメントへの返事を生成してしまう）。
AUTONOMOUS_IMAGE_ONLY_COMMENT = "（本文なしで画像が投稿されました。この画像に反応してください）"
# 配信の開始に失敗したあと、次に試すまでの基本待ち時間（分）。
# 失敗しても「話したいゲージ」は満タンのままなので、この歯止めが無いと
# 見回りのたび（＝毎分）に接続→失敗→切断を永久に繰り返す。
# 連続失敗のたびに「この値 × 連続回数」へ延びる（上限は下の倍率）。
AUTONOMOUS_START_RETRY_COOLDOWN_MINUTES = 30.0
# 上の待ち時間の倍率の上限。既定では 30分 × 8 = 最長4時間まで延びる。
# 権限設定を直したあと待たされすぎないよう、/functions のパネルで自律行動を
# 確定すると、この待ち時間はその場で解除される。
AUTONOMOUS_START_RETRY_MAX_MULTIPLIER = 8
# コメントがこの時間（分）届かなければ配信を自動終了する（idea.md の「30分」）。
AUTONOMOUS_IDLE_STOP_MINUTES = 30.0
# 1回の配信の最長時間（分）。コメントが続く限り無応答タイマーは伸び続けるため、
# 何時間もボイス接続と API 呼び出しを続けないための安全弁。
AUTONOMOUS_SESSION_MAX_MINUTES = 180.0
# 独り言・コメント応答のプロンプトに含めるチャンネル履歴の件数上限。
AUTONOMOUS_HISTORY_LIMIT = 30
# 独り言・コメント応答の Gemini 呼び出しタイムアウト（秒）。
AUTONOMOUS_TIMEOUT_SECONDS = 60.0
# ボイス接続の待ち時間の上限（秒）。接続できないチャンネルで見回りを止めないための歯止め。
AUTONOMOUS_CONNECT_TIMEOUT_SECONDS = 30.0
# ステージ参加後に「自分が発言者になれたか」を待つ回数と間隔（秒）。
# connect() 直後は自分のボイス状態がまだキャッシュに無く、
# Member.edit(suppress=False) が channel_id を載せられずに失敗することがある。
AUTONOMOUS_VOICE_STATE_RETRIES = 5
AUTONOMOUS_VOICE_STATE_WAIT_SECONDS = 0.5
# ボットが自分で開始したステージインスタンスのトピック（チャンネル一覧に表示される）。
# 「人間を誘う」ための看板なので、募集中であることが一目で分かる文言にする。
AUTONOMOUS_STAGE_TOPIC = "会話相手募集中…"
# 配信開始をチャンネルのチャットへ知らせる見出し。この下に生成した第一声を続ける。
AUTONOMOUS_OPENING_NOTICE = "🎙️ **会話相手募集中…**（ボットの自律行動）このチャットに書き込むと反応します。"
# 無応答で自動終了したときにチャットへ残す文言（Gemini は呼ばない）。
AUTONOMOUS_CLOSING_NOTICE = "🎙️ 反応が無くなったので配信を終了します。またね。"
# 最長時間に達して終了したときの文言。
AUTONOMOUS_CLOSING_NOTICE_TIMEUP = "🎙️ 長くなってきたので、いったん配信を終了します。またね。"
# 時間帯制限（深夜）に入って終了したときの文言。
AUTONOMOUS_CLOSING_NOTICE_QUIET = "🎙️ 夜も遅いので、今日はここまでにします。おやすみなさい。"
# パネルで自律行動を無効化されて終了したときの文言。
# 何も言わずに退出すると、聞いていた人からは落ちたのか止められたのか分からない。
AUTONOMOUS_CLOSING_NOTICE_DISABLED = "🎙️ 設定が変わったので、この配信はここまでにします。またね。"
# 別のチャンネルへ移動させられて終了したときの文言（元のチャンネルのチャットへ残す）。
AUTONOMOUS_CLOSING_NOTICE_MOVED = "🎙️ 別のチャンネルへ移動になったので、この配信はここで終わりにします。"

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
# 公開チャンネルへお祝いを投稿してよい記念日ラベルの許可リスト。
# 長期記憶の ANNIVERSARY_PROFILE_KEY（"anniversaries"）は記憶圧縮の LLM が
# 自由記述で書き込むため、「退院記念日」「離婚記念日」のように DM でだけ話した
# 私的な記念日が入りうる。それらをそのまま共有チャンネルで祝うと、本人が
# 公開するつもりのなかった事情が他の参加者へ露出してしまう。
# そのため、一般に祝われることが明らかなラベルだけを公開投稿の対象にする
# （ここに無いラベルは記憶としては保持するが、自動お祝いはしない）。
ANNIVERSARY_PUBLIC_LABELS = ("誕生日", "記念日")

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

# --- リマインダーの繰り返し ---
# 繰り返し種別の許容値。統合判定プロンプトが返す値の正規化に使う
# （MEMORY_ROUTE_CATEGORIES と同じ「許容値タプルを唯一の情報源にする」作法）。
REMINDER_RECURRENCE_VALUES = ("none", "daily", "weekly", "monthly")
# 繰り返し種別の表示名。確認文・一覧表示で使う。
# 今はユーザーが選ぶ UI が無い（会話から抽出する）ため choices.py ではなくここに置く。
# セレクトメニューを作ることになったら choices.py へ移すこと。
REMINDER_RECURRENCE_LABELS = {
    "none": "",
    "daily": "毎日",
    "weekly": "毎週",
    "monthly": "毎月",
}
# 停止中に積み残した繰り返しを「未来になるまで進める」ときの最大反復回数。
# 1分間隔の繰り返しは作れない（最短が毎日）ため、通常は数十回で足りる。
# 計算が前進しない不具合が入ったときに無限ループしないための歯止め。
REMINDER_RECURRENCE_MAX_CATCHUP_STEPS = 1000

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
# ユーザー選択セレクトに載せる最大人数（Discord のセレクトの上限がそのまま効く）。
STATS_MAX_USERS_IN_SELECT = DISCORD_SELECT_MAX_OPTIONS
# 統計解析（話題／性格推定）の LLM 呼び出しタイムアウト（秒）。
STATS_ANALYSIS_TIMEOUT_SECONDS = 30.0
# 時間帯ヒストグラムの集計に使うタイムゾーン（発言時刻はこの地域時刻に変換して集計）。
STATS_TIMEZONE = "Asia/Tokyo"
# 発言回数ランキング（管理者用の全体比較）に載せる人数の上限。
STATS_RANKING_TOP_N = 15
# 時間帯ヒストグラムの区切り数（0時〜23時の24区分）。
# 1時間単位の保存形式なので、区間数変更には記録処理と既存データの移行も必要。
STATS_HOURLY_BUCKETS = 24
# 新しく統計に載るユーザーの既定値。False = 収集する（従来どおり）。
# 「原則収集しない（オプトイン）」運用に切り替えたい場合はここを True にする。
STATS_OPT_OUT_DEFAULT = False

# --- リアクションによるフィードバック収集 ---
# 機能全体の有効/無効。False にするとリアクション付与も記録も行わない
# （返信のたびに API 呼び出しが2回増えるため、レート制限が厳しい環境では切れるようにする）。
FEEDBACK_ENABLED = True
# 良い/悪いを表す絵文字。ここ以外の絵文字が付いても無視する。
FEEDBACK_REACTION_GOOD = "👍"
FEEDBACK_REACTION_BAD = "👎"
# 評価対象として覚えておくボット返信の件数（インメモリのリングバッファ）。
# ここから溢れた古い返信へのリアクションは無視される（再起動でも消える）。
# 送信のたびに DB へ書くとチャットの主経路が重くなるため、あえて永続化しない。
FEEDBACK_TRACKED_MESSAGES_MAX = 500
# フィードバック記録の保持件数。古いものから捨てる（queue_tasks / reminders と同じ方針）。
FEEDBACK_MAX_STORED_ROWS = 2000
# 集計パネルに表示する内訳（モデル×ペルソナ）の最大行数。
FEEDBACK_SUMMARY_MAX_ROWS = 10
# 👍👎 の自動付与を間引くクールダウン（秒）。同じチャンネルで直近この秒数以内に
# 付与していれば今回は見送る（評価対象としての登録は全返信に対して行うため、
# 付与を見送った返信でもユーザーが自分で押せば記録される）。
# Discord のリアクション追加はチャンネル単位のレート制限が厳しく、しかも
# 「連投で詰まったときだけ」痛む。一律に N 回に1回へ間引くと痛くない場面の
# データまで等しく捨てるため、バーストのときだけ効くこの方式を採る。
# 0 以下にすると従来どおり全返信へ付与する。
FEEDBACK_REACTION_COOLDOWN_SECONDS = 60.0
# フィードバック記録の間引き（prune）を行う間隔（分）。
# 以前は評価が押されるたびに実行していたが、prune は全行走査のうえ
# database.py のプロセス唯一のロックを握るため、その間チャットの主経路
# （記憶・親密度・統計の読み書き）まで待たされていた。件数の上限は
# 「多少超過してもよい」性質なので、主経路から切り離して定期実行にする。
FEEDBACK_PRUNE_MINUTES = 60.0

# --- オンボーディング（サーバー参加時の案内・使い方パネル） ---
# サーバーに招待されたときにウェルカムメッセージを投稿するか。
# 静かに導入したい運用者（既に使い方を知っているメンバーだけのサーバーなど）向けに
# 切れるようにしておく。False にしてもヘルプ自体は失われず、
# /functions → ❓ 使い方 からいつでも読める。
WELCOME_MESSAGE_ENABLED = True

# ボイスチャンネルの読み上げ（/join・自律行動での接続中）用
# 退避先の gTTS に渡す言語コード（主エンジンの edge-tts は話者名で言語が決まる）。
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
# 音声合成（音声エンジンのネットワーク呼び出し＋デコード＋EQ）のタイムアウト（秒）。
# 読み上げはチャット本体の付随機能なので、詰まったら諦めて次へ進む。
# gTTS への退避もこの制限時間の内側で行われる。
TTS_TIMEOUT_SECONDS = 30.0
# 読み上げの再生完了待ちにかける上限。`voice_client.play()` の `after` コールバックは、
# ボイス接続が正常な `disconnect()` を経ずに切れると呼ばれないことがあり、無期限に
# 待つとそのサーバーの読み上げワーカーが永久に返らなくなる。
# 上限は「音声の実再生秒数 + この余裕秒数」とする。
TTS_PLAY_TIMEOUT_MARGIN_SECONDS = 15.0

# --- TTS の音声処理（デコード・EQ）用 ---
# バイクアッドフィルタを一度に処理するサンプル数。
# IIR フィルタは1つ前の「出力」を使う再帰計算のため numpy でベクトル化できず、
# Python のループで回すしかない。全サンプルを一気に list 化すると 30 秒の音声で
# 数十 MB の一時オブジェクトができてしまうため、メモリ 1GB の環境を想定して
# 固定長のブロックに区切る（フィルタの状態は次のブロックへ持ち越すので結果は同じ）。
TTS_BIQUAD_BLOCK_SIZE = 65536
# EQ プロファイルに frequency が無い / 数値でないときに使う既定の周波数（Hz）。
TTS_EQ_DEFAULT_FREQUENCY_HZ = 1000.0
# EQ の周波数の下限（Hz）。これより低いと係数が不安定になるため切り上げる。
TTS_EQ_MIN_FREQUENCY_HZ = 10.0
# EQ の周波数の上限を決める係数。上限は「サンプリング周波数 × この値」。
# 折り返し（エイリアシング）を避けるため、ナイキスト周波数（0.5倍）の手前に抑える。
TTS_EQ_NYQUIST_MARGIN = 0.45
# EQ プロファイルに gain_db が無い / 数値でないときに使う既定の増減量（dB）。0 は無変化。
TTS_EQ_DEFAULT_GAIN_DB = 0.0
# EQ プロファイルに q が無い / 数値でないときに使う既定の Q 値（クセのない標準的な鋭さ）。
TTS_EQ_DEFAULT_Q = 0.707
# Q 値の下限。0 に近づくと係数が発散するため、これ以下は切り上げる。
TTS_EQ_MIN_Q = 0.1
# ペルソナに eq が指定されていない / 未知の EQ 名だったときに使う既定の音色名
# （VOICE_EQ_PROFILES のキー。既定は加工なし）。
DEFAULT_VOICE_EQ_NAME = "flat"
# int16 で表せる範囲。EQ のブーストでこれを超えたまま変換すると音が割れるため、
# 必ずこの範囲へクリップしてから int16 に丸める。
PCM_INT16_MIN = -32768.0
PCM_INT16_MAX = 32767.0

# --- キャラクター別ボイスプロファイル ---
# 読み上げの声をペルソナごとに変える。声の高さ（pitch）と話す速さ（rate）は
# edge-tts のサーバー側パラメータが担当し、音色（eq）だけをこちらの DSP で作る。

# edge-tts が公開している日本語話者。無料エンドポイントではこの2人しか使えない
# （Azure 本家の Aoi / Daichi / Mayu などは含まれない）。
# 素の基本周波数の実測値: Nanami 240.6Hz / Keita 110.5Hz。
EDGE_TTS_VOICE_FEMALE = "ja-JP-NanamiNeural"
EDGE_TTS_VOICE_MALE = "ja-JP-KeitaNeural"

# pitch を振ったときに実際に何半音動くかの実測値（調整の目安。話者ごとに効き方が違う）。
#
#   Nanami : +20Hz -> +1.97半音   +40Hz -> +3.93半音   +60Hz -> +5.64半音
#            -20Hz -> -2.39半音   -40Hz -> -5.33半音
#            => おおよそ 10Hz ≒ 1半音。±40Hz までは半音に対してほぼ線形。
#
#   Keita  : +30Hz -> +2.41半音   +60Hz -> +5.19半音   +90Hz -> +6.35半音
#            -20Hz -> -0.49半音
#            => 上は +6半音付近で頭打ち。下方向はほとんど効かない。
#
# 「半音 → Hz」を計算で求めようとしないこと。Nanami は理論式の約1.5倍効き、
# Keita は約0.55倍しか効かず、しかも飽和するため換算式が成立しない。
# 値を変えるときは上の実測値を目安に Hz を直接書く。

# 音色（EQ）プロファイル。値は Audio EQ Cookbook のバイクアッド係数を組み立てる引数。
#   kind       : "high_shelf"（高域を上下させる）/ "low_pass"（高域を削る）/
#                "notch"（特定の帯域だけ深く削る）/ None（加工なし）
#   frequency  : 効き始める周波数（Hz）。出力（48kHz）基準でそのまま指定してよい。
#   gain_db    : シェルフの増減量（dB）。マイナスなら減衰として働く。
#   q          : 効き方の鋭さ。0.707 前後がクセのない標準的な値。
#   bit_crush  : 量子化ビット数（省略可）。小さいほどザラついた機械的な質感になる。
#
# 当初は warm にローパス 3.5kHz を使っていたが、実測で 8kHz 以上を
# 26.6dB も削っており、息づかいと子音が消えて電話・無線機の音質になっていた。
# 「そこから上を全部落とす」ローパスをやめ、「一定量だけ下げる」シェルフに変更。
# 同様に bright も +6dB では合成由来のノイズまで強調していたため半分に抑えた。
VOICE_EQ_PROFILES = {
    # 高域を軽く持ち上げて、若々しく張りのある明るい声にする。
    "bright": {"kind": "high_shelf", "frequency": 4000.0, "gain_db": 3.0, "q": 0.707},
    # 高域を軽く落として、落ち着きのある大人びた声にする。
    "warm": {"kind": "high_shelf", "frequency": 3000.0, "gain_db": -4.0, "q": 0.707},
    # 特定の帯域を深く削り、さらに量子化を粗くして無機質な響きにする。
    # 現在どのペルソナにも割り当てていない（将来ロボット系を追加した場合の受け皿）。
    "robotic": {"kind": "notch", "frequency": 1200.0, "gain_db": 0.0, "q": 1.0, "bit_crush": 6},
    # 加工なし（話者の素の声）。
    "flat": {"kind": None},
}

# ペルソナ（GuildSettings.current_prompt_file の値）ごとの声の設定。
#   voice : 話者。EDGE_TTS_VOICE_FEMALE / _MALE のどちらか。
#   pitch : 声の高さ。edge-tts のネイティブ形式（"+10Hz" のような符号つき整数）。
#   rate  : 話す速さ。同じくネイティブ形式（"+5%"）。長さは 1/(1+rate/100) 倍になる。
#   eq    : 音色。VOICE_EQ_PROFILES のキー。
# コメント末尾の数値は、この設定で実際に合成して測った基本周波数と長さ。
VOICE_PROFILES = {
    # 強気な口調と若さ。感嘆符の多さに合わせてやや早口に。（256.7Hz / 5.66秒）
    "../prompts/prompt_tsun.txt": {
        "voice": EDGE_TTS_VOICE_FEMALE, "pitch": "+10Hz", "rate": "+5%", "eq": "bright",
    },
    # 丁寧語ベースの落ち着いたトーンに、ねっとりした間を足して遅め。（235.3Hz / 6.46秒）
    "../prompts/prompt_devotion.txt": {
        "voice": EDGE_TTS_VOICE_FEMALE, "pitch": "-5Hz", "rate": "-8%", "eq": "warm",
    },
    # 感情の起伏を出さない大人の女性像として低め・ゆったり。（225.9Hz / 6.26秒）
    "../prompts/prompt_maid.txt": {
        "voice": EDGE_TTS_VOICE_FEMALE, "pitch": "-10Hz", "rate": "-5%", "eq": "warm",
    },
    # 幼さと高いテンションを強調（女性話者の中でもっとも高音・早口）。（287.4Hz / 5.42秒）
    "../prompts/prompt_innocent_girl.txt": {
        "voice": EDGE_TTS_VOICE_FEMALE, "pitch": "+30Hz", "rate": "+10%", "eq": "bright",
    },
    # 男の子なので男性話者。Keita の上限付近まで上げて幼さを出す。（158.4Hz / 4.94秒）
    "../prompts/prompt_innocent_boy.txt": {
        "voice": EDGE_TTS_VOICE_MALE, "pitch": "+90Hz", "rate": "+10%", "eq": "bright",
    },
    # 中年男性。話者そのものが男性なので加工は控えめ、高テンションを早口で。（117.1Hz / 5.16秒）
    "../prompts/prompt_middle_aged.txt": {
        "voice": EDGE_TTS_VOICE_MALE, "pitch": "+10Hz", "rate": "+5%", "eq": "flat",
    },
    # 「マジで」「ウケる」等の勢いのある口調に合わせ高め・早口。（265.2Hz / 5.42秒）
    "../prompts/prompt_gyaru.txt": {
        "voice": EDGE_TTS_VOICE_FEMALE, "pitch": "+15Hz", "rate": "+10%", "eq": "bright",
    },
    # からかうような高めのトーンで挑発感を強調。（271.2Hz / 5.66秒）
    "../prompts/prompt_cheeky.txt": {
        "voice": EDGE_TTS_VOICE_FEMALE, "pitch": "+20Hz", "rate": "+5%", "eq": "bright",
    },
    # 全プロファイル中もっともゆったり。包み込むような低めの声に。（217.7Hz / 6.60秒）
    "../prompts/prompt_ane.txt": {
        "voice": EDGE_TTS_VOICE_FEMALE, "pitch": "-15Hz", "rate": "-10%", "eq": "warm",
    },
    # 表面上の上品さ・優雅さをゆったりしたテンポで（高さは素のまま）。（241.2Hz / 6.60秒）
    "../prompts/prompt_kyoto.txt": {
        "voice": EDGE_TTS_VOICE_FEMALE, "pitch": "+0Hz", "rate": "-10%", "eq": "warm",
    },
    # 威圧感を出すため男性話者の素の低音のまま、間を詰めすぎない程度に遅め。
    # （Keita は下方向の pitch がほとんど効かないため素のまま使う）（113.4Hz / 5.71秒）
    "../prompts/prompt_greed.txt": {
        "voice": EDGE_TTS_VOICE_MALE, "pitch": "+0Hz", "rate": "-5%", "eq": "warm",
    },
    # 加工なしの素の声。他プロファイルの基準点。（241.2Hz / 5.95秒）
    "../prompts/prompt_default.txt": {
        "voice": EDGE_TTS_VOICE_FEMALE, "pitch": "+0Hz", "rate": "+0%", "eq": "flat",
    },
}
# 上の辞書に載っていないペルソナ（今後追加されたもの）が選ばれたときの既定値。
VOICE_PROFILE_FALLBACK = {
    "voice": EDGE_TTS_VOICE_FEMALE, "pitch": "+0Hz", "rate": "+0%", "eq": "flat",
}

