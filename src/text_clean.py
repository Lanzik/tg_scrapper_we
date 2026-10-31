"""Normalize Telegram post text before forwarding."""

from __future__ import annotations

import re

# Telegram @username at end of post (channel branding)
_HANDLE_LINE = re.compile(r"^@[\w\d_]{4,}$", re.UNICODE)
# Trailing decoration-only lines (💡, 🔹, ---, etc.) with no real words
_HAS_WORD = re.compile(r"[\w\u0600-\u06FF]", re.UNICODE)


def clean_post_text(text: str) -> str:
    """
    Remove channel @username footer and trailing decoration lines.

    Example end of post::
        ...
        💡
        💡
        @Khabar_Fouri
    """
    if not text:
        return ""

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    while lines:
        last = lines[-1].strip()
        if not last:
            lines.pop()
            continue
        if _HANDLE_LINE.match(last):
            lines.pop()
            continue
        # drop emoji/symbol-only trailers after/before the handle cleanup
        if not _HAS_WORD.search(last):
            lines.pop()
            continue
        break

    return "\n".join(lines).strip()
