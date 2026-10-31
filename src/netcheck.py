"""Connectivity checks for Telegram (via proxy) vs WeMessenger (direct)."""

from __future__ import annotations

import httpx


def telegram_reachable(proxy: str | None, timeout: float = 8.0) -> bool:
    kwargs: dict = {"follow_redirects": True, "timeout": timeout}
    if proxy:
        kwargs["proxy"] = proxy
    try:
        with httpx.Client(**kwargs) as client:
            response = client.get("https://t.me/s/telegram")
            return response.status_code < 500
    except Exception:
        return False


def wemessenger_reachable(api_base: str, timeout: float = 8.0) -> bool:
    """Direct check — never uses proxy / env proxy (VPN bypass must be system-level)."""
    url = api_base.rstrip("/") + "/"
    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=timeout,
            trust_env=False,
            proxy=None,
        ) as client:
            response = client.get(url)
            # any HTTP response means the host is reachable
            return response.status_code < 600
    except Exception:
        return False
