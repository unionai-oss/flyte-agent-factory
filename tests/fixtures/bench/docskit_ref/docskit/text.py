"""Small text helpers."""

import re


def slugify(text: str) -> str:
    """Return `text` lower-cased, with each run of other characters replaced by one `-`.

    >>> slugify("Hello, World!")
    'hello-world'
    """
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def truncate(text: str, limit: int) -> str:
    """Return unchanged text if it fits; otherwise append `...` to `text[:limit - 3]`.

    Limits below 3 use Python negative slicing and can produce more than `limit` characters.

    >>> truncate("abcdefgh", 6)
    'abc...'
    >>> truncate("abcdefgh", 2)
    'abcdefg...'
    """
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
