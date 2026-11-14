"""Production label collection, using a local results branch and offline service doubles."""

import asyncio
import copy
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast

import flyte
import pytest
from flyte import remote

from flyte_agent_factory import (
    _bench,
    _bench_flow,
    _bench_store,
    _dashboard,
    _factory_base,
    _gate_base,
    _session_config,
    _timeline,
)
from flyte_agent_factory._github import GithubApp
from tests.test_bench_store import git
from tests.test_bench_store import origin as origin

SHA = "a" * 40


@pytest.fixture
def report():
    return {
        "run": "production-run",
        "project": "sample",
        "engine_sha": SHA,
        "phase": {"kind": "done", "round": 2},
        "review": {"passed": True, "round": 2},
        "metrics": {
            "labels": [
                {"kind": "judge", "round": 1, "name": "qa", "verdict": "FAIL", "file": ""},
                {
                    "kind": "judge",
                    "round": 1,
                    "name": "qa",
                    "verdict": "FAIL",
                    "file": "src/app.py",
                    "line": 12,
                    "title": "Wrong result",
                    "evidence_kind": "test",
                },
                {"kind": "judge", "round": 2, "name": "qa", "verdict": "PASS", "file": "src/app.py"},
            ]
        },
        "standards_results": [
            {"rule": "factory:fail-to-pass", "status": "FAIL", "output": "src/app.py:12: wrong result"},
            {"rule": "factory:language-summary", "status": "PASS", "output": "clean"},
        ],
        "deliverables": {"pr": "https://github.com/acme/sample/pull/7"},
    }


def test_labels_preserve_recorded_evidence_and_schema(report):
    original = copy.deepcopy(report)
    rows = _bench.labels_from_report(report, "merged", "SUCCEEDED")
    assert report == original
    assert [r["kind"] for r in rows] == ["judge"] * 3 + ["gate"] * 2 + ["outcome", "outcome", "merge"]
    fields = {
        "source",
        "run",
        "project",
        "engine_sha",
        "kind",
        "round",
        "name",
        "verdict",
        "file",
        "line",
        "title",
        "evidence_kind",
    }
    assert all(set(r) == fields and r["source"] == "production" and r["engine_sha"] == SHA for r in rows)
    assert rows[1]["line"] == 12 and rows[1]["evidence_kind"] == "test"
    assert rows[3]["file"] == "src/app.py" and rows[3]["line"] == 12
    assert (rows[-3]["name"], rows[-3]["verdict"]) == ("phase", "SUCCEEDED")
    assert (rows[-2]["name"], rows[-2]["verdict"]) == ("supervisor_phase", "done")
    assert rows[-1]["verdict"] == "merged"
    assert not any(r["kind"] == "merge" for r in _bench.labels_from_report(report, None, "SUCCEEDED"))


def test_labels_are_bounded_and_validated(report):
    report["metrics"]["labels"] *= 200
    for row in report["metrics"]["labels"]:
        row["title"] = "x" * 500
    rows = _bench.labels_from_report(report, "open", "SUCCEEDED")
    assert len(rows) == 300
    assert all(len(row["title"]) <= 200 for row in rows)
    # Outcome and merge rows are reserved first, judges fill the rest, and gates only get leftovers.
    assert [row["kind"] for row in rows].count("judge") == 297
    assert {row["kind"] for row in rows} == {"judge", "outcome", "merge"}
    report["metrics"]["labels"] = [{"kind": "judge", "name": "qa", "verdict": "invalid"}]
    with pytest.raises(ValueError):
        _bench.labels_from_report(report, None, "SUCCEEDED")
    with pytest.raises(ValueError):
        _bench.labels_from_report(report, "invalid", "SUCCEEDED")


