"""`factory-loop.loop_run`: benchmark one reviewed PR and apply the owner's merge rule."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import secrets
import time
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import TypedDict, cast

import flyte
import flyte.report
from connectrpc.code import Code
from connectrpc.errors import ConnectError
from flyte import remote

from . import (
    _backlog,
    _bench,
    _bench_store,
    _board,
    _dashboard,
    _factory,
    _factory_base,
    _factory_meters,
    _github,
    _loop,
    _meter,
    _profile,
    _pull_requests,
    _queue,
    _router,
)
from ._images import agent_image

PROFILE = _profile.require()
env = flyte.TaskEnvironment(
    name="factory-loop",
    image=agent_image(),
    resources=flyte.Resources(cpu=1, memory="1Gi"),
    # TaskEnvironment accepts mixed secret lists; this list contains only Secret instances.
    secrets=cast(
        list[str | flyte.Secret],
        (
            (
                [flyte.Secret(key=PROFILE.board.pat_secret, as_env_var=_board.TOKEN_ENV)]
                if PROFILE.board
                else []
            )
            + (
                [flyte.Secret(key=PROFILE.github.private_key_secret, as_env_var=_pull_requests.KEY_ENV_VAR)]
                if PROFILE.github.configured
                else []
            )
        ),
    ),
    env_vars={
        "FLYTE_MCP_PROJECT": _factory.PROJECT,
        "FLYTE_MCP_DOMAIN": _factory.DOMAIN,
        **_profile.pod_env(),
    },
)


def _app() -> _github.GithubApp:
    return _github.GithubApp(PROFILE.github.app_id, os.environ[_pull_requests.KEY_ENV_VAR])


def _engine_source() -> dict[str, str]:
    """Return the benchmark's `source_repo` / `source_path` for where `[loop]` says the engine is.

    Empty when the benchmark's default is that engine already, so the call keeps its old shape: the root of
    `[repo] factory`, or, with none, `[loop]`'s own repo and path (`_bench_flow.candidate_source`).
    """
    loop, factory = PROFILE.loop, PROFILE.repos.get("factory")
    if not factory or (not loop.path and _github.parse_repo(factory).lower() == loop.repo.lower()):
        return {}
    return {"source_repo": loop.repo, "source_path": loop.path}


async def _benchmark(head: str, baseline: str) -> str:
    task = remote.Task.get("factory-bench.benchmark", auto_version="latest")
    task = await task.override.aio(timeout=flyte.Timeout(max_queued_time=timedelta(minutes=10)))
    # This workflow is executing inside a Flyte action, which always has a run name.
    parent = cast(str, flyte.ctx().action.run_name)
    # A deterministic run name reattaches a retry instead of paying for another benchmark.
    name = "loop-bench-" + hashlib.sha256(f"{parent}:{head}:{baseline}".encode()).hexdigest()[:19]
    await flyte.report.replace.aio(
        _dashboard.status_script({"loop_pending": True, "loop_benchmark": name}), do_flush=True
    )
    try:
        run = await remote.Run.get.aio(name=name)
    except ConnectError as exc:
        if exc.code != Code.NOT_FOUND:
            raise
        # Profile parsing requires [bench] whenever the loop is enabled.
        run = await flyte.with_runcontext(
            mode="remote",
            project=_factory.PROJECT,
            domain=_factory.DOMAIN,
            name=name,
            labels={"factory-loop": parent},
        ).run.aio(
            task,
            candidate_sha=head,
            baseline_sha=baseline,
            max_usd=cast(_profile.Bench, PROFILE.bench).max_usd,
            **_engine_source(),
        )
    await run.wait.aio()
    summary, _ = await run.outputs.aio()
    # The labelled benchmark owns its spend; copying cases here would bill it twice.
    await flyte.report.replace.aio(
        _dashboard.status_script({"loop_spend": [], "loop_benchmark": name}), do_flush=True
    )
    # factory-bench.benchmark declares its first output as JSON text.
    return cast(str, summary)


async def _initialize_spend() -> dict[str, object]:
    # An attempt can fail after paying for cases. Preserve its ledger when a retry reuses its decision.
    run = await remote.Run.get.aio(name=flyte.ctx().action.run_name)
    details = await run.action.details()
    for attempt in range(details.attempts, -1, -1):
        try:
            snapshot = _dashboard.from_report(await run.get_report.aio(attempt=attempt))
        except RuntimeError as exc:
            # Flyte has no typed missing-report error. Other failures must not erase the ledger.
            if not str(exc).startswith("No report is available for action"):
                raise
            continue
        if snapshot is not None:
            await flyte.report.replace.aio(_dashboard.status_script(snapshot), do_flush=True)
            return snapshot
    snapshot = {"loop_spend": []}
    await flyte.report.replace.aio(_dashboard.status_script(snapshot), do_flush=True)
    return snapshot


def _pending_cases(run: remote.Run) -> list[_PaidCase]:
    # Action.details() and outputs() exist only as coroutines; the listing has a sync form.
    actions = [
        a for a in remote.Action.listall(for_run_name=run.name) if a.task_name == "factory-bench.benchmark"
    ]
    if not actions:
        raise ValueError("loop benchmark spend is not yet available")
    return asyncio.run(_action_cases(actions))


class _PaidCase(TypedDict):
    ended_at: float
    usd_total: float | None
    reused_from: object


async def _action_cases(actions: Iterable[remote.Action]) -> list[_PaidCase]:
    cases: list[_PaidCase] = []
    for action in actions:
        details = await action.details()
        try:
            _, output = await details.outputs()
        except (ValueError, RuntimeError, TypeError) as exc:
            raise ValueError("loop benchmark spend is not yet available") from exc
        cases.extend(json.loads(line) for line in output.splitlines())
    return cases


def _spend(now: float, exclude_run: str = "") -> list[_loop.Spend]:
    """Read paid work once: project snapshots and fresh (never reused) benchmark case outputs."""
    rows = []
    after = datetime.fromtimestamp(now - 8 * 86400, timezone.utc)
    runs: dict[str, remote.Run] = {}
    for query in ({"with_label_keys": ["factory-loop"]}, {"task_name": "factory-loop.loop_run"}):
        found = list(remote.Run.listall(updated_at=remote.TimeFilter(after=after), limit=10000, **query))
        if len(found) == 10000:
            raise ValueError("loop spend listing exceeded its bound")
        runs.update((run.name, run) for run in found)
    for run in runs.values():
        if run.name == exclude_run:
            continue
        labels = _factory_base.run_labels(run)
        if "factory-bench" in labels:
            continue  # Case projects are already charged by their benchmark outputs.
        if "factory-loop" not in labels and _factory_base._task_name(run) != "factory-loop.loop_run":
            continue
        if _factory_base._task_name(run) == "factory-bench.benchmark":
            if _factory_base._phase(run) != "SUCCEEDED":
                raise ValueError("loop benchmark spend is not yet available")
            _, cases = run.outputs()
            for line in cases.splitlines():
                row = json.loads(line)
                if not row.get("reused_from"):
                    rows.append({"at": row["ended_at"], "usd_total": row["usd_total"], "engine": "claude"})
        else:
            snapshot = _dashboard.from_report(_factory_base.report_html(run))
            if snapshot is None:
                raise ValueError("loop run spend is not available")
            if snapshot.get("loop_benchmark"):
                # Its labelled run is read separately, even if the parent died before publishing a ledger.
                if snapshot["loop_benchmark"] not in runs:
                    raise ValueError("loop benchmark spend is not yet available")
                continue
            if snapshot.get("loop_pending"):
                rows.extend(
                    {"at": row["ended_at"], "usd_total": row["usd_total"], "engine": "claude"}
                    for row in _pending_cases(run)
                    if not row.get("reused_from")
                )
                continue
            if "loop_spend" in snapshot:
                rows.extend(snapshot["loop_spend"])
                continue
            total, _, _ = _bench.usd(snapshot)
            at = snapshot.get("generated_at")
            if total is None or at is None:
                raise ValueError("loop project spend is not available")
            # Sub-agent engine totals are already priced by the supervisor; the coordinator is Claude.
            by_engine = dict((snapshot.get("cost") or {}).get("by_engine") or {})
            by_engine["claude"] = by_engine.get("claude", 0) + total - sum(by_engine.values())
            rows.extend({"at": at, "usd_total": usd, "engine": engine} for engine, usd in by_engine.items())
    if any(
        isinstance(row["usd_total"], bool)
        or not isinstance(row["usd_total"], (int, float))
        or not math.isfinite(row["usd_total"])
        or row["usd_total"] < 0
        for row in rows
    ):
        raise ValueError("loop spend contains unknown or invalid cost")
    # Both the constructed rows and saved loop_spend ledgers use the admission spend schema.
    return cast(list[_loop.Spend], rows)


def _admission(app: _github.GithubApp) -> _loop.Budget:
    now = time.time()
    limits = PROFILE.loop
    if limits.daily_usd <= 0 or limits.weekly_usd <= 0:
        return _loop.budget(
            now, [], {}, False, False, daily_usd=limits.daily_usd, weekly_usd=limits.weekly_usd
        )
    readings = _factory_meters.login_meters(raw=True)
    # Every case runs on the one work token, so one reading speaks for the benchmark's account.
    token = PROFILE.bench.claude_token if PROFILE.bench else PROFILE.secrets.claude_token
    try:
        spend = _spend(now)
    except _factory.REMOTE_ERRORS:
        return _loop.Budget("wait", "spend-pending", now + _router.DEFAULT_RETRY_S)
    meters = {"claude": _factory_meters.token_meter(token, readings.values())}
    result = _loop.budget(
        now,
        spend,
        meters,
        False,
        False,
        daily_usd=limits.daily_usd,
        weekly_usd=limits.weekly_usd,
    )
    # The case's quota guard waits out a full window on the token, so admitting onto one would only
    # park the benchmark.
    full = _meter.full_windows(meters["claude"], now) if meters["claude"] is not None else []
    if result.action == "run" and full:
        known = [reset for reset in full if reset is not None]
        until = max(known) if len(known) == len(full) else now + _router.DEFAULT_RETRY_S
        result = _loop.Budget("wait", "window-full", until)
    return result


def _revert_breaker(app: _github.GithubApp, now: float) -> str:
    # Rollback (L7) will apply loop:reverted itself. Until then this fires only on a label applied by hand.
    reverts = []
    restarted = _loop.restarted_at(PROFILE, app)
    for issue in app.labelled_issues(PROFILE.loop.repo, "loop:reverted"):
        for event in app.issue_events(PROFILE.loop.repo, issue["number"]):
            if (
                event.get("event") == "labeled"
                and (event.get("label") or {}).get("name") == "loop:reverted"
                and (event.get("actor") or {}).get("login", "").lower()
                in ({app.app_login().lower()} | {login.lower() for login in PROFILE.github.maintainers})
            ):
                reverts.append(datetime.fromisoformat(event["created_at"].replace("Z", "+00:00")).timestamp())
    return _loop.breaker(now, [at for at in reverts if at > restarted])


def _comment_once(app: _github.GithubApp, repo: str, number: int, body: str) -> None:
    login = app.app_login().lower()
    record = json.loads(body.partition(_loop.MARKER)[2].partition(" -->")[0])
    for comment in app.comments(repo, number):
        if (comment.get("user") or {}).get("login", "").lower() != login:
            continue
        try:
            previous = json.loads(comment.get("body", "").partition(_loop.MARKER)[2].partition(" -->")[0])
        except json.JSONDecodeError:
            continue
        if isinstance(previous, dict) and all(
            previous.get(key) == record[key] for key in ("head", "baseline", "mode")
        ):
            if comment["body"] != body:
                app.update_comment(repo, comment["id"], body)
            return
    app.comment(repo, number, body)


def _previous_decision(
    app: _github.GithubApp, repo: str, number: int, head: str, baseline: str
) -> dict[str, object] | None:
    """Return the loop's own decision comment for this head against this main, if one was posted."""
    login = app.app_login().lower()
    for comment in app.comments(repo, number):
        if (comment.get("user") or {}).get("login", "").lower() != login or _loop.MARKER not in comment.get(
            "body", ""
        ):
            continue
        try:
            record = json.loads(comment["body"].partition(_loop.MARKER)[2].partition(" -->")[0])
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and (record.get("head"), record.get("baseline")) == (head, baseline):
            return record
    return None


