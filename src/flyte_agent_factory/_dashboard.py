"""The run report as a dashboard: one status snapshot (a dict), rendered as HTML and as JSON.

`_Timeline` (agent.py) stays the event store and builds the snapshot; this module only
turns a snapshot into the page, and reads a page back. The snapshot is also embedded in
the page as JSON (`<script type="application/json" id="factory-status">`), so tools such
as `ops/operate.py overview` read data instead of scraping HTML.

Two constraints from existing readers shape the markup:

* `ops/watch.py` hashes the `<tr><td><code>` timeline rows to detect a silent run, so no
  other table may start a row that way, and the JSON must not contain a literal `<`.
* `_meter.from_report` finds `<!--meter {...}-->` comments, which stay in the cost section.
"""

from __future__ import annotations

import html
import json
import re
import time
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any, TypeVar, cast

from flyte_agent_factory import _roles

#: The status snapshot `_Timeline` builds (agent.py), which is also the JSON embedded in the page. Its
#: sections are optional and a page written by an older version lacks some, so values stay `Any`.
Snapshot = dict[str, Any]
_T = TypeVar("_T")

SCHEMA = 1
STATUS_ID = "factory-status"
#: The whole page stays under this; the oldest timeline rows are dropped to fit.
MAX_REPORT_BYTES = 500_000
#: Rounds drawn in the chart. The numbers line keeps every round.
CHART_ROUNDS = 30

PHASE_LABELS = {
    "starting": "starting",
    "building": "building",
    "checks": "checks",
    "waiting": "waiting on human",
    "done": "done",
    "released": "released (idle after passing review)",
    "failed": "failed",
}
#: Waiting projects named on the page; the count always covers all of them.
QUEUE_SHOWN = 5

