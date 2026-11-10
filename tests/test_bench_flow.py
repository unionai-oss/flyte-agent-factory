"""The factory benchmark on Flyte, offline: a fake remote (runs, tasks, conditions) drives `run_case`'s
loop through its outcomes; git paths run against local repositories; the hidden check runs for real
against a reference service."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, cast

import flyte
import pytest
from flyte import remote

from flyte_agent_factory import (
    _bench,
    _bench_check,
    _bench_flow,
    _bench_stats,
    _bench_store,
    _bench_suite,
    _dashboard,
    _deploy,
    _factory,
    _loop,
    _profile,
)
from flyte_agent_factory._github import GithubApp
from tests.fake_flyte import FakeFlyte
from tests.test_bench import ask, profile
from tests.test_bench import snapshot as bench_snapshot

SHA = "0123456789abcdef0123456789abcdef01234567"
REF = Path(__file__).resolve().parent / "fixtures" / "bench" / "shortlinks_ref"


def snapshot(*args, **kwargs):
    # Flow tests use cheap sessions so ordinary progress fits the full shutdown reserve.
    kwargs.setdefault("usd", (0.03, 0.015))
    return bench_snapshot(*args, **kwargs)


class FakeCondition:
    def __init__(self, world, run, name):
        self.world, self.run, self.name, self.phase = world, run, name, "PAUSED"

    def signal(self, payload):
        self.world.events.append(("signal", self.name, payload))
        self.phase = "SUCCEEDED"
        if payload == _bench.STOP and self.world.obeys_stop:
            self.world.fake.runs[self.run].phase = "SUCCEEDED"


class World:
    """A clock that moves only when the harness sleeps, and a script of what changes at each sleep."""

    def __init__(self, monkeypatch):
        self.fake = FakeFlyte(monkeypatch, _factory)
        monkeypatch.setattr(_factory, "owner_demand", lambda: False)
        self.t = 1_000_000.0
        self.events: list[tuple[Any, ...]] = []
        self.conditions: dict[str, list[FakeCondition]] = {}
        self.script: list[Any] = []  # callables, one per sleep
        self.obeys_stop = True
        self.refresh_reports = True
        self.snapshots = {}
        self.start = ("c" * 40, _bench_suite.get("bugfix-shortlinks").start_tree)
        monkeypatch.setattr(_bench_flow, "PROFILE", profile())
        monkeypatch.setattr(remote.Condition, "listall", self._conditions)
        monkeypatch.setattr(_bench_flow, "start_of", self._start_of)
        monkeypatch.setattr(_bench_flow, "prepare_case", lambda *args: None)

    def _start_of(self, url, task):
        if isinstance(self.start, Exception):
            raise self.start
        return self.start

    def _conditions(self, run_name):
        return list(self.conditions.get(run_name, []))

    def open(self, run, name):
        self.conditions.setdefault(run, []).append(FakeCondition(self, run, name))

    def clock(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds
        if self.refresh_reports:
            for name, snap in list(self.snapshots.items()):
                self.report(name, {**snap, "generated_at": self.t})
        if self.script:
            self.script.pop(0)(self)

    def report(self, run, snap):
        snap = {"generated_at": self.t, **snap}
        self.snapshots[run] = snap
        self.fake.runs[run].report = _dashboard.status_script(snap)

    def drive(self, case):
        return _bench_flow.drive_case(case, "unused-url", clock=self.clock, sleep=self.sleep)


@pytest.fixture
def world(monkeypatch):
    return World(monkeypatch)


def planned(**over):
    return {
        **_bench.plan_cases("r1", SHA, [], 1, {"CRAFT_EFFORT": "low"}, "opus")[0],
        "env": "bench-0123456-1",
        **over,
    }


def started(world, case, *snaps):
    """After the first sleep the run has its direct condition and the first snapshot; then one per sleep."""

    def first(w):
        w.open(case["case"], "direct-a1-001")
        w.report(case["case"], snaps[0])

    def then(snap):
        return lambda w: w.report(case["case"], snap)

    world.script = [first, *map(then, snaps[1:])]


def test_a_case_that_passes_review_is_accepted_and_stopped(world):
    case = planned()
    started(world, case, snapshot("review"), snapshot("done", scores=[2]))
    run, final, harness = world.drive(case)
    (created,) = world.fake.created
    assert created["name"] == case["case"] and created["task"] == "bench-0123456-1.project"
    # The bench repo is the [bench] repo, never the production project repo.
    assert created["inputs"]["repo"] == "git@github.com:example-org/factory-bench.git"
    assert created["inputs"]["branch"] == f"bench/{case['case']}"
    assert created["inputs"]["name"] == case["case"] and "{name}" not in created["inputs"]["brief"]
    assert f"cp -r bench/shortlinks {case['case']}" in created["inputs"]["brief"]
    assert created["context"]["env_vars"] == {"AGENT_FACTORY_CRAFT_EFFORT": "low"}
    assert created["context"]["labels"] == {
        "factory-bench": "r1",
        "bench-arm": "candidate",
        "bench-task": "bugfix-shortlinks",
        "bench-rep": "1",
    }
    assert harness["outcome"] == "accepted" and harness["review_passed"] and not harness["forced_abort"]
    assert world.events == [("signal", "direct-a1-001", "/stop")]
    assert world.fake.runs[case["case"]].phase == "SUCCEEDED"
    assert (harness["start_sha"], harness["start_tree"]) == world.start
    record = _bench.case_record(case, run, final, harness, {"exit_code": 0, "summary": "2 passed"})
    assert record["passed_round"] == 2 and record["accepted_verified"] and record["case_url"] == run["url"]
    assert record["wall_s"] == harness["ended_at"] - harness["started_at"] == 2 * _bench_flow.POLL_S


def test_a_case_over_its_cost_cap_is_stopped_before_it_is_aborted(world):
    case = planned()
    world.obeys_stop = False  # the session never exits
    started(world, case, snapshot("review", usd=(35.0, 6.0)))
    _, _, harness = world.drive(case)
    assert harness["outcome"] == "capped_cost" and not harness["review_passed"]
    assert harness["forced_abort"] and not harness["forced_abort_without_stop"]
    run = world.fake.runs[case["case"]]
    assert run.phase == "ABORTED" and run.abort_reason == _bench_flow.ABORT_REASON
    assert world.events == [("signal", "direct-a1-001", "/stop")]  # the stop came first
    # It waited out the grace before aborting.
    assert world.t - harness["ended_at"] >= _bench_flow.STOP_GRACE_S


def test_a_cost_spike_late_in_a_case_does_not_cap_it(world):
    # The second smoke run (Oct 25) was capped at $9 of a $40 cap 28 minutes in, when four judges
    # finished within one poll: the reserve used that one poll's delta. The rate is now measured over a
    # 10-minute window, so a burst late in a case is spread over it.
    case = planned()
    snaps = [snapshot("review", usd=(0.1 * i, 0.5)) for i in range(1, 13)]  # 12 polls of slow progress
    snaps += [snapshot("review", usd=(5.7, 0.5)), snapshot("review", usd=(5.7, 0.6))]  # +$4.50 in one poll
    started(world, case, *snaps)
    _, _, harness = world.drive(case)
    assert harness["outcome"] != "capped_cost", harness


def test_burn_rate_is_measured_over_a_window():
    floor = _bench.INITIAL_BURN_USD_PER_S
    window = _bench_flow.BURN_WINDOW_S
    assert _bench_flow.burn_rate([]) == floor
    assert _bench_flow.burn_rate([(0, 5.0)]) == floor
    # Early: from the first reading.
    assert _bench_flow.burn_rate([(60, 10.0), (120, 12.0)]) == 2.0 / 60
    # Later: from the latest reading at or before the window's start.
    readings: list[tuple[float, float]] = [
        *((t, 0.01 * t) for t in range(0, 1201, 60)),
        (1260, 0.01 * 1200 + 4.5),
    ]
    assert abs(_bench_flow.burn_rate(readings) - (0.01 * 1200 + 4.5 - 0.01 * 660) / (1260 - 660)) < 1e-9
    assert window == 600
    # A sustained rate shows in full within one window.
    sustained: list[tuple[float, float]] = [
        *((t, 0.0) for t in range(0, 3601, 60)),
        *((3600 + 60 * k, 1.2 * k) for k in range(1, 12)),
    ]
    assert abs(_bench_flow.burn_rate(sustained) - 0.02) < 1e-9


def test_stop_is_retried_until_a_direct_condition_opens(world):
    case = planned()
    # Over the cap before the session has opened its direct condition; it opens three stop polls later.
    world.script = [
        lambda w: w.report(case["case"], snapshot("building", usd=(41.0, 1.0))),
        lambda w: None,
        lambda w: None,
        lambda w: w.open(case["case"], "direct-a1-001"),
    ]
    _, _, harness = world.drive(case)
    assert harness["outcome"] == "capped_cost"
    assert world.events == [("signal", "direct-a1-001", "/stop")]
    assert not harness["forced_abort"] and world.fake.runs[case["case"]].phase == "SUCCEEDED"


def test_a_case_that_never_takes_a_stop_is_left_running_and_says_so(world, capsys):
    case = planned()
    world.script = [lambda w: w.report(case["case"], snapshot("building", usd=(41.0, 1.0)))]
    _, _, harness = world.drive(case)
    assert world.events == []
    assert not harness["forced_abort"] and not harness["forced_abort_without_stop"]
    assert world.fake.runs[case["case"]].phase == "RUNNING"
    assert harness["outcome"] == "harness_error" and "stop_undeliverable" in harness["error"]
    record = _bench.case_record(case, {"name": case["case"], "url": ""}, {}, harness, None)
    assert not record["forced_abort_without_stop"]
    assert any("stop_undeliverable" in note for note in _bench.caveats([record]))
    assert "stop_undeliverable" in capsys.readouterr().err


def test_an_unreadable_report_stops_the_case(world):
    case = planned()
    world.script = [lambda w: w.open(case["case"], "direct-a1-001")]  # running, never a report
    _, _, harness = world.drive(case)
    assert harness["outcome"] == "harness_error" and "report" in harness["error"]
    assert harness["ended_at"] - harness["started_at"] <= _bench_flow.REPORT_STALE_S + _bench_flow.POLL_S
    assert world.events == [("signal", "direct-a1-001", "/stop")]


def test_a_question_is_answered_with_the_default_and_counted(world):
    case = planned()
    question = ask(timeout=600, condition="ask-a1-001", opened=world.t + 30)

    def asked(w):
        w.open(case["case"], "direct-a1-001")
        w.open(case["case"], "ask-a1-001")
        w.report(case["case"], snapshot("building", asks=[question]))

    world.script = [
        asked,
        lambda w: w.report(case["case"], snapshot("building", asks=[question])),  # not answered twice
        lambda w: w.report(case["case"], snapshot("done")),
    ]
    _, _, harness = world.drive(case)
    assert harness["outcome"] == "accepted"
    assert (harness["asks"], harness["blocking_asks"], harness["first_ask_s"]) == (1, 0, 30.0)
    assert world.events == [("signal", "ask-a1-001", _bench.ASK_ANSWER), ("signal", "direct-a1-001", "/stop")]


def test_a_blocking_question_escalates_and_is_not_answered(world):
    case = planned()
    question = ask(kind="approve", condition="ask-a1-001")

    def asked(w):
        w.open(case["case"], "direct-a1-001")
        w.open(case["case"], "ask-a1-001")
        w.report(case["case"], snapshot("building", asks=[question]))

    world.script = [asked]
    _, _, harness = world.drive(case)
    assert harness["outcome"] == "escalated" and harness["blocking_asks"] == 1
    assert world.events == [("signal", "direct-a1-001", "/stop")]


def test_a_retried_harness_attaches_to_the_case_run(world):
    case = planned()
    started(world, case, snapshot("done"))
    world.drive(case)
    world.fake.runs[case["case"]].phase = "RUNNING"  # the retry finds its case still running
    # Its inbox is open, so the /stop that ends an accepted case can be delivered (never abort without it).
    world.open(case["case"], "direct-a1-001")
    world.script = [lambda w: None]
    _, _, harness = world.drive(case)
    assert len(world.fake.created) == 1  # launched once, attached the second time
    assert harness["outcome"] == "accepted"


def test_a_harness_that_finds_its_case_ended_does_not_guess(world):
    case = planned()
    started(world, case, snapshot("done"))
    world.drive(case)  # passed and stopped; then the harness died before returning its record
    world.script = []
    world.report(case["case"], snapshot("done", reviewed=False))
    _, _, harness = world.drive(case)
    assert harness["outcome"] == "harness_error" and "outcome is unknown" in harness["error"]
    assert len(world.fake.created) == 1
    # A release, though, is only ever after a pass.
    world.report(case["case"], snapshot("released"))
    _, _, harness = world.drive(case)
    assert harness["outcome"] == "accepted"


def test_the_release_question_after_a_pass_is_not_counted(world):
    case = planned()
    release = ask(timeout=1800, condition="ask-a1-002")

    def passed(w):
        w.open(case["case"], "direct-a1-001")
        w.open(case["case"], "ask-a1-002")
        w.report(case["case"], snapshot("done", asks=[release]))

    world.script = [passed]
    _, _, harness = world.drive(case)
    assert harness["outcome"] == "accepted" and harness["asks"] == 0
    assert world.events == [("signal", "direct-a1-001", "/stop")]


def test_a_case_that_ends_without_a_pass_has_not_converged(world):
    case = planned()

    def ended(w):
        w.report(case["case"], snapshot("review", scores=[3]))
        w.fake.runs[case["case"]].phase = "SUCCEEDED"

    world.script = [ended]
    _, _, harness = world.drive(case)
    assert harness["outcome"] == "not_converged" and not harness["forced_abort"]
    assert world.events == []  # nothing to stop


def test_the_time_cap_ends_a_quiet_case(world):
    case = planned()
    started(world, case, snapshot("building"))
    _, _, harness = world.drive(case)
    assert harness["outcome"] == "capped_time"
    assert harness["ended_at"] - harness["started_at"] >= 3 * 3600


def test_a_missing_start_launches_nothing(world):
    case = planned()
    world.start = _bench_flow.HarnessError("the bench repo's default branch has no bench/shortlinks/")
    run, final, harness = world.drive(case)
    assert harness["outcome"] == "harness_error" and "has no bench/shortlinks" in harness["error"]
    assert world.fake.created == [] and run == {"name": case["case"], "url": ""}
    record = _bench.case_record(case, run, final, harness, None)
    assert record["outcome"] == "harness_error" and record["verified"] is None


def test_a_start_that_is_not_the_pinned_tree_launches_nothing(world, monkeypatch):
    task = _bench_suite.TASKS["bugfix-shortlinks"]
    monkeypatch.setitem(_bench_suite.TASKS, task.id, dataclasses.replace(task, start_tree="e" * 40))
    _, _, harness = world.drive(planned())
    assert harness["outcome"] == "harness_error" and "not the pinned" in harness["error"]
    assert world.fake.created == []


# --------------------------------------------------------------------------- #
# Git: the start, and the code a case pushed
# --------------------------------------------------------------------------- #


def git(cwd, *args):
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


@pytest.fixture
def bench_repo(tmp_path):
    """A bench repo with the start directory on main and one case's branch."""
    repo = tmp_path / "bench"
    (repo / "bench" / "shortlinks").mkdir(parents=True)
    (repo / "bench" / "shortlinks" / "README.md").write_text("start\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.org", "add", ".")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-q", "-m", "start")
    return repo


