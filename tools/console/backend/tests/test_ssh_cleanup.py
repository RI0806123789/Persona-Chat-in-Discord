"""単発SSHコマンドの開始・終了・回収を外部通信なしで検証する。"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "tests" / "helpers"))

import asyncssh

from network_guard import block_network
from ssh_session import CommandStateUnknownError, SshError, SshSession


class SshCleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        block_network(self)

    async def asyncSetUp(self):
        self.started, self.release, self.waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.tasks = []
        self.result = SimpleNamespace(stdout="架空の結果", stderr="", exit_status=0)

        async def finish(**kwargs):
            self.started.set()
            await self.release.wait()
            return self.result

        self.process = SimpleNamespace(wait=AsyncMock(side_effect=finish), close=Mock(), wait_closed=AsyncMock())
        self.connection = SimpleNamespace(
            create_process=AsyncMock(return_value=self.process), run=AsyncMock(return_value=self.result),
            close=Mock(), wait_closed=AsyncMock(), get_server_host_key=Mock(return_value=None),
        )
        self.session = SshSession()
        self.session._conn = self.connection

    async def asyncTearDown(self):
        self.release.set()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def start(self, **kwargs):
        task = asyncio.create_task(self.session.run(
            "架空のコマンド", label="回収の確認", wait_for_exit=True, on_waiting=self.waiting.set, **kwargs,
        ))
        self.tasks.append(task)
        return task

    def assert_reclaimed(self):
        self.process.close.assert_called_once()
        self.process.wait_closed.assert_awaited_once()
        self.assertFalse(self.session.command_state_unknown)

    async def test_normal_exit_waits_for_channel_cleanup(self):
        """正常終了でもチャネルを回収して結果と履歴を残す。"""
        self.release.set()
        result = await self.start(input="架空の入力")
        self.assertEqual(result.stdout, "架空の結果")
        self.assertEqual(result.exit_status, 0)
        self.connection.create_process.assert_awaited_once_with("架空のコマンド", input="架空の入力", errors="replace")
        self.assert_reclaimed()
        self.assertTrue(self.session.history[-1].ok)

    async def test_timeout_waits_for_actual_exit_before_raising(self):
        """期限切れだけで処理を残して戻らず、実際の終了後にエラーを返す。"""
        task = self.start(timeout=0.01)
        await self.waiting.wait()
        self.assertFalse(task.done())
        self.process.close.assert_not_called()
        self.release.set()
        with self.assertRaisesRegex(SshError, "終了を確認"):
            await task
        self.assert_reclaimed()
        self.assertFalse(self.session.history[-1].ok)

    async def test_repeated_cancellation_waits_for_actual_exit(self):
        """繰り返しキャンセルでも、遠隔処理とチャネルを回収してから伝える。"""
        task = self.start()
        await self.started.wait()
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
        self.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_reclaimed()
        self.assertFalse(self.session.history[-1].ok)

    async def test_cancellation_after_timeout_still_waits_for_exit(self):
        """期限切れ後にキャンセルされても回収を続ける。"""
        task = self.start(timeout=0.01)
        await self.waiting.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_reclaimed()

    async def test_cancellation_during_creation_keeps_process_reference(self):
        """プロセス生成の待機中も参照を失わず、生成後に回収する。"""
        creating, allow_creation = asyncio.Event(), asyncio.Event()

        async def create(*args, **kwargs):
            creating.set()
            await allow_creation.wait()
            return self.process

        self.connection.create_process.side_effect = create
        task = self.start()
        try:
            await creating.wait()
            task.cancel()
            await self.waiting.wait()
            self.assertFalse(task.done())
        finally:
            allow_creation.set()
            self.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_reclaimed()

    async def test_repeated_cancellation_during_channel_cleanup_waits(self):
        """終了後のチャネル回収中も追加キャンセルで打ち切らない。"""
        closing, allow_close = asyncio.Event(), asyncio.Event()

        async def close():
            closing.set()
            await allow_close.wait()

        self.process.wait_closed.side_effect = close
        self.release.set()
        task = self.start()
        try:
            await closing.wait()
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
        finally:
            allow_close.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_reclaimed()

    async def test_missing_exit_status_and_signal_exit_remain_unknown(self):
        """チャネル切断やシグナル終了だけでは更新完了と扱わない。"""
        for status in (None, -1):
            with self.subTest(status=status):
                self.session = SshSession()
                self.session._conn = self.connection
                self.result.exit_status = status
                self.release.set()
                with self.assertRaises(CommandStateUnknownError):
                    await self.start()
                self.assertTrue(self.session.command_state_unknown)
                self.assertFalse(self.session.history[-1].ok)
                with self.assertRaises(CommandStateUnknownError):
                    await self.session.run("次の更新", label="拒否の確認", wait_for_exit=True)
                result = await self.session.run("状態の読み取り", label="読み取りの確認")
                self.assertEqual(result.exit_status, status)
                self.assertTrue(self.session.command_state_unknown)

    async def test_connection_loss_keeps_unknown_state_after_local_cleanup(self):
        """通信断ではローカルの回収後も遠隔の終了未確認を保持する。"""
        self.process.wait.side_effect = asyncssh.ConnectionLost("架空の接続断")
        with self.assertRaises(CommandStateUnknownError):
            await self.start()
        self.process.close.assert_called_once()
        self.process.wait_closed.assert_awaited_once()
        self.assertTrue(self.session.command_state_unknown)

    async def test_read_only_timeout_returns_without_waiting_for_remote_exit(self):
        """読み取りは指定した期限でエラーになり、遠隔の終了待ちへ切り替えない。"""
        async def read(command, **kwargs):
            self.started.set()
            await asyncio.wait_for(self.release.wait(), timeout=kwargs["timeout"])
            return self.result

        self.connection.run.side_effect = read
        task = asyncio.create_task(self.session.run(
            "状態の読み取り", label="読み取りの確認", input="架空の入力", timeout=0.01,
            on_waiting=self.waiting.set,
        ))
        self.tasks.append(task)
        try:
            with self.assertRaisesRegex(SshError, "0.01 秒以内に応答がありませんでした。$"):
                await asyncio.wait_for(asyncio.shield(task), timeout=1)
            self.assertFalse(self.release.is_set())
            self.assertFalse(self.waiting.is_set())
            self.assertFalse(self.session.command_state_unknown)
            self.assertFalse(self.session.history[-1].ok)
            self.connection.run.assert_awaited_once_with(
                "状態の読み取り", input="架空の入力", check=False, timeout=0.01, errors="replace",
            )
            self.connection.create_process.assert_not_awaited()
        finally:
            self.release.set()

    async def test_read_only_communication_failure_allows_reconnect(self):
        """読み取り中のSSH・OS通信断は状態不明にせず、再接続して取得を再開できる。"""
        selected = SimpleNamespace(username=None, host="test.invalid", port=None)
        for error in (asyncssh.ConnectionLost("架空の接続断"), OSError("架空の通信失敗")):
            with self.subTest(error=type(error).__name__):
                self.session = SshSession()
                self.session._conn = self.connection

                async def fail(*args, **kwargs):
                    self.session._handle_connection_lost(self.connection, error)
                    raise error

                self.connection.run.side_effect = fail
                with self.assertRaises(SshError) as raised:
                    await self.session.run("状態の読み取り", label="読み取りの確認")
                self.assertNotIsInstance(raised.exception, CommandStateUnknownError)
                self.assertFalse(self.session.command_state_unknown)
                self.assertFalse(self.session.connected)
                self.connection.run.side_effect = None
                with patch.object(self.session, "_build_options", return_value={}), patch(
                    "ssh_session.asyncssh.connect", new=AsyncMock(return_value=self.connection),
                ):
                    await self.session.connect(selected)
                self.assertTrue(self.session.connected)
                self.assertEqual((await self.session.run("状態の読み取り", label="再取得")).exit_status, 0)
                self.assertFalse(self.session.command_state_unknown)

    async def test_read_only_cancellation_returns_without_waiting_for_exit(self):
        """読み取りのキャンセルも終了待ちにせず、状態不明を立てない。"""
        async def read(*args, **kwargs):
            self.started.set()
            await self.release.wait()
            return self.result

        self.connection.run.side_effect = read
        task = asyncio.create_task(self.session.run("状態の読み取り", label="読み取りの確認"))
        self.tasks.append(task)
        await asyncio.wait_for(self.started.wait(), 1)
        task.cancel()
        try:
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), 1)
            self.assertFalse(self.release.is_set())
            self.assertFalse(self.session.command_state_unknown)
        finally:
            self.release.set()

    async def test_cleanup_failure_is_also_unknown(self):
        """回収中の通信失敗でも成功を返さない。"""
        self.release.set()
        self.process.wait_closed.side_effect = OSError("架空の回収失敗")
        with self.assertRaises(CommandStateUnknownError):
            await self.start()
        self.assertTrue(self.session.command_state_unknown)

    async def test_cancellation_with_connection_loss_propagates_after_cleanup(self):
        """中断後の通信断も回収を待ち、状態不明とキャンセルの両方を保つ。"""
        async def finish(**kwargs):
            self.started.set()
            await self.release.wait()
            raise asyncssh.ConnectionLost("架空の接続断")

        self.process.wait.side_effect = finish
        task = self.start()
        await self.started.wait()
        task.cancel()
        await self.waiting.wait()
        self.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.session.command_state_unknown)
        self.process.close.assert_called_once()
        self.process.wait_closed.assert_awaited_once()
