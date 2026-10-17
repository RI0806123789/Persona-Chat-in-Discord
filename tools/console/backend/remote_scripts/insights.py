"""ボットのサーバー上で実行する集計スクリプト（ダッシュボードが SSH の標準入力で送り込む）。

ボットのコードを変えずに、次の3つから「ボットの中の状態」を集める。標準ライブラリだけで動く。
  1. Discord API … サーバー上の .env のトークンで読む（参加サーバー・人数・ボイス接続・ボット名）
  2. bot.db の guild_settings … 読み取り専用（mode=ro）で開く（サーバーごとの設定）
  3. journald のログ … 直近24時間（Gemini の呼び出し・会話の件数・Gateway の再接続・例外）

★ トークンは出力しない・コマンドライン引数にも載せない（ps で見えないように、ファイルから読む）。
★ 出力にサーバー / チャンネル / ユーザーの ID と会話の中身を含めない
  （ダッシュボードのスクリーンショットが Public の issue に貼られうるため）。
★ ボットのソースは import しない（中身が実行されるため）。ast で構文として読むだけ。

使い方: python3 - --insights <ボットのフォルダ> <サービス名>   （スクリプト本体は標準入力）
"""

import ast
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BOT_DIR = Path(sys.argv[2])
SERVICE = sys.argv[3]
JOURNAL_SUDO = "--journal-sudo" in sys.argv[4:]
# 回帰テストではネットワークに出ないよう、接続先を環境変数で差し替える
API_BASE = os.environ.get("PBC_DISCORD_API_BASE", "https://discord.com/api/v10")
USER_AGENT = "DiscordBot (persona-bot-console, 0.1)"

result = {"errors": []}


def note(message):
    result["errors"].append(message)


# ---------------------------------------------------------------- ボットのソースの場所と表示名


def working_dir():
    """systemd の WorkingDirectory（版によって src/ や index/）を調べる。"""
    try:
        value = subprocess.run(
            ["systemctl", "show", SERVICE, "-p", "WorkingDirectory", "--value"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        value = ""
    for candidate in (value, str(BOT_DIR / "src"), str(BOT_DIR / "index")):
        if candidate and Path(candidate).is_dir():
            return Path(candidate)
    return None


WORK_DIR = working_dir()


def find_source(*relative_paths):
    if WORK_DIR is None:
        return None
    for relative in relative_paths:
        path = WORK_DIR / relative
        if path.is_file():
            return path
    return None


def parse_file(path):
    try:
        return ast.parse(path.read_text(encoding="utf-8-sig"))
    except (OSError, SyntaxError, ValueError):
        note(f"{path.name} を読めませんでした。ファイルの文字コードと構文を確認してください。")
        return None


def read_constants(path):
    """`名前 = 文字列や数値` の形の定数だけを集める。"""
    values = {}
    tree = parse_file(path) if path else None
    for node in tree.body if tree else []:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Constant)):
            values[node.targets[0].id] = node.value.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and isinstance(node.value, ast.Constant):
            values[node.target.id] = node.value.value
    return values


CONSTANTS = read_constants(find_source("constants.py", "persona_bot/config/constants.py"))
CHOICES_TREE = None
_choices_path = find_source("choices.py", "persona_bot/config/choices.py")
if _choices_path:
    CHOICES_TREE = parse_file(_choices_path)


def choice_names(list_name):
    """choices.py の `XXX_CHOICES = [Choice(name=..., value=...), ...]` から {値: 表示名} を作る。"""
    names = {}
    for node in CHOICES_TREE.body if CHOICES_TREE else []:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or not isinstance(node.value, ast.List):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == list_name for target in targets):
            continue
        for call in node.value.elts:
            if not isinstance(call, ast.Call):
                continue
            keywords = {keyword.arg: keyword.value for keyword in call.keywords}
            name, value = keywords.get("name"), keywords.get("value")
            if isinstance(value, ast.Name):
                value = CONSTANTS.get(value.id)
            elif isinstance(value, ast.Constant):
                value = value.value
            else:
                value = None
            if isinstance(name, ast.Constant) and value is not None:
                names[value] = name.value
    return names


PERSONAS = choice_names("PERSONA_CHOICES")
MODELS = choice_names("MODEL_CHOICES")
MODES = choice_names("RESPOND_MODE_CHOICES")


def deployed_version():
    """README の更新履歴の先頭の見出し（例: ver0.1.6 — 2026/08/14）。"""
    try:
        for line in (BOT_DIR / "README.md").read_text(encoding="utf-8-sig").splitlines():
            match = re.match(r"^#{2,4}\s*(ver\d+\.\d+\.\d+.*)$", line.strip())
            if match:
                return match.group(1).strip()
    except OSError:
        pass
    return None


# ---------------------------------------------------------------- サーバーごとの設定（DB）


SETTINGS_COLUMNS = set()


