"""データを data/ にまとめても、ペルソナの解決と起動時の配線を使えることを確認する。"""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC))

from persona_bot.config import constants
from persona_bot.config.paths import resolve_prompt_path
from persona_bot.config.personas import persona_value


class PackageLayoutTests(unittest.TestCase):
    def test_runtime_paths_use_data_without_changing_persona_paths(self) -> None:
        self.assertEqual(constants.BASE_DIR, SRC)
        self.assertEqual(constants.PROJECT_ROOT, PROJECT_ROOT)
        locations = {
            "ENV_FILE_PATH": ".env",
            "DATA_DIR": "data",
            "MEMORY_DIR": "data/memory",
            "DATABASE_PATH": "data/memory/bot.db",
            "PROFILE_MEMORY_PATH": "data/memory/profile.json",
            "PROJECTS_MEMORY_PATH": "data/memory/projects.json",
            "NG_DIR": "data/ng",
            "NG_WORD_PATH": "data/ng/NG_WORD.txt",
            "NG_WORD_PRIVATE_PATH": "data/ng/NG_WORD_private.txt",
            "BLACKLIST_PATH": "data/ng/blacklist.json",
            "PROMPTS_DIR": "prompts",
            "PERSONA_SETTINGS_PATH": "prompts/personas.json",
        }
        # 実ファイルは読まず、保存先の計算だけを確認する。
        for name, relative in locations.items():
            with self.subTest(setting=name):
                self.assertEqual(getattr(constants, name), PROJECT_ROOT / relative)
        # 保存するペルソナの値（"../prompts/<ファイル名>"）は従来どおり prompts/ を指す。
        # キャラ別のファイルは Git 管理外なので、公開しているテンプレートだけ実在を確かめる。
        for name in (constants.PROMPT_TEMPLATE_FILE_NAME, "架空のペルソナ.txt"):
            with self.subTest(persona=name):
                self.assertEqual(resolve_prompt_path(persona_value(name)), PROJECT_ROOT / "prompts" / name)
        for template in (constants.PROMPT_TEMPLATE_FILE_NAME, constants.PERSONA_SETTINGS_TEMPLATE_FILE_NAME):
            with self.subTest(template=template):
                self.assertTrue((PROJECT_ROOT / "prompts" / template).is_file())
        self.assertTrue(constants.NG_WORD_PATH.is_file())

    def test_legacy_data_stops_before_clients_and_database_are_created(self) -> None:
        script = textwrap.dedent("""\
            from pathlib import Path
            import runpy
            from unittest.mock import patch

            with patch('persona_bot.config.settings_loader.load_settings') as settings, \\
                 patch('persona_bot.services.genai_client.create_genai_client') as factory:
                try:
                    runpy.run_path('bot.py', run_name='__main__')
                except RuntimeError as error:
                    assert 'migrate_data_directory.py' in str(error)
                else:
                    raise AssertionError('旧データを残したまま起動しました')
                settings.assert_not_called()
                factory.assert_not_called()
                assert not Path('../data').exists()
                assert Path('../memory/bot.db').read_bytes() == b'fictional'
            """)
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["MPLBACKEND"] = "Agg"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "src"
            shutil.copytree(SRC, source, ignore=shutil.ignore_patterns("__pycache__"))
            (root / "memory").mkdir()
            (root / "memory/bot.db").write_bytes(b"fictional")
            result = subprocess.run(
                [sys.executable, "-B", "-c", script], cwd=source, env=environment,
                capture_output=True, text=True, encoding="utf-8", timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_legacy_entrypoint_starts_in_an_isolated_project(self) -> None:
        # ソースだけをコピーし、実際の設定・DB・会話・記憶には触れずに起動する。
        script = textwrap.dedent("""\
            from pathlib import Path
            import runpy
            import sys
            from unittest.mock import Mock, patch

            sys.path.insert(0, str(Path.cwd()))
            with patch('socket.socket.connect', side_effect=AssertionError('外部通信は禁止')) as connect, \\
                 patch('socket.getaddrinfo', side_effect=AssertionError('外部DNSは禁止')) as dns, \\
                 patch('persona_bot.services.genai_client.create_genai_client', return_value=Mock()) as factory, \\
                 patch('persona_bot.core.bot_client.PersonaClient.run') as run:
                namespace = runpy.run_path('bot.py', run_name='__main__')
                try:
                    factory.assert_called_once_with('FICTIONAL_GEMINI_KEY')
                    run.assert_called_once_with('FICTIONAL_DISCORD_TOKEN')
                    assert {command.name for command in namespace['tree'].get_commands()} == {'functions', 'join', 'leave'}
                    assert Path('../data/memory/bot.db').is_file()
                    assert not Path('../memory').exists()
                    connect.assert_not_called()
                    dns.assert_not_called()
                finally:
                    namespace['database'].close()
            """)
        environment = os.environ.copy()
        for key in ("API_KEY_GEMINI", "TOKEN_DISCORD", "PYTHONPATH"):
            environment.pop(key, None)
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["MPLBACKEND"] = "Agg"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "src"
            shutil.copytree(SRC, source, ignore=shutil.ignore_patterns("__pycache__"))
            (root / ".env").write_text(
                "API_KEY_GEMINI=FICTIONAL_GEMINI_KEY\nTOKEN_DISCORD=FICTIONAL_DISCORD_TOKEN\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, "-B", "-c", script], cwd=source, env=environment,
                capture_output=True, text=True, encoding="utf-8", timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
