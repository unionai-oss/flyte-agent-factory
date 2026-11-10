"""`factory-bench`: the factory benchmark, one launchable workflow.

    benchmark(candidate_sha, baseline_sha, ...) -> (summary JSON, cases JSONL)

It deploys the engine at `candidate_sha` as bench envs of its own (`deploy_engine`), runs each case as
an ordinary project on one of them (`run_case`), scores the code the case pushed with the task's hidden
check in a pod without credentials (`_bench_check`), and writes the report. Measurement is `_bench`; the
tasks are `_bench_suite`; `design-benchmark.md` is the spec. Paired runs interleave candidate and
baseline on the same bench envs, with calibration and results in the results store.

Isolation from production: bench envs are named apart from the slots and run on the work token
(`[secrets] claude_token`, or the `[bench]` table's own); the bench profile has no `[state]`, `[board]` or
intake (`_bench.bench_profile`); nothing here writes object storage. A case always ends with `/stop` before
any abort, so the session can wind down its work.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import flyte
import flyte.errors
import flyte.io
import flyte.report
import httpx
from connectrpc.code import Code
from connectrpc.errors import ConnectError
from flyte import remote
from obstore.exceptions import BaseError as ObstoreError

# The runtime imports are absolute (see `_webhook` for why); mypy resolves `flyte_agent_factory` to the
# install, which has no py.typed marker, so it checks against the relative imports instead.
if TYPE_CHECKING:
    from . import (
        _bench,
        _bench_check,
        _bench_stats,
        _bench_store,
        _bench_suite,
        _dashboard,
        _deploy,
        _factory,
        _factory_base,
        _factory_policy,
        _github,
        _loop,
        _meter,
        _profile,
        _pull_requests,
        _router,
    )
    from . import (
        _git as _repo_git,
    )
    from ._errors import REMOTE_ERRORS
    from ._images import agent_image
    from ._policy import LAUNCH_MODEL
else:
    from flyte_agent_factory import (
        _bench,
        _bench_check,
        _bench_stats,
        _bench_store,
        _bench_suite,
        _dashboard,
        _deploy,
        _factory,
        _factory_base,
        _factory_policy,
        _github,
        _loop,
        _meter,
        _profile,
        _pull_requests,
        _router,
    )
    from flyte_agent_factory import (
        _git as _repo_git,
    )
    from flyte_agent_factory._errors import REMOTE_ERRORS
    from flyte_agent_factory._images import agent_image
    from flyte_agent_factory._policy import LAUNCH_MODEL

PROFILE = _profile.require()
PROJECT = _factory.PROJECT
DOMAIN = _factory.DOMAIN
#: Seconds between reads of a case's report.
POLL_S = _bench.POLL_S
REPORT_STALE_S = 5 * 60
STOP_RETRY_S = _bench.STOP_RETRY_S
#: How long a case has to end after `/stop` before it is aborted.
STOP_GRACE_S = _bench.STOP_GRACE_S
STOP_POLL_S = 15
#: The stop reserve's burn rate is measured over this window of cost readings.
BURN_WINDOW_S = 600
ABORT_REASON = "Benchmark case did not end within 10 minutes of /stop"
#: The queue bound for spot attempts, after which a child goes on-demand (DESIGN.md: always bound the queue).
QUEUE_BOUND = timedelta(minutes=10)
#: Labels on every case run, so a benchmark's runs are listed by the server.
LABEL_BENCH = "factory-bench"

env = flyte.TaskEnvironment(
    name="factory-bench",
    image=agent_image(),
    # Polling, git fetches and the deploy subprocesses that submit image builds: nothing heavy.
    resources=flyte.Resources(cpu=1, memory="2Gi"),
    # The App fetches the engine and each case's branch; it never reaches a deploy child or a check.
    secrets=(
        [flyte.Secret(key=PROFILE.github.private_key_secret, as_env_var=_deploy.GITHUB_KEY_ENV)]
        if PROFILE.github.private_key_secret
        else []
    ),
    env_vars={"FLYTE_MCP_PROJECT": PROJECT, "FLYTE_MCP_DOMAIN": DOMAIN, **_profile.pod_env()},
    depends_on=[_bench_check.env],
)


class HarnessError(RuntimeError):
    """The case cannot be run as specified (its start is missing or not the pinned one)."""


def _bench_table() -> _profile.Bench:
    if PROFILE.bench is None:
        raise ValueError("the deployment profile has no [bench] table (profiles/README.md)")
    return PROFILE.bench


def _app() -> _github.GithubApp:
    key = os.environ.get(_deploy.GITHUB_KEY_ENV, "")
    if not key:
        raise RuntimeError(f"the benchmark needs the GitHub App key mounted as {_deploy.GITHUB_KEY_ENV}")
    return _github.GithubApp(PROFILE.github.app_id, key)


def _clone_url(app: _github.GithubApp, url: str) -> str:
    """Return an HTTPS URL for `url` carrying an installation token. Secret: never log it."""
    repo = _github.parse_repo(url)
    return _github.clone_url(repo, app.installation_token(repo))


def _store(app: _github.GithubApp) -> _bench_store.Store:
    if PROFILE.direction is None:
        raise _bench_store.StoreError("benchmark results require a [direction] repository")
    try:
        return _bench_store.Store(_clone_url(app, PROFILE.direction.repo))
    except _github.GithubError:
        raise _bench_store.StoreError(
            "results store could not obtain a GitHub App installation token for the direction repository; "
            "check its installation and contents: write permission"
        ) from None


def resolve_sha(app: _github.GithubApp, ref: str) -> str:
    """`ref` as a commit SHA of the engine repo: itself when it is one, else resolved through the App."""
    if len(ref) == 40:
        return _bench.check_sha(ref)
    _, sha = app.default_branch_sha(_github.parse_repo(PROFILE.repos["factory"]), ref)
    return _bench.check_sha(sha)


def _git(tree: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=tree, capture_output=True, text=True, check=False)
    return done.stdout.strip() if done.returncode == 0 else ""


def start_of(url: str, task: _bench_suite.Task) -> tuple[str, str]:
    """`(commit, tree)` of `task.workdir` at the bench repo's default branch head. HarnessError if absent."""
    try:
        with _deploy.fetched_tree(url, "HEAD") as tree:
            commit, workdir = _git(tree, "rev-parse", "HEAD"), _git(tree, "rev-parse", f"HEAD:{task.workdir}")
    except _deploy.DeployError as exc:
        raise HarnessError(f"could not read the bench repo: {exc}") from exc
    if not workdir:
        raise HarnessError(f"the bench repo's default branch has no {task.workdir}/ (seed it first)")
    return commit, workdir


def _case_branch(case: dict[str, Any]) -> str:
    return f"bench/{case['case']}"


