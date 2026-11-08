# Telegram to WeMessenger bridge

Scrape public Telegram channels, route each source to its configured WeMessenger channel, publish queued posts, and expire old database records in one process.

## Configure

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp settings.yaml.example settings.yaml
```

Edit `settings.yaml`. Each `channels` entry maps one Telegram source to one WeMessenger node and can set its own schedule and retention period. Entries can use different destinations, or share a destination when several Telegram sources should publish to the same WeMessenger channel:

```yaml
channels:
  - telegram: "@source_one"
    wemessenger_node: "WE_CHANNEL_NODE_UUID"
    poll_interval_seconds: 30
    publish_interval_seconds: 15
    send_delay_seconds: 20
    retention_days: 30
  - telegram: "@source_two"
    wemessenger_node: "ANOTHER_WE_CHANNEL_NODE_UUID"
  - telegram: "@source_three"
    wemessenger_node: "ANOTHER_WE_CHANNEL_NODE_UUID"
```

In this example, `@source_one` publishes to its own WeMessenger channel, while `@source_two` and `@source_three` both publish to another one. Sources sharing a destination are published sequentially.

`poll_interval_seconds` is the pause between Telegram checks. `publish_interval_seconds` is the pause between attempts to drain that source's queue. `send_delay_seconds` is the pause between messages in a publishing batch. `retention_days` deletes posts older than that many days, measured from when they were scraped; set it to `0` to keep data indefinitely. Deleting a post also deletes its photos and queue rows.

Each channel can set `footer_tag` to ensure its tag appears once on a separate final line. Set `remove_trailing_phrases` at the top level to apply the same list to every channel, without repeating it on each entry:

```yaml
remove_trailing_phrases: ["Link", "Follow us"]
```

It removes the entire trailing line when it contains one of the configured phrases. A channel-level `remove_trailing_phrases` overrides the global list for that channel. `remove_trailing_urls: true` removes URLs from the end of a post. Persian ZWNJ and common Arabic/Persian Yeh/Kaf variants are normalized for phrase matching.

Set top-level `exclude_post_terms` to filter words across all channels, for example `exclude_post_terms: ["تبلیغ", "کون"]`. A channel-level `exclude_post_terms` adds terms to the global list for that channel. Matching is case-insensitive substring matching and ignores Persian/Arabic Kaf and Yeh variants, diacritics, and invisible joiner characters. New matches are skipped before saving; existing queued matches are marked `filtered` and never sent. Filtered posts remain in the database for audit and can be removed later by normal retention.

`download_media` enables media downloads, and `max_media_per_post` limits how many images or videos are downloaded and published from each Telegram post. The limit defaults to `1`; selection follows the media order in the Telegram post. Set a higher value to keep and publish more items. The old `download_photos` key is still accepted. Videos are stored as files and uploaded using WeMessenger's video media type.

`deduplicate_posts: true` (default) skips posts whose normalized text has already been stored, including across configured Telegram channels. For posts with no text, the checker compares media type and URL. Duplicate posts are skipped before saving or queueing, so they are not published. Set it to `false` to disable the check.

`delete_media_after_publish: true` removes downloaded media files and DB media rows only after a successful WeMessenger send; failed posts retain media for retry. On startup, media for previously completed posts is cleaned up too.

`max_database_size_gb` sets the SQLite size limit (default `5`). At startup and hourly, if the DB is over the limit, the app removes the oldest sent/duplicate posts in batches and vacuums SQLite. Pending and failed posts are never purged. If they alone keep the DB over the limit, the app logs an error instead of losing unpublished data. Per-channel retention follows the same protection.

`log_max_mb` and `log_backup_count` configure rotating logs (defaults `5` MB and `5` backups, for about `30` MB total per log file). Old rotated logs are removed automatically.

`delete_media_after_publish: true` removes a post's downloaded media files and DB media rows only after a successful WeMessenger send; failed posts keep their media for retry. On startup, previously sent/duplicate posts with remaining media are cleaned up too.

`max_database_size_gb` sets the SQLite size limit (default `5`). At startup and hourly, if the DB is over the limit, the app removes the oldest sent/duplicate posts in batches and vacuums SQLite. Pending and failed posts are never purged; if they alone keep the DB over the limit, the app logs an error instead of losing unpublished data. Per-channel `retention_days` follows the same pending/failed protection.

`log_max_mb` and `log_backup_count` configure rotating logs (defaults `5` MB and `5` backups, for about `30` MB total per log file). Old rotated logs are removed automatically.

The shared WeMessenger bot token and API settings live under `wemessenger`. Proxy settings for scraping and publishing are independent. Legacy string channel lists and a single top-level `wemessenger.channel_node` remain supported.

## Run

Run the combined scraper and publisher from the project root:

```bash
python src/main.py
```

Set `poll: false` to perform one scrape/publish pass and exit. Stop a continuous run with Ctrl+C.
In continuous mode, all due Telegram channels are scraped sequentially in a newly shuffled order each pass. Each channel keeps its own polling interval. Publishing runs separately, with one worker per configured channel; a channel's `send_delay_seconds` does not delay other channels. One-shot mode scrapes first, then publishes the resulting queue.

For deployment, run this command from a service manager such as systemd or Supervisor with the project root as its working directory. Keep `settings.yaml` on the host and ensure the service user can write to the configured database and log paths. The process handles `SIGINT` via Ctrl+C; use the service manager's normal stop/restart controls.

If the Telegram proxy runs on another host, set `telegram_proxy` to an address reachable by the service.

## Tests

```bash
python -m unittest discover -s tests -v
```
