"""
Scrape Telegram channels (with SOCKS proxy) into SQLite + publish_queue.

Run this WITH VPN/proxy so Telegram is reachable.

  cd src
  python main.py

Publish separately (VPN OFF):

  python publish.py
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from config import load_settings
from db import Database
from logging_setup import setup_logging
from netcheck import telegram_reachable
from poller import run_poller
from scraper import scrape_channel

logger = logging.getLogger(__name__)


def _scrape_once(settings: dict, *, only_new: bool, first: bool) -> list[int]:
    saved: list[int] = []
    for channel in settings["channels"]:
        logger.info("scrape %s", channel)
        ids = scrape_channel(
            channel,
            limit=settings["limit"] if first else None,
            download_photos=settings["download_photos"],
            database=settings["database"],
            only_new=only_new,
            proxy=settings["telegram_proxy"],
        )
        saved.extend(ids)
    return saved


def _queue_summary(database) -> str:
    with Database(database) as db:
        q = db.count_queue()
    return (
        f"unpublished={q['unpublished']} "
        f"(pending={q['pending']}, failed={q['failed']}, sent={q['sent']})"
    )


def main() -> None:
    settings = load_settings()
    setup_logging(settings["log_file"], level=settings["log_level"])

    logger.info(
        "SCRAPE ONLY (Telegram via proxy) | channels=%s | proxy=%s | "
        "poll_interval=%ss | db=%s | log=%s",
        settings["channels"],
        settings["telegram_proxy"],
        settings["poll_interval"],
        settings["database"],
        settings["log_file"],
    )
    logger.info(
        "Posts are queued in DB. Run publish.py with VPN OFF to send to WeMessenger."
    )
    logger.info("Queue now: %s", _queue_summary(settings["database"]))

    if settings["poll"]:
        def on_saved(post_ids: list[int]) -> None:
            if post_ids:
                logger.info(
                    "queued %s new post(s); %s",
                    len(post_ids),
                    _queue_summary(settings["database"]),
                )

        try:
            # Reachability hint on first tick
            started = datetime.now(timezone.utc).strftime("%H:%M:%S")
            if not telegram_reachable(settings["telegram_proxy"]):
                logger.warning(
                    "[%s] Telegram may be unreachable via %s — "
                    "keep VPN/proxy ON for scraping",
                    started,
                    settings["telegram_proxy"] or "direct",
                )

            run_poller(
                settings["channels"],
                interval=settings["poll_interval"],
                limit=settings["limit"],
                download_photos=settings["download_photos"],
                database=settings["database"],
                proxy=settings["telegram_proxy"],
                on_posts_saved=on_saved,
            )
        except KeyboardInterrupt:
            logger.info("Scrape stopped.")
        return

    _scrape_once(settings, only_new=False, first=True)
    logger.info("Done. %s", _queue_summary(settings["database"]))


if __name__ == "__main__":
    main()
