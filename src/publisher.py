"""Publish scraped Telegram posts to a WeMessenger channel from DB queue."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from config import ROOT_DIR
from db import Database
from text_clean import clean_post_text
from wemessenger import WeMessengerClient

logger = logging.getLogger(__name__)


def _photo_payloads(db: Database, post_id: int) -> list[tuple[bytes, str]]:
    photos: list[tuple[bytes, str]] = []
    for row in db.get_photos(post_id):
        content = row["image"]
        if not content and row["file_path"]:
            path = Path(row["file_path"])
            if not path.is_absolute():
                path = ROOT_DIR / path
            if path.exists():
                content = path.read_bytes()
        if not content:
            continue
        name = Path(row["file_path"] or f"photo_{row['position']}.jpg").name
        photos.append((bytes(content), name))
    return photos


def publish_posts_to_wemessenger(
    database: Path,
    post_ids: list[int],
    client: WeMessengerClient,
    *,
    send_delay_seconds: int = 20,
    target: str = "wemessenger",
) -> int:
    """Send posts to WeMessenger. Returns count successfully sent."""
    if not post_ids:
        return 0

    sent = 0
    with Database(database) as db:
        ordered = sorted(
            post_ids,
            key=lambda pid: (db.get_post(pid) or {"message_id": 0})["message_id"],
        )

        for index, post_id in enumerate(ordered):
            post = db.get_post(post_id)
            if post is None:
                continue
            if db.is_delivered(post_id, target=target):
                logger.info("skip already sent telegram #%s", post["message_id"])
                continue

            if sent > 0 and send_delay_seconds > 0:
                logger.info(
                    "waiting %ss before next WeMessenger send",
                    send_delay_seconds,
                )
                time.sleep(send_delay_seconds)

            text = clean_post_text(post["text"] or "")
            photos = _photo_payloads(db, post_id)
            now = datetime.now(timezone.utc).isoformat()

            try:
                client.send_post(text=text, photos=photos)
            except Exception as exc:
                db.mark_delivery(
                    post_id,
                    target=target,
                    status="failed",
                    sent_at=None,
                    error=str(exc)[:500],
                )
                logger.error(
                    "WeMessenger FAIL #%s: %s",
                    post["message_id"],
                    exc,
                )
                continue

            db.mark_delivery(
                post_id,
                target=target,
                status="sent",
                sent_at=now,
                error=None,
            )
            sent += 1
            mode = "image+caption" if photos else "text"
            preview = (text or "[photo only]")[:60].replace("\n", " ")
            logger.info(
                "WeMessenger sent #%s (%s): %s",
                post["message_id"],
                mode,
                preview,
            )

        queue = db.count_queue(target)
        logger.info(
            "queue after publish: unpublished=%s pending=%s failed=%s sent=%s",
            queue["unpublished"],
            queue["pending"],
            queue["failed"],
            queue["sent"],
        )

    return sent


def publish_pending_to_wemessenger(
    database: Path,
    client: WeMessengerClient,
    *,
    send_delay_seconds: int = 20,
    target: str = "wemessenger",
    source_channel: str | None = None,
) -> tuple[int, int]:
    """Drain unpublished queue. Returns (sent, pending_before)."""
    with Database(database) as db:
        pending = db.get_pending_delivery_post_ids(
            target=target,
            source_channel=source_channel,
        )
        queue = (
            db.count_channel_queue(target, source_channel)
            if source_channel
            else db.count_queue(target)
        )
    if not pending:
        logger.info("unpublished queue empty (sent=%s)", queue["sent"])
        return 0, 0
    logger.info(
        "unpublished queue: %s (pending=%s, failed=%s)",
        len(pending),
        queue["pending"],
        queue["failed"],
    ) 
    sent = publish_posts_to_wemessenger(
        database,
        pending,
        client,
        send_delay_seconds=send_delay_seconds,
        target=target,
    )
    return sent, len(pending)
