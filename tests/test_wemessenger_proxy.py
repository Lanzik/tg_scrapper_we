import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wm_client import build_wemessenger


class WemessengerProxyTests(unittest.TestCase):
    def test_build_uses_proxy_for_publish_when_enabled(self):
        settings = {
            "telegram_proxy": "socks5://127.0.0.1:10808",
            "publish_use_proxy": True,
            "wemessenger": {
                "enabled": True,
                "bot_token": "token",
                "channel_node": "node",
                "api_base": "https://api.wemessenger.ir",
                "file_upload_url": "https://ms-file.wemessenger.ir/upload",
                "to_category": "CHANNEL",
                "session_id": "*",
            },
        }

        client = build_wemessenger(settings)

        self.assertEqual(client.proxy, "socks5://127.0.0.1:10808")

    def test_build_disables_proxy_for_publish_when_flag_is_off(self):
        settings = {
            "telegram_proxy": "socks5://127.0.0.1:10808",
            "publish_use_proxy": False,
            "wemessenger": {
                "enabled": True,
                "bot_token": "token",
                "channel_node": "node",
                "api_base": "https://api.wemessenger.ir",
                "file_upload_url": "https://ms-file.wemessenger.ir/upload",
                "to_category": "CHANNEL",
                "session_id": "*",
            },
        }

        client = build_wemessenger(settings)

        self.assertIsNone(client.proxy)


if __name__ == "__main__":
    unittest.main()
