"""Launch policy shared by the operator CLI and factory-mcp, plus the factory token-meter task."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re
import secrets
import tempfile
import time
from collections.abc import Collection, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast

import flyte
import flyte.errors

# Registers the Dir transformer start_project needs to read slot task interfaces.
import flyte.io
from connectrpc.code import Code
from connectrpc.errors import ConnectError
from flyte import remote

from flyte_agent_factory import (
    _dashboard,
    _deploy,
    _factory_base,
    _factory_grants,
    _factory_live,
    _factory_meters,
    _factory_policy,
    _factory_recreate,
    _factory_status,
    _gardening,
    _gates,
    _git,
    _github,
    _issues,
    _meter,
    _names,
    _profile,
    _queue,
    browser,
)
from flyte_agent_factory._errors import REMOTE_ERRORS as REMOTE_ERRORS
from flyte_agent_factory._images import agent_image
from flyte_agent_factory._policy import LAUNCH_MODEL

if TYPE_CHECKING:
    # Not re-exported by `flyte.remote`, though `remote.Task.get` returns it.
    from flyte.remote._task import LazyEntity


PROJECT = os.environ.get(
    "FLYTE_MCP_PROJECT", os.environ.get("FLYTE_INTERNAL_PROJECT", _factory_base.PROFILE.backend.project)
)
DOMAIN = os.environ.get(
    "FLYTE_MCP_DOMAIN", os.environ.get("FLYTE_INTERNAL_DOMAIN", _factory_base.PROFILE.backend.domain)
)
REPO = os.environ.get("OPERATE_REPO", _factory_base.PROFILE.repo)
MAX_BRIEF = 100_000
RECENT = 10  # closed requests `queue_view` lists
TOKEN_ROTATED = "token rotated"
#: Sent to a project's `direct-` condition to end it with a checkpoint (`_inbox._STOP_WORDS`; not imported
#: here, because `_inbox` pulls in the session machinery).
STOP = "/stop"


def live_runs() -> list[remote.Run]:
    # The server filters on phase. Listing the newest runs and filtering here lost live projects
    # once other runs (every sub-agent, the deployer's 288 a day) filled the window: the cap and
    # the one-project-per-slot check passed blindly and the watcher exited "no owned runs".
    runs = remote.Run.listall(in_phase=_factory_base.SERVER_LIVE, limit=200, sort_by=("created_at", "desc"))
    # The deployer's and the reconciler's own ticks are live every five minutes and are not projects.
    return [
        r
        for r in runs
        if _factory_base._phase(r) in _factory_base.LIVE
        and _factory_base._task_name(r)
        not in {_deploy.DEPLOYER_TASK, _deploy.DEPLOY_PAIR, _deploy.RECONCILE_TASK}
    ]


def operated_live() -> list[remote.Run]:
    """Live runs of the slots, asked of the server one slot at a time.

    A slot holds at most a few live runs, so the answer never depends on how many other runs
    the project has.
    """
    found: dict[str, remote.Run] = {}
    for slot in _factory_base.SLOTS:
        runs = remote.Run.listall(
            task_name=slot,
            in_phase=_factory_base.SERVER_LIVE,
            limit=max(20, 2 * _factory_base.cap()),
            sort_by=("created_at", "desc"),
        )
        found.update(
            (r.name, r)
            for r in runs
            if _factory_base._phase(r) in _factory_base.LIVE and _factory_base._task_name(r) == slot
        )
    return list(found.values())


def owner_demand() -> bool:
    """Live projects outside the autonomous loop take priority over new bench cases."""
    return any("factory-loop" not in _factory_base.run_labels(run) for run in operated_live())


def recent_runs(hours: float = 24) -> list[remote.Run]:
    """Project runs live now or updated in the last `hours`, newest first.

    Live runs are merged in by name: a project parked on a question for days has an old
    `updated_at` and would fall out of the time window while it is the one that matters.
    """
    after = datetime.now(timezone.utc) - timedelta(hours=hours)
    runs = {r.name: r for r in operated_live()}
    for slot in _factory_base.SLOTS:
        recent = remote.Run.listall(
            task_name=slot,
            limit=20,
            sort_by=("created_at", "desc"),
            updated_at=remote.TimeFilter(after=after),
        )
        runs.update((r.name, r) for r in recent if _factory_base._task_name(r) == slot and r.name not in runs)
    return list(runs.values())


def allowed_repos() -> dict[str, str]:
    """Return the repositories a launch may target, by [repo] key; the first is the default."""
    keys = _factory_base.PROFILE.mcp.repos if _factory_base.PROFILE.mcp else ("project",)
    return {key: (REPO if key == "project" else _factory_base.PROFILE.repos[key]) for key in keys}


def _resolve_repo(repo: str, original: str = "", *, issue: str = "") -> str:
    """`repo` if it is allowed; empty means the project's original repo on a resume, else the default."""
    allowed = list(_factory_base.PROFILE.repos.values() if issue else allowed_repos().values())
    # A project or request from before a repo was renamed names its old URL; it is the same repo.
    repo, original = (
        _factory_base.PROFILE.canonical_repo(repo),
        _factory_base.PROFILE.canonical_repo(original),
    )
    if not repo:
        return original or allowed[0]
    if repo not in allowed:
        raise ValueError(f"repo {repo!r} is not allowed; the allowed repos are: {', '.join(allowed)}")
    if original and repo != original:
        raise ValueError(f"repo must stay {original!r} when a project is resumed, not {repo!r}")
    return repo


def run_project(
    task: LazyEntity,
    brief: str,
    name: str,
    model: str = "",
    demo: str = "",
    repo: str = REPO,
    policy: Mapping[str, str] | None = None,
    *,
    run_name: str | None = None,
    labels: Mapping[str, str] | None = None,
    workspace_in: flyte.io.Dir | None = None,
    branch: str = "",
    hard_pin: bool = False,
    identities: Sequence[str] = (),
) -> remote.Run:
    context: dict[str, Any] = {"mode": "remote", "project": PROJECT, "domain": DOMAIN}
    # The grant is set here and nowhere else; `normalize_policy` refuses the key, so policy cannot carry it.
    # A request file not written by `launch` could still carry the key in its policy: drop it there.
    env_vars = {
        **{k: v for k, v in (policy or {}).items() if k != _factory_policy.BROWSER_ENV},
        **({_factory_policy.BROWSER_ENV: ",".join(sorted(identities))} if identities else {}),
    }
    if env_vars:
        context["env_vars"] = env_vars
    if run_name:
        context["name"] = run_name
    if labels:
        context["labels"] = labels
    return flyte.with_runcontext(**context).run(
        task,
        brief=_factory_policy.substitute_name(brief, name),
        name=name,
        repo=repo,
        browser=True,
        demo_package=demo,
        **({"model": model} if model else {}),
        **({"workspace_in": workspace_in} if workspace_in is not None else {}),
        **({"branch": branch} if branch else {}),
        **({"hard_pin": True} if hard_pin else {}),
    )


class SlotContention(ValueError):
    """The launch lost the race for its slot and was aborted; the caller may try again.

    `run_name` is the aborted run, for the caller to record: the abort reason does not read back.
    """

    def __init__(self, message: str, run_name: str) -> None:
        super().__init__(message)
        self.run_name = run_name


