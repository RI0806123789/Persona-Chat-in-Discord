"""サーバー側で動かすスクリプト（insights.py・DB 集計）を、手元の一時フォルダに作った架空のボットで検証する。

本物のボット・Discord・ネットワークには一切触れない（Discord API の接続先は使われていないポートへ差し替える）。
"""

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

import remote_commands as rc  # noqa: E402

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


class DbSummaryScriptTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