@env.task(report=True, timeout=flyte.Timeout(max_queued_time=timedelta(minutes=5)))
async def loop_run(pr_number: int) -> str:
    """Process a PR in [loop] repo. Dry-run records the decision; disabled loops do nothing."""
    if not PROFILE.loop.enabled:
        return "skip"
    snapshot = await _initialize_spend() or {}
    app, repo = _app(), PROFILE.loop.repo
    pull = await asyncio.to_thread(app.pull, repo, pr_number)
    if pull["state"] != "open":
        return "skip"
    # Any reviewed PR gets the status that wakes this task; only the loop's own may be merged or closed.
    # GithubApp returns an unparameterized dict; its PR response supplies the typed head fields.
    if not await asyncio.to_thread(_loop.loop_pull, app, repo, cast(_loop.Pull, pull)):
        print(f"PR #{pr_number} is not a loop PR; skipped", flush=True)
        return "not-loop"
    head = pull["head"]["sha"]
    branch, baseline = await asyncio.to_thread(app.default_branch_sha, repo)
    files = await asyncio.to_thread(app.pull_files, repo, pr_number)
    # The files endpoint supplies filename and, on renames, previous_filename.
    classification = _loop.classify(
        cast(list[_loop.ChangedFile], files), pull.get("changed_files"), PROFILE.loop.path
    )
    # Benchmarks resolve engine SHAs in the loop's repo, so a foreign head or another target is unsafe.
    if (pull["head"].get("repo") or {}).get("full_name") != repo or pull["base"]["ref"] != branch:
        classification = "needs_owner"
    review = await asyncio.to_thread(app.status_at, repo, head, _pull_requests.STATUS_CONTEXT)
    health, _ = await asyncio.to_thread(app.commit_health, repo, head)
    reviewed = review == "success" and health == "ok" and not pull.get("draft", False)
    verdict = {"verdict": "not_run", "reason": classification}
    previous = (
        await asyncio.to_thread(_previous_decision, app, repo, pr_number, head, baseline)
        if classification == "benchmark" and reviewed
        else None
    )
    if previous is not None and previous.get("decision") == "inconclusive":
        # The same head against the same main was measured already: another run costs a full benchmark.
        return "inconclusive"
    if reviewed and classification != "needs_owner":
        reason = await asyncio.to_thread(_revert_breaker, app, time.time())
        if reason:
            await asyncio.to_thread(_loop.open_breaker, PROFILE, app, reason)
            return "killed"
    if classification == "benchmark" and reviewed:
        if await asyncio.to_thread(_loop.killed, PROFILE, app):
            return "killed"
        admission = await asyncio.to_thread(_admission, app)
        if admission.action == "wait":
            row = {
                "event": "loop_wait",
                "at": time.time(),
                "reason": admission.reason,
                "until": admission.until,
            }
            snapshot["loop_wait"] = row
            # The saved timeline is a list of report rows, including this admission event.
            cast(list[dict[str, object]], snapshot.setdefault("timeline", [])).append(row)
            row.update(time=datetime.now(timezone.utc).isoformat(), icon="wait", text=admission.reason)
            await flyte.report.replace.aio(
                f"<table><tr><td><code>{row['time']}</code></td>"
                f"<td>Waiting: {admission.reason}</td></tr></table>" + _dashboard.status_script(snapshot),
                do_flush=True,
            )
            print(json.dumps(row), flush=True)
            return admission.reason
        summary = json.loads(await _benchmark(head, baseline))
        # The verdict is evidence only for exactly this head against exactly this main.
        if summary.get("engine_sha") != head or summary.get("baseline_sha") != baseline:
            raise ValueError("benchmark summary is not for this head against this main")
        verdict = summary.get("verdict") or {"verdict": "inconclusive", "reason": "no benchmark verdict"}
    decision = _loop.decide(verdict, classification, reviewed)
    if decision == "skip":
        return decision
    if decision in ("merge", "close"):
        current = await asyncio.to_thread(app.pull, repo, pr_number)
        _, current_main = await asyncio.to_thread(app.default_branch_sha, repo)
        if current["state"] != "open":
            return "skip"
        if current["head"]["sha"] != head or current_main != baseline or current["base"]["ref"] != branch:
            decision = "stale"
        elif decision == "merge":
            review = await asyncio.to_thread(app.status_at, repo, head, _pull_requests.STATUS_CONTEXT)
            health, _ = await asyncio.to_thread(app.commit_health, repo, head)
            if review != "success" or health != "ok" or current.get("draft", False):
                return "skip"
    mode = PROFILE.loop.merge
    body = _loop.decision_comment(decision, verdict, head, baseline, mode=mode)
    await asyncio.to_thread(_comment_once, app, repo, pr_number, body)
    if mode == "dry-run":
        return decision
    label = {"needs_owner": "needs-owner", "inconclusive": "loop:inconclusive"}.get(decision)
    if label:
        await asyncio.to_thread(app.add_labels, repo, pr_number, [label])
    if decision == "merge":
        # Posting the comment can take time; do not merge a head or base that moved during it.
        current = await asyncio.to_thread(app.pull, repo, pr_number)
        current_branch, current_main = await asyncio.to_thread(app.default_branch_sha, repo)
        if current["state"] != "open":
            return "skip"
        if (
            current["head"]["sha"] != head
            or current_main != baseline
            or current_branch != branch
            or current["base"]["ref"] != branch
        ):
            body = _loop.decision_comment("stale", verdict, head, baseline, mode=mode)
            await asyncio.to_thread(_comment_once, app, repo, pr_number, body)
            return "stale"
        review = await asyncio.to_thread(app.status_at, repo, head, _pull_requests.STATUS_CONTEXT)
        health, _ = await asyncio.to_thread(app.commit_health, repo, head)
        if review != "success" or health != "ok" or current.get("draft", False):
            body = _loop.decision_comment("skip", verdict, head, baseline, mode=mode)
            await asyncio.to_thread(_comment_once, app, repo, pr_number, body)
            return "skip"
        # Comments are audit and dedupe only. Every retry rechecks live authority and the switch.
        if await asyncio.to_thread(_loop.killed, PROFILE, app):
            body = _loop.decision_comment("killed", verdict, head, baseline, mode=mode)
            await asyncio.to_thread(_comment_once, app, repo, pr_number, body)
            return "killed"
        # The kill check makes several calls; main may have moved meanwhile. Head is pinned by `sha` below.
        _, latest_main = await asyncio.to_thread(app.default_branch_sha, repo)
        if latest_main != baseline:
            body = _loop.decision_comment("stale", verdict, head, baseline, mode=mode)
            await asyncio.to_thread(_comment_once, app, repo, pr_number, body)
            return "stale"
        result = await asyncio.to_thread(app.merge_pull, repo, pr_number, head)
        if not result.get("merged"):
            raise _github.GithubError("GitHub did not merge the pull request")
        await asyncio.to_thread(app.add_labels, repo, pr_number, ["loop:merged"])
    elif decision == "close":
        await asyncio.to_thread(app.close_pull, repo, pr_number)
        await asyncio.to_thread(app.add_labels, repo, pr_number, ["loop:rejected"])
    return decision


