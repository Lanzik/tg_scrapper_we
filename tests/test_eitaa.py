import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from config import load_settings
from db import Database
from main import _publish_route, _scrape_route
from publisher import publish_posts_to_wemessenger
from scraper import _collect_posts, _download_bytes, _parse_page, normalize_eitaa_username, scrape_channel


class EitaaTests(unittest.TestCase):
    def test_routes_allow_same_username_on_both_platforms(self):
        data = {
            "channels": [
                {"telegram": "@andishetubaee", "wemessenger_node": "node-a"},
                {"eitaa": "https://eitaa.com/andishetubaee", "wemessenger_node": "node-b"},
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "settings.yaml"
            path.write_text(yaml.safe_dump(data), encoding="utf-8")
            routes = load_settings(path)["channel_routes"]
        self.assertEqual([r["username"] for r in routes], ["andishetubaee", "eitaa:andishetubaee"])
        self.assertEqual(routes[1]["platform"], "eitaa")

    def test_eitaa_url_validation(self):
        self.assertEqual(normalize_eitaa_username("https://eitaa.com/andishetubaee"), "andishetubaee")
        for value in ("https://example.com/channel", "https://eitaa.com/channel/123", "../channel"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_eitaa_username(value)

    def test_parse_text_photo_video_and_unavailable_media(self):
        html = """
        <div class="etme_channel_info_header_title">Test</div>
        <div class="etme_widget_message" data-post="test/1">
          <a class="etme_widget_message_reply">
            <div class="etme_widget_message_text js-message_reply_text">old…</div>
          </a>
          <a class="etme_widget_message_photo_wrap" style="background-image:url('/download_photo?token=x')"></a>
          <div class="etme_widget_message_text js-message_text">caption<br>line two</div>
          <time datetime="2026-01-01"></time>
        </div>
        <div class="etme_widget_message" data-post="test/2">
          <a class="etme_widget_message_video_player"><video src="/download_video?token=x"></video></a>
          <div class="etme_widget_message_text js-message_text">video</div>
        </div>
        <div class="etme_widget_message" data-post="test/3">
          <a class="etme_widget_message_video_player not_supported" href="/s/test/3"></a>
          <div class="etme_widget_message_text js-message_text">unavailable video</div>
        </div>
        <div class="etme_widget_message" data-post="test/4">
          <a class="etme_widget_message_document_wrap"></a>
          <div class="etme_widget_message_text js-message_text">document</div>
        </div>
        """
        title, posts = _parse_page(html, platform="eitaa")
        self.assertEqual(title, "Test")
        self.assertEqual(posts[0]["text"], "caption\nline two")
        self.assertEqual(posts[0]["media"], [{"type": "image", "url": "https://eitaa.com/download_photo?token=x"}])
        self.assertEqual(posts[1]["media"], [{"type": "video", "url": "https://eitaa.com/download_video?token=x"}])
        self.assertEqual(posts[2]["media"], [{"type": "video", "url": ""}])
        self.assertEqual(posts[3]["media"], [{"type": "file", "url": ""}])

    def test_pagination_fetches_before_and_stops_at_known_post(self):
        def page(ids):
            return '<div class="etme_channel_info_header_title">Test</div>' + "".join(
                f'<div class="etme_widget_message" data-post="test/{i}">'
                f'<div class="etme_widget_message_text">text {i}</div></div>' for i in ids
            )
        client = MagicMock()
        client.get.side_effect = [
            httpx.Response(200, text=page([3, 4]), request=httpx.Request("GET", "https://eitaa.com/test")),
            httpx.Response(200, text=page([1, 2]), request=httpx.Request("GET", "https://eitaa.com/test?before=3")),
        ]
        _, posts = _collect_posts(client, "test", limit=None, after_id=2, platform="eitaa")
        self.assertEqual([p["id"] for p in posts], [4, 3])
        self.assertEqual(client.get.call_args_list[1].args[0], "https://eitaa.com/test?before=3")

    def test_partial_media_is_never_treated_as_complete(self):
        response = httpx.Response(
            206, content=b"partial", headers={"content-range": "bytes 0-6/100"},
            request=httpx.Request("GET", "https://eitaa.com/video"),
        )
        client = MagicMock()
        client.get.return_value = response
        with self.assertRaisesRegex(RuntimeError, "incomplete content"):
            _download_bytes(client, "https://eitaa.com/video", media_type="video")
        self.assertEqual(client.get.call_count, 3)

    def test_missing_eitaa_media_is_not_published_as_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "posts.db"
            with Database(path) as db:
                channel = db.upsert_channel("eitaa:test", None, "https://eitaa.com/test", "2020")
                post = db.upsert_post(channel, message_id=1, date=None, text="caption", views=None,
                                      source="https://eitaa.com/test/1", scraped_at="2020")
                db.replace_photos(post, [{"position": 1, "media_type": "video", "url": ""}])
                db.enqueue_for_publish(post, target="node")
            sender = MagicMock()
            self.assertEqual(publish_posts_to_wemessenger(path, [post], sender, target="node"), 0)
            sender.send_post.assert_not_called()
            with Database(path) as db:
                self.assertEqual(db.count_queue("node")["failed"], 1)

    def test_scrape_and_publish_eitaa_media_with_proxy(self):
        post = {"id": 1, "date": None, "text": "caption", "views": None,
                "media": [{"type": "video", "url": "https://eitaa.com/download_video"}]}
        http = MagicMock()
        http.__enter__.return_value = http
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "posts.db"
            with patch("scraper.DATA_DIR", root), patch("scraper._collect_posts", return_value=("Test", [post])), \
                 patch("scraper.httpx.Client", return_value=http) as factory:
                ids = scrape_channel("https://eitaa.com/test", database=path, platform="eitaa",
                                     download_photos=False, target="node", proxy="socks5://127.0.0.1:1080")
                self.assertFalse(factory.call_args.kwargs["trust_env"])
                self.assertEqual(factory.call_args.kwargs["proxy"], "socks5://127.0.0.1:1080")
            with Database(path) as db:
                self.assertEqual(db.get_max_message_id("eitaa:test"), 1)
                self.assertEqual(db.get_post(ids[0])["source"], "https://eitaa.com/test/1")
            sender = MagicMock()
            with patch("publisher.httpx.Client", return_value=http) as factory, \
                 patch("publisher._download_bytes", return_value=(b"video", ".mp4")):
                self.assertEqual(publish_posts_to_wemessenger(
                    path, ids, sender, target="node", telegram_proxy="socks5://127.0.0.1:1080"
                ), 1)
                self.assertEqual(factory.call_args.kwargs["proxy"], "socks5://127.0.0.1:1080")
            sender.send_post.assert_called_once_with(text="caption", media=[(b"video", "media_1.mp4", "video")])

    def test_eitaa_media_retry_without_proxy_stays_direct(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "posts.db"
            with Database(path) as db:
                channel = db.upsert_channel("eitaa:test", None, "https://eitaa.com/test", "2020")
                post = db.upsert_post(channel, message_id=1, date=None, text="caption", views=None,
                                      source="https://eitaa.com/test/1", scraped_at="2020")
                db.replace_photos(post, [{"position": 1, "media_type": "image",
                                          "url": "https://eitaa.com/image"}])
                db.enqueue_for_publish(post, target="node")
            http = MagicMock()
            http.__enter__.return_value = http
            with patch("publisher.httpx.Client", return_value=http) as factory, \
                 patch("publisher._download_bytes", return_value=(b"image", ".jpg")):
                self.assertEqual(publish_posts_to_wemessenger(path, [post], MagicMock(), target="node"), 1)
            self.assertIsNone(factory.call_args.kwargs["proxy"])
            self.assertFalse(factory.call_args.kwargs["trust_env"])

    def test_eitaa_route_obeys_scraper_proxy_setting(self):
        settings = {"telegram_proxy": "socks5://proxy:1080", "scraper_use_proxy": True,
                    "limit": 1, "download_media": True, "database": Path("test.db"),
                    "max_media_per_post": 1, "deduplicate_posts": True}
        route = {"telegram": "https://eitaa.com/test", "platform": "eitaa", "exclude_post_terms": []}
        for enabled in (True, False):
            with self.subTest(enabled=enabled), patch("main.scrape_channel", return_value=[]) as scrape:
                settings["scraper_use_proxy"] = enabled
                _scrape_route(settings, route, initial=True, target="node")
                self.assertEqual(scrape.call_args.kwargs["proxy"], "socks5://proxy:1080" if enabled else None)
                self.assertEqual(scrape.call_args.kwargs["platform"], "eitaa")

    def test_eitaa_publish_route_uses_scraper_proxy_independently_of_we_proxy(self):
        settings = {
            "telegram_proxy": "socks5://proxy:1080", "scraper_use_proxy": True,
            "publish_use_proxy": False, "wemessenger": {"api_base": "https://we.test"},
            "database": Path("test.db"), "delete_media_after_publish": True,
        }
        route = {
            "telegram": "https://eitaa.com/test", "username": "eitaa:test", "platform": "eitaa",
            "send_delay_seconds": 0, "footer_tag": "", "remove_trailing_phrases": [],
            "remove_trailing_urls": False, "exclude_post_terms": [],
        }
        with patch("main.wemessenger_reachable", return_value=True) as reachable, \
             patch("main.publish_pending_to_wemessenger", return_value=(0, 0)) as publish:
            _publish_route(settings, route, "node", MagicMock())
            self.assertIsNone(reachable.call_args.kwargs["proxy"])
            self.assertEqual(publish.call_args.kwargs["telegram_proxy"], "socks5://proxy:1080")
            settings["scraper_use_proxy"] = False
            _publish_route(settings, route, "node", MagicMock())
            self.assertIsNone(publish.call_args.kwargs["telegram_proxy"])


if __name__ == "__main__":
    unittest.main()