def pinned_to(repo, task_id, monkeypatch):
    """The task, pinned to the fake bench repo's own start tree (the real pin names the committed fixture)."""
    task = _bench_suite.get(task_id)
    pinned = dataclasses.replace(task, start_tree=git(repo, "rev-parse", f"HEAD:{task.workdir}"))
    monkeypatch.setitem(_bench_suite.TASKS, task_id, pinned)
    return pinned


def test_the_start_is_the_tree_of_the_start_directory(bench_repo):
    task = _bench_suite.get("bugfix-shortlinks")
    commit, tree = _bench_flow.start_of(bench_repo.as_uri(), task)
    expected = subprocess.run(
        ["git", "rev-parse", "HEAD", "HEAD:bench/shortlinks"], cwd=bench_repo, capture_output=True, text=True
    ).stdout.split()
    assert [commit, tree] == expected
    with pytest.raises(_bench_flow.HarnessError, match="has no bench/other/"):
        _bench_flow.start_of(bench_repo.as_uri(), dataclasses.replace(task, workdir="bench/other"))
    with pytest.raises(_bench_flow.HarnessError, match="could not read the bench repo"):
        _bench_flow.start_of((bench_repo.parent / "missing").as_uri(), task)


def test_the_hidden_check_gets_the_case_directory_from_the_case_branch(bench_repo, monkeypatch):
    # Bench envs have no App; the clone branch is also the push upstream.
    case = planned(case="b-bugfix-sho-abc123")
    shutil.copytree(REF, bench_repo / "b-bugfix-sho-abc123")
    git(bench_repo, "-c", "user.name=t", "-c", "user.email=t@example.org", "add", ".")
    git(bench_repo, "-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-q", "-m", "fix")
    git(bench_repo, "branch", f"bench/{case['case']}")
    git(bench_repo, "branch", "bench/b-bugfix-sho-ffffff")
    seen = {}

    async def from_local(path):
        seen["files"] = sorted(p.name for p in Path(path).iterdir())
        return path

    async def spot_first(task, code, task_id):
        assert task is _bench_check.hidden_check
        return json.dumps({"exit_code": 0, "summary": f"checked {task_id}"})

    monkeypatch.setattr(flyte.io.Dir, "from_local", from_local)
    monkeypatch.setattr(_bench_flow, "_spot_first", spot_first)
    check = asyncio.run(_bench_flow.check_case(bench_repo.as_uri(), case))
    assert check == {"exit_code": 0, "summary": "checked bugfix-shortlinks"}
    assert seen["files"] == ["shortlinks", "tests"]
    # Nothing pushed: the check fails. An unreadable repo: it could not run.
    missing = asyncio.run(_bench_flow.check_case(bench_repo.as_uri(), planned(case="b-bugfix-sho-ffffff")))
    assert missing == {"exit_code": 1, "summary": "the case branch has no b-bugfix-sho-ffffff/ directory"}
    assert asyncio.run(_bench_flow.check_case((bench_repo.parent / "gone").as_uri(), case)) is None