def launch_on_slot(
    slot: str,
    brief: str,
    name: str,
    model: str = LAUNCH_MODEL,
    demo: str = "",
    repo: str = REPO,
    *,
    exclude_run: str = "",
    policy: Mapping[str, str] | None = None,
    engine: str = "claude",
    **run_kwargs: Any,
) -> remote.Run:
    """Start the project on `slot`, which the caller saw free; SlotContention if another run is on it too.

    A launcher only ever aborts its own new run, and does so when any other run is live on the slot: it
    cannot tell a rival launch from a project that legitimately holds the slot (a stale listing, a hand-over
    in progress), and aborting the wrong one kills real work. Two newcomers that see each other both yield;
    the dispatch that raised retries on a later sweep. `exclude_run` is a run that is still live but has
    finished its work (a project handing over in its last act): it does not count as holding the slot.

    On a shared environment other runs are the point, but the caps are not: the same second look keeps
    the oldest runs within them, for `engine`, the one this launch was admitted for.
    """
    task = remote.Task.get(slot, auto_version="latest")
    run = run_project(task, brief, name, model, demo, repo, policy, **run_kwargs)
    if _factory_base.PROFILE.shared:
        _yield_if_over_caps(run, engine, exclude_run)
        return run
    # A second look catches two launchers that saw the same free slot before either run became visible.
    others = [
        r
        for r in operated_live()
        if _factory_base._task_name(r) == slot and r.name not in (exclude_run, run.name)
    ]
    if not others:
        return run
    run.abort(reason=_factory_base.CONTENTION_REASON)
    raise SlotContention(
        f"refusing concurrent launch on {slot}: {others[0].name} already holds it; this launch was "
        "aborted; retry after checking live runs",
        run.name,
    )


def _yield_if_over_caps(run: remote.Run, engine: str, exclude_run: str) -> None:
    """Abort `run` and raise SlotContention if, counting the live runs oldest first, it is over a cap.

    Dispatchers that listed the same room before either run existed each pass `_shared_admits`. They now
    see each other, and each judges only its own run by the same order, so the newest yield and the
    oldest stay: the caps hold without any of them aborting another's run.
    """
    runs = _factory_base._oldest_first([r for r in operated_live() if r.name != exclude_run])
    if not any(r.name == run.name for r in runs):  # not listed yet: every listed run is older
        runs.append(run)
    admitted: list[str] = []
    for other in runs:
        other_engine = engine if other.name == run.name else _factory_policy._pick_run_engine(other)
        if _factory_policy._has_room(other_engine, admitted):
            admitted.append(other_engine)
        elif other.name == run.name:
            break
    else:
        return
    run.abort(reason=_factory_base.CONTENTION_REASON)
    raise SlotContention(
        f"refusing launch of {run.name}: the shared environment is at its limit; this launch was "
        "aborted; it is retried on a later sweep",
        run.name,
    )


def _started(queue: str, entry: _queue.Entry, run_name: str, url: str = "", slot: str = "") -> None:
    _queue.mark(
        queue, entry.name, entry.gen, "started", run=run_name, url=url, slot=slot, attempt=entry.attempt
    )


def _reconcile(queue: str, entry: _queue.Entry) -> remote.Run | bool:
    """Close `entry` if Flyte already has a run for it; truthy once closed: the run it started, or True.

    The markers are caches of Flyte's state: a dispatcher that created the run and died before
    writing `.started` is healed here by whoever looks next. The run is found by the request's labels,
    never by a run name. Runs that an `attempt-<n>` marker lists as aborted (we aborted them, and
    the request is to be tried again) are not the request's run. Any other aborted run is a
    decision of whoever aborted it, which is not recoverable from Flyte, so the request is closed.
    """
    retried = set(entry.yielded) | {
        _queue.marker(queue, entry.name, entry.gen, f"attempt-{n}").get("aborted_run")
        for n in range(1, entry.attempt + 1)
    }
    runs = [r for r in _factory_base._labelled_runs(entry.name, entry.gen) if r.name not in retried]
    if not runs:
        return False
    kept = _factory_base._oldest_first([r for r in runs if _factory_base._phase(r) != "ABORTED"])
    if kept:
        _started(queue, entry, kept[0].name, kept[0].url, _factory_base._task_name(kept[0]))
    else:
        _queue.mark(queue, entry.name, entry.gen, "failed", error=f"run {runs[0].name} was aborted")
    return kept[0] if kept else True


def dispatch(*, exclude_run: str = "") -> list[str]:
    """Start the waiting projects that fit on the free slots, oldest request first.

    Returns the names of the runs started.

    `exclude_run` is the caller's own run when it is handing over its slot in its last act.
    """
    if _factory_base.PROFILE.planner.admission == "planner":
        return []
    queue = _factory_base.queue_root()
    open_requests = _queue.pending(queue)
    for entry in open_requests:
        if entry.corrupt:  # it can never be dispatched, and its error row would stay forever
            _queue.mark(queue, entry.name, entry.gen, "failed", error=entry.error)
    waiting = [e for e in open_requests if e.request is not None]
    if not waiting:  # an empty sweep costs one listing
        return []
    holder = secrets.token_hex(8)
    if (other := _queue.take_lease(queue, holder)) is not None:
        print(f"[queue] dispatch skipped: the lease is held by {other}", flush=True)
        return []  # whoever has it is dispatching; the next sweep looks again
    try:
        return _dispatch_pending(queue, waiting, exclude_run)
    finally:
        _queue.release_lease(queue, holder)


def wake_planner_for_free_slot() -> bool:
    """In planner mode, wake the planner when launches wait and a slot is free; True if it was woken.

    `dispatch` admits nothing in planner mode, and a slot freed by a crash or abort has no hand-over run
    to wake the planner, so without this it would wait for the planner's hourly timer. Admission itself
    stays in the planner: this only asks it to look now.
    """
    if _factory_base.PROFILE.planner.admission != "planner" or not _factory_base.PROFILE.state.uri:
        return False
    from . import _planner_runtime

    try:
        # The listings too: dispatch has already run, and a transient read error must not fail the sweep.
        if not any(e.request is not None for e in _queue.pending(_factory_base.queue_root())):
            return False
        if len({_factory_base._task_name(r) for r in operated_live()}) >= _factory_base.cap():
            return False
        _planner_runtime.notify(_factory_base.PROFILE, os.environ[_deploy.GITHUB_KEY_ENV])
    except REMOTE_ERRORS as exc:  # the planner timer is the backstop, as for a launch
        print(f"[queue] planner wake deferred to its timer: {type(exc).__name__}: {exc}", flush=True)
        return False
    return True


def _loop_issue(request: _queue.Request) -> bool:
    # L4's generator supplies this marker inside the framed issue; no new queue format is needed.
    if not request.issue or not _factory_base.PROFILE.loop.repo:
        return False
    repo, _ = _issues.parse_issue(request.issue)
    return repo.lower() == _factory_base.PROFILE.loop.repo.lower() and bool(
        re.search(r"<!-- loop-source: [^\n]+ -->", _queue.effective_brief(request))
    )


def _ended_at(run: remote.Run) -> float | None:
    """When a terminal run ended, as a timestamp; None while it has not."""
    status = run.pb2.action.status
    if not status.HasField("end_time"):
        return None
    return cast(float, status.end_time.ToDatetime().replace(tzinfo=timezone.utc).timestamp())


def _gardening_targets() -> list[_gardening.Target] | None:
    """Return the owner's targets from the workspace's default branch, or None when they cannot be read.

    A workspace without `gardening.toml` has no targets: gardening is opt-in.
    """
    profile = _factory_base.PROFILE
    if not (profile.github.configured and _deploy.GITHUB_KEY_ENV in os.environ and profile.repo):
        return None
    app = _github.GithubApp(profile.github.app_id, os.environ[_deploy.GITHUB_KEY_ENV])
    try:
        text = app.read_file(_github.parse_repo(profile.repo), _gardening.FILE)
    except _github.GithubError as exc:
        if exc.status == 404:
            return []
        raise
    return _gardening.parse(text, profile.repos)


