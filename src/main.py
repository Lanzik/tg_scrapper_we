"""Run Telegram scraping, WeMessenger publishing, and retention together."""

from __future__ import annotations

import logging
import time

from config import load_settings
from db import Database
from logging_setup import setup_logging
from netcheck import wemessenger_reachable
from publisher import publish_pending_to_wemessenger
from scraper import scrape_channel
from wm_client import build_wemessenger

logger = logging.getLogger(__name__)


def _scrape_route(settings: dict, route: dict, *, initial: bool, target: str) -> None:
    proxy = settings["telegram_proxy"] if settings["scraper_use_proxy"] else None
    saved = scrape_channel(
        route["telegram"],
        limit=settings["limit"] if initial else None,
        download_photos=settings["download_photos"],
        database=settings["database"],
        only_new=True,
        proxy=proxy,
        target=target,
    )
    if saved:
        logger.info("queued %s new post(s) from %s", len(saved), route["telegram"])


def _expire_posts(settings: dict) -> None:
    with Database(settings["database"]) as db:
        for route in settings["channel_routes"]:
            deleted = db.delete_channel_posts_older_than(
                route["username"], route["retention_days"]
            )
            if deleted:
                logger.info(
                    "expired %s post(s) from %s (retention=%s days)",
                    deleted,
                    route["telegram"],
                    route["retention_days"],
                )


def main() -> None:
    settings = load_settings()
    setup_logging(settings["log_file"], level=settings["log_level"])
    publishing_enabled = settings["wemessenger"]["enabled"]
    targets = {
        route["username"]: route["wemessenger_node"] if publishing_enabled else "wemessenger"
        for route in settings["channel_routes"]
    }
    clients = {}
    if publishing_enabled:
        with Database(settings["database"]) as db:
            db.route_legacy_queue(targets)
        clients = {
            route["username"]: build_wemessenger(
                settings,
                channel_node=route["wemessenger_node"],
            )
            for route in settings["channel_routes"]
        }

    logger.info(
        "APP START | routes=%s | scrape_proxy=%s | publish_proxy=%s | db=%s",
        len(settings["channel_routes"]),
        "enabled" if settings["scraper_use_proxy"] else "direct",
        "enabled" if settings["publish_use_proxy"] else "direct",
        settings["database"],
    )
    _expire_posts(settings)

    first_scrape = {route["username"]: True for route in settings["channel_routes"]}
    next_scrape = {route["username"]: 0.0 for route in settings["channel_routes"]}
    next_publish = {route["username"]: 0.0 for route in settings["channel_routes"]}
    next_expire = time.monotonic() + 86400

    try:
        while True:
            now = time.monotonic()
            for route in settings["channel_routes"]:
                username = route["username"]
                target = targets[username]
                if now >= next_scrape[username]:
                    try:
                        _scrape_route(settings, route, initial=first_scrape[username], target=target)
                        first_scrape[username] = False
                    except Exception:
                        logger.exception("scrape failed for %s", route["telegram"])
                    next_scrape[username] = time.monotonic() + route["poll_interval_seconds"]

                if publishing_enabled and now >= next_publish[username]:
                    try:
                        proxy = settings["telegram_proxy"] if settings["publish_use_proxy"] else None
                        if wemessenger_reachable(settings["wemessenger"]["api_base"], proxy=proxy):
                            sent, pending = publish_pending_to_wemessenger(
                                settings["database"],
                                clients[username],
                                send_delay_seconds=route["send_delay_seconds"],
                                target=target,
                                source_channel=username,
                            )
                            if pending:
                                logger.info("published %s/%s post(s) from %s", sent, pending, route["telegram"])
                        else:
                            logger.warning("WeMessenger unreachable; queue retained for %s", route["telegram"])
                    except Exception:
                        logger.exception("publish failed for %s", route["telegram"])
                    next_publish[username] = time.monotonic() + route["publish_interval_seconds"]

            if time.monotonic() >= next_expire:
                try:
                    _expire_posts(settings)
                except Exception:
                    logger.exception("database retention cleanup failed")
                next_expire = time.monotonic() + 86400

            if not settings["poll"]:
                return

            due_times = list(next_scrape.values())
            if publishing_enabled:
                due_times.extend(next_publish.values())
            due_times.append(next_expire)
            time.sleep(max(0.1, min(1.0, min(due_times) - time.monotonic())))
    except KeyboardInterrupt:
        logger.info("Application stopped.")


if __name__ == "__main__":
    main()