def _checked_git(tree: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=tree, capture_output=True, text=True, check=False)
    if done.returncode:
        # Git errors can echo installation-token URLs. Keep credentials out of case records.
        raise HarnessError(f"git {args[0]} failed while preparing or cleaning the case")
    return done.stdout.strip()


def prepare_case(url: str, case: dict[str, Any], task: _bench_suite.Task) -> None:
    """Publish only the start tree, on a new root commit with no reachable answer history."""
    with (
        _deploy.fetched_tree(url, "HEAD") as source,
        tempfile.TemporaryDirectory(prefix="bench-case-") as tmp,
    ):
        actual = _git(source, "rev-parse", f"HEAD:{task.workdir}")
        if task.start_tree and actual != task.start_tree:
            raise HarnessError(f"{task.workdir} is tree {actual}, not the pinned {task.start_tree}")
        tree = Path(tmp)
        destination = tree / task.workdir
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source / task.workdir, destination)
        _checked_git(tree, "init", "-q", "-b", _case_branch(case))
        _checked_git(tree, "add", ".")
        _checked_git(
            tree,
            "-c",
            "user.name=Factory benchmark",
            "-c",
            "user.email=benchmark@example.org",
            "commit",
            "-qm",
            "Benchmark start",
        )
        _checked_git(tree, "push", url, f"HEAD:refs/heads/{_case_branch(case)}")


def cleanup_case(url: str, case: dict[str, Any]) -> str | None:
    """Remove case branches after scoring; a cleanup failure is a measurement caveat, not a retry."""
    try:
        with tempfile.TemporaryDirectory(prefix="bench-cleanup-") as tmp:
            tree = Path(tmp)
            _checked_git(tree, "init", "-q")
            branches = _checked_git(
                tree,
                "ls-remote",
                "--heads",
                url,
                f"refs/heads/{_case_branch(case)}",
                f"refs/heads/factory/{case['case']}",
            )
            for line in branches.splitlines():
                ref = line.split()[1]
                _checked_git(tree, "push", url, f":{ref}")
    except (HarnessError, OSError) as exc:
        return str(exc)
    return None


def _existing_run(name: str) -> remote.Run | None:
    """Return the case's run if one exists.

    Creating a run under a taken name replaces that run (fake_flyte).
    """
    try:
        return remote.Run.get(name=name)
    except ConnectError as exc:
        if exc.code != Code.NOT_FOUND:
            raise
        return None


def _max_runtime_s(task: _bench_suite.Task) -> float:
    return task.cap_hours * 3600 + STOP_RETRY_S + STOP_GRACE_S


def _launch(case: dict[str, Any], task: _bench_suite.Task) -> Any:
    slot = remote.Task.get(f"{case['env']}.project", auto_version="latest").override(
        timeout=flyte.Timeout(max_runtime=timedelta(seconds=_max_runtime_s(task)))
    )
    return _factory.run_project(
        slot,
        _bench_suite.brief(task),
        case["case"],
        case["model"],
        repo=_bench_table().repo,
        policy=_factory_policy.normalize_policy(case["policy"]),
        run_name=case["case"],
        branch=_case_branch(case),
        hard_pin=True,
        labels={
            LABEL_BENCH: case["bench_run"],
            "bench-arm": case["arm"],
            "bench-task": case["task"],
            "bench-rep": str(case["repeat"]),
            **({"factory-loop": case["bench_run"]} if case.get("loop_origin") else {}),
        },
    )


def _snapshot(run: remote.Run) -> dict[str, Any]:
    try:
        page = _factory_base.report_html(run)
    except REMOTE_ERRORS:  # read again at the next poll; the error text may hold a signed URL
        return {}
    return _dashboard.from_report(page) or {}


def _signal(run_name: str, wanted: Callable[[str], bool], payload: str) -> bool:
    """Signal the first open condition of the run whose name is `wanted`; whether there was one."""
    for condition in remote.Condition.listall(run_name=run_name):
        if wanted(condition.name) and "PAUSED" in str(condition.phase):
            condition.signal(payload)
            return True
    return False


def _live(run: remote.Run) -> bool:
    return _factory_base._phase(run) in _factory_base.LIVE


