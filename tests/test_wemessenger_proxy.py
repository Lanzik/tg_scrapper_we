import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scraper import _collect_posts
from config import load_settings
from db import Database
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


class ChannelRouteTests(unittest.TestCase):
    def test_load_settings_supports_channel_specific_routes_and_defaults(self):
        config_text = yaml.safe_dump(
            {
                "channels": [
                    {
                        "telegram": "@First",
                        "wemessenger_node": "node-1",
                        "poll_interval_seconds": 45,
                        "publish_interval_seconds": 25,
                        "send_delay_seconds": 3,
                        "retention_days": 14,
                    },
                    {"telegram": "@second", "wemessenger_node": "node-2"},
                ],
                "poll_interval": 10,
                "publish_interval": 15,
                "send_delay_seconds": 20,
                "retention_days": 60,
                "wemessenger": {"enabled": True, "bot_token": "token"},
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_path = Path(temp_dir) / "settings.yaml"
            settings_path.write_text(config_text, encoding="utf-8")
            settings = load_settings(settings_path)

        first, second = settings["channel_routes"]
        self.assertEqual(first["username"], "First")
        self.assertEqual(first["wemessenger_node"], "node-1")
        self.assertEqual(first["poll_interval_seconds"], 45)
        self.assertEqual(first["publish_interval_seconds"], 25)
        self.assertEqual(first["send_delay_seconds"], 3)
        self.assertEqual(first["retention_days"], 14)
        self.assertEqual(second["wemessenger_node"], "node-2")
        self.assertEqual(second["retention_days"], 60)

    def test_legacy_queue_is_routed_and_expired_posts_cascade(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with Database(Path(temp_dir) / "test.db") as db:
                channel_id = db.upsert_channel(
                    "first", "First", "https://t.me/s/first", "2020-01-01T00:00:00+00:00"
                )
                post_id = db.upsert_post(
                    channel_id,
                    message_id=1,
                    date="2020-01-01T00:00:00+00:00",
                    text="old post",
                    views=None,
                    source="https://t.me/first/1",
                    scraped_at=(datetime.now(timezone.utc) - timedelta(days=40)).isoformat(),
                )
                db.replace_photos(
                    post_id,
                    [{"position": 1, "url": None, "file_path": None, "image": b"image"}],
                )
                db.enqueue_for_publish(post_id)
                db.route_legacy_queue({"first": "node-1"})

                self.assertEqual(
                    db.get_pending_delivery_post_ids("node-1", "first"), [post_id]
                )
                self.assertEqual(
                    db.get_pending_delivery_post_ids("node-1", "second"), []
                )
                self.assertEqual(db.delete_channel_posts_older_than("first", 30), 1)
                self.assertIsNone(db.get_post(post_id))
                self.assertEqual(db.count_queue("node-1")["unpublished"], 0)


if __name__ == "__main__":
    unittest.main()