def load_guild_settings():
    rows = {}
    db_path = BOT_DIR / "memory" / "bot.db"
    if not db_path.exists():
        note("bot.db がありません。")
        return rows
    try:
        con = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
        con.row_factory = sqlite3.Row
        try:
            SETTINGS_COLUMNS.update(row[1] for row in con.execute("PRAGMA table_info(guild_settings)"))
            for row in con.execute("SELECT * FROM guild_settings"):
                try:
                    rows[int(row["guild_id"])] = dict(row)
                except (TypeError, ValueError):
                    note("保存された設定の一部を読み取れませんでした。")
        finally:
            con.close()
    except sqlite3.Error:
        note("guild_settings を読めませんでした。DB の状態と読み取り権限を確認してください。")
    return rows


def describe_settings(row):
    """DB の行（無ければ空）を、既定値を補った表示用の設定にする。"""
    prompt_file = row.get("prompt_file") or CONSTANTS.get("DEFAULT_PROMPT_FILE") or ""
    model = row.get("model_name") or CONSTANTS.get("DEFAULT_MODEL_NAME") or ""
    mode = row.get("respond_mode") or CONSTANTS.get("DEFAULT_RESPOND_MODE") or ""

    def flag(key, default):
        value = row.get(key)
        return default if value is None else bool(value)

    if "autonomous_enabled" in SETTINGS_COLUMNS:
        autonomous = flag("autonomous_enabled", False)
    else:
        autonomous = None  # この版の DB には列が無い（自律行動に未対応の版）
    return {
        "persona": PERSONAS.get(prompt_file, Path(prompt_file).stem or None),
        "model": MODELS.get(model, model or None),
        "respond_mode": MODES.get(mode, mode or None),
        "grounding": flag("grounding_enabled", True),
        "auto_topic_enabled": flag("auto_topic_enabled", False),
        "auto_topic_interval_hours": row.get("auto_topic_interval_hours")
        or CONSTANTS.get("DEFAULT_AUTO_TOPIC_INTERVAL_HOURS"),
        "autonomous_enabled": autonomous,
        "saved": bool(row),
    }


# ---------------------------------------------------------------- Discord API


def read_token():
    token = os.environ.get("TOKEN_DISCORD")
    env_path = BOT_DIR / ".env"
    if not token:
        try:
            lines = env_path.read_text(encoding="utf-8-sig").splitlines()
        except (OSError, ValueError):
            return None
        for line in lines:
            key, separator, value = line.strip().removeprefix("export ").partition("=")
            if separator and key.strip() == "TOKEN_DISCORD":
                try:
                    words = shlex.split(value, comments=True)
                    token = words[0] if len(words) == 1 else None
                except ValueError:
                    note(".env の TOKEN_DISCORD の引用符を確認してください。")
    return token or None


class DiscordError(Exception):
    pass


def discord_get(path, token):
    request = urllib.request.Request(
        API_BASE + path,
        headers={"Authorization": "Bot " + token, "User-Agent": USER_AGENT},
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=6) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None, round((time.perf_counter() - started) * 1000)
        if error.code == 401:
            raise DiscordError("トークンが無効です（401）。") from None
        if error.code == 429:
            raise DiscordError("Discord API の利用制限中です（429）。少し待ってから更新してください。") from None
        raise DiscordError(f"Discord API がエラーを返しました（{error.code}）。") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise DiscordError("Discord API に接続できませんでした。接続先の状態を確認してください。") from None
    except (ValueError, UnicodeError):
        raise DiscordError("Discord API の応答を読み取れませんでした。時間をおいて更新してください。") from None
    return body, round((time.perf_counter() - started) * 1000)


def collect_discord(settings_rows):
    token = TOKEN

    def unavailable(message):
        # API が使えなくても、DB に保存済みの設定は ID を伏せた名前で表示する
        guilds = [
            {"name": f"保存済みサーバー {index}", "members": None, "online": None,
             "voice_channel": None, "voice_error": "Discord API からボイス状態を取得できませんでした。",
             "settings": describe_settings(row)}
            for index, row in enumerate((row for key, row in settings_rows.items() if key != 0), 1)
        ]
        return {"ok": False, "error": message, "guilds": guilds}

    if not token:
        return unavailable(".env に TOKEN_DISCORD がありません。")
    try:
        me, rtt_ms = discord_get("/users/@me", token)
        guilds, _ = discord_get("/users/@me/guilds?with_counts=true", token)
        if not isinstance(me, dict) or not isinstance(guilds, list):
            raise DiscordError("Discord API の応答を読み取れませんでした。")
        summaries = []
        voice_error = None
        for guild in guilds:
            guild_id = int(guild["id"])
            voice_channel = None
            if voice_error is None:
                try:
                    voice, _ = discord_get(f"/guilds/{guild_id}/voice-states/@me", token)
                    if voice and voice.get("channel_id"):
                        channel, _ = discord_get(f"/channels/{voice['channel_id']}", token)
                        voice_channel = (channel or {}).get("name") or "（名前を取得できないチャンネル）"
                except DiscordError as error:
                    voice_error = str(error)
                    note(f"ボイス状態を取得できませんでした。{voice_error}")
            summaries.append({
                "name": guild.get("name"),
                "members": guild.get("approximate_member_count"),
                "online": guild.get("approximate_presence_count"),
                "voice_channel": voice_channel,
                "voice_error": voice_error,
                "settings": describe_settings(settings_rows.get(guild_id, {})),
            })
    except DiscordError as error:
        return unavailable(str(error))
    except (KeyError, TypeError, ValueError, AttributeError):
        return unavailable("Discord API の応答を読み取れませんでした。")
    username = me.get("username") if me else None
    discriminator = me.get("discriminator") if me else None
    if username and discriminator and discriminator != "0":
        username = f"{username}#{discriminator}"
    return {"ok": True, "error": None, "api_rtt_ms": rtt_ms, "bot_user": username, "guilds": summaries}


