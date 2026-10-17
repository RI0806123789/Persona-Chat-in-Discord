"""SSH で実行するコマンドの組み立てと、その出力の読み取り（パース）。

★ ここに書いたコマンドだけがサーバーで実行される（ホワイトリスト方式）。
  ブラウザから届いた文字列をそのままコマンドに埋め込まないこと。
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

# systemctl show で取り出す項目
SERVICE_PROPERTIES = (
    "Description",
    "LoadState",
    "ActiveState",
    "SubState",
    "Result",
    "UnitFileState",
    "MainPID",
    "NRestarts",
    "MemoryCurrent",
    "CPUUsageNSec",
    "ExecMainStartTimestampMonotonic",
)

# サービス操作として許可する動作。これ以外は実行しない
SERVICE_ACTIONS = ("start", "stop", "restart")

# 一度に取得するログの最大行数
MAX_LOG_LINES = 1000

# systemd が「値なし」を表すときに使う数値（2^64 - 1）
_SYSTEMD_UNSET = 18446744073709551615
_SAFE_NAME = re.compile(r"^[A-Za-z0-9@._-]+$")

# 疎通確認（何もしないコマンド。往復時間の計測に使う）
PING_COMMAND = "true"

# 接続直後に1回だけ取るサーバー情報（ホスト名 / カーネル / OS）
SERVER_INFO_COMMAND = (
    "hostname; uname -sr; "
    "grep '^PRETTY_NAME=' /etc/os-release | cut -d= -f2- | tr -d '\"'"
)

# CPU 負荷・メモリ・ディスクを1回のコマンドでまとめて取る
RESOURCES_COMMAND = (
    "cat /proc/uptime /proc/loadavg; nproc; free -b; "
    "df -B1 --output=size,used,avail / | tail -n 1"
)


def _check_safe_name(value: str) -> str:
    if not _SAFE_NAME.match(value):
        raise ValueError(f"使えない文字を含む名前です: {value!r}")
    return value


# ---------------------------------------------------------------- コマンドの組み立て


def service_show_command(service: str) -> str:
    properties = ",".join(SERVICE_PROPERTIES)
    return f"systemctl show {_check_safe_name(service)} --no-pager --property={properties}"


def service_control_command(service: str, action: str) -> str:
    """サービスの起動・停止・再起動。

    sudo -n は「パスワードを聞かずに失敗する」指定。付けないと、パスワードの入力待ちで
    コマンドが返ってこなくなる。
    """
    if action not in SERVICE_ACTIONS:
        raise ValueError(f"許可されていない操作です: {action!r}")
    return f"sudo -n systemctl {action} {_check_safe_name(service)}"


def journal_command(service: str, *, lines: int, follow: bool, use_sudo: bool) -> str:
    """journald からボットのログを JSON 形式で取り出す（1行が1件）。"""
    lines = max(1, min(int(lines), MAX_LOG_LINES))
    prefix = "sudo -n " if use_sudo else ""
    follow_flag = " -f" if follow else ""
    return f"{prefix}journalctl -u {_check_safe_name(service)} -n {lines} -o json --no-pager{follow_flag}"


def git_command(bot_dir: str) -> str:
    """ブランチ / 最新コミット / タグ / 未コミットのファイル数 を1行ずつ出す。"""
    directory = shlex.quote(bot_dir)
    return (
        f"cd {directory} && git rev-parse --abbrev-ref HEAD"
        " && git log -1 --format=%h%x1f%s%x1f%cI"
        " && (git describe --tags --always 2>/dev/null || echo -)"
        " && git status --porcelain | wc -l"
    )


def insights_command(bot_dir: str, service: str) -> str:
    """remote_scripts/insights.py（INSIGHTS_SCRIPT）を標準入力から python3 に渡して実行する。"""
    return f"python3 - --insights {shlex.quote(bot_dir)} {_check_safe_name(service)}"


def db_summary_command(bot_dir: str) -> str:
    """DB の集計スクリプト（DB_SUMMARY_SCRIPT）を標準入力から python3 に渡して実行する。"""
    base = bot_dir.rstrip("/")
    db_path = shlex.quote(f"{base}/memory/bot.db")
    blacklist_path = shlex.quote(f"{base}/ng/blacklist.json")
    return f"python3 - {db_path} {blacklist_path}"


# ボットの中の状態（Discord API・サーバーごとの設定・直近24時間のログ）を集めるスクリプト。
# 中身は remote_scripts/insights.py（サーバー側で標準入力から実行する）。
INSIGHTS_SCRIPT = (Path(__file__).resolve().parent / "remote_scripts" / "insights.py").read_text(encoding="utf-8")

# サーバー側で動かす集計スクリプト（標準ライブラリだけで動く）。
# ★ DB は mode=ro（読み取り専用）で開き、件数だけを返す。会話や記憶の中身は持ち出さない。
DB_SUMMARY_SCRIPT = r'''
import json, os, sqlite3, sys
from pathlib import Path

db_path, blacklist_path = sys.argv[1], sys.argv[2]
result = {"path": db_path, "exists": os.path.exists(db_path), "errors": []}

def count_by(con, sql):
    try:
        return {str(key): int(value) for key, value in con.execute(sql)}
    except sqlite3.Error as error:
        result["errors"].append(str(error))
        return {}

if result["exists"]:
    result["size_bytes"] = os.path.getsize(db_path)
    wal_path = db_path + "-wal"
    result["wal_bytes"] = os.path.getsize(wal_path) if os.path.exists(wal_path) else 0
    try:
        con = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
        try:
            result["schema_version"] = con.execute("PRAGMA user_version").fetchone()[0]
            names = [row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            result["tables"] = {
                name: con.execute('SELECT COUNT(*) FROM "%s"' % name.replace('"', '""')).fetchone()[0]
                for name in names
            }
            result["reminders_by_status"] = count_by(con, "SELECT status, COUNT(*) FROM reminders GROUP BY status")
            result["queue_by_status"] = count_by(con, "SELECT status, COUNT(*) FROM queue_tasks GROUP BY status")
            rating = count_by(con, "SELECT rating, COUNT(*) FROM feedback GROUP BY rating")
            result["feedback"] = {"good": rating.get("1", 0), "bad": rating.get("-1", 0)}
            # 直近7日の 👍 / 👎（日本時間の日付で区切る。created_at は UTC の ISO 8601）
            try:
                result["feedback_by_day"] = [
                    {"day": day, "good": int(good or 0), "bad": int(bad or 0)}
                    for day, good, bad in con.execute(
                        "SELECT date(created_at, '+9 hours') AS day, SUM(rating = 1), SUM(rating = -1) FROM feedback "
                        "WHERE date(created_at, '+9 hours') >= date('now', '+9 hours', '-6 days') "
                        "GROUP BY day ORDER BY day")
                ]
            except sqlite3.Error as error:
                result["errors"].append(str(error))
        finally:
            con.close()
    except sqlite3.Error as error:
        result["errors"].append("bot.db: " + str(error))

try:
    with open(blacklist_path, encoding="utf-8-sig") as file:
        data = json.load(file)
    records = [r for r in data.values() if isinstance(r, dict)] if isinstance(data, dict) else []
    result["blacklist"] = {"users": len(records), "blocked": sum(1 for r in records if r.get("blocked"))}
except FileNotFoundError:
    result["blacklist"] = None
except (OSError, ValueError) as error:
    result["blacklist"] = None
    result["errors"].append("blacklist.json: " + str(error))

print(json.dumps(result))
'''


# ---------------------------------------------------------------- 出力の読み取り


def _to_int(value: object) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return None if number == _SYSTEMD_UNSET else number


def parse_key_values(text: str) -> dict[str, str]:
    """`キー=値` の行を辞書にする（systemctl show の出力形式）。"""
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip()
    return values


def parse_service_show(text: str, service: str, host_uptime_seconds: float | None) -> dict:
    values = parse_key_values(text)
    active_state = values.get("ActiveState", "unknown")
    cpu_ns = _to_int(values.get("CPUUsageNSec"))
    start_us = _to_int(values.get("ExecMainStartTimestampMonotonic"))

    # 起動からの経過時間 = サーバーの稼働時間 - 「起動時点の稼働時間」
    uptime = None
    if active_state == "active" and start_us and host_uptime_seconds is not None:
        uptime = max(0.0, host_uptime_seconds - start_us / 1_000_000)

    return {
        "name": service,
        "description": values.get("Description", ""),
        "load_state": values.get("LoadState", ""),
        "active_state": active_state,
        "sub_state": values.get("SubState", ""),
        "result": values.get("Result", ""),
        "unit_file_state": values.get("UnitFileState", ""),
        "main_pid": _to_int(values.get("MainPID")) or None,
        "restarts": _to_int(values.get("NRestarts")),
        "memory_bytes": _to_int(values.get("MemoryCurrent")),
        "cpu_seconds": None if cpu_ns is None else cpu_ns / 1_000_000_000,
        "uptime_seconds": uptime,
    }


def parse_resources(text: str) -> dict:
    """RESOURCES_COMMAND の出力を読む。読めなかった項目は None のまま返す。"""
    lines = [line for line in text.splitlines() if line.strip()]
    result: dict = {
        "uptime_seconds": None,
        "load": None,
        "cpu_count": None,
        "memory": None,
        "swap": None,
        "disk": None,
    }
    try:
        result["uptime_seconds"] = float(lines[0].split()[0])
    except (IndexError, ValueError):
        pass
    try:
        load = lines[1].split()
        result["load"] = [float(load[0]), float(load[1]), float(load[2])]
    except (IndexError, ValueError):
        pass
    try:
        result["cpu_count"] = int(lines[2])
    except (IndexError, ValueError):
        pass

    for line in lines:
        parts = line.split()
        try:
            if parts[0] == "Mem:":
                total, available = int(parts[1]), int(parts[6])
                # 「使用中」は total - available で数える（キャッシュは解放できるので使用中に含めない）
                result["memory"] = {"total": total, "used": total - available, "available": available}
            elif parts[0] == "Swap:":
                total, used = int(parts[1]), int(parts[2])
                result["swap"] = {"total": total, "used": used, "available": total - used}
        except (IndexError, ValueError):
            continue

    try:
        size, used, available = (int(value) for value in lines[-1].split())
        result["disk"] = {"total": size, "used": used, "available": available}
    except (IndexError, ValueError):
        pass
    return result


def parse_git(text: str) -> dict:
    lines = text.splitlines()
    if len(lines) < 4:
        raise ValueError("Git の出力が想定より短いです。")
    commit, subject, committed_at = (lines[1].split("\x1f") + ["", ""])[:3]
    return {
        "branch": lines[0].strip(),
        "commit": commit.strip(),
        "subject": subject.strip(),
        "committed_at": committed_at.strip(),
        "describe": lines[2].strip(),
        "dirty_files": _to_int(lines[3]) or 0,
    }


def parse_journal_line(line: str) -> dict | None:
    """journalctl -o json の1行を読む。JSON でない行（区切りの表示など）は None。"""
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    message = data.get("MESSAGE", "")
    if isinstance(message, list):
        # 不正な UTF-8 を含むと、journald は本文を「バイトの数値の配列」で返す
        try:
            message = bytes(message).decode("utf-8", errors="replace")
        except (TypeError, ValueError):
            message = ""
    elif message is None:
        message = ""

    timestamp_us = _to_int(data.get("__REALTIME_TIMESTAMP")) or 0
    return {
        "ts": timestamp_us // 1000,
        "message": str(message),
        "priority": _to_int(data.get("PRIORITY")),
        "pid": _to_int(data.get("_PID")),
        "source": str(data.get("SYSLOG_IDENTIFIER") or data.get("_COMM") or ""),
    }


def parse_server_info(text: str) -> dict:
    lines = text.splitlines() + ["", "", ""]
    return {"hostname": lines[0].strip(), "kernel": lines[1].strip(), "os": lines[2].strip()}
