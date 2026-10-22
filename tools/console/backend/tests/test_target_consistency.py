"""接続先照合・状態更新とWebSocket終了を外部接続なしで確認する。"""
import asyncio
import importlib.util
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import app
from deploy import DeployManager, DeployRunner, StepFailed, new_record
from ssh_session import CommandResult
from test_api import request
from test_connection_lifecycle import profile


class TargetConsistencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_deploy_requires_confirmed_profile_and_connection_time(self):
        selected = profile()
        session = SimpleNamespace(require_profile=lambda: selected, connected_at=100)
        manager = SimpleNamespace(running=False, current=None, start=Mock())
        with patch.object(app, "session", session), patch.object(app, "deploys", manager):
            for target in (None, {"profile_id": "old", "connected_at": 100}, {"profile_id": selected.id, "connected_at": 99}):
                status, _ = await request("/api/deploy", method="POST", body=target)
                self.assertEqual(status, 409)
            manager.start.assert_not_called()
            status, _ = await request("/api/deploy", method="POST", body={"profile_id": selected.id, "connected_at": 100})
            self.assertEqual(status, 200)
            self.assertIs(manager.start.call_args.kwargs["on_finished"], app._invalidate_insights)

    async def test_service_success_and_deploy_completion_invalidate_insights(self):
        selected = profile()
        session = SimpleNamespace(require_profile=lambda: selected, connected_at=100,
            run=AsyncMock(return_value=CommandResult("", "", 0, 1)), require_command_completion=lambda: None)
        app._insights_cache["fictional"] = (0, 0, {})
        with patch.object(app, "session", session), patch.object(app, "deploys", SimpleNamespace(running=False)):
            operation = await app.control_service("restart", app.ServiceTarget(profile_id=selected.id, connected_at=100))
        self.assertTrue(operation["ok"])
        self.assertFalse(app._insights_cache)
        manager = DeployManager()
        with patch.object(DeployRunner, "run", AsyncMock()):
            manager.start(session, selected, on_finished=app._invalidate_insights)
            app._insights_cache["fictional"] = (0, 0, {})
            await manager.wait()
        self.assertFalse(app._insights_cache)

    async def test_inflight_insights_cannot_repopulate_invalidated_cache(self):
        selected = profile()
        async def run(*args, **kwargs):
            app._invalidate_insights()
            return CommandResult('{"old":true}', "", 0, 1)
        session = SimpleNamespace(require_profile=lambda: selected, generation=1, connected=True, run=run)
        app._invalidate_insights()
        with patch.object(app, "session", session):
            await app.get_insights()
        self.assertFalse(app._insights_cache)

    async def test_server_metadata_is_not_written_after_generation_change_or_command_failure(self):
        selected = profile()
        for changed, exit_status in ((True, 0), (False, 1)):
            session = SimpleNamespace(connect=AsyncMock(), generation=1, connected=True, server_info=None, snapshot=lambda: {})
            async def run(*args, **kwargs):
                if changed:
                    session.generation += 1
                    session.server_info = {"hostname": "新接続の値"}
                return CommandResult("old metadata", "", exit_status, 1)
            session.run = run
            with patch.object(app, "session", session), patch.object(app, "load_profiles", return_value=({selected.id: selected}, "fixture")), patch.object(app.rc, "parse_server_info") as parse:
                await app._connect(app.ConnectRequest(profile_id=selected.id))
            parse.assert_not_called()
            self.assertEqual(session.server_info, {"hostname": "新接続の値"} if changed else None)

    async def test_head_rejects_nonzero_exit_even_with_valid_looking_stdout(self):
        selected = profile()
        session = SimpleNamespace(run=AsyncMock(return_value=CommandResult("main\n" + "a" * 40, "", 1, 1)))
        record = new_record(1, selected)
        runner = DeployRunner(session, selected, record)
        with self.assertRaises(StepFailed):
            await runner._head(record.steps[0])

    async def test_websocket_disconnect_during_send_reaps_both_directions_and_process(self):
        selected = profile()
        async def lines():
            yield '{"MESSAGE":"架空のログ"}'
        process = SimpleNamespace(stdout=lines(), stderr=SimpleNamespace(read=AsyncMock(return_value="")),
                                  close=Mock(), wait_closed=AsyncMock())
        receiving_done = asyncio.Event()
        async def receive():
            try:
                await asyncio.Future()
            finally:
                receiving_done.set()
        websocket = SimpleNamespace(headers={"origin": "http://127.0.0.1:5173"}, accept=AsyncMock(),
            send_json=AsyncMock(side_effect=app.WebSocketDisconnect()), receive=receive, close=AsyncMock())
        session = SimpleNamespace(require_profile=lambda: selected, open_process=AsyncMock(return_value=process))
        with patch.object(app, "session", session):
            await app.stream_logs(websocket)
        self.assertTrue(receiving_done.is_set())
        process.close.assert_called_once()
        process.wait_closed.assert_awaited_once()

    async def test_repeated_websocket_handler_cancellation_waits_for_transfer_cleanup(self):
        receiving = asyncio.Event()
        cleaning = asyncio.Event()
        release = asyncio.Event()
        async def lines():
            try:
                receiving.set()
                await asyncio.Future()
                yield ""
            finally:
                cleaning.set()
                await release.wait()
        process = SimpleNamespace(stdout=lines(), stderr=SimpleNamespace(read=AsyncMock(return_value="")), close=Mock(), wait_closed=AsyncMock())
        websocket = SimpleNamespace(headers={"origin": "http://127.0.0.1:5173"}, accept=AsyncMock(), send_json=AsyncMock(), receive=AsyncMock(side_effect=lambda: asyncio.Future()), close=AsyncMock())
        async def receive():
            await asyncio.Future()
        websocket.receive = receive
        session = SimpleNamespace(require_profile=profile, open_process=AsyncMock(return_value=process))
        with patch.object(app, "session", session):
            task = asyncio.create_task(app.stream_logs(websocket))
            try:
                await asyncio.wait_for(receiving.wait(), 2)
                task.cancel()
                await asyncio.wait_for(cleaning.wait(), 2)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        process.close.assert_called_once()
        process.wait_closed.assert_awaited_once()
        websocket.close.assert_awaited_once()


