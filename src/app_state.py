import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from constants import (
    DEFAULT_AUTO_TOPIC_INTERVAL_HOURS,
    DEFAULT_MODEL_NAME,
    DEFAULT_PROMPT_FILE,
    DEFAULT_RESPOND_MODE,
    DM_SETTINGS_GUILD_KEY,
)


@dataclass
class AutonomousSession:
    """進行中の自律行動（ボイス/ステージでの「配信」）1件分の実行時状態。

    ボットが自分からボイス/ステージチャンネルへ入っている間だけ存在し、
    終了時に破棄される。ボイス接続そのものが再起動をまたげないため、
    この状態も **インメモリのみ**（永続化しない）。

    実際の接続は `guild.voice_client` が唯一の情報源なので、ここには
    VoiceClient を保持しない（保持すると、外部から切断されたときに
    実態と食い違った参照が残る）。
    """

    # 配信しているボイス/ステージチャンネル。コメントを受け付けるのもこのチャンネル
    # 付属のチャットだけ（他のテキストチャンネルからの発言はコメントとして扱わない）。
    channel_id: int
    is_stage: bool
    started_at: datetime
    # 最後に人間のコメントが届いた時刻。届かないまま
    # AUTONOMOUS_IDLE_STOP_MINUTES が過ぎたら配信を自動終了する。
    last_human_at: datetime
    # 最後にボットが喋った時刻（独り言の間隔を測るための基準）。
    last_spoke_at: datetime
    # ステージインスタンス（配信枠）を自分で開始したか。
    # 他の人が開いていたステージに相乗りした場合は False にし、終了時に閉じない。
    stage_started_by_bot: bool = False
    # 生成・発話中フラグ。見回りは1分ごとに来るため、前回の生成が終わる前に
    # 次の独り言が走らないようにする（同じチャンネルへの二重投稿を防ぐ）。
    busy: bool = False
    # 最後にコメントへ応答した時刻。コメントの拾いすぎを抑えるクールダウンの基準。
    # **last_spoke_at とは別に持つこと** — last_spoke_at は独り言でも更新されるため、
    # そちらを流用すると「独り言を聞いて反応した最初のコメント」を捨ててしまう
    # （いちばん拾いたいコメントなので、独り言の直後は必ず応答できるようにする）。
    last_comment_reply_at: datetime | None = None
    # これまでに喋った独り言の回数（プロンプトで「話し始めか続きか」を伝えるのに使う）。
    turn_count: int = 0


@dataclass(frozen=True)
class ResponseSettings:
    """1つの応答の生成・評価・読み上げで共用する、変更されない設定。"""

    prompt_file: str
    model_name: str
    grounding_enabled: bool


