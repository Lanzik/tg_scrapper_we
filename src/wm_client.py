"""Shared WeMessenger client builder from settings."""

from __future__ import annotations

from wemessenger import WeMessengerClient


def build_wemessenger(
    settings: dict,
    *,
    channel_node: str | None = None,
) -> WeMessengerClient:
    wm = settings["wemessenger"]
    if not wm["enabled"]:
        raise SystemExit("Set wemessenger.enabled: true in settings.yaml")
    node = channel_node or wm["channel_node"]
    if not wm["bot_token"] or not node:
        raise SystemExit(
            "wemessenger.bot_token and a WeMessenger channel node are required "
            "in settings.yaml"
        )

    proxy = settings.get("telegram_proxy")
    if not settings.get("publish_use_proxy", False):
        proxy = None

    return WeMessengerClient(
        bot_token=wm["bot_token"],
        channel_node=node,
        api_base=wm["api_base"],
        file_upload_url=wm["file_upload_url"],
        to_category=wm["to_category"],
        session_id=wm["session_id"],
        proxy=proxy,
    )
