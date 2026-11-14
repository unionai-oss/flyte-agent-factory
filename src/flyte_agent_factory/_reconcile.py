"""Hourly board and milestone repair from Flyte runs and GitHub evidence.

The supervisor writes live run fields and milestones; this heals writes lost to an outage or a dead
pod. GitHub issues and main commits also prove completion for work merged without a project run.
Uncertain board drift is reported for review, with a summary status on main. The tick keeps no durable
state: a failed pass is the next pass's work, and a failed tick raises so the run list shows the streak.
The retired `factory-board-sync` and `issue_outbox` outboxes are no longer read or written.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from itertools import islice
from typing import Callable, cast

import flyte
from connectrpc.code import Code
from connectrpc.errors import ConnectError
from flyte import remote

from . import (
    _board,
    _board_events,
    _dashboard,
    _deploy,
    _factory,
    _factory_base,
    _github,
    _issues,
    _profile,
    _pull_requests,
    _queue,
)
from ._errors import REMOTE_ERRORS
from ._images import agent_image

PROFILE = _profile.require()
#: How long after its run ends an item is still the factory's to move. Past it the reconciler leaves the
#: item alone, so it never fights an owner who has since moved it by hand.
BOARD_WINDOW = timedelta(hours=2)
#: How long after its run ends a missing milestone is still posted.
MILESTONE_WINDOW_H = 24
#: Set on every run the queue starts for an issue (`_factory`): `_issues.issue_label` of the issue.
LABEL_ISSUE = "factory-issue"
log = logging.getLogger(__name__)
_missing_reports: set[str] = set()

_secrets: list[str | flyte.Secret] = []
if PROFILE.board:
    _secrets.append(flyte.Secret(key=PROFILE.board.pat_secret, as_env_var=_board.TOKEN_ENV))
if PROFILE.github.configured:
    _secrets.append(
        flyte.Secret(key=PROFILE.github.private_key_secret, as_env_var=_pull_requests.KEY_ENV_VAR)
    )
env = flyte.TaskEnvironment(
    name="factory-reconcile",
    image=agent_image(),
    resources=flyte.Resources(cpu=1, memory="1Gi"),
    interruptible=True,
    secrets=_secrets,
    # As `factory`'s: the project and domain `_factory` lists runs in.
    env_vars={
        "FLYTE_MCP_PROJECT": _factory.PROJECT,
        "FLYTE_MCP_DOMAIN": _factory.DOMAIN,
        **_profile.pod_env(),
    },
)


def _ended_at(run: remote.Run) -> datetime | None:
    """When a terminal run ended; None while it has not."""
    status = run.pb2.action.status
    if not status.HasField("end_time"):
        return None
    # The generated protobuf timestamp has no typed return annotation.
    return cast(datetime, status.end_time.ToDatetime().replace(tzinfo=timezone.utc))


def _started_at(run: remote.Run) -> float | None:
    """When the run started, or None before it has (the server then reads the epoch)."""
    started = run.action.start_time.timestamp()
    return started if started > 0 else None


def _ended_within(run: remote.Run, window: timedelta, now: float) -> bool:
    ended = _ended_at(run)
    return ended is not None and now - ended.timestamp() <= window.total_seconds()


def _snapshot(run: remote.Run) -> _issues.Snapshot | None:
    # Dashboard reports carry the review and deliverables fields consumed here.
    return cast(_issues.Snapshot | None, _dashboard.from_report(_factory_base.report_html(run)))


def _board_snapshot(run: remote.Run) -> _issues.Snapshot | None:
    """Return the run's status snapshot; None, an empty one, for a run whose report is not there (yet)."""
    try:
        return _snapshot(run)
    except REMOTE_ERRORS:
        return None


#: The SDK pages listings in batches of 100. Bound work even during a burst of project runs.
RUN_PAGE_SIZE = 100
RUN_PAGE_CAP = 5
LIVE_RUN_LIMIT = 200


