"""Normalize Telegram post text before forwarding."""

from __future__ import annotations

import re
import unicodedata

# Telegram @username at end of post (channel branding)
_HANDLE_LINE = re.compile(r"^@[\w\d_]{4,}$", re.UNICODE)
# Trailing decoration-only lines (💡, 🔹, ---, etc.) with no real words
_HAS_WORD = re.compile(r"[\w\u0600-\u06FF]", re.UNICODE)
_URL = re.compile(r"https?://\S+", re.IGNORECASE)


def contains_excluded_term(text: str, terms: list[str] | tuple[str, ...]) -> str | None:
    normalized_text = unicodedata.normalize("NFKC", text or "").casefold()
    for term in terms:
        normalized_term = unicodedata.normalize("NFKC", str(term)).strip().casefold()
        if normalized_term and normalized_term in normalized_text:
            return str(term)
    return None


def clean_post_text(
    text: str,
    *,
    footer_tag: str | None = None,
    remove_trailing_phrases: tuple[str, ...] | list[str] = (),
    remove_trailing_urls: bool = False,
) -> str:
    """
    Clean configured footer content and keep a channel tag on its own final line.
    """
    if not text:
        return ""

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines = [line.strip() for line in lines]
    tag = (footer_tag or "").strip()
    if tag:
        tag_pattern = re.compile(re.escape(tag), re.IGNORECASE)
        lines = [tag_pattern.sub("", line).strip() for line in lines]
    else:
        tag = ""
        while lines and _HANDLE_LINE.fullmatch(lines[-1]):
            found = lines.pop().strip()
            tag = tag or found

    while lines:
        last = lines[-1].strip()
        if not last:
            lines.pop()
            continue
        cleaned = last
        for phrase in remove_trailing_phrases:
            phrase = str(phrase).strip()
            if phrase:
                cleaned = re.sub(
                    rf"(?<!\w){re.escape(phrase)}(?!\w)",
                    "",
                    cleaned,
                    flags=re.IGNORECASE,
                )
        if remove_trailing_urls:
            cleaned = _URL.sub("", cleaned)
        cleaned = cleaned.strip(" \t|,-")
        if cleaned != last:
            if cleaned:
                lines[-1] = cleaned
            else:
                lines.pop()
            continue
        # drop emoji/symbol-only trailers after/before the handle cleanup
        if not _HAS_WORD.search(last):
            lines.pop()
            continue
        break

    result = "\n".join(line for line in lines if line).strip()
    if tag:
        return f"{result}\n{tag}" if result else tag
    return result
