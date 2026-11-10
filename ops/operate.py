"""Operate a real project run on the cluster: launch it, watch it, answer it.

The measure is how often a human is needed per completed project, so everything
here is about making each intervention cheap and *recordable*.

    python ops/operate.py launch briefs/smoke-wordstats.md --name smoke-01
    python ops/operate.py queue                 # the slots, the launches waiting for one, the recent ones
    python ops/operate.py cancel NAME           # remove a waiting launch
    python ops/operate.py status RUN            # phase, open questions, report text
    python ops/operate.py answer RUN COND "text"   # resolve an open question
    python ops/operate.py running               # every live run, whoever started it
    python ops/operate.py meters                # usage meters of every live project, from its report
    python ops/operate.py overview [--html F]   # every live and recent (24 h) project, one row each

Deliberately absent: abort. This tool never signals a run; `cancel` only removes a launch that has not
started.

A launch never fails for capacity: it is validated, queued, and started at once when a slot is free.
Runs are started with `flyte.run(...)` on the deployed task and NOT after
`attach()`, so each is a top-level run rather than a child of the operator.
"""

from __future__ import annotations

import argparse
import html
import os
import re
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import flyte
from flyte import remote

from flyte_agent_factory import _profile

_profile.select_from_argv()  # before _factory reads the profile at import

from flyte_agent_factory import (  # noqa: E402 -- must follow select_from_argv() so the profile is chosen first
    _dashboard,
    _factory,
    _factory_base,
    _factory_meters,
    _factory_policy,
    _meter,
    _queue,
)
from flyte_agent_factory._errors import REMOTE_ERRORS  # noqa: E402 -- must follow select_from_argv() so the profile is chosen first

PROJECT = _factory.PROJECT
DOMAIN = _factory.DOMAIN
SLOTS = _factory_base.SLOTS
TASK = os.environ.get("OPERATE_TASK", SLOTS[0])
CAP = _factory_base.cap()
REPO = _factory.REPO
LIVE = _factory_base.LIVE


LAUNCH_HINT = "PYTHONPATH=src uv run python ops/operate.py launch <brief> --name <name>"


def _init() -> None:
    flyte.init_from_api_key(api_key=os.environ["_UNION_EAGER_API_KEY"], project=PROJECT, domain=DOMAIN)


_phase = _factory_base._phase
_task_name = _factory_base._task_name
live_runs = _factory.live_runs


def operated_live() -> list[remote.Run]:
    """Live runs of the project tasks we launch. The supervisor itself is not one."""
    return [r for r in live_runs() if _task_name(r) in SLOTS]


def free_slot(live: list[remote.Run], headroom: Mapping[str, float | None] | None = None) -> str | None:
    """Return a slot no live project is on, or None: OPERATE_TASK's first, else the most 5-hour headroom."""
    return _factory_meters.free_slot(live, TASK, task_name=_task_name, headroom=headroom)


def launch(args: argparse.Namespace) -> None:
    """Launch a project from `--brief`, applying any `--policy KEY=VALUE` overrides, and print the result."""
    policy: dict[str, str] = {}
    for item in getattr(args, "policy", []):
        key, sep, value = item.partition("=")
        if not sep:
            raise ValueError("--policy requires KEY=VALUE with an uppercase policy key")
        policy[key] = value
    policy = _factory_policy.normalize_policy(policy)
    brief = Path(args.brief).read_text()
    model = getattr(args, "model", "")
    # Refuse what will be refused before the watcher exists: a refused launch starts nothing.
    _factory.prepare_launch(brief, args.name, model, "", args.demo, policy)
    # Structural, not a habit: the watcher that reports open questions starts
    # *before* the run exists (run 2 sat blocked on the operator for 40 minutes),
    # so no failure part-way through a launch can leave a live run unwatched.
    # It idles harmlessly until the run appears (or, for a queued launch, until its turn comes).
    import watch  # type: ignore[import-not-found]  # ops/ is on sys.path only when run as a script

    pid = watch.ensure_running()
    print(f"watcher pid {pid}; log {watch.LOG}  (attach a Monitor to it)", flush=True)
    try:
        result = _factory.launch_project(
            brief, args.name, model, "", args.demo, policy, priority=getattr(args, "priority", "normal")
        )
    except ValueError as e:
        # A past run: under /opt/venv the client cannot rebuild the task's
        # `Dir | None` input and dies with a proto dump that reads like a launch.
        # No run is created. The project venv (`uv run`) works.
        if "No transformers could reverse" not in str(e):
            raise
        sys.exit(f"launch failed, NO run was created: {str(e).splitlines()[0]}. Use `{LAUNCH_HINT}`.")
    if "No transformers could reverse" in result.get("note", ""):
        sys.exit(f"launch queued, NO run was created: {result['note'].splitlines()[0]}. Use `{LAUNCH_HINT}`.")
    if result["status"] == "started":
        print(f"slot {result['slot']}", flush=True)
        print(result["run"], result["url"])
    else:
        print(
            f"{result['status']}: {args.name} is number {result['position']} in the queue and starts when "
            "a slot frees (`operate.py queue` shows it, `operate.py cancel` removes it)"
        )
    if result.get("note"):
        print(result["note"])


