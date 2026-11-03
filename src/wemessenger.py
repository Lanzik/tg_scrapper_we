"""Send text and photos to a WeMessenger CHANNEL via Bot API."""

from __future__ import annotations

import logging
import mimetypes
import uuid
from typing import Any

import httpx

DEFAULT_API_BASE = "https://api.wemessenger.ir"
DEFAULT_FILE_UPLOAD_URL = "https://ms-file.wemessenger.ir/upload"
TEXT_LIMIT = 4096
logger = logging.getLogger(__name__)

# Metadata returned by file server that clients need to open the file
_UPLOAD_META_KEYS = (
    "sign",
    "width",
    "height",
    "blurHash",
    "hash",
    "tempLink",
    "singVersion",
    "audioWaveform",
    "duration",
    "name",
)


class WeMessengerClient:
    def __init__(
        self,
        bot_token: str,
        channel_node: str,
        *,
        api_base: str = DEFAULT_API_BASE,
        file_upload_url: str | None = None,
        to_category: str = "CHANNEL",
        session_id: str = "*",
        proxy: str | None = None,
    ) -> None:
        self.bot_token = bot_token.strip()
        self.channel_node = channel_node.strip()
        self.api_base = api_base.rstrip("/")
        self.file_upload_url = (file_upload_url or DEFAULT_FILE_UPLOAD_URL).rstrip("/")
        self.to_category = to_category
        self.session_id = session_id or "*"
        self.proxy = proxy.strip() if isinstance(proxy, str) and proxy.strip() else None

        if not self.bot_token:
            raise ValueError("wemessenger.bot_token is required")
        if not self.channel_node:
            raise ValueError("wemessenger.channel_node is required")

    def _destination(self) -> dict[str, str]:
        return {
            "category": self.to_category,
            "node": self.channel_node,
            "session_id": self.session_id,
        }

    def _trim_text(self, text: str) -> str:
        body = (text or "").strip()
        if len(body) > TEXT_LIMIT:
            return body[: TEXT_LIMIT - 1] + "…"
        return body

    def _send(self, client: httpx.Client, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.api_base}/v2/{self.bot_token}/sendMessage"
        response = client.post(url, json=payload, timeout=60.0)
        body_text = response.text or ""
        try:
            data = response.json() if body_text else {}
        except ValueError:
            data = {"raw": body_text[:1000]}

        if not response.is_success:
            raise RuntimeError(
                f"sendMessage HTTP {response.status_code}: {body_text[:500] or data}"
            )
        return data if isinstance(data, dict) else {"raw": data}

    def get_auth_token(self, client: httpx.Client) -> str:
        url = f"{self.api_base}/v2/{self.bot_token}/getAuthToken"
        response = client.post(url, json={}, timeout=30.0)
        body_text = response.text or ""
        try:
            data = response.json() if body_text else {}
        except ValueError:
            data = {}

        if not response.is_success:
            raise RuntimeError(
                f"getAuthToken HTTP {response.status_code}: {body_text[:500]}"
            )

        jwt = str(data.get("jwtToken") or data.get("jwt_token") or "").strip()
        if not jwt:
            raise RuntimeError("getAuthToken response missing jwtToken")
        return jwt

    def upload_file(
        self,
        client: httpx.Client,
        *,
        content: bytes,
        filename: str,
        file_type: str = "image",
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        jwt = self.get_auth_token(client)
        mime = mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        response = client.post(
            self.file_upload_url,
            headers={"Authorization": jwt},
            files={"file": (filename, content, mime)},
            data={"type": file_type},
            timeout=120.0,
        )
        body_text = response.text or ""
        try:
            data = response.json() if body_text else {}
        except ValueError:
            data = {}

        if not response.is_success:
            raise RuntimeError(
                f"upload HTTP {response.status_code}: {body_text[:500]}"
            )

        file_obj = data.get("file") if isinstance(data, dict) else None
        if not isinstance(file_obj, dict):
            file_obj = data if isinstance(data, dict) else {}

        if not file_obj.get("uuid") or file_obj.get("size") is None:
            raise RuntimeError(f"upload response missing uuid/size: {data}")
        return file_obj

    def send_text(self, client: httpx.Client, text: str) -> dict[str, Any]:
        body = self._trim_text(text)
        if not body:
            raise ValueError("text is empty")

        logger.info("WeMessenger send text (%d chars)", len(body))
        return self._send(
            client,
            {
                "to": self._destination(),
                "text": {"text": body},
                "packet_id": str(uuid.uuid4()),
            },
        )

    def send_image(
        self,
        client: httpx.Client,
        *,
        content: bytes,
        filename: str,
        caption: str | None = None,
    ) -> dict[str, Any]:
        return self.send_media(
            client,
            content=content,
            filename=filename,
            media_type="image",
            caption=caption,
        )

    def send_video(
        self,
        client: httpx.Client,
        *,
        content: bytes,
        filename: str,
        caption: str | None = None,
    ) -> dict[str, Any]:
        return self.send_media(
            client,
            content=content,
            filename=filename,
            media_type="video",
            caption=caption,
        )

    def send_media(
        self,
        client: httpx.Client,
        *,
        content: bytes,
        filename: str,
        media_type: str,
        caption: str | None = None,
    ) -> dict[str, Any]:
        if media_type not in {"image", "video"}:
            raise ValueError(f"Unsupported WeMessenger media type: {media_type}")
        uploaded = self.upload_file(
            client,
            content=content,
            filename=filename,
            file_type=media_type,
        )

        file_obj: dict[str, Any] = {
            "uuid": str(uploaded["uuid"]),
            "size": str(uploaded["size"]),
            "type": str(uploaded.get("type") or media_type),
        }
        if not file_obj.get("name"):
            file_obj["name"] = filename

        for key in _UPLOAD_META_KEYS:
            value = uploaded.get(key)
            if value not in (None, "", {}):
                file_obj[key] = value

        if caption and caption.strip():
            file_obj["caption"] = self._trim_text(caption)

        logger.info(
            "WeMessenger send %s file=%s caption=%s sign=%s",
            media_type,
            filename,
            bool(file_obj.get("caption")),
            bool(file_obj.get("sign")),
        )
        return self._send(
            client,
            {
                "to": self._destination(),
                "file": file_obj,
                "packet_id": str(uuid.uuid4()),
            },
        )

    def send_post(
        self,
        *,
        text: str,
        media: list[tuple[bytes, str, str]] | None = None,
        photos: list[tuple[bytes, str]] | None = None,
    ) -> None:
        """
        Send one Telegram post to the WeMessenger channel.

        With media, the first item carries the caption; extra items follow alone.
        - Text only: send text message.
        """
        caption = (text or "").strip() or None
        if media is None:
            media = [(content, filename, "image") for content, filename in photos or []]

        with httpx.Client(
            follow_redirects=True,
            timeout=60.0,
            trust_env=False,
            proxy=self.proxy,
        ) as client:
            if media:
                first_bytes, first_name, first_type = media[0]
                self.send_media(
                    client,
                    content=first_bytes,
                    filename=first_name,
                    media_type=first_type,
                    caption=caption,
                )
                for content, filename, media_type in media[1:]:
                    self.send_media(
                        client,
                        content=content,
                        filename=filename,
                        media_type=media_type,
                        caption=None,
                    )
                return

            if caption:
                self.send_text(client, caption)
                return

            raise ValueError("post has neither text nor media")
