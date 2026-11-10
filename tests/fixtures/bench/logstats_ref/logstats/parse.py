"""Parse one Common Log Format line into a Record."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}

# host ident user [time] "request" status size, then anything (the combined format's extras).
# The status must be exactly three digits and the size digits or "-", each followed by a space or
# the end of the line. A request may hold an escaped quote, so it is matched as "not a quote or
# backslash, or a backslash and any character".
LINE_RE = re.compile(
    r"(?P<host>\S+) (?P<ident>\S+) (?P<user>\S+) "
    r"\[(?P<time>[^\]]*)\] "
    r'"(?P<request>(?:[^"\\]|\\.)*)" '
    r"(?P<status>\d{3}) (?P<size>\d+|-)"
    r"(?:\s.*)?"
)

# The bracketed timestamp, for example 10/Oct/2026:13:55:36 +0530.
TIME_RE = re.compile(r"(\d{2})/([A-Za-z]{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2}) ([+-])(\d{2})(\d{2})")


# Timezones repeat endlessly in a log, so build each offset once.
_TZ_CACHE: dict[tuple[str, int, int], timezone] = {}


class Record:
    """One parsed log line."""

    __slots__ = ("host", "path", "status", "size", "when")

    def __init__(self, host: str, path: str, status: int, size: int, when: datetime) -> None:
        """Hold the fields of one line; `size` is 0 for a `-` size."""
        self.host = host
        self.path = path
        self.status = status
        self.size = size
        self.when = when


def _zone(sign: str, hours: int, minutes: int) -> timezone:
    """Return the fixed-offset timezone for a `+hhmm` or `-hhmm` suffix."""
    key = (sign, hours, minutes)
    zone = _TZ_CACHE.get(key)
    if zone is None:
        delta = timedelta(hours=hours, minutes=minutes)
        zone = timezone(-delta if sign == "-" else delta)
        _TZ_CACHE[key] = zone
    return zone


def parse_time(text: str) -> datetime | None:
    """Return a timezone-aware datetime from `10/Oct/2026:13:55:36 +0000`, or None if it is not a real date.

    Hand-parsed because datetime.strptime costs about 25 s per million lines.
    """
    match = TIME_RE.fullmatch(text)
    if match is None:
        return None
    day, month_name, year, hour, minute, second, sign, off_h, off_m = match.groups()
    month = MONTHS.get(month_name)
    if month is None:
        return None
    try:
        return datetime(
            int(year),
            month,
            int(day),
            int(hour),
            int(minute),
            int(second),
            tzinfo=_zone(sign, int(off_h), int(off_m)),
        )
    except ValueError:
        # Out-of-range fields, such as 31/Feb or hour 25.
        return None


def parse_request(text: str) -> str | None:
    """Return the path of a `METHOD PATH PROTOCOL` request with its query string dropped, or None.

    The request field is kept as written in the log, so an escaped quote is still a backslash
    followed by a quote here.
    """
    text = text.replace('\\"', '"')
    parts = text.split(" ")
    if len(parts) != 3:
        return None
    path = parts[1]
    return path.split("?", 1)[0]


def status_class(status: int) -> str:
    """Return the class a three digit status belongs to: 2xx to 5xx, or other."""
    if 200 <= status <= 599:
        return f"{status // 100}xx"
    return "other"


def parse_line(line: str) -> Record | None:
    """Return a Record for one log line without its line ending, or None if the line is malformed."""
    match = LINE_RE.fullmatch(line)
    if match is None:
        return None
    path = parse_request(match.group("request"))
    if path is None:
        return None
    when = parse_time(match.group("time"))
    if when is None:
        return None
    size = match.group("size")
    return Record(
        host=match.group("host"),
        path=path,
        status=int(match.group("status")),
        size=0 if size == "-" else int(size),
        when=when,
    )


def strip_newline(line: str) -> str:
    r"""Return the line without a trailing \n or \r\n; a lone \r in the middle is kept."""
    if line.endswith("\n"):
        line = line[:-1]
        if line.endswith("\r"):
            line = line[:-1]
    return line