def project_runs() -> list[remote.Run]:
    """Live issue runs and recently updated issue runs, using the SDK's creation-sorted pagination.

    Two listings shared across all slots and items: normally two RPCs, at most seven pages.
    Updated time includes long projects that only just ended, even with an old creation time.
    """
    after = datetime.now(timezone.utc) - timedelta(hours=MILESTONE_WINDOW_H)
    found: dict[str, remote.Run] = {}
    for live, limit in ((True, LIVE_RUN_LIMIT), (False, RUN_PAGE_SIZE * RUN_PAGE_CAP)):
        listed = remote.Run.listall(
            in_phase=_factory_base.SERVER_LIVE if live else None,
            updated_at=None if live else remote.TimeFilter(after=after),
            with_label_keys=[LABEL_ISSUE],
            limit=limit,
            sort_by=("created_at", "desc"),
        )
        # Stop before asking the iterator for another page at the cap.
        page = list(islice(listed, limit))
        if len(page) == limit:
            log.warning(
                "Reconcile %s run listing reached its %s-run page cap", "live" if live else "recent", limit
            )
        found.update((r.name, r) for r in page if _factory_base._task_name(r) in _factory_base.SLOTS)
    log.info("Reconcile run listings: 2 queries, at most %s pages", 2 + RUN_PAGE_CAP)
    return list(found.values())


class RunIndex:
    """One tick's project runs by the issue they were started for, so an item is a lookup, not a listing."""

    def __init__(self, runs: list[remote.Run], now: float | None = None) -> None:
        self.now = time.time() if now is None else now
        self._by_issue: dict[str, list[remote.Run]] = {}
        self._seen = {r.name for r in runs}
        self._fetched: dict[str, remote.Run | None] = {}
        self._snapshots: dict[str, _issues.Snapshot | None] = {}
        # Newest first. A run that never started (aborted while queued) has no report and no start time:
        # it is not a run the item could show, as in `heal_milestones`.
        started = [(at, r) for r in runs if (at := _started_at(r)) is not None]
        for _, run in sorted(started, key=lambda pair: pair[0], reverse=True):
            label = _factory_base.run_labels(run).get(LABEL_ISSUE)
            if label:
                self._by_issue.setdefault(label, []).append(run)

    def include_shown(self, ref: str, url: str) -> None:
        """Fetch a run the board still shows if the bounded listing did not include it."""
        prefix = _board.run_url(PROFILE, "")
        if not prefix or not url.startswith(prefix):
            return
        name = url[len(prefix) :]
        if not name or "/" in name or name in self._seen:
            return
        if name not in self._fetched:
            try:
                self._fetched[name] = remote.Run.get(name=name)
            except ConnectError as exc:
                if exc.code != Code.NOT_FOUND:
                    raise
                self._fetched[name] = None
            log.info("Reconcile fetched board run %s directly (%s fetches)", name, len(self._fetched))
        run = self._fetched[name]
        if (
            run is None
            or _factory_base._task_name(run) not in _factory_base.SLOTS
            or _started_at(run) is None
        ):
            return
        label = _factory_base.run_labels(run).get(LABEL_ISSUE)
        if label and label != _issues.issue_label(ref):
            return
        # Run.get reconstructs the action without run labels; the board supplies the issue then.
        runs = self._by_issue.setdefault(_issues.issue_label(ref), [])
        if not any(r.name == name for r in runs):
            runs.append(run)
            # Every indexed run has passed the start-time check above.
            runs.sort(key=cast(Callable[[remote.Run], float], _started_at), reverse=True)

    def live_issues(self) -> list[str]:
        """Return the issues (`owner/repo#N`) of the live runs this tick indexed, which the board holds."""
        issues = []
        for runs in self._by_issue.values():
            run = next((r for r in runs if _factory_base._phase(r) in _factory_base.LIVE), None)
            issue = _run_issue(PROFILE, _factory_base.run_labels(run)) if run else ""
            if not issue:
                continue
            try:
                issues.append(_board.normalise_issue(issue, PROFILE.repos.values()))
            except ValueError:
                log.warning("Run %s links an issue the board cannot name", run.name)  # type: ignore[union-attr]  # A nonempty issue above requires a run.
        return issues

    def latest(self, ref: str) -> tuple[bool, str, _issues.Snapshot | None] | None:
        """Return `(live, url, snapshot)` of issue `ref`'s newest project run, or None.

        The run counts if it is live or ended within `BOARD_WINDOW`; with None the reconciler leaves
        the item alone.

        The newest *live* run wins over a newer ended one: a duplicate that `_factory_recreate._keep_oldest`
        aborted is newer than the run it lost to.
        """
        runs = self._by_issue.get(_issues.issue_label(ref), [])
        live = [r for r in runs if _factory_base._phase(r) in _factory_base.LIVE]
        # An issue with no indexed run has no run to project.
        candidates = live or runs
        if not candidates:
            return None
        run = candidates[0]
        if not (live or _ended_within(run, BOARD_WINDOW, self.now)):
            return None
        return bool(live), _board.run_url(PROFILE, run.name), _board_snapshot(run)

    def newest(self, ref: str) -> tuple[bool, datetime | None, _issues.Snapshot | None] | None:
        """Return `(live, ended_at, snapshot)` of issue `ref`'s newest indexed run, however long ago it ended.

        `latest` is for projecting a run onto its card and goes quiet after `BOARD_WINDOW`; the truth pass
        needs the run a stale card last showed. The index holds runs updated in the last day, plus the
        ones the board still shows (`include_shown`).
        """
        runs = self._by_issue.get(_issues.issue_label(ref), [])
        live = [r for r in runs if _factory_base._phase(r) in _factory_base.LIVE]
        candidates = live or runs
        if not candidates:
            return None
        run = candidates[0]
        if run.name not in self._snapshots:
            self._snapshots[run.name] = _board_snapshot(run)
        return bool(live), _ended_at(run), self._snapshots[run.name]


