import sys
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from unittest.mock import MagicMock
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scraper import _collect_posts, _parse_page, scrape_channel
from config import load_settings
from db import Database
from main import _publisher_worker, _scraper_worker
from publisher import publish_posts_to_wemessenger
from wemessenger import WeMessengerClient
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

    def test_parser_limits_mixed_media_and_keeps_video_type(self):
        html = """
        <div class="tgme_channel_info_header_title">Test</div>
        <div class="tgme_widget_message" data-post="test/1">
                    <div class="tgme_widget_message_video_player"
                             data-video="https://cdn.test/clip.mp4"></div>
          <a class="tgme_widget_message_photo_wrap"
             style="background-image:url(https://cdn.test/photo.jpg)"></a>
        </div>
        """
        _, posts = _parse_page(html, max_media_per_post=1)
        self.assertEqual(
            posts[0]["media"],
            [{"type": "video", "url": "https://cdn.test/clip.mp4"}],
        )

        _, posts = _parse_page(html, max_media_per_post=2)
        self.assertEqual([item["type"] for item in posts[0]["media"]], ["video", "image"])

    def test_scraper_skips_duplicate_before_saving_or_queueing(self):
        posts = [
            {"id": 1, "date": None, "text": "Same post text", "views": None, "media": []},
            {"id": 2, "date": None, "text": "  SAME   post text ", "views": None, "media": []},
        ]
        fake_http_client = MagicMock()
        fake_http_client.__enter__.return_value = fake_http_client
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database_path = root / "posts.db"
            with patch("scraper.DATA_DIR", root), patch(
                "scraper._collect_posts", return_value=("Source", posts)
            ), patch("scraper.httpx.Client", return_value=fake_http_client):
                saved = scrape_channel(
                    "@source",
                    database=database_path,
                    only_new=False,
                )

            self.assertEqual(len(saved), 1)
            with Database(database_path) as db:
                self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 1)
                self.assertEqual(db.count_queue()["pending"], 1)


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

    def test_send_video_uploads_as_video_and_keeps_caption(self):
        client = WeMessengerClient("token", "node")
        client.upload_file = lambda _client, **kwargs: {
            "uuid": "media-id",
            "size": 12,
            "type": kwargs["file_type"],
        }
        sent = {}
        client._send = lambda _client, payload: sent.update(payload) or {}

        client.send_video(
            object(),
            content=b"video-bytes",
            filename="clip.mp4",
            caption="video caption",
        )

        self.assertEqual(sent["file"]["type"], "video")
        self.assertEqual(sent["file"]["caption"], "video caption")


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
                "max_media_per_post": 2,
                "download_media": False,
                "deduplicate_posts": True,
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
        self.assertEqual(settings["max_media_per_post"], 2)
        self.assertFalse(settings["download_media"])
        self.assertTrue(settings["deduplicate_posts"])

    def test_existing_photos_table_migrates_media_type(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "legacy.db"
            connection = sqlite3.connect(database_path)
            connection.execute(
                """CREATE TABLE photos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_id INTEGER NOT NULL,
                    position INTEGER NOT NULL DEFAULT 1,
                    url TEXT,
                    file_path TEXT,
                    image BLOB,
                    UNIQUE (post_id, position)
                )"""
            )
            connection.close()

            with Database(database_path) as db:
                columns = {
                    row["name"]
                    for row in db.conn.execute("PRAGMA table_info(photos)").fetchall()
                }
                self.assertIn("media_type", columns)

    def test_duplicate_fingerprint_normalizes_text_and_checks_other_channels(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with Database(Path(temp_dir) / "dedupe.db") as db:
                channel_id = db.upsert_channel(
                    "first", "First", "https://t.me/s/first", "2020-01-01T00:00:00+00:00"
                )
                db.upsert_post(
                    channel_id,
                    message_id=1,
                    date=None,
                    text="BREAKING   story\nnow",
                    views=None,
                    source="https://t.me/first/1",
                    scraped_at="2020-01-01T00:00:00+00:00",
                )

                self.assertTrue(db.has_duplicate_post("breaking story now"))
                self.assertFalse(db.has_duplicate_post("a different post"))

    def test_existing_posts_get_fingerprints_backfilled_on_open(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "backfill.db"
            with Database(database_path) as db:
                channel_id = db.upsert_channel(
                    "first", "First", "https://t.me/s/first", "2020-01-01T00:00:00+00:00"
                )
                post_id = db.upsert_post(
                    channel_id,
                    message_id=1,
                    date=None,
                    text="previous post",
                    views=None,
                    source="https://t.me/first/1",
                    scraped_at="2020-01-01T00:00:00+00:00",
                )
                db.conn.execute("UPDATE posts SET content_hash = NULL WHERE id = ?", (post_id,))
                db.conn.commit()

            with Database(database_path) as db:
                self.assertTrue(db.has_duplicate_post("PREVIOUS post"))

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
                    [{
                        "position": 1,
                        "url": None,
                        "file_path": "data/media/first/1/video.mp4",
                        "media_type": "video",
                        "image": None,
                    }],
                )
                self.assertEqual(db.get_photos(post_id)[0]["media_type"], "video")
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


class DuplicatePublishTests(unittest.TestCase):
    def test_existing_duplicate_queue_publishes_only_first_copy(self):
        class FakePublisher:
            def __init__(self):
                self.posts = []

            def send_post(self, **kwargs):
                self.posts.append(kwargs)

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "duplicate-queue.db"
            with Database(database_path) as db:
                first_channel = db.upsert_channel(
                    "first", "First", "https://t.me/s/first", "2020-01-01T00:00:00+00:00"
                )
                second_channel = db.upsert_channel(
                    "second", "Second", "https://t.me/s/second", "2020-01-01T00:00:00+00:00"
                )
                post_ids = []
                for channel_id, message_id in ((first_channel, 1), (second_channel, 2)):
                    post_ids.append(
                        db.upsert_post(
                            channel_id,
                            message_id=message_id,
                            date=None,
                            text="same announcement",
                            views=None,
                            source=f"https://t.me/source/{message_id}",
                            scraped_at="2020-01-01T00:00:00+00:00",
                        )
                    )
                    db.enqueue_for_publish(post_ids[-1], target="node")

            publisher = FakePublisher()
            sent = publish_posts_to_wemessenger(
                database_path,
                post_ids,
                publisher,
                send_delay_seconds=0,
                target="node",
            )

            self.assertEqual(sent, 1)
            self.assertEqual(len(publisher.posts), 1)
            with Database(database_path) as db:
                self.assertEqual(db.count_queue("node")["duplicate"], 1)
                self.assertEqual(db.get_pending_delivery_post_ids("node"), [])


class WorkerConcurrencyTests(unittest.TestCase):
    def test_publisher_runs_while_scraper_is_blocked(self):
        route = {
            "username": "source",
            "telegram": "@source",
            "poll_interval_seconds": 60,
            "publish_interval_seconds": 60,
        }
        settings = {"channel_routes": [route]}
        targets = {"source": "node"}
        stop_event = threading.Event()
        scrape_started = threading.Event()
        release_scrape = threading.Event()
        publish_called = threading.Event()

        def block_scrape(*_args, **_kwargs):
            scrape_started.set()
            release_scrape.wait(timeout=3)

        def finish_publish(*_args, **_kwargs):
            publish_called.set()
            stop_event.set()

        scraper = threading.Thread(target=_scraper_worker, args=(settings, targets, stop_event))
        publisher = threading.Thread(
            target=_publisher_worker,
            args=(settings, targets, {"source": object()}, stop_event),
        )
        try:
            with patch("main._scrape_route", side_effect=block_scrape), patch(
                "main._publish_route", side_effect=finish_publish
            ):
                scraper.start()
                publisher.start()
                self.assertTrue(scrape_started.wait(timeout=1))
                self.assertTrue(publish_called.wait(timeout=1))
                self.assertTrue(scraper.is_alive())
        finally:
            release_scrape.set()
            stop_event.set()
            scraper.join(timeout=2)
            publisher.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
