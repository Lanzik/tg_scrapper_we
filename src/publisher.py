"""Publish scraped Telegram posts to a WeMessenger channel from DB queue."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from config import ROOT_DIR
from db import Database
from text_clean import clean_post_text, contains_excluded_term
from wemessenger import WeMessengerClient

logger = logging.getLogger(__name__)


def _media_payloads(db: Database, post_id: int) -> list[tuple[bytes, str, str]]:
    media: list[tuple[bytes, str, str]] = []
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
        media.append((bytes(content), name, str(row["media_type"] or "image")))
    return media


def _delete_saved_media(db: Database, post_id: int) -> None:
    media_root = (ROOT_DIR / "data" / "media").resolve()
    for stored_path in db.delete_post_media(post_id):
        path = Path(stored_path)
        if not path.is_absolute():
            path = ROOT_DIR / path
        path = path.resolve()
        try:
            path.relative_to(media_root)
        except ValueError:
            logger.warning("not deleting media outside data/media: %s", stored_path)
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("could not remove published media %s: %s", path, exc)
            continue

        parent = path.parent
        while parent != media_root:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent


def publish_posts_to_wemessenger(
    database: Path,
    post_ids: list[int],
    client: WeMessengerClient,
    *,
    send_delay_seconds: int = 20,
    target: str = "wemessenger",
    delete_media_after_publish: bool = True,
    footer_tag: str | None = None,
    remove_trailing_phrases: list[str] | None = None,
    remove_trailing_urls: bool = False,
    exclude_post_terms: list[str] | None = None,
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
            excluded_term = contains_excluded_term(
                post["text"] or "",
                exclude_post_terms or [],
            )
            if excluded_term:
                db.mark_delivery(
                    post_id,
                    target=target,
                    status="filtered",
                    sent_at=None,
                    error=f"excluded by term {excluded_term!r}",
                )
                logger.info(
                    "skip filtered telegram #%s (matched %r)",
                    post["message_id"],
                    excluded_term,
                )
                if delete_media_after_publish:
                    _delete_saved_media(db, post_id)
                continue
            duplicate_of = db.get_prior_duplicate_post_id(post_id)
            if duplicate_of is not None:
                db.mark_delivery(
                    post_id,
                    target=target,
                    status="duplicate",
                    sent_at=None,
                    error=f"duplicate content of post #{duplicate_of}",
                )
                logger.info(
                    "skip duplicate telegram #%s (matches stored post #%s)",
                    post["message_id"],
                    duplicate_of,
                )
                if delete_media_after_publish:
                    _delete_saved_media(db, post_id)
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

            text = clean_post_text(
                post["text"] or "",
                footer_tag=footer_tag,
                remove_trailing_phrases=remove_trailing_phrases or [],
                remove_trailing_urls=remove_trailing_urls,
            )
            media = _media_payloads(db, post_id)
            now = datetime.now(timezone.utc).isoformat()

            try:
                client.send_post(text=text, media=media)
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
            if delete_media_after_publish:
                _delete_saved_media(db, post_id)
            sent += 1
            mode = f"{media[0][2]}+caption" if media else "text"
            preview = (text or "[media only]")[:60].replace("\n", " ")
            logger.info(
                "WeMessenger sent #%s (%s): %s",
                post["message_id"],
                mode,
                preview,
            )

        queue = db.count_queue(target)
        logger.info(
            "queue after publish: unpublished=%s pending=%s failed=%s sent=%s duplicates=%s",
            queue["unpublished"],
            queue["pending"],
            queue["failed"],
            queue["sent"],
            queue.get("duplicate", 0),
        )

    return sent


def publish_pending_to_wemessenger(
    database: Path,
    client: WeMessengerClient,
    *,
    send_delay_seconds: int = 20,
    target: str = "wemessenger",
    source_channel: str | None = None,
    delete_media_after_publish: bool = True,
    footer_tag: str | None = None,
    remove_trailing_phrases: list[str] | None = None,
    remove_trailing_urls: bool = False,
    exclude_post_terms: list[str] | None = None,
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
        delete_media_after_publish=delete_media_after_publish,
        footer_tag=footer_tag,
        remove_trailing_phrases=remove_trailing_phrases,
        remove_trailing_urls=remove_trailing_urls,
        exclude_post_terms=exclude_post_terms,
    )
    return sent, len(pending)
