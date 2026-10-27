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
import queue
import re
import shlex
import sqlite3
import subprocess
import sys
import threading
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
# SSH 側の60秒の応答期限より前に、部分取得の結果を返す余裕を残す。
INSIGHTS_TIMEOUT_SECONDS = 50
DISCORD_TIMEOUT_SECONDS = 20
DISCORD_REQUEST_TIMEOUT_SECONDS = 6
DISCORD_GUILD_PAGE_SIZE = 200
DISCORD_MAX_GUILD_PAGES = 50
DISCORD_MAX_VOICE_GUILDS = 100
INSIGHTS_DEADLINE = time.monotonic() + INSIGHTS_TIMEOUT_SECONDS

result = {"errors": []}


def note(message):
    if message not in result["errors"]:
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


def text_constant(values, name, default):
    value = values.get(name)
    return value if isinstance(value, str) and value else default


# ---------------------------------------------------------------- ペルソナ
# ver0.1.8 以降のボット（persona_bot/config/personas.py がある版）は固定の選択肢を持たず、
# prompts/ の .txt と任意の prompts/personas.json から一覧と既定を作る。ここでは
# ボットを import せずに同じ規則を再現する。旧版は choices.py の選択肢をそのまま使う。

PERSONAS_SOURCE = find_source("persona_bot/config/personas.py")
PERSONA_CONSTANTS = read_constants(PERSONAS_SOURCE)
PERSONA_VALUE_PREFIX = text_constant(CONSTANTS, "PERSONA_VALUE_PREFIX", "../prompts/")
PROMPT_TEMPLATE_FILE_NAME = text_constant(CONSTANTS, "PROMPT_TEMPLATE_FILE_NAME", "00_prompt_format.txt")
BUILTIN_PERSONA_NAME = text_constant(PERSONA_CONSTANTS, "BUILTIN_PERSONA_DISPLAY_NAME", "標準（組み込み）")
_select_limit = CONSTANTS.get("DISCORD_SELECT_VALUE_MAX_LENGTH")
SELECT_VALUE_MAX_LENGTH = (
    _select_limit if isinstance(_select_limit, int) and not isinstance(_select_limit, bool) and _select_limit > 0 else 100
)


def is_persona_file_name(name):
    """ボットの personas._is_persona_file_name と同じ判定（テンプレート・隠し・.txt 以外を除く）。"""
    return (
        bool(name)
        and "/" not in name
        and "\\" not in name
        and not name.startswith(".")
        and name != PROMPT_TEMPLATE_FILE_NAME
        and Path(name).suffix.lower() == ".txt"
    )


def persona_file_name(value):
    """保存値がペルソナの形（"../prompts/<名前>.txt"）ならファイル名、違えば None。"""
    if not isinstance(value, str) or not value.startswith(PERSONA_VALUE_PREFIX):
        return None
    name = value[len(PERSONA_VALUE_PREFIX):]
    return name if is_persona_file_name(name) else None


def load_persona_settings():
    """prompts/personas.json を読む。無い・読めない場合は空にする（ボットも無視して続行する）。"""
    path = BOT_DIR / "prompts" / "personas.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        note("prompts/personas.json を読めませんでした。JSON の形式と読み取り権限を確認してください。")
        return {}
    if not isinstance(data, dict):
        note("prompts/personas.json の最上位がオブジェクトではありません。")
        return {}
    return data


def load_persona_catalog():
    """現行版のボットと同じ規則で、ペルソナの {値: 表示名}（一覧の順）と既定の値を作る。

    ボットは起動時とキャラ設定パネルを開いたときに読み直した一覧（スナップショット）で
    応答する。ここでは今のファイル構成から推定するため、その後に足したり消したりした
    ファイルは、ボットにはまだ反映されていないことがある。prompts/ を読めない場合は
    None を返し、推定しない。
    """
    prompts = BOT_DIR / "prompts"
    files = set()
    try:
        if prompts.is_dir():
            for path in prompts.iterdir():
                name = path.name
                # ボットはセレクトの値の上限を超える名前を選択肢から外す
                if (is_persona_file_name(name) and len(PERSONA_VALUE_PREFIX + name) <= SELECT_VALUE_MAX_LENGTH
                        and path.is_file()):
                    files.add(name)
    except OSError:
        note("prompts/ を読めませんでした。読み取り権限を確認してください。")
        return None
    settings = load_persona_settings()
    entries = settings.get("personas")
    entries = entries if isinstance(entries, dict) else {}
    # personas.json に書いた順を優先し、書いていないファイルはファイル名の順に続ける
    ordered = [name for name in entries if name in files] + sorted(name for name in files if name not in entries)
    names = {}
    for name in ordered:
        entry = entries.get(name)
        display = entry.get("name") if isinstance(entry, dict) else None
        display = " ".join(display.split()) if isinstance(display, str) else ""
        names[PERSONA_VALUE_PREFIX + name] = display or Path(name).stem
    default = settings.get("default")
    if isinstance(default, str) and PERSONA_VALUE_PREFIX + default in names:
        default_value = PERSONA_VALUE_PREFIX + default
    else:
        # default が無い・見つからないときは先頭、1つも無ければ空（組み込みの標準）
        default_value = next(iter(names), "")
    return names, default_value


