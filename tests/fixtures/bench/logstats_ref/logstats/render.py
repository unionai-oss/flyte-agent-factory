"""Turn a Summary into the report both output formats print."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from logstats.aggregate import Summary


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