@pytest.mark.parametrize(
    ("changed_path", "ask_offset", "verified"),
    [("case/file.py", -1, True), ("case/file.py", 1, False), ("case-other/file.py", 1, True)],
)
def test_escalation_scores_the_case_branch_history(
    bench_repo, monkeypatch, changed_path, ask_offset, verified
):
    case = planned(task="vague", kind="escalate", case="case")
    _bench_flow.prepare_case(bench_repo.as_uri(), case, pinned_to(bench_repo, "vague", monkeypatch))
    git(bench_repo, "checkout", "-q", "bench/case")
    path = bench_repo / changed_path
    path.parent.mkdir(parents=True)
    path.write_text("changed\n")
    git(bench_repo, "add", ".")
    git(bench_repo, "-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-qm", "change")
    sha = git(bench_repo, "rev-parse", "HEAD")
    at = int(git(bench_repo, "show", "-s", "--format=%ct", "HEAD"))

    def no_check(*args):
        pytest.fail("escalation tasks must not upload code or run a hidden check")

    monkeypatch.setattr(flyte.io.Dir, "from_local", no_check)
    monkeypatch.setattr(_bench_flow, "_spot_first", no_check)
    monkeypatch.setattr(_bench_flow, "_app", lambda: object())
    monkeypatch.setattr(_bench_flow, "_clone_url", lambda *args: bench_repo.as_uri())
    monkeypatch.setattr(_bench_flow, "_bench_table", lambda: profile().bench)
    harness = {
        "outcome": "escalated",
        "review_passed": False,
        "started_at": at - 10,
        "ended_at": at + 10,
        "asks": 1,
        "blocking_asks": 1,
        "forced_abort": False,
        # A blocking ask, answered before the change (a build after an unanswered ask is not an escalation).
        "conditions": [
            {
                "name": "ask-a1",
                "opened_at": at + ask_offset,
                "blocking": True,
                "answered_at": at + ask_offset + 0.5,
            }
        ],
    }
    monkeypatch.setattr(
        _bench_flow,
        "drive_case",
        lambda *args, **kwargs: ({"name": "case", "url": ""}, snapshot(), harness),
    )
    monkeypatch.setattr(_bench_flow, "_case_live", lambda *args: False)
    monkeypatch.setattr(_bench_flow, "cleanup_case", lambda *args: None)
    record = json.loads(asyncio.run(_bench_flow.run_case.func(json.dumps(case))))
    assert harness["commits"] == [{"sha": sha, "at": at, "paths": [changed_path]}]
    assert record["verified"] is verified
    assert record["accepted_verified"] is verified
    assert record["spot_check"] is verified  # heuristic escalation successes are adjudicated by hand


@pytest.mark.parametrize("task_id", ["vague", "too-big"])
def test_an_escalation_with_no_changes_has_empty_history(bench_repo, monkeypatch, task_id):
    case = planned(task=task_id, kind="escalate", case="case")
    task = _bench_suite.get(task_id)
    if not (bench_repo / task.workdir).exists():  # too-big starts from its own directory
        (bench_repo / task.workdir).mkdir(parents=True)
        (bench_repo / task.workdir / "BRIEF.md").write_text("start\n")
        git(bench_repo, "-c", "user.name=t", "-c", "user.email=t@example.org", "add", ".")
        git(bench_repo, "-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-q", "-m", "start")
    _bench_flow.prepare_case(bench_repo.as_uri(), case, pinned_to(bench_repo, task_id, monkeypatch))

    def no_check(*args):
        pytest.fail("escalation tasks must not upload code or run a hidden check")

    monkeypatch.setattr(flyte.io.Dir, "from_local", no_check)
    monkeypatch.setattr(_bench_flow, "_spot_first", no_check)
    assert asyncio.run(_bench_flow.check_case(bench_repo.as_uri(), case)) == {"commits": []}
    assert asyncio.run(_bench_flow.check_case((bench_repo.parent / "gone").as_uri(), case)) is None


def test_unreadable_commit_history_is_not_reported_as_empty(bench_repo, monkeypatch):
    case = planned(task="vague", kind="escalate", case="case")
    _bench_flow.prepare_case(bench_repo.as_uri(), case, pinned_to(bench_repo, "vague", monkeypatch))

    def failed(*args):
        raise _bench_flow.HarnessError("git log failed")

    monkeypatch.setattr(_bench_flow, "_checked_git", failed)
    assert asyncio.run(_bench_flow.check_case(bench_repo.as_uri(), case)) is None


# --------------------------------------------------------------------------- #
# The hidden check itself
# --------------------------------------------------------------------------- #


def reference(tmp_path: Path, **switches) -> Path:
    project = tmp_path / "case"
    shutil.copytree(REF, project)
    init = project / "shortlinks" / "__init__.py"
    text = init.read_text()
    for name, value in switches.items():
        text = text.replace(f"{name} = True", f"{name} = {value}")
    init.write_text(text)
    return project


@pytest.mark.slow  # runs the shortlinks hidden check: the benchmark lane
def test_the_shortlinks_check_passes_the_fixed_service(tmp_path):
    result = _bench_check.run_hidden_check(
        _bench_suite.get("bugfix-shortlinks"), reference(tmp_path), key_vars=()
    )
    assert result["exit_code"] == 0, result["summary"]
    assert "6 passed" in result["summary"]


@pytest.mark.slow  # runs the shortlinks hidden check: the benchmark lane
@pytest.mark.parametrize(
    "switch, failing",
    [
        ("FREE_DELETED_SLUGS", "test_a_deleted_slug_can_be_registered_again"),
        ("ATOMIC_VISITS", "test_fifty_concurrent_visits_are_all_counted"),
    ],
)
def test_the_shortlinks_check_fails_each_reported_bug(tmp_path, switch, failing):
    result = _bench_check.run_hidden_check(
        _bench_suite.get("bugfix-shortlinks"), reference(tmp_path, **{switch: False}), key_vars=()
    )
    assert result["exit_code"] != 0
    assert "FAILED" in result["summary"] and failing in result["summary"]
    assert "1 failed, 5 passed" in result["summary"]


def test_a_pytest_module_in_the_case_does_not_stand_in_for_pytest(tmp_path):
    project = tmp_path / "case"
    project.mkdir()
    (project / "pytest.py").write_text("import sys\nsys.exit(0)\n")
    result = _bench_check.run_hidden_check(_bench_suite.get("bugfix-shortlinks"), project, key_vars=())
    assert result["exit_code"] not in (0, None), result["summary"]


LAUNCH = """
import json, sys
from pathlib import Path
from flyte_agent_factory import _bench_check, _bench_suite
import dataclasses
seal = sys.argv[1] == "seal"
if seal:
    _bench_check.seal()
read = (
    "import os, sys\\n"
    "try:\\n"
    "    found = b'BENCH_FAKE_API_KEY' in open(f'/proc/{os.getppid()}/environ', 'rb').read()\\n"
    "except OSError:\\n"
    "    found = False\\n"
    "print('KEY FOUND' if found else 'no key')\\n"
    "sys.exit(7 if found else 0)\\n"
)
task = dataclasses.replace(
    _bench_suite.get("bugfix-shortlinks"), check_files={}, check_commands=(("python", "-c", read),)
)
print(json.dumps(_bench_check.run_hidden_check(task, Path(sys.argv[2]), key_vars=("BENCH_FAKE_API_KEY",))))
"""


