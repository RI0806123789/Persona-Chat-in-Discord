"""実際の.envを使わず、BOM除去とOS環境変数の優先を検証する。"""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.config import settings_loader


class EnvEncodingTests(unittest.TestCase):
    @unittest.skipIf(settings_loader.load_dotenv is None, "python-dotenvが未導入")
    def test_bom_first_key_loads_and_existing_environment_stays_preferred(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.env"
            path.write_text("API_KEY_GEMINI=fictional-gemini\nTOKEN_DISCORD=fictional-discord\n", encoding="utf-8-sig")
            with patch.object(settings_loader, "ENV_FILE_PATH", path), patch.dict(os.environ, {}, clear=True):
                loaded = settings_loader.load_settings()
                self.assertEqual(loaded.API_KEY_GEMINI, "fictional-gemini")
                self.assertEqual(loaded.TOKEN_DISCORD, "fictional-discord")
                self.assertNotIn("\ufeffAPI_KEY_GEMINI", os.environ)
            with patch.object(settings_loader, "ENV_FILE_PATH", path), patch.dict(
                os.environ, {"API_KEY_GEMINI": "fictional-environment"}, clear=True,
            ):
                self.assertEqual(settings_loader.load_settings().API_KEY_GEMINI, "fictional-environment")


if __name__ == "__main__":
    unittest.main()
