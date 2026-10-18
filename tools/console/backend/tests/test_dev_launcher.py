"""開発画面の寿命と、起動失敗時のプロセス回収を確認する。"""

from contextlib import ExitStack
import ctypes
from ctypes import wintypes
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch


LAUNCHER_PATH = Path(__file__).resolve().parents[2] / "dev_launcher.py"
spec = importlib.util.spec_from_file_location("console_dev_launcher", LAUNCHER_PATH)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class ConsoleLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        temporary = self.resources.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(temporary) / "開発 console's folder"
        for relative in ("backend/.venv/Scripts/python.exe", "frontend/node_modules/vite/bin/vite.js"):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        (self.root / ".run").mkdir()
        self.processes = [Mock(pid=100 + index) for index in range(3)]
        for process in self.processes:
            process.poll.return_value = None
        self.spawn = self.resources.enter_context(patch.object(launcher.subprocess, "Popen", side_effect=self.processes))
        self.resources.enter_context(patch.object(launcher.shutil, "which", return_value="node.exe"))
        self.resources.enter_context(patch.object(launcher, "find_browser", return_value=Path("browser.exe")))
        self.ports = self.resources.enter_context(patch.object(launcher, "check_port_available"))
        self.ready = self.resources.enter_context(patch.object(launcher, "wait_until_ready"))
        self.find_window = self.resources.enter_context(patch.object(launcher, "find_browser_window", return_value=42))
        self.window = self.resources.enter_context(patch.object(launcher, "browser_window_is_open", side_effect=[True, False]))
        self.resources.enter_context(patch.object(launcher.time, "sleep"))
        self.notify = Mock()

    def assert_stopped(self, count=3):
        for process in self.processes[:count]:
            process.terminate.assert_called_once()
            process.wait.assert_called()

    def test_closing_window_stops_services_even_if_browser_is_in_background(self):
        launcher.run_console(self.root, self.notify)
        self.notify.assert_called_once()
        self.assert_stopped()
        # 引用符や空白を含むパスも、シェルのコードとして解釈させない。
        commands = self.spawn.call_args_list
        self.assertEqual(commands[0].args[0][0], str(self.root / "backend/.venv/Scripts/python.exe"))
        self.assertEqual(commands[1].args[0][1], str(self.root / "frontend/node_modules/vite/bin/vite.js"))
        self.assertIn(f"--user-data-dir={self.root / '.run/browser-profile'}", commands[2].args[0])
        if os.name == "nt":
            for command in commands[:2]:
                self.assertEqual(command.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)

    def test_window_stays_open_across_refreshes(self):
        checks = iter([True, True, True, False])

        def window_is_open(_process_id):
            for process in self.processes:
                process.terminate.assert_not_called()
            return next(checks)

        self.window.side_effect = window_is_open
        launcher.run_console(self.root, self.notify)
        self.assert_stopped()

    def test_second_service_launch_failure_cleans_up_first_service(self):
        self.spawn.side_effect = [self.processes[0], OSError("起動失敗")]
        with self.assertRaisesRegex(OSError, "起動失敗"):
            launcher.run_console(self.root, self.notify)
        self.assert_stopped(1)
        self.notify.assert_not_called()

    def test_readiness_failure_cleans_up_both_services_without_opening_browser(self):
        self.ready.side_effect = RuntimeError("起動確認失敗")
        with self.assertRaisesRegex(RuntimeError, "起動確認失敗"):
            launcher.run_console(self.root, self.notify)
        self.assertEqual(self.spawn.call_count, 2)
        self.assert_stopped(2)
        self.notify.assert_not_called()

    def test_browser_launch_failure_cleans_up_services(self):
        self.spawn.side_effect = [*self.processes[:2], OSError("画面起動失敗")]
        with self.assertRaisesRegex(OSError, "画面起動失敗"):
            launcher.run_console(self.root, self.notify)
        self.assert_stopped(2)

    def test_browser_without_window_times_out_and_cleans_up_everything(self):
        self.find_window.return_value = None
        with patch.object(launcher, "WINDOW_TIMEOUT", 0):
            with self.assertRaisesRegex(RuntimeError, "画面を開けません"):
                launcher.run_console(self.root, self.notify)
        self.assert_stopped()
        self.notify.assert_not_called()

    def test_service_crash_after_start_closes_browser_and_other_service(self):
        self.window.side_effect = [True, True]
        with patch.object(launcher, "check_services", side_effect=RuntimeError("バックエンドが終了")):
            with self.assertRaisesRegex(RuntimeError, "バックエンドが終了"):
                launcher.run_console(self.root, self.notify)
        self.notify.assert_called_once()
        self.assert_stopped()

    def test_occupied_port_does_not_launch_or_stop_any_process(self):
        self.ports.side_effect = RuntimeError("ポートは使用中")
        with self.assertRaisesRegex(RuntimeError, "使用中"):
            launcher.run_console(self.root, self.notify)
        self.spawn.assert_not_called()
        for process in self.processes:
            process.terminate.assert_not_called()

    def test_missing_setup_does_not_launch_background_processes(self):
        with patch.object(launcher.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "初回セットアップ"):
                launcher.run_console(self.root, self.notify)
        self.spawn.assert_not_called()


class LauncherBoundaryTests(unittest.TestCase):
    def test_port_probe_preserves_existing_listener(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            with self.assertRaisesRegex(RuntimeError, "使用中"):
                launcher.check_port_available(listener.getsockname()[1])
            self.assertGreater(listener.fileno(), -1)

    def test_stopping_exited_process_does_not_signal_another_process(self):
        process = Mock()
        process.poll.return_value = 0
        launcher.stop_process(process)
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows の Job Object を確認する")
    def test_terminating_supervisor_also_terminates_descendants(self):
        # 実際のボットや SSH・ブラウザを使わず、監視役の強制終了を再現する。
        with tempfile.TemporaryDirectory() as temporary:
            status = Path(temporary) / "children.json"
            child_code = (
                "import json, pathlib, subprocess, sys, time; "
                "grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                f"record = pathlib.Path({str(status)!r}); "
                "record.with_suffix('.tmp').write_text(json.dumps([__import__('os').getpid(), grandchild.pid])); "
                "record.with_suffix('.tmp').replace(record); "
                "time.sleep(60)"
            )
            code = (
                "import json, pathlib, subprocess, sys, time; "
                f"sys.path.insert(0, {str(LAUNCHER_PATH.parent)!r}); "
                "import dev_launcher; dev_launcher.attach_lifetime_job(); "
                f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
                "time.sleep(60)"
            )
            supervisor = subprocess.Popen([sys.executable, "-B", "-c", code],
                                          creationflags=subprocess.CREATE_NO_WINDOW)
            self.addCleanup(lambda: launcher.stop_process(supervisor))
            deadline = time.monotonic() + 10
            while not status.exists() and supervisor.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(status.exists(), "検証用の子プロセスを起動できない")
            process_ids = json.loads(status.read_text())
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handles = [kernel.OpenProcess(0x100000, False, process_id) for process_id in process_ids]
            self.assertTrue(all(handles))
            try:
                supervisor.kill()
                supervisor.wait(timeout=5)
                for handle in handles:
                    self.assertEqual(kernel.WaitForSingleObject(handle, 5000), 0,
                                     "監視役が強制終了した後も子・孫プロセスが残っている")
            finally:
                for handle in handles:
                    if handle:
                        kernel.CloseHandle(handle)


if __name__ == "__main__":
    unittest.main()