@pytest.mark.parametrize(
    "pull,expected",
    [
        ({"merged": True, "state": "closed"}, "merged"),
        ({"merged": False, "state": "closed"}, "closed"),
        ({"merged": False, "state": "open"}, "open"),
    ],
)
def test_collect_by_name_and_bench_exclusion(origin, report, monkeypatch, pull, expected):
    slot = _factory_base.SLOTS[0]
    runs: list[SimpleNamespace] = []

    def run(name, ended, *, bench=False, task=slot, phase="SUCCEEDED"):
        data = {**report, "run": name}
        item = SimpleNamespace(
            name=name,
            phase=phase,
            pb2=SimpleNamespace(labels={"factory-bench": "b1"} if bench else {}),
            action=SimpleNamespace(
                pb2=SimpleNamespace(
                    status=SimpleNamespace(
                        end_time=SimpleNamespace(
                            ToDatetime=lambda: datetime.fromtimestamp(ended, timezone.utc)
                        )
                    )
                )
            ),
            to_dict=lambda: {"action": {"metadata": {"task": {"id": {"name": task, "version": SHA}}}}},
            get_report=lambda: _dashboard.status_script(data),
        )
        runs.append(item)

    run("existing", 100)
    run("older", 50)
    run("production-run", 200)
    run("bench-case", 300, bench=True)
    run("other-task", 300, task="bench-test.project")
    run("live", 300, phase="RUNNING")
    with _bench_store.Store(origin.as_uri()) as store:
        store.write_production_labels(
            "existing", _bench.labels_from_report({**report, "run": "existing"}, None, "SUCCEEDED")
        )
    monkeypatch.setattr(_bench_flow, "_store", lambda app: _bench_store.Store(origin.as_uri()))
    reads: list[tuple[str, int]] = []

    def read_pull(repo, number):
        reads.append((repo, number))
        return pull

    app = SimpleNamespace(pull=read_pull)  # fake app; cast to GithubApp where passed on
    monkeypatch.setattr(_bench_flow, "_app", lambda: app)
    monkeypatch.setattr(remote.Run, "listall", lambda **kw: iter(runs if kw["task_name"] == slot else []))
    asyncio.run(_bench_flow.collect_labels.func())
    head = git(origin, "rev-parse", "bench-results")
    asyncio.run(_bench_flow.collect_labels.func())
    assert git(origin, "rev-parse", "bench-results") == head
    assert reads == [("acme/sample", 7)] * 2  # production-run and the older, previously skipped run
    with _bench_store.Store(origin.as_uri()) as store:
        assert store.production_runs() == {"existing", "older", "production-run"}
        path = store.tree / "labels/production/production-run.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        assert rows == _bench.labels_from_report(report, expected, "SUCCEEDED")


def test_daily_trigger_is_interruptible_and_queue_bounded():
    task = _bench_flow.collect_labels
    assert task.interruptible is True
    timeout = task.timeout
    assert isinstance(timeout, flyte.Timeout)
    max_queued = timeout.max_queued_time
    assert isinstance(max_queued, timedelta)
    assert 0 < max_queued.total_seconds() <= 3600
    triggers = task.triggers
    trigger = triggers[0] if isinstance(triggers, (list, tuple)) else triggers
    assert isinstance(trigger, flyte.Trigger)
    assert cast(Any, trigger.automation).interval_minutes == 24 * 60  # a FixedRate, not a Cron


def test_snapshot_exposes_saved_gates_and_engine_revision(tmp_path, monkeypatch):

    monkeypatch.setattr(_session_config, "WORKSPACE", tmp_path)
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(_session_config, "_project_name", "sample")
    monkeypatch.setattr(_session_config, "_acceptance_text", lambda: ("", False))
    monkeypatch.setenv("AGENT_FACTORY_ENGINE_SHA", SHA)
    path = tmp_path / ".claude-flyte/sample/standards-results.json"
    path.parent.mkdir(parents=True)
    gates = [{"rule": "factory:fail-to-pass", "status": "FAIL", "output": "src/app.py:12: failure"}]
    path.write_text(json.dumps(gates))
    timeline = _timeline._Timeline()
    snapshot = timeline.snapshot()
    assert snapshot["standards_results"] == gates
    assert snapshot["engine_sha"] == SHA
    path.write_text("broken JSON")
    assert timeline.snapshot()["standards_results"] == []


@pytest.mark.parametrize(
    "field,value", [("phase", None), ("review", []), ("metrics", None), ("standards_results", [None])]
)
def test_malformed_report_is_rejected(report, field, value):
    report[field] = value
    with pytest.raises(ValueError):
        _bench.labels_from_report(report, None, "SUCCEEDED")


def test_store_rejects_invalid_labels_and_keeps_first_publication(origin, report):
    rows = _bench.labels_from_report(report, None, "SUCCEEDED")
    with _bench_store.Store(origin.as_uri()) as store:
        with pytest.raises(ValueError):
            store.write_production_labels("../escape", rows)
        with pytest.raises(ValueError):
            store.write_production_labels("other", rows)
        with pytest.raises(ValueError):
            store.write_production_labels(report["run"], rows * 100)
        store.write_production_labels(report["run"], rows)
        first = git(origin, "rev-parse", "bench-results")
        store.write_production_labels(report["run"], _bench.labels_from_report(report, "open", "SUCCEEDED"))
        assert git(origin, "rev-parse", "bench-results") == first