def queue(_args: argparse.Namespace) -> None:
    """Print the project slots, the waiting launches and the recent outcomes."""
    view = _factory.queue_view()
    for s in view["slots"]:
        print(f"{s['slot']}: {s['project'] or s['run']} ({s['phase'].lower()}) {s['run']}")
    print("\n".join(queue_lines(view["queue"])))
    for hold in view.get("reserve", []):
        print(hold)
    for r in view["recent"]:
        print(f"recent: {r['name']} {r['outcome']} {r['url']}".rstrip())


def _why(row: Mapping[str, Any]) -> str:
    """Return why a waiting launch was queued or is held, as a suffix for its line, or ""."""
    notes = [row[key] for key in ("reason", "waiting_for") if row.get(key)]
    return f" [{'; '.join(notes)}]" if notes else ""


def queue_lines(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """Return the waiting launches, one line each."""
    if not rows:
        return ["queue is empty"]
    return [
        f"error: {r['name']} gen {r['gen']}: {r['error']}"
        if "error" in r
        else f"{r['position']}. {r['name']} ({r['kind']}, {r['model'] or 'default model'}"
        f"{'' if r.get('priority', 'normal') == 'normal' else ', ' + r['priority'] + ' priority'}) waiting "
        f"{_dashboard.duration(r['waiting_s'])}: {r['brief_head']}{_why(r)}"
        for r in rows
    ]


def cancel(args: argparse.Namespace) -> None:
    """Cancel the waiting launch `args.name`."""
    result = _factory.cancel(args.name)
    print(f"cancelled {args.name} (gen {', '.join(map(str, result['cancelled']))})")


def _conditions(run_name: str) -> list[remote.Condition]:
    return list(remote.Condition.listall(run_name=run_name))


def status(args: argparse.Namespace) -> None:
    """Print a run's phase and its conditions, marking which are questions open for an answer."""
    run = remote.Run.get(args.run)
    print(f"{run.name}: {_phase(run)}  {run.url}")
    for c in _conditions(args.run):
        # PAUSED is a condition waiting for a signal. The direct- inbox is always open by design,
        # so only the others are questions.
        waiting = "PAUSED" in c.phase
        open_ = (
            " <- inbox, open for direction"
            if waiting and c.name.startswith("direct-")
            else (" <- OPEN, needs an answer" if waiting else "")
        )
        print(f"  condition {c.name}: {c.phase.replace('ACTION_PHASE_', '')}{open_}")
    if args.report:
        try:
            html = run.get_report() if hasattr(run, "get_report") else ""
            text = re.sub(r"<(style|script)[^>]*>.*?</\1>", " ", str(html), flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<[^>]+>", " ", text)
            print(re.sub(r"\s+", " ", text)[: args.report])
        except REMOTE_ERRORS as e:  # a missing report is printed, not fatal to the command
            print(f"(report unavailable: {type(e).__name__}: {e})")


def answer(args: argparse.Namespace) -> None:
    """Signal condition `args.condition` on a run with `args.value`, read as yes or no for a boolean."""
    cond = remote.Condition.get(args.condition, run_name=args.run)
    assert cond is not None, f"no condition {args.condition} on {args.run}"
    value: object = args.value
    if cond.expected_type is bool:
        value = args.value.lower() in ("1", "true", "yes", "approve", "approved")
    cond.signal(value)
    print(f"answered {args.condition} on {args.run}: {value!r}")


def running(_args: argparse.Namespace) -> None:
    """Print the live runs."""
    for r in live_runs():
        print(r.name, _phase(r), r.url)


def meters(_args: argparse.Namespace) -> None:
    """Each live project's latest meters, as its supervisor wrote them into the report."""
    live = operated_live()
    if not live:
        print("no live projects")
    for run in live:
        print(f"{run.name} ({_task_name(run)})")
        try:
            found = _meter.from_report(_factory_base.report_html(run))
        except REMOTE_ERRORS as e:  # one run's report failing must not hide the others; it is printed
            print(f"  (report unavailable: {type(e).__name__}: {e})")
            continue
        for meter in found:
            age = max(0, time.time() - meter.observed_at) / 60
            print(f"  {_meter.format_line(meter)}  [{meter.source}, read {age:.0f}m ago]")
        if not found:
            print("  no meter in the report yet")


#: Flyte phases after which a run is over; its report's own "done" says nothing about how it ended.
_ENDED = ("SUCCEEDED", "FAILED", "ABORTED", "TIMED_OUT")


def _legacy_status(page: str) -> dict[str, Any]:
    """Return what a report written before the dashboard shows: the status text and meters, no more."""
    status = re.search(r"<b>Status:</b> (.*?)</p>", page, flags=re.DOTALL)
    project = re.search(r"<td><b>project</b></td><td>(.*?)</td>", page)
    return {
        "legacy": True,
        "project": html.unescape(project.group(1)) if project else "",
        "status_text": html.unescape(status.group(1)) if status else "",
        "cost": {"meters": [m.to_dict() for m in _meter.from_report(page)]},
    }


def _open_questions(run: remote.Run) -> int:
    """Open questions by asking Flyte, for a legacy report that does not list them."""
    try:
        return sum(
            1 for c in _conditions(run.name) if "PAUSED" in c.phase and not c.name.startswith("direct-")
        )
    except REMOTE_ERRORS:  # one run's lookup must not hide the others
        return 0


def _row(run: remote.Run, now: float) -> dict[str, Any]:
    """One overview row. Never raises: a run whose report cannot be read still gets its row."""
    phase = _phase(run)
    try:
        page = _factory_base.report_html(run)
        data = _dashboard.from_report(page) or _legacy_status(page)
        error = ""
    except REMOTE_ERRORS as e:  # a run whose report cannot be read still gets its row, with the error
        data, error = {"legacy": True}, f"report unavailable: {type(e).__name__}"
    cost = data.get("cost") or {}
    spend = (cost.get("subagents") or {}).get("usd") or 0.0
    spend += (cost.get("coordinator") or {}).get("usd_estimate") or 0.0
    last = data.get("last_activity_at")
    questions = (
        len(data["needs_you"])
        if "needs_you" in data
        else (_open_questions(run) if phase in _factory_base.LIVE else 0)
    )
    if phase in _ENDED:
        shown = phase.lower()
    elif data.get("phase"):
        shown = _dashboard.phase_label(data["phase"])
    else:
        shown = data.get("status_text") or phase.lower()
    return {
        "name": run.name,
        "project": data.get("project") or run.name,
        "slot": _task_name(run),
        "phase": shown,
        "round": (data.get("phase") or {}).get("round")
        or len((data.get("review") or {}).get("scores") or [])
        or "",
        "needs_you": questions,
        "last_activity": f"{_dashboard.duration(now - last)} ago" if last else "",
        "spend_usd": round(spend, 2),
        "url": run.url,
        "note": error or ("older report: no dashboard data" if data.get("legacy") else ""),
        "meters": cost.get("meters") or [],
    }


def build_overview(
    runs: Sequence[remote.Run],
    now: float | None = None,
    queued: list[dict[str, Any]] | None = None,
    gardening: str = "",
    reserve: Sequence[str] = (),
) -> dict[str, Any]:
    """Return rows for `runs` (newest first), the waiting launches `queued`, and each slot's latest meter.

    `reserve` is why each account is held back by its usage reserve (`_factory_meters.reserve_holds`).
    """
    now = time.time() if now is None else now
    rows = [_row(run, now) for run in runs]
    slots: dict[str, list[Any]] = {}
    for slot in SLOTS:
        # Newest run on the slot with a reading; its report is the freshest the slot has.
        for row in rows:
            if row["slot"] == slot and row["meters"]:
                slots[slot] = row["meters"]
                break
        else:
            slots[slot] = []
    return {
        "generated_at": now,
        "rows": rows,
        "slots": slots,
        "queue": queued or [],
        "gardening": gardening,
        "reserve": list(reserve),
    }


def _meter_lines(readings: list[_meter.MeterData]) -> list[str]:
    return [_meter.format_line(_meter.Meter.from_dict(m)) for m in readings]


def overview_text(view: dict[str, Any]) -> str:
    """Return the plain-text overview table for `view`."""
    out = [
        f"{'PROJECT':<24} {'SLOT':<24} {'PHASE / ROUND':<28} {'NEEDS YOU':<14} {'ACTIVITY':<10} "
        f"{'SPEND':<10} RUN"
    ]
    rows = view["rows"]
    if not rows:
        out.append("no live or recent projects")
    for r in rows:
        flag = f"NEEDS YOU x{r['needs_you']}" if r["needs_you"] else "-"
        rnd = f" r{r['round']}" if r["round"] else ""
        out.append(
            f"{r['project']:<24} {r['slot']:<24} {r['phase'] + rnd:<28} {flag:<14} "
            f"{r['last_activity'] or '-':<10} ${r['spend_usd']:<8.2f} {r['url']}"
            + (f"  ({r['note']})" if r["note"] else "")
        )
    out.append("")
    out.append("QUEUED")
    out.extend(queue_lines(view.get("queue", [])))
    if view.get("gardening"):
        out.append(view["gardening"])
    out.extend(view.get("reserve", []))
    out.append("")
    for slot, readings in view["slots"].items():
        out.append(f"{slot}: " + ("; ".join(_meter_lines(readings)) if readings else "no meter reading"))
    return "\n".join(out)


def overview_html(view: dict[str, Any]) -> str:
    """Return the overview for `view` as an HTML page."""

    def cell(value: object, label: str) -> str:
        return f'<td data-label="{label}">{html.escape(str(value))}</td>'

    head = "".join(
        f"<th>{h}</th>"
        for h in ("project", "slot", "phase", "round", "needs you", "last activity", "spend", "run")
    )
    body = ""
    for r in view["rows"]:
        needs = (
            f'<td data-label="needs you"><b style="color:#cf222e">{r["needs_you"]}</b></td>'
            if r["needs_you"]
            else '<td data-label="needs you"></td>'
        )
        link = (
            f'<td data-label="run"><a href="{html.escape(r["url"], quote=True)}">open</a></td>'
            if r["url"].startswith("http")
            else '<td data-label="run"></td>'
        )
        body += (
            "<tr>"
            + cell(r["project"], "project")
            + cell(r["slot"], "slot")
            + cell(r["phase"], "phase")
            + cell(r["round"], "round")
            + needs
            + cell(r["last_activity"], "last activity")
            + cell(f"${r['spend_usd']:.2f}", "spend")
            + link
            + "</tr>"
        )
    slots = "".join(
        f"<li><b>{html.escape(slot)}</b>: "
        + (html.escape("; ".join(_meter_lines(readings))) if readings else "no meter reading")
        + "</li>"
        for slot, readings in view["slots"].items()
    )
    queued = "".join(
        "<tr>"
        + cell(r.get("position", "!"), "position")
        + cell(r["name"], "project")
        + cell(
            _dashboard.duration(r["waiting_s"]) if "waiting_s" in r else "",
            "waiting",
        )
        + cell(r.get("model", ""), "model")
        + cell((r.get("brief_head") or r.get("error", "")) + _why(r), "brief")
        + "</tr>"
        for r in view.get("queue", [])
    )
    queue_table = (
        "<h4>Queued</h4><table><tr>"
        + "".join(f"<th>{h}</th>" for h in ("#", "project", "waiting", "model", "brief"))
        + f"</tr>{queued}</table>"
        if queued
        else "<h4>Queued</h4><p>nothing is waiting for a slot</p>"
    )
    if view.get("gardening"):
        queue_table += f"<p>{html.escape(view['gardening'])}</p>"
    return (
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>Factory overview</title><style>body{font:14px system-ui,sans-serif;margin:12px}"
        "table{border-collapse:collapse;width:100%}td,th{padding:3px 8px;text-align:left;border-bottom:1px "
        "solid #8884}"
        "@media (max-width:600px){table,tbody,tr,td{display:block}th{display:none}"
        "td{overflow-wrap:break-word;min-width:0}td:before{content:attr(data-label) ': ';font-weight:bold}"
        "tr{margin-bottom:12px}li{overflow-wrap:break-word}}"
        "@media (prefers-color-scheme:dark){body{background:#0d1117;color:#e6edf3}a{color:#58a6ff}}</style>"
        f"<h3>Factory overview</h3><table><tr>{head}</tr>{body}</table>{queue_table}"
        f"<h4>Meters per slot</h4><ul>{slots}</ul>"
        f"<p style='color:#888'>generated "
        f"{html.escape(time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(view['generated_at'])))}</p>"
    )


def overview(args: argparse.Namespace) -> None:
    """Print the overview of live and recent projects, and write the HTML page if asked."""
    queued, gardening, reserve = [], "", []
    try:
        queued = _factory.waiting_rows(_factory_base.queue_root(), time.time())
        gardening = _factory.gardening_status()
        reserve = _factory_meters.reserve_holds()
    except REMOTE_ERRORS as e:  # the projects are still worth showing without the queue
        print(f"(queue unavailable: {type(e).__name__}: {e})")
    view = build_overview(_factory.recent_runs(), queued=queued, gardening=gardening, reserve=reserve)
    print(overview_text(view))
    if args.html:
        Path(args.html).write_text(overview_html(view))
        print(f"wrote {args.html}")


def main(argv: Sequence[str] | None = None) -> None:
    """Parse the command line and run the chosen subcommand."""
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # Also accepted after the subcommand. The profile is already selected from argv at import
    # (select_from_argv), so a subparser must neither reject the flag nor reset it to None.
    profile_arg = argparse.ArgumentParser(add_help=False)
    profile_arg.add_argument(
        "--profile",
        default=argparse.SUPPRESS,
        help="deployment profile name or path (default: $AGENT_FACTORY_PROFILE)",
    )
    p.add_argument("--profile", help="deployment profile name or path (default: $AGENT_FACTORY_PROFILE)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("launch", parents=[profile_arg])
    s.add_argument("brief")
    s.add_argument("--name", required=True)
    s.add_argument("--demo", default="", help="the demo package name, e.g. churn-report, for demo briefs")
    s.add_argument("--model", default="", help="the author (coordinator) model, e.g. opus, sonnet, haiku")
    s.add_argument(
        "--policy", action="append", default=[], help="AGENT_FACTORY policy KEY=VALUE (repeatable)"
    )
    s.add_argument(
        "--priority",
        choices=_queue.PRIORITIES,
        default="normal",
        help="low starts only when nothing else waits; expedite goes first",
    )
    s.set_defaults(fn=launch)
    s = sub.add_parser("queue", parents=[profile_arg])
    s.set_defaults(fn=queue)
    s = sub.add_parser("cancel", parents=[profile_arg])
    s.add_argument("name")
    s.set_defaults(fn=cancel)
    s = sub.add_parser("status", parents=[profile_arg])
    s.add_argument("run")
    s.add_argument("--report", type=int, default=0, help="print the first N chars of the report text")
    s.set_defaults(fn=status)
    s = sub.add_parser("answer", parents=[profile_arg])
    s.add_argument("run")
    s.add_argument("condition")
    s.add_argument("value")
    s.set_defaults(fn=answer)
    s = sub.add_parser("running", parents=[profile_arg])
    s.set_defaults(fn=running)
    s = sub.add_parser("meters", parents=[profile_arg])
    s.set_defaults(fn=meters)
    s = sub.add_parser("overview", parents=[profile_arg])
    s.add_argument("--html", metavar="FILE", help="also write the overview as a standalone HTML page")
    s.set_defaults(fn=overview)
    args = p.parse_args(argv)
    _init()
    args.fn(args)


if __name__ == "__main__":
    main()
