"""Running totals over parsed log lines."""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from logstats.parse import Record, parse_line, status_class, strip_newline

STATUS_CLASSES = ("2xx", "3xx", "4xx", "5xx", "other")


class Summary:
    """Running totals over every line of every file.

    The counters grow with the number of distinct statuses, paths and hosts, never with the
    number of lines, which is what keeps memory bounded on a large log.
    """

    def __init__(self) -> None:
        """Start with every count at zero and no timestamps."""
        self.lines = 0
        self.parsed = 0
        self.malformed = 0
        self.total_bytes = 0
        self.statuses: Counter[str] = Counter()
        self.paths: Counter[str] = Counter()
        self.hosts: Counter[str] = Counter()
        self.earliest: datetime | None = None
        self.latest: datetime | None = None

    def add_line(self, line: str) -> None:
        """Count one raw line, parsing it."""
        self.lines += 1
        record = parse_line(strip_newline(line))
        if record is None:
            self.malformed += 1
            return
        self.add_record(record)

    def add_record(self, record: Record) -> None:
        """Fold one parsed record into the totals."""
        self.parsed += 1
        self.total_bytes += record.size
        self.statuses[status_class(record.status)] += 1
        self.paths[record.path] += 1
        self.hosts[record.host] += 1
        # Strict comparisons: on a tie the first one encountered stays.
        if self.earliest is None or record.when < self.earliest:
            self.earliest = record.when
        if self.latest is None or record.when > self.latest:
            self.latest = record.when

    def status_counts(self) -> dict[str, int]:
        """Return the count for every status class, zero included, in display order."""
        return {name: self.statuses.get(name, 0) for name in STATUS_CLASSES}

    def top_paths(self, limit: int) -> list[tuple[str, int]]:
        """Return the `limit` most requested paths as (path, count), ties by path ascending."""
        return top(self.paths, limit)

    def top_hosts(self, limit: int) -> list[tuple[str, int]]:
        """Return the `limit` busiest hosts as (host, count), ties by host ascending."""
        return top(self.hosts, limit)


def top(counter: Counter[str], limit: int) -> list[tuple[str, int]]:
    """Return the `limit` largest entries of a Counter, ties broken by key in code-point order."""
    ranked = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    return ranked[:limit]