class SummaryIndexTests(unittest.TestCase):
    def test_direct_import_uses_feedback_period_index_and_preserves_database(self):
        script = Path(__file__).resolve().parents[1] / "remote_scripts/db_summary.py"
        spec = importlib.util.spec_from_file_location("test_db_summary_module", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "bot.db"
            con = sqlite3.connect(db_path)
            try:
                con.executescript("CREATE TABLE feedback(guild_id INTEGER, created_at TEXT, rating INTEGER); CREATE INDEX idx_feedback_guild_created ON feedback(guild_id,created_at);")
                con.execute("INSERT INTO feedback VALUES(1,datetime('now'),1)")
                con.commit()
            finally:
                con.close()
            original = db_path.read_bytes()
            statements = []
            connect = sqlite3.connect
            def traced(*args, **kwargs):
                connection = connect(*args, **kwargs)
                connection.set_trace_callback(statements.append)
                return connection
            with patch.object(module.sqlite3, "connect", side_effect=traced):
                result = module.collect(str(db_path), str(Path(directory) / "blacklist.json"))
            self.assertEqual(result["feedback"], {"good": 1, "bad": 0})
            query = next(sql for sql in statements if "SUM(rating = 1)" in sql)
            con = connect(db_path)
            try:
                plans = " ".join(row[3] for row in con.execute("EXPLAIN QUERY PLAN " + query))
                self.assertIn("SEARCH feedback USING INDEX idx_feedback_guild_created", plans)
                self.assertIn("created_at>? AND created_at<?", plans)
            finally:
                con.close()
            self.assertEqual(db_path.read_bytes(), original)
