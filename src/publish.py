"""
Publish unpublished queue to WeMessenger.

Run this WITHOUT VPN/proxy (direct network to api.wemessenger.ir).

  cd src
  python publish.py
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from config import ROOT_DIR, load_settings
from db import Database
from logging_setup import setup_logging
from netcheck import wemessenger_reachable
from publisher import publish_pending_to_wemessenger
from wm_client import build_wemessenger

logger = logging.getLogger(__name__)


def _queue_summary(database: Path) -> str:
    with Database(database) as db:
        q = db.count_queue()
    return (
        f"unpublished={q['unpublished']} "
        f"(pending={q['pending']}, failed={q['failed']}, sent={q['sent']})"
    )


def main() -> None:
    settings = load_settings()
    log_file = settings.get("publish_log_file") or (ROOT_DIR / "logs" / "publish.log")
    if not isinstance(log_file, Path):
        log_file = Path(log_file)
        if not log_file.is_absolute():
            log_file = ROOT_DIR / log_file

    setup_logging(log_file, level=settings["log_level"])
    publish_proxy = settings["telegram_proxy"] if settings.get("publish_use_proxy", False) else None
    client = build_wemessenger(settings)

    logger.info(
        "PUBLISH ONLY | proxy=%s | delay=%ss | interval=%ss | db=%s | log=%s",
        publish_proxy or "direct",
        settings["send_delay_seconds"],
        settings["publish_interval"],
        settings["database"],
        log_file,
    )
    logger.info("Queue now: %s", _queue_summary(settings["database"]))

    if not settings["poll"]:
        if not wemessenger_reachable(
            settings["wemessenger"]["api_base"],
            proxy=publish_proxy,
        ):
            raise SystemExit(
                "WeMessenger unreachable. Check the proxy or direct connectivity and try again."
            )
        sent, total = publish_pending_to_wemessenger(
            settings["database"],
            client,
            send_delay_seconds=settings["send_delay_seconds"],
        )
        logger.info("Done. Published %s/%s", sent, total)
        return

    logger.info("Polling WeMessenger queue. Ctrl+C to stop.")
    try:
        while True:
            started = datetime.now(timezone.utc).strftime("%H:%M:%S")
            summary = _queue_summary(settings["database"])
            api_base = settings["wemessenger"]["api_base"]

            if not wemessenger_reachable(api_base, proxy=publish_proxy):
                logger.warning(
                    "[%s] WeMessenger unreachable via %s — queue remains [%s]",
                    started,
                    publish_proxy or "direct",
                    summary,
                )
            else:
                logger.info("[%s] flushing queue [%s]", started, summary)
                try:
                    sent, total = publish_pending_to_wemessenger(
                        settings["database"],
                        client,
                        send_delay_seconds=settings["send_delay_seconds"],
                    )
                    logger.info("Published %s/%s this round", sent, total)
                except Exception as exc:
                    logger.exception("publish error: %s", exc)

            time.sleep(settings["publish_interval"])
    except KeyboardInterrupt:
        logger.info("Publish stopped.")


if __name__ == "__main__":
    main()
