"""模擬 SSH サーバーへ「本物の SSH」で接続する結合テスト。

ホスト鍵の検証・鍵認証・コマンド実行・ログのストリーム・サービス操作を、
本番サーバーに触れずに確かめる（127.0.0.1 の空きポートだけを使う）。
"""

import asyncio
import json
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncssh  # noqa: E402

import remote_commands as rc  # noqa: E402
from dev_ssh_server import start_dev_server  # noqa: E402
from profiles import Profile  # noqa: E402
from ssh_session import SshError, SshSession  # noqa: E402


class SshIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.key_dir = Path(tempfile.mkdtemp(prefix="pbc-test-"))
        self.acceptor, self.bot, port, self.live_task = await start_dev_server(
            "127.0.0.1", 0, self.key_dir, live_interval=(0.2, 0.4)
        )
        self.profile = Profile(
            id="test",
            label="test",
            environment="development",
            host="127.0.0.1",
            port=port,
            username="ubuntu",
            key_path=str(self.key_dir / "client_key"),
            known_hosts=str(self.key_dir / "known_hosts"),
            use_ssh_config=False,
            service="bot",
            bot_dir="/home/ubuntu/Persona-Chat-in-Discord",
            journal_sudo=False,
            note="",
        )
        self.session = SshSession()

    async def asyncTearDown(self):
        await self.session.disconnect()
        self.live_task.cancel()
        self.acceptor.close()
        await self.acceptor.wait_closed()
        shutil.rmtree(self.key_dir, ignore_errors=True)

    async def test_connect_and_read_status(self):
        await self.session.connect(self.profile)
        self.assertTrue(self.session.connected)
        self.assertTrue(self.session.fingerprint.startswith("SHA256:"))

        service = await self.session.run(rc.service_show_command("bot"), label="サービス状態")
        resources = await self.session.run(rc.RESOURCES_COMMAND, label="リソース")
        parsed_resources = rc.parse_resources(resources.stdout)
        status = rc.parse_service_show(service.stdout, "bot", parsed_resources["uptime_seconds"])
        self.assertEqual(status["active_state"], "active")
        self.assertGreater(status["uptime_seconds"], 0)
        self.assertEqual(parsed_resources["cpu_count"], 2)

        git = rc.parse_git((await self.session.run(rc.git_command("/home/ubuntu/x"), label="Git")).stdout)
        self.assertEqual(git["branch"], "main")
        self.assertGreaterEqual(len(self.session.history), 4)

    async def test_logs_and_live_stream(self):
        await self.session.connect(self.profile)
        result = await self.session.run(
            rc.journal_command("bot", lines=20, follow=False, use_sudo=False), label="ログ取得"
        )
        entries = [entry for line in result.stdout.splitlines() if (entry := rc.parse_journal_line(line))]
        self.assertEqual(len(entries), 20)

        process = await self.session.open_process(
            rc.journal_command("bot", lines=1, follow=True, use_sudo=False), label="ログ追跡"
        )
        try:
            received = []
            async with asyncio.timeout(5):
                async for line in process.stdout:
                    received.append(rc.parse_journal_line(line))
                    if len(received) >= 3:  # 過去1件 + 新着2件
                        break
            self.assertEqual(len(received), 3)
        finally:
            process.close()
        # 閉じたあとも同じ接続で別のコマンドを実行できる
        self.assertEqual((await self.session.run("true", label="疎通確認")).exit_status, 0)

    async def test_restart_changes_pid(self):
        await self.session.connect(self.profile)
        before = self.bot.pid
        result = await self.session.run(rc.service_control_command("bot", "restart"), label="再起動")
        self.assertEqual(result.exit_status, 0)
        self.assertNotEqual(self.bot.pid, before)
        self.assertTrue(self.bot.active)

        await self.session.run(rc.service_control_command("bot", "stop"), label="停止")
        service = await self.session.run(rc.service_show_command("bot"), label="サービス状態")
        self.assertEqual(rc.parse_service_show(service.stdout, "bot", None)["active_state"], "inactive")

    async def test_db_summary_via_stdin(self):
        await self.session.connect(self.profile)
        result = await self.session.run(
            rc.db_summary_command(self.profile.bot_dir), label="DB", input=rc.DB_SUMMARY_SCRIPT
        )
        self.assertEqual(result.exit_status, 0)
        self.assertIn('"schema_version": 10', result.stdout)

    async def test_insights_via_stdin(self):
        await self.session.connect(self.profile)
        result = await self.session.run(
            rc.insights_command(self.profile.bot_dir, "bot"), label="状態", input=rc.INSIGHTS_SCRIPT
        )
        self.assertEqual(result.exit_status, 0)
        data = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(data["discord"]["ok"])
        self.assertEqual(len(data["discord"]["guilds"]), 3)

    async def test_rejects_unknown_host_key(self):
        """known_hosts に別の鍵が書かれていたら、接続してはいけない（なりすまし対策）。"""
        other_key = asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode().split()
        fake_known_hosts = self.key_dir / "fake_known_hosts"
        fake_known_hosts.write_text(f"[127.0.0.1]:{self.profile.port} {other_key[0]} {other_key[1]}\n")
        with self.assertRaises(SshError) as context:
            await self.session.connect(replace(self.profile, known_hosts=str(fake_known_hosts)))
        self.assertIn("ホスト鍵", str(context.exception))
        self.assertFalse(self.session.connected)

    async def test_rejects_empty_known_hosts(self):
        empty = self.key_dir / "empty_known_hosts"
        empty.write_text("")
        with self.assertRaises(SshError):
            await self.session.connect(replace(self.profile, known_hosts=str(empty)))
        self.assertFalse(self.session.connected)

    async def test_rejects_wrong_client_key(self):
        wrong_key = self.key_dir / "wrong_key"
        asyncssh.generate_private_key("ssh-ed25519").write_private_key(wrong_key)
        with self.assertRaises(SshError) as context:
            await self.session.connect(replace(self.profile, key_path=str(wrong_key)))
        self.assertIn("認証", str(context.exception))

    async def test_detects_connection_loss(self):
        await self.session.connect(self.profile)
        for conn in list(self.bot.connections):
            conn.close()  # サーバー側から切る
        async with asyncio.timeout(5):
            while self.session.connected:
                await asyncio.sleep(0.05)
        self.assertIn("切れました", self.session.last_error)


if __name__ == "__main__":
    unittest.main()
