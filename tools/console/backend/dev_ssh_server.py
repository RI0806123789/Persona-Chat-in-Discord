"""開発用の模擬 SSH サーバー（本番サーバーに触らずに画面を試すためのもの）。

本物の SSH プロトコル（鍵認証・ホスト鍵の検証・コマンド実行・ストリーム）で通信し、
systemctl / journalctl / free などの「出力だけ」を作り物で返す。

起動:  .venv\\Scripts\\python.exe dev_ssh_server.py     （127.0.0.1:2222 で待ち受ける）

初回起動時に .dev/ フォルダへ次のファイルを作る（Git には入れないこと）:
- host_key          … このサーバーのホスト鍵
- client_key(.pub)  … ダッシュボードがログインに使う鍵
- known_hosts       … ダッシュボードがホスト鍵を検証するためのファイル
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncssh

DEV_DIR = Path(__file__).resolve().parent / ".dev"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 2222
SERVICE_NAME = "bot"

# 実際のボットが出すログに似せた文言（重み, 本文）
LIVE_LOG_TEMPLATES: list[tuple[int, str]] = [
    (14, "Gemini: 応答を生成しました（{model} / {sec:.2f}秒 / 入力 {tin:,} tok / 出力 {tout:,} tok）"),
    (6, "記憶圧縮: ユーザー 1 人分の pending_log {n} 件を profile へ統合しました。"),
    (5, "自発的な会話: #雑談 へ話題を投稿しました（無会話 6 時間）"),
    (4, "リマインダー: {n} 件を通知しました。"),
    (4, "文書処理: report_{n}.pdf の要約が完了しました（{sec:.1f}秒）"),
    (3, "自律行動: ステージ「雑談ステージ」で配信を開始しました"),
    (3, "自律行動: 配信を終了しました（理由: コメントが30分途絶えたため）"),
    (3, "TTS: edge-tts に失敗したため gTTS で読み上げます（ConnectionResetError）"),
    (2, "TTS: edge-tts が復旧しました。通常の読み上げに戻ります。"),
    (2, "自発的な会話: 話題の生成に失敗しました。次回の見回りで再試行します。"),
    (2, "警告: モデレーションがタイムアウトしたため、安全として扱います。"),
    (2, "ルーティング判定エラー: 503 UNAVAILABLE"),
    (1, "バックグラウンド処理エラー (stats.record): TimeoutError: "),
]
STARTUP_LOGS = [
    "データベース: スキーマは最新です（バージョン 10）",
    "ログイン成功: PersonaBot#0000",
    "スラッシュコマンドを同期しました（3 件）",
    "見回りループを開始しました（記憶 / 自発会話 / 自律行動 / 記念日 / リマインダー / 評価）",
    "文書ワーカーを起動しました。",
]
TRACEBACK_LOGS = [
    "Traceback (most recent call last):",
    '  File "/home/ubuntu/Persona-Chat-in-Discord/src/persona_bot/services/gemini_service.py", line 212, in generate',
    "google.genai.errors.ServerError: 503 UNAVAILABLE. The model is overloaded.",
]
MODELS = ["gemini-3.1-flash-lite", "gemini-3.5-flash", "gemini-2.5-flash"]


@dataclass
class FakeBot:
    """模擬サーバーの中の「ボット」の状態。"""

    active: bool = True
    pid: int = 41237
    restarts: int = 2
    memory_bytes: float = 182_000_000
    host_memory_used: float = 610_000_000
    boot_mono: float = field(default_factory=lambda: time.monotonic() - 9 * 86400 - 3600 * 5)
    started_mono: float = field(default_factory=lambda: time.monotonic() - 2 * 86400 - 3600 * 3 - 60 * 17)
    cpu_seconds_base: float = 5321.0
    stopped_at: float | None = None
    gemini_calls: int = 1342
    gemini_tokens: int = 1_820_000
    entries: list[dict] = field(default_factory=list)
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    connections: set[asyncssh.SSHServerConnection] = field(default_factory=set)
    _busy: asyncio.Lock = field(default_factory=asyncio.Lock)

    # ---------------------------------------------------------- ログ

    def log(self, message: str, *, source: str = "python", priority: int = 6, at: float | None = None) -> None:
        entry = {
            "__REALTIME_TIMESTAMP": str(int((at if at is not None else time.time()) * 1_000_000)),
            "PRIORITY": str(priority),
            "_PID": str(self.pid if source == "python" else 1),
            "SYSLOG_IDENTIFIER": source,
            "_SYSTEMD_UNIT": f"{SERVICE_NAME}.service",
            "MESSAGE": message,
        }
        self.entries.append(entry)
        del self.entries[:-2000]  # 古いものから捨てる
        for queue in list(self.subscribers):
            if queue.qsize() < 500:
                queue.put_nowait(entry)

    def seed_history(self) -> None:
        """起動時に「過去6時間分」のログを用意する。"""
        now = time.time()
        moments = sorted(now - random.uniform(60, 6 * 3600) for _ in range(140))
        self.log(f"Started {SERVICE_NAME}.service - Persona Chat in Discord bot.", source="systemd", at=moments[0] - 5)
        for message in STARTUP_LOGS:
            self.log(message, at=moments[0] - 4)
        for moment in moments:
            self.log(random_log_message(), at=moment)

    async def live_loop(self, interval: tuple[float, float]) -> None:
        while True:
            await asyncio.sleep(random.uniform(*interval))
            if not self.active:
                continue
            if random.random() < 0.04:
                for line in TRACEBACK_LOGS:
                    self.log(line, priority=3)
            else:
                self.log(random_log_message())

    # ---------------------------------------------------------- systemctl

    def service_show(self) -> str:
        now = time.monotonic()
        if self.active:
            self.memory_bytes = min(260e6, max(140e6, self.memory_bytes + random.uniform(-3e6, 3.4e6)))
            values = {
                "ActiveState": "active",
                "SubState": "running",
                "MainPID": str(self.pid),
                "MemoryCurrent": str(int(self.memory_bytes)),
                "ExecMainStartTimestampMonotonic": str(int((self.started_mono - self.boot_mono) * 1_000_000)),
                "CPUUsageNSec": str(int((self.cpu_seconds_base + (now - self.started_mono) * 0.031) * 1e9)),
            }
        else:
            values = {
                "ActiveState": "inactive",
                "SubState": "dead",
                "MainPID": "0",
                "MemoryCurrent": "[not set]",
                "ExecMainStartTimestampMonotonic": "0",
                "CPUUsageNSec": "[not set]",
            }
        values.update(
            {
                "Description": "Persona Chat in Discord bot",
                "LoadState": "loaded",
                "Result": "success",
                "UnitFileState": "enabled",
                "NRestarts": str(self.restarts),
            }
        )
        return "".join(f"{key}={value}\n" for key, value in values.items())

    async def control(self, action: str) -> None:
        async with self._busy:
            if action in ("stop", "restart") and self.active:
                self.log(f"Stopping {SERVICE_NAME}.service - Persona Chat in Discord bot...", source="systemd")
                await asyncio.sleep(0.8)
                self.active = False
                self.stopped_at = time.time()
                self.log(f"{SERVICE_NAME}.service: Deactivated successfully.", source="systemd")
                self.log(f"Stopped {SERVICE_NAME}.service - Persona Chat in Discord bot.", source="systemd")
            if action in ("start", "restart") and not self.active:
                await asyncio.sleep(0.5)
                self.active = True
                self.pid += random.randint(30, 400)
                self.started_mono = time.monotonic()
                self.cpu_seconds_base = 0.0
                self.memory_bytes = 150e6
                self.stopped_at = None
                self.gemini_calls = 0
                self.gemini_tokens = 0
                self.log(f"Started {SERVICE_NAME}.service - Persona Chat in Discord bot.", source="systemd")
                asyncio.get_running_loop().create_task(self._startup_logs())

    async def _startup_logs(self) -> None:
        for message in STARTUP_LOGS:
            await asyncio.sleep(random.uniform(0.3, 0.9))
            if self.active:
                self.log(message)

    # ---------------------------------------------------------- その他のコマンド

    def resources(self) -> str:
        uptime = time.monotonic() - self.boot_mono
        load1 = max(0.02, random.gauss(0.22 if self.active else 0.05, 0.08))
        total = 1_003_024_384
        self.host_memory_used = min(900e6, max(420e6, self.host_memory_used + random.uniform(-8e6, 8e6)))
        used = int(self.host_memory_used if self.active else self.host_memory_used - self.memory_bytes)
        available = total - used
        cache = 240_000_000
        free = max(0, available - cache)
        return (
            f"{uptime:.2f} {uptime * 1.7:.2f}\n"
            f"{load1:.2f} {load1 * 0.8:.2f} {load1 * 0.6:.2f} 2/187 {self.pid}\n"
            "2\n"
            "               total        used        free      shared  buff/cache   available\n"
            f"Mem:      {total} {used} {free} 1200000 {cache} {available}\n"
            "Swap:     2147479552 312000000 1835479552\n"
            " 47641534464 9831251968 37793505280\n"
        )


def random_log_message() -> str:
    weights = [weight for weight, _ in LIVE_LOG_TEMPLATES]
    template = random.choices([text for _, text in LIVE_LOG_TEMPLATES], weights=weights)[0]
    return template.format(
        model=random.choice(MODELS),
        sec=random.uniform(0.6, 4.2),
        tin=random.randint(800, 6000),
        tout=random.randint(80, 900),
        n=random.randint(1, 12),
    )


GIT_OUTPUT = (
    "main\n"
    "3840429\x1ffix: 最新mainを取り込み、会話要約の投稿失敗の案内を統合\x1f2026-10-16T21:14:05+09:00\n"
    "ver0.1.7-12-g3840429\n"
    "0\n"
)
SERVER_INFO_OUTPUT = "dev-mock-server\nLinux 6.8.0-1015-oracle\nUbuntu 24.04.1 LTS（模擬）\n"


def insights_output(bot: FakeBot) -> str:
    """remote_scripts/insights.py の出力の真似（Discord API・DB・ログから集めた状態）。"""
    now = time.time()

    def settings(persona, model, mode, grounding, auto_topic, interval, autonomous, saved=True):
        return {
            "persona": persona, "model": model, "respond_mode": mode, "grounding": grounding,
            "auto_topic_enabled": auto_topic, "auto_topic_interval_hours": interval,
            "autonomous_enabled": autonomous, "saved": saved,
        }

    return json.dumps({
        "errors": [],
        "source_dir": "src",
        "version": "ver0.1.7(BETA)",
        "autonomous_supported": True,
        "discord": {
            "ok": True,
            "error": None,
            "api_rtt_ms": random.randint(60, 140),
            "bot_user": "PersonaBot#0000",
            "guilds": [
                {"name": "Persona Lab", "members": 42, "online": 11, "voice_channel": "雑談ステージ" if bot.active else None,
                 "settings": settings("ツンデレ", "Gemini 3.1 Flash Lite", "メンションのみに反応", True, True, 6, True)},
                {"name": "雑談ラウンジ", "members": 71, "online": 19, "voice_channel": None,
                 "settings": settings("お姉さん", "Gemini 3.5 Flash", "すべての発言に反応", False, True, 3, True)},
                {"name": "テスト用サーバー", "members": 3, "online": 1, "voice_channel": None,
                 "settings": settings("標準", "Gemini 2.5 Flash", "メンションのみに反応", True, False, 6, False, saved=False)},
            ],
        },
        "dm_settings": settings("メイド", "Gemini 3.1 Flash Lite", "すべての発言に反応", False, False, 6, False),
        "activity": {
            "window_hours": 24,
            "gemini_calls": 214, "gemini_tokens": 512_340, "conversations": 188,
            "last_conversation_at": now - 140,
            "gateway_connects": 1, "gateway_resumes": 14, "gateway_invalidated": 0, "heartbeat_blocked": 0,
            "exceptions": 2,
            "autonomous_events": [
                {"at": now - 7400, "text": "配信を終了しました（理由: コメントが30分途絶えたため）"},
                {"at": now - 740, "text": "ステージ「雑談ステージ」で配信を開始しました"},
            ],
        },
        "generated_at": now,
    }, ensure_ascii=False)


def db_summary_output() -> str:
    good = 412 + random.randint(0, 3)
    return json.dumps(
        {
            "path": "/home/ubuntu/Persona-Chat-in-Discord/memory/bot.db",
            "exists": True,
            "errors": [],
            "size_bytes": 3_481_600,
            "wal_bytes": random.choice([0, 32_992, 164_832]),
            "schema_version": 10,
            "tables": {
                "affinity": 37,
                "anniversary_posts": 6,
                "feedback": good + 58,
                "guild_settings": 4,
                "pending_log": random.randint(3, 40),
                "profile": 121,
                "queue_tasks": 50,
                "reminders": 29,
                "user_stats": 41,
            },
            "reminders_by_status": {"pending": 7, "done": 19, "canceled": 3},
            "queue_by_status": {"done": 47, "failed": 3},
            "feedback": {"good": good, "bad": 58},
            "feedback_by_day": [
                {"day": (datetime.now() - timedelta(days=6 - offset)).strftime("%Y-%m-%d"), "good": g, "bad": b}
                for offset, (g, b) in enumerate([(21, 4), (34, 6), (18, 2), (27, 9), (31, 5), (40, 7), (16, 3)])
            ],
            "blacklist": {"users": 5, "blocked": 1},
        }
    )


# ---------------------------------------------------------------- SSH の受け口


async def handle_process(process: asyncssh.SSHServerProcess, bot: FakeBot) -> None:
    command = (process.command or "").strip()
    try:
        exit_code = await dispatch(command, process, bot)
    except (BrokenPipeError, ConnectionError, asyncssh.Error):
        return  # 相手が先に切った
    try:
        process.exit(exit_code)
    except (BrokenPipeError, ConnectionError, asyncssh.Error):
        pass


async def dispatch(command: str, process: asyncssh.SSHServerProcess, bot: FakeBot) -> int:
    if not command:
        process.stderr.write("dev-ssh-server: 対話シェルには対応していません。\n")
        return 1
    if command == "true":
        return 0
    if command.startswith(f"systemctl show {SERVICE_NAME} "):
        process.stdout.write(bot.service_show())
        return 0
    if match := re.fullmatch(r"sudo -n systemctl (start|stop|restart) (\S+)", command):
        if match.group(2) != SERVICE_NAME:
            process.stderr.write(f"Failed to {match.group(1)} {match.group(2)}.service: Unit not found.\n")
            return 5
        await bot.control(match.group(1))
        return 0
    if "journalctl" in command and f"-u {SERVICE_NAME} " in command:
        count_match = re.search(r"-n (\d+)", command)
        count = int(count_match.group(1)) if count_match else 10
        for entry in bot.entries[-count:]:
            process.stdout.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if command.endswith(" -f"):
            await follow_logs(process, bot)
        return 0
    if "/proc/loadavg" in command:
        process.stdout.write(bot.resources())
        return 0
    if "git rev-parse" in command:
        process.stdout.write(GIT_OUTPUT)
        return 0
    if command.startswith("hostname"):
        process.stdout.write(SERVER_INFO_OUTPUT)
        return 0
    if command.startswith("python3 - --insights "):
        await process.stdin.read()  # 集計スクリプトを最後まで受け取る（中身は実行しない）
        process.stdout.write(insights_output(bot) + "\n")
        return 0
    if command.startswith("python3 - "):
        await process.stdin.read()  # 集計スクリプトを最後まで受け取る（中身は実行しない）
        process.stdout.write(db_summary_output() + "\n")
        return 0
    process.stderr.write(f"dev-ssh-server: 未対応のコマンドです: {command}\n")
    return 127


async def follow_logs(process: asyncssh.SSHServerProcess, bot: FakeBot) -> None:
    """journalctl -f の真似。クライアントがチャンネルを閉じるまで新しいログを送り続ける。"""
    queue: asyncio.Queue = asyncio.Queue()
    bot.subscribers.add(queue)
    # クライアントがチャンネルを閉じると、標準入力の読み込みが終わる
    closed = asyncio.ensure_future(process.stdin.read())
    closed.add_done_callback(lambda future: future.cancelled() or future.exception())
    try:
        while True:
            getter = asyncio.ensure_future(queue.get())
            done, _ = await asyncio.wait({getter, closed}, return_when=asyncio.FIRST_COMPLETED)
            if closed in done:
                getter.cancel()
                return
            process.stdout.write(json.dumps(getter.result(), ensure_ascii=False) + "\n")
    finally:
        bot.subscribers.discard(queue)
        closed.cancel()


class _DevServer(asyncssh.SSHServer):
    def __init__(self, bot: FakeBot) -> None:
        self._bot = bot
        self._conn: asyncssh.SSHServerConnection | None = None

    def connection_made(self, conn: asyncssh.SSHServerConnection) -> None:
        self._conn = conn
        self._bot.connections.add(conn)  # サーバー側から切るときのために覚えておく
        peer = conn.get_extra_info("peername")
        print(f"[dev-ssh] 接続: {peer[0]}:{peer[1]}" if peer else "[dev-ssh] 接続")

    def connection_lost(self, exc: Exception | None) -> None:
        self._bot.connections.discard(self._conn)
        print(f"[dev-ssh] 切断{f'（{exc}）' if exc else ''}")

    def begin_auth(self, username: str) -> bool:
        return True  # 認証を必須にする（鍵認証のみ）

    def password_auth_supported(self) -> bool:
        return False


def ensure_keys(key_dir: Path) -> None:
    key_dir.mkdir(parents=True, exist_ok=True)
    host_key = key_dir / "host_key"
    client_key = key_dir / "client_key"
    if not host_key.exists():
        asyncssh.generate_private_key("ssh-ed25519", comment="dev-mock-server").write_private_key(host_key)
    if not client_key.exists():
        key = asyncssh.generate_private_key("ssh-ed25519", comment="persona-bot-console")
        key.write_private_key(client_key)
        key.write_public_key(key_dir / "client_key.pub")


def write_known_hosts(key_dir: Path, host: str, port: int) -> None:
    """ダッシュボードがこのサーバーのホスト鍵を検証するための known_hosts を作る。"""
    public_key = asyncssh.read_private_key(key_dir / "host_key").export_public_key().decode().split()
    key_text = " ".join(public_key[:2])  # 「鍵の種類 本体」だけ（コメントは不要）
    names = sorted({host, "127.0.0.1", "localhost"})
    lines = [f"[{name}]:{port} {key_text}" if port != 22 else f"{name} {key_text}" for name in names]
    (key_dir / "known_hosts").write_text("\n".join(lines) + "\n", encoding="utf-8")


async def start_dev_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    key_dir: Path = DEV_DIR,
    *,
    live_interval: tuple[float, float] = (2.0, 6.0),
) -> tuple[asyncssh.SSHAcceptor, FakeBot, int, asyncio.Task]:
    """模擬サーバーを起動して (受け口, 模擬ボット, 実際のポート, ログ生成タスク) を返す。"""
    ensure_keys(key_dir)
    bot = FakeBot()
    bot.seed_history()
    acceptor = await asyncssh.create_server(
        lambda: _DevServer(bot),
        host,
        port,
        server_host_keys=[str(key_dir / "host_key")],
        authorized_client_keys=str(key_dir / "client_key.pub"),
        process_factory=lambda process: handle_process(process, bot),
    )
    actual_port = acceptor.sockets[0].getsockname()[1]
    write_known_hosts(key_dir, host, actual_port)
    live_task = asyncio.get_running_loop().create_task(bot.live_loop(live_interval))
    return acceptor, bot, actual_port, live_task


async def main() -> None:
    acceptor, _bot, port, _live_task = await start_dev_server()
    print(f"開発用の模擬 SSH サーバーを起動しました: {DEFAULT_HOST}:{port}")
    print(f"鍵と known_hosts: {DEV_DIR}")
    print("ダッシュボードで「開発用の模擬サーバー」を選んで接続してください。終了は Ctrl+C。")
    await acceptor.wait_closed()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("模擬 SSH サーバーを停止しました。")
