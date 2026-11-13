"""Reading a project report: standards diagnostics, and the HTML timeline of reports predating JSON ones."""

from __future__ import annotations

import re
import time
from typing import Any

from flyte_agent_factory import _dashboard

_HTML_ROW = re.compile(r"<tr><td><code>([^<]*)</code></td><td>([^<]*)</td><td>(.*?)</td></tr>", re.DOTALL)
_HTML_STATUS = re.compile(r"<p><b>Status:</b> (.*?)</p>", re.DOTALL)


def _html_timeline(page: str) -> list[dict[str, Any]]:
    """Return the visible timeline table, oldest first, for a report that predates the JSON timeline."""
    import html

    start = page.find("<summary>Timeline (")
    if start < 0:
        return []
    end = page.find("</table>", start)
    table = page[start : end if end >= 0 else len(page)]
    rows = [
        {"time": None, "clock": at, "icon": html.unescape(icon), "text": html.unescape(text)}
        for at, icon, text in _HTML_ROW.findall(table)
    ]
    return rows[::-1]  # the table is newest first


def phase_fields(phase: dict[str, Any]) -> dict[str, Any]:
    """Return the `phase` label and, while the author waits on a usage limit, the structured `paused`."""
    return {"phase": _dashboard.phase_label(phase), "paused": phase.get("paused")}


#: What `factory.status` carries of each decision the factory made in the owner's place.
_DECISION_KEYS = ("id", "at", "question", "decision", "reasoning", "overruled_at", "overruled_by")


def decisions(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the report's decisions (oldest first, like the timeline), each with `overruled` spelled out."""
    rows = data.get("decisions")
    # A report is read back from a page: a row that is not a record is skipped, not a failed status.
    return [
        {**{key: d[key] for key in _DECISION_KEYS if key in d}, "overruled": bool(d.get("overruled_at"))}
        for d in (rows if isinstance(rows, list) else [])
        if isinstance(d, dict)
    ]


def spent(result: dict[str, Any]) -> bool:
    """Whether `shrink` has nothing left to cut from a status that is still too large."""
    return not result["timeline"] and not result["decisions"]


def shrink(result: dict[str, Any]) -> None:
    """Halve what a too-large status carries: the timeline first (the oldest rows), then the decisions."""
    key = "timeline" if result["timeline"] else "decisions"
    # Large row requests should take a few encodes, not one per discarded row.
    result[key] = result[key][(len(result[key]) + 1) // 2 :]


def _clock(row: dict[str, Any]) -> str:
    """`HH:MM:SS` for a row: UTC from its Unix time, or the clock an older report showed (pod time)."""
    if isinstance(row.get("time"), (int, float)):
        return time.strftime("%H:%M:%S", time.gmtime(row["time"]))
    return str(row.get("clock") or "")


def _html_activity(page: str) -> str:
    import html

    found = _HTML_STATUS.search(page)
    return html.unescape(found.group(1)) if found else ""


def _standards(data: dict[str, Any]) -> dict[str, Any]:
    """Return the report's bounded standards diagnostics, or `configured: None` and a warning."""
    if "standards" not in data:
        return {"configured": None, "warning": "standards metadata unavailable (older report)"}
    standards = data["standards"]
    if (
        not isinstance(standards, dict)
        or "configured" not in standards
        or (standards["configured"] is not None and type(standards["configured"]) is not bool)
        or ("warning" in standards and not isinstance(standards["warning"], str))
    ):
        return {"configured": None, "warning": "standards metadata invalid; open the run URL"}
    # This survives even the summary size fallback, so keep only bounded diagnostic fields.
    return {
        "configured": standards["configured"],
        **({"warning": standards["warning"][:512]} if "warning" in standards else {}),
    }


def _review(data: dict[str, Any]) -> dict[str, Any]:
    """Return the supervisor's review state: findings per round, the open ones, whether it converges."""
    phase = data.get("phase") or {}
    snapshot = data.get("review") or {}
    review = {
        key: snapshot[key]
        for key in ("scores", "converging", "rulings", "judges", "no_verdict")
        if key in snapshot
    }
    if "open" in snapshot:
        review["open"] = list(snapshot["open"])[:10]
    for key in ("round", "max_rounds"):
        if key in phase:
            review[key] = phase[key]
    return review