def gardening_decision(now: float | None = None) -> tuple[str, list[str]]:
    """How a queued gardening launch ranks now (`_gardening.priority`), read from Flyte, state and GitHub."""
    now = time.time() if now is None else now
    runs = [
        ended
        for run in _factory_base._labelled_runs(_gardening.NAME, limit=50)
        if _factory_base._phase(run) == "SUCCEEDED" and (ended := _ended_at(run)) is not None
    ]
    open_issues = 0
    if (
        _factory_base.PROFILE.github.configured
        and _deploy.GITHUB_KEY_ENV in os.environ
        and _factory_base.PROFILE.repo
    ):
        app = _github.GithubApp(_factory_base.PROFILE.github.app_id, os.environ[_deploy.GITHUB_KEY_ENV])
        open_issues = len(app.open_issues(_github.parse_repo(_factory_base.PROFILE.repo), _gardening.LABEL))
    regression = (
        _gardening.regressed(_gardening.read_counts(_factory_base.PROFILE.state.uri))
        if _factory_base.PROFILE.state.uri
        else []
    )
    # With the targets unknown, a reserved slot could go to a launch that has nothing to tend.
    try:
        targets = _gardening_targets()
    except (*REMOTE_ERRORS, _github.GithubError) as exc:
        return "low", [f"gardening targets unknown: {type(exc).__name__}: {exc}"]
    if targets is None:
        return "low", ["gardening targets unknown: GitHub or the workspace repository is not configured"]
    return _gardening.priority(
        now, runs, regression, open_issues, _factory_base.PROFILE.gardening, len(targets)
    )


def effective_priority(request: _queue.Request) -> str:
    """Return the priority `request` is admitted by: its own, except a low gardening launch may rise."""
    if request.priority != "low" or request.name != _gardening.NAME:
        return request.priority
    try:
        return gardening_decision()[0]
    except (*REMOTE_ERRORS, _github.GithubError) as exc:  # unknown is the quiet case: stay low
        print(f"[queue] gardening priority unavailable, staying low: {type(exc).__name__}: {exc}", flush=True)
        return "low"


def gardening_status() -> str:
    """Return the overview line for a waiting gardening launch, or "" when none waits."""
    if not any(e.name == _gardening.NAME for e in _queue.pending(_factory_base.queue_root())):
        return ""
    try:
        return _gardening.describe(*gardening_decision())
    except (*REMOTE_ERRORS, _github.GithubError) as exc:
        return f"gardening: priority unknown ({type(exc).__name__})"


def _dispatch_pending(queue: str, waiting: list[_queue.Entry], exclude_run: str) -> list[str]:
    waiting = [e for e in waiting if not _reconcile(queue, e)]
    waits = _factory_policy.Waits()
    # `dispatch` passed only entries with a readable request. Python's sort is stable: within one priority
    # the order stays oldest first, with loop issues last.
    levels = [effective_priority(cast(_queue.Request, e.request)) for e in waiting]
    admission = _gardening.Admission(levels, [_loop_issue(cast(_queue.Request, e.request)) for e in waiting])
    all_live = operated_live()
    live = [r for r in all_live if r.name != exclude_run]
    # `exclude_run` frees its slot for other projects only: for its own project it is still running and
    # has not published the workspace a resume starts from.
    running = _live_projects(all_live)
    kept_for: list[remote.Run] = []  # the run handing over its slot, which its resume gets before `low` work
    busy = {_factory_base._task_name(r) for r in live}
    demand = any("factory-loop" not in _factory_base.run_labels(r) for r in live)
    started: list[str] = []
    launched: dict[str, str] = {}  # engine of each run started in this sweep, by run name
    headroom: dict[str, float | None] | None = None
    for i in admission.order:
        entry = waiting[i]
        request = cast(_queue.Request, entry.request)  # `dispatch` passed only readable requests
        if levels[i] == "low" and kept_for:  # other projects may take that slot, `low` ones may not
            live, busy, kept_for = live + kept_for, _factory_policy.keep_slots(busy, kept_for), []
        if admission.held(i):
            break  # sorted last: a request that is not low is still waiting, and it keeps the slots
        if holder := running.get(entry.name):  # it starts on a later pass, once that run ended and published
            waits.hold(entry, i, admission, "project_running", holder, holders=[holder])
            if holder == exclude_run:
                kept_for = [r for r in all_live if r.name == holder]
            continue
        if _factory_base.PROFILE.shared:
            if len(live) >= _factory_base.cap():
                break
            # Past its engine's cap only this request waits; a later one on the other engine may go.
            free = (
                list(_factory_base.SLOTS) if _factory_policy._shared_admits(request, live, launched) else []
            )
            if not free:
                waits.hold(entry, i, admission, "engine_limit", _factory_policy.word_engine_limit(request))
                continue
        else:
            free = [s for s in _factory_base.SLOTS if s not in busy]
            if len(busy) >= _factory_base.cap() or not free:
                break
        if headroom is None:
            headroom = _factory_meters.slot_headroom(free)
        engine = None if _factory_base.PROFILE.shared else _factory_policy.read_engine_pin(request)
        if engine in _profile.ENGINES:  # an unknown pin is refused by `_profile._policy`
            if not _factory_policy._has_token(engine):
                waits.hold(entry, i, admission, "no_token", f"no {engine} login is configured")
                continue
            if engine == "codex":  # a Codex login is one slot's: only slots holding a credential qualify
                profile = _factory_base.PROFILE
                codex_slots = {slot.task for slot in profile.slots if profile.codex_secret(slot)}
                free = [s for s in free if s in codex_slots]
                if not free:
                    waits.hold(entry, i, admission, "no_codex_slot", "no free Codex slot")
                    continue
        loop_issue = _loop_issue(request)
        if loop_issue:
            free = [s for s in free if _factory_meters._loop_slot_budget(s, demand=demand).action == "run"]
            if not free:
                waits.hold(entry, i, admission, "loop_budget", "the loop budget allows no start")
                continue
        slot = _factory_meters.free_slot(
            sorted(set(_factory_base.SLOTS) - set(free)), task_name=lambda s: s, headroom=headroom
        )
        if slot is None:
            waits.hold_without_headroom(entry, i, admission, busy, headroom)
            continue
        if _queue.cancelled(queue, entry.name, entry.gen):
            admission.mark_unrunnable(i)  # cancelled: nothing to hold `low` for
            continue
        try:
            workspace, note = _factory_recreate._recreated_workspace(
                queue, entry
            ) or _factory_recreate._workspace_for(request)
            run = launch_on_slot(
                slot,
                _queue.effective_brief(request) + note,
                entry.name,
                request.model,
                request.demo_package,
                # Queued before a repo was renamed, it still names the old URL.
                _factory_base.PROFILE.canonical_repo(request.repo),
                exclude_run=exclude_run,
                policy=request.policy,
                identities=request.browser,
                engine=_factory_policy.pick_launch_engine(request),
                run_name=_queue.run_id(entry.name, entry.gen, entry.attempt),
                labels={
                    _factory_base.LABEL_PROJECT: entry.name,
                    _factory_base.LABEL_GEN: str(entry.gen),
                    **({"factory-issue": _issues.issue_label(request.issue)} if request.issue else {}),
                    **({"factory-loop": _issues.issue_label(request.issue)} if loop_issue else {}),
                    **(
                        {_factory_base.LABEL_ENGINE: _factory_policy.pick_launch_engine(request)}
                        if _factory_base.PROFILE.shared
                        else {}
                    ),
                },
                workspace_in=workspace,
            )
        except SlotContention as exc:
            _queue.mark(queue, entry.name, entry.gen, f"yield-{exc.run_name}")
            _queue.mark(
                queue, entry.name, entry.gen, f"attempt-{entry.attempt + 1}", aborted_run=exc.run_name
            )
            # The slot is someone else's; this launch retries on the next sweep. Launches behind it
            # may still take the other free slots.
            busy.add(slot)
            waits.add(entry, "slot_contention", f"{exc.run_name} took the slot first; retrying")
            continue
        except flyte.errors.RuntimeUserError as exc:
            _queue.mark(queue, entry.name, entry.gen, "failed", error=str(exc))
            admission.mark_unrunnable(i)  # closed: nothing to hold `low` for
            continue
        except flyte.errors.RuntimeSystemError as exc:
            print(f"[queue] {entry.name}: launch will be retried: {exc}", flush=True)
            waits.add(entry, "launch_retry", "the launch failed and will be retried")
            waits.retrying = entry.name
            break  # FIFO: nothing behind it goes first
        except (ValueError, TypeError, LookupError, AttributeError) as exc:
            # A defect in this request (or how it is read) that a retry meets again: close it so it cannot
            # wedge the queue; other errors are transient and propagate. If its run exists, it did start.
            if not (kept := _reconcile(queue, entry)):
                _queue.mark(queue, entry.name, entry.gen, "failed", error=f"{type(exc).__name__}: {exc}")
            if kept is False or kept is True:  # closed without a run of its own: it holds nothing
                admission.mark_unrunnable(i)
                continue
            run, slot = kept, _factory_base._task_name(kept) or slot  # it started: counted as below
        # A cancel that wrote its marker after our check above may have listed no run yet: it is ours to
        # abort.
        if _queue.cancelled(queue, entry.name, entry.gen):
            run.abort(reason=_factory_base.CANCEL_REASON)
            _queue.mark(queue, entry.name, entry.gen, "cancelled", aborted_run=run.name)
            admission.mark_unrunnable(i)
            continue
        keeper = _factory_recreate._keep_oldest(entry, run)
        if keeper.name != run.name:  # lost to a run of the same request; its dispatcher reports it
            _started(queue, entry, keeper.name, keeper.url, _factory_base._task_name(keeper))
            _factory_policy.retain_live_run(keeper, live, busy)
            admission.mark_unrunnable(i)
            continue
        # The dedupe took time: look once more, so only a cancel landing after this line is left to
        # `cancel` (which aborts the run it lists) and to `_queue.entries` (which reads the later marker).
        if _queue.cancelled(queue, entry.name, entry.gen):
            run.abort(reason=_factory_base.CANCEL_REASON)
            _queue.mark(queue, entry.name, entry.gen, "cancelled", aborted_run=run.name)
            admission.mark_unrunnable(i)
            continue
        _started(queue, entry, run.name, run.url, slot)
        busy.add(slot)
        live.append(run)
        launched[run.name] = _factory_policy.pick_launch_engine(request)
        demand = demand or not loop_issue
        started.append(run.name)
        admission.admitted(i)
    waits.finish(queue, waiting, admission)
    return started


