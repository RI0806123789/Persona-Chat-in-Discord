"""サーバー設定と記念日の記録の保存が、並行・キャンセル時も順序を保つことを確かめる（#79）。"""

import asyncio
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.core.app_state import GuildSettings


class BlockingDatabase:
    """最初の書き込みだけを止められる、架空の保存先。"""

    def __init__(self) -> None:
        self.calls = 0
        self.saved: list[str] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()

    def _write(self, value: str) -> None:
        with self._lock:
            self.calls += 1
            first = self.calls == 1
        if first:
            self.started.set()
            if not self.release.wait(5):
                raise AssertionError("書き込みが解放されませんでした")
        self.saved.append(value)

    def guild_settings_save(self, guild_key, **values) -> None:
        self._write(values["model_name"])

    def anniversary_post_record(self, guild_key, user_id, label, posted_date) -> None:
        self._write(posted_date)


class GuildSettingsPersistOrderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = BlockingDatabase()
        # 失敗したテストでもワーカースレッドを残さない。
        self.addCleanup(self.db.release.set)
        self.settings = GuildSettings(current_model_name="fictional-old")
        self.settings.attach(self.db, 1)

    async def start_blocked(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.assertTrue(await asyncio.to_thread(self.db.started.wait, 5))
        return task

    async def test_later_persist_waits_and_writes_latest_values(self) -> None:
        first = await self.start_blocked(self.settings.persist())
        self.settings.current_model_name = "fictional-new"
        second = asyncio.create_task(self.settings.persist())
        await asyncio.sleep(0.05)
        # 先の書き込みが終わるまで、次の保存は書き込みを始めない。
        self.assertEqual(self.db.calls, 1)
        self.db.release.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=5)
        # 最後に書かれるのは最新の値（古い値で上書きしない）。
        self.assertEqual(self.db.saved, ["fictional-old", "fictional-new"])

    async def test_canceled_persist_finishes_write_before_next_one(self) -> None:
        first = await self.start_blocked(self.settings.persist())
        first.cancel()
        await asyncio.sleep(0.05)
        # キャンセルされても、書き込みが終わるまでは戻らない。
        self.assertFalse(first.done())
        self.settings.current_model_name = "fictional-new"
        second = asyncio.create_task(self.settings.persist())
        await asyncio.sleep(0.05)
        self.assertEqual(self.db.calls, 1)
        self.db.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(first, timeout=5)
        await asyncio.wait_for(second, timeout=5)
        self.assertEqual(self.db.saved, ["fictional-old", "fictional-new"])

    async def test_canceled_anniversary_record_finishes_write(self) -> None:
        task = await self.start_blocked(
            self.settings.record_anniversary_post("test-user", "架空の記念日", "2026-01-01")
        )
        task.cancel()
        await asyncio.sleep(0.05)
        self.assertFalse(task.done())
        self.db.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        self.assertEqual(self.db.saved, ["2026-01-01"])
        self.assertEqual(self.settings.anniversary_posted_dates, {"test-user:架空の記念日": "2026-01-01"})


if __name__ == "__main__":
    unittest.main()
