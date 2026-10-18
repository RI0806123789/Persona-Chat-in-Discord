"""デプロイと接続変更の排他・終了順序を、外部通信なしで検証する。"""

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app
from deploy import DeployManager, DeployRunner, StepFailed, new_record
from profiles import Profile
from ssh_session import CommandResult, SshError, SshSession
from test_api import request


def profile():
    return Profile("test", "架空の接続先", "development", "test.invalid", None, None, None, None,
                   True, "bot", "/srv/test-bot", True, "")


class ConnectionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_running_deploy_rejects_connect_and_disconnect_before_side_effects(self):
        session = SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock())
        with patch.object(app, "session", session), patch.object(app, "deploys", SimpleNamespace(running=True)), patch.object(
            app, "load_profiles", side_effect=AssertionError("接続設定を読む前に拒否する"),
        ):
            for path, body in (("/api/connect", {"profile_id": "test"}), ("/api/disconnect", None)):
                with self.subTest(path=path):
                    status, _ = await request(path, method="POST", body=body)
                    self.assertEqual(status, 409)
        session.connect.assert_not_awaited()
        session.disconnect.assert_not_awaited()

    async def test_connect_waits_for_deploy_start_lock_then_rechecks_running(self):
        with patch.object(app, "_connection_change_lock", asyncio.Lock()), patch.object(
            app, "deploys", SimpleNamespace(running=False),
        ):
            async with app._connection_change_lock:
                task = asyncio.create_task(app.connect(app.ConnectRequest(profile_id="test")))
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                app.deploys.running = True
            with self.assertRaises(app.HTTPException) as error:
                await task
            self.assertEqual(error.exception.status_code, 409)

    async def test_shutdown_waits_even_after_repeated_cancellation_before_disconnecting(self):
        manager = DeployManager()
        release = asyncio.Event()
        manager._task = asyncio.create_task(release.wait())
        session = SimpleNamespace(disconnect=AsyncMock())
        with patch.object(app, "deploys", manager), patch.object(app, "session", session):
            context = app.lifespan(app.app)
            await context.__aenter__()
            shutdown = asyncio.create_task(context.__aexit__(None, None, None))
            try:
                await asyncio.sleep(0)
                for _ in range(2):
                    shutdown.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(shutdown.done())
                    self.assertFalse(manager._task.cancelled())
                    session.disconnect.assert_not_awaited()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await shutdown
            finally:
                release.set()
                await asyncio.gather(shutdown, manager._task, return_exceptions=True)
        session.disconnect.assert_awaited_once()

    async def test_failed_reconnect_clears_previous_profile_and_metadata(self):
        session = SshSession()
        connection = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
        session._conn = connection
        session.profile = profile()
        session.connected_at = 1
        session.fingerprint = "架空のホスト鍵"
        session.server_info = {"hostname": "架空のホスト"}
        with patch.object(session, "_build_options", return_value={}), patch("ssh_session.asyncssh.connect", AsyncMock(
            side_effect=OSError("架空の接続失敗"),
        )):
            with self.assertRaises(SshError):
                await session.connect(profile())
        self.assertFalse(session.connected)
        for field in ("profile", "connected_at", "fingerprint", "server_info"):
            self.assertIsNone(getattr(session, field))
        self.assertIsNotNone(session.last_error)
        connection.close.assert_called_once()

    async def test_health_log_command_failure_is_reported_immediately_without_raw_stderr(self):
        selected = profile()
        record = new_record(1, selected)
        session = SimpleNamespace(run=AsyncMock(return_value=CommandResult("", "架空の非公開詳細", 1, 1)))
        runner = DeployRunner(session, selected, record)
        step = next(step for step in record.steps if step.key == "health")
        with self.assertRaisesRegex(StepFailed, "journal_sudo") as error:
            await runner._logs_since(step, "架空のログコマンド")
        session.run.assert_awaited_once()
        self.assertNotIn("非公開詳細", str(error.exception))
        self.assertFalse(step.checks[-1].ok)
