"""サーバー側で動かすスクリプト（insights.py・DB 集計・テーブルの中身）を、手元の一時フォルダに作った架空のボットで検証する。

本物のボット・Discord・ネットワークには一切触れない（Discord API の接続先は使われていないポートへ差し替える）。
"""

import ast
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import remote_commands as rc  # noqa: E402
from fake_ssh_server import FAKE_OPTED_OUT_USER, FAKE_USERS, make_fake_database  # noqa: E402

FAKE_TOKEN = "fake-token-for-test-only"
# 架空の ID（実在のサーバーではない）
GUILD_ID = 100000000000000001

CONSTANTS_PY = '''
DEFAULT_PROMPT_FILE = "../prompts/prompt_default.txt"
DEFAULT_MODEL_NAME = "gemini-3.1-flash-lite"
DEFAULT_RESPOND_MODE = "mention"
DEFAULT_AUTO_TOPIC_INTERVAL_HOURS = 6
'''
CHOICES_PY = '''
from discord import app_commands
PERSONA_CHOICES = [
    app_commands.Choice(name="ツンデレ", value="../prompts/prompt_tsun.txt"),
    app_commands.Choice(name="標準", value=DEFAULT_PROMPT_FILE),
]
MODEL_CHOICES = [app_commands.Choice(name="Gemini 3.1 Flash Lite", value=DEFAULT_MODEL_NAME)]
RESPOND_MODE_CHOICES = [
    app_commands.Choice(name="すべての発言に反応", value="all"),
    app_commands.Choice(name="メンションのみに反応", value=DEFAULT_RESPOND_MODE),
]
'''


def make_fake_bot(root: Path, *, layout: str, with_autonomous_column: bool) -> None:
    """ボットのフォルダ構成だけを真似た一時フォルダを作る。"""
    source = root / ("index" if layout == "index" else "src/persona_bot/config")
    source.mkdir(parents=True)
    (source / "constants.py").write_text(CONSTANTS_PY, encoding="utf-8")
    (source / "choices.py").write_text(CHOICES_PY, encoding="utf-8")
    if with_autonomous_column:
        services = root / "src/persona_bot/services"
        services.mkdir(parents=True)
        (services / "autonomous_service.py").write_text("", encoding="utf-8")
    (root / "README.md").write_text("# bot\n\n## 📋 更新履歴\n\n### ver0.1.6 — 2026/08/14\n", encoding="utf-8")

    (root / "memory").mkdir()
    con = sqlite3.connect(root / "memory" / "bot.db")
    columns = "guild_id INTEGER PRIMARY KEY, prompt_file TEXT, model_name TEXT, respond_mode TEXT, " \
              "grounding_enabled INTEGER, auto_topic_enabled INTEGER, auto_topic_interval_hours INTEGER"
    if with_autonomous_column:
        columns += ", autonomous_enabled INTEGER"
    con.execute(f"CREATE TABLE guild_settings ({columns})")
    con.execute(
        "INSERT INTO guild_settings (guild_id, prompt_file, model_name, respond_mode, grounding_enabled, "
        "auto_topic_enabled, auto_topic_interval_hours) VALUES (0, '../prompts/prompt_tsun.txt', '', 'all', 0, 1, 3)"
    )
    con.commit()
    con.close()


def run_script(script: str, arguments: list[str], *, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", "-"] + arguments,
        input=script, capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", **(env or {})},
    )


class InsightsScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        # 使われていないポート（つながらずにすぐ失敗する）。本物の Discord へは出ない
        self.offline_env = {"PBC_DISCORD_API_BASE": "http://127.0.0.1:9", "TOKEN_DISCORD": ""}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def insights(self, env: dict | None = None, *, responses=None, journal=None, journal_code=0, sudo=False) -> dict:
        # 子プロセス内で外部境界だけを差し替え、スクリプト全体を実行する
        prelude = ""
        if responses is not None or journal is not None:
            prelude = f'''
import io, json, subprocess, urllib.request, urllib.error, urllib.parse
_responses = {responses!r}
_journal = {journal!r}
def _fake_urlopen(request, **kwargs):
    path = urllib.parse.urlsplit(request.full_url).path.removeprefix('/api/v10')
    value = (_responses or {{}}).get(path, 404)
    if isinstance(value, int):
        raise urllib.error.HTTPError(request.full_url, value, 'test', {{}}, None)
    return io.BytesIO(json.dumps(value).encode())
def _fake_run(command, **kwargs):
    if 'journalctl' in command:
        assert ('sudo' in command) == {sudo!r}
        values = _journal or []
        if '-g' in command:
            values = [row for row in values if isinstance(row, dict) and str(row.get('MESSAGE', '')).startswith('User (')][-1:]
        output = '\\n'.join(json.dumps(row) for row in values)
        return subprocess.CompletedProcess(command, {journal_code!r}, output, 'private journal detail')
    if command[0] == 'systemctl':
        return subprocess.CompletedProcess(command, 0, '', '')
    raise AssertionError('許可していない外部コマンド')
urllib.request.urlopen = _fake_urlopen
subprocess.run = _fake_run
'''
        args = ["--insights", str(self.root), "bot"] + (["--journal-sudo"] if sudo else [])
        completed = run_script(prelude + rc.INSIGHTS_SCRIPT, args, env={**self.offline_env, **(env or {})})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.stdout = completed.stdout
        return json.loads(completed.stdout.strip().splitlines()[-1])

    def test_old_layout_reads_display_names_and_marks_autonomous_unsupported(self) -> None:
        make_fake_bot(self.root, layout="index", with_autonomous_column=False)
        data = self.insights()
        self.assertEqual(data["source_dir"], "index")
        self.assertEqual(data["version"], "ver0.1.6 — 2026/08/14")
        self.assertFalse(data["autonomous_supported"])
        # DM（guild_id=0）の行。モデルは空なので既定値の表示名、自律行動は列が無いので None
        self.assertEqual(
            data["dm_settings"],
            {"persona": "ツンデレ", "model": "Gemini 3.1 Flash Lite", "respond_mode": "すべての発言に反応",
             "grounding": False, "auto_topic_enabled": True, "auto_topic_interval_hours": 3,
             "autonomous_enabled": None, "saved": True},
        )

    def test_new_layout_and_autonomous_column(self) -> None:
        make_fake_bot(self.root, layout="src", with_autonomous_column=True)
        data = self.insights()
        self.assertEqual(data["source_dir"], "src")
        self.assertTrue(data["autonomous_supported"])
        self.assertIs(data["dm_settings"]["autonomous_enabled"], False)

    def test_without_token_reports_error(self) -> None:
        make_fake_bot(self.root, layout="index", with_autonomous_column=False)
        discord = self.insights()["discord"]
        self.assertFalse(discord["ok"])
        self.assertIn("TOKEN_DISCORD", discord["error"])

    def test_token_from_env_file_is_never_printed(self) -> None:
        make_fake_bot(self.root, layout="index", with_autonomous_column=False)
        (self.root / ".env").write_text(f'API_KEY_GEMINI=x\nTOKEN_DISCORD="{FAKE_TOKEN}"\n', encoding="utf-8")
        discord = self.insights()["discord"]
        self.assertFalse(discord["ok"])
        self.assertIn("接続できませんでした", discord["error"])  # .env から読めたので API へ行こうとした
        self.assertNotIn(FAKE_TOKEN, self.stdout)

    def test_output_has_no_guild_ids(self) -> None:
        make_fake_bot(self.root, layout="index", with_autonomous_column=False)
        con = sqlite3.connect(self.root / "memory" / "bot.db")
        con.execute("INSERT INTO guild_settings (guild_id, prompt_file) VALUES (?, '../prompts/prompt_tsun.txt')", (GUILD_ID,))
        con.commit()
        con.close()
        self.insights()
        self.assertNotIn(str(GUILD_ID), self.stdout)

    def test_discord_success_has_names_defaults_and_no_ids_or_token(self):
        make_fake_bot(self.root, layout="src", with_autonomous_column=True)
        responses = {
            "/users/@me": {"id": str(GUILD_ID + 2), "username": "架空のボット", "discriminator": "0"},
            "/users/@me/guilds": [{"id": str(GUILD_ID), "name": "架空のサーバー",
                                   "approximate_member_count": 12, "approximate_presence_count": 3}],
            f"/guilds/{GUILD_ID}/voice-states/@me": {"channel_id": str(GUILD_ID + 1)},
            f"/channels/{GUILD_ID + 1}": {"id": str(GUILD_ID + 1), "name": "架空のボイス"},
        }
        data = self.insights({"TOKEN_DISCORD": FAKE_TOKEN}, responses=responses, journal=[])
        guild = data["discord"]["guilds"][0]
        self.assertEqual((guild["members"], guild["online"], guild["voice_channel"]), (12, 3, "架空のボイス"))
        self.assertEqual(guild["settings"]["persona"], "標準")
        self.assertIs(guild["settings"]["autonomous_enabled"], False)
        self.assertFalse(guild["settings"]["saved"])
        for secret in (FAKE_TOKEN, str(GUILD_ID), str(GUILD_ID + 1), str(GUILD_ID + 2)):
            self.assertNotIn(secret, self.stdout)

    def test_discord_errors_keep_db_and_log_results(self):
        make_fake_bot(self.root, layout="src", with_autonomous_column=True)
        con = sqlite3.connect(self.root / "memory/bot.db")
        con.execute("INSERT INTO guild_settings (guild_id, respond_mode) VALUES (?, 'all')", (GUILD_ID,))
        con.commit()
        con.close()
        for code in (401, 429):
            with self.subTest(code=code):
                data = self.insights({"TOKEN_DISCORD": FAKE_TOKEN}, responses={"/users/@me": code},
                                     journal=[{"MESSAGE": "Total token count：42"}])
                self.assertIn(str(code), data["discord"]["error"])
                self.assertEqual(data["discord"]["guilds"][0]["settings"]["respond_mode"], "すべての発言に反応")
                self.assertEqual(data["activity"]["gemini_tokens"], 42)
                self.assertNotIn(FAKE_TOKEN, self.stdout)
                self.assertNotIn(str(GUILD_ID), self.stdout)

    def test_journal_counts_events_without_returning_conversation_or_ids(self):
        make_fake_bot(self.root, layout="src", with_autonomous_column=True)
        messages = ["Total token count：42", "User (架空の利用者)：架空の会話本文",
                    "has connected to Gateway", "successfully RESUMED", "session has been invalidated",
                    "heartbeat blocked", "Traceback (most recent call last):",
                    f"自律行動: ボイス「架空のボイス」で配信を開始しました（guild={GUILD_ID}）。",
                    f"自律行動: 配信を終了しました（guild={GUILD_ID} / 理由: 架空の終了理由）。"]
        entries = [{"MESSAGE": message, "__REALTIME_TIMESTAMP": "1000000"} for message in messages]
        entries += [None, [], {"MESSAGE": [-1, 300]}, {"MESSAGE": [255], "__REALTIME_TIMESTAMP": None}]
        data = self.insights(journal=entries, sudo=True)
        activity = data["activity"]
        for key in ("gemini_calls", "conversations", "gateway_connects", "gateway_resumes", "gateway_invalidated",
                    "heartbeat_blocked", "exceptions"):
            self.assertEqual(activity[key], 1, key)
        self.assertEqual(activity["last_conversation_at"], 1)
        self.assertEqual([event["text"] for event in activity["autonomous_events"]],
                         ["配信を開始しました", "配信を終了しました"])
        for secret in (str(GUILD_ID), "架空の会話本文", "架空の終了理由", "架空の利用者"):
            self.assertNotIn(secret, self.stdout)

    def test_journal_failure_is_reported_without_stderr(self):
        make_fake_bot(self.root, layout="index", with_autonomous_column=False)
        data = self.insights(journal=[], journal_code=1)
        self.assertTrue(any("読み取り" in error or "journal_sudo" in error for error in data["errors"]))
        self.assertNotIn("private journal detail", self.stdout)

    def test_voice_rate_limit_keeps_guilds_and_marks_voice_unknown(self):
        make_fake_bot(self.root, layout="src", with_autonomous_column=True)
        responses = {"/users/@me": {"username": "架空のボット"},
                     "/users/@me/guilds": [{"id": str(GUILD_ID), "name": "架空のサーバー"}],
                     f"/guilds/{GUILD_ID}/voice-states/@me": 429}
        data = self.insights({"TOKEN_DISCORD": FAKE_TOKEN}, responses=responses, journal=[])
        self.assertTrue(data["discord"]["ok"])
        self.assertIn("429", data["discord"]["guilds"][0]["voice_error"])
        self.assertNotIn(str(GUILD_ID), self.stdout)

    def test_env_comments_are_supported_and_database_is_unchanged(self):
        make_fake_bot(self.root, layout="src", with_autonomous_column=True)
        (self.root / ".env").write_text(f'export TOKEN_DISCORD="{FAKE_TOKEN}" # 架空の設定\n', encoding="utf-8")
        before = (self.root / "memory/bot.db").read_bytes()
        data = self.insights(responses={"/users/@me": {"username": "架空のボット"}, "/users/@me/guilds": []}, journal=[])
        self.assertTrue(data["discord"]["ok"])
        self.assertNotIn(FAKE_TOKEN, self.stdout)
        self.assertEqual((self.root / "memory/bot.db").read_bytes(), before)

    def test_annotated_sources_are_read_without_importing(self):
        make_fake_bot(self.root, layout="src", with_autonomous_column=True)
        source = self.root / "src/persona_bot/config"
        (source / "constants.py").write_text(CONSTANTS_PY.replace("DEFAULT_MODEL_NAME =", "DEFAULT_MODEL_NAME: str =")
                                            + '\nraise RuntimeError("実行禁止")', encoding="utf-8")
        (source / "choices.py").write_text(CHOICES_PY.replace("PERSONA_CHOICES =", "PERSONA_CHOICES: list =")
                                          + '\nraise RuntimeError("実行禁止")', encoding="utf-8")
        data = self.insights(journal=[])
        self.assertEqual(data["dm_settings"]["persona"], "ツンデレ")
        self.assertEqual(data["dm_settings"]["model"], "Gemini 3.1 Flash Lite")

    def test_voice_permission_error_is_local_but_rate_limit_stops_later_requests(self):
        make_fake_bot(self.root, layout="src", with_autonomous_column=True)
        responses = {
            "/users/@me": {"username": "架空のボット"},
            "/users/@me/guilds": [{"id": "11", "name": None}, {"id": "12", "name": "架空の次サーバー"}],
            "/guilds/11/voice-states/@me": 403,
            "/guilds/12/voice-states/@me": {"channel_id": "13"},
            "/channels/13": {"name": "架空の通話"},
        }
        first, second = self.insights({"TOKEN_DISCORD": FAKE_TOKEN}, responses=responses, journal=[])["discord"]["guilds"]
        self.assertIsInstance(first["name"], str)
        self.assertIn("403", first["voice_error"])
        self.assertIsNone(second["voice_error"])
        self.assertEqual(second["voice_channel"], "架空の通話")
        responses["/guilds/11/voice-states/@me"] = 429
        first, second = self.insights({"TOKEN_DISCORD": FAKE_TOKEN}, responses=responses, journal=[])["discord"]["guilds"]
        self.assertIn("429", second["voice_error"])
        self.assertIsNone(second["voice_channel"])

    def test_env_empty_or_placeholder_lines_do_not_erase_valid_token(self):
        make_fake_bot(self.root, layout="src", with_autonomous_column=True)
        (self.root / ".env").write_text(f'TOKEN_DISCORD={FAKE_TOKEN}\nTOKEN_DISCORD=\nTOKEN_DISCORD=YOUR_DISCORD_BOT_TOKEN\n', encoding="utf-8-sig")
        responses = {"/users/@me": {"username": "架空"}, "/users/@me/guilds": []}
        self.assertTrue(self.insights(responses=responses, journal=[])["discord"]["ok"])
        (self.root / ".env").write_text('TOKEN_DISCORD=YOUR_DISCORD_BOT_TOKEN\n', encoding="utf-8")
        self.assertIn("TOKEN_DISCORD がありません", self.insights(responses=responses, journal=[])["discord"]["error"])