def test_recorded_gate_labels_do_not_duplicate_standards_entries(report):
    report["metrics"]["labels"].insert(
        0,
        {
            "kind": "gate",
            "round": 1,
            "name": "factory:fail-to-pass",
            "verdict": "FAIL",
            "file": "src/app.py",
            "line": 12,
        },
    )
    rows = _bench.labels_from_report(report, None, "SUCCEEDED")
    assert [row["kind"] for row in rows] == ["judge"] * 3 + ["gate"] * 2 + ["outcome"] * 2


def test_gates_fill_only_the_budget_judges_leave(report):
    report["metrics"]["labels"] = [
        {"kind": "judge", "round": 1, "name": "qa", "verdict": "FAIL"} for _ in range(290)
    ]
    report["standards_results"] = [{"rule": f"R{n}", "status": "PASS", "output": ""} for n in range(50)]
    rows = _bench.labels_from_report(report, "open", "SUCCEEDED")
    kinds = [row["kind"] for row in rows]
    assert len(rows) == 300 and kinds.count("judge") == 290 and kinds.count("gate") == 7
    assert kinds[-3:] == ["outcome", "outcome", "merge"]


def test_outcome_is_the_remote_terminal_phase_not_the_supervisor_phase(report):
    report["phase"] = {"kind": "building", "round": 1}
    rows = _bench.labels_from_report(report, None, "FAILED")
    outcomes = {row["name"]: row["verdict"] for row in rows if row["kind"] == "outcome"}
    assert outcomes == {"phase": "FAILED", "supervisor_phase": "building"}


@pytest.mark.parametrize("state", ["missing", "truncated"])
def test_unusable_report_keeps_only_outcome_and_merge_rows(report, state):
    rows = _bench.labels_from_report({"run": "r1", "project": "sample"}, "closed", "ABORTED", state)
    assert [(r["kind"], r["name"], r["verdict"], r["evidence_kind"]) for r in rows] == [
        ("outcome", "phase", "ABORTED", state),
        ("merge", "pr", "closed", state),
    ]
    assert len(_bench.labels_from_report({"run": "r1", "project": "p"}, None, "FAILED", state)) == 1


def _ended(at):
    end_time = SimpleNamespace(ToDatetime=lambda: datetime.fromtimestamp(at, timezone.utc))
    return SimpleNamespace(pb2=SimpleNamespace(status=SimpleNamespace(end_time=end_time)))


def _collect_runs(origin, monkeypatch, report, specs, reports, *, ended=None):
    """Run the collector over fake runs: specs are (name, extra labels, remote phase).

    The runs end now unless `ended` says otherwise, or is `False` for a run with no end time.
    """
    slot = _factory_base.SLOTS[0]
    action = SimpleNamespace(pb2=SimpleNamespace()) if ended is False else _ended(ended or time.time())
    runs = [
        SimpleNamespace(
            name=name,
            phase=phase,
            pb2=SimpleNamespace(labels=labels),
            action=action,
            to_dict=lambda: {"action": {"metadata": {"task": {"id": {"name": slot, "version": SHA}}}}},
            get_report=lambda name=name: reports[name],
        )
        for name, labels, phase in specs
    ]
    monkeypatch.setattr(_bench_flow, "_store", lambda app: _bench_store.Store(origin.as_uri()))
    monkeypatch.setattr(
        _bench_flow,
        "_app",
        lambda: SimpleNamespace(pull=lambda repo, number: {"merged": True, "state": "closed"}),
    )
    monkeypatch.setattr(remote.Run, "listall", lambda **kw: iter(runs if kw["task_name"] == slot else []))
    return asyncio.run(_bench_flow.collect_labels.func())


def _read(origin, name):
    with _bench_store.Store(origin.as_uri()) as store:
        path = store.tree / f"labels/production/{name}.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]


def test_runs_without_a_usable_snapshot_do_not_stall_collection(origin, report, monkeypatch):
    truncated = _dashboard.status_script({"schema": 1, "truncated": True, "project": "sample"})
    reports = {
        "old-none": "<p>no snapshot</p>",
        "old-truncated": truncated,
        "new": _dashboard.status_script(report),
    }
    specs: list[tuple[str, dict[str, str], str]] = [(n, {}, "FAILED") for n in reports]
    assert _collect_runs(origin, monkeypatch, report, specs, reports) == 3
    for name, state in (("old-none", "missing"), ("old-truncated", "truncated")):
        rows = _read(origin, name)
        assert [r["kind"] for r in rows] == ["outcome"]
        assert rows[0]["verdict"] == "FAILED" and rows[0]["evidence_kind"] == state
    new = _read(origin, "new")
    assert {r["verdict"] for r in new if r["kind"] == "outcome"} == {"FAILED", "done"}
    assert _collect_runs(origin, monkeypatch, report, specs, reports) == 0