def launch(tmp_path, mode):
    import fcntl
    import os
    import sys
    import tempfile

    env = {**os.environ, "BENCH_FAKE_API_KEY": "fake-key-value"}
    # One launch at a time across parallel workers and runs: the sealed check scans this user's processes
    # for a readable key, and the unsealed launch (another test) leaves one readable while it runs.
    lock = Path(tempfile.gettempdir()) / f"factory-tests-key-launch-{os.getuid()}.lock"
    with open(lock, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        done = subprocess.run(
            [sys.executable, "-c", LAUNCH, mode, str(tmp_path)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_the_agents_code_cannot_read_the_key_from_its_parent(tmp_path):
    """The check task's own start-up environment holds the pod's API key and cannot be scrubbed: it is
    made unreadable instead (`seal`), and the agent's code reads /proc/<ppid>/environ and finds nothing."""
    result = launch(tmp_path, "seal")
    assert result["exit_code"] == 0 and "no key" in result["summary"], result


def test_an_unsealed_check_refuses_to_run_the_agents_code(tmp_path):
    result = launch(tmp_path, "open")
    assert result["exit_code"] is None and "refused" in result["summary"]
    assert "KEY FOUND" not in result["summary"]  # it never ran


def test_another_process_holding_the_key_refuses_the_check(tmp_path):
    import os
    import sys

    holder = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], env={**os.environ, "BENCH_FAKE_KEY_2": "x"}
    )
    try:
        task = dataclasses.replace(
            _bench_suite.get("bugfix-shortlinks"), check_files={}, check_commands=(("python", "-c", "pass"),)
        )
        result = _bench_check.run_hidden_check(task, tmp_path, key_vars=("BENCH_FAKE_KEY_2",))
    finally:
        holder.kill()
        holder.wait()
    assert result["exit_code"] is None and f"pid {holder.pid}" in result["summary"]


def test_the_check_runs_without_this_pods_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("_UNION_EAGER_API_KEY", "secret-value")
    leak = "import os, sys; sys.exit(3 if any('EAGER' in k for k in os.environ) else 0)"
    task = dataclasses.replace(
        _bench_suite.get("bugfix-shortlinks"),
        check_files={"x.txt": "hidden"},
        check_commands=(
            ("python", "-c", leak),
            ("python", "-c", "import sys; sys.exit(open(sys.argv[1]).read() != 'hidden')", "{hidden}/x.txt"),
        ),
    )
    project = tmp_path / "p"
    project.mkdir()
    assert _bench_check.run_hidden_check(task, project, key_vars=())["exit_code"] == 0


def test_a_hung_check_is_killed(tmp_path):
    task = dataclasses.replace(
        _bench_suite.get("bugfix-shortlinks"),
        check_commands=(("python", "-c", "import time; time.sleep(60)"),),
    )
    result = _bench_check.run_hidden_check(task, tmp_path, timeout=1, key_vars=())
    assert result["exit_code"] == 124 and "timed out after 1 s" in result["summary"]


# --------------------------------------------------------------------------- #
# Launch validation, the spot fallback and the login pool
# --------------------------------------------------------------------------- #


def test_a_launch_is_validated_and_estimated_before_anything_runs(monkeypatch):
    monkeypatch.setattr(_bench_flow, "PROFILE", profile())
    cases, estimate, go = _bench_flow.prepare("r1", SHA, ["bugfix-shortlinks"], 1, "{}", 0.0)
    assert not go and len(cases) == 1
    assert estimate == {
        "cases": 1,
        "expected_usd": 15.0,
        "ceiling_usd": 40.0,
        "max_usd": 0.0,
        "profile_max_usd": 100.0,
    }
    assert _bench_flow.prepare("r1", SHA, ["bugfix-shortlinks"], 1, "{}", 40.0)[2]
    for args, error in (
        (("r1", SHA, ["bugfix-shortlinks"], 1, "{}", 39.0), "can spend up to"),
        (("r1", SHA, ["bugfix-shortlinks"], 1, "{}", 101.0), "over the profile's"),
        (("r1", SHA, ["nope"], 1, "{}", 40.0), "no task 'nope'"),
        (("r1", SHA, ["bugfix-shortlinks"], 4, "{}", 200.0), "repeats must be 1 to 3"),
        (("r1", SHA, [], 1, '{"bad key": "x"}', 40.0), "not an uppercase policy key"),
        (("r1", SHA, [], 1, "[1]", 40.0), "policy must be a JSON object"),
    ):
        with pytest.raises(ValueError, match=error):
            _bench_flow.prepare(*args)
    monkeypatch.setattr(_bench_flow, "PROFILE", profile(""))
    with pytest.raises(ValueError, match="no .bench. table"):
        _bench_flow.prepare("r1", SHA, [], 1, "{}", 0.0)


def test_a_task_with_no_pinned_start_is_refused_at_launch(monkeypatch):
    monkeypatch.setattr(_bench_flow, "PROFILE", profile())
    task = _bench_suite.TASKS["bugfix-shortlinks"]
    monkeypatch.setitem(_bench_suite.TASKS, task.id, dataclasses.replace(task, start_tree=""))
    # An estimate-only launch (max_usd 0) is refused too: it would promise a run that cannot start.
    for max_usd in (0.0, 40.0):
        with pytest.raises(ValueError, match=r"no pinned start_tree.*bugfix-shortlinks.*ops/bench_seed.py"):
            _bench_flow.prepare("r1", SHA, ["bugfix-shortlinks"], 1, "{}", max_usd)


def test_a_child_falls_back_to_on_demand_when_spot_cannot_be_placed():
    import flyte
    import flyte.errors

    calls = []

    class Task:
        def override(self, **kw):
            calls.append(kw)

            async def aio(*args):
                if kw["interruptible"]:
                    raise flyte.errors.MaxQueuedTimeExceededError("queued too long")
                return args

            return type("Overridden", (), {"aio": staticmethod(aio)})()

    assert asyncio.run(_bench_flow._spot_first(Task(), "a", 1)) == ("a", 1)
    assert [c["interruptible"] for c in calls] == [True, False]
    assert calls[0]["timeout"].max_queued_time == _bench_flow.QUEUE_BOUND
    assert calls[1]["timeout"] == flyte.Timeout()


def test_each_in_flight_case_holds_its_own_bench_env(world, monkeypatch):
    holding: set[str] = set()
    most = []

    async def spot_first(task, case):
        env = json.loads(case)["env"]
        assert env not in holding
        holding.add(env)
        most.append(len(holding))
        await asyncio.sleep(0.01)
        holding.remove(env)
        return json.dumps({"case": json.loads(case)["case"], "env": env})

    monkeypatch.setattr(_bench_flow, "_spot_first", spot_first)
    cases = [{"case": f"c{i}"} for i in range(5)]
    records = asyncio.run(_bench_flow._run_all(cases, ["bench-a-1", "bench-a-2"]))
    assert [r["case"] for r in records] == ["c0", "c1", "c2", "c3", "c4"]  # plan order
    assert max(most) == 2 and {r["env"] for r in records} == {"bench-a-1", "bench-a-2"}


def test_one_case_failing_keeps_every_other_record(world, monkeypatch):
    async def spot_first(task, case):
        planned_case = json.loads(case)
        if planned_case["case"] == "c1":
            raise RuntimeError("harness pod lost")
        return json.dumps({"case": planned_case["case"], "outcome": "accepted"})

    monkeypatch.setattr(_bench_flow, "_spot_first", spot_first)
    cases = [planned(case=f"c{i}") for i in range(3)]
    records = asyncio.run(_bench_flow._run_all(cases, ["bench-a-1"]))
    assert [r["case"] if "case" in r else r["case_run"] for r in records] == ["c0", "c1", "c2"]
    assert records[1]["outcome"] == "harness_error" and "harness pod lost" in records[1]["error"]


def test_an_engine_already_deployed_is_not_deployed_again(monkeypatch):
    import flyte.errors

    monkeypatch.setattr(_bench_flow, "PROFILE", profile())
    deployed = {"bench-0123456-1.project"}
    fetched = []

    class Lazy:
        def __init__(self, name):
            self.name = name

        def fetch(self):
            if self.name not in deployed:
                raise flyte.errors.RemoteTaskNotFoundError(f"no {self.name}")

    monkeypatch.setattr(remote.Task, "get", lambda name, **kw: Lazy(name))
    calls = []

    def deploy_slots(profile, src, slots, **kw):
        calls.append((slots, kw["ref"], set(kw["base_env"]) & {"GITHUB_APP_PRIVATE_KEY", "_UNION_APP_KEY"}))
        return 0

    class App:
        def installation_token(self, repo):
            fetched.append(repo)
            return "token"

    class Tree:
        def __init__(self, url, sha):
            self.sha = sha

        def __enter__(self):
            return Path("/engine")

        def __exit__(self, *exc):
            return False

    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "pem")
    monkeypatch.setenv("_UNION_APP_KEY", "pem")  # Flyte's second name for the secret `app_key`
    monkeypatch.setattr(_deploy, "deploy_slots", deploy_slots)
    monkeypatch.setattr(_deploy, "fetched_tree", Tree)
    assert _bench_flow.deploy_engine_sync(cast(GithubApp, App()), SHA) == [
        "bench-0123456-1",
        "bench-0123456-2",
    ]
    assert calls == [(["bench-0123456-2"], SHA, set())]  # only the missing one, and never with the App key
    assert fetched == ["example-org/factory"]
    deployed.add("bench-0123456-2.project")
    calls.clear()
    _bench_flow.deploy_engine_sync(cast(GithubApp, App()), SHA)
    assert calls == []


