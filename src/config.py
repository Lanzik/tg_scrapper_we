from __future__ import annotations

from pathlib import Path

import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
SETTINGS_PATH = ROOT_DIR / "settings.yaml"
DEFAULT_DB_PATH = DATA_DIR / "telegram.db"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

def _parse_channels(data: dict) -> list[str]:
    """Accept `channels: [...]` and/or legacy single `channel:`."""
    channels: list[str] = []

    raw_list = data.get("channels")
    if raw_list is not None:
        if not isinstance(raw_list, list):
            raise SystemExit("`channels` in settings.yaml must be a list.")
        for item in raw_list:
            value = str(item or "").strip()
            if value:
                channels.append(value)

    single = str(data.get("channel") or "").strip()
    if single and single not in channels:
        channels.insert(0, single)

    seen: set[str] = set()
    unique: list[str] = []
    for ch in channels:
        key = ch.lstrip("@").lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(ch)

    if not unique:
        raise SystemExit(
            'Set `channels` in settings.yaml '
            '(example: channels: ["@Khabar_Fouri", "@khabaronline_ir"])'
        )
    return unique


def load_settings(path: Path | None = None) -> dict:
    settings_file = path or SETTINGS_PATH
    if not settings_file.exists():
        raise SystemExit(
            f"Missing {settings_file.name}.\n"
            f"Copy settings.yaml.example to settings.yaml and edit the values."
        )

    with settings_file.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    channels = _parse_channels(data)

    limit_raw = data.get("limit", 50)
    try:
        limit_value = int(limit_raw)
    except (TypeError, ValueError):
        raise SystemExit("`limit` in settings.yaml must be an integer (0 = all).")

    interval_raw = data.get("poll_interval", 10)
    try:
        poll_interval = int(interval_raw)
    except (TypeError, ValueError):
        raise SystemExit("`poll_interval` in settings.yaml must be an integer (seconds).")
    if poll_interval < 1:
        raise SystemExit("`poll_interval` must be >= 1 second.")

    publish_interval_raw = data.get("publish_interval", poll_interval)
    try:
        publish_interval = int(publish_interval_raw)
    except (TypeError, ValueError):
        raise SystemExit("`publish_interval` must be an integer (seconds).")
    if publish_interval < 1:
        raise SystemExit("`publish_interval` must be >= 1 second.")

    delay_raw = data.get("send_delay_seconds", 20)
    try:
        send_delay_seconds = int(delay_raw)
    except (TypeError, ValueError):
        raise SystemExit("`send_delay_seconds` must be an integer (seconds).")
    if send_delay_seconds < 0:
        raise SystemExit("`send_delay_seconds` must be >= 0.")

    db_raw = str(data.get("database") or "data/telegram.db").strip()
    db_path = Path(db_raw)
    if not db_path.is_absolute():
        db_path = ROOT_DIR / db_path

    log_raw = str(data.get("log_file") or "logs/scrape.log").strip()
    log_path = Path(log_raw)
    if not log_path.is_absolute():
        log_path = ROOT_DIR / log_path

    publish_log_raw = str(data.get("publish_log_file") or "logs/publish.log").strip()
    publish_log_path = Path(publish_log_raw)
    if not publish_log_path.is_absolute():
        publish_log_path = ROOT_DIR / publish_log_path

    wm = data.get("wemessenger") or {}
    if not isinstance(wm, dict):
        raise SystemExit("`wemessenger` in settings.yaml must be a mapping.")

    proxy_raw = data.get("telegram_proxy")
    if proxy_raw is None or proxy_raw is True:
        telegram_proxy = "socks5://127.0.0.1:10808"
    elif proxy_raw is False or str(proxy_raw).strip().lower() in {"", "null", "none", "false"}:
        telegram_proxy = None
    else:
        telegram_proxy = str(proxy_raw).strip()

    return {
        "channels": channels,
        "limit": None if limit_value == 0 else limit_value,
        "download_photos": bool(data.get("download_photos", True)),
        "database": db_path,
        "poll": bool(data.get("poll", True)),
        "poll_interval": poll_interval,
        "publish_interval": publish_interval,
        "send_delay_seconds": send_delay_seconds,
        "telegram_proxy": telegram_proxy,
        "log_file": log_path,
        "publish_log_file": publish_log_path,
        "log_level": str(data.get("log_level") or "INFO").strip() or "INFO",
        "wemessenger": {
            "enabled": bool(wm.get("enabled", False)),
            "bot_token": str(wm.get("bot_token") or "").strip(),
            "channel_node": str(wm.get("channel_node") or "").strip(),
            "api_base": str(wm.get("api_base") or "https://api.wemessenger.ir").strip(),
            "file_upload_url": str(
                wm.get("file_upload_url") or "https://ms-file.wemessenger.ir/upload"
            ).strip(),
            "to_category": str(wm.get("to_category") or "CHANNEL").strip() or "CHANNEL",
            "session_id": str(wm.get("session_id") or "*").strip() or "*",
        },
    }