def test_loop_runs_are_not_production(origin, report, monkeypatch):
    reports = {"prod": _dashboard.status_script(report), "loop": _dashboard.status_script(report)}
    specs: list[tuple[str, dict[str, str], str]] = [
        ("prod", {}, "SUCCEEDED"),
        ("loop", {"factory-loop": "acme-sample-3"}, "SUCCEEDED"),
    ]
    assert _collect_runs(origin, monkeypatch, report, specs, reports) == 1
    with _bench_store.Store(origin.as_uri()) as store:
        assert store.production_runs() == {"prod"}


def test_worst_case_snapshot_renders_untruncated(tmp_path, monkeypatch):

    monkeypatch.setattr(_session_config, "WORKSPACE", tmp_path)
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(_session_config, "_project_name", "sample")
    monkeypatch.setattr(_session_config, "_acceptance_text", lambda: ("", False))
    monkeypatch.setenv("AGENT_FACTORY_ENGINE_SHA", SHA)
    path = tmp_path / ".claude-flyte/sample/standards-results.json"
    path.parent.mkdir(parents=True)
    # Every character costs the most in the page: `<` is 6 bytes, an astral code point 12.
    worst = "<" * 400 + "\U0001f600" * 600
    path.write_text(
        json.dumps([{"rule": worst, "status": worst, "output": f"{worst}\nsrc/{'a' * 300}.py:1"}] * 300)
    )
    timeline = _timeline._Timeline()
    for n in range(300):
        timeline.metric_label(
            kind="judge",
            round=n,
            name="qa",
            verdict="FAIL",
            file="src/" + "f" * 295,
            line=1,
            title="t" * 200,
            evidence_kind="e" * 200,
        )
    snapshot = timeline.snapshot()
    page = _dashboard.render(
        snapshot,
        header_html="",
        meter_rows="",
        activity="",
        rows=[(0.0, "i", "row")],
        routine=[],
        now=0.0,
    )
    assert len(page.encode()) <= _dashboard.MAX_REPORT_BYTES
    back = _dashboard.from_report(page)
    assert back is not None
    assert not back.get("truncated")
    assert back["engine_sha"] == SHA
    assert (
        back["standards_results"] == snapshot["standards_results"] and len(back["standards_results"]) == 100
    )
    assert back["metrics"]["labels"] == snapshot["metrics"]["labels"]


def test_every_gate_status_survives_compaction(report):
    statuses = sorted(_bench._GATE_STATUSES, key=len)
    rows = [
        {"rule": "factory:suppressions", "status": status, "output": "src/app.py:3: x"} for status in statuses
    ]
    report["standards_results"] = _dashboard.compact_standards_results(rows)
    gates = [r for r in _bench.labels_from_report(report, None, "SUCCEEDED") if r["kind"] == "gate"]
    assert sorted((r["verdict"] for r in gates), key=len) == statuses


def test_a_harness_fault_is_a_gate_label():
    assert _gate_base.Status.HARNESS_FAULT.value in _bench._GATE_STATUSES


def test_an_unreadable_pr_drops_only_the_merge_row(origin, report, monkeypatch):
    bad = {**report, "deliverables": {"pr": "not a pull request url"}}
    reports = {"bad-pr": _dashboard.status_script(bad), "next": _dashboard.status_script(report)}
    specs: list[tuple[str, dict[str, str], str]] = [(n, {}, "SUCCEEDED") for n in reports]
    assert _collect_runs(origin, monkeypatch, report, specs, reports) == 2
    assert "merge" not in {r["kind"] for r in _read(origin, "bad-pr")}
    assert "merge" in {r["kind"] for r in _read(origin, "next")}


def _raise(exc):
    raise exc


def test_a_run_whose_report_cannot_be_fetched_gets_an_error_row(origin, report, monkeypatch):
    reports = {"gone": None, "next": _dashboard.status_script(report)}
    specs: list[tuple[str, dict[str, str], str]] = [(n, {}, "ABORTED") for n in reports]
    monkeypatch.setattr(
        _factory_base,
        "report_html",
        lambda run: (
            _raise(RuntimeError("No report is available")) if run.name == "gone" else reports[run.name]
        ),
    )
    assert _collect_runs(origin, monkeypatch, report, specs, reports) == 2
    rows = _read(origin, "gone")
    assert [(r["kind"], r["verdict"], r["evidence_kind"]) for r in rows] == [
        ("outcome", "ABORTED", "error:RuntimeError")
    ]
    assert "judge" in {r["kind"] for r in _read(origin, "next")}


