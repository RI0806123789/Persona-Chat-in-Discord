"""API の入口・集計キャッシュ・WebSocket の終了を、本番に触れずに検証する。"""

import asyncio
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app
import remote_commands as rc
from fake_ssh_server import start_fake_server
from profiles import Profile
from ssh_session import CommandResult, NotConnectedError, SshError, SshSession


async def request(path, *, method="GET", host="127.0.0.1", header=True, body=None):
    """追加の HTTP クライアントに依存せず、ASGI の入口から要求を送る。"""
    headers = [(b"host", host.encode()), (b"content-type", b"application/json")]
    if header:
        headers.append((app.CLIENT_HEADER.encode(), app.CLIENT_HEADER_VALUE.encode()))
    messages = asyncio.Queue()
    messages.put_nowait({"type": "http.request", "body": json.dumps(body).encode() if body else b""})
    sent = []

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
             "root_path": "", "query_string": b"", "headers": headers,
             "client": ("127.0.0.1", 1234), "server": ("127.0.0.1", app.PORT)}
    await app.app(scope, messages.get, send)
    status = next(message["status"] for message in sent if message["type"] == "http.response.start")
    content = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    return status, content


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.acceptor, self.bot, port, self.live = await start_fake_server(
            "127.0.0.1", 0, Path(self.temp.name), live_interval=(0.05, 0.1)
        )
        self.profile = Profile(
            id="test", label="検証", environment="development", host="127.0.0.1", port=port,
            username="tester", key_path=str(Path(self.temp.name) / "client_key"),
            known_hosts=str(Path(self.temp.name) / "known_hosts"), use_ssh_config=False,
            service="bot", bot_dir="/srv/test-bot", journal_sudo=False, note="",
        )
        self.session = SshSession()
        self.patch = mock.patch.object(app, "session", self.session)
        self.patch.start()
        app._insights_cache.clear()
        app._insights_lock = asyncio.Lock()
        await self.session.connect(self.profile)

    async def asyncTearDown(self):
        await self.session.disconnect()
        self.patch.stop()
        self.live.cancel()
        await asyncio.gather(self.live, return_exceptions=True)
        self.acceptor.close()
        await self.acceptor.wait_closed()
        self.temp.cleanup()

    async def test_http_rejects_missing_header_and_foreign_host(self):
        for path in ("/api/profiles", "/api/status", "/api/insights", "/api/history"):
            with self.subTest(path=path):
                self.assertEqual((await request(path, header=False))[0], 403)
                self.assertEqual((await request(path, host="foreign.example"))[0], 400)
        status, body = await request("/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["service"]["active_state"], "active")

    async def test_connect_accepts_only_a_profile_id(self):
        status, _ = await request("/api/connect", method="POST", body={"profile_id": "test", "host": "foreign.example"})
        self.assertEqual(status, 422)
        status, _ = await request("/api/service/reboot", method="POST")
        self.assertEqual(status, 400)

    async def test_insights_cache_coalesces_requests_and_refresh_bypasses_it(self):
        with mock.patch.object(self.session, "run", wraps=self.session.run) as run:
            results = await asyncio.gather(*(app.get_insights() for _ in range(4)))
            self.assertEqual(run.await_count, 1)
            self.assertEqual(sum(not result["cached"] for result in results), 1)
            await app.get_insights(refresh=True)
            self.assertEqual(run.await_count, 2)
            key = next(iter(app._insights_cache))
            saved_at, fetched_at, data = app._insights_cache[key]
            app._insights_cache[key] = (saved_at - 61, fetched_at, data)
            self.assertFalse((await app.get_insights())["cached"])
            self.assertEqual(run.await_count, 3)

    async def test_reconnect_with_same_id_invalidates_cache(self):
        await app.get_insights()
        await self.session.connect(replace(self.profile, journal_sudo=True))
        with mock.patch.object(self.session, "run", wraps=self.session.run) as run:
            self.assertFalse((await app.get_insights())["cached"])
            self.assertIn("--journal-sudo", run.call_args.args[0])

    async def test_connection_change_during_collection_does_not_cache_old_data(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def collect(*_args, **_kwargs):
            started.set()
            await release.wait()
            return CommandResult("{}", "", 0, 1)

        with mock.patch.object(self.session, "run", side_effect=collect):
            task = asyncio.create_task(app.get_insights())
            await started.wait()
            await self.session.disconnect()
            release.set()
            with self.assertRaises(NotConnectedError):
                await task
        self.assertFalse(app._insights_cache)

    async def test_bad_insights_output_is_not_cached_or_exposed(self):
        for stdout, stderr, code in (("[]", "", 0), ("invalid", "", 0), ("", "private detail", 1)):
            with self.subTest(stdout=stdout, code=code):
                with mock.patch.object(self.session, "run", return_value=CommandResult(stdout, stderr, code, 1)):
                    with self.assertRaises(SshError) as raised:
                        await app.get_insights()
                    self.assertNotIn("private detail", str(raised.exception))
                    self.assertFalse(app._insights_cache)

    async def test_invalid_resource_numbers_and_failed_commands_do_not_break_status(self):
        original = self.session.run
        for exit_status in (0, 1):
            async def run(command, **kwargs):
                if command == rc.RESOURCES_COMMAND:
                    return CommandResult("nan 0\ninf 0 0\n0\n", "private detail", exit_status, 1)
                return await original(command, **kwargs)

            with self.subTest(exit_status=exit_status), mock.patch.object(self.session, "run", side_effect=run):
                status, body = await request("/api/status")
                self.assertEqual(status, 200)
                data = json.loads(body)
                self.assertIsNone(data["service"]["uptime_seconds"])
                if exit_status:
                    self.assertTrue(data["resources_error"])
                else:
                    self.assertIsNone(data["resources"]["load"])
                self.assertNotIn(b"private detail", body)

    async def websocket(self, *, origin="http://127.0.0.1:5173", host="127.0.0.1"):
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        headers = [(b"host", host.encode())]
        if origin is not None:
            headers.append((b"origin", origin.encode()))
        scope = {"type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.4"},
                 "scheme": "ws", "path": "/api/logs/stream", "raw_path": b"/api/logs/stream",
                 "root_path": "", "query_string": b"lines=1", "headers": headers,
                 "client": ("127.0.0.1", 1234), "server": ("127.0.0.1", app.PORT), "subprotocols": []}
        await incoming.put({"type": "websocket.connect"})
        task = asyncio.create_task(app.app(scope, incoming.get, outgoing.put))
        self.addAsyncCleanup(self.close_websocket, task, incoming)
        return task, incoming, outgoing

    async def close_websocket(self, task, incoming):
        if task.cancelled():
            return
        await incoming.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(task, timeout=5)

    async def test_websocket_rejects_missing_foreign_or_null_origin_and_foreign_host(self):
        for origin, host in ((None, "127.0.0.1"), ("null", "127.0.0.1"),
                             ("http://foreign.example", "127.0.0.1"),
                             ("http://127.0.0.1:5173", "foreign.example")):
            with self.subTest(origin=origin, host=host):
                task, _, outgoing = await self.websocket(origin=origin, host=host)
                await asyncio.wait_for(task, timeout=3)
                messages = []
                while not outgoing.empty():
                    messages.append(outgoing.get_nowait())
                self.assertFalse(any(message["type"] == "websocket.accept" for message in messages))
        self.assertFalse(self.bot.subscribers)

    async def test_websocket_stream_closes_remote_channel_when_client_leaves(self):
        task, incoming, outgoing = await self.websocket()
        self.assertEqual((await asyncio.wait_for(outgoing.get(), 3))["type"], "websocket.accept")
        for _ in range(3):
            message = await asyncio.wait_for(outgoing.get(), 3)
            self.assertEqual(json.loads(message["text"])["type"], "entry")
        await self.close_websocket(task, incoming)
        for _ in range(50):
            if not self.bot.subscribers:
                break
            await asyncio.sleep(0.01)
        self.assertFalse(self.bot.subscribers)
        self.assertEqual((await self.session.run("true", label="疎通確認")).exit_status, 0)

    async def test_cancelled_websocket_handler_closes_remote_channel(self):
        task, _, outgoing = await self.websocket()
        while (await asyncio.wait_for(outgoing.get(), 3))["type"] != "websocket.send":
            pass
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        for _ in range(50):
            if not self.bot.subscribers:
                break
            await asyncio.sleep(0.01)
        self.assertFalse(self.bot.subscribers)
