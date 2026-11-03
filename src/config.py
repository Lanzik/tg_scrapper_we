from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
SETTINGS_PATH = ROOT_DIR / "settings.yaml"
DEFAULT_DB_PATH = DATA_DIR / "telegram.db"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

def _channel_username(channel: str) -> str:
    value = channel.strip()
    if "t.me/" in value:
        path = urlparse(value if "://" in value else f"https://{value}").path
        parts = [part for part in path.split("/") if part and part != "s"]
        value = parts[0] if parts else ""
    return value.lstrip("@").strip("/")


def _parse_channel_routes(data: dict) -> list[dict]:
    """Accept legacy channel strings or mappings with per-channel settings."""
    raw_list = data.get("channels")
    if raw_list is not None and not isinstance(raw_list, list):
        raise SystemExit("`channels` in settings.yaml must be a list.")

    entries = list(raw_list or [])
    single = data.get("channel")
    if single:
        entries.insert(0, single)

    wm = data.get("wemessenger") or {}
    default_node = str(wm.get("channel_node") or "").strip()
    defaults = {
        "poll_interval_seconds": data.get("poll_interval", 10),
        "publish_interval_seconds": data.get("publish_interval", 15),
        "send_delay_seconds": data.get("send_delay_seconds", 20),
        "retention_days": data.get("retention_days", 0),
    }
    routes: list[dict] = []
    seen: set[str] = set()
    for entry in entries:
        if isinstance(entry, dict):
            channel = str(entry.get("telegram") or "").strip()
            node = str(entry.get("wemessenger_node") or entry.get("channel_node") or default_node).strip()
        else:
            channel = str(entry or "").strip()
            node = default_node
        if not channel:
            continue

        username = _channel_username(channel)
        if not username:
            raise SystemExit(f"Invalid Telegram channel in settings.yaml: {channel!r}")
        if username.lower() in seen:
            raise SystemExit(f"Duplicate Telegram channel in settings.yaml: {channel}")
        seen.add(username.lower())

        route = {"telegram": channel, "username": username, "wemessenger_node": node}
        raw_footer_tag = (
            entry.get("footer_tag", data.get("footer_tag", "")) if isinstance(entry, dict)
            else data.get("footer_tag", "")
        )
        route["footer_tag"] = str(raw_footer_tag or "").strip()

        trailing_phrases = (
            entry.get("remove_trailing_phrases", data.get("remove_trailing_phrases", []))
            if isinstance(entry, dict)
            else data.get("remove_trailing_phrases", [])
        )
        if isinstance(trailing_phrases, str):
            trailing_phrases = [trailing_phrases]
        if not isinstance(trailing_phrases, list):
            raise SystemExit(f"`remove_trailing_phrases` for {channel} must be a list or string.")
        route["remove_trailing_phrases"] = [
            str(phrase).strip() for phrase in trailing_phrases if str(phrase).strip()
        ]
        route["remove_trailing_urls"] = bool(
            entry.get("remove_trailing_urls", data.get("remove_trailing_urls", False))
            if isinstance(entry, dict)
            else data.get("remove_trailing_urls", False)
        )

        for key, default in defaults.items():
            raw_value = entry.get(key, default) if isinstance(entry, dict) else default
            try:
                value = int(raw_value)
            except (TypeError, ValueError):
                raise SystemExit(f"`{key}` for {channel} must be an integer.")
            minimum = 0 if key in {"send_delay_seconds", "retention_days"} else 1
            if value < minimum:
                raise SystemExit(f"`{key}` for {channel} must be >= {minimum}.")
            route[key] = value
        routes.append(route)

    if not routes:
        raise SystemExit(
            'Set `channels` in settings.yaml '
            '(example: channels: ["@Khabar_Fouri", "@khabaronline_ir"])'
        )
    return routes


def load_settings(path: Path | None = None) -> dict:
    settings_file = path or SETTINGS_PATH
    if not settings_file.exists():
        raise SystemExit(
            f"Missing {settings_file.name}.\n"
            f"Copy settings.yaml.example to settings.yaml and edit the values."
        )

    with settings_file.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    wm = data.get("wemessenger") or {}
    if not isinstance(wm, dict):
        raise SystemExit("`wemessenger` in settings.yaml must be a mapping.")

    channel_routes = _parse_channel_routes(data)
    channels = [route["telegram"] for route in channel_routes]

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

    media_limit_raw = data.get("max_media_per_post", 1)
    try:
        max_media_per_post = int(media_limit_raw)
    except (TypeError, ValueError):
        raise SystemExit("`max_media_per_post` must be an integer.")
    if max_media_per_post < 1:
        raise SystemExit("`max_media_per_post` must be >= 1.")

    try:
        max_database_size_gb = float(data.get("max_database_size_gb", 5))
    except (TypeError, ValueError):
        raise SystemExit("`max_database_size_gb` must be a positive number.")
    if max_database_size_gb <= 0:
        raise SystemExit("`max_database_size_gb` must be > 0.")

    try:
        log_max_mb = int(data.get("log_max_mb", 5))
        log_backup_count = int(data.get("log_backup_count", 5))
    except (TypeError, ValueError):
        raise SystemExit("`log_max_mb` and `log_backup_count` must be integers.")
    if log_max_mb < 1 or log_backup_count < 0:
        raise SystemExit("`log_max_mb` must be >= 1 and `log_backup_count` >= 0.")

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

    proxy_raw = data.get("telegram_proxy")
    if proxy_raw is None or proxy_raw is True:
        telegram_proxy = "socks5://127.0.0.1:10808"
    elif proxy_raw is False or str(proxy_raw).strip().lower() in {"", "null", "none", "false"}:
        telegram_proxy = None
    else:
        telegram_proxy = str(proxy_raw).strip()

    scraper_use_proxy = bool(data.get("scraper_use_proxy", True))
    publish_use_proxy = bool(data.get("publish_use_proxy", False))
    download_media = bool(data.get("download_media", data.get("download_photos", True)))

    return {
        "channels": channels,
        "channel_routes": channel_routes,
        "limit": None if limit_value == 0 else limit_value,
        "download_media": download_media,
        "download_photos": download_media,
        "database": db_path,
        "poll": bool(data.get("poll", True)),
        "poll_interval": poll_interval,
        "publish_interval": publish_interval,
        "send_delay_seconds": send_delay_seconds,
        "max_media_per_post": max_media_per_post,
        "deduplicate_posts": bool(data.get("deduplicate_posts", True)),
        "delete_media_after_publish": bool(data.get("delete_media_after_publish", True)),
        "max_database_size_gb": max_database_size_gb,
        "log_max_mb": log_max_mb,
        "log_backup_count": log_backup_count,
        "retention_days": int(data.get("retention_days", 0)),
        "telegram_proxy": telegram_proxy,
        "scraper_use_proxy": scraper_use_proxy,
        "publish_use_proxy": publish_use_proxy,
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