def _live_projects(runs: Sequence[remote.Run]) -> dict[str, str]:
    """Return the project name of each of `runs` that the queue started, with its run name."""
    labelled = ((_factory_base.run_labels(r).get(_factory_base.LABEL_PROJECT), r.name) for r in runs)
    return {project: run for project, run in labelled if project}


def _position(queue: str, name: str, gen: int) -> int:
    readable = [e for e in _queue.pending(queue) if e.request is not None]
    return next(i for i, e in enumerate(readable, 1) if (e.name, e.gen) == (name, gen))


class LaunchPlan(NamedTuple):
    request: _queue.Request
    replaces_waiting: bool  # the name was already waiting; `request` keeps its place
    unreadable: (
        _queue.Entry | None
    )  # a waiting request that could not be read, to close once the launch is accepted


def _check_author_pin(policy: Mapping[str, str]) -> None:
    """Refuse a pin to an engine the profile has no token for, rather than run on the other one."""
    pin = _names.coordinator_engine(policy)
    if pin in _profile.ENGINES and not _factory_policy._has_token(pin):
        raise ValueError(f"AGENT_FACTORY_COORDINATOR_ENGINE pins {pin}, but this profile has no {pin} token")


def prepare_launch(
    brief: str,
    name: str,
    model: str = LAUNCH_MODEL,
    repo: str = "",
    demo: str = "",
    policy: Mapping[str, str] | None = None,
    issue: str = "",
    priority: str = "normal",
    browser: str = "",
) -> LaunchPlan:
    """Everything `launch_project` can refuse, checked without writing anything.

    The operator CLI calls it before starting its watcher, so a launch that is going to be refused
    leaves nothing behind: no watcher, no queue file.
    """
    if priority not in _queue.PRIORITIES:
        raise ValueError(f"priority must be one of {', '.join(_queue.PRIORITIES)}")
    if issue:
        _issues.parse_issue(issue)
    # An empty model means the slot task's own default (the author model), as `operate.py launch` always
    # had it.
    _factory_policy.validate(name, model or LAUNCH_MODEL)
    if demo:
        _factory_policy.validate_demo(demo)
    if len(brief) > MAX_BRIEF:
        raise ValueError(f"brief must be at most {MAX_BRIEF} characters")
    policy = _factory_policy.normalize_policy(policy or {})
    _check_author_pin(policy)
    grant = _factory_policy.parse_browser(browser)  # None: not given, so the project's own grant stands
    queue = _factory_base.queue_root()
    found = _queue.entries(queue, name)
    waiting = next((_queue.load(queue, e) for e in reversed(found) if e.pending), None)
    old = waiting.request if waiting else None
    if old:
        if issue and old.issue and issue.lower() != old.issue.lower():
            raise ValueError("a project cannot change its linked GitHub issue")
        request = dataclasses.replace(
            old,
            brief=brief,
            model=model,
            repo=_resolve_repo(
                repo, old.repo if old.kind == "resume" or old.issue else "", issue=issue or old.issue
            ),
            demo_package=demo,
            policy=policy,
            priority=priority,
            browser=old.browser if grant is None else grant,
        )
    else:
        request = _new_request(queue, found, brief, name, model, repo, demo, policy, issue, priority, grant)
    if issue:
        request = dataclasses.replace(request, issue=issue)
    if len(_queue.effective_brief(request)) > MAX_BRIEF:
        raise ValueError(f"the project's brief plus the new direction must be at most {MAX_BRIEF} characters")
    return LaunchPlan(request, bool(old), waiting if waiting and not old else None)


