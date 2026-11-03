"""Scrape public Telegram channels via t.me/s (no login / API keys)."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from config import DATA_DIR, USER_AGENT
from db import Database

PHOTO_URL_RE = re.compile(r"url\(['\"]?(https?://[^'\")\s]+)['\"]?\)")


def normalize_username(channel: str) -> str:
    """Accept @name, name, t.me/name, or t.me/s/name."""
    value = channel.strip()
    if value.startswith("@"):
        value = value[1:]

    if "t.me/" in value:
        path = urlparse(value if "://" in value else f"https://{value}").path
        parts = [p for p in path.split("/") if p and p != "s"]
        if not parts:
            raise ValueError(f"Could not parse channel from: {channel}")
        value = parts[0]

    value = value.strip("/")
    if not value or "/" in value:
        raise ValueError(f"Invalid public channel username: {channel}")
    return value


def _headers() -> dict[str, str]:
    return {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}


def _extract_media(message_el, limit: int) -> list[dict[str, str]]:
    media: list[dict[str, str]] = []
    seen: set[str] = set()

    for element in message_el.select(
        "a.tgme_widget_message_photo_wrap, .tgme_widget_message_video_player, video"
    ):
        media_type = "image"
        url = None
        if element.name == "a":
            match = PHOTO_URL_RE.search(element.get("style") or "")
            url = match.group(1) if match else None
        elif element.name == "video":
            media_type = "video"
            url = element.get("src") or element.get("data-src")
            if not url:
                source = element.select_one("source[src], source[data-src]")
                if source:
                    url = source.get("src") or source.get("data-src")
        else:
            media_type = "video"
            url = (
                element.get("data-video")
                or element.get("data-video-url")
                or element.get("src")
            )

        if not url or not url.startswith(("http://", "https://")) or url in seen:
            continue
        media.append({"type": media_type, "url": url})
        seen.add(url)
        if len(media) >= limit:
            break
    return media


def _parse_message(message_el, *, max_media_per_post: int = 1) -> dict | None:
    data_post = message_el.get("data-post")  # e.g. channel/123
    if not data_post or "/" not in data_post:
        return None

    _, msg_id_str = data_post.rsplit("/", 1)
    try:
        msg_id = int(msg_id_str)
    except ValueError:
        return None

    text_el = message_el.select_one(".tgme_widget_message_text")
    text = text_el.get_text("\n", strip=True) if text_el else ""
    media = _extract_media(message_el, max_media_per_post)
    if not text and not media:
        return None

    time_el = message_el.select_one("time[datetime]")
    date = time_el.get("datetime") if time_el else None

    views_el = message_el.select_one(".tgme_widget_message_views")
    views = views_el.get_text(strip=True) if views_el else None

    return {
        "id": msg_id,
        "data_post": data_post,
        "date": date,
        "text": text,
        "views": views,
        "media": media,
    }


def _parse_page(
    html: str,
    *,
    max_media_per_post: int = 1,
) -> tuple[str | None, list[dict]]:
    soup = BeautifulSoup(html, "lxml")
    title_el = soup.select_one(".tgme_channel_info_header_title")
    title = title_el.get_text(strip=True) if title_el else None

    posts: list[dict] = []
    for el in soup.select(".tgme_widget_message"):
        post = _parse_message(el, max_media_per_post=max_media_per_post)
        if post:
            posts.append(post)
    return title, posts


def _looks_like_telegram_channel_html(html: str) -> bool:
    """Return True only when the response looks like a real public-channel page."""
    if not html or not html.strip():
        return False

    lowered = html.lower()
    has_channel_markers = (
        "tgme_widget_message" in lowered
        or "tgme_channel_info_header_title" in lowered
        or "data-post" in lowered
    )
    if not has_channel_markers:
        challenge_markers = (
            "checking your browser",
            "please wait a few seconds",
            "verify you are human",
            "captcha",
            "cloudflare",
            "challenge",
            "not a robot",
        )
        if any(marker in lowered for marker in challenge_markers):
            return False
        return False
    return True


def _download_bytes(
    client: httpx.Client,
    url: str,
    *,
    media_type: str = "image",
) -> tuple[bytes | None, str]:
    """Return (content, preferred_suffix)."""
    try:
        response = client.get(url)
        response.raise_for_status()
        suffix = Path(urlparse(url).path).suffix.lower()
        allowed = {".mp4", ".m4v", ".mov", ".webm", ".mkv"} if media_type == "video" else {".jpg", ".jpeg", ".png", ".webp", ".gif"}
        if suffix not in allowed:
            suffix = ".mp4" if media_type == "video" else ".jpg"
        return response.content, suffix
    except httpx.HTTPError:
        return None, ".mp4" if media_type == "video" else ".jpg"


def _collect_posts(
    client: httpx.Client,
    username: str,
    *,
    limit: int | None,
    after_id: int | None = None,
    max_media_per_post: int = 1,
) -> tuple[str | None, list[dict]]:
    """
    Fetch posts from the web preview.

    If after_id is set, only keep posts with message_id > after_id (newer).
    """
    collected: dict[int, dict] = {}
    title: str | None = None
    before: int | None = None

    while True:
        url = f"https://t.me/s/{username}"
        if before is not None:
            url = f"{url}?before={before}"

        response = client.get(url)
        if response.status_code == 404:
            raise SystemExit(
                f"Channel @{username} not found or is not public.\n"
                "This scraper only works with public channels (t.me/s/...)."
            )
        response.raise_for_status()

        if not _looks_like_telegram_channel_html(response.text):
            snippet = re.sub(r"\s+", " ", response.text[:500]).strip()
            raise RuntimeError(
                f"Telegram returned HTTP {response.status_code} but no channel content for @{username}. "
                "This usually means a bot/challenge page or a blocked/proxy response. "
                f"Snippet: {snippet!r}"
            )

        page_title, posts = _parse_page(
            response.text,
            max_media_per_post=max_media_per_post,
        )
        if page_title:
            title = page_title

        if not posts:
            if not page_title:
                raise RuntimeError(
                    f"Telegram returned HTTP {response.status_code} for @{username}, "
                    "but the page contained no visible post markup."
                )
            break

        new_on_page = 0
        reached_known = False
        for post in posts:
            if after_id is not None and post["id"] <= after_id:
                reached_known = True
                continue
            if post["id"] in collected:
                continue
            collected[post["id"]] = post
            new_on_page += 1

        oldest_id = min(p["id"] for p in posts)
        if before is not None and oldest_id >= before:
            break
        before = oldest_id

        # For only-new mode: stop once we hit posts already in DB
        if after_id is not None and reached_known:
            break

        if limit is not None and len(collected) >= limit:
            break
        if new_on_page == 0:
            break

    ordered = sorted(collected.values(), key=lambda p: p["id"], reverse=True)
    if limit is not None:
        ordered = ordered[:limit]
    return title, ordered


def _save_post(
    db: Database,
    client: httpx.Client,
    *,
    channel_id: int,
    username: str,
    post: dict,
    media_dir: Path,
    download_photos: bool,
    max_media_per_post: int,
    scraped_at: str,
) -> int:
    post_id = db.upsert_post(
        channel_id,
        message_id=post["id"],
        date=post["date"],
        text=post["text"] or "",
        views=post["views"],
        source=f"https://t.me/{username}/{post['id']}",
        scraped_at=scraped_at,
    )

    media_rows: list[dict] = []
    media_items = post.get("media", [])[:max_media_per_post]
    if download_photos and media_items:
        post_media = media_dir / str(post["id"])
        post_media.mkdir(parents=True, exist_ok=True)

        for i, media in enumerate(media_items, start=1):
            media_type = media["type"]
            content, suffix = _download_bytes(
                client,
                media["url"],
                media_type=media_type,
            )
            if content is None:
                continue

            prefix = "video" if media_type == "video" else "photo"
            name = f"{prefix}_{i}{suffix}" if len(media_items) > 1 else f"{prefix}{suffix}"
            file_path = post_media / name
            file_path.write_bytes(content)

            media_rows.append(
                {
                    "position": i,
                    "url": media["url"],
                    "file_path": str(file_path.relative_to(DATA_DIR.parent)),
                    "media_type": media_type,
                    "image": content if media_type == "image" else None,
                }
            )

    db.replace_photos(post_id, media_rows)

    preview = (post["text"] or "[media only]")[:80].replace("\n", " ")
    media_note = f" (+{len(media_rows)} media)" if media_rows else ""
    print(f"  saved #{post['id']}: {preview}{media_note}")
    return post_id


def scrape_channel(
    channel: str,
    *,
    limit: int | None = 50,
    download_photos: bool = True,
    database: Path | None = None,
    only_new: bool = False,
    proxy: str | None = None,
    target: str = "wemessenger",
    max_media_per_post: int = 1,
) -> list[int]:
    """
    Scrape a *public* Telegram channel via https://t.me/s/<username>
    and store posts/photos in SQLite.

    Returns list of SQLite post row ids saved in this run.
    If only_new=True, skips posts already stored (by message_id).
    `proxy` applies only to Telegram HTTP fetches (e.g. socks5://127.0.0.1:10808).
    """
    username = normalize_username(channel)
    db_path = database or (DATA_DIR / "telegram.db")
    media_dir = DATA_DIR / "media" / username
    media_dir.mkdir(parents=True, exist_ok=True)
    saved_ids: list[int] = []

    with Database(db_path) as db:
        after_id = db.get_max_message_id(username) if only_new else None

        client_kwargs: dict = {
            "headers": _headers(),
            "follow_redirects": True,
            "timeout": 30.0,
        }
        if proxy:
            client_kwargs["proxy"] = proxy
            print(f"Telegram proxy: {proxy}")

        with httpx.Client(**client_kwargs) as client:
            title, ordered = _collect_posts(
                client,
                username,
                limit=limit,
                after_id=after_id,
                max_media_per_post=max_media_per_post,
            )

            mode = "new posts only" if only_new else "posts"
            since = f" (after #{after_id})" if after_id is not None else ""
            print(f"Channel: {title or username} (@{username})")
            print(f"Found {len(ordered)} {mode}{since} → {db_path}")

            if not ordered:
                print("  no new posts")
                return []

            scraped_at = datetime.now(timezone.utc).isoformat()
            channel_id = db.upsert_channel(
                username,
                title,
                f"https://t.me/s/{username}",
                scraped_at,
            )

            for post in ordered:
                if only_new and db.has_post(channel_id, post["id"]):
                    continue
                post_row_id = _save_post(
                    db,
                    client,
                    channel_id=channel_id,
                    username=username,
                    post=post,
                    media_dir=media_dir,
                    download_photos=download_photos,
                    max_media_per_post=max_media_per_post,
                    scraped_at=scraped_at,
                )
                # Persist on unpublished queue until WeMessenger send succeeds
                db.enqueue_for_publish(post_row_id, target=target)
                saved_ids.append(post_row_id)

            queue = db.count_channel_queue(target, username)
            print(
                f"Saved {len(saved_ids)} post(s); "
                f"unpublished queue={queue['unpublished']} "
                f"(pending={queue['pending']}, failed={queue['failed']}, sent={queue['sent']})\n"
            )
    return saved_ids
