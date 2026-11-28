import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from db import Database
from publisher import publish_posts_to_wemessenger
from scraper import _download_bytes, _parse_page, _save_post, scrape_channel
from wemessenger import TEXT_LIMIT, WeMessengerClient


class RequiredMediaTests(unittest.TestCase):
    def test_download_retries_three_times_and_reports_failure(self):
        client = MagicMock()
        client.get.side_effect = httpx.ConnectError("offline")
        with self.assertRaisesRegex(RuntimeError, "after 3 attempts"):
            _download_bytes(client, "https://example.test/photo.jpg")
        self.assertEqual(client.get.call_count, 3)

    def test_scrape_failure_keeps_media_and_queue_retries_without_text_fallback(self):
        post = {
            "id": 1, "date": None, "text": "caption", "views": None,
            "media": [{"type": "image", "url": "https://example.test/photo.jpg"}],
        }
        telegram = MagicMock()
        telegram.__enter__.return_value = telegram
        sender = MagicMock()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "posts.db"
            with patch("scraper.DATA_DIR", root), patch(
                "scraper._collect_posts", return_value=("Source", [post])
            ), patch("scraper.httpx.Client", return_value=telegram), patch(
                "scraper._download_bytes", side_effect=RuntimeError("download failed")
            ):
                ids = scrape_channel("source", database=path, target="node")
            with Database(path) as db:
                self.assertEqual(db.get_photos(ids[0])[0]["url"], post["media"][0]["url"])
            with patch("publisher._download_bytes", side_effect=RuntimeError("download failed")), patch(
                "publisher.httpx.Client", return_value=telegram
            ):
                self.assertEqual(publish_posts_to_wemessenger(path, ids, sender, target="node"), 0)
            sender.send_post.assert_not_called()
            with Database(path) as db:
                self.assertEqual(db.count_queue("node")["failed"], 1)
                self.assertEqual(len(db.get_photos(ids[0])), 1)
            with patch("publisher._download_bytes", return_value=(b"image", ".jpg")), patch(
                "publisher.httpx.Client", return_value=telegram
            ):
                self.assertEqual(publish_posts_to_wemessenger(path, ids, sender, target="node"), 1)
            sender.send_post.assert_called_once_with(
                text="caption", media=[(b"image", "media_1.jpg", "image")]
            )

    def test_missing_video_and_missing_url_never_send_caption(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "posts.db"
            with Database(path) as db:
                channel = db.upsert_channel("source", None, "", "2020")
                post = db.upsert_post(
                    channel, message_id=1, date=None, text="caption", views=None,
                    source="", scraped_at="2020"
                )
                db.replace_photos(post, [{"position": 1, "media_type": "video"}])
                db.enqueue_for_publish(post, target="node")
            sender = MagicMock()
            self.assertEqual(publish_posts_to_wemessenger(path, [post], sender, target="node"), 0)
            sender.send_post.assert_not_called()
            with Database(path) as db:
                self.assertEqual(db.count_queue("node")["failed"], 1)

    def test_media_metadata_and_queue_are_committed_together(self):
        post = {
            "id": 1, "date": None, "text": "caption", "views": None,
            "media": [{"type": "video", "url": "https://example.test/video.mp4"}],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "posts.db"
            with Database(path) as db, Database(path) as observer:
                channel = db.upsert_channel("source", None, "", "2020")
                enqueue = db.enqueue_for_publish

                def check_before_enqueue(post_id, **kwargs):
                    self.assertIsNone(observer.get_post(post_id))
                    return enqueue(post_id, **kwargs)

                with patch.object(db, "enqueue_for_publish", side_effect=check_before_enqueue):
                    post_id = _save_post(
                        db, MagicMock(), channel_id=channel, username="source", post=post,
                        media_dir=Path(temp_dir), download_photos=False,
                        max_media_per_post=1, scraped_at="2020", target="node"
                    )
                self.assertEqual(observer.get_photos(post_id)[0]["media_type"], "video")
                self.assertEqual(observer.get_pending_delivery_post_ids("node"), [post_id])

    def test_text_only_post_is_published_without_media(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "posts.db"
            with Database(path) as db:
                channel = db.upsert_channel("source", None, "", "2020")
                post = db.upsert_post(
                    channel, message_id=1, date=None, text="text only", views=None,
                    source="", scraped_at="2020"
                )
                db.enqueue_for_publish(post, target="node")
            sender = MagicMock()
            self.assertEqual(publish_posts_to_wemessenger(path, [post], sender, target="node"), 1)
            sender.send_post.assert_called_once_with(text="text only", media=[])

    def test_text_only_telegram_and_eitaa_posts_reach_wemessenger(self):
        for platform, prefix, source in (
            ("telegram", "tgme", "https://t.me/source/1"),
            ("eitaa", "etme", "https://eitaa.com/source/1"),
        ):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as temp_dir:
                html = (
                    f'<div class="{prefix}_widget_message" data-post="source/1">'
                    f'<div class="{prefix}_widget_message_text">متن پیام<br>خط دوم</div></div>'
                )
                _, posts = _parse_page(html, platform=platform)
                self.assertEqual(posts[0]["media"], [])
                path = Path(temp_dir) / "posts.db"
                with Database(path) as db:
                    channel = db.upsert_channel(platform, None, source, "2020")
                    post = db.upsert_post(
                        channel, message_id=1, date=None, text=posts[0]["text"],
                        views=None, source=source, scraped_at="2020",
                    )
                    db.enqueue_for_publish(post, target="node")
                client = WeMessengerClient("token", "node")
                client._send = MagicMock(return_value={})
                client.upload_file = MagicMock()
                http_client = MagicMock()
                http_client.__enter__.return_value = http_client
                with patch("wemessenger.httpx.Client", return_value=http_client):
                    self.assertEqual(
                        publish_posts_to_wemessenger(path, [post], client, target="node"), 1
                    )
                payload = client._send.call_args.args[1]
                self.assertEqual(payload["text"]["text"], "متن پیام\nخط دوم")
                self.assertNotIn("file", payload)
                client.upload_file.assert_not_called()
                with Database(path) as db:
                    self.assertEqual(db.count_queue("node")["sent"], 1)

    def test_unavailable_source_media_is_not_treated_as_text_only(self):
        html = (
            '<div class="tgme_widget_message" data-post="source/1">'
            '<a class="tgme_widget_message_photo_wrap"></a>'
            '<div class="tgme_widget_message_text">caption</div></div>'
        )
        _, posts = _parse_page(html)
        self.assertEqual(posts[0]["media"], [{"type": "image", "url": ""}])

    def test_all_uploads_complete_before_any_post_is_sent(self):
        client = WeMessengerClient("token", "node")
        http_client = MagicMock()
        http_client.__enter__.return_value = http_client
        sender = MagicMock()
        client._send = sender
        uploads = MagicMock(side_effect=[
            {"uuid": "first", "size": 1},
            RuntimeError("offline"), RuntimeError("offline"), RuntimeError("offline"),
        ])
        client.upload_file = uploads
        with patch("wemessenger.httpx.Client", return_value=http_client):
            with self.assertRaisesRegex(RuntimeError, "after 3 attempts"):
                client.send_post(
                    text="caption", media=[(b"image", "a.jpg", "image"), (b"video", "b.mp4", "video")]
                )
        self.assertEqual(uploads.call_count, 4)
        sender.assert_not_called()

    def test_upload_recovers_on_third_attempt_with_caption(self):
        client = WeMessengerClient("token", "node")
        http_client = MagicMock()
        http_client.__enter__.return_value = http_client
        client.upload_file = MagicMock(side_effect=[
            RuntimeError("offline"), RuntimeError("offline"), {"uuid": "x", "size": 1}
        ])
        client._send = MagicMock(return_value={})
        with patch("wemessenger.httpx.Client", return_value=http_client):
            client.send_post(text="caption", media=[(b"video", "a.mp4", "video")])
        self.assertEqual(client.upload_file.call_count, 3)
        self.assertEqual(client._send.call_args.args[1]["file"]["caption"], "caption")

    def test_file_send_failure_does_not_send_caption_separately(self):
        client = WeMessengerClient("token", "node")
        http_client = MagicMock()
        http_client.__enter__.return_value = http_client
        client.upload_file = MagicMock(return_value={"uuid": "id", "size": 1})
        client._send = MagicMock(side_effect=RuntimeError("send failed"))
        with patch("wemessenger.httpx.Client", return_value=http_client):
            with self.assertRaisesRegex(RuntimeError, "send failed"):
                client.send_post(text="a" * (TEXT_LIMIT + 10), media=[(b"file", "a.pdf", "file")])
        self.assertEqual(client._send.call_count, 1)
        self.assertIn("file", client._send.call_args.args[1])

    def test_file_send_failure_keeps_queue_failed_and_media(self):
        client = WeMessengerClient("token", "node")
        http_client = MagicMock()
        http_client.__enter__.return_value = http_client
        client.upload_file = MagicMock(return_value={"uuid": "id", "size": 1})
        client._send = MagicMock(side_effect=RuntimeError("send failed"))
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "posts.db"
            with Database(path) as db:
                channel = db.upsert_channel("source", None, "", "2020")
                post = db.upsert_post(
                    channel, message_id=1, date=None, text="caption", views=None,
                    source="", scraped_at="2020",
                )
                db.replace_photos(post, [{"position": 1, "media_type": "file",
                                          "image": b"file", "filename": "a.pdf"}])
                db.enqueue_for_publish(post, target="node")
            with patch("wemessenger.httpx.Client", return_value=http_client):
                self.assertEqual(publish_posts_to_wemessenger(path, [post], client, target="node"), 0)
            self.assertEqual(client._send.call_count, 1)
            self.assertIn("file", client._send.call_args.args[1])
            with Database(path) as db:
                self.assertEqual(db.count_queue("node")["failed"], 1)
                self.assertEqual(db.get_photos(post)[0]["image"], b"file")

    def test_later_file_failure_prevents_standalone_caption_continuation(self):
        client = WeMessengerClient("token", "node")
        http_client = MagicMock()
        http_client.__enter__.return_value = http_client
        client.upload_file = MagicMock(return_value={"uuid": "id", "size": 1})
        client._send = MagicMock(side_effect=[{}, RuntimeError("send failed")])
        with patch("wemessenger.httpx.Client", return_value=http_client):
            with self.assertRaisesRegex(RuntimeError, "send failed"):
                client.send_post(
                    text="a" * (TEXT_LIMIT + 10),
                    media=[(b"first", "a.pdf", "file"), (b"second", "b.mp3", "audio")],
                )
        self.assertEqual(client._send.call_count, 2)
        self.assertTrue(all("file" in call.args[1] for call in client._send.call_args_list))

    def test_all_files_are_sent_before_caption_continuation(self):
        client = WeMessengerClient("token", "node")
        http_client = MagicMock()
        http_client.__enter__.return_value = http_client
        client.upload_file = MagicMock(return_value={"uuid": "id", "size": 1})
        client._send = MagicMock(return_value={})
        text = "a" * (TEXT_LIMIT + 10)
        with patch("wemessenger.httpx.Client", return_value=http_client):
            client.send_post(
                text=text, media=[(b"first", "a.pdf", "file"), (b"second", "b.mp3", "audio")]
            )
        payloads = [call.args[1] for call in client._send.call_args_list]
        self.assertEqual(["file" if "file" in payload else "text" for payload in payloads],
                         ["file", "file", "text"])
        self.assertEqual(payloads[0]["file"]["caption"] + payloads[2]["text"]["text"], text)

    def test_empty_posts_and_chunks_are_not_sent(self):
        client = WeMessengerClient("token", "node")
        client._send = MagicMock()
        client.upload_file = MagicMock()
        for text in ("", " \n\t ", "x" + " " * (TEXT_LIMIT * 2) + "y"):
            with self.subTest(length=len(text)), patch("wemessenger.httpx.Client") as http:
                with self.assertRaises(ValueError):
                    client.send_post(text=text)
                http.assert_not_called()
        with self.assertRaisesRegex(ValueError, "text is empty"):
            client._send_text_chunk(object(), "  \n")
        with self.assertRaisesRegex(ValueError, "Empty media"):
            client.send_media(object(), content=b"", filename="a.pdf", media_type="file")
        client.upload_file.assert_not_called()
        client._send.assert_not_called()

    def test_text_splitting_respects_limit_at_separator_boundary(self):
        client = WeMessengerClient("token", "node")
        for separator in (" ", "\n"):
            text = "a" * TEXT_LIMIT + separator + "b"
            with self.subTest(separator=repr(separator)):
                chunks = client._split_text(text)
                self.assertEqual("".join(chunks), text)
                self.assertTrue(all(chunk.strip() and len(chunk) <= TEXT_LIMIT for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
