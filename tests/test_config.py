import tempfile
import unittest
from pathlib import Path

from app.config.config import Settings


class SettingsTests(unittest.TestCase):
    def test_settings_loads_runtime_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "ENV=prod\nPORT=8082\nDEBUG=release\n"
                "BOT_TOKEN=token\nCHAT_ID=123\n",
                encoding="utf-8",
            )

            settings = Settings(_env_file=env_file)

        self.assertEqual(settings.env, "prod")
        self.assertEqual(settings.port, 8082)
        self.assertFalse(settings.debug)
        self.assertIsNone(settings.docs_url)
        self.assertEqual(settings.bot_token, "token")
        self.assertEqual(settings.chat_id, "123")
