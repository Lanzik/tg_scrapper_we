# Telegram → WeMessenger bridge

Two separate processes (recommended for VPN):

| Script | Network | Job |
|--------|---------|-----|
| `python main.py` | **VPN / SOCKS ON** | Scrape Telegram → SQLite + unpublished queue |
| `python publish.py` | **VPN OFF** (direct) | Send queue to WeMessenger channel |

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp settings.yaml.example settings.yaml
# edit settings.yaml
```

## Run

**Terminal 1 — scrape (proxy/VPN on):**
```bash
source .venv/bin/activate
cd src
python main.py
```

**Terminal 2 — publish (VPN off):**
```bash
source .venv/bin/activate
cd src
python publish.py
```

## Settings

```yaml
channels:
  - "@Khabar_Fouri"
  - "@khabaronline_ir"
telegram_proxy: "socks5://127.0.0.1:10808"
send_delay_seconds: 20
log_file: logs/scrape.log
publish_log_file: logs/publish.log
```

## Behavior

- Scraper saves text + photos and enqueues as `pending` in `publish_queue`
- Publisher sends image+caption (`file.caption`), waits `send_delay_seconds` between posts
- Channel `@username` footer is stripped from text
- Logs: `logs/scrape.log`, `logs/publish.log`
