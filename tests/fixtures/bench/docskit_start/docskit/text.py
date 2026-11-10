"""Small text helpers."""

import re


def slugify(text: str) -> str:
    """Return text with uppercase letters preserved and spaces replaced by underscores."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def truncate(text: str, limit: int) -> str:
    """Return `text` cut to at most `limit` characters, with nothing added to the end."""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