def test_a_failing_pr_lookup_keeps_the_judge_rows(origin, report, monkeypatch):
    from flyte_agent_factory import _github

    reports = {"pr-404": _dashboard.status_script(report)}
    app = SimpleNamespace(
        pull=lambda repo, number: _raise(_github.GithubError("HTTP 404", 404))
    )  # fake app; cast to GithubApp where passed on
    original = _bench_flow._production_rows
    monkeypatch.setattr(
        _bench_flow,
        "_production_rows",
        lambda _app, name, run, ended: original(cast(GithubApp, app), name, run, ended),
    )
    assert _collect_runs(origin, monkeypatch, report, [("pr-404", {}, "SUCCEEDED")], reports) == 1
    kinds = {r["kind"] for r in _read(origin, "pr-404")}
    assert "judge" in kinds and "merge" not in kinds


def test_line_zero_citations_name_no_line(report):
    report["standards_results"] = [
        {"rule": "factory:fail-to-pass", "status": "FAIL", "output": "bind 127.0.0.1:0 failed"}
    ]
    report["metrics"]["labels"].append(
        {"kind": "judge", "round": 2, "name": "qa", "verdict": "FAIL", "line": 0}
    )
    rows = _bench.labels_from_report(report, None, "SUCCEEDED")
    assert all(r["line"] is None or r["line"] >= 1 for r in rows)


_PRODUCTION_ROWS = _bench_flow._production_rows


def _collect_with_pull(origin, monkeypatch, report, name, pull, *, ended=None):
    reports = {name: _dashboard.status_script(report)}
    original = _PRODUCTION_ROWS
    app = SimpleNamespace(pull=pull)  # fake app; cast to GithubApp where passed on
    monkeypatch.setattr(
        _bench_flow,
        "_production_rows",
        lambda _app, n, run, ended: original(cast(GithubApp, app), n, run, ended),
    )
    return _collect_runs(origin, monkeypatch, report, [(name, {}, "SUCCEEDED")], reports, ended=ended)


def test_a_recent_run_with_an_open_pr_waits_for_it_to_settle(origin, report, monkeypatch):
    assert _collect_with_pull(origin, monkeypatch, report, "fresh", lambda r, n: {"state": "open"}) == 0
    with _bench_store.Store(origin.as_uri()) as store:
        assert store.production_runs() == set()


def test_a_transient_failure_leaves_the_run_for_a_later_pass(origin, report, monkeypatch):
    from flyte_agent_factory import _github

    for error in (OSError("reset"), _github.GithubError("HTTP 502", 502)):
        assert _collect_with_pull(origin, monkeypatch, report, "flaky", lambda r, n, e=error: _raise(e)) == 0
    assert _collect_with_pull(origin, monkeypatch, report, "flaky", lambda r, n: {"merged": True}) == 1
    assert "merge" in {r["kind"] for r in _read(origin, "flaky")}


def test_past_the_settling_window_an_open_pr_or_a_failure_is_recorded(origin, report, monkeypatch):
    from flyte_agent_factory import _github

    old = time.time() - 30 * 86400
    assert (
        _collect_with_pull(origin, monkeypatch, report, "open", lambda r, n: {"state": "open"}, ended=old)
        == 1
    )
    assert ("merge", "open") in {(r["kind"], r["verdict"]) for r in _read(origin, "open")}
    failing = lambda r, n: _raise(_github.GithubError("HTTP 502", 502))  # noqa: E731 -- a one-line fake, clearer inline
    assert _collect_with_pull(origin, monkeypatch, report, "pr-502", failing, ended=old) == 1
    kinds = {r["kind"] for r in _read(origin, "pr-502")}
    assert "judge" in kinds and "merge" not in kinds


def test_a_run_with_no_end_time_counts_as_long_ago(origin, report, monkeypatch):
    assert (
        _collect_with_pull(origin, monkeypatch, report, "no-end", lambda r, n: {"state": "open"}, ended=False)
        == 1
    )


def test_a_lasting_report_failure_is_recorded_after_the_window(origin, report, monkeypatch):
    reports = {"expired": None}
    monkeypatch.setattr(_factory_base, "report_html", lambda run: _raise(OSError("expired link")))
    specs: list[tuple[str, dict[str, str], str]] = [("expired", {}, "SUCCEEDED")]
    assert _collect_runs(origin, monkeypatch, report, specs, reports) == 0
    assert _collect_runs(origin, monkeypatch, report, specs, reports, ended=time.time() - 30 * 86400) == 1
    assert [r["evidence_kind"] for r in _read(origin, "expired")] == ["error:OSError"]
