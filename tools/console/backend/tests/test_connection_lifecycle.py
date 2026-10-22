"""デプロイと接続変更の排他・終了順序を、外部通信なしで検証する。"""

import asyncio
from collections import deque
from contextlib import ExitStack
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
    async def test_unknown_deploy_keeps_api_exclusion_after_task_finishes(self):
        """終了未確認のデプロイはタスク完了後もサービス操作・再デプロイを拒否する。"""
        selected = profile()
        manager = DeployManager()
        manager.current = new_record(1, selected)
        manager.current.status = "unknown"
        session = SimpleNamespace(require_profile=lambda: selected, connected_at=100,
                                  require_command_completion=lambda: None, run=AsyncMock())
        target = {"profile_id": selected.id, "connected_at": 100}
        with patch.object(app, "session", session), patch.object(app, "deploys", manager), patch.object(
            app, "_connection_change_lock", asyncio.Lock(),
        ):
            for path in ("/api/service/start", "/api/service/stop", "/api/service/restart", "/api/deploy"):
                with self.subTest(path=path):
                    status, _ = await request(path, method="POST", body=target)
                    self.assertEqual(status, 409)
        session.run.assert_not_awaited()

    async def test_running_deploy_rejects_connect_and_disconnect_before_side_effects(self):
        session = SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock())
        with patch.object(app, "session", session), patch.object(app, "deploys", SimpleNamespace(active=True)), patch.object(
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
            app, "deploys", SimpleNamespace(active=False),
        ):
            async with app._connection_change_lock:
                task = asyncio.create_task(app.connect(app.ConnectRequest(profile_id="test")))
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                app.deploys.active = True
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


class ServiceOperationConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        self.selected = profile()
        self.target = app.ServiceTarget(profile_id=self.selected.id, connected_at=100)
        self.started, self.release = asyncio.Event(), asyncio.Event()
        self.events = []
        self.tasks = []

        async def run(command, **kwargs):
            self.events.append("service-start")
            self.started.set()
            await self.release.wait()
            self.events.append("service-end")
            return CommandResult("", "", 0, 1)

        self.session = SimpleNamespace(
            require_profile=Mock(return_value=self.selected), connected_at=100,
            require_command_completion=lambda: None,
            run=AsyncMock(side_effect=run), disconnect=AsyncMock(side_effect=lambda: self.events.append("disconnect")),
            snapshot=Mock(return_value={}),
        )
        self.manager = SimpleNamespace(running=False, active=False, current=None)

        def start(*args, **kwargs):
            self.events.append("deploy-start")
            self.manager.running = True

        self.manager.start = Mock(side_effect=start)
        self.resources.enter_context(patch.multiple(
            app, session=self.session, deploys=self.manager,
            _connection_change_lock=asyncio.Lock(), operations=deque(),
            _invalidate_insights=Mock(),
        ))

    async def asyncTearDown(self):
        self.release.set()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        return task

    async def begin_service(self, action="stop"):
        task = self.spawn(app.control_service(action, self.target))
        await asyncio.wait_for(self.started.wait(), 2)
        return task

    async def test_deploy_waits_for_each_service_action_to_finish(self):
        for action in ("stop", "start", "restart"):
            with self.subTest(action=action):
                self.started.clear()
                self.release.clear()
                self.events.clear()
                self.manager.running = False
                self.manager.start.reset_mock()
                service = await self.begin_service(action)
                deploy = self.spawn(app.start_deploy(self.target))
                await asyncio.sleep(0)
                self.assertFalse(deploy.done())
                self.manager.start.assert_not_called()
                self.release.set()
                operation, snapshot = await asyncio.wait_for(asyncio.gather(service, deploy), 2)
                self.assertTrue(operation["ok"])
                self.assertTrue(snapshot["running"])
                self.assertEqual(self.events, ["service-start", "service-end", "deploy-start"])

    async def test_running_deploy_rejects_service_without_command(self):
        self.manager.running = True
        with self.assertRaises(app.HTTPException) as error:
            await app.control_service("restart", self.target)
        self.assertEqual(error.exception.status_code, 409)
        self.session.run.assert_not_awaited()
        self.assertFalse(app._connection_change_lock.locked())

    async def test_service_actions_are_serialized(self):
        first = await self.begin_service("stop")
        second = self.spawn(app.control_service("start", self.target))
        await asyncio.sleep(0)
        self.assertFalse(second.done())
        self.session.run.assert_awaited_once()
        self.release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 2)
        self.assertEqual(self.events, ["service-start", "service-end", "service-start", "service-end"])

    async def test_connection_changes_wait_for_service_to_finish(self):
        async def connect(body):
            self.events.append("connect")
            self.session.connected_at = 200
            return {}

        with patch.object(app, "_connect", new=AsyncMock(side_effect=connect)) as connecting:
            service = await self.begin_service()
            connection = self.spawn(app.connect(app.ConnectRequest(profile_id=self.selected.id)))
            disconnect = self.spawn(app.disconnect())
            await asyncio.sleep(0)
            connecting.assert_not_awaited()
            self.session.disconnect.assert_not_awaited()
            self.release.set()
            await asyncio.wait_for(asyncio.gather(service, connection, disconnect), 2)
        self.assertEqual(self.events, ["service-start", "service-end", "connect", "disconnect"])

    async def test_waiting_service_rechecks_connection_before_sending_command(self):
        async with app._connection_change_lock:
            service = self.spawn(app.control_service("stop", self.target))
            await asyncio.sleep(0)
            self.assertFalse(service.done())
            self.session.connected_at = 200
        with self.assertRaises(app.HTTPException) as error:
            await service
        self.assertEqual(error.exception.status_code, 409)
        self.session.run.assert_not_awaited()

    async def test_waiting_service_rechecks_deploy_state_after_lock(self):
        async with app._connection_change_lock:
            service = self.spawn(app.control_service("stop", self.target))
            await asyncio.sleep(0)
            self.manager.running = True
        with self.assertRaises(app.HTTPException) as error:
            await service
        self.assertEqual(error.exception.status_code, 409)
        self.session.run.assert_not_awaited()

    async def test_repeated_cancellation_keeps_lock_until_sent_operation_finishes(self):
        service = await self.begin_service()
        deploy = self.spawn(app.start_deploy(self.target))
        for _ in range(2):
            service.cancel()
            await asyncio.sleep(0)
            self.assertFalse(service.done())
            self.assertTrue(app._connection_change_lock.locked())
            self.manager.start.assert_not_called()
        self.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(service, 2)
        self.assertTrue((await asyncio.wait_for(deploy, 2))["running"])
        self.assertEqual(self.events, ["service-start", "service-end", "deploy-start"])

    async def test_cancellation_while_waiting_does_not_send_command_or_keep_lock(self):
        async with app._connection_change_lock:
            service = self.spawn(app.control_service("stop", self.target))
            await asyncio.sleep(0)
            service.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await service
            self.session.run.assert_not_awaited()
        self.release.set()
        self.assertTrue((await app.control_service("stop", self.target))["ok"])
        self.assertFalse(app._connection_change_lock.locked())

    async def test_command_error_releases_lock_for_next_operation(self):
        self.session.run.side_effect = SshError("架空の操作失敗")
        with self.assertRaises(SshError):
            await app.control_service("stop", self.target)
        self.assertFalse(app._connection_change_lock.locked())
        self.session.run.side_effect = None
        self.session.run.return_value = CommandResult("", "", 0, 1)
        self.assertTrue((await app.control_service("restart", self.target))["ok"])