# Scheduling remains probabilistic without the seed's sleep, even with overlapping HTTP requests.
@pytest.mark.slow  # runs the shortlinks hidden check: the benchmark lane
@pytest.mark.parametrize(
    "variant",
    [
        "both",
        "stale_visits",
        pytest.param(
            "no_sleep",
            marks=pytest.mark.xfail(
                reason="HTTP requests cannot force a thread switch in an unlocked update"
            ),
        ),
    ],
)
def test_hidden_check_rejects_other_incorrect_fixes(tmp_path, variant):
    switches = {"ATOMIC_VISITS": False}
    if variant == "both":
        switches["FREE_DELETED_SLUGS"] = False
    elif variant == "stale_visits":
        switches = {}
    project = reference(tmp_path, **switches)
    module = project / "shortlinks" / "__main__.py"
    text = module.read_text()
    if variant == "no_sleep":
        text = text.replace("time.sleep(0.02)", "pass")
    if variant == "stale_visits":
        text = text.replace(
            'db.execute("DELETE FROM links WHERE slug = ?", (slug,))',
            'old = db.execute("SELECT slug, url, created_at, visits FROM links WHERE slug = ?", '
            "(slug,)).fetchone()",
        )
        text = text.replace(
            "INSERT INTO links (slug, url, created_at) VALUES (?, ?, ?)",
            "INSERT OR REPLACE INTO links (slug, url, created_at, visits) VALUES (?, ?, ?, ?)",
        ).replace('(slug, body["url"], created)', '(slug, body["url"], created, old[3] if old else 0)')
    module.write_text(text)
    result = _bench_check.run_hidden_check(_bench_suite.get("bugfix-shortlinks"), project, key_vars=())
    assert result["exit_code"] != 0, result["summary"]
    assert "FAILED" in result["summary"]
    if variant == "both":
        assert "2 failed, 4 passed" in result["summary"]
    else:
        assert "1 failed, 5 passed" in result["summary"]
        failing = (
            "test_fifty_concurrent_visits_are_all_counted"
            if variant == "no_sleep"
            else "test_a_deleted_slug_can_be_registered_again"
        )
        assert failing in result["summary"]


def test_the_committed_seed_is_pinned_and_fails_the_hidden_check(tmp_path):
    from ops import bench_seed

    seed = REF.parent / "shortlinks_start"
    task = _bench_suite.get("bugfix-shortlinks")
    assert bench_seed.tree_id(seed) == task.start_tree
    project = tmp_path / "start"
    shutil.copytree(seed, project)
    result = _bench_check.run_hidden_check(task, project, key_vars=())
    assert result["exit_code"] != 0 and "5 failed, 1 passed" in result["summary"]


def test_limit_wait_does_not_exhaust_the_time_cap(world):
    case = planned()
    limited = snapshot("limited", since=world.t)
    started(world, case, limited)

    def wait_on_limit(w):
        # Under Flyte's max_runtime (the cap plus the stop window), which counts this wait too.
        w.t += 2 * 3600
        w.report(case["case"], {**limited, "generated_at": w.t})

    world.script += [
        wait_on_limit,
        lambda w: w.report(case["case"], snapshot("done")),
    ]
    _, _, harness = world.drive(case)
    assert harness["outcome"] == "accepted"
    assert harness["waited_on_limit_s"] >= 2 * 3600


def test_a_long_limit_wait_is_stopped_before_flytes_max_runtime(world):
    case = planned()
    limited = snapshot("limited", since=world.t)
    started(world, case, limited)

    def wait_on_limit(w):
        # Elapsed time (which excludes the wait) is nowhere near the cap, but Flyte's clock counts it all.
        w.t += 3 * 3600 - _bench_flow.POLL_S
        w.report(case["case"], {**limited, "generated_at": w.t})

    world.script += [wait_on_limit]
    _, _, harness = world.drive(case)
    assert harness["outcome"] == "capped_time"
    assert harness["waited_on_limit_s"] >= 2.9 * 3600
    assert ("signal", "direct-a1-001", "/stop") in world.events
    max_runtime = 3 * 3600 + _bench_flow.STOP_RETRY_S + _bench_flow.STOP_GRACE_S
    assert harness["ended_at"] - harness["started_at"] < max_runtime - _bench_flow.STOP_GRACE_S


def test_case_start_has_no_history_or_other_projects(bench_repo):
    task = dataclasses.replace(_bench_suite.get("bugfix-shortlinks"), start_tree="")
    case = planned()
    (bench_repo / "shortlinks-01").mkdir()
    (bench_repo / "shortlinks-01" / "answer").write_text("working answer")
    git(bench_repo, "add", ".")
    git(bench_repo, "-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-qm", "answer")
    _bench_flow.prepare_case(bench_repo.as_uri(), case, task)
    branch = f"bench/{case['case']}"
    assert git(bench_repo, "rev-list", "--count", branch) == "1"
    assert git(bench_repo, "ls-tree", "--name-only", branch) == "bench"
    assert not _bench_flow.cleanup_case(bench_repo.as_uri(), case)
    assert not git(bench_repo, "branch", "--list", branch)


def test_branch_cleanup_failures_are_recorded_not_retried(world, monkeypatch):
    case = planned()
    monkeypatch.setattr(_bench_flow, "_app", lambda: object())
    monkeypatch.setattr(_bench_flow, "_clone_url", lambda *args: "unused-url")
    monkeypatch.setattr(_bench_flow, "_bench_table", lambda: profile().bench)
    monkeypatch.setattr(
        _bench_flow,
        "drive_case",
        lambda *args, **kwargs: (
            {"name": case["case"], "url": ""},
            snapshot("done"),
            {
                "outcome": "accepted",
                "review_passed": True,
                "started_at": 1,
                "ended_at": 2,
                "asks": 0,
                "blocking_asks": 0,
                "forced_abort": False,
            },
        ),
    )

    async def checked(*args):
        return {"exit_code": 0, "summary": "passed"}

    monkeypatch.setattr(_bench_flow, "check_case", checked)
    monkeypatch.setattr(_bench_flow, "cleanup_case", lambda *args: "git push failed")
    record = json.loads(asyncio.run(_bench_flow.run_case.func(json.dumps(case))))
    assert record["accepted_verified"]
    assert record["cleanup_error"] == "git push failed"
    assert any("branch cleanup failed" in note for note in _bench.caveats([record]))


def test_seed_script_publishes_every_task_start_in_one_commit(bench_repo, monkeypatch, capsys):
    import sys

    from ops import bench_seed

    git(bench_repo, "config", "receive.denyCurrentBranch", "updateInstead")
    selected = profile()
    assert selected.bench is not None
    selected = dataclasses.replace(
        selected, bench=dataclasses.replace(selected.bench, repo=bench_repo.as_uri())
    )
    monkeypatch.setattr(_profile, "load", lambda *args: selected)
    monkeypatch.setattr(sys, "argv", ["bench_seed.py", "--profile", "unused"])
    before = git(bench_repo, "rev-list", "--count", "HEAD")
    bench_seed.main()
    printed = dict(line.split() for line in capsys.readouterr().out.strip().splitlines())
    assert set(printed) == set(_bench_suite.TASKS)
    for task in _bench_suite.TASKS.values():
        assert git(bench_repo, "rev-parse", f"HEAD:{task.workdir}") == printed[task.id]
        # A task that is already pinned must publish exactly the pinned tree.
        assert not task.start_tree or printed[task.id] == task.start_tree
    assert int(git(bench_repo, "rev-list", "--count", "HEAD")) == int(before) + 1


def test_benchmark_project_pins_are_hard(world):
    case = planned(policy={"CRAFT_ENGINE": "claude"})
    _bench_flow._launch(case, _bench_suite.get(case["task"]))
    assert world.fake.created[0]["inputs"]["hard_pin"] is True


