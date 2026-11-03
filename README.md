# Telegram to WeMessenger bridge

Scrape public Telegram channels, route each source to its configured WeMessenger channel, publish queued posts, and expire old database records in one process.

## Configure

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp settings.yaml.example settings.yaml
```

Edit `settings.yaml`. Each `channels` entry maps one Telegram source to one WeMessenger node and can set its own schedule and retention period:

```yaml
channels:
  - telegram: "@source_one"
    wemessenger_node: "WE_CHANNEL_NODE_UUID"
    poll_interval_seconds: 30
    publish_interval_seconds: 15
    send_delay_seconds: 20
    retention_days: 30
```

`poll_interval_seconds` is the pause between Telegram checks. `publish_interval_seconds` is the pause between attempts to drain that source's queue. `send_delay_seconds` is the pause between messages in a publishing batch. `retention_days` deletes posts older than that many days, measured from when they were scraped; set it to `0` to keep data indefinitely. Deleting a post also deletes its photos and queue rows.

The shared WeMessenger bot token and API settings live under `wemessenger`. Proxy settings for scraping and publishing are independent. Legacy string channel lists and a single top-level `wemessenger.channel_node` remain supported.

## Run

Run the combined scraper and publisher from the project root:

```bash
python src/main.py
```

Set `poll: false` to perform one scrape/publish pass and exit. Stop a continuous run with Ctrl+C.

For deployment, run this command from a service manager such as systemd or Supervisor with the project root as its working directory. Keep `settings.yaml` on the host and ensure the service user can write to the configured database and log paths. The process handles `SIGINT` via Ctrl+C; use the service manager's normal stop/restart controls.

If the Telegram proxy runs on another host, set `telegram_proxy` to an address reachable by the service.

## Tests

```bash
python -m unittest discover -s tests -v
```
