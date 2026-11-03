"""SQLite storage for scraped Telegram posts, photos, and publish queue."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from text_clean import clean_post_text

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS channels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT    NOT NULL UNIQUE,
    title       TEXT,
    source      TEXT,
    updated_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS posts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id  INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    message_id  INTEGER NOT NULL,
    date        TEXT,
    text        TEXT,
    views       TEXT,
    source      TEXT,
    scraped_at  TEXT    NOT NULL,
    content_hash TEXT,
    UNIQUE (channel_id, message_id)
);

CREATE TABLE IF NOT EXISTS photos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id     INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL DEFAULT 1,
    url         TEXT,
    file_path   TEXT,
    media_type  TEXT    NOT NULL DEFAULT 'image',
    image       BLOB,
    UNIQUE (post_id, position)
);

-- Unpublished queue for WeMessenger (and future targets)
CREATE TABLE IF NOT EXISTS publish_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id     INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    target      TEXT    NOT NULL,
    status      TEXT    NOT NULL,   -- pending | sent | failed
    queued_at   TEXT    NOT NULL,
    sent_at     TEXT,
    error       TEXT,
    UNIQUE (post_id, target)
);

CREATE INDEX IF NOT EXISTS idx_posts_channel ON posts(channel_id);
CREATE INDEX IF NOT EXISTS idx_posts_date ON posts(date);
CREATE INDEX IF NOT EXISTS idx_photos_post ON photos(post_id);
CREATE INDEX IF NOT EXISTS idx_publish_queue_status ON publish_queue(target, status);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _content_fingerprint(text: str, media: list[dict] | None = None) -> str | None:
    normalized_text = unicodedata.normalize("NFKC", clean_post_text(text or "")).casefold()
    normalized_text = "".join(
        character
        for character in normalized_text
        if unicodedata.category(character) != "Cf"
    )
    normalized_text = normalized_text.replace("\u200c", " ")
    normalized_text = re.sub(r"https?://\S+", " ", normalized_text)
    normalized_text = re.sub(r"\s*@[^\s@]{4,}\s*$", "", normalized_text)
    normalized_text = " ".join(normalized_text.split())
    if normalized_text:
        content = {"text": normalized_text}
    else:
        media_items = []
        for item in media or []:
            url = str(item.get("url") or "").split("?", 1)[0].split("#", 1)[0]
            media_type = str(item.get("type") or item.get("media_type") or "image").lower()
            if url:
                media_items.append({"type": media_type, "url": url})
        if not media_items:
            return None
        content = {"media": media_items}

    payload = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate_media_type()
        self._migrate_content_hashes()
        self._migrate_from_deliveries()
        self._backfill_missing_queue_rows()
        self.conn.commit()

    def _migrate_media_type(self) -> None:
        columns = {
            str(row["name"])
            for row in self.conn.execute("PRAGMA table_info(photos)").fetchall()
        }
        if "media_type" not in columns:
            self.conn.execute(
                "ALTER TABLE photos ADD COLUMN media_type TEXT NOT NULL DEFAULT 'image'"
            )

    def _migrate_content_hashes(self) -> None:
        columns = {
            str(row["name"])
            for row in self.conn.execute("PRAGMA table_info(posts)").fetchall()
        }
        if "content_hash" not in columns:
            self.conn.execute("ALTER TABLE posts ADD COLUMN content_hash TEXT")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_posts_content_hash ON posts(content_hash)"
        )

        rows = self.conn.execute(
            "SELECT id, text FROM posts WHERE content_hash IS NULL"
        ).fetchall()
        for row in rows:
            media = [
                {"type": item["media_type"], "url": item["url"]}
                for item in self.conn.execute(
                    "SELECT media_type, url FROM photos WHERE post_id = ? ORDER BY position",
                    (row["id"],),
                ).fetchall()
            ]
            fingerprint = _content_fingerprint(row["text"] or "", media)
            if fingerprint:
                self.conn.execute(
                    "UPDATE posts SET content_hash = ? WHERE id = ?",
                    (fingerprint, row["id"]),
                )

    def _migrate_from_deliveries(self) -> None:
        """Copy legacy deliveries table into publish_queue once."""
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='deliveries'"
        ).fetchone()
        if not row:
            return
        self.conn.execute(
            """
            INSERT OR IGNORE INTO publish_queue
                (post_id, target, status, queued_at, sent_at, error)
            SELECT
                post_id,
                target,
                status,
                COALESCE(sent_at, ?),
                CASE WHEN status = 'sent' THEN sent_at ELSE NULL END,
                error
            FROM deliveries
            """,
            (_now(),),
        )

    def _backfill_missing_queue_rows(self) -> None:
        """Any post without a queue row becomes pending (unpublished)."""
        self.conn.execute(
            """
            INSERT INTO publish_queue (post_id, target, status, queued_at, sent_at, error)
            SELECT p.id, 'wemessenger', 'pending', ?, NULL, NULL
            FROM posts p
            WHERE NOT EXISTS (
                SELECT 1 FROM publish_queue q
                WHERE q.post_id = p.id
            )
            """,
            (_now(),),
        )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def upsert_channel(self, username: str, title: str | None, source: str, updated_at: str) -> int:
        self.conn.execute(
            """
            INSERT INTO channels (username, title, source, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                title = excluded.title,
                source = excluded.source,
                updated_at = excluded.updated_at
            """,
            (username, title, source, updated_at),
        )
        row = self.conn.execute(
            "SELECT id FROM channels WHERE username = ?", (username,)
        ).fetchone()
        self.conn.commit()
        return int(row["id"])

    def upsert_post(
        self,
        channel_id: int,
        *,
        message_id: int,
        date: str | None,
        text: str,
        views: str | None,
        source: str,
        scraped_at: str,
        media: list[dict] | None = None,
    ) -> int:
        content_hash = _content_fingerprint(text, media)
        self.conn.execute(
            """
            INSERT INTO posts
                (channel_id, message_id, date, text, views, source, scraped_at, content_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id, message_id) DO UPDATE SET
                date = excluded.date,
                text = excluded.text,
                views = excluded.views,
                source = excluded.source,
                scraped_at = excluded.scraped_at,
                content_hash = excluded.content_hash
            """,
            (channel_id, message_id, date, text, views, source, scraped_at, content_hash),
        )
        row = self.conn.execute(
            """
            SELECT id FROM posts
            WHERE channel_id = ? AND message_id = ?
            """,
            (channel_id, message_id),
        ).fetchone()
        self.conn.commit()
        return int(row["id"])

    def has_duplicate_post(self, text: str, media: list[dict] | None = None) -> bool:
        fingerprint = _content_fingerprint(text, media)
        if not fingerprint:
            return False
        return self.conn.execute(
            "SELECT 1 FROM posts WHERE content_hash = ? LIMIT 1",
            (fingerprint,),
        ).fetchone() is not None

    def get_prior_duplicate_post_id(self, post_id: int) -> int | None:
        row = self.conn.execute(
            """
            SELECT previous.id
            FROM posts current
            JOIN posts previous
              ON previous.content_hash = current.content_hash
             AND previous.id < current.id
            WHERE current.id = ?
            ORDER BY previous.id ASC
            LIMIT 1
            """,
            (post_id,),
        ).fetchone()
        return int(row["id"]) if row else None

    def replace_photos(
        self,
        post_id: int,
        photos: list[dict],
    ) -> None:
        """photos items: position, url, file_path, image (bytes|None)."""
        self.conn.execute("DELETE FROM photos WHERE post_id = ?", (post_id,))
        self.conn.executemany(
            """
                INSERT INTO photos (post_id, position, url, file_path, media_type, image)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    post_id,
                    p["position"],
                    p.get("url"),
                    p.get("file_path"),
                    p.get("media_type", "image"),
                    p.get("image"),
                )
                for p in photos
            ],
        )
        self.conn.commit()

    def get_channel_id(self, username: str) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM channels WHERE username = ?", (username,)
        ).fetchone()
        return int(row["id"]) if row else None

    def get_max_message_id(self, username: str) -> int | None:
        row = self.conn.execute(
            """
            SELECT MAX(p.message_id) AS max_id
            FROM posts p
            JOIN channels c ON c.id = p.channel_id
            WHERE c.username = ?
            """,
            (username,),
        ).fetchone()
        if not row or row["max_id"] is None:
            return None
        return int(row["max_id"])

    def has_post(self, channel_id: int, message_id: int) -> bool:
        row = self.conn.execute(
            """
            SELECT 1 FROM posts
            WHERE channel_id = ? AND message_id = ?
            """,
            (channel_id, message_id),
        ).fetchone()
        return row is not None

    def get_post(self, post_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM posts WHERE id = ?", (post_id,)
        ).fetchone()

    def get_photos(self, post_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT * FROM photos
                WHERE post_id = ?
                ORDER BY position ASC
                """,
                (post_id,),
            )
        )

    def enqueue_for_publish(
        self,
        post_id: int,
        *,
        target: str = "wemessenger",
    ) -> None:
        """
        Put post on unpublished queue.

        Never downgrades an already-sent item back to pending.
        """
        existing = self.conn.execute(
            """
            SELECT status FROM publish_queue
            WHERE post_id = ? AND target = ?
            """,
            (post_id, target),
        ).fetchone()
        if existing and existing["status"] == "sent":
            return

        self.conn.execute(
            """
            INSERT INTO publish_queue (post_id, target, status, queued_at, sent_at, error)
            VALUES (?, ?, 'pending', ?, NULL, NULL)
            ON CONFLICT(post_id, target) DO UPDATE SET
                status = 'pending',
                error = NULL
            WHERE publish_queue.status != 'sent'
            """,
            (post_id, target, _now()),
        )
        self.conn.commit()

    def enqueue_many(self, post_ids: list[int], *, target: str = "wemessenger") -> int:
        for post_id in post_ids:
            self.enqueue_for_publish(post_id, target=target)
        return len(post_ids)

    def is_delivered(self, post_id: int, target: str = "wemessenger") -> bool:
        row = self.conn.execute(
            """
            SELECT status FROM publish_queue
            WHERE post_id = ? AND target = ?
            """,
            (post_id, target),
        ).fetchone()
        return bool(row and row["status"] == "sent")

    def mark_delivery(
        self,
        post_id: int,
        *,
        target: str = "wemessenger",
        status: str,
        sent_at: str | None = None,
        error: str | None = None,
    ) -> None:
        queued_at = _now()
        self.conn.execute(
            """
            INSERT INTO publish_queue (post_id, target, status, queued_at, sent_at, error)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(post_id, target) DO UPDATE SET
                status = excluded.status,
                sent_at = excluded.sent_at,
                error = excluded.error
            """,
            (post_id, target, status, queued_at, sent_at, error),
        )
        self.conn.commit()

    def get_pending_delivery_post_ids(
        self,
        target: str = "wemessenger",
        source_channel: str | None = None,
    ) -> list[int]:
        """Unpublished queue: pending or failed, optionally by source channel."""
        query = """
            SELECT q.post_id AS id
            FROM publish_queue q
            JOIN posts p ON p.id = q.post_id
            JOIN channels c ON c.id = p.channel_id
            WHERE q.target = ?
              AND q.status IN ('pending', 'failed')
        """
        params: list = [target]
        if source_channel:
            query += " AND c.username = ?"
            params.append(source_channel)
        query += " ORDER BY q.queued_at ASC, p.message_id ASC, p.id ASC"
        rows = self.conn.execute(query, params).fetchall()
        return [int(row["id"]) for row in rows]

    def count_queue(self, target: str = "wemessenger") -> dict[str, int]:
        rows = self.conn.execute(
            """
            SELECT status, COUNT(*) AS n
            FROM publish_queue
            WHERE target = ?
            GROUP BY status
            """,
            (target,),
        ).fetchall()
        counts = {"pending": 0, "failed": 0, "sent": 0}
        for row in rows:
            counts[str(row["status"])] = int(row["n"])
        counts["unpublished"] = counts["pending"] + counts["failed"]
        return counts

    def count_channel_queue(self, target: str, source_channel: str) -> dict[str, int]:
        rows = self.conn.execute(
            """
            SELECT q.status, COUNT(*) AS n
            FROM publish_queue q
            JOIN posts p ON p.id = q.post_id
            JOIN channels c ON c.id = p.channel_id
            WHERE q.target = ? AND c.username = ?
            GROUP BY q.status
            """,
            (target, source_channel),
        ).fetchall()
        counts = {"pending": 0, "failed": 0, "sent": 0}
        for row in rows:
            counts[str(row["status"])] = int(row["n"])
        counts["unpublished"] = counts["pending"] + counts["failed"]
        return counts

    def route_legacy_queue(self, routes: dict[str, str]) -> None:
        """Move legacy global queue rows to each configured channel destination."""
        for source_channel, target in routes.items():
            if target == "wemessenger":
                continue
            self.conn.execute(
                """
                INSERT OR IGNORE INTO publish_queue
                    (post_id, target, status, queued_at, sent_at, error)
                SELECT q.post_id, ?, q.status, q.queued_at, q.sent_at, q.error
                FROM publish_queue q
                JOIN posts p ON p.id = q.post_id
                JOIN channels c ON c.id = p.channel_id
                WHERE q.target = 'wemessenger' AND c.username = ?
                """,
                (target, source_channel),
            )
            self.conn.execute(
                """
                DELETE FROM publish_queue
                WHERE target = 'wemessenger' AND post_id IN (
                    SELECT p.id FROM posts p
                    JOIN channels c ON c.id = p.channel_id
                    WHERE c.username = ?
                )
                """,
                (source_channel,),
            )
        self.conn.commit()

    def delete_channel_posts_older_than(self, username: str, days: int) -> int:
        """Delete aged delivered posts; never discard pending or failed items."""
        if days <= 0:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cursor = self.conn.execute(
            """
            DELETE FROM posts
                        WHERE channel_id = (SELECT id FROM channels WHERE username = ?)
                            AND scraped_at < ?
                            AND NOT EXISTS (
                                    SELECT 1 FROM publish_queue q
                                    WHERE q.post_id = posts.id AND q.status IN ('pending', 'failed')
                            )
            """,
            (username, cutoff),
        )
        self.conn.commit()
        return cursor.rowcount

    def database_size_bytes(self) -> int:
        size = self.path.stat().st_size if self.path.exists() else 0
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{self.path}{suffix}")
            if sidecar.exists():
                size += sidecar.stat().st_size
        return size

    def delete_oldest_published_until_size(
        self,
        max_bytes: int,
        *,
        batch_size: int = 250,
    ) -> int:
        """Purge oldest delivered/duplicate posts until the DB is under its cap."""
        if max_bytes <= 0 or self.database_size_bytes() <= max_bytes:
            return 0

        deleted = 0
        while self.database_size_bytes() > max_bytes:
            rows = self.conn.execute(
                """
                SELECT p.id
                FROM posts p
                WHERE EXISTS (
                    SELECT 1 FROM publish_queue q
                    WHERE q.post_id = p.id AND q.status IN ('sent', 'duplicate', 'filtered')
                )
                AND NOT EXISTS (
                    SELECT 1 FROM publish_queue q
                    WHERE q.post_id = p.id AND q.status IN ('pending', 'failed')
                )
                ORDER BY p.scraped_at ASC, p.id ASC
                LIMIT ?
                """,
                (batch_size,),
            ).fetchall()
            if not rows:
                break

            post_ids = [int(row["id"]) for row in rows]
            placeholders = ",".join("?" for _ in post_ids)
            cursor = self.conn.execute(
                f"DELETE FROM posts WHERE id IN ({placeholders})",
                post_ids,
            )
            self.conn.commit()
            deleted += cursor.rowcount
            self.conn.execute("VACUUM")

        return deleted

    def delete_post_media(self, post_id: int) -> list[str]:
        """Remove saved media records and return their file paths for cleanup."""
        rows = self.conn.execute(
            "SELECT file_path FROM photos WHERE post_id = ? AND file_path IS NOT NULL",
            (post_id,),
        ).fetchall()
        paths = [str(row["file_path"]) for row in rows]
        self.conn.execute("DELETE FROM photos WHERE post_id = ?", (post_id,))
        self.conn.commit()
        return paths

    def get_completed_post_ids_with_media(self) -> list[int]:
        rows = self.conn.execute(
            """
            SELECT DISTINCT p.id
            FROM posts p
            JOIN publish_queue q ON q.post_id = p.id
            JOIN photos m ON m.post_id = p.id
            WHERE q.status IN ('sent', 'duplicate', 'filtered')
              AND NOT EXISTS (
                  SELECT 1 FROM publish_queue pending
                  WHERE pending.post_id = p.id
                    AND pending.status IN ('pending', 'failed')
              )
            """
        ).fetchall()
        return [int(row["id"]) for row in rows]
