import json
from typing import Any

from samples import LINE  # type: ignore[import-not-found]


def at(
    path: str = "/",
    host: str = "10.0.0.1",
    status: int = 200,
    size: int | str = 10,
    when: str = "10/Oct/2026:13:55:36 +0000",
) -> str:
    return f'{host} - - [{when}] "GET {path} HTTP/1.1" {status} {size}'


def summary(run: Any, log: Any, *lines: str, extra: tuple[str, ...] = ()) -> Any:
    result = run(log(*lines), "--json", *extra)
    return json.loads(result.stdout)


def test_counts_and_bytes(run, log):
    report = summary(run, log, at(size=5), at(size=7), "garbage", "", at(status=404, size="-"))
    assert (report["lines"], report["parsed"], report["malformed"]) == (5, 3, 2)
    assert report["bytes"] == 12


def test_status_classes_include_other(run, log):
    report = summary(
        run, log, at(status=200), at(status=302), at(status=404), at(status=503), at(status=999)
    )
    assert report["status"] == {"2xx": 1, "3xx": 1, "4xx": 1, "5xx": 1, "other": 1}


def test_top_lists_break_ties_by_code_point_order(run, log):
    lines = [
        at(path="/b", host="10.0.0.9"),
        at(path="/a", host="10.0.0.10"),
        at(path="/a", host="10.0.0.9"),
    ]
    report = summary(run, log, *lines, at(path="/c", host="10.0.0.10"))
    assert [e["path"] for e in report["top_paths"]] == ["/a", "/b", "/c"]
    assert [e["host"] for e in report["top_hosts"]] == ["10.0.0.10", "10.0.0.9"]


def test_top_limits_the_lists(run, log):
    lines = [at(path=f"/p{i}") for i in range(5)]
    report = summary(run, log, *lines, extra=("--top", "2"))
    assert len(report["top_paths"]) == 2


def test_earliest_and_latest_compare_instants_across_offsets(run, log):
    report = summary(
        run,
        log,
        at(when="10/Oct/2026:12:00:00 +0200"),  # 10:00 UTC, the earliest
        at(when="10/Oct/2026:11:00:00 +0000"),
        at(when="10/Oct/2026:09:00:00 -0300"),  # 12:00 UTC, the latest
    )
    assert report["earliest"] == "2026-10-10T12:00:00+02:00"
    assert report["latest"] == "2026-10-10T09:00:00-03:00"


def test_a_timestamp_tie_keeps_the_first_seen(run, log):
    report = summary(
        run, log, at(when="10/Oct/2026:12:00:00 +0200"), at(when="10/Oct/2026:10:00:00 +0000")
    )
    assert report["earliest"] == report["latest"] == "2026-10-10T12:00:00+02:00"


def test_no_parsed_lines_leaves_the_timestamps_null(run, log):
    result = run(log("garbage"), "--json")
    report = json.loads(result.stdout)
    assert report["earliest"] is None and report["latest"] is None


def test_the_text_summary_lists_the_numbers(run, log):
    result = run(log(LINE, LINE))
    lines = result.stdout.decode().splitlines()
    assert "lines: 2" in lines and "bytes: 4652" in lines and "  2xx: 2" in lines
    assert "  2 /index.html" in lines and "  2 203.0.113.9" in lines
    assert lines[-2] == "earliest: 2026-10-10T13:55:36+00:00"
