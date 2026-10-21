"""サーバー設定と記念日の記録の保存が、並行・キャンセル時も順序を保つことを確かめる（#79）。"""

import asyncio
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from persona_bot.core.app_state import GuildSettings
from persona_bot.config import choices, constants, personas
from tests.helpers.persona_helpers import install_persona_catalog


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


class GuildSettingsRestoreValidationTests(unittest.TestCase):
    def test_every_allowed_auto_topic_interval_is_restored(self) -> None:
        for choice in choices.AUTO_TOPIC_INTERVAL_CHOICES:
            with self.subTest(interval=choice.value):
                settings = GuildSettings()
                settings.apply_persisted({"auto_topic_interval_hours": choice.value})
                self.assertEqual(settings.auto_topic_interval_hours, choice.value)

    def test_invalid_auto_topic_intervals_use_default_without_logging_values(self) -> None:
        for interval in (True, False, 0, -1, 2, 25, 1.0, "1", [], {"架空": "設定"}):
            with self.subTest(interval=interval), patch("builtins.print") as logging:
                settings = GuildSettings(auto_topic_interval_hours=24)
                settings.apply_persisted({"auto_topic_interval_hours": interval})
                self.assertEqual(settings.auto_topic_interval_hours, constants.DEFAULT_AUTO_TOPIC_INTERVAL_HOURS)
                logging.assert_called_once()
                self.assertNotIn(str(interval), str(logging.call_args_list))

    def test_missing_auto_topic_interval_keeps_current_value(self) -> None:
        for values in ({}, {"auto_topic_interval_hours": None}):
            with self.subTest(values=values):
                settings = GuildSettings(auto_topic_interval_hours=24)
                settings.apply_persisted(values)
                self.assertEqual(settings.auto_topic_interval_hours, 24)

    def test_allowed_settings_restore_with_existing_prompt(self) -> None:
        install_persona_catalog(self, "../prompts/架空のペルソナ.txt")
        values = {
            "prompt_file": "../prompts/架空のペルソナ.txt",
            "model_name": choices.MODEL_CHOICES[0].value,
            "respond_mode": "all",
        }
        settings = GuildSettings()
        settings.apply_persisted(values)
        self.assertEqual(settings.current_prompt_file, values["prompt_file"])
        self.assertEqual(settings.current_model_name, values["model_name"])
        self.assertEqual(settings.current_respond_mode, "all")

    def test_disallowed_values_use_defaults_without_logging_saved_values(self) -> None:
        for value in ("架空の廃止された値", ["不正な型"], {"不正": "型"}):
            with self.subTest(value=value), patch("builtins.print") as logging:
                settings = GuildSettings(current_prompt_file="previous", current_model_name="previous", current_respond_mode="previous")
                settings.apply_persisted({key: value for key in ("prompt_file", "model_name", "respond_mode")})
                self.assertEqual(settings.current_prompt_file, personas.FOLLOW_DEFAULT_PERSONA)
                self.assertEqual(settings.current_model_name, constants.DEFAULT_MODEL_NAME)
                self.assertEqual(settings.current_respond_mode, constants.DEFAULT_RESPOND_MODE)
                self.assertEqual(logging.call_count, 3)
                self.assertNotIn(str(value), str(logging.call_args_list))

    def test_missing_prompt_file_keeps_value_and_responds_with_default(self) -> None:
        # Git の更新でキャラ別のファイルが一時的に消えても、保存値は残して既定で応答する。
        # ファイルを戻して一覧を読み直せば、元のペルソナに戻る。
        install_persona_catalog(self, "../prompts/既定.txt", default="../prompts/既定.txt")
        settings = GuildSettings(current_model_name=choices.MODEL_CHOICES[0].value)
        with patch("builtins.print") as logging:
            settings.apply_persisted({"prompt_file": "../prompts/消えたキャラ.txt"})
        self.assertEqual(settings.current_prompt_file, "../prompts/消えたキャラ.txt")
        self.assertEqual(settings.snapshot_response().prompt_file, "../prompts/既定.txt")
        self.assertIn("消えたキャラ.txt", str(logging.call_args_list))
        self.assertEqual(settings.current_model_name, choices.MODEL_CHOICES[0].value)

        install_persona_catalog(self, "../prompts/既定.txt", "../prompts/消えたキャラ.txt", default="../prompts/既定.txt")
        self.assertEqual(settings.snapshot_response().prompt_file, "../prompts/消えたキャラ.txt")

    def test_prompt_file_outside_prompts_folder_is_rejected(self) -> None:
        # 保存値から prompts/ の外（.env など）やテンプレートを読ませない
        for value in ("../.env", "../prompts/../.env", "../prompts/sub/架空.txt", "../prompts/..\\.env",
                      "../prompts/00_prompt_format.txt", "../prompts/.hidden.txt", "../prompts/架空.md"):
            with self.subTest(value=value), patch("builtins.print"):
                settings = GuildSettings(current_prompt_file="previous")
                settings.apply_persisted({"prompt_file": value})
                self.assertEqual(settings.current_prompt_file, personas.FOLLOW_DEFAULT_PERSONA)

    def test_unselected_persona_follows_catalog_default(self) -> None:
        settings = GuildSettings()
        self.assertEqual(settings.current_prompt_file, personas.FOLLOW_DEFAULT_PERSONA)
        install_persona_catalog(self)
        self.assertEqual(settings.snapshot_response().prompt_file, personas.FOLLOW_DEFAULT_PERSONA)
        install_persona_catalog(self, "../prompts/架空A.txt", "../prompts/架空B.txt", default="../prompts/架空B.txt")
        self.assertEqual(settings.snapshot_response().prompt_file, "../prompts/架空B.txt")

    def test_former_default_model_moves_to_current_default(self) -> None:
        # 既定を Gemini 3.5 Flash Lite へ変える前の既定を保存したサーバーも、次の起動で新しい既定へ移る。
        self.assertIn(constants.DEFAULT_MODEL_NAME, [choice.value for choice in choices.MODEL_CHOICES])
        settings = GuildSettings(current_model_name="previous")
        with patch("builtins.print"):
            settings.apply_persisted({"model_name": "gemini-3.1-flash-lite"})
        self.assertEqual(settings.current_model_name, "gemini-3.5-flash-lite")
        self.assertEqual(settings.current_model_name, constants.DEFAULT_MODEL_NAME)


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