def launch_project(
    brief: str,
    name: str,
    model: str = LAUNCH_MODEL,
    repo: str = "",
    demo: str = "",
    policy: Mapping[str, str] | None = None,
    issue: str = "",
    priority: str = "normal",
    browser: str = "",
) -> dict[str, Any]:
    """Validate a launch, queue it and dispatch: the one way a project starts.

    A `low` priority launch takes a slot only when no other request waits; `expedite` goes first.

    Returns {status: started|queued|updated, name, gen, kind, run?, url?, slot?, position?}. A name that
    is already waiting has its request replaced and keeps its place; a name whose run ended is resumed
    from that run's workspace with `brief` appended as direction; a name with a live run is refused.

    `browser` grants the project browser identities: names, "none" for no grant, or "" to leave the grant
    as it is (a new project has none; a resume or a replaced waiting request keeps its own).
    """
    request, old, unreadable = prepare_launch(
        brief, name, model, repo, demo, policy, issue, priority, browser
    )
    queue = _factory_base.queue_root()
    if unreadable:  # close it so the new generation is the only open one
        error = f"replaced a request that was unreadable ({unreadable.error})"
        _queue.mark(queue, name, unreadable.gen, "failed", error=error)
    _queue.enqueue(queue, request)
    note = ""
    try:
        if _factory_base.PROFILE.planner.admission == "legacy":
            dispatch()
        else:
            from . import _planner_runtime

            _planner_runtime.notify(_factory_base.PROFILE, os.environ[_deploy.GITHUB_KEY_ENV])
    except REMOTE_ERRORS as exc:  # the request is queued, and the sweep dispatches it
        retry = "planner timer" if _factory_base.PROFILE.planner.admission == "planner" else "sweep"
        note = f"dispatch deferred to the {retry}: {type(exc).__name__}: {exc}"
    result: dict[str, Any] = {"name": name, "gen": request.gen, "kind": request.kind}
    closed = next(e.closed for e in _queue.entries(queue, name) if e.gen == request.gen)
    if closed == "started":
        started = _queue.marker(queue, name, request.gen, "started")
        result.update(status="started", **{k: started.get(k, "") for k in ("run", "url", "slot")})
    elif closed:
        error = _queue.marker(queue, name, request.gen, closed).get("error", closed)
        raise ValueError(f"launch of {name} did not start: {error}")
    else:
        result.update(status="updated" if old else "queued", position=_position(queue, name, request.gen))
    if note:
        result["note"] = note
    return result


def _new_request(
    queue: str,
    found: list[_queue.Entry],
    brief: str,
    name: str,
    model: str,
    repo: str,
    demo: str,
    policy: dict[str, str],
    issue: str = "",
    priority: str = "normal",
    grant: tuple[str, ...] | None = None,
) -> _queue.Request:
    """Return the next generation of `name`: a resume of its newest run if it has one, else a new project."""
    live = [
        r
        for r in _factory_base._labelled_runs(name, in_phase=_factory_base.SERVER_LIVE, limit=5)
        if _factory_base._phase(r) in _factory_base.LIVE
    ]
    if live:
        raise ValueError(
            f"project {name} is running as {live[0].name}; send direction through its direct- "
            "condition (list_conditions), or /stop it and launch again"
        )
    previous = _factory_base._labelled_runs(name, limit=_factory_recreate.RESUME_CANDIDATES)
    base, original = "", ""
    if previous:
        last = next((_queue.load(queue, e) for e in reversed(found) if e.closed == "started"), None)
        if last and last.request:
            base, original = _queue.effective_brief(last.request), last.request.repo
            if issue and last.request.issue and issue.lower() != last.request.issue.lower():
                raise ValueError("a project cannot change its linked GitHub issue")
            issue = issue or last.request.issue
            if grant is None:
                grant = last.request.browser
    return _queue.Request(
        name=name,
        gen=_queue.next_gen(found),
        kind="resume" if previous else "new",
        brief=brief,
        model=model,
        repo=_resolve_repo(repo, original, issue=issue),
        demo_package=demo,
        policy=policy,
        enqueued_at=time.time(),
        resume_from=next((r.name for r in previous if _factory_base._phase(r) == "SUCCEEDED"), ""),
        base=base,
        issue=issue,
        priority=priority,
        browser=grant or (),
    )


def _waiting_for(name: str) -> str:
    return f"waiting for {name}'s current run to finish"


def waiting_rows(queue: str, now: float, running: Collection[str] = ()) -> list[dict[str, Any]]:
    """Return the waiting launches in order. An unreadable request is an error row and holds up nothing.

    `running` names the projects with a live run: their requests wait for it (`waiting_for`). A request the
    factory queued itself says why in `reason`. Each row's `wait_reason` and `wait` (`code`, `holders`,
    `limit`, `behind`) are from the last admission pass.
    """
    rows, position = [], 0
    waits = _queue.read_waits(_factory_base.PROFILE.state.uri)
    for entry in _queue.pending(queue):
        request = entry.request
        if request is None:
            error, wait = f"unreadable request: {entry.error}", {"code": "unreadable_request"}
            rows.append(
                {"name": entry.name, "gen": entry.gen, "error": error, "wait_reason": error, "wait": wait}
            )
            continue
        position += 1
        rows.append(
            {
                "position": position,
                "name": request.name,
                "gen": request.gen,
                "kind": request.kind,
                "waiting_s": int(now - request.enqueued_at),
                "brief_head": (request.brief.strip().splitlines() or [""])[0][:120],
                "model": request.model,
                "repo": request.repo,
                "priority": request.priority,
                **({"browser": list(request.browser)} if request.browser else {}),
                **({"reason": request.reason} if request.reason else {}),
                **({"waiting_for": _waiting_for(request.name)} if request.name in running else {}),
                **_factory_policy.describe_queue_wait(request, waits, request.name in running),
            }
        )
    return rows


def queue_view(now: float | None = None) -> dict[str, Any]:
    """{slots: live projects, queue: waiting requests in order, recent: the last closed ones}."""
    queue = _factory_base.queue_root()
    now = time.time() if now is None else now
    live = operated_live()
    slots = [
        {
            "slot": _factory_base._task_name(r),
            "run": r.name,
            "project": _project_name(r),
            "phase": _factory_base._phase(r),
        }
        for r in live
    ]
    rows = waiting_rows(queue, now, _live_projects(live))
    closed = [e for e in _queue.entries(queue) if e.closed]
    recent = []
    for entry in sorted(closed, key=lambda e: e.modified, reverse=True)[:RECENT]:
        body = _queue.marker(queue, entry.name, entry.gen, entry.closed)
        recent.append(
            {
                "name": entry.name,
                "run": body.get("run", ""),
                "url": body.get("url", ""),
                "outcome": entry.closed + (f": {body['error']}" if body.get("error") else ""),
            }
        )
    view: dict[str, Any] = {"slots": slots, "queue": rows, "recent": recent}
    if holds := _factory_meters.reserve_holds(now=now):
        view["reserve"] = holds
    if line := gardening_status():
        view["gardening"] = line
    return view


_project_name = _factory_live.project_name


