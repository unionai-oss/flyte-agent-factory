"""Reading a project report: standards diagnostics, and the HTML timeline of reports predating JSON ones."""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Mapping
from typing import Any

from flyte_agent_factory import _dashboard, _github_mode, _profile_oss, _profile_union, _state
from flyte_agent_factory._profile import IDENTITIES_ENV, Profile

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


def github_fields(profile: Profile, env: Mapping[str, str]) -> dict[str, str]:
    """How this deploy talks to GitHub: the mode and the sentence a person reads."""
    return {
        "github_mode": _github_mode.mode(profile, env),
        "github": _github_mode.describe(profile, env),
    }


def union_fields(profile: Profile, deploy_output: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return the `[union]` display: each tier and the secret it acts as, never a key.

    A row carries the client id the latest successful deploy's outputs recorded for it
    (`deploy_output["identities"]`) when there is one.
    """
    if _profile_oss.keyless(profile):
        return {"union": []}  # no tiers and no keys on OSS
    recorded = deploy_output.get("identities") if deploy_output else None
    clients = {
        (row.get("tier"), row.get("secret")): row.get("client_id")
        for row in (recorded if isinstance(recorded, list) else [])
        if isinstance(row, dict)
    }
    return {
        "union": [
            {
                **row,
                **({"client_id": clients[key]} if (key := (row["tier"], row["secret"])) in clients else {}),
            }
            # `_union_identity.describe`, spelled out: the slot bundle carries this module (`_factory`
            # imports it) but not `_union_identity`, which only the pods that act as a tier key ship.
            for row in ({"tier": t, "secret": profile.union.key_for(t)} for t in _profile_union.TIERS)
        ]
    }


def status_fields(profile: Profile, env: Mapping[str, str]) -> dict[str, Any]:
    """`github_fields` and `union_fields`, from what the deployer passed this pod (`IDENTITIES_ENV`)."""
    try:
        recorded = json.loads(env.get(IDENTITIES_ENV) or "[]")
    except ValueError:  # status must not fail on a garbled value
        recorded = []
    return {**github_fields(profile, env), **union_fields(profile, {"identities": recorded})}


def phase_fields(phase: dict[str, Any]) -> dict[str, Any]:
    """Return the `phase` label and, while the author waits on a usage limit, the structured `paused`."""
    return {"phase": _dashboard.phase_label(phase), "paused": phase.get("paused")}


def report_checkpoint_fields(data: dict[str, Any], now: float) -> dict[str, Any]:
    """Return the run's last good checkpoint, its age now, and the last failure (`_state.CheckpointHealth`).

    The age is from now, not from the report's own time: a pod that stopped publishing keeps aging.
    """
    raw = data.get("checkpoints")
    if not isinstance(raw, dict):  # older reports, or a garbled one: status must not fail on it
        return {"checkpoints": None}
    at = raw.get("last_ok_at")
    # JSON reads NaN and Infinity as floats, and an age cannot be computed from either.
    at = at if isinstance(at, (int, float)) and math.isfinite(at) else None
    failure = raw.get("last_failure") if isinstance(raw.get("last_failure"), dict) else None
    health = _state.CheckpointHealth(
        last_ok_at=at, last_ok_kind=raw.get("last_ok_kind") or "", failure=failure
    )
    return {"checkpoints": health.report(now)}


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


def _no_verdict_rate(no_verdict: Any, judge_calls: Any) -> dict[str, dict[str, dict[str, Any]]]:
    """Return `{by_judge|by_engine: {name: {calls, no_verdict, rate}}}`; {} when a snapshot has neither count.

    A snapshot with `no_verdict` but no `judge_calls` (from before the denominator) gives each judge `calls` 0
    and `rate` None.

    Counts cover the project run so far (the timeline's counters), so they are "recent" in the sense of the
    rounds this run has done. `rate` is None where nothing was called (an old snapshot has no denominator).
    """
    rates: dict[str, dict[str, dict[str, Any]]] = {}
    if not isinstance(no_verdict, dict) and not isinstance(judge_calls, dict):
        return rates
    for key in ("by_judge", "by_engine"):
        bad = no_verdict.get(key) if isinstance(no_verdict, dict) else None
        calls_by = judge_calls.get(key) if isinstance(judge_calls, dict) else None
        bad = bad if isinstance(bad, dict) else {}
        calls_by = calls_by if isinstance(calls_by, dict) else {}
        rates[key] = {}
        for name in sorted({*bad, *calls_by}):
            calls, missing = _count(calls_by.get(name)), _count(bad.get(name))
            rates[key][name] = {
                "calls": calls,
                "no_verdict": missing,
                "rate": round(min(missing / calls, 1.0), 4) if calls else None,
            }
    return rates


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _review(data: dict[str, Any]) -> dict[str, Any]:
    """Return the supervisor's review state: findings per round, the open ones, whether it converges."""
    phase = data.get("phase") or {}
    snapshot = data.get("review") or {}
    review = {
        key: snapshot[key]
        for key in (
            "scores",
            "converging",
            "rulings",
            "judges",
            "no_verdict",
            "judge_calls",
            "followups",
            "reruns",
        )
        if key in snapshot
    }
    if rate := _no_verdict_rate(snapshot.get("no_verdict"), snapshot.get("judge_calls")):
        review["no_verdict_rate"] = rate
    if "open" in snapshot:
        review["open"] = list(snapshot["open"])[:10]
    for key in ("round", "max_rounds"):
        if key in phase:
            review[key] = phase[key]
    return review