@dataclass
class GuildSettings:
    """1つの Discord サーバー（ギルド）に紐づく可変設定。

    ペルソナ・モデル・応答モード・グラウンディング・会話リセット地点・自発会話の
    設定はサーバーごとに独立させたいため、BotState 直下ではなくこちらに持たせる。
    DM（サーバーに属さない会話）はギルドが存在しないため、BotState.dm_settings に
    集約された「全DM共有の1スロット」として扱う（guild_id=None 相当）。
    """

    current_prompt_file: str = DEFAULT_PROMPT_FILE
    current_model_name: str = DEFAULT_MODEL_NAME
    current_respond_mode: str = DEFAULT_RESPOND_MODE
    grounding_enabled: bool = True
    # 会話リセット地点は「実行時に自然に作り直される一時状態」のため永続化しない。
    channel_reset_points: dict[int, int] = field(default_factory=dict)

    # --- 永続化のための紐づけ（BotState が attach() で設定する） ---
    # 自分がどのサーバーの設定かと、保存先の Database。DM は guild_id=None。
    # DB が未接続（None）のときは保存を試みない（DB を使わないテスト等で動くように）。
    guild_id: int | None = None
    _db: Any = field(default=None, repr=False, compare=False)

    # --- 自発的な会話 ---
    # 機能の有効/無効と無会話しきい値（時間）。/functions → 💬 自発会話 で変更する。
    auto_topic_enabled: bool = False
    auto_topic_interval_hours: int = DEFAULT_AUTO_TOPIC_INTERVAL_HOURS
    # 以下2つは監視の実行時状態。会話が続けば自然に再構築されるため永続化しない。
    # 監視対象チャンネル: ボットが会話に参加したチャンネル ID → 最後に人間の発言があった時刻
    auto_topic_channels: dict[int, datetime] = field(default_factory=dict)
    # チャンネル ID → 最後に自動で話題提供した時刻（沈黙が続く限り連投しないための記録）
    auto_topic_posted_at: dict[int, datetime] = field(default_factory=dict)

    # --- 自律行動（ボイス/ステージへ自分から出て「配信」する） ---
    # 機能の有効/無効。自発会話（テキストへの話題提供）とは独立して切り替えられる。
    # 「話したいゲージ」が満タンになるまでの時間は auto_topic_interval_hours を共用する
    # （設定項目を増やさず /functions → 💬 自発会話 パネル1枚で完結させるため）。
    autonomous_enabled: bool = False
    # 以下3つは実行時状態。会話が続けば自然に再構築されるため永続化しない。
    # 進行中の配信（無いときは None）。
    autonomous_session: AutonomousSession | None = None
    # 最後に人間が発言した時刻（このサーバーのどのチャンネルでもよい）。
    # 「話したいゲージ」の基礎値（無会話時間）を測る基準。None は「まだ観測していない」。
    autonomous_last_activity: datetime | None = None
    # 直近の人間の発言時刻（新しい順ではなく到着順）。ゲージの加点に使う。
    # AUTONOMOUS_ACTIVITY_WINDOW_MINUTES より古い分と、
    # AUTONOMOUS_ACTIVITY_SAMPLES_MAX を超えた分は追加時に捨てる。
    autonomous_recent_activity: list[datetime] = field(default_factory=list)
    # 配信の開始に失敗したときの再試行の抑制。失敗してもゲージは満タンのままなので、
    # これが無いと見回りのたび（＝毎分）に接続→失敗→切断を永久に繰り返す。
    # この時刻より前は開始を試みない。連続失敗回数に応じて待ち時間が延びる。
    autonomous_start_retry_at: datetime | None = None
    autonomous_start_failures: int = 0

    # --- 記念日・誕生日の自動お祝い ---
    # "{user_id}:{label}" → 最後にお祝いを投稿した日付（"YYYY-MM-DD"）。
    # 同じ記念日に同じサーバーで二重投稿しないための記録。DB にも保存され、
    # 起動時に復元される（当日に再起動しても同じお祝いを繰り返さないため）。
    anniversary_posted_dates: dict[str, str] = field(default_factory=dict)

    def snapshot_response(self) -> ResponseSettings:
        """非同期の処理を始める前に、今回の応答に使う設定を確定する。"""
        return ResponseSettings(
            prompt_file=self.current_prompt_file,
            model_name=self.current_model_name,
            grounding_enabled=self.grounding_enabled,
        )

    def attach(self, db: Any, guild_id: int | None) -> None:
        """永続化先の Database と自分のギルドIDを紐づける（BotState が生成時に呼ぶ）。"""
        self._db = db
        self.guild_id = guild_id

    @property
    def persist_key(self) -> int:
        """DB 上でこの設定を表すギルドID。DM はギルドIDが無いため 0 を使う。"""
        return DM_SETTINGS_GUILD_KEY if self.guild_id is None else self.guild_id

    def apply_persisted(self, values: dict[str, Any]) -> None:
        """DB から読み出した設定値を反映する（起動時の復元用）。

        値が空・欠落している項目は既定値のままにする（列を増やした直後の行など、
        中途半端なデータでボットが起動しなくなることを避けるため）。
        """
        self.current_prompt_file = values.get("prompt_file") or self.current_prompt_file
        self.current_model_name = values.get("model_name") or self.current_model_name
        self.current_respond_mode = values.get("respond_mode") or self.current_respond_mode
        if values.get("grounding_enabled") is not None:
            self.grounding_enabled = bool(values["grounding_enabled"])
        if values.get("auto_topic_enabled") is not None:
            self.auto_topic_enabled = bool(values["auto_topic_enabled"])
        if values.get("autonomous_enabled") is not None:
            self.autonomous_enabled = bool(values["autonomous_enabled"])
        interval = values.get("auto_topic_interval_hours")
        if isinstance(interval, int) and interval > 0:
            self.auto_topic_interval_hours = interval

    async def persist(self) -> None:
        """現在の設定値を DB へ保存する。設定を変更した UI 側が変更確定後に呼ぶ。

        保存に失敗してもチャットや UI 操作は止めない（フェイルオープン）。その場合、
        変更はインメモリには残るため、次回の再起動まではそのまま使える。
        """
        if self._db is None:
            return
        try:
            await asyncio.to_thread(
                self._db.guild_settings_save,
                self.persist_key,
                prompt_file=self.current_prompt_file,
                model_name=self.current_model_name,
                respond_mode=self.current_respond_mode,
                grounding_enabled=self.grounding_enabled,
                auto_topic_enabled=self.auto_topic_enabled,
                auto_topic_interval_hours=self.auto_topic_interval_hours,
                autonomous_enabled=self.autonomous_enabled,
            )
        except Exception as error:
            print(f"ギルド設定の保存に失敗しました (guild_id={self.guild_id}): {error}")

    async def record_anniversary_post(self, user_id: str, label: str, posted_date: str) -> None:
        """記念日のお祝いを投稿したことを、メモリと DB の両方へ記録する。"""
        self.anniversary_posted_dates[f"{user_id}:{label}"] = posted_date
        if self._db is None:
            return
        try:
            await asyncio.to_thread(
                self._db.anniversary_post_record, self.persist_key, user_id, label, posted_date
            )
        except Exception as error:
            print(f"記念日の投稿記録の保存に失敗しました (guild_id={self.guild_id}): {error}")