def read_status(name: str, rows: int = 40) -> dict[str, Any]:
    """Read a project's live or newest run report, or a specific run by id."""
    live = _factory_base._labelled_runs(name, in_phase=_factory_base.SERVER_LIVE, limit=1)
    run = next((r for r in live if _factory_base._phase(r) in _factory_base.LIVE), None)
    if run is None:
        # Runs launched before the queue's project labels can still be live for days.
        run = next((r for r in operated_live() if _project_name(r) == name), None)
    if run is None:
        run = next(iter(_factory_base._labelled_runs(name, limit=1)), None)
    if run is None:
        try:
            run = remote.Run.get(name=name)
        except ConnectError as exc:
            if exc.code != Code.NOT_FOUND:
                raise
            return {"error": "No project or run found for this name", "name": name}

    result: dict[str, Any] = {
        "name": _factory_base.run_labels(run).get(_factory_base.LABEL_PROJECT) or name,
        "run": run.name,
        "url": run.url,
    }
    if _factory_base.PROFILE.state.uri:
        queue = _factory_base.queue_root()
        # The grant is the newest request that was or will be run: a cancelled or failed one never ran.
        latest = next(
            (
                r.request
                for e in reversed(_queue.entries(queue, result["name"]))
                if e.closed in ("", "started") and (r := _queue.load(queue, e)).request
            ),
            None,
        )
        if latest and latest.browser:
            result["browser"] = list(latest.browser)
    if _factory_base.PROFILE.state.uri:  # a request of this project that waits, and why
        request = next(
            (e.request for e in _queue.pending(_factory_base.queue_root(), result["name"]) if e.request), None
        )
        if request:
            result["queued"] = {
                **({"reason": request.reason} if request.reason else {}),
                **(
                    {"waiting_for": _waiting_for(request.name)}
                    if _factory_base._phase(run) in _factory_base.LIVE
                    else {}
                ),
            }
    try:
        page = _factory_base.report_html(run)
    except REMOTE_ERRORS:  # a remote call; its error text may hold signed URLs, so it is omitted
        return {**result, "error": "Run report unavailable; open the run URL for details"}
    if not page or page == "None":
        return {**result, "error": "Run report is not available yet"}
    data = _dashboard.from_report(page)
    if data is None:
        return {**result, "error": "Run report has no dashboard snapshot (older report or not published yet)"}

    phase = data.get("phase") or {}
    review = _factory_status._review(data)
    generated = data.get("generated_at")
    # Reports from before the JSON timeline have only the HTML one.
    timeline = data.get("timeline") or _factory_status._html_timeline(page)
    verdicts_round, verdicts = _dashboard.latest_verdicts(timeline)
    if verdicts:
        review["verdicts"] = verdicts
        if verdicts_round is not None:
            review["verdicts_round"] = verdicts_round
    result.update(
        name=data.get("project") or result["name"],
        run_phase=_factory_base._phase(run),
        **_factory_status.phase_fields(phase),
        goal=data.get("goal", ""),
        verdict=data.get("verdict") or _dashboard.verdict(data),
        activity=data.get("activity") or _factory_status._html_activity(page),
        timeline=[
            {
                "icon": row.get("icon"),
                "time": row.get("time"),
                "clock": _factory_status._clock(row),
                "text": row.get("text"),
            }
            for row in (timeline[-rows:] if rows > 0 else [])
        ],
        open_questions=data.get("needs_you", []),
        decisions=_factory_status.decisions(data),
        review=review,
        pr_url=(data.get("deliverables") or {}).get("pr", ""),
        meters=(data.get("cost") or {}).get("meters", []),
        credentials=_factory_grants.reduce_to_public(data.get("credentials") or {}),
        snapshot_age_s=max(0, time.time() - generated) if generated is not None else None,
        standards=_factory_status._standards(data),
    )
    while True:
        output = json.dumps(result)
        if len(output) <= 40_000:
            return result
        if _factory_status.spent(result):
            return {
                "name": result["name"],
                "run": run.name,
                "url": run.url,
                "error": "Dashboard summary exceeds the status size limit; open the run URL",
                "standards": result["standards"],
            }
        _factory_status.shrink(result)


cancel = _factory_live.cancel


def _stop(run: remote.Run) -> bool:
    """Send `/stop` to the run's open `direct-` condition; whether there was one to send it to."""
    for condition in remote.Condition.listall(run_name=run.name):
        if condition.name.startswith("direct-") and "PAUSED" in str(condition.phase):
            condition.signal(STOP)
            return True
    return False


def _rotation_request(
    queue: str, found: list[_queue.Entry], run: remote.Run, now: float
) -> _queue.Request | None:
    """Return the resume of `run`'s project, built from the request that started it; None if unknown."""
    gen = _factory_base.run_labels(run).get(_factory_base.LABEL_GEN, "")
    started = next((_queue.load(queue, e).request for e in found if str(e.gen) == gen and e.closed), None)
    if started is None:
        return None
    return dataclasses.replace(
        started,
        gen=_queue.next_gen(found),
        kind="resume",
        brief=(
            "The factory's access token was rotated, so this project was stopped with a checkpoint and is "
            "resumed on a new pod. Carry on from where you were; nothing else has changed."
        ),
        base=_queue.effective_brief(started),
        enqueued_at=now,
        resume_from="",
        # The slot this resume waits for is the one its own project frees: it goes ahead of launches that
        # queued meanwhile, so rotating the token never costs a project its place.
        priority="expedite",
        reason=TOKEN_ROTATED,
    )


def cycle_projects() -> dict[str, Any]:
    """Cycle every live project onto a new pod, so that it mounts the current token secret.

    A mounted secret does not change inside a running pod. Each live project is sent `/stop` (the supervisor
    saves a checkpoint, publishes the workspace and ends) and one resume of it is queued with the reason
    "token rotated". Dispatch starts the resume once the old run has ended (`_dispatch_pending`). A project
    that already has a request waiting is not queued again, so a second call only repeats the stop, which a
    project that has not stopped yet still needs. A run the queue did not start has no request to resume
    from: it is reported and left running.
    """
    queue = _factory_base.queue_root()
    rows: list[dict[str, Any]] = []
    now = time.time()
    for run in operated_live():
        project = _factory_base.run_labels(run).get(_factory_base.LABEL_PROJECT, "")
        row: dict[str, Any] = {"name": project or run.name, "run": run.name}
        found = _queue.entries(queue, project) if project else []
        if any(e.pending for e in found):
            row["status"] = "already queued"
        elif (request := _rotation_request(queue, found, run, now)) is None:
            row.update(
                status="skipped",
                error="the queue did not start this run (launched before the queue); stop it and launch "
                "it again by name",
            )
            rows.append(row)
            continue
        else:
            # Before the stop: a crash between them leaves a resume that waits for the old run, and the
            # next call stops it; the other order could leave a stopped project with no resume.
            _queue.enqueue(queue, request)
            row.update(status="queued", gen=request.gen)
        if not _stop(run):
            row["error"] = "no open direct- condition to send /stop to; call again once the run has one"
        rows.append(row)
    if not rows:
        return {"status": "nothing to rotate", "message": "no live project to rotate", "projects": []}
    return {"status": "rotating", "projects": rows}


def _env_secrets(profile: _profile.Profile) -> list[str | flyte.Secret]:
    mounted: list[str | flyte.Secret] = (
        [
            flyte.Secret(key=profile.github.private_key_secret, as_env_var=_deploy.GITHUB_KEY_ENV),
            flyte.Secret(key=cast(_profile.Board, profile.board).pat_secret, as_env_var="GITHUB_BOARD_PAT"),
        ]
        if profile.planner.admission == "planner"
        else []
    )
    if profile.github.configured and profile.planner.admission != "planner":
        # Read-only use: `gardening_decision` counts the open `gardening` issues.
        mounted.append(flyte.Secret(key=profile.github.private_key_secret, as_env_var=_deploy.GITHUB_KEY_ENV))
    if profile.secrets.claude_token:
        # Only for `mounted_token_fingerprint`: admission must tell the current token's readings from a
        # swapped-out one's, and only the value can.
        mounted.append(flyte.Secret(key=profile.secrets.claude_token, as_env_var=_factory_meters.TOKEN_ENV))
    # Only for `factory.browser_link`, which asks an identity's app for a view ticket.
    mounted.extend(
        flyte.Secret(key=identity.token_secret, as_env_var=identity.token_env)
        for identity in profile.browser.identities
    )
    return mounted


def _scan_secrets(profile: _profile.Profile) -> list[str | flyte.Secret]:
    """Return the deploy keys a scan pod clones with: the default one and each `[repo_keys]` entry."""
    keys = [
        (profile.secrets.deploy_key, _git.GIT_DEPLOY_KEY_ENV_VAR),
        *((secret, _git.repo_key_env_var(key)) for key, secret in profile.repo_keys.items()),
    ]
    return [flyte.Secret(key=secret, as_env_var=var) for secret, var in keys if secret]


