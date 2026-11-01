import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scraper import _collect_posts
from wm_client import build_wemessenger


class ScraperGuardTests(unittest.TestCase):
    def test_collect_posts_rejects_challenge_page_html(self):
        class FakeResponse:
            status_code = 200
            text = """
            <html>
              <head><title>Checking your browser</title></head>
              <body><p>Please wait a few seconds while we verify you are not a bot.</p></body>
            </html>
            """

            def raise_for_status(self):
                return None

        class FakeClient:
            def get(self, _url):
                return FakeResponse()

        with self.assertRaisesRegex(RuntimeError, "no channel content|challenge|bot"):
            _collect_posts(FakeClient(), "Khabar_Fouri", limit=10)


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