@dataclass
class BotState:
    # ギルドID → そのサーバー専用の設定。get_guild() が初回アクセス時に自動生成する。
    guild_settings: dict[int, GuildSettings] = field(default_factory=dict)
    # DM（ギルドに属さない会話）専用の設定。すべての DM で共有する単一スロット。
    dm_settings: GuildSettings = field(default_factory=GuildSettings)

    last_activity_timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # ウェルカムメッセージを投稿済みのサーバーID。**インメモリのみ**（永続化しない）。
    # on_guild_join は実際に招待されたときにしか発火せず、再起動・再接続では
    # 発火しないため、プロセスをまたぐ重複投稿は原理的に起きない。この集合は
    # 「同一プロセス内で万一イベントが重複して届いた場合」の保険にすぎないので、
    # auto_topic_channels と同じ「実行時に自然に再構築される一時状態」として扱う。
    welcomed_guild_ids: set[int] = field(default_factory=set)
    memory_background_started: bool = False
    document_worker_started: bool = False
    commands_synced: bool = False
    auto_topic_background_started: bool = False
    autonomous_background_started: bool = False
    anniversary_background_started: bool = False
    reminder_background_started: bool = False
    feedback_prune_background_started: bool = False

    # 設定の保存先。restore_from_database() で紐づけられる。
    _db: Any = field(default=None, repr=False, compare=False)

    def get_guild(self, guild_id: int | None) -> GuildSettings:
        """guild_id に対応する設定を返す。未登録のギルドはデフォルト値で自動生成する。

        DM（guild_id が None）は全 DM 共有の dm_settings を返す。
        新しく作った設定にも DB を引き継ぐため、そのまま persist() で保存できる。
        """
        if guild_id is None:
            return self.dm_settings
        settings = self.guild_settings.get(guild_id)
        if settings is None:
            settings = GuildSettings()
            settings.attach(self._db, guild_id)
            self.guild_settings[guild_id] = settings
        return settings

    def restore_from_database(self, db: Any) -> None:
        """Database を紐づけ、保存済みの設定と記念日の投稿記録をメモリへ復元する。

        起動時に1回だけ呼ぶ（bot.py）。読み込みに失敗しても既定値で起動を続ける
        （設定が戻らないだけで、ボット自体は動かせるようにするため）。
        """
        self._db = db
        self.dm_settings.attach(db, None)

        try:
            saved_settings = db.guild_settings_load_all()
            saved_posts = db.anniversary_posts_load_all()
        except Exception as error:
            print(f"保存済みのギルド設定の読み込みに失敗しました: {error}")
            return

        for guild_key, values in saved_settings.items():
            self._settings_for_key(guild_key).apply_persisted(values)
        for guild_key, posted in saved_posts.items():
            self._settings_for_key(guild_key).anniversary_posted_dates.update(posted)

        if saved_settings:
            print(f"データベース: {len(saved_settings)} 件のサーバー設定を復元しました。")

    def _settings_for_key(self, guild_key: int) -> GuildSettings:
        """DB 上のギルドキー（DM は 0）に対応する設定を返す。"""
        if guild_key == DM_SETTINGS_GUILD_KEY:
            return self.dm_settings
        return self.get_guild(guild_key)

    def iter_all_guild_settings(self):
        """(guild_id, GuildSettings) を全件返す。DM は guild_id=None として含む。

        記念日チェックなど「全サーバー + DM を横断して見回る」処理に使う。
        """
        yield None, self.dm_settings
        yield from self.guild_settings.items()

    def touch(self) -> None:
        self.last_activity_timestamp = datetime.now(timezone.utc)