PERSONA_CATALOG = load_persona_catalog() if PERSONAS_SOURCE else None
LEGACY_PERSONAS = choice_names("PERSONA_CHOICES")
MODELS = choice_names("MODEL_CHOICES")
MODES = choice_names("RESPOND_MODE_CHOICES")
AUTO_TOPIC_INTERVALS = choice_names("AUTO_TOPIC_INTERVAL_CHOICES")


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


def describe_persona(row):
    """保存値と、ボットが応答に使うペルソナ（現行版はファイル構成からの推定）をまとめる。

    persona_source は saved（保存した選択）・default（未選択なので既定）・
    missing（保存したファイルが見つからないので既定）。ボットは見つからない保存値を
    消さずに既定で応答するため、保存値（persona_missing）と実効の表示名を分けて返す。
    """
    saved = row.get("prompt_file")
    saved = saved if isinstance(saved, str) else ""
    if PERSONA_CATALOG is None:
        # 旧版（または prompts/ を読めない）: 保存値か既定（DEFAULT_PROMPT_FILE）の表示名
        value = saved or text_constant(CONSTANTS, "DEFAULT_PROMPT_FILE", "")
        return {
            "persona": LEGACY_PERSONAS.get(value, Path(value).stem or None),
            "persona_source": "saved" if saved else "default",
            "persona_missing": None,
            "persona_builtin": False,
        }
    names, default_value = PERSONA_CATALOG
    if saved in names:
        effective, source = saved, "saved"
    else:
        effective = default_value
        # ペルソナの形でない保存値は、ボットが復元時に破棄して既定へ戻す
        source = "missing" if persona_file_name(saved) else "default"
    return {
        "persona": names[effective] if effective else BUILTIN_PERSONA_NAME,
        "persona_source": source,
        "persona_missing": Path(persona_file_name(saved)).stem if source == "missing" else None,
        "persona_builtin": not effective,
    }


def describe_settings(row):
    """DB の行（無ければ空）を、ボットと同じ選択肢・型の検証で実効の設定にする。"""
    model = row.get("model_name")
    if not isinstance(model, str) or model not in MODELS:
        model = CONSTANTS.get("DEFAULT_MODEL_NAME") or ""
    mode = row.get("respond_mode")
    if not isinstance(mode, str) or mode not in MODES:
        mode = CONSTANTS.get("DEFAULT_RESPOND_MODE") or ""
    interval = row.get("auto_topic_interval_hours")
    if (not isinstance(interval, int) or isinstance(interval, bool)
            or interval not in AUTO_TOPIC_INTERVALS):
        interval = CONSTANTS.get("DEFAULT_AUTO_TOPIC_INTERVAL_HOURS")

    def flag(key, default):
        value = row.get(key)
        return default if value is None else bool(value)

    if "autonomous_enabled" in SETTINGS_COLUMNS:
        autonomous = flag("autonomous_enabled", False)
    else:
        autonomous = None  # この版の DB には列が無い（自律行動に未対応の版）
    return {
        **describe_persona(row),
        "model": MODELS.get(model, model or None),
        "respond_mode": MODES.get(mode, mode or None),
        "grounding": flag("grounding_enabled", True),
        "auto_topic_enabled": flag("auto_topic_enabled", False),
        "auto_topic_interval_hours": interval,
        "autonomous_enabled": autonomous,
        "saved": bool(row),
    }


# ---------------------------------------------------------------- Discord API