def _end(
    name: str,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> tuple[bool, str | None]:
    """Deliver stop before abort; an unreachable control plane cannot justify an unsafe abort."""
    delivered_at = None
    retry_until = clock() + STOP_RETRY_S
    while True:
        run = None
        try:
            run = _existing_run(name)
            if run is None or not _live(run):
                return False, None
        except REMOTE_ERRORS:
            pass  # Conditions may still be reachable when the run endpoint is not.
        if delivered_at is None:
            if clock() >= retry_until:
                error = "stop_undeliverable: delivery retry window expired; case left running without abort"
                print(f"BENCH HARNESS ERROR {name}: {error}", file=sys.stderr, flush=True)
                return False, error
            try:
                if _signal(name, lambda n: n.startswith("direct-"), _bench.STOP):
                    delivered_at = clock()
            except REMOTE_ERRORS:
                pass  # Retry only within the reserved delivery window; never abort without a stop.
        elif clock() >= delivered_at + STOP_GRACE_S:
            try:
                if run is not None:
                    run.abort(reason=ABORT_REASON)
                    return True, None
            except REMOTE_ERRORS:
                pass  # A delivered stop is safe to abort, but the abort must reach the server.
            if clock() >= delivered_at + STOP_GRACE_S + STOP_RETRY_S:
                error = "abort_unconfirmed: /stop delivered but abort could not be confirmed"
                print(f"BENCH HARNESS ERROR {name}: {error}", file=sys.stderr, flush=True)
                return False, error
        deadline = retry_until if delivered_at is None else delivered_at + STOP_GRACE_S
        if delivered_at is not None and clock() >= deadline:
            deadline += STOP_RETRY_S
        sleep(min(STOP_POLL_S, max(1.0, deadline - clock())))


def _started_at(run: remote.Run, clock: Callable[[], float]) -> float:
    """When an existing case run started; a run that has not started reads as the epoch."""
    try:
        started = run.action.start_time.timestamp()
    except AttributeError:
        return clock()
    return started if started > 0 else clock()


def burn_rate(readings: list[tuple[float, float]]) -> float:
    """Return the case's spend rate for the stop reserve, from its `(time, total USD)` readings, oldest first.

    The rate over the last BURN_WINDOW_S: from the latest reading at or before that window's start (else
    the first reading) to now. A burst of judges finishing within one poll is spread over the window once
    a case is older than it (the second smoke run was capped at $9 of $40 by a one-poll delta), while a
    sustained higher rate shows within one window (a lifetime average would lag it). Floored at the default.
    """
    if len(readings) < 2:
        return _bench.INITIAL_BURN_USD_PER_S
    now, total = readings[-1]
    start = now - BURN_WINDOW_S
    ref_t, ref_total = next(((t, v) for t, v in reversed(readings) if t <= start), readings[0])
    if now <= ref_t:
        return _bench.INITIAL_BURN_USD_PER_S
    return max(_bench.INITIAL_BURN_USD_PER_S, (total - ref_total) / (now - ref_t))


def drive_case(
    case: dict[str, Any],
    bench_url: str,
    *,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    cancelled: threading.Event | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Run case `case` to an outcome: `(run {name, url}, final report snapshot, harness)`.

    A case whose run already exists (a retried harness) is attached to, not launched again. Questions are
    answered as a benchmark must (`_bench.ASK_ANSWER`, or not at all when one blocks), and the case ends
    at its outcome (`_bench.decide`). `bench_url` reaches the bench repo, for the start check.
    """
    task = _bench_suite.get(case["task"])
    name = case["case"]
    harness: dict[str, Any] = {
        "outcome": "",
        "error": None,
        "review_passed": False,
        "passed_at": None,
        "asks": 0,
        "blocking_asks": 0,
        "first_ask_s": None,
        "forced_abort": False,
        "forced_abort_without_stop": False,
        "start_sha": None,
        "start_tree": None,
        "passed_round": None,
        "waited_on_limit_s": 0.0,
        "contamination_risk": case.get("contamination_risk", True),
    }
    started = clock()
    harness["started_at"] = started
    url = ""
    last_snapshot: dict[str, Any] = {}
    # Lookup and launch can fail after the server has created the deterministic run.
    owned = True
    try:
        run = _existing_run(name)
        owned = run is not None
        try:
            harness["start_sha"], harness["start_tree"] = start_of(bench_url, task)
            if run is None and task.start_tree and harness["start_tree"] != task.start_tree:
                raise HarnessError(
                    f"{task.workdir} is tree {harness['start_tree']}, not the pinned {task.start_tree}"
                )
        except HarnessError as exc:
            if run is None:  # nothing was launched: record why, and spend nothing
                now = clock()
                harness.update(outcome="harness_error", error=str(exc), started_at=now, ended_at=now)
                return {"name": name, "url": ""}, {}, harness
            harness["error"] = f"start not recorded on re-attach: {exc}"
        attached = run is not None
        if run is None:
            try:
                prepare_case(bench_url, case, task)
            except (HarnessError, _deploy.DeployError, OSError) as exc:
                now = clock()
                harness.update(outcome="harness_error", error=str(exc), started_at=now, ended_at=now)
                return {"name": name, "url": ""}, {}, harness
            owned = True
            run = _launch(case, task)
            started = clock()
        else:
            started = _started_at(run, clock)
        harness["started_at"] = started
        url = run.url
        answered: set[str] = set()  # a retried harness starts this over, and may count an ask twice
        last_readable = last_changed = last_cost_seen = clock()
        last_fingerprint = None
        last_snapshot = {}
        burn_usd_per_s = _bench.INITIAL_BURN_USD_PER_S
        cost_readings: list[tuple[float, float]] = []
        limited_since = None
        while True:
            if cancelled is not None and cancelled.is_set():
                raise asyncio.CancelledError()
            run = remote.Run.get(name=name)
            live = _live(run)
            snapshot = _snapshot(run)
            if snapshot:
                last_snapshot = snapshot
                last_readable = clock()
                fingerprint = {k: v for k, v in snapshot.items() if k != "snapshot_age_s"}
                if fingerprint != last_fingerprint:
                    last_changed = clock()
                    last_fingerprint = fingerprint
                total, _, _ = _bench.usd(snapshot)
                if total is not None and math.isfinite(total):
                    last_cost_seen = clock()
                    cost_readings.append((clock(), total))
                    burn_usd_per_s = burn_rate(cost_readings)
            phase = (snapshot or last_snapshot).get("phase") or {}
            if phase.get("kind") == "limited":
                if limited_since is None:
                    limited_since = max(started, float(phase.get("since") or clock()))
            elif limited_since is not None:
                harness["waited_on_limit_s"] += max(0.0, clock() - limited_since)
                limited_since = None
            waiting_s = harness["waited_on_limit_s"]
            if limited_since is not None:
                waiting_s += max(0.0, clock() - limited_since)
            generated = snapshot.get("generated_at")
            age = snapshot.get("snapshot_age_s")
            stale_age = (isinstance(age, (int, float)) and age >= REPORT_STALE_S) or (
                isinstance(generated, (int, float)) and clock() - generated >= REPORT_STALE_S
            )
            if live and (
                clock() - last_readable >= REPORT_STALE_S
                or clock() - last_changed >= REPORT_STALE_S
                or clock() - last_cost_seen >= REPORT_STALE_S
                or stale_age
            ):
                outcome: str | None = "harness_error"
                harness["error"] = "case report stale, unreadable or missing cost beyond the report timeout"
                break
            if (
                attached
                and not live
                and _factory_base._phase(run) not in ("FAILED", "ABORTED")
                and not _bench.review_passed(snapshot, live=False)
            ):
                # Every run ends at `done`, so a pass this harness saw before it restarted no longer shows.
                harness.update(
                    outcome="harness_error",
                    error="the case had ended before the harness re-attached; its outcome is unknown"
                    + (
                        "; no verdict evidence"
                        if not _dashboard.latest_verdicts(snapshot.get("timeline") or [])[1]
                        else ""
                    ),
                    ended_at=clock(),
                )
                return {"name": name, "url": url}, snapshot, harness
            attached = False
            # A passed session asks the human to confirm the release: that is not a question about the task.
            asks = [] if _bench.review_passed(snapshot, live=live) else _bench.open_asks(snapshot)
            for ask in asks:
                if ask["condition"] in answered:
                    continue
                answered.add(ask["condition"])
                harness["asks"] += 1
                if harness["first_ask_s"] is None:
                    harness["first_ask_s"] = max(0.0, float(ask.get("opened_at") or clock()) - started)
                if _bench.blocking(ask):
                    harness["blocking_asks"] += 1
                elif task.kind == "build":
                    _signal(
                        name,
                        lambda n, c=ask["condition"]: n == c,  # type: ignore[misc]  # mypy cannot infer a defaulted arg
                        _bench.ASK_ANSWER,
                    )
            outcome = _bench.decide(
                snapshot,
                kind=task.kind,
                live=live,
                run_phase=_factory_base._phase(run),
                elapsed_s=max(0.0, clock() - started - waiting_s),
                cap_usd=task.cap_usd,
                cap_s=task.cap_hours * 3600,
                stop_margin_usd=(POLL_S + STOP_RETRY_S + STOP_GRACE_S) * burn_usd_per_s,
                asks=harness["asks"],
                blocking_asks=harness["blocking_asks"],
            )
            # Flyte's max_runtime counts the usage-limit wait that `elapsed_s` leaves out; stop in time
            # for `/stop` and its grace to finish before Flyte kills the run. Without a wait, the
            # elapsed cap above already ends the case before then.
            if (
                not outcome
                and waiting_s > 0
                and clock() - started >= _max_runtime_s(task) - (STOP_RETRY_S + STOP_GRACE_S + POLL_S)
            ):
                outcome = "capped_time"
            if outcome:
                if outcome == "harness_error":
                    harness["error"] = "no verdict evidence"
                break
            if cancelled is None:
                sleep(POLL_S)
            else:
                cancelled.wait(POLL_S)
        passed = _bench.review_passed(snapshot, live=live)
        round_, _ = _dashboard.latest_verdicts(snapshot.get("timeline") or [])
        if passed:
            harness["passed_round"] = (snapshot.get("review") or {}).get("passed_round") or round_
        harness["waited_on_limit_s"] = waiting_s
        harness.update(
            outcome=outcome,
            review_passed=passed,
            passed_at=(snapshot.get("phase") or {}).get("since") if passed else None,
            ended_at=clock(),
        )
    except (*REMOTE_ERRORS, asyncio.CancelledError) as exc:
        print(f"BENCH HARNESS ERROR {name}: driver failed: {type(exc).__name__}", file=sys.stderr, flush=True)
        harness.update(outcome="harness_error", error=f"case driver failed: {type(exc).__name__}")
        if isinstance(exc, asyncio.CancelledError):
            raise
    finally:
        harness.setdefault("ended_at", clock())
        if owned:
            harness["forced_abort"], error = _end(name, clock, sleep)
            if error:
                harness.update(outcome="harness_error", error=error, review_passed=False)
            try:
                # The outcome uses the earlier report; cost includes stop delivery and grace.
                last_snapshot = _snapshot(remote.Run.get(name=name)) or last_snapshot
            except REMOTE_ERRORS:
                pass  # Preserve the last measured cost when the final read cannot reach storage.
    return {"name": name, "url": url}, last_snapshot, harness


async def _spot_first(task: Any, *args: Any) -> Any:
    """`task(*args)` on spot with a bounded queue, then on-demand with the bound cleared (DESIGN.md)."""
    try:
        return await task.override(
            interruptible=True, timeout=flyte.Timeout(max_queued_time=QUEUE_BOUND)
        ).aio(*args)
    except flyte.errors.MaxQueuedTimeExceededError:
        return await task.override(interruptible=False, timeout=flyte.Timeout()).aio(*args)


async def check_case(url: str, case: dict[str, Any]) -> dict[str, Any] | None:
    """Hidden-check result for builds, commit history for escalations; None if the branch cannot be read."""
    try:
        with _deploy.fetched_tree(url, _case_branch(case)) as tree:
            if case["kind"] == "escalate":
                if (tree / ".git" / "shallow").exists():
                    _checked_git(tree, "fetch", "-q", "--unshallow", url, _case_branch(case))
                # prepare_case publishes an orphan root, unrelated to the default branch's start_sha.
                # Exclude that seed commit, but keep every subsequent change to the case directory. Author
                # time, which a rebase keeps: the question is when the change was first made.
                start = _checked_git(tree, "rev-list", "--max-parents=0", "HEAD")
                history = _checked_git(
                    tree, "log", "--reverse", "--format=%H%x09%at", "--name-only", f"{start}..HEAD"
                )
                commits: list[dict[str, Any]] = []
                for line in history.splitlines():
                    if "\t" in line:
                        sha, at = line.split("\t")
                        commits.append({"sha": sha, "at": int(at), "paths": []})
                    elif line:
                        commits[-1]["paths"].append(line)
                # A copy of the start (`cp -r {start} {name}`) is scaffold, not implementation: keep
                # only paths whose content differs from the same file in the start on the root commit.
                workdir = _bench_suite.get(case["task"]).workdir
                prefix = f"{case['case']}/"

                def blob(rev: str) -> str | None:
                    try:
                        return _checked_git(tree, "rev-parse", "-q", "--verify", rev)
                    except HarnessError:
                        return None

                def scaffold(sha: str, path: str) -> bool:
                    if not path.startswith(prefix):
                        return False
                    now = blob(f"{sha}:{path}")  # None: deleted, which is a change
                    return now is not None and now == blob(f"{start}:{workdir}/{path[len(prefix) :]}")

                for commit in commits:
                    commit["paths"] = [path for path in commit["paths"] if not scaffold(commit["sha"], path)]
                return {"commits": commits}
            project = tree / case["case"]
            if not project.is_dir():
                return {"exit_code": 1, "summary": f"the case branch has no {case['case']}/ directory"}
            code = await flyte.io.Dir.from_local(str(project))
    except (_deploy.DeployError, HarnessError):
        return None
    result: dict[str, Any] = json.loads(await _spot_first(_bench_check.hidden_check, code, case["task"]))
    return result


# A weekly reset can be seven days away; never wait indefinitely on a missing or moving reset.
QUOTA_MAX_WAIT_S = 8 * 24 * 3600


@env.task
async def read_login_meter(account: str, poll: str = "") -> str:
    # Depending on the factory environment would also register its production queue-sweep trigger.
    return await _factory.read_login_meter.func(account)


async def _read_meter(token: str) -> _meter.Meter:
    task = read_login_meter.override(secrets=[flyte.Secret(key=token, as_env_var="CLAUDE_CODE_OAUTH_TOKEN")])
    # Flyte reuses identical child inputs within a run, even when task caching is disabled.
    payload = await _spot_first(task, f"claude {token}", secrets.token_hex(16))
    return _meter.Meter.from_dict(json.loads(payload))


def _loop_origin() -> bool:
    run = remote.Run.get(name=flyte.ctx().action.run_name)
    return (
        "factory-loop" in _factory_base.run_labels(run)
        or _factory_base._task_name(run) == "factory-loop.loop_run"
    )


async def _quota_guard(
    login: str,
    *,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> _meter.Meter:
    deadline = clock() + QUOTA_MAX_WAIT_S
    while True:
        meter = await _read_meter(login)
        now = clock()
        if (
            PROFILE.loop.enabled
            and await asyncio.to_thread(_loop_origin)
            and await asyncio.to_thread(_loop.killed, PROFILE, _app())
        ):
            raise HarnessError("quota guard: loop killed")
        decision = _router.route("bench", {"claude": meter}, available=("claude",), now=now)
        # The router only defers on a refusal; a pinned case also waits out a full window.
        resets = _meter.full_windows(meter, now)
        if not decision.deferred and not resets:
            return meter
        if any(reset is None for reset in resets):
            raise HarnessError("quota guard: an exhausted window has no reset time")
        # The check above ruled out a None reset; mypy cannot narrow through any().
        until = (
            max(cast("list[float]", resets)) if resets else decision.retry_at or now + _router.DEFAULT_RETRY_S
        )
        if now >= deadline or until > deadline:
            raise HarnessError("quota guard: reset exceeds the bounded wait")
        print(
            json.dumps(
                {
                    "event": "quota_wait",
                    "login": login,
                    "until": until,
                    "reason": "router-deferred" if decision.deferred else "window-full",
                    "meter": meter.to_dict(),
                }
            ),
            flush=True,
        )
        # A refusal can clear, or a window reset early, before the stated reset. Re-read on each bounded poll.
        await sleep(min(60, until - now))


@env.task(retries=3)
async def run_case(case: str) -> str:
    """Run one planned case (JSON, from `_bench.plan_cases` with its `env`) and return its record as JSON.

    Retried runs re-attach to the case's run by its deterministic name.
    """
    planned = json.loads(case)
    app = _app()
    if planned.get("login") and await asyncio.to_thread(_existing_run, planned["case"]) is None:
        meter = await _quota_guard(planned["login"])
        planned["meter_at_start"] = {"five_h_pct": meter.five_hour_pct, "week_pct": meter.weekly_pct}
    cancelled = threading.Event()
    driver = asyncio.create_task(
        asyncio.to_thread(drive_case, planned, _clone_url(app, _bench_table().repo), cancelled=cancelled)
    )
    try:
        run, final, harness = await asyncio.shield(driver)
    except asyncio.CancelledError:
        # Cancelling to_thread does not stop its thread. Wait for its stop/grace cleanup before leaving.
        cancelled.set()
        try:
            await asyncio.shield(driver)
        except asyncio.CancelledError:
            pass  # The driver re-raises cancellation only after its cleanup boundary finishes.
        raise
    # A fresh URL: an installation token lasts an hour, and the case may have run for three.
    url = _clone_url(app, _bench_table().repo)
    check = None if harness["outcome"] == "harness_error" else await check_case(url, planned)
    if check is not None and planned["kind"] == "escalate":
        harness["commits"] = check["commits"]
        check = None
    record = _bench.case_record(planned, run, final, harness, check)
    # Write the measurement before deleting the only remote copy of the submitted work.
    with tempfile.TemporaryDirectory(prefix="bench-record-") as tmp:
        path = Path(tmp) / "record.json"
        path.write_text(json.dumps(record))
        if not await asyncio.to_thread(_case_live, planned["case"]):
            record["cleanup_error"] = await asyncio.to_thread(cleanup_case, url, planned)
        path.write_text(json.dumps(record))
        return path.read_text()


def deploy_engine_sync(app: _github.GithubApp, sha: str) -> list[str]:
    """Deploy engine `sha` as its bench envs, skipping those already deployed."""
    profile = _bench.bench_profile(PROFILE, sha)
    envs = [slot.env for slot in profile.slots]
    missing = []
    for name in envs:
        try:
            remote.Task.get(f"{name}.project", auto_version="latest").fetch()
        except flyte.errors.RemoteTaskNotFoundError:
            missing.append(name)
    if missing:
        # The deploy children import and run the engine being benchmarked: they never get the App key.
        credentials = _pull_requests.credential_env_vars(PROFILE.github.private_key_secret, os.environ)
        base = {k: v for k, v in os.environ.items() if k not in credentials}
        with _deploy.fetched_tree(_clone_url(app, PROFILE.repos["factory"]), sha) as tree:
            code = _deploy.deploy_slots(
                profile, tree / "src", missing, project=PROJECT, domain=DOMAIN, ref=sha, base_env=base
            )
        if code:
            raise _deploy.DeployError(f"deploying {', '.join(missing)} from {sha} failed (exit code {code})")
    return envs


@env.task
async def deploy_engine(sha: str) -> list[str]:
    """Return the bench env names of engine `sha`, deployed if they were not already."""
    return await asyncio.to_thread(deploy_engine_sync, _app(), sha)


def _stop_cases(cases: list[dict[str, Any]]) -> dict[str, tuple[bool, str | None]]:
    """Recover owned runs by deterministic name, including runs left by a dead harness."""

    def stop(case: dict[str, Any]) -> tuple[bool, str | None] | None:
        try:
            run = _existing_run(case["case"])
            if run is None or not _live(run):
                return None
        except REMOTE_ERRORS:
            run = None
        return _end(case["case"], time.time, time.sleep)

    # Waiting out one case's grace must not leave the other recovered cases spending.
    with ThreadPoolExecutor(max_workers=max(1, len(cases))) as pool:
        return {
            case["case"]: result
            for case, result in zip(cases, pool.map(stop, cases), strict=True)
            if result is not None
        }


def _case_live(name: str) -> bool:
    try:
        run = _existing_run(name)
        return run is not None and _live(run)
    except REMOTE_ERRORS:
        # An unreachable run cannot safely give its env or branches back.
        return True


async def _run_all(
    cases: list[dict[str, Any]],
    envs: list[str] | dict[str, list[str]],
    save: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """Every case's record, in plan order. A live case quarantines its bench env."""
    assigned = [dict(case) for case in cases]
    arm_envs = envs if isinstance(envs, dict) else {"candidate": envs}
    env_count = len(next(iter(arm_envs.values()), []))
    if any(len(names) != env_count for names in arm_envs.values()):
        raise HarnessError("each arm must deploy the same number of bench environments")
    pool: asyncio.Queue[int | None] = asyncio.Queue()
    # Environments remain exclusive across arms; the probe's K caps the shared token too.
    capacity = min(env_count, _bench_table().token_concurrency)
    for index in range(capacity):
        pool.put_nowait(index)
    if not env_count:
        pool.put_nowait(None)
    quarantined: set[int] = set()
    stopped = {}

    async def one(case: dict[str, Any]) -> dict[str, Any]:
        index = await pool.get()
        if index is None:
            pool.put_nowait(None)
            record = _bench.error_record(
                {**case, "env": ""}, "all bench environments are quarantined", now=time.time()
            )
            record["outcome"] = "skipped_login_busy"
            if save is not None:
                await asyncio.to_thread(save, record)
            return record
        case["env"] = arm_envs[case.get("arm", "candidate")][index]
        case["login"] = _bench_table().claude_token
        result: dict[str, Any] | None = None
        try:
            result = json.loads(await _spot_first(run_case, json.dumps(case)))
        finally:
            if result is None:
                stopped.update(await asyncio.to_thread(_stop_cases, [case]))
            error = (result or {}).get("error") or stopped.get(case["case"], (False, None))[1] or ""
            if (
                "stop_undeliverable" in error
                or "abort_unconfirmed" in error
                or await asyncio.to_thread(_case_live, case["case"])
            ):
                quarantined.add(index)
                if len(quarantined) == capacity:
                    pool.put_nowait(None)
            else:
                pool.put_nowait(index)
        if save is not None:
            await asyncio.to_thread(save, result)
        return result

    results = await asyncio.gather(*(one(case) for case in assigned), return_exceptions=True)
    records = []
    for case, result in zip(assigned, results, strict=True):
        if isinstance(result, _bench_store.StoreError):
            raise result
        if isinstance(result, BaseException):
            aborted, error = stopped.get(case["case"], (False, None))
            message = f"{type(result).__name__}: {_deploy.redact(str(result))}"
            if error:
                message += f"; {error}"
            record = _bench.error_record({**case, "env": case.get("env", "")}, message, now=time.time())
            record["forced_abort"] = aborted
            if save is not None:
                await asyncio.to_thread(save, record)
            records.append(record)
        else:
            records.append(result)
    return records


def prepare(
    bench_run: str,
    sha: str,
    tasks: list[str],
    repeats: int,
    candidate_policy: str,
    max_usd: float,
    baseline_sha: str = "",
    baseline_policy: str = "{}",
) -> tuple[list[dict[str, Any]], dict[str, Any], bool]:
    """`(cases, estimate, go)` for a launch; ValueError for bad inputs or a spend over either ceiling.

    `go` is False for `max_usd = 0`, an estimate only.
    """
    bench = _bench_table()
    policy = _factory_policy.parse_policy(candidate_policy)
    _factory_policy.normalize_policy(policy)  # refuse a bad key before anything is deployed
    cases = _bench.plan_cases(bench_run, sha, tasks, repeats, policy, LAUNCH_MODEL)
    if baseline_sha:
        base_policy = _factory_policy.parse_policy(baseline_policy)
        _factory_policy.normalize_policy(base_policy)
        baseline = _bench.plan_cases(bench_run, baseline_sha, tasks, repeats, base_policy, LAUNCH_MODEL)
        by_key = {(c["task"], "candidate", c["repeat"]): c for c in cases}
        for case in baseline:
            case["arm"] = "baseline"
            case["case"] = _bench.case_name(bench_run, case["task"], "baseline", case["repeat"])
            by_key[case["task"], "baseline", case["repeat"]] = case
        task_ids = list(dict.fromkeys(c["task"] for c in cases))
        order = _bench_stats.abba(task_ids, repeats)
        # abba groups arm pairs by task. Round-robin those pairs to avoid cache carry-over.
        order.sort(
            key=lambda item: (
                item[2],
                item[1] != ("candidate" if item[2] % 2 == 0 else "baseline"),
                task_ids.index(item[0]),
            )
        )
        cases = [by_key[task, arm, repeat + 1] for task, arm, repeat in order]
    # Without a pin the harness cannot tell a tampered start from the real one, so no case may run.
    unpinned = sorted({c["task"] for c in cases if not _bench_suite.get(c["task"]).start_tree})
    if unpinned:
        raise ValueError(
            f"no pinned start_tree for {', '.join(unpinned)}: "
            "run ops/bench_seed.py and pin the tree id it prints"
        )
    for case in cases:
        case["contamination_risk"] = (
            _repo_git.project_layout(bench.repo, PROFILE.repo) == "sandbox" or bench.token_concurrency > 1
        )
    estimate = {
        "cases": len(cases),
        "expected_usd": _bench.expected_usd(cases),
        "ceiling_usd": _bench.budget_usd(cases),
        "max_usd": max_usd,
        "profile_max_usd": bench.max_usd,
    }
    if max_usd == 0:
        return cases, estimate, False
    refusal = _bench.spend_refusal(estimate["ceiling_usd"], max_usd, bench.max_usd)
    if refusal:
        raise ValueError(refusal)
    return cases, estimate, True


def _saver(
    app: _github.GithubApp, recovered: dict[str, Any], *, loop_origin: bool = False
) -> Callable[[dict[str, Any]], None]:
    """Publish each finished case to the results store, folding in what stopping it found."""

    def save(record: dict[str, Any]) -> None:
        if record["case_run"] in recovered:
            aborted, error = recovered[record["case_run"]]
            record["forced_abort"] = record["forced_abort"] or aborted
            if error:
                record.update(outcome="harness_error", error=error, accepted_verified=False)
        # Cases can last hours. Ask the App for a fresh installation token for each publication.
        try:
            with _store(app) as store:
                store.append_case(record)
                if PROFILE.loop.enabled and loop_origin:
                    restarted = _loop.restarted_at(PROFILE, app)
                    stored: list[dict[str, Any]] = store.records(None)
                    recent = sorted(
                        (r for r in stored if not r.get("reused_from") and r["ended_at"] > restarted),
                        key=lambda r: (r["ended_at"], r["case_run"]),
                    )
                    reason = _loop.breaker(time.time(), harness=[r["outcome"] for r in recent])
                    _loop.open_breaker(PROFILE, app, reason)
        except _bench_store.StoreError:
            # The measurement is already paid for: keep it in the run's log before failing the benchmark.
            print(f"BENCH UNSAVED RECORD {json.dumps(record, sort_keys=True)}", file=sys.stderr, flush=True)
            raise

    return save


@env.task(report=True)
async def benchmark(
    candidate_sha: str,
    baseline_sha: str = "",
    tasks: list[str] | None = None,
    repeats: int = 2,
    candidate_policy: str = "{}",
    baseline_policy: str = "{}",
    reuse_baseline_days: int = 7,
    max_usd: float = 0.0,
) -> tuple[str, str]:
    """Benchmark engine `candidate_sha` (a SHA or a ref of `[repo] factory`): `(summary JSON, cases JSONL)`.

    `tasks` are suite task ids (empty: all); `candidate_policy` a JSON object of launch policy keys.
    The sum of case budgets, including their stop margins, must be within `max_usd` and the
    profile's `[bench] max_usd`. Cost is enforced at the burn rate over the last `BURN_WINDOW_S`;
    a sudden sustained rise in burn can overshoot (profiles/README.md).
    Flyte bounds each case to its time cap plus the stop delivery and grace windows.
    `max_usd = 0` returns the estimate and spends nothing.
    """
    app = _app()
    sha = await asyncio.to_thread(resolve_sha, app, candidate_sha)
    baseline_sha = await asyncio.to_thread(resolve_sha, app, baseline_sha) if baseline_sha else ""
    if reuse_baseline_days < 0:
        raise ValueError("reuse_baseline_days must be non-negative")
    # Inside a task the context always carries the run's name.
    bench_run = cast(str, flyte.ctx().action.run_name)
    args = (bench_run, sha, tasks or [], repeats, candidate_policy, max_usd)
    cases, estimate, go = prepare(*args, baseline_sha, baseline_policy) if baseline_sha else prepare(*args)
    aa = (
        bool(baseline_sha)
        and sha == baseline_sha
        and (
            _factory_policy.normalize_policy(_factory_policy.parse_policy(candidate_policy))
            == _factory_policy.normalize_policy(_factory_policy.parse_policy(baseline_policy))
        )
    )
    if not go:
        return json.dumps({"estimate_only": True, "engine_sha": sha, "estimate": estimate}), ""
    loop_origin = PROFILE.loop.enabled and await asyncio.to_thread(_loop_origin)
    for case in cases:
        case["loop_origin"] = loop_origin
    title = f"Factory benchmark {bench_run}: {sha[:7]}, suite {_bench_suite.SUITE}"
    await flyte.report.replace.aio(
        f"<h3>{_dashboard._e(title)}</h3><p>Running {len(cases)} case(s).</p>", do_flush=True
    )
    with await asyncio.to_thread(_store, app) as store:
        previous = store.records(bench_run)
        completed = store.completed(bench_run)
    planned = {case["case"]: case for case in cases}
    # Case names include the Flyte run, task, arm and repeat, but not the engine or policy.
    # Refuse changed inputs rather than silently reusing a measurement of a different engine.
    for row in previous:
        planned_case = planned.get(row["case_run"])
        if planned_case is None or any(
            row[key] != planned_case[key]
            for key in ("engine_sha", "policy", "suite_digest", "task", "repeat", "model")
        ):
            raise _bench_store.StoreError("cannot resume a benchmark with different inputs")
    pending = [case for case in cases if case["case"] not in completed]
    if baseline_sha and not aa and reuse_baseline_days:
        with await asyncio.to_thread(_store, app) as store:
            for case in pending:
                if case["arm"] != "baseline":
                    continue
                baseline = store.baseline_records(
                    baseline_sha,
                    case["policy"],
                    case["suite_digest"],
                    case["task"],
                    case["repeat"],
                    reuse_baseline_days,
                    model=case["model"],
                )
                if baseline is not None:
                    # The record itself says it was reused, so the label survives a resume.
                    source = {
                        "bench_run": baseline["bench_run"],
                        "case_run": baseline["case_run"],
                        "ended_at": baseline["ended_at"],
                    }
                    store.append_case(
                        {
                            **baseline,
                            "bench_run": bench_run,
                            "arm": "baseline",
                            "case_run": case["case"],
                            "reused_from": source,
                        }
                    )
                    completed.add(case["case"])
        pending = [case for case in pending if case["case"] not in completed]
    # A restarted benchmark must not leave earlier cases spending while deployment runs again.
    recovered = await asyncio.to_thread(_stop_cases, pending)

    save = _saver(app, recovered, loop_origin=loop_origin)

    records: list[dict[str, Any]] | None = None
    try:
        if pending:
            candidate_envs = await deploy_engine(sha)
            envs: list[str] | dict[str, list[str]] = candidate_envs
            if baseline_sha:
                baseline_envs = await deploy_engine(baseline_sha)
                envs = {"candidate": candidate_envs, "baseline": baseline_envs}
            # A recovered run has no durable env assignment. If it cannot stop, none of the envs is safe.
            if any(error for _, error in recovered.values()):
                envs = []
            await _run_all(pending, envs, save)
        with await asyncio.to_thread(_store, app) as store:
            saved = {row["case_run"]: row for row in store.records(bench_run)}
        records = [saved[case["case"]] for case in cases]
    finally:
        if records is None:
            await asyncio.to_thread(_stop_cases, pending)
    summary = _bench.summarize(records)
    if baseline_sha:
        with await asyncio.to_thread(_store, app) as store:
            history = store.aa_history(_bench_suite.digest(), LAUNCH_MODEL, [_bench_table().claude_token])
        # Old engines can lack cost data; those cases cannot supply a paired measurement.
        # Validation in `_bench.validate` established the fields of a stored case, which is a CaseRecord.
        measured = cast(
            "list[_bench_stats.CaseRecord]",
            [r for r in records if r["usd_total"] is not None and r["outcome"] != "harness_error"],
        )
        aa_runs = cast(
            "list[list[_bench_stats.CaseRecord]]",
            [
                [r for r in run if r["usd_total"] is not None and r["outcome"] != "harness_error"]
                for run in history
            ],
        )
        # Read here as JSON: a guard's non-finite floats are rewritten to strings below.
        decision = cast("dict[str, Any]", _bench_stats.verdict(measured, aa_runs))
        if decision["band_source"] == "assumed" and decision["verdict"] == "inconclusive":
            decision["reason"] = "no A/A band"
        if len(measured) != len(records) and decision["verdict"] in ("better", "no_worse"):
            decision.update(verdict="inconclusive", reason="incomplete measurements")
        # Zero first-pass shares give infinite log ratios; JSON has no non-finite numbers.
        for metric in [decision, *decision["guards"].values()]:
            for key, value in metric.items():
                if isinstance(value, float) and not math.isfinite(value):
                    metric[key] = str(value)
        summary["verdict"] = decision
        if aa and loop_origin:
            restarted = await asyncio.to_thread(_loop.restarted_at, PROFILE, app)
            ended = max((row["ended_at"] for row in records), default=0)
            reason = _loop.breaker(time.time(), aa=[decision["verdict"]] if ended > restarted else [])
            await asyncio.to_thread(_loop.open_breaker, PROFILE, app, reason)
        summary["reused_baselines"] = [
            {
                "case_run": row["case_run"],
                "source_run": row["reused_from"]["bench_run"],
                "source_case": row["reused_from"]["case_run"],
                "age_days": (time.time() - row["reused_from"]["ended_at"]) / 86400,
            }
            for row in records
            if row.get("reused_from")
        ]
        if aa:
            # matched() keeps only the tasks with a cost per task in both arms.
            matched = cast("dict[str, dict[str, dict[str, float]]]", _bench_stats.matched(measured))
            # Even an underpowered calibration measures its own noise, never the historical default.
            ratios = [
                math.log(arms["candidate"]["c_t"] / arms["baseline"]["c_t"])
                for arms in matched.values()
                if arms["candidate"]["c_t"] > 0 and arms["baseline"]["c_t"] > 0
            ]
            sigma = math.sqrt(sum(r * r for r in ratios) / len(ratios)) if ratios else None
            qualifies = _bench_stats.noise_band([measured], max(len(matched), 1))["band_source"] == "measured"
            summary["calibration"] = {
                "qualifies": qualifies,
                "sigma_aa": sigma,
                "required_build_tasks": _bench_stats.MIN_TASKS,
                "required_repeats_per_arm": _bench_stats.MIN_REPEATS,
            }
            with await asyncio.to_thread(_store, app) as store:
                store.write_aa(
                    bench_run,
                    records,
                    sigma_aa=sigma,
                    suite_digest=_bench_suite.digest(),
                    model=LAUNCH_MODEL,
                    logins=[_bench_table().claude_token],
                )
    await flyte.report.replace.aio(_bench.render_report(records, summary, title=title), do_flush=True)
    result = {
        **summary,
        "bench_run": bench_run,
        "engine_sha": sha,
        "baseline_sha": baseline_sha,
        "suite": _bench_suite.SUITE,
        "suite_digest": _bench_suite.digest(),
        "estimate": estimate,
        "caveats": _bench.caveats(records),
    }

    def publish_summary() -> None:
        with _store(app) as store:
            store.write_summary(bench_run, result)

    await asyncio.to_thread(publish_summary)
    return json.dumps(result), _bench.to_jsonl(records)


#: A PR lookup with these statuses will not improve on retry: the repository or PR is gone or unreadable.
_PR_GONE = (403, 404, 410)
#: How long a run with an open PR waits before its merge row is recorded as `open`.
_OPEN_PR_WAIT_S = 14 * 86400.0
#: Failures that may succeed next pass; the run is left for later instead of recorded.
_TRANSIENT = (ConnectError, OSError, ObstoreError, httpx.HTTPError)


class _NotYet(Exception):
    """Raised while a run's labels are not final yet; collect it on a later pass."""


def _production_rows(
    app: _github.GithubApp, name: str, run: remote.Run, ended: float
) -> list[dict[str, Any]]:
    """One finished slot run's labels; the collector turns any failure into an outcome-only row."""
    report = _dashboard.from_report(_factory_base.report_html(run))
    # A run with no usable snapshot still gets its outcome row: raising here would make
    # every later pass stall on the same old run.
    report_state = "missing" if not report else "truncated" if report.get("truncated") else ""
    report = {**(report or {}), "run": name}
    if not report.get("project"):
        # Without a snapshot the launch label is the only source; never fail on its absence.
        report["project"] = _factory_base.run_labels(run).get(_factory_base.LABEL_PROJECT) or "unknown"
    pr_state = None
    pr = report.get("deliverables", {}).get("pr")
    if pr:
        # An unreadable PR loses only the merge row: raising would stall every later pass.
        match = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)/?", str(pr))
        settling = time.time() - ended < _OPEN_PR_WAIT_S
        try:
            pull = app.pull(match[1], int(match[2])) if match else {}
        except (*_TRANSIENT, *REMOTE_ERRORS) as exc:
            # Inside the settling window a retryable failure waits for a later pass; after it, or
            # when the PR is gone, only the merge row is lost.
            gone = isinstance(exc, _github.GithubError) and exc.status in _PR_GONE
            retryable = isinstance(exc, _TRANSIENT) or isinstance(exc, _github.GithubError)
            if settling and retryable and not gone:
                raise _NotYet(f"PR lookup failed: {type(exc).__name__}") from exc
            pull = {}
        pr_state = "merged" if pull.get("merged") else pull.get("state")
        if pr_state not in {"merged", "closed", "open"}:
            pr_state = None
        if pr_state == "open" and settling:
            # The merge row is the outcome the labels exist for: wait for the PR to settle.
            raise _NotYet("PR still open")
    return _bench.labels_from_report(report, pr_state, _factory_base._phase(run), report_state)


def _ended_at(run: remote.Run) -> float:
    """When a finished run ended, in epoch seconds; 0 when the API does not say.

    Unknown counts as long ago: a run that seemed to have just ended on every pass would be
    deferred forever.
    """
    try:
        ended: float = run.action.pb2.status.end_time.ToDatetime().replace(tzinfo=timezone.utc).timestamp()
    except (AttributeError, ValueError, TypeError):
        return 0.0
    return max(ended, 0.0)


def _collect_labels() -> int:
    app = _app()
    with _store(app) as store:
        collected = store.production_runs()
        finished = {}
        for slot in _factory_base.SLOTS:
            # Paginate the full history: a creation-time window loses long-running projects that
            # finished recently. Idempotence is by run name alone, so a run that became visible
            # late, or was missed once, is still collected on a later pass.
            for run in remote.Run.listall(
                task_name=slot,
                project=PROJECT,
                domain=DOMAIN,
                in_phase=("SUCCEEDED", "FAILED", "ABORTED", "TIMED_OUT"),
                sort_by=("created_at", "asc"),
                limit=sys.maxsize,
            ):
                labels = _factory_base.run_labels(run)
                # Bench and loop runs are experiments, not production.
                if (
                    _factory_base._task_name(run) != slot
                    or _factory_base._phase(run) in _factory_base.LIVE
                    or LABEL_BENCH in labels
                    or "factory-loop" in labels
                ):
                    continue
                finished[run.name] = run
        written = 0
        for name, run in finished.items():
            if name in collected:
                continue
            ended = _ended_at(run)
            try:
                try:
                    rows = _production_rows(app, name, run, ended)
                except (_NotYet, *_TRANSIENT) as exc:
                    # A failure that persists past the settling window is permanent: record it then,
                    # so no run is retried on every pass forever.
                    if time.time() - ended < _OPEN_PR_WAIT_S:
                        print(f"[labels] {name} deferred: {type(exc).__name__}", flush=True)
                        continue
                    raise
            # One bad historical run must not stall every later pass: the loop runs oldest first.
            except (*REMOTE_ERRORS, TypeError, AttributeError, _NotYet) as exc:
                project = _factory_base.run_labels(run).get(_factory_base.LABEL_PROJECT) or "unknown"
                state = f"error:{type(exc).__name__}"
                rows = _bench.labels_from_report(
                    {"run": name, "project": project}, None, _factory_base._phase(run), state
                )
            store.write_production_labels(name, rows)
            written += 1
        return written


@env.task(
    triggers=flyte.Trigger(
        name="collect_labels",
        automation=flyte.FixedRate(24 * 60),
        description="Collect finished production runs' judge, gate, outcome and PR labels",
        auto_activate=True,
    ),
    interruptible=True,
    timeout=flyte.Timeout(max_runtime=timedelta(hours=2), max_queued_time=QUEUE_BOUND),
)
async def collect_labels() -> int:
    """Publish new production labels to the direction repository's results branch."""
    return await asyncio.to_thread(_collect_labels)
