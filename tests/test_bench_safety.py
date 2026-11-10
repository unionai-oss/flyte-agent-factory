"""Regression coverage for benchmark ownership, stop delivery and report freshness."""

import asyncio
import dataclasses
import json
import sys
import time

import pytest
from flyte import remote

from flyte_agent_factory import _bench, _bench_flow, _bench_suite, _profile
from tests.test_bench import profile
from tests.test_bench_flow import git, planned, snapshot, started
from tests.test_bench_flow import world as world


@pytest.mark.parametrize("failure", [OSError("remote unavailable"), asyncio.CancelledError()])
def test_drive_failure_stops_the_owned_run(world, monkeypatch, failure):
    case = planned()
    started(world, case, snapshot("building"))
    get = remote.Run.get
    calls = 0

    def failing_get(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise failure
        return get(*args, **kwargs)

    monkeypatch.setattr(remote.Run, "get", failing_get)
    if isinstance(failure, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            world.drive(case)
    else:
        _, _, harness = world.drive(case)
        assert harness["outcome"] == "harness_error"
        assert "OSError" in harness["error"]
    assert world.events == [("signal", "direct-a1-001", "/stop")]
    assert world.fake.runs[case["case"]].phase == "SUCCEEDED"


@pytest.mark.parametrize("mode", ["missing_cost", "unchanged", "aging"])
def test_readable_but_unusable_reports_stop_the_case(world, mode):
    case = planned()
    world.refresh_reports = False

    def report(w):
        snap = snapshot("building", usd=(None, None) if mode == "missing_cost" else (0.0, 0.0))
        if mode == "missing_cost":
            snap["generated_at"] = w.t
        elif mode == "aging":
            snap["snapshot_age_s"] = w.t - 1_000_000
        else:
            snap["generated_at"] = 1_000_000
        w.report(case["case"], snap)

    world.script = [lambda w: (w.open(case["case"], "direct-a1-001"), report(w))]
    world.script += [report] * 10
    _, _, harness = world.drive(case)
    assert harness["outcome"] == "harness_error"
    assert harness["ended_at"] - harness["started_at"] <= _bench_flow.REPORT_STALE_S + 2 * _bench.POLL_S
    assert world.events == [("signal", "direct-a1-001", "/stop")]


def test_margin_reserves_poll_delivery_and_grace():
    assert _bench.STOP_MARGIN_USD == _bench.INITIAL_BURN_USD_PER_S * (
        _bench.POLL_S + _bench_flow.STOP_RETRY_S + _bench_flow.STOP_GRACE_S
    )


def test_live_verdict_rows_without_recorded_pass_do_not_accept():
    snap = snapshot("done")
    snap["review"].pop("passed_round", None)
    assert not _bench.review_passed(snap, live=True)


def test_stop_retries_only_within_delivery_budget_without_aborting(world, capsys):
    case = planned()
    started(world, case, snapshot("building", usd=(100.0, 0.0)))
    # No condition ever becomes deliverable.
    world.script[0] = lambda w: w.report(case["case"], snapshot("building", usd=(100.0, 0.0)))
    _, _, harness = world.drive(case)
    assert not harness["forced_abort"] and not harness["forced_abort_without_stop"]
    assert harness["outcome"] == "harness_error" and "stop_undeliverable" in harness["error"]
    assert world.fake.runs[case["case"]].phase == "RUNNING"
    assert world.t - harness["ended_at"] == _bench_flow.STOP_RETRY_S
    assert "stop_undeliverable" in capsys.readouterr().err


def test_stop_after_delivery_window_is_not_attempted(world):
    case = planned()
    world.script = [lambda w: w.report(case["case"], snapshot("building", usd=(100.0, 0.0)))]
    world.script += [lambda w: None] * (int(_bench_flow.STOP_RETRY_S / _bench_flow.STOP_POLL_S) + 1)
    world.script += [lambda w: w.open(case["case"], "direct-late")]
    _, _, harness = world.drive(case)
    assert world.events == []
    assert not harness["forced_abort"]
    assert "stop_undeliverable" in harness["error"]
    assert world.fake.runs[case["case"]].phase == "RUNNING"


def test_seed_initializes_an_empty_bare_repo_idempotently(tmp_path, monkeypatch, capsys):
    from ops import bench_seed

    repo = tmp_path / "empty.git"
    repo.mkdir()
    git(repo, "init", "--bare", "-q", "-b", "trunk")
    selected = profile()
    assert selected.bench is not None
    selected = dataclasses.replace(selected, bench=dataclasses.replace(selected.bench, repo=repo.as_uri()))
    monkeypatch.setattr(_profile, "load", lambda *args: selected)
    monkeypatch.setattr(sys, "argv", ["bench_seed.py", "--profile", "unused"])
    bench_seed.main()
    first = git(repo, "rev-parse", "HEAD")
    task = _bench_suite.get("bugfix-shortlinks")
    assert git(repo, "rev-parse", f"HEAD:{task.workdir}") == task.start_tree
    bench_seed.main()
    assert git(repo, "rev-parse", "HEAD") == first
    # One "<task> <tree>" line per task, per run (suite v2): the second run prints exactly what the first did.
    lines = capsys.readouterr().out.splitlines()
    runs = [lines[: len(lines) // 2], lines[len(lines) // 2 :]]
    assert runs[0] == runs[1] and f"bugfix-shortlinks {task.start_tree}" in runs[0]


def test_a_fresh_case_is_not_cost_capped():
    assert (
        _bench.decide(
            snapshot("building", usd=(0.0, 0.0)),
            kind="build",
            live=True,
            elapsed_s=0,
            cap_usd=40,
            cap_s=10800,
            asks=0,
            blocking_asks=0,
        )
        is None
    )


def test_restart_stops_existing_runs_by_deterministic_name(world, monkeypatch):
    from datetime import datetime, timezone

    case = planned()
    run = world.fake.add(case["case"], "bench.project")
    run.action.start_time = datetime.fromtimestamp(world.t, timezone.utc)
    world.open(case["case"], "direct-recovered")
    monkeypatch.setattr(time, "time", world.clock)
    monkeypatch.setattr(time, "sleep", world.sleep)
    _bench_flow._stop_cases([case])
    assert world.events == [("signal", "direct-recovered", "/stop")]
    assert run.phase == "SUCCEEDED" and not world.fake.created


def test_exhausted_case_task_retries_stop_the_live_case(world, monkeypatch):
    from datetime import datetime, timezone

    case = planned()
    run = world.fake.add(case["case"], "bench.project")
    run.action.start_time = datetime.fromtimestamp(world.t, timezone.utc)
    world.open(case["case"], "direct-orphan")
    monkeypatch.setattr(time, "time", world.clock)
    monkeypatch.setattr(time, "sleep", world.sleep)

    async def exhausted(*args):
        raise RuntimeError("retries exhausted")

    monkeypatch.setattr(_bench_flow, "_spot_first", exhausted)
    records = asyncio.run(_bench_flow._run_all([case], [case["env"]]))
    assert records[0]["outcome"] == "harness_error"
    assert "retries exhausted" in records[0]["error"]
    assert world.events == [("signal", "direct-orphan", "/stop")]
    assert run.phase == "SUCCEEDED"


def test_cancelling_run_case_waits_for_driver_cleanup(monkeypatch):
    import threading

    entered = threading.Event()
    cleaned = threading.Event()
    monkeypatch.setattr(_bench_flow, "_app", lambda: object())
    monkeypatch.setattr(_bench_flow, "_clone_url", lambda *args: "unused")
    monkeypatch.setattr(_bench_flow, "_bench_table", lambda: profile().bench)

    def driver(*args, cancelled):
        entered.set()
        assert cancelled.wait(5), "run_case did not signal cancellation to its driver"
        cleaned.set()
        raise asyncio.CancelledError()

    monkeypatch.setattr(_bench_flow, "drive_case", driver)

    async def cancel():
        task = asyncio.create_task(_bench_flow.run_case.func('{"task": "bugfix-shortlinks"}'))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned.is_set()

    asyncio.run(cancel())


@pytest.mark.parametrize("cost", [(None, 0.0), (0.0, None)])
def test_partial_cost_is_missing(cost):
    from tests.test_bench import snapshot as full_snapshot

    assert _bench.usd(full_snapshot(usd=cost))[0] is None


@pytest.mark.parametrize("missing", [0, 1])
def test_partial_cost_updates_do_not_reset_watchdog(world, missing):
    case = planned()

    def report(w):
        cost: list[float | None] = [0.0, 0.0]
        cost[1 - missing] = (w.t - 1_000_000) / 10000
        cost[missing] = None
        w.report(case["case"], snapshot("building", usd=cost))

    world.script = [lambda w: (w.open(case["case"], "direct-partial"), report(w))]
    world.script += [report] * 10
    _, _, harness = world.drive(case)
    assert harness["outcome"] == "harness_error"
    assert "missing cost" in harness["error"]
    assert harness["ended_at"] - harness["started_at"] == _bench_flow.REPORT_STALE_S
    assert world.events == [("signal", "direct-partial", "/stop")]


def test_initial_rate_matches_case_estimate():
    assert _bench.INITIAL_BURN_USD_PER_S == 15 / (3 * 3600)


def test_measured_rate_uses_last_two_readings_and_can_fall(world):
    case = planned()
    started(world, case, snapshot("building", usd=(20.0, 0.0)), snapshot("building", usd=(20.6, 0.0)))

    def slower_progress(w):
        w.t += 3600
        w.report(case["case"], snapshot("building", usd=(28.0, 0.0)))

    world.script += [slower_progress, lambda w: w.report(case["case"], snapshot("done", usd=(28.06, 0.0)))]
    _, _, harness = world.drive(case)
    assert harness["outcome"] == "accepted"
    assert harness["ended_at"] - harness["started_at"] == 3600 + 4 * _bench_flow.POLL_S


def test_measured_rate_reserves_full_stop_window(world):
    case = planned()
    started(world, case, snapshot("building", usd=(10.0, 0.0)), snapshot("building", usd=(12.0, 0.0)))
    _, _, harness = world.drive(case)
    assert harness["outcome"] == "capped_cost"
    assert harness["ended_at"] - harness["started_at"] == 2 * _bench_flow.POLL_S


@pytest.mark.parametrize("recorded_round", [1, 3])
def test_pass_must_match_recorded_round(recorded_round):
    snap = snapshot("done", scores=[2])
    snap["review"]["passed_round"] = recorded_round
    assert not _bench.review_passed(snap, live=True)


def test_recorded_pass_without_timeline_verdicts_is_rejected():
    snap = snapshot("done", scores=[2])
    snap["timeline"] = []
    assert not _bench.review_passed(snap, live=True)


@pytest.mark.parametrize("stop_error", ["stop_undeliverable", "abort_unconfirmed", ""])
def test_live_or_undeliverable_case_quarantines_login(world, monkeypatch, stop_error):
    cases = [planned(case=f"quarantine-{i}") for i in range(3)]
    launched = []

    async def run(task, payload):
        case = json.loads(payload)
        launched.append(case["case"])
        run = world.fake.add(case["case"], "bench.project")
        if stop_error:
            run.phase = "SUCCEEDED"
        record = _bench.error_record(case, stop_error or "driver returned", now=world.t)
        return json.dumps(record)

    monkeypatch.setattr(_bench_flow, "_spot_first", run)
    records = asyncio.run(_bench_flow._run_all(cases, ["bench-only"]))
    assert launched == [cases[0]["case"]]
    assert [r["outcome"] for r in records] == ["harness_error", "skipped_login_busy", "skipped_login_busy"]
    for record in records:
        _bench.validate(record)


def test_run_case_keeps_branches_while_run_is_live(world, monkeypatch):
    case = planned()
    world.fake.add(case["case"], "bench.project")
    monkeypatch.setattr(_bench_flow, "_app", lambda: object())
    monkeypatch.setattr(_bench_flow, "_clone_url", lambda *args: "unused")
    monkeypatch.setattr(_bench_flow, "_bench_table", lambda: profile().bench)
    harness = dict(
        outcome="harness_error",
        error="stop_undeliverable",
        review_passed=False,
        started_at=world.t,
        ended_at=world.t,
        asks=0,
        blocking_asks=0,
        forced_abort=False,
    )
    monkeypatch.setattr(
        _bench_flow, "drive_case", lambda *args, **kwargs: ({"name": case["case"], "url": ""}, {}, harness)
    )
    cleanups = []
    monkeypatch.setattr(_bench_flow, "cleanup_case", lambda *args: cleanups.append(args))
    record = json.loads(asyncio.run(_bench_flow.run_case.func(json.dumps(case))))
    assert record["outcome"] == "harness_error"
    assert cleanups == []


@pytest.mark.parametrize("usd", [(0.0, 0.0), (0.01, 0.0)])
def test_slow_measured_rate_keeps_default_floor(world, usd):
    case = planned()
    started(
        world,
        case,
        snapshot("building", usd=(38.0, 0.0)),
        snapshot("building", usd=(38.0 + usd[0], usd[1])),
        snapshot("building", usd=(38.5, 0.0)),
    )
    _, _, harness = world.drive(case)
    assert harness["outcome"] == "capped_cost"
    assert harness["ended_at"] - harness["started_at"] == 3 * _bench_flow.POLL_S


def test_empty_new_review_round_cannot_reuse_old_pass():
    snap = snapshot("done")
    snap["timeline"].append({"icon": "🔍", "text": "QA round 2: 3 reviewers"})
    assert not _bench.review_passed(snap, live=True)


def test_quarantine_does_not_block_other_safe_logins(world, monkeypatch):
    cases = [planned(case=f"mixed-{i}") for i in range(5)]
    launched = []

    async def run(task, payload):
        case = json.loads(payload)
        launched.append(case["env"])
        if case["env"] == "unsafe":
            world.fake.add(case["case"], "bench.project")
        await asyncio.sleep(0)
        return json.dumps(_bench.error_record(case, "driver returned", now=world.t))

    monkeypatch.setattr(_bench_flow, "_spot_first", run)
    records = asyncio.run(_bench_flow._run_all(cases, ["unsafe", "safe"]))
    assert launched.count("unsafe") == 1
    assert launched.count("safe") == 4
    assert len(records) == 5
    assert all(r["outcome"] == "harness_error" for r in records)


def test_exhausted_task_with_undeliverable_stop_quarantines_login(world, monkeypatch):
    from datetime import datetime, timezone

    cases = [planned(case=f"exhausted-{i}") for i in range(2)]
    run = world.fake.add(cases[0]["case"], "bench.project")
    run.action.start_time = datetime.fromtimestamp(world.t, timezone.utc)
    monkeypatch.setattr(time, "time", world.clock)
    monkeypatch.setattr(time, "sleep", world.sleep)
    launched = []

    async def exhausted(task, payload):
        launched.append(json.loads(payload)["case"])
        raise RuntimeError("task retries exhausted")

    monkeypatch.setattr(_bench_flow, "_spot_first", exhausted)
    records = asyncio.run(_bench_flow._run_all(cases, ["bench-only"]))
    assert launched == [cases[0]["case"]]
    assert "stop_undeliverable" in records[0]["error"]
    assert records[1]["outcome"] == "skipped_login_busy"
    assert world.t - 1_000_000 == _bench_flow.STOP_RETRY_S
    assert run.phase == "RUNNING"


def test_recorded_pass_with_only_round_header_is_rejected():
    snap = snapshot("done")
    snap["timeline"] = [{"icon": "🔍", "text": "QA round 1: 3 reviewers"}]
    assert not _bench.review_passed(snap, live=True)


@pytest.mark.parametrize("timeline", [[], [{"icon": "🔍", "text": "QA round 1: 3 reviewers"}]])
def test_missing_verdict_evidence_is_a_harness_error(world, timeline):
    case = planned()
    snap = snapshot("done")
    snap["timeline"] = timeline
    started(world, case, snap)
    _, _, harness = world.drive(case)
    assert harness["outcome"] == "harness_error"
    assert harness["error"] == "no verdict evidence"
    assert not harness["review_passed"]
    assert world.events == [("signal", "direct-a1-001", "/stop")]


def test_failed_aborts_are_bounded_by_shutdown_window(world, monkeypatch, capsys):
    case = planned()
    world.obeys_stop = False
    started(world, case, snapshot("done"))
    attempts = []

    def unavailable(self, reason):
        attempts.append(world.t)
        raise OSError("unavailable")

    from tests.fake_flyte import FakeRun

    monkeypatch.setattr(FakeRun, "abort", unavailable)
    _, _, harness = world.drive(case)
    assert harness["outcome"] == "harness_error"
    assert "abort_unconfirmed" in harness["error"]
    assert "abort_unconfirmed" in capsys.readouterr().err
    assert attempts
    assert world.t - harness["ended_at"] <= _bench_flow.STOP_GRACE_S + _bench_flow.STOP_RETRY_S
    assert not harness["forced_abort"]
    assert world.events == [("signal", "direct-a1-001", "/stop")]


def test_case_launch_has_platform_runtime_limit(world, monkeypatch):
    from datetime import timedelta
    from types import SimpleNamespace

    case = planned()
    overrides = []

    def override(**kwargs):
        overrides.append(kwargs)
        return "bench.project"

    monkeypatch.setattr(remote.Task, "get", lambda *args, **kwargs: SimpleNamespace(override=override))
    _bench_flow._launch(case, _bench_suite.get(case["task"]))
    assert overrides[0]["timeout"].max_runtime == timedelta(
        hours=_bench_suite.get(case["task"]).cap_hours,
        seconds=_bench_flow.STOP_GRACE_S + _bench_flow.STOP_RETRY_S,
    )
    assert len(world.fake.created) == 1
    assert world.fake.created[0]["task"] == "bench.project"
