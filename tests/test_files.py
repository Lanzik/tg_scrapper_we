import sys
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from db import Database
from publisher import publish_posts_to_wemessenger
from scraper import _download_bytes, _parse_page, _save_post
from wemessenger import WeMessengerClient


class FileTests(unittest.TestCase):
    def test_parser_handles_public_file_and_audio_and_unavailable_file(self):
        html = """
        <div class="etme_widget_message" data-post="test/1">
          <a class="etme_widget_message_document_wrap" href="/download_abc?token=x">
            <div class="etme_widget_message_document_title">book.pdf</div>
          </a>
        </div>
        <div class="etme_widget_message" data-post="test/2">
          <div class="etme_widget_message_audio_player">
            <div class="etme_widget_message_audio_title">music.mp3</div>
            <audio src="/download_song"></audio>
          </div>
        </div>
        <div class="etme_widget_message" data-post="test/3">
          <a class="etme_widget_message_document_wrap" href="/test/3">
            <div class="etme_widget_message_document_title">archive.zip</div>
          </a><div class="etme_widget_message_text">caption</div>
        </div>
        """
        _, posts = _parse_page(html, platform="eitaa")
        self.assertEqual(posts[0]["media"], [{"type": "file", "url": "https://eitaa.com/download_abc?token=x", "filename": "book.pdf"}])
        self.assertEqual(posts[1]["media"], [{"type": "audio", "url": "https://eitaa.com/download_song", "filename": "music.mp3"}])
        self.assertEqual(posts[2]["media"], [{"type": "file", "url": "", "filename": "archive.zip"}])

    def test_download_keeps_arbitrary_extension_or_mime_type(self):
        for url, content_type, suffix in (
            ("https://example.test/x.zip", "application/octet-stream", ".zip"),
            ("https://example.test/hashed", "application/pdf", ".pdf"),
            ("https://example.test/hashed", "application/octet-stream", ".bin"),
        ):
            with self.subTest(url=url, content_type=content_type):
                client = MagicMock()
                client.get.return_value = httpx.Response(
                    200, content=b"contents", headers={"content-type": content_type},
                    request=httpx.Request("GET", url),
                )
                self.assertEqual(_download_bytes(client, url, media_type="file"), (b"contents", suffix))

    def test_files_are_stored_with_filename_and_published_with_caption(self):
        for media_type, filename in (("file", "جزوه.pdf"), ("audio", "song.mp3")):
            with self.subTest(media_type=media_type), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                path = root / "posts.db"
                post = {"id": 1, "date": None, "text": "caption", "views": None,
                        "media": [{"type": media_type, "url": "https://example.test/download", "filename": filename}]}
                with Database(path) as db:
                    channel = db.upsert_channel("test", None, "", "2020")
                    with patch("scraper.DATA_DIR", root / "data"), patch(
                        "scraper._download_bytes", return_value=(b"content", ".bin")
                    ):
                        post_id = _save_post(
                            db, MagicMock(), channel_id=channel, username="test", post=post,
                            media_dir=root / "data/media/test", download_photos=True,
                            max_media_per_post=1, scraped_at="2020", target="node"
                        )
                    row = db.get_photos(post_id)[0]
                    self.assertEqual(row["filename"], filename)
                    self.assertEqual(row["media_type"], media_type)
                    self.assertTrue((root / row["file_path"]).exists())
                sender = MagicMock()
                with patch("publisher.ROOT_DIR", root):
                    self.assertEqual(publish_posts_to_wemessenger(
                        path, [post_id], sender, target="node", delete_media_after_publish=False
                    ), 1)
                sender.send_post.assert_called_once_with(text="caption", media=[(b"content", filename, media_type)])

    def test_we_uploads_file_and_audio_with_caption(self):
        for media_type, name in (("file", "report.pdf"), ("audio", "song.mp3")):
            with self.subTest(media_type=media_type):
                client = WeMessengerClient("token", "node")
                client.upload_file = MagicMock(return_value={"uuid": "id", "size": 7})
                client._send = MagicMock(return_value={})
                http = MagicMock()
                http.__enter__.return_value = http
                with patch("wemessenger.httpx.Client", return_value=http):
                    client.send_post(text="caption", media=[(b"content", name, media_type)])
                self.assertEqual(client.upload_file.call_args.kwargs["file_type"], media_type)
                payload = client._send.call_args.args[1]["file"]
                self.assertEqual((payload["type"], payload["name"], payload["caption"]), (media_type, name, "caption"))

    def test_on_demand_download_preserves_name(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "posts.db"
            with Database(path) as db:
                channel = db.upsert_channel("test", None, "", "2020")
                post_id = db.upsert_post(channel, message_id=1, date=None, text="caption", views=None, source="", scraped_at="2020")
                db.replace_photos(post_id, [{"position": 1, "media_type": "file",
                                            "url": "https://example.test/download", "filename": "../book.pdf"}])
                db.enqueue_for_publish(post_id, target="node")
            sender = MagicMock()
            http = MagicMock()
            http.__enter__.return_value = http
            with patch("publisher.httpx.Client", return_value=http), patch(
                "publisher._download_bytes", return_value=(b"pdf", ".bin")
            ):
                self.assertEqual(publish_posts_to_wemessenger(path, [post_id], sender, target="node"), 1)
            sender.send_post.assert_called_once_with(text="caption", media=[(b"pdf", "book.pdf", "file")])

    def test_legacy_media_table_gains_filename_without_losing_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "posts.db"
            with closing(sqlite3.connect(path)) as conn:
                with conn:
                    conn.execute("CREATE TABLE photos (id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL, "
                                 "position INTEGER NOT NULL DEFAULT 1, url TEXT, file_path TEXT, image BLOB)")
                    conn.execute("INSERT INTO photos (post_id, position, url) VALUES (1, 1, 'https://example.test/p')")
            with Database(path) as db:
                row = db.get_photos(1)[0]
                self.assertIsNone(row["filename"])
                self.assertEqual(row["media_type"], "image")
                self.assertEqual(row["url"], "https://example.test/p")

    def test_missing_file_stays_failed_without_sending_caption(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "posts.db"
            with Database(path) as db:
                channel = db.upsert_channel("test", None, "", "2020")
                post_id = db.upsert_post(channel, message_id=1, date=None, text="caption", views=None, source="", scraped_at="2020")
                db.replace_photos(post_id, [{"position": 1, "media_type": "file", "filename": "book.pdf"}])
                db.enqueue_for_publish(post_id, target="node")
            sender = MagicMock()
            self.assertEqual(publish_posts_to_wemessenger(path, [post_id], sender, target="node"), 0)
            sender.send_post.assert_not_called()
            with Database(path) as db:
                self.assertEqual(db.count_queue("node")["failed"], 1)

    def test_file_upload_failure_never_sends_caption(self):
        client = WeMessengerClient("token", "node")
        client.upload_file = MagicMock(side_effect=RuntimeError("upload failed"))
        client._send = MagicMock()
        http = MagicMock()
        http.__enter__.return_value = http
        with patch("wemessenger.httpx.Client", return_value=http):
            with self.assertRaisesRegex(RuntimeError, "after 3 attempts"):
                client.send_post(text="caption", media=[(b"content", "archive.zip", "file")])
        self.assertEqual(client.upload_file.call_count, 3)
        client._send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