@pytest.mark.slow  # runs the shortlinks hidden check: the benchmark lane
@pytest.mark.parametrize("change", ["edit", "delete", "no_regressions"])
def test_the_shortlinks_check_requires_preserved_tests_and_new_regressions(tmp_path, change):
    project = reference(tmp_path)
    if change == "no_regressions":
        originals = {path.name for path in (REF.parent / "shortlinks_start" / "tests").glob("*.py")}
        for path in (project / "tests").glob("*.py"):
            if path.name not in originals:
                path.unlink()
    else:
        original = project / "tests" / "test_import.py"
        if change == "edit":
            original.write_text("def test_replacement():\n    assert True\n")
        else:
            original.unlink()
    result = _bench_check.run_hidden_check(_bench_suite.get("bugfix-shortlinks"), project, key_vars=())
    assert result["exit_code"] != 0, result["summary"]
    failing = (
        "test_new_regression_tests_exist"
        if change == "no_regressions"
        else "test_original_tests_are_unchanged"
    )
    assert failing in result["summary"]


def test_the_shortlinks_reference_preserves_start_tests():
    for original in (REF.parent / "shortlinks_start" / "tests").glob("*.py"):
        assert (REF / "tests" / original.name).read_bytes() == original.read_bytes()


@pytest.mark.slow  # runs the shortlinks hidden check: the benchmark lane
@pytest.mark.parametrize(
    "alternative", ["locked_update", "physical_delete_and_moved_server", "pytest_suffix_filename"]
)
def test_the_shortlinks_check_accepts_alternative_fixes(tmp_path, alternative):
    project = reference(tmp_path)
    module = project / "shortlinks" / "__main__.py"
    text = module.read_text()
    if alternative == "locked_update":
        text = text.replace(
            'db.execute("UPDATE links SET visits = visits + 1 WHERE slug = ?", (slug,))',
            'seen = db.execute("SELECT visits FROM links WHERE slug = ?", (slug,)).fetchone()[0]\n'
            '                    db.execute("UPDATE links SET visits = ? WHERE slug = ?", (seen + 1, slug))',
        )
    elif alternative == "pytest_suffix_filename":
        regressions = project / "tests" / "test_regressions.py"
        regressions.rename(regressions.with_name("regressions_test.py"))
    else:
        text = text.replace("UPDATE links SET deleted = 1 WHERE slug = ?", "DELETE FROM links WHERE slug = ?")
        (module.parent / "server.py").write_text(text)
        text = "from shortlinks import server\n"
    module.write_text(text)
    result = _bench_check.run_hidden_check(_bench_suite.get("bugfix-shortlinks"), project, key_vars=())
    assert result["exit_code"] == 0, result["summary"]


