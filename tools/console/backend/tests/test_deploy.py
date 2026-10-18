"""デプロイ（deploy.py）の回帰テスト。

テスト専用の模擬 SSH サーバーへ「本物の SSH」で接続し、git pull → 依存の更新 → 再起動 → ヘルスチェックと、
途中で失敗したときの自動の巻き戻しを、1ステップずつ確かめる（本番サーバーには触れない）。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import remote_commands as rc  # noqa: E402
from deploy import DeployManager, DeployRecord, DeployRunner, new_record  # noqa: E402
from fake_ssh_server import start_fake_server  # noqa: E402
from profiles import Profile  # noqa: E402
from ssh_session import SshSession  # noqa: E402

FAST = {"restart_settle_timeout": 5, "health_timeout": 5, "poll_interval": 0.05}


class DeployTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.key_dir = Path(tempfile.mkdtemp(prefix="pbc-deploy-"))
        # 実際の時間を待たないよう、再起動や起動ログの待ち時間を小さくする
        self.acceptor, self.bot, port, self.live_task = await start_fake_server(
            "127.0.0.1", 0, self.key_dir, live_interval=(30, 60), delay_scale=0.05
        )
        self.repo = self.bot.repo
        self.profile = Profile(
            id="test",
            label="test",
            environment="production",
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
        await self.session.connect(self.profile)

    async def asyncTearDown(self) -> None:
        await self.session.disconnect()
        self.live_task.cancel()
        self.acceptor.close()
        await self.acceptor.wait_closed()
        shutil.rmtree(self.key_dir, ignore_errors=True)

    async def deploy(self) -> DeployRecord:
        record = new_record(1, self.profile)
        return await DeployRunner(self.session, self.profile, record, **FAST).run()

    def statuses(self, record: DeployRecord) -> dict[str, str]:
        return {step.key: step.status for step in record.steps}

    def step(self, record: DeployRecord, key: str):
        return next(step for step in record.steps if step.key == key)

    def sent(self, fragment: str) -> list[str]:
        """模擬サーバーへ送られたコマンドのうち、fragment を含むもの。"""
        return [record.command for record in self.session.history if fragment in record.command]

    # ------------------------------------------------------------ 成功

    async def test_success_runs_every_step_and_records_facts(self) -> None:
        before, target, old_pid = self.repo.head, self.repo.origin, self.bot.pid
        record = await self.deploy()

        self.assertEqual(record.status, "succeeded", record.message)
        self.assertEqual(
            self.statuses(record),
            {"precheck": "succeeded", "pull": "succeeded", "deps": "skipped", "restart": "succeeded", "health": "succeeded"},
        )
        self.assertEqual((record.before_commit, record.after_commit), (before, target))
        self.assertEqual(self.repo.head, target)
        self.assertEqual([commit["subject"] for commit in record.commits], [self.repo.history[3][1], self.repo.history[2][1]])
        # 各ステップで確かめた事実が、すべて ✓ で残る
        for step in record.steps:
            self.assertTrue(all(check.ok for check in step.checks), (step.key, step.checks))
        restart = self.step(record, "restart")
        self.assertIn(f"{old_pid} → {self.bot.pid}", [check.detail for check in restart.checks])
        self.assertNotEqual(self.bot.pid, old_pid)
        health = self.step(record, "health")
        self.assertIn(rc.LOGIN_SUCCESS_TEXT, health.checks[0].detail)
        # 実行したコマンドと出力が画面に出せる形で残る
        self.assertTrue(self.step(record, "pull").commands[0].endswith("git merge --ff-only origin/main && git rev-parse HEAD"))
        self.assertIn("Fast-forward", self.step(record, "pull").output)
        self.assertEqual(self.sent("-m pip install"), [])  # requirements.txt は変わっていない

    async def test_reinstalls_requirements_only_when_changed(self) -> None:
        self.repo.requirements_changed_at = {3}
        record = await self.deploy()

        self.assertEqual(record.status, "succeeded", record.message)
        deps = self.step(record, "deps")
        self.assertEqual(deps.status, "succeeded")
        self.assertEqual(
            [check.label for check in deps.checks],
            ["pip install が成功（終了コード 0）", "pip check で依存関係の食い違いなし"],
        )
        self.assertEqual(len(self.sent("-m pip install")), 1)

    async def test_up_to_date_changes_nothing(self) -> None:
        self.repo.head_index = len(self.repo.history) - 1
        old_pid = self.bot.pid
        record = await self.deploy()

        self.assertEqual(record.status, "up_to_date")
        self.assertEqual(self.statuses(record)["precheck"], "succeeded")
        self.assertTrue(all(step.status == "skipped" for step in record.steps[1:]))
        self.assertEqual(self.bot.pid, old_pid)
        self.assertEqual(self.sent("systemctl restart"), [])

    # ------------------------------------------------------------ 事前確認で止める（何も変えない）

    async def test_refuses_when_server_has_uncommitted_changes(self) -> None:
        self.repo.dirty = 2
        before = self.repo.head
        record = await self.deploy()

        self.assertEqual(record.status, "failed")
        precheck = self.step(record, "precheck")
        self.assertEqual(precheck.status, "failed")
        self.assertIn("未コミットの変更", precheck.message)
        self.assertEqual(self.repo.head, before)
        self.assertEqual(self.sent("git merge"), [])
        self.assertEqual(self.sent("systemctl restart"), [])

    async def test_refuses_on_another_branch(self) -> None:
        self.repo.branch = "develop"
        record = await self.deploy()

        self.assertEqual(record.status, "failed")
        self.assertIn("git switch main", self.step(record, "precheck").message)
        self.assertEqual(self.sent("git fetch"), [])

    async def test_fetch_failure_changes_nothing(self) -> None:
        self.repo.fetch_fails = True
        record = await self.deploy()

        self.assertEqual(record.status, "failed")
        self.assertIn("取り寄せられませんでした", self.step(record, "precheck").message)
        self.assertEqual(self.sent("git merge"), [])

    # ------------------------------------------------------------ 途中で失敗したら、自動で前の版へ戻す

    async def test_pip_failure_rolls_back_without_restart(self) -> None:
        before, old_pid = self.repo.head, self.bot.pid
        self.repo.requirements_changed_at = {3}
        self.repo.pip_fails_at = {self.repo.origin}
        record = await self.deploy()

        self.assertEqual(record.status, "rolled_back", record.message)
        statuses = self.statuses(record)
        self.assertEqual((statuses["deps"], statuses["restart"], statuses["health"]), ("failed", "skipped", "skipped"))
        self.assertEqual(statuses["rollback_code"], "succeeded")
        self.assertEqual(statuses["rollback_deps"], "succeeded")  # 前の requirements.txt で入れ直した
        self.assertEqual((statuses["rollback_restart"], statuses["rollback_health"]), ("skipped", "skipped"))
        self.assertEqual(self.repo.head, before)
        self.assertEqual(self.bot.pid, old_pid)  # 再起動していないので、前の版のまま動いている
        self.assertEqual(self.sent("systemctl restart"), [])

    async def test_broken_version_rolls_back_and_restarts_previous(self) -> None:
        before = self.repo.head
        self.repo.broken = {self.repo.origin}
        record = await self.deploy()

        self.assertEqual(record.status, "rolled_back", record.message)
        statuses = self.statuses(record)
        self.assertEqual((statuses["restart"], statuses["health"]), ("succeeded", "failed"))
        self.assertIn("止まりました", self.step(record, "health").message)
        self.assertIn("Traceback", self.step(record, "health").output)  # 原因のログが残る
        self.assertEqual(
            [statuses[key] for key in ("rollback_code", "rollback_deps", "rollback_restart", "rollback_health")],
            ["succeeded", "skipped", "succeeded", "succeeded"],
        )
        self.assertEqual(self.repo.head, before)
        self.assertTrue(self.bot.active)
        self.assertFalse(self.bot.failed)

    async def test_rollback_failure_asks_for_manual_steps(self) -> None:
        self.repo.broken = {self.repo.origin}
        self.repo.reset_fails = True
        record = await self.deploy()

        self.assertEqual(record.status, "rollback_failed")
        statuses = self.statuses(record)
        self.assertEqual(statuses["rollback_code"], "failed")
        self.assertEqual(
            [statuses[key] for key in ("rollback_deps", "rollback_restart", "rollback_health")], ["skipped"] * 3
        )
        self.assertIn(f"git reset --hard {record.before_commit}", record.message)
        self.assertIn("手動", record.message)

    # ------------------------------------------------------------ 同時に1つだけ

    async def test_manager_runs_one_deploy_at_a_time(self) -> None:
        manager = DeployManager()
        manager.start(self.session, self.profile, **FAST)
        self.assertTrue(manager.running)
        with self.assertRaises(RuntimeError):
            manager.start(self.session, self.profile, **FAST)
        record = await manager.wait()
        self.assertFalse(manager.running)
        self.assertEqual(record.status, "succeeded")


class DeployCommandTests(unittest.TestCase):
    def test_rejects_unsafe_values(self) -> None:
        for branch in ("main; rm -rf /", "--upload-pack=x", "a..b", "-x"):
            with self.assertRaises(ValueError):
                rc.deploy_fetch_command("/srv/bot", branch)
        with self.assertRaises(ValueError):
            rc.deploy_reset_command("/srv/bot", "HEAD~1")  # コミット ID（40 桁）以外は受け付けない
        with self.assertRaises(ValueError):
            rc.deploy_pip_install_command("/srv/bot", ".venv/bin/python; id")

    def test_deploy_log_round_trip_preserves_commit_hash_and_subject(self):
        command = rc.deploy_log_command("/srv/bot", "a" * 40, "b" * 40)
        self.assertIn("%h%x1f%s", command)
        self.assertEqual(rc.parse_commit_list("abc1234\x1ffix: 架空の修正"), [{"hash": "abc1234", "subject": "fix: 架空の修正"}])
        with self.assertRaises(ValueError):
            rc.parse_commit_list("abc1234\x1f")

    def test_paths_are_quoted(self) -> None:
        self.assertTrue(rc.deploy_state_command("/home/u/my bot").startswith("cd '/home/u/my bot' && "))
        self.assertEqual(
            rc.deploy_pip_check_command("/srv/bot", "/opt/py3/bin/python"), "cd /srv/bot && /opt/py3/bin/python -m pip check"
        )
        with self.assertRaises(ValueError):
            rc.deploy_pip_check_command("/srv/bot", "/opt/py 3/bin/python")  # 空白を含む場所は受け付けない


if __name__ == "__main__":
    unittest.main()