def read_token():
    token = os.environ.get("TOKEN_DISCORD")
    env_path = BOT_DIR / ".env"
    if token and token.startswith("YOUR_"):
        token = None
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
                    candidate = words[0] if len(words) == 1 else ""
                    if candidate and not candidate.startswith("YOUR_"):
                        token = candidate
                except ValueError:
                    note(".env の TOKEN_DISCORD の引用符を確認してください。")
    return token or None


class DiscordError(Exception):
    pass


class DiscordRateLimit(DiscordError):
    """全サーバーの追加取得を止める利用制限。"""


class DiscordDeadline(DiscordError):
    """Discord の追加取得に使える時間が残っていない。"""


def discord_get(path, token, *, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DiscordDeadline("Discord API の取得時間の上限に達しました。時間をおいて更新してください。")
    request = urllib.request.Request(
        API_BASE + path,
        headers={"Authorization": "Bot " + token, "User-Agent": USER_AGENT},
    )
    started = time.perf_counter()
    completed = queue.Queue(maxsize=1)

    def read_response():
        # urlopen の timeout は個々の受信待ちに適用される。細切れの応答でも全体の期限を越えない。
        # 期限後の受信スレッドは結果を表示せず、スクリプトの終了も妨げない。
        try:
            with urllib.request.urlopen(request, timeout=min(DISCORD_REQUEST_TIMEOUT_SECONDS, remaining)) as response:
                body = json.loads(response.read().decode("utf-8"))
            completed.put((body, None))
        except Exception as error:
            completed.put((None, error))

    threading.Thread(target=read_response, daemon=True).start()
    try:
        body, error = completed.get(timeout=remaining)
        if error:
            raise error
        if time.monotonic() >= deadline:
            raise DiscordDeadline("Discord API の取得時間の上限に達しました。時間をおいて更新してください。")
    except queue.Empty:
        raise DiscordDeadline("Discord API の取得時間の上限に達しました。時間をおいて更新してください。") from None
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None, round((time.perf_counter() - started) * 1000)
        if error.code == 401:
            raise DiscordError("トークンが無効です（401）。") from None
        if error.code == 429:
            raise DiscordRateLimit("Discord API の利用制限中です（429）。少し待ってから更新してください。") from None
        raise DiscordError(f"Discord API がエラーを返しました（{error.code}）。") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise DiscordError("Discord API に接続できませんでした。接続先の状態を確認してください。") from None
    except (ValueError, UnicodeError):
        raise DiscordError("Discord API の応答を読み取れませんでした。時間をおいて更新してください。") from None
    return body, round((time.perf_counter() - started) * 1000)


def discord_guild_id(guild):
    """ID を浮動小数点へ変換せず照合する。ID 自体は出力しない。"""
    value = guild.get("id") if isinstance(guild, dict) else None
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise DiscordError("Discord API のサーバー識別子を読み取れませんでした。")
    text = str(value)
    if len(text) > 20 or not text.isascii() or not text.isdecimal() or not 0 < int(text) < 2 ** 64:
        raise DiscordError("Discord API のサーバー識別子を読み取れませんでした。")
    return int(text)


def collect_discord_guilds(token, *, deadline):
    guilds = {}
    after = 0
    try:
        for _ in range(DISCORD_MAX_GUILD_PAGES):
            path = f"/users/@me/guilds?with_counts=true&limit={DISCORD_GUILD_PAGE_SIZE}"
            if after:
                path += f"&after={after}"
            page, _ = discord_get(path, token, deadline=deadline)
            if not isinstance(page, list) or len(page) > DISCORD_GUILD_PAGE_SIZE:
                raise DiscordError("Discord API のサーバー一覧を読み取れませんでした。")
            if not page:
                return guilds, None
            # ページ全体を確認してから取り込む。途中の不正値で既存ページを失わない。
            entries = [(discord_guild_id(guild), guild) for guild in page]
            cursor = max(guild_id for guild_id, _ in entries)
            if cursor <= after:
                raise DiscordError("Discord API のサーバー一覧の取得が進まないため中止しました。")
            for guild_id, guild in entries:
                guilds.setdefault(guild_id, guild)
            if len(page) < DISCORD_GUILD_PAGE_SIZE:
                return guilds, None
            after = cursor
        raise DiscordError("サーバー一覧の取得上限に達しました。全件は取得できていません。")
    except DiscordError as error:
        return guilds, error


def collect_discord(settings_rows):
    token = TOKEN
    deadline = min(INSIGHTS_DEADLINE, time.monotonic() + DISCORD_TIMEOUT_SECONDS)

    def saved_guilds(excluded=()):
        # API が使えなくても、DB に保存済みの設定は ID を伏せた名前で表示する
        return [
            {"name": f"保存済みサーバー {index}", "members": None, "online": None,
             "voice_channel": None, "voice_error": "Discord API からボイス状態を取得できませんでした。",
             "settings": describe_settings(row)}
            for index, row in enumerate((row for key, row in settings_rows.items() if key != 0 and key not in excluded), 1)
        ]

    def unavailable(message):
        return {"ok": False, "partial": False, "error": message, "guilds": saved_guilds()}

    if not token:
        return unavailable(".env に TOKEN_DISCORD がありません。")
    try:
        me, rtt_ms = discord_get("/users/@me", token, deadline=deadline)
        if not isinstance(me, dict):
            raise DiscordError("Discord API の応答を読み取れませんでした。")
        guilds, list_error = collect_discord_guilds(token, deadline=deadline)
        if list_error and not guilds:
            return unavailable(str(list_error))
        summaries = []
        rate_error = str(list_error) if isinstance(list_error, (DiscordRateLimit, DiscordDeadline)) else None
        for index, (guild_id, guild) in enumerate(guilds.items()):
            voice_error = rate_error
            if voice_error is None and index >= DISCORD_MAX_VOICE_GUILDS:
                voice_error = "ボイス状態の取得上限に達したため、このサーバーの確認を省略しました。"
            voice_channel = None
            if voice_error is None:
                try:
                    voice, _ = discord_get(f"/guilds/{guild_id}/voice-states/@me", token, deadline=deadline)
                    if voice and voice.get("channel_id"):
                        channel, _ = discord_get(f"/channels/{voice['channel_id']}", token, deadline=deadline)
                        voice_channel = (channel or {}).get("name") or "（名前を取得できないチャンネル）"
                except DiscordError as error:
                    voice_error = str(error)
                    if isinstance(error, (DiscordRateLimit, DiscordDeadline)):
                        rate_error = voice_error
                    note(f"ボイス状態を取得できませんでした。{voice_error}")
            summaries.append({
                "name": guild.get("name") if isinstance(guild.get("name"), str) else "名前不明のサーバー",
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
    if list_error:
        summaries.extend(saved_guilds(guilds))
    return {"ok": list_error is None, "partial": list_error is not None, "error": str(list_error) if list_error else None,
            "api_rtt_ms": rtt_ms, "bot_user": username, "guilds": summaries}


# ---------------------------------------------------------------- ログ（直近24時間）

TOKEN_LINE = re.compile(r"^Total token count：(\d+)")
AUTONOMOUS_LINE = re.compile(r"^自律行動: .*(配信を開始しました|配信を終了しました)")


def journal(arguments):
    """取得成功なら行の一覧（0件を含む）、実行・権限・期限の失敗なら None を返す。"""
    command = ["journalctl", "-u", SERVICE, "--no-pager", "-o", "json"] + arguments
    if JOURNAL_SUDO:
        command = ["sudo", "-n"] + command
    try:
        remaining = INSIGHTS_DEADLINE - time.monotonic()
        if remaining <= 0:
            note("インサイトの取得時間の上限に達したため、ログの追加取得を省略しました。")
            return None
        completed = subprocess.run(command, capture_output=True, text=True, timeout=min(30, remaining), errors="replace")
    except (OSError, subprocess.SubprocessError):
        note("journalctl を実行できませんでした。ログの読み取り権限を確認してください。")
        return None
    # -g の該当行なしは終了コード1。stderr があるときは権限・sudo 等の失敗として残す。
    no_matches = completed.returncode == 1 and "-g" in arguments and not completed.stderr.strip()
    if completed.returncode != 0 and not no_matches:
        note("ログを取得できませんでした。adm / systemd-journal グループ、または journal_sudo の設定を確認してください。")
        return None
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
    entries = journal(["--since", "-24h"])
    for timestamp, message in entries or []:
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

    if entries is not None and activity["last_conversation_at"] is None:
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
# ペルソナを prompts/ のファイル構成から推定したか（現行版で prompts/ を読めたとき）
result["persona_estimated"] = PERSONA_CATALOG is not None
result["discord"] = collect_discord(settings_rows)
result["dm_settings"] = describe_settings(settings_rows.get(0, {}))
result["activity"] = collect_activity()
result["generated_at"] = time.time()
print(json.dumps(result))