@pytest.fixture
def benchmark_world(world, monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace

    class Store:
        def __init__(self):
            self.rows = []
            self.aa = []
            self.reuses = []
            self.summary = None

        def records(self, run=None):
            return [r for r in self.rows if run is None or r["bench_run"] == run]

        def completed(self, run):
            return {r["case_run"] for r in self.records(run)}

        def append_case(self, row):
            self.rows.append(row)

        def baseline_records(self, sha, policy, digest, task, repeat, age, model="opus"):
            # The real store's filters: same model, and a harness fault is never a baseline.
            self.reuses.append((sha, policy, digest, task, repeat, age))
            return next(
                (
                    r
                    for r in self.rows
                    if (r["engine_sha"], r["policy"], r["suite_digest"], r["task"], r["repeat"])
                    == (sha, policy, digest, task, repeat)
                    and r.get("model", "opus") == model
                    and r.get("outcome") not in _bench_store.RETRYABLE
                ),
                None,
            )

        def aa_history(self, *args):
            return []

        def write_aa(self, run, rows, **kwargs):
            self.aa.append((run, rows, kwargs))

        def write_summary(self, run, summary):
            self.summary = summary

    bench_profile = profile()
    assert bench_profile.bench is not None
    monkeypatch.setattr(
        _bench_flow,
        "PROFILE",
        dataclasses.replace(bench_profile, bench=dataclasses.replace(bench_profile.bench, max_usd=10000)),
    )
    build_tasks = [t for t in _bench_suite.TASKS.values() if t.kind == "build"][:2]
    monkeypatch.setattr(_bench_suite, "TASKS", {t.id: t for t in build_tasks})
    monkeypatch.setattr(_bench_flow, "start_of", lambda url, task: ("c" * 40, task.start_tree))
    store = Store()
    deployed, launched = [], []
    monkeypatch.setattr(_bench_flow, "_app", lambda: object())
    monkeypatch.setattr(_bench_flow, "_store", lambda app: nullcontext(store))
    monkeypatch.setattr(_bench_flow, "resolve_sha", lambda app, sha: sha)
    monkeypatch.setattr(flyte, "ctx", lambda: SimpleNamespace(action=SimpleNamespace(run_name="r1")))

    async def report(*args, **kwargs):
        pass

    async def deploy(sha):
        deployed.append(sha)
        return [f"bench-{sha[:7]}-1"]

    async def spot(task, payload):
        case = json.loads(payload)
        launched.append(case)
        started(world, case, snapshot("done", scores=[]))
        run, final, harness = world.drive(case)
        return json.dumps(_bench.case_record(case, run, final, harness, {"exit_code": 0, "summary": "pass"}))

    monkeypatch.setattr(flyte.report.replace, "aio", report)
    monkeypatch.setattr(_bench_flow, "deploy_engine", deploy)
    monkeypatch.setattr(_bench_flow, "_spot_first", spot)
    return store, deployed, launched


def test_benchmark_two_arms_abba_and_verdict(benchmark_world):
    store, deployed, launched = benchmark_world
    baseline = "f" * 40
    summary, _ = asyncio.run(_bench_flow.benchmark.func(SHA, baseline_sha=baseline, max_usd=1000))
    assert deployed == [SHA, baseline]
    for task in {c["task"] for c in launched}:
        assert [c["arm"] for c in launched if c["task"] == task] == [
            "candidate",
            "baseline",
            "baseline",
            "candidate",
        ]
    assert all(a["task"] != b["task"] for a, b in zip(launched, launched[1:], strict=False))
    assert len({c["case"] for c in launched}) == len(launched)
    result = json.loads(summary)
    assert result["verdict"]["verdict"] == "inconclusive"
    assert result["verdict"]["band_source"] == "assumed"
    assert store.summary == result
    assert "no A/A band" in _bench.render_report(store.rows, result, title="test")


def test_benchmark_reuses_baseline(benchmark_world):
    store, _, launched = benchmark_world
    for case in _bench.plan_cases("old", "f" * 40, [], 2, {}, "opus"):
        # A measured baseline; a harness fault is never reused (the real store's rule).
        measured = {**_bench.error_record({**case, "env": "old-env"}, "old", now=1), "outcome": "failed"}
        store.rows.append(measured)
    summary, jsonl = asyncio.run(_bench_flow.benchmark.func(SHA, baseline_sha="f" * 40, max_usd=1000))
    assert store.reuses
    assert {c["arm"] for c in launched} == {"candidate"}
    assert len(json.loads(summary)["reused_baselines"]) == len(launched)
    rows = [json.loads(line) for line in jsonl.splitlines()]
    assert len(rows) == len(launched) * 2
    assert all(r["bench_run"] == "r1" for r in rows)


def test_aa_is_recorded_and_never_reused(benchmark_world):
    store, _, launched = benchmark_world
    summary, _ = asyncio.run(_bench_flow.benchmark.func(SHA, baseline_sha=SHA, max_usd=1000))
    assert not store.reuses
    assert len(store.aa) == 1
    assert store.aa[0][2]["sigma_aa"] == 0
    assert json.loads(summary)["calibration"]["qualifies"] is False
    assert {c["arm"] for c in launched} == {"candidate", "baseline"}


def test_benchmark_single_arm_keeps_candidate_plan(benchmark_world):
    store, deployed, launched = benchmark_world
    summary, _ = asyncio.run(_bench_flow.benchmark.func(SHA, repeats=1, max_usd=1000))
    assert deployed == [SHA]
    assert all(c["arm"] == "candidate" for c in launched)
    assert "verdict" not in json.loads(summary)
    assert not store.aa and not store.reuses


def test_quota_guard_waits_for_both_resets(world, monkeypatch, capsys):
    from flyte_agent_factory import _meter

    reads = []
    monkeypatch.setattr(_factory, "owner_demand", lambda: True)

    async def read(login):
        reads.append(login)
        # Both windows are exhausted; each only clears at its own reset.
        return _meter.Meter(
            "claude test",
            "fake",
            world.t,
            100 if world.t < 1000100 else 0,
            1000100,
            100 if world.t < 1000200 else 60,
            1000200,
        )

    async def sleep(seconds):
        world.sleep(seconds)

    monkeypatch.setattr(_bench_flow, "_read_meter", read)
    meter = asyncio.run(_bench_flow._quota_guard("test", clock=world.clock, sleep=sleep))
    assert world.t == 1000200
    assert len(reads) >= 2
    assert meter.weekly_pct == 60
    assert "quota_wait" in capsys.readouterr().out


def test_aa_with_six_build_tasks_qualifies(benchmark_world, monkeypatch):
    store, _, _ = benchmark_world
    task = next(iter(_bench_suite.TASKS.values()))
    monkeypatch.setattr(
        _bench_suite, "TASKS", {f"build-{i}": dataclasses.replace(task, id=f"build-{i}") for i in range(6)}
    )
    summary, _ = asyncio.run(_bench_flow.benchmark.func(SHA, baseline_sha=SHA, max_usd=1000))
    calibration = json.loads(summary)["calibration"]
    assert calibration["qualifies"] is True
    assert calibration["sigma_aa"] == store.aa[0][2]["sigma_aa"] == 0
    assert len(store.aa[0][1]) == 24


@pytest.mark.parametrize("reset", [None, 1000000 + 9 * 86400])
def test_quota_guard_refuses_an_unbounded_wait(world, monkeypatch, reset):
    monkeypatch.setattr(_factory, "owner_demand", lambda: True)
    from flyte_agent_factory import _meter

    async def read(login):
        return _meter.Meter("claude test", "fake", world.t, 100, reset)

    monkeypatch.setattr(_bench_flow, "_read_meter", read)
    with pytest.raises(_bench_flow.HarnessError, match="quota guard"):
        asyncio.run(_bench_flow._quota_guard("test", clock=world.clock))
    assert world.t == 1000000


def test_two_arm_estimate_and_refusal_cover_both_arms(world):
    args = ("r1", SHA, ["bugfix-shortlinks"], 1, "{}")
    cases, estimate, go = _bench_flow.prepare(*args, 0, "f" * 40)
    assert not go and len(cases) == 2
    assert estimate["ceiling_usd"] == 80
    with pytest.raises(ValueError, match="can spend up to"):
        _bench_flow.prepare(*args, 40, "f" * 40)


def test_benchmark_waits_on_login_meter_before_launch(benchmark_world, world, monkeypatch, capsys):
    monkeypatch.setattr(_factory, "owner_demand", lambda: True)
    from flyte_agent_factory import _meter

    reset = world.t + 120
    original_guard = _bench_flow._quota_guard
    original_driver = _bench_flow.drive_case

    async def read(login):
        assert not world.fake.created
        return _meter.Meter("claude test", "fake", world.t, 100 if world.t < reset else 10, reset)

    async def sleep(seconds):
        world.t += seconds

    async def guard(login):
        return await original_guard(login, clock=world.clock, sleep=sleep)

    async def check(*args):
        return {"exit_code": 0, "summary": "pass"}

    async def spot(task, payload):
        case = json.loads(payload)
        started(world, case, snapshot("done", scores=[]))
        return await _bench_flow.run_case.func(payload)

    monkeypatch.setattr(_bench_flow, "_read_meter", read)
    monkeypatch.setattr(_bench_flow, "_quota_guard", guard)
    monkeypatch.setattr(_bench_flow, "_clone_url", lambda *args: "unused")
    monkeypatch.setattr(_bench_flow, "check_case", check)
    monkeypatch.setattr(_bench_flow, "cleanup_case", lambda *args: None)
    monkeypatch.setattr(_bench_flow, "_spot_first", spot)
    monkeypatch.setattr(
        _bench_flow,
        "drive_case",
        lambda case, url, **kw: original_driver(case, url, clock=world.clock, sleep=world.sleep),
    )
    _, jsonl = asyncio.run(
        _bench_flow.benchmark.func(SHA, tasks=["bugfix-shortlinks"], repeats=1, max_usd=1000)
    )
    row = json.loads(jsonl)
    assert row["started_at"] >= reset
    assert row["meter_at_start"] == {"five_h_pct": 10, "week_pct": None}
    assert row["accepted_verified"]
    assert "quota_wait" in capsys.readouterr().out


def test_aa_sigma_is_measured_from_this_run(benchmark_world, monkeypatch):
    import math

    store, _, _ = benchmark_world
    original = _bench_flow._spot_first

    async def spot(task, payload):
        row = json.loads(await original(task, payload))
        if row["arm"] == "candidate":
            row["usd_total"] *= 2
        return json.dumps(row)

    monkeypatch.setattr(_bench_flow, "_spot_first", spot)
    summary, _ = asyncio.run(_bench_flow.benchmark.func(SHA, baseline_sha=SHA, max_usd=1000))
    assert json.loads(summary)["calibration"]["sigma_aa"] == pytest.approx(math.log(2))
    assert store.aa[0][2]["sigma_aa"] == pytest.approx(math.log(2))


def test_same_sha_with_different_policies_is_an_experiment(benchmark_world):
    store, _, launched = benchmark_world
    asyncio.run(
        _bench_flow.benchmark.func(
            SHA, baseline_sha=SHA, candidate_policy='{"CRAFT_EFFORT": "low"}', max_usd=1000
        )
    )
    assert not store.aa
    assert store.reuses
    assert {c["arm"] for c in launched} == {"candidate", "baseline"}


def test_both_arms_share_the_same_login_lock(benchmark_world, monkeypatch):
    active = set()
    original = _bench_flow._spot_first

    async def spot(task, payload):
        case = json.loads(payload)
        login = case["login"]
        assert login not in active
        active.add(login)
        await asyncio.sleep(0.01)
        result = await original(task, payload)
        active.remove(login)
        return result

    monkeypatch.setattr(_bench_flow, "_spot_first", spot)
    _, jsonl = asyncio.run(_bench_flow.benchmark.func(SHA, baseline_sha="f" * 40, max_usd=1000))
    rows = [json.loads(line) for line in jsonl.splitlines()]
    assert len(rows) == 8
    assert all(row["outcome"] == "accepted" for row in rows)
    assert {row["env"] for row in rows} == {f"bench-{SHA[:7]}-1", "bench-fffffff-1"}


def test_bench_meter_does_not_deploy_production_environment(monkeypatch):
    from flyte_agent_factory import _meter

    overrides = []

    class Reader:
        def override(self, **kwargs):
            overrides.append(kwargs)
            return self

        async def aio(self, account, poll):
            assert poll
            return json.dumps(_meter.Meter(account, "fake", 1).to_dict())

    monkeypatch.setattr(_bench_flow, "read_login_meter", Reader())
    meter = asyncio.run(_bench_flow._read_meter("bench_login_one"))
    assert meter.account == "claude bench_login_one"
    (secret,) = overrides[0]["secrets"]
    assert secret.key == "bench_login_one"
    assert secret.as_env_var == "CLAUDE_CODE_OAUTH_TOKEN"
    assert _factory.env not in _bench_flow.env.depends_on


# QA step 5b, with a real results store on a local bare repo.
BASE = "f" * 40


@pytest.fixture
def bare(tmp_path):
    repo = tmp_path / "direction.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(repo)], check=True)
    work = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    (work / "README").write_text("x")
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(work), "-c", "user.name=a", "-c", "user.email=a@b", "commit", "-qm", "s"],
        check=True,
    )
    subprocess.run(["git", "-C", str(work), "push", "-q", str(repo), "main"], check=True)
    return f"file://{repo}"


def _wire_real_store(monkeypatch, url):
    monkeypatch.setattr(_bench_flow, "_store", lambda app: _bench_store.Store(url))


def _row(case, outcome="accepted", ended=None, model="opus", usd=1.0):
    rec = _bench.error_record({**case, "env": "old"}, "x", now=time.time() - 3600)
    rec.update(outcome=outcome, model=model, usd_total=usd, accepted_verified=outcome == "accepted")
    if ended:
        rec["ended_at"] = ended
    return rec


def test_reuse_real_store_and_resume_label(benchmark_world, monkeypatch, bare):
    store_fake, deployed, launched = benchmark_world
    _wire_real_store(monkeypatch, bare)
    old = _bench.plan_cases("old", BASE, [], 2, {}, "opus")
    with _bench_store.Store(bare) as s:
        for case in old:
            # task0 rep1: harness fault; task0 rep2: wrong model; task1 rep1: good; task1 rep2: too old (8d)
            kind = [
                ("harness_error", None, "opus"),
                ("accepted", None, "sonnet"),
                ("accepted", None, "opus"),
                ("accepted", time.time() - 8 * 86400, "opus"),
            ]
            o, e, m = kind[
                [(0, 1), (0, 2), (1, 1), (1, 2)].index(
                    (sorted({c["task"] for c in old}).index(case["task"]), case["repeat"])
                )
            ]
            s.append_case(_row(case, o, e, m))
    summary, jsonl = asyncio.run(_bench_flow.benchmark.func(SHA, baseline_sha=BASE, max_usd=1000))
    result = json.loads(summary)
    assert len(result["reused_baselines"]) == 1
    # resume: same bench_run
    launched.clear()
    summary2, _ = asyncio.run(_bench_flow.benchmark.func(SHA, baseline_sha=BASE, max_usd=1000))
    r2 = json.loads(summary2)
    rows = [json.loads(line) for line in jsonl.splitlines()]
    reused_row = next(r for r in rows if r["case_run"] == result["reused_baselines"][0]["case_run"])
    assert reused_row["reused_from"]["bench_run"] == "old"
    assert r2["reused_baselines"], "resume dropped the reused label"