# ---------------------------------------------------------------- ログ（直近24時間）

TOKEN_LINE = re.compile(r"^Total token count：(\d+)")
AUTONOMOUS_LINE = re.compile(r"^自律行動: .*(配信を開始しました|配信を終了しました)")


def journal(arguments):
    command = ["journalctl", "-u", SERVICE, "--no-pager", "-o", "json"] + arguments
    if JOURNAL_SUDO:
        command = ["sudo", "-n"] + command
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=30, errors="replace")
    except (OSError, subprocess.SubprocessError):
        note("journalctl を実行できませんでした。ログの読み取り権限を確認してください。")
        return []
    if completed.returncode != 0:
        note("ログを取得できませんでした。adm / systemd-journal グループ、または journal_sudo の設定を確認してください。")
    entries = []
    for line in completed.stdout.splitlines():
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        message = data.get("MESSAGE", "")
        if isinstance(message, list):
            try:
                message = bytes(message).decode("utf-8", errors="replace")
            except (TypeError, ValueError):
                continue
        try:
            timestamp = int(data.get("__REALTIME_TIMESTAMP", "0")) / 1_000_000
        except (TypeError, ValueError, OverflowError):
            timestamp = 0
        entries.append((timestamp, str(message or "")))
    return entries


def collect_activity():
    activity = {
        "window_hours": 24,
        "gemini_calls": 0,
        "gemini_tokens": 0,
        "conversations": 0,
        "last_conversation_at": None,
        "gateway_connects": 0,
        "gateway_resumes": 0,
        "gateway_invalidated": 0,
        "heartbeat_blocked": 0,
        "exceptions": 0,
        "autonomous_events": [],
    }
    for timestamp, message in journal(["--since", "-24h"]):
        if match := TOKEN_LINE.match(message):
            try:
                count = int(match.group(1))
            except ValueError:
                continue
            activity["gemini_calls"] += 1
            activity["gemini_tokens"] += count
        elif message.startswith("User ("):
            # 会話の中身は持ち帰らない（件数と時刻だけ）
            activity["conversations"] += 1
            activity["last_conversation_at"] = timestamp
        elif "has connected to Gateway" in message:
            activity["gateway_connects"] += 1
        elif "successfully RESUMED" in message:
            activity["gateway_resumes"] += 1
        elif "session has been invalidated" in message:
            activity["gateway_invalidated"] += 1
        elif "heartbeat blocked" in message:
            activity["heartbeat_blocked"] += 1
        elif message.startswith("Traceback (most recent call last)"):
            activity["exceptions"] += 1
        elif match := AUTONOMOUS_LINE.match(message):
            # 元の行にはサーバー ID・チャンネル名・終了理由があるため、イベントの種類だけ返す
            activity["autonomous_events"].append({"at": timestamp, "text": match.group(1)})

    if activity["last_conversation_at"] is None:
        # 24時間より前の最後の会話（-g は本文の検索。時刻だけを使う）
        latest = journal(["-r", "-n", "1", "-g", r"^User \("])
        if latest:
            activity["last_conversation_at"] = latest[0][0]
    activity["autonomous_events"] = activity["autonomous_events"][-10:]
    return activity


# ---------------------------------------------------------------- まとめて出力

settings_rows = load_guild_settings()
TOKEN = read_token()
result["source_dir"] = WORK_DIR.name if WORK_DIR else None
result["version"] = deployed_version()
result["autonomous_supported"] = "autonomous_enabled" in SETTINGS_COLUMNS
result["discord"] = collect_discord(settings_rows)
result["dm_settings"] = describe_settings(settings_rows.get(0, {}))
result["activity"] = collect_activity()
result["generated_at"] = time.time()
print(json.dumps(result))