class DbSummaryScriptTests(unittest.TestCase):
    def summarize(self, root):
        completed = run_script(rc.DB_SUMMARY_SCRIPT, [str(root / "bot.db"), str(root / "blacklist.json")])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout.strip().splitlines()[-1])

    def test_missing_tables_keep_other_counts_and_never_read_conversation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            con = sqlite3.connect(root / "bot.db")
            con.execute('CREATE TABLE "fictional\""table" (content TEXT)')
            con.execute('INSERT INTO "fictional\""table" VALUES (?)', ("架空の秘密の会話",))
            con.execute("CREATE TABLE reminders (status TEXT)")
            con.execute("INSERT INTO reminders VALUES ('pending')")
            con.commit()
            con.close()
            before = (root / "bot.db").read_bytes()
            (root / "blacklist.json").write_text(json.dumps({"11": {"blocked": True}, "12": {"blocked": False}}), encoding="utf-8")
            data = self.summarize(root)
            self.assertEqual(data["tables"]['fictional"table'], 1)
            self.assertEqual(data["reminders_by_status"], {"pending": 1})
            self.assertIsNone(data["queue_by_status"])
            self.assertIsNone(data["feedback"])
            self.assertEqual(data["blacklist"], {"users": 2, "blocked": 1})
            self.assertTrue(any("文書キュー" in error for error in data["errors"]))
            self.assertNotIn("架空の秘密の会話", json.dumps(data, ensure_ascii=False))
            self.assertEqual((root / "bot.db").read_bytes(), before)

    def test_missing_database_and_broken_blacklist_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "blacklist.json").write_text("[]", encoding="utf-8")
            data = self.summarize(root)
            self.assertFalse(data["exists"])
            self.assertIsNone(data["blacklist"])
            self.assertTrue(any("blacklist.json" in error for error in data["errors"]))
            self.assertFalse((root / "bot.db").exists())

    def test_corrupt_database_keeps_blacklist_counts_without_error_details(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bot.db").write_bytes(b"fictional-invalid-database")
            (root / "blacklist.json").write_text('{"11":{"blocked":true}}', encoding="utf-8")
            data = self.summarize(root)
            self.assertEqual(data["blacklist"], {"users": 1, "blocked": 1})
            self.assertTrue(any("bot.db を読み取れません" in error for error in data["errors"]))
            self.assertFalse(any("file is not a database" in error for error in data["errors"]))

    def test_daily_feedback_excludes_future_and_old_days_at_jst_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            con = sqlite3.connect(root / "bot.db")
            con.execute("CREATE TABLE feedback (rating INTEGER, created_at TEXT)")
            now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
            today = now_jst.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=9)
            rows = [(1, today), (-1, today - timedelta(seconds=1)),
                    (1, today - timedelta(days=6)), (-1, today - timedelta(days=6, seconds=1)),
                    (1, today + timedelta(days=1)), (1, today - timedelta(days=20))]
            con.executemany("INSERT INTO feedback VALUES (?, ?)", [(rating, at.isoformat()) for rating, at in rows])
            con.commit()
            con.close()
            data = self.summarize(root)
            self.assertEqual(sum(day["good"] + day["bad"] for day in data["feedback_by_day"]), 3)
            today_key = now_jst.strftime("%Y-%m-%d")
            self.assertEqual(next(day for day in data["feedback_by_day"] if day["day"] == today_key)["good"], 1)

    def test_boundary_accepts_old_space_and_iso_utc_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            con = sqlite3.connect(root / "bot.db")
            try:
                con.execute("CREATE TABLE feedback(created_at TEXT, rating INTEGER)")
                start = con.execute("SELECT datetime('now', '+9 hours', 'start of day', '-6 days', '-9 hours')").fetchone()[0]
                for stamp in (start, start.replace(" ", "T") + "+00:00"):
                    con.execute("INSERT INTO feedback VALUES (?, 1)", (stamp,))
                con.commit()
            finally:
                con.close()
            data = self.summarize(root)
            self.assertEqual(sum(day["good"] for day in data["feedback_by_day"]), 2)

    def test_counts_and_feedback_by_day(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "bot.db"
            con = sqlite3.connect(db_path)
            con.execute("CREATE TABLE reminders (status TEXT)")
            con.execute("CREATE TABLE queue_tasks (status TEXT)")
            con.execute("CREATE TABLE feedback (rating INTEGER, created_at TEXT)")
            con.executemany("INSERT INTO reminders VALUES (?)", [("pending",), ("done",), ("done",)])
            now = datetime.now(timezone.utc).replace(microsecond=0)
            rows = [(1, now), (1, now), (-1, now), (1, now - timedelta(days=1)), (-1, now - timedelta(days=20))]
            con.executemany("INSERT INTO feedback VALUES (?, ?)", [(rating, at.isoformat()) for rating, at in rows])
            con.execute("PRAGMA user_version = 8")
            con.commit()
            con.close()

            completed = run_script(rc.DB_SUMMARY_SCRIPT, [str(db_path), str(Path(temp) / "missing.json")])
            self.assertEqual(completed.returncode, 0, completed.stderr)
            data = json.loads(completed.stdout.strip().splitlines()[-1])

        self.assertEqual(data["schema_version"], 8)
        self.assertEqual(data["reminders_by_status"], {"pending": 1, "done": 2})
        self.assertEqual(data["feedback"], {"good": 3, "bad": 2})
        # 20日前の評価は直近7日に入らない。日付は日本時間で区切る
        by_day = {item["day"]: (item["good"], item["bad"]) for item in data["feedback_by_day"]}
        today_jst = (now + timedelta(hours=9)).strftime("%Y-%m-%d")
        self.assertEqual(by_day[today_jst], (2, 1))
        self.assertEqual(sum(good + bad for good, bad in by_day.values()), 4)
        self.assertIsNone(data["blacklist"])


class DbTableScriptTests(unittest.TestCase):
    """テーブルの中身を取り出すスクリプト（remote_scripts/db_table.py）。架空の bot.db で確かめる。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path, _ = make_fake_database(Path(self.temp.name))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def read(self, table, offset=0, limit=50, db_path=None):
        completed = run_script(rc.DB_TABLE_SCRIPT, ["--db-table", str(db_path or self.db_path), table, str(offset), str(limit)])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        # 出力は ASCII だけ（サーバーのロケールが UTF-8 でなくても書き出せるように）
        self.assertTrue(completed.stdout.isascii())
        return json.loads(completed.stdout.strip().splitlines()[-1])

    def test_tables_match_the_backend_whitelist(self) -> None:
        tree = ast.parse(rc.DB_TABLE_SCRIPT)
        node = next(
            item for item in tree.body
            if isinstance(item, ast.Assign) and any(getattr(target, "id", "") == "TABLES" for target in item.targets)
        )
        self.assertEqual(set(ast.literal_eval(node.value)), set(rc.DB_TABLES))

    def test_every_table_can_be_read(self) -> None:
        for table in rc.DB_TABLES:
            with self.subTest(table=table):
                data = self.read(table)
                self.assertEqual(data["errors"], [])
                self.assertEqual(data["table"], table)
                self.assertGreater(data["total"], 0)
                self.assertEqual(len(data["rows"]), min(50, data["total"]))
                self.assertEqual(set(data["rows"][0]["cells"]), set(data["columns"]))

    def test_ids_are_strings_and_names_are_attached(self) -> None:
        data = self.read("feedback")
        con = sqlite3.connect(self.db_path)
        newest = con.execute("SELECT message_id, user_id FROM feedback ORDER BY created_at DESC LIMIT 1").fetchone()
        con.close()
        first = data["rows"][0]["cells"]
        # 19 桁の ID も、下の桁が丸まらないよう文字列で届く
        self.assertEqual(first["message_id"], str(newest[0]))
        self.assertIsInstance(first["guild_id"], str)
        self.assertIsInstance(first["rating"], int)
        self.assertEqual(data["users"][newest[1]], dict(FAKE_USERS)[newest[1]])
        created = [row["cells"]["created_at"] for row in data["rows"]]
        self.assertEqual(created, sorted(created, reverse=True))

    def test_pages_do_not_overlap_and_limit_is_capped(self) -> None:
        pages = [self.read("feedback", offset=offset) for offset in (0, 50, 100)]
        self.assertEqual([len(page["rows"]) for page in pages], [50, 50, 30])
        keys = [(row["cells"]["message_id"], row["cells"]["user_id"]) for page in pages for row in page["rows"]]
        self.assertEqual(len(set(keys)), 130)
        self.assertEqual(self.read("feedback", limit=100000)["limit"], 200)
        self.assertEqual(self.read("feedback", offset=-5)["offset"], 0)

    def test_json_columns_are_parsed(self) -> None:
        rows = {row["cells"]["user_id"]: row["cells"] for row in self.read("user_stats")["rows"]}
        stats = rows[FAKE_USERS[0][0]]
        self.assertEqual(len(stats["hourly"]), 24)
        self.assertIsInstance(stats["recent_samples"], list)
        self.assertEqual(set(stats["analysis"]["big_five"]), {"openness", "conscientiousness", "extraversion", "agreeableness", "neuroticism"})
        values = {row["cells"]["key"]: row["cells"]["value"] for row in self.read("profile")["rows"] if row["cells"]["user_id"] == FAKE_USERS[0][0]}
        self.assertEqual(values["好きなもの"], ["猫", "カレー", "RPG"])
        self.assertEqual(values["anniversaries"][0]["label"], "誕生日")

    def test_opted_out_user_samples_and_analysis_are_not_returned(self) -> None:
        con = sqlite3.connect(self.db_path)
        con.execute(
            "UPDATE user_stats SET recent_samples = ?, analysis = ? WHERE user_id = ?",
            (json.dumps(["架空の停止中の発言"], ensure_ascii=False), json.dumps({"personality": "架空の停止中の解析"}, ensure_ascii=False), FAKE_OPTED_OUT_USER),
        )
        con.commit()
        con.close()
        data = self.read("user_stats")
        row = next(row for row in data["rows"] if row["cells"]["user_id"] == FAKE_OPTED_OUT_USER)
        self.assertIsNone(row["cells"]["recent_samples"])
        self.assertIsNone(row["cells"]["analysis"])
        self.assertEqual(row["hidden"], ["recent_samples", "analysis"])
        text = json.dumps(data, ensure_ascii=False)
        self.assertNotIn("架空の停止中の発言", text)
        self.assertNotIn("架空の停止中の解析", text)
        # 止めていない人の発言サンプルは届く
        others = [row for row in data["rows"] if row["cells"]["user_id"] != FAKE_OPTED_OUT_USER]
        self.assertTrue(all(row["cells"]["recent_samples"] and "hidden" not in row for row in others))

    def test_database_is_opened_read_only(self) -> None:
        before = self.db_path.read_bytes()
        for table in rc.DB_TABLES:
            self.read(table)
        self.assertEqual(self.db_path.read_bytes(), before)
        self.assertFalse(Path(str(self.db_path) + "-wal").exists())

    def test_unknown_table_and_missing_database_are_reported_without_paths(self) -> None:
        data = self.read("sqlite_master")
        self.assertEqual(data["rows"], [])
        self.assertTrue(data["errors"])
        missing = Path(self.temp.name) / "fictional-missing" / "bot.db"
        data = self.read("feedback", db_path=missing)
        self.assertEqual(data["errors"], ["bot.db が見つかりません。"])
        self.assertNotIn("fictional-missing", json.dumps(data))

    def test_old_schema_and_broken_values_do_not_stop_the_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "bot.db"
            con = sqlite3.connect(db_path)
            # 並び順に使う created_at が無い古い形・JSON として読めない値・NaN が入った値
            con.execute("CREATE TABLE feedback (message_id INTEGER, user_id TEXT, rating INTEGER)")
            con.executemany("INSERT INTO feedback VALUES (?, ?, ?)", [(1, "11", 1), (2, "12", -1)])
            con.execute("CREATE TABLE user_stats (user_id TEXT, hourly TEXT, analysis TEXT)")
            con.execute("INSERT INTO user_stats VALUES ('11', '[NaN, 2, Infinity]', 'not-json')")
            con.commit()
            con.close()
            feedback = self.read("feedback", db_path=db_path)
            self.assertEqual([row["cells"]["message_id"] for row in feedback["rows"]], ["2", "1"])
            stats = self.read("user_stats", db_path=db_path)["rows"][0]["cells"]
            self.assertEqual(stats["hourly"], [None, 2, None])
            self.assertEqual(stats["analysis"], "not-json")
            missing = self.read("reminders", db_path=db_path)
            self.assertEqual(missing["rows"], [])
            self.assertTrue(any("reminders" in error for error in missing["errors"]))


if __name__ == "__main__":
    unittest.main()