def test_resume_two_arms_real_store(benchmark_world, monkeypatch, bare):
    store_fake, deployed, launched = benchmark_world
    _wire_real_store(monkeypatch, bare)
    original = _bench_flow._spot_first
    calls = {"n": 0}

    async def flaky(task, payload):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("boom")
        return await original(task, payload)

    monkeypatch.setattr(_bench_flow, "_spot_first", flaky)
    summary, jsonl = asyncio.run(_bench_flow.benchmark.func(SHA, baseline_sha=BASE, max_usd=1000))
    rows = [json.loads(line) for line in jsonl.splitlines()]
    # The third launch fails before the fake records it: seven launched, one harness fault recorded.
    assert len(launched) == 7 and sum(r["outcome"] == "harness_error" for r in rows) == 1
    launched.clear()
    summary, jsonl = asyncio.run(_bench_flow.benchmark.func(SHA, baseline_sha=BASE, max_usd=1000))
    rows = [json.loads(line) for line in jsonl.splitlines()]
    assert len(launched) == 1
    assert all(r["outcome"] == "accepted" for r in rows) and len(rows) == 8


# Bench logins are never slot logins, so owner demand leaves them their whole window (no reserve).
@pytest.mark.parametrize(
    "demand,used,wait",
    [(False, 99, False), (True, 99, False), (True, 75, False), (True, 100, True), (False, 100, True)],
)
def test_quota_waits_only_for_an_exhausted_window(world, monkeypatch, demand, used, wait):
    from flyte_agent_factory import _meter

    reset = world.t + 120
    monkeypatch.setattr(_factory, "owner_demand", lambda: demand)

    async def read(login):
        return _meter.Meter("claude test", "fake", world.t, used if world.t < reset else 0, reset)

    async def sleep(seconds):
        world.t += seconds

    monkeypatch.setattr(_bench_flow, "_read_meter", read)
    asyncio.run(_bench_flow._quota_guard("test", clock=world.clock, sleep=sleep))
    assert (world.t >= reset) == wait


def test_quota_resumes_when_a_reread_shows_capacity_before_the_reset(world, monkeypatch):
    from flyte_agent_factory import _meter

    start = world.t

    async def read(login):
        return _meter.Meter("claude test", "fake", world.t, 100 if world.t == start else 50, start + 3600)

    async def sleep(seconds):
        world.t += seconds

    monkeypatch.setattr(_bench_flow, "_read_meter", read)
    asyncio.run(_bench_flow._quota_guard("test", clock=world.clock, sleep=sleep))
    assert world.t == start + 60


def test_owner_aa_ignores_the_loop_switch_and_breaker(benchmark_world, monkeypatch):
    monkeypatch.setattr(
        _bench_flow,
        "PROFILE",
        dataclasses.replace(
            _bench_flow.PROFILE, loop=dataclasses.replace(_bench_flow.PROFILE.loop, enabled=True)
        ),
    )
    monkeypatch.setattr(_bench_flow, "_loop_origin", lambda: False)
    monkeypatch.setattr(_loop, "killed", lambda *args: True)
    monkeypatch.setattr(_loop, "open_breaker", lambda *args: pytest.fail("owner A/A must keep running"))
    original = _bench_stats.verdict

    def alarm(*args):
        return {**original(*args), "verdict": "worse"}

    monkeypatch.setattr(_bench_stats, "verdict", alarm)
    summary, cases = asyncio.run(_bench_flow.benchmark.func(SHA, baseline_sha=SHA, max_usd=1000))
    assert json.loads(summary)["verdict"]["verdict"] == "worse"
    assert len(cases.splitlines()) == 8


def test_loop_aa_does_not_retrip_on_alarm_before_owner_close(benchmark_world, monkeypatch):
    from types import SimpleNamespace

    created = []
    app = SimpleNamespace(
        app_login=lambda: "factory[bot]",
        labelled_issues=lambda *args: [{"number": 1}],
        issue_events=lambda *args: [
            {
                "event": "closed",
                "actor": {"login": "owner"},
                "created_at": "2026-10-25T11:00:00Z",
            }
        ],
        create_issue=lambda *args: created.append(args),
    )
    monkeypatch.setattr(_bench_flow, "_app", lambda: app)
    monkeypatch.setattr(_bench_flow, "_loop_origin", lambda: True)
    monkeypatch.setattr(
        _bench_flow,
        "PROFILE",
        dataclasses.replace(
            _bench_flow.PROFILE,
            loop=dataclasses.replace(_bench_flow.PROFILE.loop, enabled=True, repo="acme/factory"),
            github=dataclasses.replace(_bench_flow.PROFILE.github, maintainers=("owner",)),
        ),
    )
    original = _bench_stats.verdict

    def alarm(*args):
        return {**original(*args), "verdict": "worse"}

    monkeypatch.setattr(_bench_stats, "verdict", alarm)
    asyncio.run(_bench_flow.benchmark.func(SHA, baseline_sha=SHA, max_usd=1000))
    assert not created


def test_harness_breaker_streak_starts_after_owner_close(benchmark_world, monkeypatch):
    from datetime import datetime, timezone
    from types import SimpleNamespace

    store, _, _ = benchmark_world
    restart = datetime(2026, 10, 25, 11, tzinfo=timezone.utc).timestamp()
    created = []
    app = SimpleNamespace(
        labelled_issues=lambda *args: [{"number": 1}],
        issue_events=lambda *args: [
            {"event": "closed", "actor": {"login": "owner"}, "created_at": "2026-10-25T11:00:00Z"}
        ],
        create_issue=lambda *args: created.append(args),
    )
    monkeypatch.setattr(
        _bench_flow,
        "PROFILE",
        dataclasses.replace(
            _bench_flow.PROFILE,
            loop=dataclasses.replace(_bench_flow.PROFILE.loop, enabled=True, repo="acme/factory"),
            github=dataclasses.replace(_bench_flow.PROFILE.github, maintainers=("owner",)),
        ),
    )
    store.rows.extend(
        {"case_run": f"old-{index}", "ended_at": restart - index, "outcome": "harness_error"}
        for index in (1, 2)
    )
    save = _bench_flow._saver(cast(GithubApp, app), {}, loop_origin=True)  # a stub; see its methods
    save({"case_run": "new", "ended_at": restart + 1, "outcome": "harness_error"})
    assert not created


def test_every_case_runs_on_the_one_token_when_the_bench_names_no_logins(world, monkeypatch):
    """#84: `[bench]` has no logins; every case, whichever env it takes, runs on the production token."""
    configured = profile()
    assert configured.bench is not None and configured.bench.claude_token == "production_token"
    used = []

    async def spot_first(task, encoded):
        case = json.loads(encoded)
        used.append((case["env"], case["login"]))
        return json.dumps({"case": case["case"], "env": case["env"]})

    monkeypatch.setattr(_bench_flow, "_spot_first", spot_first)
    monkeypatch.setattr(_bench_flow, "_case_live", lambda name: False)
    records = asyncio.run(_bench_flow._run_all([{"case": f"c{i}"} for i in range(5)], ["bench-1", "bench-2"]))
    assert len(records) == len(used) == 5
    assert {login for _, login in used} == {"production_token"}
    assert {env for env, _ in used} == {"bench-1", "bench-2"}


@pytest.mark.parametrize("safe_k", [1, 2])
def test_shared_bench_token_uses_the_probed_concurrency(world, monkeypatch, safe_k):
    configured = profile()
    assert configured.bench is not None
    monkeypatch.setattr(
        _bench_flow,
        "PROFILE",
        dataclasses.replace(
            configured,
            bench=dataclasses.replace(configured.bench, claude_token="bench_token", token_concurrency=safe_k),
        ),
    )
    active = 0
    maximum = 0

    async def spot_first(task, encoded):
        nonlocal active, maximum
        case = json.loads(encoded)
        assert case["login"] == "bench_token"
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return json.dumps({"case": case["case"], "env": case["env"]})

    monkeypatch.setattr(_bench_flow, "_spot_first", spot_first)
    monkeypatch.setattr(_bench_flow, "_case_live", lambda name: False)
    records = asyncio.run(_bench_flow._run_all([{"case": f"c{i}"} for i in range(5)], ["bench-1", "bench-2"]))
    assert len(records) == 5
    assert maximum == safe_k