def _generate_backlog(app: _github.GithubApp) -> str:
    repo = PROFILE.loop.repo
    if not PROFILE.state.uri:
        raise ValueError("backlog generation requires [state] uri")
    holder = secrets.token_hex(16)
    root = f"{PROFILE.state.uri.rstrip('/')}/loop/backlog"
    if _queue.take_lease(root, holder) is not None:
        raise RuntimeError("another backlog generator is running")
    board = None
    try:
        if _loop.killed(PROFILE, app):
            return "killed"
        records, summaries = [], []
        if PROFILE.direction:
            store_repo = _github.parse_repo(PROFILE.direction.repo)
            url = _github.clone_url(store_repo, app.installation_token(store_repo))
            with _bench_store.Store(url) as store:
                records = store.records()
                for summary_path in sorted((store.tree / "runs").glob("*/summary.json")):
                    summaries.append(json.loads(summary_path.read_text()))
        snapshots = []
        for run in _factory.recent_runs(hours=168):
            if _factory_base._phase(run) in _factory_base.LIVE or not _factory_base.run_labels(run).get(
                _factory_base.LABEL_PROJECT
            ):
                continue
            snapshot = _dashboard.from_report(_factory_base.report_html(run))
            if snapshot:
                snapshots.append(snapshot)
        paths = app.tracked_paths(repo)
        notes = app.read_file(repo, ".briefs/NOTES.md") if ".briefs/NOTES.md" in paths else ""
        inbox = {}
        for path in sorted(paths):
            match = re.fullmatch(r"\.briefs/inbox-([0-9]+)\.md", path)
            if match:
                inbox[match[1]] = app.read_file(repo, path)
        hypotheses = re.findall(r"^\d+\. (.+)$", inbox.get("004", ""), re.M)
        # Store records and report snapshots supply the source fields consumed by the backlog.
        proposals = _backlog.drafts(
            records=cast(list[_backlog._Record], records),
            summaries=summaries,
            snapshots=cast(list[_backlog._Snapshot], snapshots),
            notes=notes,
            inbox=inbox,
            hypotheses=hypotheses,
        )
        if PROFILE.board:
            board = _board.BoardClient(_board.take_token(PROFILE.board.pat_secret))
            board.bootstrap(PROFILE.board.owner, PROFILE.board.owner_type, PROFILE.board.number)
        created = _backlog.publish(
            app,
            repo,
            proposals,
            now=datetime.now(timezone.utc),
            board=board,
            intake_label=PROFILE.github.intake_label,
        )
        return json.dumps([issue["number"] for issue in created])
    finally:
        if board:
            board.close()
        _queue.release_lease(root, holder)


@env.task(
    triggers=flyte.Trigger(
        name="factory-loop-backlog",
        automation=flyte.FixedRate(1440),
        auto_activate=True,
        description="Draft factory improvement contracts from benchmark and production findings",
    ),
    timeout=flyte.Timeout(max_runtime=timedelta(minutes=4), max_queued_time=timedelta(minutes=10)),
)
async def generate_backlog() -> str:
    """Generate at most five improvement issues daily, only when the loop is enabled."""
    if not PROFILE.loop.enabled:
        return "skip"
    return await asyncio.to_thread(_generate_backlog, _app())