def latest_run(ref: str, now: float | None = None) -> tuple[bool, str, _issues.Snapshot | None] | None:
    """`RunIndex.latest` of a fresh listing: for one issue. A tick indexes once for all its items."""
    return RunIndex(project_runs(), now).latest(ref)


def queued_issues(profile: _profile.Profile) -> set[str]:
    """Lower-case `owner/repo#N` of each issue a pending launch request names; none without `[state]`."""
    if not profile.state.uri:
        return set()
    queued = set()
    for entry in _queue.pending(_queue.root(profile.state.uri)):
        if entry.request and entry.request.issue:
            try:
                queued.add(_board.normalise_issue(entry.request.issue, profile.repos.values()))
            except ValueError:
                log.warning("Queued launch %s links an issue the board cannot name", entry.name)
    return queued


def _run_issue(profile: _profile.Profile, labels: dict[str, str]) -> str:
    """Return the issue a run's `factory-issue` label stands for, from the launch request that started it.

    The label is a hash, so the reference comes from the queue (still in `[state]` until state step 3,
    which passes it to the run as its `issue` input instead). A request that names a different issue
    than the label is not trusted.
    """
    issue = _issues.linked_issue(profile, labels.get(_factory_base.LABEL_PROJECT, ""))
    return issue if issue and _issues.issue_label(issue) == labels.get(LABEL_ISSUE) else ""


def heal_milestones(
    profile: _profile.Profile,
    app: _github.GithubApp,
    now: float | None = None,
    runs: list[remote.Run] | None = None,
) -> int:
    """Post the milestones Flyte proves happened that are missing from their issues; how many it posted.

    Only runs live or ended within `MILESTONE_WINDOW_H`. While a run is live a key is the supervisor's
    for `_issues.SUPERVISOR_GRACE_S` after it happened, so the two rarely race on one marker. Raises the
    first remote error once every run has been tried. Unavailable reports skip that run for this tick,
    with warnings limited to an hourly window even across fresh processes. `runs` defaults to the
    project's recent runs.
    """
    now = time.time() if now is None else now
    posted, failed = 0, []
    runs = list(_factory.recent_runs(MILESTONE_WINDOW_H) if runs is None else runs)
    _missing_reports.intersection_update(run.name for run in runs)
    for run in runs:
        issue = ""
        try:
            labels = _factory_base.run_labels(run)
            if LABEL_ISSUE not in labels:
                continue
            phase = _factory_base._phase(run)
            live = phase in _factory_base.LIVE
            if not live and not _ended_within(run, timedelta(hours=MILESTONE_WINDOW_H), now):
                continue
            issue = _run_issue(profile, labels)
            if not issue:
                continue
            started = _started_at(run)
            if started is None:
                continue
            try:
                snapshot = _snapshot(run)
            except REMOTE_ERRORS as exc:
                # Scheduled ticks start fresh processes. A GitHub streak marker would need extra
                # writes and cleanup; one five-minute window per run-hour needs no durable state.
                if run.name not in _missing_reports and (now - started) % 3600 < 300:
                    log.warning("Could not read the report of %s: %s", run.name, type(exc).__name__)
                _missing_reports.add(run.name)
                continue
            _missing_reports.discard(run.name)
            due = [
                (key, body)
                for key, body, at in _issues.derive(
                    snapshot,
                    url=_board.run_url(profile, run.name),
                    phase=phase,
                    live=live,
                    started_at=started,
                )
                if not live or (at is not None and now - at >= _issues.SUPERVISOR_GRACE_S)
            ]
            since = datetime.fromtimestamp(started, timezone.utc).isoformat()
            for key, body in due:
                posted += _issues.post_milestone(app, issue, run.name, key, body, since=since)
        except REMOTE_ERRORS as exc:
            log.warning("Could not heal the milestones of %s on %s: %s", run.name, issue, type(exc).__name__)
            failed.append(exc)
    if failed:
        raise failed[0]
    return posted


