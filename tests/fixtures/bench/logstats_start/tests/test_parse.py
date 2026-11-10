from typing import Any

import pytest
from samples import LINE  # type: ignore[import-not-found]

from logstats import cli


def parse(line: str) -> Any:
    """Parse a line the test expects to be well formed."""
    record = cli.parse_line(line)
    assert record is not None, line
    return record


def test_a_common_log_line_parses():
    record = parse(LINE)
    assert (record.host, record.path, record.status, record.size) == (
        "203.0.113.9",
        "/index.html",
        200,
        2326,
    )
    assert record.when.isoformat() == "2026-10-10T13:55:36+00:00"


def test_a_dash_size_is_zero():
    assert parse(LINE.replace("2326", "-")).size == 0


def test_the_query_string_is_dropped():
    assert parse(LINE.replace("/index.html", "/a/b?x=1&y=2")).path == "/a/b"


def test_the_combined_format_extras_are_ignored():
    assert cli.parse_line(LINE + ' "http://example.com/" "Mozilla/5.0 (X11)"') is not None


def test_hosts_may_be_ipv6_or_names():
    assert parse(LINE.replace("203.0.113.9", "2001:db8::1")).host == "2001:db8::1"
    assert parse(LINE.replace("203.0.113.9", "web-1.example.com")).host == "web-1.example.com"


def test_an_escaped_quote_stays_inside_the_request():
    record = parse(LINE.replace("/index.html", '/say\\"hi\\"'))
    assert record.path == '/say"hi"'


@pytest.mark.parametrize(
    "line",
    [
        "",
        "not a log line",
        LINE.replace('"GET /index.html HTTP/1.1"', '"-"'),
        LINE.replace('"GET /index.html HTTP/1.1"', '"GET /index.html"'),
        LINE.replace('"GET /index.html HTTP/1.1"', '"GET /a b HTTP/1.1"'),
        LINE.replace(" 200 ", " 20 "),
        LINE.replace(" 200 ", " 2000 "),
        LINE.replace("2326", "12x"),
        LINE.replace("10/Oct/2026", "31/Feb/2026"),
        LINE.replace("10/Oct/2026", "10/Foo/2026"),
        LINE.replace("13:55:36", "25:00:00"),
    ],
)
def test_malformed_lines_do_not_parse(line):
    assert cli.parse_line(line) is None


@pytest.mark.parametrize(
    "status, expected",
    [
        (200, "2xx"),
        (204, "2xx"),
        (301, "3xx"),
        (404, "4xx"),
        (599, "5xx"),
        (100, "other"),
        (600, "other"),
    ],
)
def test_status_classes(status, expected):
    assert cli.status_class(status) == expected


def test_a_timestamp_keeps_the_offset_it_was_written_with():
    record = parse(LINE.replace("+0000", "+0530"))
    assert cli.format_time(record.when) == "2026-10-10T13:55:36+05:30"
    record = parse(LINE.replace("+0000", "-0800"))
    assert cli.format_time(record.when) == "2026-10-10T13:55:36-08:00"


def test_line_endings_are_stripped():
    assert cli.strip_newline("abc\r\n") == "abc"
    assert cli.strip_newline("abc\n") == "abc"
    assert cli.strip_newline("abc") == "abc"
    assert cli.strip_newline("a\rbc\n") == "a\rbc"
