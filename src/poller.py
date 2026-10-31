"""Poll Telegram channels on an interval and save only new posts."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from scraper import scrape_channel


def run_poller(
    channels: list[str],
    *,
    interval: int = 10,
    limit: int | None = 50,
    download_photos: bool = True,
    database: Path | None = None,
    proxy: str | None = None,
    on_posts_saved: Callable[[list[int]], None] | None = None,
) -> None:
    """
    Forever loop: every `interval` seconds, fetch new posts from each channel.

    First tick also backfills up to `limit` posts per channel if the DB is empty.
    Stop with Ctrl+C.
    """
    names = ", ".join(f"@{c.lstrip('@')}" for c in channels)
    print(f"Polling {names} every {interval}s (Ctrl+C to stop)\n")

    first = True
    while True:
        started = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"[{started}] checking {len(channels)} channel(s)...")

        for channel in channels:
            try:
                print(f"  → {channel}")
                saved_ids = scrape_channel(
                    channel,
                    limit=limit if first else None,
                    download_photos=download_photos,
                    database=database,
                    only_new=True,
                    proxy=proxy,
                )
                if saved_ids and on_posts_saved:
                    on_posts_saved(saved_ids)
            except Exception as exc:  # keep polling other channels
                print(f"  error ({channel}): {exc}")

        first = False
        time.sleep(interval)
