"""Run Telegram scraping, WeMessenger publishing, and retention together."""

from __future__ import annotations

import logging
import threading
import time

from config import load_settings
from db import Database
from logging_setup import setup_logging
from netcheck import wemessenger_reachable
from publisher import _delete_saved_media, publish_pending_to_wemessenger
from scraper import scrape_channel
from wm_client import build_wemessenger

logger = logging.getLogger(__name__)


def _scrape_route(settings: dict, route: dict, *, initial: bool, target: str) -> None:
    proxy = settings["telegram_proxy"] if settings["scraper_use_proxy"] else None
    saved = scrape_channel(
        route["telegram"],
        limit=settings["limit"] if initial else None,
        download_photos=settings["download_media"],
        database=settings["database"],
        only_new=True,
        proxy=proxy,
        target=target,
        max_media_per_post=settings["max_media_per_post"],
        deduplicate_posts=settings["deduplicate_posts"],
        exclude_post_terms=route["exclude_post_terms"],
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


def _enforce_database_limit(settings: dict) -> None:
    max_bytes = int(settings["max_database_size_gb"] * 1024**3)
    with Database(settings["database"]) as db:
        before = db.database_size_bytes()
        if before <= max_bytes:
            return
        deleted = db.delete_oldest_published_until_size(max_bytes)
        after = db.database_size_bytes()

    if deleted:
        logger.warning(
            "database size cleanup removed %s old post(s), size %.2f -> %.2f GiB",
            deleted,
            before / 1024**3,
            after / 1024**3,
        )
    if after > max_bytes:
        logger.error(
            "database remains above %.2f GiB limit (current %.2f GiB); "
            "pending/failed posts were preserved",
            settings["max_database_size_gb"],
            after / 1024**3,
        )


def _cleanup_completed_media(settings: dict) -> None:
    if not settings["delete_media_after_publish"]:
        return
    with Database(settings["database"]) as db:
        post_ids = db.get_completed_post_ids_with_media()
        for post_id in post_ids:
            _delete_saved_media(db, post_id)
    if post_ids:
        logger.info("removed saved media for %s completed posts", len(post_ids))


def _publish_route(settings: dict, route: dict, target: str, client) -> None:
    proxy = settings["telegram_proxy"] if settings["publish_use_proxy"] else None
    if not wemessenger_reachable(settings["wemessenger"]["api_base"], proxy=proxy):
        logger.warning("WeMessenger unreachable; queue retained for %s", route["telegram"])
        return

    sent, pending = publish_pending_to_wemessenger(
        settings["database"],
        client,
        send_delay_seconds=route["send_delay_seconds"],
        target=target,
        source_channel=route["username"],
        delete_media_after_publish=settings["delete_media_after_publish"],
        footer_tag=route["footer_tag"],
        remove_trailing_phrases=route["remove_trailing_phrases"],
        remove_trailing_urls=route["remove_trailing_urls"],
        exclude_post_terms=route["exclude_post_terms"],
    )
    if pending:
        logger.info("published %s/%s post(s) from %s", sent, pending, route["telegram"])


def _scraper_worker(settings: dict, targets: dict[str, str], stop_event: threading.Event) -> None:
    routes = settings["channel_routes"]
    first_scrape = {route["username"]: True for route in routes}
    next_scrape = {route["username"]: 0.0 for route in routes}
    next_expire = time.monotonic() + 86400
    next_database_check = time.monotonic() + 3600

    while not stop_event.is_set():
        now = time.monotonic()
        for route in routes:
            username = route["username"]
            if now < next_scrape[username]:
                continue
            try:
                _scrape_route(
                    settings,
                    route,
                    initial=first_scrape[username],
                    target=targets[username],
                )
                first_scrape[username] = False
            except Exception:
                logger.exception("scrape failed for %s", route["telegram"])
            next_scrape[username] = time.monotonic() + route["poll_interval_seconds"]

        if time.monotonic() >= next_expire:
            try:
                _expire_posts(settings)
            except Exception:
                logger.exception("database retention cleanup failed")
            next_expire = time.monotonic() + 86400

        if time.monotonic() >= next_database_check:
            try:
                _enforce_database_limit(settings)
            except Exception:
                logger.exception("database size cleanup failed")
            next_database_check = time.monotonic() + 3600

        wait_until = min([*next_scrape.values(), next_expire, next_database_check])
        stop_event.wait(max(0.1, min(1.0, wait_until - time.monotonic())))


def _publisher_worker(
    settings: dict,
    targets: dict[str, str],
    clients: dict,
    stop_event: threading.Event,
    route: dict,
) -> None:
    username = route["username"]
    next_publish = 0.0

    while not stop_event.is_set():
        if time.monotonic() >= next_publish:
            try:
                _publish_route(settings, route, targets[username], clients[username])
            except Exception:
                logger.exception("publish failed for %s", route["telegram"])
            next_publish = time.monotonic() + route["publish_interval_seconds"]

        stop_event.wait(max(0.1, min(1.0, next_publish - time.monotonic())))


def main() -> None:
    settings = load_settings()
    setup_logging(
        settings["log_file"],
        level=settings["log_level"],
        max_bytes=settings["log_max_mb"] * 1024 * 1024,
        backup_count=settings["log_backup_count"],
    )
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
    _cleanup_completed_media(settings)
    _expire_posts(settings)
    _enforce_database_limit(settings)

    if not settings["poll"]:
        for route in settings["channel_routes"]:
            try:
                _scrape_route(
                    settings,
                    route,
                    initial=True,
                    target=targets[route["username"]],
                )
            except Exception:
                logger.exception("scrape failed for %s", route["telegram"])
        if publishing_enabled:
            for route in settings["channel_routes"]:
                try:
                    _publish_route(
                        settings,
                        route,
                        targets[route["username"]],
                        clients[route["username"]],
                    )
                except Exception:
                    logger.exception("publish failed for %s", route["telegram"])
        return

    stop_event = threading.Event()
    workers = [
        threading.Thread(
            target=_scraper_worker,
            args=(settings, targets, stop_event),
            name="telegram-scraper",
        )
    ]
    if publishing_enabled:
        workers.extend(
            threading.Thread(
                target=_publisher_worker,
                args=(settings, targets, clients, stop_event, route),
                name=f"wemessenger-publisher-{route['username']}",
            )
            for route in settings["channel_routes"]
        )

    try:
        for worker in workers:
            worker.start()
        while any(worker.is_alive() for worker in workers):
            for worker in workers:
                worker.join(timeout=0.5)
    except KeyboardInterrupt:
        stop_event.set()
        for worker in workers:
            worker.join()
        logger.info("Application stopped.")


if __name__ == "__main__":
    main()