# The factory env mounts no deploy key (its projects' pods do), so the scan has its own environment.
# `_git.install_deploy_key` keeps one key per pod, which is why each repository is a separate action.
scan_env = flyte.TaskEnvironment(
    name="gardening-scan",
    image=agent_image(),  # the image the factory runs in, i.e. $FLYTE_INTERNAL_IMAGE there
    resources=flyte.Resources(cpu=2, memory="4Gi"),
    secrets=_scan_secrets(_factory_base.PROFILE),
    env_vars={
        **(
            {_git.REPO_KEYS_ENV_VAR: json.dumps(_factory_base.PROFILE.repo_key_vars)}
            if _factory_base.PROFILE.repo_keys
            else {}
        ),
        **_profile.pod_env(),
    },
)

env = flyte.TaskEnvironment(
    name="factory",
    image=agent_image(),
    resources=flyte.Resources(cpu=1, memory="1Gi"),
    secrets=_env_secrets(_factory_base.PROFILE),
    depends_on=[scan_env],
    env_vars={
        "FLYTE_MCP_PROJECT": PROJECT,
        "FLYTE_MCP_DOMAIN": DOMAIN,
        "OPERATE_CAP": str(_factory_base.cap()),
        **_profile.pod_env(),
    },
)


@env.task
async def read_login_meter(account: str, engine: str = "claude") -> str:
    """Read the mounted setup-token's usage; return no secret material."""
    if engine != "claude" or account.partition(" ")[0] != engine:
        raise ValueError("meter account must name the Claude setup-token")
    try:
        meter = await _meter.read_claude(
            os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip(), account, setup_token=True
        )
    except _meter.MeterError:
        # Raw exceptions may carry credentials; persist only a fixed category in run outputs.
        meter = _meter.Meter(account, "refresh-failed", time.time(), error="auth-or-meter-unavailable")
    return json.dumps(meter.to_dict())


@env.task
async def launch(
    brief: str,
    name: str,
    model: str = LAUNCH_MODEL,
    repo: str = "",
    demo_package: str = "",
    policy: str = "",
    issue: str = "",
    priority: str = "normal",
    browser: str = "",
) -> str:
    """Queue a project and start it if a slot is free. `policy` is a JSON object of KEY: value overrides.

    `priority` is normal, low (starts only when nothing else waits) or expedite (goes first). `browser`
    names the signed-in browser identities the project may use, comma-separated; "none" removes the grant a
    resume would inherit, and "" leaves it as it is (a new project has none).
    """
    # Signed intake may use every configured repo; chat remains limited by [mcp] repos.
    if repo:
        _resolve_repo(repo)
    result = await asyncio.to_thread(
        launch_project,
        brief,
        name,
        model,
        repo,
        demo_package,
        _factory_policy.parse_policy(policy),
        issue,
        priority,
        browser,
    )
    return json.dumps(result)


@env.task
async def browser_link(identity: str) -> str:
    """Return JSON {identity, url}: a one-time link (ten minutes, single use) to sign in to `identity`."""
    configured = _factory_policy.browser_identity_names(_factory_base.PROFILE)
    if identity not in configured:
        known = ", ".join(sorted(configured)) or "none configured"
        raise ValueError(f"unknown browser identity {identity!r}; the configured identities are: {known}")
    url = await asyncio.to_thread(browser.view_link_for, identity)
    return json.dumps({"identity": identity, "url": url})


@env.task
async def credential_link(name: str, credential: str, hours: float = 4.0) -> str:
    """Return JSON {project, credential, hours, url, paste_to} for sealing `credential` for project `name`.

    `url` is the owner's seal link, `paste_to` the `direct-` condition to signal the sealed text to. No
    credential passes here.
    """
    link = await asyncio.to_thread(_factory_grants.credential_link, name, credential, hours, operated_live)
    return json.dumps(link)


@env.task
async def credentials() -> str:
    """JSON list over every live project: {project, credentials: [name, kind, state, expires]}. No values."""
    return json.dumps(await asyncio.to_thread(_factory_grants.credentials, operated_live))


@env.task
async def status(name: str, rows: int = 40) -> str:
    """JSON status and the newest timeline rows from a project's run report."""
    return json.dumps(await asyncio.to_thread(read_status, name, rows))


@env.task
async def queue_list() -> str:
    """JSON of the live projects, the waiting launches in order, and the most recent closed ones."""
    return json.dumps(await asyncio.to_thread(queue_view))


@env.task
async def queue_cancel(name: str) -> str:
    """Remove a waiting launch from the queue.

    Aborts a live run of the request if a dispatcher had just created one.
    """
    return json.dumps(await asyncio.to_thread(cancel, name))


@env.task
async def rotate_token() -> str:
    """Stop every live project with a checkpoint and queue its resume, so a replaced token secret is mounted.

    Update the Flyte secret first. JSON: {status: rotating|nothing to rotate, projects: [...]}.
    """
    return json.dumps(await asyncio.to_thread(cycle_projects))


@env.task
async def set_reserve(account: str, k: float) -> str:
    """Set an account's usage reserve: hold back k percent of each window per hour left until it resets.

    `account` is claude or codex; 0 removes the reserve. The next admission uses it.
    """
    state = _factory_base.PROFILE.state.uri
    if not state:
        raise ValueError("the reserve needs [state] uri in the deployment profile")
    await asyncio.to_thread(_queue.put_reserve, state, account, k)
    return json.dumps({"account": account, "k": k})


@env.task
async def get_reserve() -> str:
    """JSON of each account's reserve constant (an account that is absent has none)."""
    state = _factory_base.PROFILE.state.uri
    return json.dumps(await asyncio.to_thread(_queue.read_reserves, state) if state else {})


@env.task(
    triggers=flyte.Trigger(
        name="queue_sweep",
        automation=flyte.FixedRate(_deploy.QUEUE_SWEEP_MINUTES),
        description="Start waiting projects on free slots, and recover launches that lost a slot race",
        auto_activate=True,
    ),
    # The queue's backstop: a missed tick costs one interval (`_deploy.QUEUE_SWEEP_MINUTES`), so it
    # may run on spot, but a tick that cannot start must not pile up behind the next ones.
    interruptible=True,
    timeout=flyte.Timeout(
        max_runtime=timedelta(minutes=4), max_queued_time=_factory_recreate.SWEEP_QUEUE_BOUND
    ),
)
async def sweep() -> str:
    """Dispatch the queue; the safety net for the event-driven dispatch at the end of every project."""
    try:
        await asyncio.to_thread(_factory_recreate.recreate_aborted)
    except REMOTE_ERRORS as exc:  # a repair; the queue must still be dispatched without it
        print(f"[queue] recreating aborted runs failed: {type(exc).__name__}: {exc}", flush=True)
    dispatched = await asyncio.to_thread(dispatch)
    await asyncio.to_thread(wake_planner_for_free_slot)
    return json.dumps({"dispatched": dispatched})


GARDENING_BRIEF = (
    "Read briefs/factory-gardening.md in this repository and carry it out. "
    "Only the targets in gardening.toml at the repository root are yours to tend."
)


SCAN_QUEUE_BOUND = timedelta(minutes=10)
SCAN_RUNTIME = timedelta(minutes=40)


def _no_scan_key(repo: str) -> str:
    """Why this pod cannot clone `repo`, or "" when it holds the key that reaches it."""
    if repo in _factory_base.PROFILE.repo_key_vars or _git._repo_identity(repo) == _git._repo_identity(
        _factory_base.PROFILE.repo
    ):
        return ""
    return "no deploy key for this repository in the profile ([repo_keys])"