#: A criterion id: `3`, `C3`, `G1`, `1.10`, `3.10a`, `P0.2` -- up to three capitals, then a
#: dotted number and an optional letter, as the contract and the acceptance files number them.
_CRITERION_ID = r"(?:[A-Z]{1,3})?\d+(?:\.\d+)*[a-z]?"
#: A session's own per-criterion note: `C3 PASS`, `C4: FAIL`, `1.2 - pass`. A bare `3 PASS` is
#: not one (`round 3 PASS`); `criterion_results` drops it.
_CRITERION_NOTE = re.compile(rf"(?<![\w.])((?:{_CRITERION_ID})|c\d+)\b\s*[:=\-–]?\s*((?i:PASS|FAIL))\b")
#: `1. text`, `- C2. text`, `**C3.** text`, `- G1 text`, `- 3.10a text` at the start of a line
#: or list item. No `#` in the prefix: `## 1. Core` is a section heading, not criterion 1.
_CRITERION_LINE = re.compile(rf"^[ \t>*_`-]*({_CRITERION_ID})[*_`]*([.):])?[*_`]*[ \t]+(.+)$")
_FINDING_LINE = re.compile(r"^[ \t]*(?:#{2,4}[ \t]+|\d+[.)][ \t]+|[-*][ \t]+\*\*)(.+)$")
_NOT_A_FINDING = re.compile(
    r"^[\W_]*(?:blocking|non-blocking|verdict|findings?|summary|what'?s good|good|design "
    r"conflicts)\b|.*\breviewer\s*$",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://[^\s<>)\]\"'`]+")


def duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h{minutes % 60:02d}m"


def clock(ts: float | None) -> str:
    return time.strftime("%H:%M UTC", time.gmtime(ts)) if ts else "?"


def _criterion_key(raw: str) -> str:
    """One spelling per criterion: `3`, `C3` and `c3` are all `C3`; other ids are kept as written."""
    return f"C{raw}" if raw.isdigit() else raw[0].upper() + raw[1:]


def parse_criteria(text: str, limit: int = 150) -> list[dict[str, str]]:
    """Return the criteria of an ACCEPTANCE.md, as `{"id": "C4", "text": ...}` or `{"id": "1.2", ...}`.

    `limit` only bounds the page: at most 140 characters a row, 150 rows are well inside
    MAX_REPORT_BYTES, and a real factory-improvement file has 65.
    """
    out: list[dict[str, str]] = []
    for line in text.splitlines():
        found = _CRITERION_LINE.match(line)
        # A bare number needs its `.`, `)` or `:`; without one `7 plain prose` would be criterion 7.
        if found and (found.group(2) or not found.group(1).isdigit()):
            out.append(
                {
                    "id": _criterion_key(found.group(1)),
                    "text": _short(re.sub(r"[*_`]", "", found.group(3)), 140),
                }
            )
        if len(out) >= limit:
            break
    return out


def criterion_results(notes: Iterable[str]) -> dict[str, str]:
    """Criterion id -> PASS/FAIL from the session's notes, the latest note winning."""
    results: dict[str, str] = {}
    for note in notes:
        for raw, verdict in _CRITERION_NOTE.findall(note):
            if not raw.isdigit():
                results[_criterion_key(raw)] = verdict.upper()
    return results


def finding_titles(report: str, limit: int = 8) -> list[str]:
    """Short titles of a reviewer's blocking findings: the headings and numbered items before NON-BLOCKING."""
    head = re.split(r"^[ \t#*_-]*NON-BLOCKING", report, maxsplit=1, flags=re.MULTILINE | re.IGNORECASE)[0]
    titles: list[str] = []
    for line in head.splitlines():
        found = _FINDING_LINE.match(line)
        if not found:
            continue
        title = re.sub(r"[*_`#]", "", found.group(1)).strip(" :-")
        if title and not _NOT_A_FINDING.match(title):
            titles.append(_short(title, 100))
        if len(titles) >= limit:
            break
    return titles


# Rows name a judge `judge-craft`; rows of older runs say `craft`. Either is keyed by the bare name.
_VERDICT_ROW = re.compile(r"^(?:judge-)?(\w+): (PASS|FAIL)$")
_NO_VERDICT_ROW = re.compile(r"^(?:judge-)?(\w+): no verdict, treating as FAIL$")
_ROUND_ROW = re.compile(r"^QA round (\d+)\b")


def latest_verdicts(timeline: Iterable[Snapshot]) -> tuple[int | None, dict[str, str]]:
    """`(round, {judge: PASS|FAIL})` of the newest QA round in a snapshot's timeline rows, oldest first.

    "🔍 QA round 3: ..." starts a round; then "✅ correctness: PASS", "❌ craft: FAIL", or
    "⚠️ craft: no verdict, treating as FAIL". `(None, {})` when no round is in the rows.
    """
    verdicts: dict[str, str] = {}
    round_ = None
    for row in timeline:
        text = str(row.get("text", ""))
        started = _ROUND_ROW.match(text) if row.get("icon") == "🔍" else None
        if started:
            verdicts, round_ = {}, int(started.group(1))
            continue
        found = _VERDICT_ROW.match(text)
        if found and row.get("icon") in ("✅", "❌"):
            verdicts[found.group(1)] = found.group(2)
        elif row.get("icon") == "⚠️" and (missing := _NO_VERDICT_ROW.match(text)):
            verdicts[missing.group(1)] = "FAIL"
    return round_, verdicts


def links_in(text: str) -> list[str]:
    return [u.rstrip(".,;:") for u in _URL.findall(text)]


def _short(text: str, limit: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _e(value: object) -> str:
    return html.escape(str(value))


def _link(url: str, label: str | None = None) -> str:
    """Return an anchor for http(s) URLs only.

    A note can contain anything, and `javascript:` must not render.
    """
    if not url.startswith(("http://", "https://")):
        return _e(label or url)
    return f'<a href="{html.escape(url, quote=True)}">{_e(label or url)}</a>'


def phase_label(phase: Snapshot) -> str:
    kind: str = phase.get("kind", "starting")
    if kind == "review":
        total = phase.get("max_rounds")
        return f"review round {phase.get('round', '?')}" + (f" of {total}" if total else "")
    if kind == "limited":
        until = phase.get("limited_until")
        return f"limited until {clock(until)}" if until else "limited (reset time not shown)"
    return PHASE_LABELS.get(kind, kind)


# --------------------------------------------------------------------------- #
# Machine-readable twin
# --------------------------------------------------------------------------- #


# The gate helper checks paths against the live workspace, which is unavailable for historical
# reports. Match citations lexically here, using the supervisor's evidence format.
LABEL_PATH = re.compile(r"([\w./-]+\.[\w]+)(?::(\d+))?")
# The page cap counts JSON-escaped bytes (`<` and non-ASCII become 6-12 bytes each), so the gate
# diagnostics carried in a report are cut to what a label needs: rule, status, one citation and a
# first line. 100 entries at these bounds stay near 300 KB even when every character is an
# astral code point, leaving room for the judge labels under _dashboard.MAX_REPORT_BYTES.
STANDARDS_ENTRIES = 100


def compact_standards_results(rows: Iterable[object]) -> list[Snapshot]:
    """Bound raw gate results for the report: rule, status, and the first citation plus first line."""
    compact: list[Snapshot] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        output = str(row.get("output", ""))
        first = output.splitlines()[0] if output else ""
        cited = LABEL_PATH.search(output)
        # Keep the citation the label parser would find even when it is not on the first line.
        if cited and cited[0] not in first:
            first = f"{cited[0]} {first}"
        compact.append(
            {
                "rule": str(row.get("rule", ""))[:32],
                # Never cut a status: a shortened one is no longer a valid gate status.
                "status": str(row.get("status", ""))[:64],
                "output": first[:200],
            }
        )
        if len(compact) == STANDARDS_ENTRIES:
            break
    return compact


#: Decisions on the page, newest last, each text cut: the page cap counts JSON-escaped bytes.
DECISIONS_ON_PAGE = 20
_DECISION_FIELD_MAX = {"question": 300, "decision": 600, "reasoning": 800, "overruled_by": 300}


def compact_decisions(rows: Iterable[object]) -> list[Snapshot]:
    """Bound the decisions the factory made in the owner's place for the report (the newest are kept)."""
    compact: list[Snapshot] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        at = row.get("at")
        item: Snapshot = {
            "id": str(row.get("id", ""))[:16],
            "at": at if isinstance(at, (int, float)) else None,
        }
        for key, limit in _DECISION_FIELD_MAX.items():
            if row.get(key):
                item[key] = str(row[key])[:limit]
        if row.get("overruled_at"):
            item["overruled_at"] = row["overruled_at"]
        compact.append(item)
    return compact[-DECISIONS_ON_PAGE:]


def status_script(data: Snapshot) -> str:
    # `<` escaped so the blob can neither close its own tag nor start a comment, and so the
    # watcher's row pattern and the meter comment pattern cannot match inside it.
    blob = json.dumps(data, separators=(",", ":"), sort_keys=True).replace("<", "\\u003c")
    return f'<script type="application/json" id="{STATUS_ID}">{blob}</script>'


_STATUS_BLOB = re.compile(rf'<script type="application/json" id="{STATUS_ID}">(.*?)</script>', re.DOTALL)


def from_report(page: str) -> Snapshot | None:
    """Return the snapshot embedded in a report, or None for a report written before this existed."""
    found = _STATUS_BLOB.search(str(page))
    if not found:
        return None
    try:
        data = json.loads(found.group(1))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

_CSS = """
.cx{--fg:#1f2328;--bg:#fff;--mute:#656d76;--line:#d0d7de;--ok:#1a7f37;--bad:#cf222e;--warn:#9a6700;--bar:#0969da;
font:14px/1.45 system-ui,sans-serif;color:var(--fg);background:var(--bg);max-width:960px;margin:0 auto;padding:8px}
@media (prefers-color-scheme:dark){html,body{background:#0d1117;color:#e6edf3}.cx{--fg:#e6edf3;--bg:#0d1117;--mute:#8d96a0;--line:#30363d;--ok:#3fb950;--bad:#f85149;--warn:#d29922;--bar:#58a6ff}}
.cx h3{margin:0 0 4px}.cx h4{margin:14px 0 4px;font-size:15px;border-bottom:1px solid var(--line)}
.cx table{border-collapse:collapse;width:100%}.cx td,.cx th{padding:2px 6px;vertical-align:top;text-align:left}
.cx td{overflow-wrap:break-word}.cx code{white-space:nowrap}.cx a{color:var(--bar)}.cx .mute{color:var(--mute)}
.cx .pill{display:inline-block;padding:1px 8px;border-radius:10px;border:1px solid var(--line);font-weight:600}
.cx .ok{color:var(--ok)}.cx .bad{color:var(--bad)}.cx .warn{color:var(--warn)}
.cx .need{border:2px solid var(--bad);border-radius:6px;padding:4px 8px;margin:6px 0}
.cx .bar{fill:var(--bar)}.cx svg{max-width:100%;height:auto}
.cx details{margin:8px 0}.cx summary{cursor:pointer;font-weight:600}
"""  # noqa: E501 -- stylesheet shipped as written; wrapping would change the served bytes


def _section(title: str, body: str) -> str:
    return f"<h4>{_e(title)}</h4>{body}"


def _kv(rows: Iterable[tuple[str, str]]) -> str:
    """Rows of already-escaped HTML. Cells are `<th>`/`<td>` with no `<code>`: see the module note."""
    return "<table>" + "".join(f"<tr><th>{_e(k)}</th><td>{v}</td></tr>" for k, v in rows) + "</table>"


def verdict(data: Snapshot) -> dict[str, str]:
    """Whether the work is good, bad or unclear, and why in one clause.

    Good: every criterion passes or the review passed. Bad: the phase failed, or a criterion fails after a
    review round has judged the work (a FAIL before any review is the session's own work in progress).
    """
    criteria = (data.get("progress") or {}).get("criteria") or []
    review = data.get("review") or {}
    failing = [c["id"] for c in criteria if c.get("status") == "FAIL"]
    if (data.get("phase") or {}).get("kind") == "failed":
        return {"word": "bad", "reason": "the run failed"}
    if review.get("passed") or review.get("passed_round"):
        return {"word": "good", "reason": "the review passed"}
    if criteria and all(c.get("status") == "PASS" for c in criteria):
        return {"word": "good", "reason": f"all {len(criteria)} criteria pass"}
    rounds = review.get("round") or len(review.get("verdicts") or [])
    if failing and rounds:
        return {"word": "bad", "reason": f"criterion {failing[0]} still fails after review round {rounds}"}
    if failing:
        return {"word": "unclear", "reason": "no review round has judged them yet"}
    return {"word": "unclear", "reason": "no result reported yet"}


def _evidence(data: Snapshot) -> str:
    criteria = (data.get("progress") or {}).get("criteria") or []
    review = data.get("review") or {}
    passed = sum(1 for c in criteria if c.get("status") == "PASS")
    failed = sum(1 for c in criteria if c.get("status") == "FAIL")
    text = (
        f"{passed} of {len(criteria)} criteria pass, {failed} failing, "
        f"{len(criteria) - passed - failed} not reported yet"
        if criteria
        else "no acceptance criteria yet"
    )
    last = (review.get("verdicts") or [None])[-1]
    if review.get("passed") or review.get("passed_round"):
        text += "; review passed"
    elif last:
        text += f"; review round {last['round']} {last['verdict']} ({last.get('blocking', 0)} blocking)"
    else:
        text += "; no review yet"
    return text


def _lead(data: Snapshot, activity: str) -> str:
    """Goal, what it is doing now, the evidence and the verdict: what the owner reads first."""
    now = activity or (data.get("working") or {}).get("note") or "nothing yet"
    call = verdict(data)
    return (
        f"<p><b>Goal:</b> {_e(data.get('goal') or '?')}<br>"
        f"<b>Now:</b> {_e(now)}<br>"
        f"<b>Evidence so far:</b> {_e(_evidence(data))}<br>"
        f"<b>Verdict:</b> {_e(call['word'])}: {_e(call['reason'])}</p>"
    )


def _status(data: Snapshot, header_html: str, activity: str, now: float) -> str:
    phase = data["phase"]
    kind = phase["kind"]
    cls = (
        "bad"
        if kind == "failed"
        else "ok"
        if kind in ("done", "released")
        else "warn"
        if kind in ("waiting", "limited")
        else ""
    )
    last = data.get("last_activity_at")
    rows = [
        (
            "phase",
            f'<span class="pill {cls}">{_e(phase_label(phase))}</span>'
            + f' <span class="mute">for {_e(duration(now - phase["since"]))}</span>',
        ),
        (
            "last activity",
            _e(f"{duration(now - last)} ago") if last else '<span class="mute">none yet</span>',
        ),
        ("project", _e(data.get("project") or "?")),
        ("model", _e(data.get("model") or "?")),
    ]
    if data.get("run_url"):
        rows.append(("run", _link(data["run_url"], data.get("run") or data["run_url"])))
    out = _kv(rows)
    out += _queue(data.get("queue") or {}, now)
    if header_html:
        out += header_html
    if activity:
        out += f"<p><b>Status:</b> {_e(activity)}</p>"
    return out


def _queue(queue: Snapshot, now: float) -> str:
    waiting = queue.get("waiting", 0)
    if not waiting:
        return ""
    lines = "".join(
        f'<li>{e["position"]}. {_e(e["name"])} <span class="mute">waiting '
        f"{_e(duration(now - e['since']))}</span></li>"
        for e in queue.get("entries", [])
    )
    return f"<p><b>{waiting} waiting for a slot</b></p><ul>{lines}</ul>"


def _needs_you(data: Snapshot, now: float) -> str:
    items = data.get("needs_you") or []
    if not items:
        return ""
    cards = []
    for q in items:
        parts = [f"<b>{_e(q.get('kind', 'question'))}</b>: {_e(q.get('text', ''))}"]
        meta = [f"open {_e(duration(now - q['opened_at']))}"] if q.get("opened_at") else []
        if q.get("timed_out"):
            meta.append(
                '<span class="warn">timed out on the session side: it carried on with its default; '
                "an answer now is delivered as late direction</span>"
            )
        elif q.get("applies_at"):
            default = q.get("default") or "the default stated in the question"
            meta.append(f"if unanswered, the session uses {_e(default)} at {_e(clock(q['applies_at']))}")
        if q.get("url"):
            meta.append(_link(q["url"], "open in the console"))
        cards.append(f'<div class="need">{parts[0]}<br><span class="mute">{" · ".join(meta)}</span></div>')
    return _section(f"Needs you ({len(items)})", "".join(cards))


def _decided(data: Snapshot) -> str:
    items = data.get("decisions") or []
    cards = []
    items = [d for d in items if isinstance(d, Mapping)] if isinstance(items, list) else []
    if not items:
        return ""
    for d in reversed(items):  # newest first, like the timeline
        overruled = (
            f' <span class="warn">overruled: {_e(d.get("overruled_by", ""))}</span>'
            if d.get("overruled_at")
            else ""
        )
        at = d.get("at")
        when = f"{_e(clock(at))} · " if isinstance(at, (int, float)) and not isinstance(at, bool) else ""
        cards.append(
            f'<div class="need"><b>{_e(d.get("id", ""))}</b> <span class="mute">{when}'
            f"{_e(d.get('question', ''))}</span>{overruled}<br>"
            f"{_e(d.get('decision', ''))}<br>"
            f'<span class="mute">why: {_e(d.get("reasoning", ""))}</span></div>'
        )
    return _section(f"Decided in the factory ({len(items)})", "".join(cards))


def _progress(data: Snapshot) -> str:
    progress = data["progress"]
    criteria = progress.get("criteria") or []
    rows = []
    marks = {"PASS": '<span class="ok">PASS</span>', "FAIL": '<span class="bad">FAIL</span>'}
    for c in criteria:
        rows.append(
            f"<tr><td>{_e(c['id'])}</td><td>{marks.get(c.get('status'), 'unverified')}</td>"
            f"<td>{_e(c['text'])}</td></tr>"
        )
    passed = sum(1 for c in criteria if c.get("status") == "PASS")
    head = f"<p>{passed} of {len(criteria)} criteria passing" if criteria else "<p>no acceptance criteria yet"
    if progress.get("pinned"):
        head += ' <span class="mute">(pinned contract)</span>'
    head += "</p>"
    checks = progress.get("checks")
    if checks:
        status = '<span class="ok">passed</span>' if checks.get("ok") else '<span class="bad">failed</span>'
        head += f"<p>latest checks: {status} in {_e(duration(checks.get('duration_s', 0)))}</p>"
    elif progress.get("checks_script"):
        head += f'<p class="mute">declared checks: {_e(progress["checks_script"])}; no harness result yet</p>'
    else:
        head += '<p class="mute">checks have not run yet</p>'
    return _section("Progress", head + (f"<table>{''.join(rows)}</table>" if rows else ""))


def _chart(scores: list[int]) -> str:
    shown = scores[-CHART_ROUNDS:]
    if not shown:
        return ""
    peak = max(shown) or 1
    w, gap, h = 16, 4, 40
    bar_rects = []
    for i, s in enumerate(shown):
        height = max(1, round(h * s / peak)) if s else 1
        bar_rects.append(
            f'<rect class="bar" x="{i * (w + gap)}" y="{h - height}" width="{w}" height="{height}">'
            f"<title>{s}</title></rect>"
        )
    bars = "".join(bar_rects)
    width = len(shown) * (w + gap)
    return (
        f'<svg role="img" aria-label="blocking findings per round" width="{width}" height="{h}" '
        f'viewBox="0 0 {width} {h}">{bars}</svg>'
    )


def _review(data: Snapshot) -> str:
    review = data["review"]
    runs = review.get("internal_runs") or {}
    subagents, delegations = runs.get("subagents", 0), runs.get("delegations", 0)
    qa = runs.get("qa_subagents", 0)
    # Only a sub-agent launched as `verify` (or its alias `qa`) is QA; a delegation is implementation work.
    # A snapshot from before the per-role count has only the two numbers.
    by_role = runs.get("by_role") or {}
    breakdown = (
        ", ".join(f"{n} {_e(role)}" for role, n in sorted(by_role.items(), key=lambda kv: (-kv[1], kv[0])))
        if by_role
        else f"{qa} QA sub-agents, {subagents - qa} other sub-agents"
    )
    internal = (
        f"<p>internal runs: {subagents + delegations} ({breakdown}, {delegations} delegations)</p>"
        if subagents or delegations
        else ""
    )
    scores = review.get("scores") or []
    if not scores and not review.get("rulings") and not review.get("open"):
        return _section("Review", internal + '<p class="mute">no formal review round yet</p>')
    numbers = " → ".join(f"R{i} {s}" for i, s in enumerate(scores, 1)) or "none"
    converging = {True: "converging", False: "not converging", None: "no verdict"}[review.get("converging")]
    out = (
        f"{_chart(scores)}<p>blocking findings per round: {_e(numbers)}<br>"
        f"planner: {_e(converging)} · arbiter rulings: {_e(review.get('rulings', 0))}</p>"
    )
    if review.get("open"):
        out += (
            "<p>open blocking findings (latest round):</p><ul>"
            + "".join(f"<li>{_e(t)}</li>" for t in review["open"])
            + "</ul>"
        )
    return _section("Review", internal + out)


def _working(data: Snapshot, now: float) -> str:
    work = data["working"]
    out = ""
    if work.get("plan"):
        out += f"<p>plan: {_e(work['plan'])}</p>"
    if work.get("note"):
        out += f"<p>last note: {_e(work['note'])}</p>"
    running = work.get("delegations") or []
    rows = "".join(
        f"<tr><td>{_e(d['id'])}</td><td>{_e(d.get('setting') or d.get('engine', ''))}</td>"
        f"<td>started {_e(clock(d['started_at']))}</td><td>{_e(duration(now - d['started_at']))}</td></tr>"
        for d in running
    )
    out += f"<p>delegations in flight: {len(running)}</p>" + (f"<table>{rows}</table>" if rows else "")
    recent = work.get("recent") or []
    if recent:
        ok, failed = '<span class="ok">ok</span>', '<span class="bad">failed</span>'
        out += (
            "<p>last completed:</p><table>"
            + "".join(
                f"<tr><td>{_e(d['id'])}</td><td>{_e(d.get('setting') or d.get('engine', ''))}</td>"
                f"<td>{ok if d.get('ok') else failed}</td>"
                f"<td>{_e(duration(d.get('elapsed_s', 0)))}</td></tr>"
                for d in recent
            )
            + "</table>"
        )
    return _section("Working on now", out or '<p class="mute">nothing recorded yet</p>')


def _tokens(t: Snapshot) -> str:
    def m(key: str) -> str:
        return f"{t.get(key, 0) / 1e6:.2f}M"

    return (
        f"{m('output_tokens')} out · {m('input_tokens')} in · {m('cache_creation_input_tokens')} "
        f"cache-write · "
        f"{m('cache_read_input_tokens')} cache-read"
    )


def _cost(data: Snapshot, meter_rows: str) -> str:
    cost = data["cost"]
    rows = []
    sub = cost.get("subagents") or {}
    if sub:
        rows.append(("sub-agents", _e(f"${sub.get('usd', 0):.2f} · {_tokens(sub.get('tokens', {}))}")))
        for name, label in (("by_role", "by role"), ("by_engine", "by engine")):
            parts = cost.get(name) or {}
            if name == "by_role":  # a report from before the rename keys these by the old purpose names
                merged: dict[str, float] = {}
                for key, usd in parts.items():
                    merged[_roles.display_role(key)] = merged.get(_roles.display_role(key), 0.0) + usd
                parts = merged
            if parts:
                rows.append(
                    (
                        label,
                        _e(
                            "; ".join(
                                f"{k} ${v:.2f}" for k, v in sorted(parts.items(), key=lambda kv: -kv[1])
                            )
                        ),
                    )
                )
    coordinator = cost.get("coordinator")
    if coordinator:
        est = coordinator.get("usd_estimate")
        rate = coordinator.get("usd_per_hour")
        rows.append(
            (
                "coordinator",
                _e(
                    _tokens(coordinator.get("tokens", {}))
                    + (f" · ≈${est:.2f} at list price" if est is not None else "")
                    + (f" · ≈${rate:.2f}/hour" if rate is not None else "")
                ),
            )
        )
    breakdown = cost.get("breakdown")
    if breakdown:  # a report from before the breakdown has none; the rows above stand alone
        for name, label in (
            ("coordinator", "breakdown: coordinator"),
            ("helpers", "breakdown: in-pod helpers"),
            ("subagents", "breakdown: sub-agents"),
            ("workers", "breakdown: workers"),
        ):
            group = breakdown.get(name) or {}
            usd = group.get("usd")
            if not group.get("measured", True):  # absent in a report from before this field: measured
                text = "not measured (the author engine reports no usage)"
            else:
                text = _tokens(group.get("tokens", {})) + (
                    f" · ≈${usd:.2f}" if usd is not None else " · no list price"
                )
            rows.append((label, _e(text)))
        coordinator = breakdown.get("coordinator") or {}
        if not coordinator.get("measured", True):
            note = "without the coordinator (not measured)"
        elif coordinator.get("usd") is None:
            note = "without the coordinator (no list price)"
        else:
            note = "with the coordinator at list price"
        rows.append(("total", _e(f"≈${cost.get('total_usd') or 0.0:.2f} {note}")))
    if not rows:
        rows.append(("spend", '<span class="mute">nothing recorded yet</span>'))
    fallbacks = cost.get("fallbacks") or []
    if fallbacks:
        counts = cost.get("fallback_counts") or Counter(fallbacks)
        rows.append(
            ("fallbacks", _e("; ".join(f"{n}× {_short(f, 100)}" for f, n in list(counts.items())[-3:])))
        )
    # The meter rows keep their hidden JSON comment; `ops/operate.py meters` reads it.
    return _section("Cost & limits", _kv(rows) + (f"<table>{meter_rows}</table>" if meter_rows else ""))


def _deliverables(data: Snapshot) -> str:
    d = data["deliverables"]
    rows = []
    if d.get("repo"):
        rows.append(("repo", _link(d["repo"]) + (f" · branch {_e(d['branch'])}" if d.get("branch") else "")))
    if d.get("commit"):
        pushed = d.get("unpushed")
        state = (
            ""
            if pushed is None
            else " (pushed)"
            if pushed == 0
            else f' <span class="warn">({pushed} unpushed)</span>'
        )
        rows.append(("last commit", _e(d["commit"]) + state))
    if d.get("pr"):
        rows.append(("pull request", _link(d["pr"])))
    for link in d.get("links") or []:
        rows.append((link.get("label") or "link", _link(link["url"])))
    return _section("Deliverables", _kv(rows) if rows else '<p class="mute">none yet</p>')


def _risks(data: Snapshot) -> str:
    risks = data.get("risks") or []
    if not risks:
        return _section("Risks", '<p class="ok">none noticed</p>')
    items = (f"{count}× {risk}" if count > 1 else risk for risk, count in Counter(risks).items())
    return _section("Risks", "<ul>" + "".join(f'<li class="warn">{_e(r)}</li>' for r in items) + "</ul>")


def _rows_table(rows: Iterable[tuple[float, str, str]]) -> str:
    return (
        "<table>"
        + "".join(
            f"<tr><td><code>{time.strftime('%H:%M:%S', time.localtime(at))}</code></td>"
            f"<td>{icon}</td><td>{_e(text)}</td></tr>"
            for at, icon, text in rows
        )
        + "</table>"
    )


def _bounded(data: _T, limit: int = 2000) -> _T:
    """Bound prose before HTML escaping and JSON duplication.

    Returns the same shape it is given, except that a tuple comes back as a list (callers pass str, dict
    and list), hence the casts.
    """
    if isinstance(data, str):
        return cast(_T, data if len(data) <= limit else data[: limit - 1] + "…")
    if isinstance(data, dict):
        return cast(_T, {_bounded(k, limit): _bounded(v, limit) for k, v in data.items()})
    if isinstance(data, (list, tuple)):
        return cast(_T, [_bounded(v, limit) for v in data])
    return data


def render(
    data: Snapshot,
    *,
    header_html: str,
    meter_rows: str,
    activity: str,
    rows: list[tuple[float, str, str]],
    routine: list[tuple[float, str, str]],
    now: float,
) -> str:
    """Return the page. Newest timeline rows first; the oldest are dropped until it fits MAX_REPORT_BYTES."""
    metrics = data.get("metrics")
    data = _bounded(data)
    if metrics is not None:
        data["metrics"] = metrics
    activity = _bounded(activity)
    header_html = header_html if len(header_html.encode()) < 20_000 else "<p>Header truncated …</p>"
    meter_rows = meter_rows if len(meter_rows.encode()) < 20_000 else "<tr><td>Meters truncated …</td></tr>"
    head = (
        f'<div class="cx"><style>{_CSS}</style><h3>Flyte Agent Factory</h3>'
        + _lead(data, activity)
        + _status(data, header_html, activity, now)
        + _needs_you(data, now)
        + _decided(data)
        + _progress(data)
        + _review(data)
        + _working(data, now)
        + _cost(data, meter_rows)
        + _deliverables(data)
        + _risks(data)
    )
    data = {**data, "activity": activity}
    keep, keep_routine = _bounded(rows), _bounded(routine)
    while True:
        # Keep the machine-readable timeline within the same budget as the visible rows.
        tail = status_script(
            {
                **data,
                "timeline": [{"time": at, "icon": icon, "text": text} for at, icon, text in keep],
            }
        )
        timeline = (
            f"<details><summary>Timeline ({len(keep)})</summary>{_rows_table(reversed(keep))}</details>"
        )
        if keep_routine:
            timeline += (
                f"<details><summary>Routine rows ({len(keep_routine)}): checkpoints, login "
                f"refreshes</summary>"
                f"{_rows_table(reversed(keep_routine))}</details>"
            )
        page = f"{head}{timeline}</div>{tail}"
        if len(page.encode()) <= MAX_REPORT_BYTES:
            return page
        if not (keep or keep_routine):
            # Escaping and JSON duplicate the text, so field limits alone cannot enforce
            # the page budget. Keep a small, valid snapshot when sections alone overflow.
            summary: Snapshot = {
                "schema": SCHEMA,
                "phase": data["phase"],
                "project": data.get("project"),
                "truncated": True,
                # Small, and what `factory.credential_link` needs: dropping it would stop a grant being given.
                "credentials": data.get("credentials") or {},
                **({"metrics": metrics} if metrics is not None else {}),
            }
            prefix = f'<div class="cx"><style>{_CSS}</style><p>Report truncated …</p></div>'
            tail = status_script(summary)
            if len((prefix + tail).encode()) > MAX_REPORT_BYTES and metrics is not None:
                # Labels and the per-call ledger are expendable before the benchmark's core history.
                summary["metrics"] = {k: v for k, v in metrics.items() if k not in {"labels", "usage"}}
                tail = status_script(summary)
            if len((prefix + tail).encode()) > MAX_REPORT_BYTES:
                tail = status_script({"schema": SCHEMA, "truncated": True})
            return prefix + tail
        # Halve rather than pop one at a time: a page far over the cap should not take 300 passes.
        keep, keep_routine = keep[(len(keep) + 1) // 2 :], keep_routine[(len(keep_routine) + 1) // 2 :]
