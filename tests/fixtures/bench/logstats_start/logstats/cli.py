"""Command line tool that summarises Common Log Format access logs.

Everything lives in this one module: the line parser, the aggregation, the two output formats
and the command line. It streams its input, so memory grows with the number of distinct paths
and hosts, never with the number of lines.

Text output, one `name: value` line each unless noted::

    lines, parsed, malformed, bytes
    status:        an indented `2xx` to `other` count per line
    top paths:     an indented `<count> <path>` per line, busiest first
    top hosts:     an indented `<count> <host>` per line, busiest first
    earliest, latest

`--json` prints the same content as one object with the keys `lines`, `parsed`, `malformed`,
`bytes`, `status`, `top_paths`, `top_hosts`, `earliest` and `latest`. The top lists are arrays of
`{"path": ..., "count": ...}` and `{"host": ..., "count": ...}`, and a missing timestamp is null.

Exit codes: 0 when at least one line parsed, 1 when none did (the summary is still printed),
and 2 for a usage error, a file that cannot be opened or a file that fails while being read.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import re
import sys
from collections import Counter
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any, TextIO

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

DEFAULT_TOP = 10

STATUS_CLASSES = ("2xx", "3xx", "4xx", "5xx", "other")

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

# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

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


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def format_time(when: datetime | None) -> str | None:
    """Return the ISO 8601 extended form with the offset the line was written with, or None."""
    if when is None:
        return None
    return when.isoformat()


def build_report(summary: Summary, limit: int) -> dict[str, Any]:
    """Return the summary as plain dicts and lists: the one shape both output formats print."""
    return {
        "lines": summary.lines,
        "parsed": summary.parsed,
        "malformed": summary.malformed,
        "bytes": summary.total_bytes,
        "status": summary.status_counts(),
        "top_paths": [{"path": path, "count": count} for path, count in summary.top_paths(limit)],
        "top_hosts": [{"host": host, "count": count} for host, count in summary.top_hosts(limit)],
        "earliest": format_time(summary.earliest),
        "latest": format_time(summary.latest),
    }


def render_json(report: dict[str, Any]) -> str:
    """Return the report as one JSON document ending in a newline."""
    return json.dumps(report, indent=2) + "\n"


def render_text(report: dict[str, Any]) -> str:
    """Return the report as human-readable text ending in a newline.

    One `name: value` line per scalar, then the status classes and each top list indented by
    two spaces, with the count first so the counts line up on the left.
    """
    out = [
        f"lines: {report['lines']}",
        f"parsed: {report['parsed']}",
        f"malformed: {report['malformed']}",
        f"bytes: {report['bytes']}",
        "status:",
    ]
    for name, count in report["status"].items():
        out.append(f"  {name}: {count}")
    out.append("top paths:")
    for entry in report["top_paths"]:
        out.append(f"  {entry['count']} {entry['path']}")
    out.append("top hosts:")
    for entry in report["top_hosts"]:
        out.append(f"  {entry['count']} {entry['host']}")
    out.append(f"earliest: {report['earliest'] or '-'}")
    out.append(f"latest: {report['latest'] or '-'}")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# Input
# --------------------------------------------------------------------------- #


def open_text(name: str) -> TextIO:
    r"""Open a text stream over `name`: gzip for `.gz`, stdin for `-`, plain text otherwise.

    Invalid UTF-8 is replaced, and newline="\n" keeps a lone \r from splitting a line.
    """
    if name == "-":
        return io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace", newline="\n")
    if name.endswith(".gz"):
        return gzip.open(name, "rt", encoding="utf-8", errors="replace", newline="\n")
    return open(name, encoding="utf-8", errors="replace", newline="\n")


def check_inputs(names: list[str]) -> str | None:
    """Return the first `logstats: <file>: <reason>` message for a file that cannot be opened, or None."""
    for name in names:
        if name == "-":
            continue
        try:
            with open(name, "rb"):
                pass
        except OSError as exc:
            return f"logstats: {name}: {exc.strerror or exc}"
    return None


class InputError(Exception):
    """A file that failed while it was being read, such as a truncated gzip."""

    def __init__(self, name: str, cause: BaseException) -> None:
        """Record which file failed and the exception that stopped the read."""
        super().__init__(f"{name}: {cause}")
        self.name = name
        self.cause = cause


def iter_lines(name: str) -> Iterator[str]:
    """Every raw line of one input, raising InputError if reading it fails part way."""
    try:
        with open_text(name) as stream:
            yield from stream
    except (gzip.BadGzipFile, EOFError, OSError) as exc:
        # BadGzipFile is an OSError; it is listed anyway because it is the case that matters.
        raise InputError(name, exc) from exc


def read_all(names: list[str], summary: Summary) -> None:
    """Feed every line of every file, in argument order, to `summary`."""
    for name in names:
        for line in iter_lines(name):
            summary.add_line(line)


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #


def positive_int(text: str) -> int:
    """Parse the --top value: an integer of at least 1."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid positive integer: {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer: {text!r}")
    return value


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser; its usage errors exit with status 2."""
    parser = argparse.ArgumentParser(
        prog="logstats",
        description="Summarise Common Log Format access logs.",
    )
    parser.add_argument(
        "files", nargs="+", metavar="FILE", help="a log file, .gz for gzip, - for stdin"
    )
    parser.add_argument(
        "--top", type=positive_int, default=DEFAULT_TOP, help="entries in the top lists"
    )
    parser.add_argument("--json", action="store_true", help="print one JSON object")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the tool and return its exit code: 0 parsed something, 1 parsed nothing, 2 an error."""
    args = build_parser().parse_args(argv)
    problem = check_inputs(args.files)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    summary = Summary()
    try:
        read_all(args.files, summary)
    except InputError as exc:
        print(f"logstats: {exc}", file=sys.stderr)
        return 2
    report = build_report(summary, args.top)
    text = render_json(report) if args.json else render_text(report)
    sys.stdout.write(text)
    return 0 if summary.parsed else 1