@env.task(
    triggers=flyte.Trigger(
        name="factory-reconcile",
        automation=flyte.FixedRate(_deploy.RECONCILE_MINUTES),
        auto_activate=True,
        description="Reconcile board fields and issue milestones from Flyte and GitHub",
    ),
    # Scheduled passes finish before the next tick; event passes also have this runtime bound.
    timeout=flyte.Timeout(max_runtime=timedelta(minutes=4), max_queued_time=timedelta(minutes=5)),
)
async def reconcile(completion_run: str = "") -> str:
    """Heal the board (with `[board]`) and the issue milestones (with the GitHub App)."""
    if completion_run:
        # Submitted by the project's last act, before Flyte has marked its run terminal.
        while True:
            # Run.get takes only the name; project and domain come from the task's own context.
            run = await asyncio.to_thread(remote.Run.get, name=completion_run)
            if _factory_base._phase(run) in ("SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"):
                break
            await asyncio.sleep(2)
    token = _board.take_token(PROFILE.board.pat_secret if PROFILE.board else "")
    key = _pull_requests.take_app_key(PROFILE.github.private_key_secret)
    done, failed = [], []
    milestones = PROFILE.github.configured and key
    # Shared by both halves; direct fetches are only for distinct board runs absent from the listing.
    runs = await asyncio.to_thread(project_runs) if PROFILE.board or milestones else []
    if PROFILE.board:
        try:
            index = RunIndex(runs)
            await asyncio.to_thread(
                _board.Reconciler(
                    PROFILE,
                    token,
                    index.latest,
                    include_shown=index.include_shown,
                    live_issues=index.live_issues,
                ).tick
            )
            done.append("board")
        except REMOTE_ERRORS as exc:
            failed.append(exc)
    if PROFILE.board and milestones:
        try:
            # After the run projection. The one board audit: it needs the App for the pull requests,
            # commits and comments its facts come from; without it Status is not guessed.
            rows = await asyncio.to_thread(
                _board_events.drift,
                PROFILE,
                token,
                _github.GithubApp(PROFILE.github.app_id, key),
                index.latest,
                newest=index.newest,
                queued=await asyncio.to_thread(queued_issues, PROFILE),
            )
            corrections = sum(map(_board_events.is_correction, rows))
            done.append(f"{corrections} board truth correction(s)")
            done.append(f"{len(rows) - corrections} board drift row(s)")
        except REMOTE_ERRORS as exc:
            failed.append(exc)
    if milestones:
        try:
            count = await asyncio.to_thread(
                heal_milestones, PROFILE, _github.GithubApp(PROFILE.github.app_id, key), None, runs
            )
            done.append(f"{count} milestone(s) posted")
        except REMOTE_ERRORS as exc:
            failed.append(exc)
    if PROFILE.board and not milestones:
        log.info("Board truth skipped: it needs the GitHub App for pull requests, commits and comments")
    if failed:
        raise failed[0]
    return "; ".join(done) or "nothing to reconcile"