@scan_env.task(timeout=flyte.Timeout(max_runtime=SCAN_RUNTIME))
async def list_targets(workspace_repo: str) -> str:
    """Return the targets in `gardening.toml` on `workspace_repo`, as JSON names."""
    if reason := _no_scan_key(workspace_repo):
        raise ValueError(f"cannot read {_gardening.FILE}: {reason}")
    with tempfile.TemporaryDirectory(prefix="gardening-targets-") as tmp:
        await _git.clone(workspace_repo, "", Path(tmp) / "repo")
        targets = _gardening.load(Path(tmp) / "repo", _factory_base.PROFILE.repos)
    return json.dumps([t.name if t.repo else f"path:{t.path}" for t in targets])


@scan_env.task(timeout=flyte.Timeout(max_runtime=SCAN_RUNTIME))
async def scan_target(repo: str) -> str:
    """Return standards gate on `repo`'s default branch: `{"counts": {rule: n}}` or `{"unscanned": why}`."""
    if reason := _no_scan_key(repo):
        return json.dumps({"unscanned": reason})
    with tempfile.TemporaryDirectory(prefix="gardening-scan-") as tmp:
        checkout = Path(tmp) / "repo"
        try:
            await _git.clone(repo, "", checkout)
        except RuntimeError as exc:
            return json.dumps({"unscanned": f"clone failed: {exc}"})
        # The target's own standards: a repository without a `standards/` directory has no rules to scan.
        standards = checkout / "standards"
        results = await asyncio.to_thread(
            _gates.standards_scan, checkout, standards if standards.is_dir() else None
        )
    return json.dumps({"counts": _gates.finding_counts(results)})


async def _spot_first(task: Any, *args: Any) -> Any:
    """`task(*args)` on spot with a bounded queue, then on-demand with the bound cleared (DESIGN.md).

    `_bench_flow` has the same helper, private to a module that brings the benchmark's environments.
    """
    bound = flyte.Timeout(max_runtime=SCAN_RUNTIME, max_queued_time=SCAN_QUEUE_BOUND)
    try:
        return await task.override(interruptible=True, timeout=bound).aio(*args)
    except flyte.errors.MaxQueuedTimeExceededError:
        return await task.override(interruptible=False, timeout=flyte.Timeout(max_runtime=SCAN_RUNTIME)).aio(
            *args
        )


async def _scan_one(target: str) -> dict[str, Any]:
    if target.startswith("path:"):
        return {"unscanned": "a workspace path, which only the gardening project can reach"}
    return cast(dict[str, Any], json.loads(await _spot_first(scan_target, target)))


async def scan_targets(names: list[str]) -> dict[str, dict[str, Any]]:
    """Each named target's scan, by name: `{"counts": ...}` or `{"unscanned": reason}`."""
    return dict(zip(names, await asyncio.gather(*(_scan_one(name) for name in names)), strict=True))


@env.task(
    triggers=flyte.Trigger(
        name="factory_gardening",
        automation=flyte.FixedRate(7 * 24 * 60),
        description="Queue the weekly low-priority gardening project (see briefs/factory-gardening.md)",
        auto_activate=True,
    ),
    interruptible=True,
    # Long enough for the scan's two tries at a queue (`_spot_first`) and the gate itself.
    timeout=flyte.Timeout(
        max_runtime=timedelta(hours=2), max_queued_time=_factory_recreate.SWEEP_QUEUE_BOUND
    ),
)
async def garden_weekly() -> str:
    """Scan the targets, then queue the gardening project at low priority.

    The scan records the standards-gate findings that `dispatch` weighs (`gardening_decision`) when it
    decides whether the launch takes a slot early.
    """
    if not _factory_base.PROFILE.repo:
        # A weekly trigger that raised would only fail every week; a skip says why and costs nothing.
        return "skip: gardening runs in the workspace repository: the profile needs one ([direction] repo)"
    # A name that is still waiting from last week has its request replaced and keeps its place; one
    # that is still running is refused, which is the right answer for a run that overran its week.
    live = await asyncio.to_thread(
        _factory_base._labelled_runs, _gardening.NAME, in_phase=_factory_base.SERVER_LIVE, limit=5
    )
    if any(_factory_base._phase(r) in _factory_base.LIVE for r in live):
        return "skip: the previous gardening run is still live"
    # Without the list there is no telling whether anything needs tending, so nothing is queued.
    try:
        names = json.loads(await _spot_first(list_targets, _factory_base.PROFILE.repo))
        if names is None:
            raise ValueError("the target list came back empty-handed")
    except (*REMOTE_ERRORS, flyte.errors.MaxQueuedTimeExceededError) as exc:
        return f"skip: cannot read {_gardening.FILE}: {exc}"
    if not names:  # an empty run would take a slot ahead of normal work for nothing
        return f"skip: no targets in {_gardening.FILE}"
    try:
        scans = await scan_targets(names)
    except (*REMOTE_ERRORS, flyte.errors.MaxQueuedTimeExceededError) as exc:
        # No scan means no new counts, not no gardening.
        print(f"[gardening] scan failed: {type(exc).__name__}: {exc}", flush=True)
        scans = {}
    unscanned = {t: scan["unscanned"] for t, scan in scans.items() if "unscanned" in scan}
    if _factory_base.PROFILE.state.uri and (
        counts := {t: scan["counts"] for t, scan in scans.items() if "counts" in scan}
    ):
        await asyncio.to_thread(_gardening.record_counts, _factory_base.PROFILE.state.uri, counts)
    result = await asyncio.to_thread(
        launch_project,
        GARDENING_BRIEF,
        _gardening.NAME,
        repo=_factory_base.PROFILE.repo,
        priority="low",
    )
    return json.dumps({**result, "unscanned": unscanned})


@env.task(
    retries=0,
    timeout=flyte.Timeout(max_runtime=timedelta(minutes=4), max_queued_time=timedelta(minutes=5)),
)
async def plan_pass() -> str:
    """Gather and admit Ready work while holding the direction repository's CAS lock."""
    from . import _admission, _board, _planner_facts, _planner_runtime

    if _factory_base.PROFILE.planner.admission != "planner":
        return json.dumps({"dispatched": []})

    run_name = flyte.ctx().action.run_name

    def run() -> list[str]:
        app, repo, base = _planner_runtime.connection(_factory_base.PROFILE)
        lock = _admission.GitRecord(app, repo, "factory/locks/planner", base)
        if not _admission.acquire_lock(lock, cast(str, run_name), lambda name: remote.Run.get(name=name)):
            return []
        board = _board.BoardClient(os.environ[_board.TOKEN_ENV])
        planner_board = cast(
            _profile.Board, _factory_base.PROFILE.board
        )  # planner admission requires [board]
        board.bootstrap(planner_board.owner, planner_board.owner_type, planner_board.number, create=False)
        rows = board.items()
        manual, requests = _planner_runtime.import_requests(app, repo, base, board, rows)
        rows = board.items()
        facts = _planner_facts.read_facts(_factory_base.PROFILE)
        allowed = _issues.repo_targets(_factory_base.PROFILE)
        facts = dataclasses.replace(
            facts,
            items=tuple(item for item in facts.items if item.repo.lower() in allowed),
            manual_requests=tuple(manual),
        )
        return _planner_runtime.execute(_factory_base.PROFILE, app, repo, base, board, rows, facts, requests)

    return json.dumps({"dispatched": await asyncio.to_thread(run)})
