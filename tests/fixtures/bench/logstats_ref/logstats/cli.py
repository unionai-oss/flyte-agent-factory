"""Command line tool that summarises Common Log Format access logs.

The line parser is in `parse`, the totals in `aggregate` and the two output formats in `render`;
this module holds the input handling and the command line. It streams its input, so memory grows
with the number of distinct paths and hosts, never with the number of lines.

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
import sys
from collections.abc import Iterator
from typing import TextIO

from logstats.aggregate import Summary
from logstats.parse import parse_line, status_class, strip_newline
from logstats.render import build_report, format_time, render_json, render_text

# The parsing helpers stay importable from here, where the tests and older callers find them.
__all__ = ["format_time", "main", "parse_line", "status_class", "strip_newline"]

DEFAULT_TOP = 10


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
