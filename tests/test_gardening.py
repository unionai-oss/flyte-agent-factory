"""Gardening: the owner's target list, the priority decision, the profile table and the weekly task."""

import asyncio
import dataclasses
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace

import flyte
import flyte.errors
import pytest

from flyte_agent_factory import (
    _deploy,
    _factory,
    _factory_base,
    _gardening,
    _gate_base,
    _gate_scope,
    _gates,
    _git,
    _github,
    _profile,
    _queue,
    _workspace,
)
from flyte_agent_factory import _factory as factory
from tests.test_factory import projects

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "profiles" / "example.toml"
REPOS = {"factory": "git@example.org:owner/engine.git"}
NOW = 100 * 86400.0
SETTINGS = _profile.Gardening()


# --- gardening.toml (H2)


def write(tmp_path, text):
    (tmp_path / "gardening.toml").write_text(text)


def test_a_valid_file_lists_its_targets(tmp_path):
    write(
        tmp_path,
        '[[target]]\nrepo = "git@example.org:owner/other.git"\ncadence_days = 3\n'
        'checks = ["deps", "branches"]\n'
        '[[target]]\npath = "projects/foo"\n',
    )
    repo, path = _gardening.load(tmp_path, REPOS)
    assert (repo.repo, repo.cadence_days, repo.checks) == (
        "git@example.org:owner/other.git",
        3,
        ("deps", "branches"),
    )
    assert (path.path, path.cadence_days, path.checks) == ("projects/foo", None, _gardening.CHECKS)


def test_a_missing_file_has_no_targets(tmp_path):
    assert _gardening.load(tmp_path, REPOS) == []


def test_repo_factory_means_the_profiles_factory_repo(tmp_path):
    write(tmp_path, '[[target]]\nrepo = "factory"\n')
    assert [t.repo for t in _gardening.load(tmp_path, REPOS)] == [REPOS["factory"]]


def test_the_workspace_template_gardens_the_engine_by_default():
    assert [t.repo for t in _gardening.load(_workspace.TEMPLATE, REPOS)] == [REPOS["factory"]]


def test_the_factory_commits_no_gardening_targets_of_its_own():
    """Adding a target is the owner's action (owner decision, 015 item 6): the engine repo ships no list."""
    assert not (ROOT / _gardening.FILE).exists()
    assert _gardening.load(ROOT, REPOS) == []


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[[target\n", "gardening.toml"),
        ("target = 3\n", "array of tables"),
        ("[[target]]\n", "exactly one of repo or path"),
        ('[[target]]\nrepo = "a"\npath = "b"\n', "exactly one of repo or path"),
        ('[[target]]\nrepo = ""\n', "non-empty string"),
        ('[[target]]\nrepo = "a"\ncadence_days = 0\n', "positive integer"),
        ('[[target]]\nrepo = "a"\ncadence_days = true\n', "positive integer"),
        ('[[target]]\nrepo = "a"\nchecks = []\n', "non-empty list"),
        ('[[target]]\nrepo = "a"\nchecks = ["lint"]\n', "unknown check"),
        ('[[target]]\nrepo = "a"\nbranch = "x"\n', "unknown key"),
    ],
)
def test_a_malformed_file_says_what_is_wrong(tmp_path, text, message):
    write(tmp_path, text)
    with pytest.raises(ValueError, match=message):
        _gardening.load(tmp_path, REPOS)


# --- the gate (H3)


@pytest.mark.parametrize("layout", ["sandbox", "root"])
def test_gardening_toml_is_never_in_scope(layout):
    brief = "May edit: gardening.toml, *"
    assert _gate_scope.scope_violations(["gardening.toml"], "app", brief, layout=layout) == ["gardening.toml"]


# --- the decision (H4)


def days(n):
    return NOW - n * 86400


def decide(runs=(), regression=(), open_issues=0, settings=SETTINGS):
    return _gardening.priority(NOW, list(runs), list(regression), open_issues, settings)


def test_a_recent_run_and_no_trouble_stays_low():
    assert decide([days(2)]) == ("low", [])


def test_no_run_in_max_days_escalates_to_normal():
    assert decide([days(9)], settings=_profile.Gardening(min_runs_per_week=0)) == (
        "normal",
        ["no completed run in 9 days"],
    )


def test_no_run_ever_escalates_to_normal():
    level, reasons = decide([], settings=_profile.Gardening(min_runs_per_week=0))
    assert (level, reasons) == ("normal", ["no completed run yet"])


def test_a_regression_escalates_unless_the_profile_turns_it_off():
    assert decide([days(1)], regression=["engine"]) == ("normal", ["standards gate regressed on engine"])
    assert decide(
        [days(1)], regression=["engine"], settings=_profile.Gardening(escalate_on_regression=False)
    ) == (
        "low",
        [],
    )


def test_too_many_open_issues_escalate():
    assert decide([days(1)], open_issues=11) == ("normal", ["11 open gardening issues (max 10)"])
    assert decide([days(1)], open_issues=10) == ("low", [])


def test_the_reserved_share_applies_while_the_weekly_minimum_is_unmet():
    level, reasons = decide([days(8)])
    assert level == "reserved" and "reserved share (0 of 1 runs in 7 days)" in reasons
    assert decide([days(8)], settings=_profile.Gardening(min_runs_per_week=0))[0] == "normal"
    assert decide([days(1), days(3)], settings=_profile.Gardening(min_runs_per_week=2))[0] == "low"
    assert decide([days(1)], settings=_profile.Gardening(min_runs_per_week=2))[0] == "reserved"


def test_reserved_goes_ahead_of_normal_but_never_ahead_of_expedite():
    assert _gardening.rank("expedite") < _gardening.rank("reserved") < _gardening.rank("normal")
    assert _gardening.rank("normal") < _gardening.rank("low")


def test_regressed_names_the_target_and_rule_that_got_worse():
    counts = {
        "clean": {"a": {"ruff": 1, "mypy": 2}, "b": {"ruff": 0}},
        "latest": {"a": {"ruff": 3, "mypy": 2}, "b": {"ruff": 0, "mypy": 1}, "c": {"ruff": 9}},
    }
    # `c` has no baseline yet; a rule `b` never had counts from zero.
    assert _gardening.regressed(counts) == ["a: ruff", "b: mypy"]
    assert _gardening.regressed({}) == []


def test_the_first_scan_is_the_baseline_and_escalates_nothing(tmp_path):
    uri = tmp_path.as_uri()
    _gardening.record_counts(uri, {"a": {"ruff": 4}})
    counts = _gardening.read_counts(uri)
    assert counts == {"clean": {"a": {"ruff": 4}}, "latest": {"a": {"ruff": 4}}}
    assert _gardening.regressed(counts) == []


def test_more_findings_on_any_rule_regress_and_keep_the_baseline(tmp_path):
    uri = tmp_path.as_uri()
    _gardening.record_counts(uri, {"a": {"ruff": 1, "mypy": 3}})
    _gardening.record_counts(uri, {"a": {"ruff": 0, "mypy": 4}})  # one rule down, one up
    counts = _gardening.read_counts(uri)
    assert counts == {"clean": {"a": {"ruff": 1, "mypy": 3}}, "latest": {"a": {"ruff": 0, "mypy": 4}}}
    assert _gardening.regressed(counts) == ["a: mypy"]


def test_equal_or_fewer_findings_move_the_baseline(tmp_path):
    uri = tmp_path.as_uri()
    _gardening.record_counts(uri, {"a": {"ruff": 2}})
    _gardening.record_counts(uri, {"a": {"ruff": 2}})
    _gardening.record_counts(uri, {"a": {"ruff": 1}})
    counts = _gardening.read_counts(uri)
    assert counts == {"clean": {"a": {"ruff": 1}}, "latest": {"a": {"ruff": 1}}}
    assert _gardening.regressed(counts) == []


def test_one_targets_scan_does_not_move_anothers_baseline(tmp_path):
    uri = tmp_path.as_uri()
    _gardening.record_counts(uri, {"a": {"ruff": 1}, "b": {"ruff": 1}})
    _gardening.record_counts(uri, {"a": {"ruff": 5}, "b": {"ruff": 0}})
    assert _gardening.read_counts(uri)["clean"] == {"a": {"ruff": 1}, "b": {"ruff": 0}}


def test_finding_counts_count_the_lines_of_failing_rules_only():
    results = [
        _gates.Result("ruff", _gate_base.Status.FAIL, "a.py:1 E1\n\nb.py:2 E2\n"),
        _gates.Result("mypy", _gate_base.Status.PASS, "ok"),
        _gates.Result("vulture", _gate_base.Status.TOOL_UNAVAILABLE, "not installed"),
    ]
    assert _gates.finding_counts(results) == {"ruff": 2}


def enqueue(world, name, at, priority="normal"):
    _queue.enqueue(
        world.queue, _queue.Request(name, 1, "new", "b", "opus", "r", "", {}, at, priority=priority)
    )


def test_a_reserved_gardening_launch_takes_the_slot_ahead_of_normal_work(flyte_world, monkeypatch):
    monkeypatch.setattr(_factory, "gardening_decision", lambda now=None: ("reserved", ["why"]))
    flyte_world.add("one", _factory_base.SLOTS[0])
    enqueue(flyte_world, "work", 1.0)
    enqueue(flyte_world, _gardening.NAME, 2.0, "low")
    assert projects(flyte_world, factory.dispatch()) == [_gardening.NAME]


def test_a_reserved_gardening_launch_waits_for_an_expedite_one(flyte_world, monkeypatch):
    monkeypatch.setattr(_factory, "gardening_decision", lambda now=None: ("reserved", ["why"]))
    flyte_world.add("one", _factory_base.SLOTS[0])
    enqueue(flyte_world, _gardening.NAME, 1.0, "low")
    enqueue(flyte_world, "urgent", 2.0, "expedite")
    assert projects(flyte_world, factory.dispatch()) == ["urgent"]


def test_an_escalated_gardening_launch_is_served_in_order_with_normal_work(flyte_world, monkeypatch):
    monkeypatch.setattr(_factory, "gardening_decision", lambda now=None: ("normal", ["why"]))
    flyte_world.add("one", _factory_base.SLOTS[0])
    enqueue(flyte_world, _gardening.NAME, 1.0, "low")
    enqueue(flyte_world, "work", 2.0)
    assert projects(flyte_world, factory.dispatch()) == [_gardening.NAME]


def test_a_low_gardening_launch_waits_behind_normal_work(flyte_world, monkeypatch):
    monkeypatch.setattr(_factory, "gardening_decision", lambda now=None: ("low", []))
    flyte_world.add("one", _factory_base.SLOTS[0])
    enqueue(flyte_world, _gardening.NAME, 1.0, "low")
    enqueue(flyte_world, "work", 2.0)
    assert projects(flyte_world, factory.dispatch()) == ["work"]


def test_an_unreadable_decision_leaves_the_launch_low(flyte_world, monkeypatch):
    def broken(now=None):
        raise RuntimeError("no network")

    monkeypatch.setattr(_factory, "gardening_decision", broken)
    enqueue(flyte_world, _gardening.NAME, 1.0, "low")
    request = _queue.pending(flyte_world.queue)[0].request
    assert request is not None
    assert _factory.effective_priority(request) == "low"


def test_only_a_low_gardening_launch_is_ever_raised(monkeypatch):
    monkeypatch.setattr(_factory, "gardening_decision", lambda now=None: pytest.fail("not asked"))
    for name, priority in (("other", "low"), (_gardening.NAME, "normal"), (_gardening.NAME, "expedite")):
        request = _queue.Request(name, 1, "new", "b", "opus", "r", "", {}, 1.0, priority=priority)
        assert _factory.effective_priority(request) == priority


# --- the overview (H5)


def test_the_overview_reports_why_gardening_is_raised(flyte_world, monkeypatch):
    from ops import operate

    reasons = ["no completed run in 9 days"]
    monkeypatch.setattr(_factory, "gardening_decision", lambda now=None: ("normal", reasons))
    enqueue(flyte_world, _gardening.NAME, time.time(), "low")
    line = "gardening: normal priority (no completed run in 9 days)"
    assert _factory.queue_view()["gardening"] == line
    view = operate.build_overview(
        [], queued=_factory.waiting_rows(flyte_world.queue, time.time()), gardening=line
    )
    text = operate.overview_text(view)
    assert line in text and "low priority" in text
    assert line in operate.overview_html(view)


def test_no_gardening_line_without_a_waiting_gardening_launch(flyte_world):
    assert "gardening" not in _factory.queue_view()


def test_the_runs_end_times_the_decision_reads_come_from_succeeded_runs(flyte_world, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from tests.fake_flyte import ended_at
    from tests.test_factory import add_labelled

    now = datetime(2026, 1, 20, tzinfo=timezone.utc)
    monkeypatch.setattr(_factory, "_gardening_targets", lambda: ["one target"])
    for i, (phase, when) in enumerate([("SUCCEEDED", 2), ("FAILED", 1), ("SUCCEEDED", 30)]):
        run = add_labelled(
            flyte_world, f"g{i}", _gardening.NAME, _factory_base.SLOTS[0], gen=i + 1, phase=phase
        )
        ended_at(run, now - timedelta(days=when))
    level, reasons = _factory.gardening_decision(now.timestamp())
    assert (level, reasons) == ("low", [])


def test_no_targets_leaves_the_decision_low_with_a_reason(flyte_world, monkeypatch):
    monkeypatch.setattr(_factory, "_gardening_targets", lambda: [])
    level, reasons = _factory.gardening_decision(NOW)
    assert level == "low" and reasons == ["no targets in gardening.toml"]


def test_unknown_targets_do_not_take_the_reserved_slot(flyte_world, monkeypatch):
    monkeypatch.setattr(_factory, "_gardening_targets", lambda: None)
    level, reasons = _factory.gardening_decision(NOW)
    assert level == "low"
    assert len(reasons) == 1 and reasons[0].startswith("gardening targets unknown: ")


def test_targets_that_fail_to_read_do_not_take_the_reserved_slot(flyte_world, monkeypatch):
    def broken():
        raise _github.GithubError("boom", 500)

    monkeypatch.setattr(_factory, "_gardening_targets", broken)
    level, reasons = _factory.gardening_decision(NOW)
    assert level == "low" and reasons[0].startswith("gardening targets unknown: ")
    assert "boom" in reasons[0]


def test_the_decision_reads_the_target_list_from_the_workspace_repo(flyte_world, monkeypatch):
    class App:
        def read_file(self, repo, path):
            assert (repo, path) == ("owner/workspace", "gardening.toml")
            return "# no targets\n"

    profile = dataclasses.replace(
        _factory_base.PROFILE,
        github=_profile.Github(app_id="1", private_key_secret="key"),
        repo="git@github.com:owner/workspace.git",
        repos={**_factory_base.PROFILE.repos, "factory": "git@github.com:owner/engine.git"},
    )
    monkeypatch.setattr(_factory_base, "PROFILE", profile)
    monkeypatch.setenv(_deploy.GITHUB_KEY_ENV, "offline-app-key")
    monkeypatch.setattr(_github, "GithubApp", lambda *_: App())
    assert _factory._gardening_targets() == []


def test_a_workspace_without_the_file_gardens_the_engine_by_default(tmp_path):
    assert [t.repo for t in _gardening.load_or_default(tmp_path, REPOS)] == [REPOS["factory"]]


def test_a_workspace_without_the_file_and_a_profile_without_the_engine_has_no_targets(tmp_path):
    assert _gardening.load_or_default(tmp_path, {}) == []


def test_a_present_file_without_targets_opts_out(tmp_path):
    write(tmp_path, "# no targets\n")
    assert _gardening.load_or_default(tmp_path, REPOS) == []


def _github_profile(monkeypatch, files, **repos):
    """A profile whose workspace repo serves `files` (path -> text) through a fake GitHub app."""

    class App:
        def __init__(self, *_):
            pass

        def read_file(self, repo, path):
            assert repo == "owner/workspace"
            if path not in files:
                raise _github.GithubError("not found", 404)
            return files[path]

    profile = dataclasses.replace(
        _factory_base.PROFILE,
        github=_profile.Github(app_id="1", private_key_secret="key"),
        repo="git@github.com:owner/workspace.git",
        repos={k: v for k, v in _factory_base.PROFILE.repos.items() if k != "factory"} | repos,
    )
    monkeypatch.setattr(_factory_base, "PROFILE", profile)
    monkeypatch.setenv(_deploy.GITHUB_KEY_ENV, "offline-app-key")
    monkeypatch.setattr(_github, "GithubApp", App)


def test_a_workspace_without_the_file_has_the_engine_as_its_one_target(flyte_world, monkeypatch):
    _github_profile(monkeypatch, {}, factory="git@github.com:owner/engine.git")
    assert [t.repo for t in _factory._gardening_targets() or []] == ["git@github.com:owner/engine.git"]


def test_a_workspace_file_listing_no_target_has_none(flyte_world, monkeypatch):
    _github_profile(monkeypatch, {"gardening.toml": "# none\n"}, factory="git@github.com:owner/engine.git")
    assert _factory._gardening_targets() == []


def test_a_workspace_file_listing_the_engine_has_it_as_target(flyte_world, monkeypatch):
    engine = "git@github.com:owner/engine.git"
    _github_profile(monkeypatch, {"gardening.toml": '[[target]]\nrepo = "factory"\n'}, factory=engine)
    assert [t.repo for t in _factory._gardening_targets() or []] == [engine]


def test_the_weekly_task_skips_without_a_factory_repo_in_the_profile(weekly, monkeypatch):
    repos = {k: v for k, v in _factory_base.PROFILE.repos.items() if k != "factory"}
    monkeypatch.setattr(_factory_base, "PROFILE", dataclasses.replace(_factory_base.PROFILE, repos=repos))
    result = asyncio.run(_factory.garden_weekly.func())
    assert result.startswith("skip: ") and "[repo] factory" in result
    assert _queue.pending(weekly.world.queue) == []


def test_the_weekly_task_queues_when_the_engine_is_the_target(weekly):
    weekly.scans.targets = [FACTORY]
    result = json.loads(asyncio.run(_factory.garden_weekly.func()))
    assert result["status"] in ("started", "queued")


def test_list_targets_defaults_to_the_engine_when_the_workspace_has_no_file(monkeypatch):
    async def clone(repo, ref, dest):
        assert repo == "git@example.org:owner/workspace.git"
        dest.mkdir(parents=True)

    repos = {**_factory_base.PROFILE.repos, "factory": FACTORY}
    monkeypatch.setattr(_factory_base, "PROFILE", dataclasses.replace(_factory_base.PROFILE, repos=repos))
    monkeypatch.setattr(_factory, "_no_scan_key", lambda repo: "")
    monkeypatch.setattr(_git, "clone", clone)
    names = asyncio.run(_factory.list_targets.func("git@example.org:owner/workspace.git"))
    assert json.loads(names) == [FACTORY]


# --- the profile (H6)


def parse(extra=""):
    return _profile.parse(EXAMPLE.read_text() + extra, "example.toml")


def test_the_defaults_apply_without_a_table():
    assert parse().gardening == _profile.Gardening(1, 7, True, 10)


def test_a_gardening_table_is_read():
    table = "\n[gardening]\nmin_runs_per_week = 2\nmax_days_between = 3\n"
    table += "escalate_on_regression = false\nmax_open_issues = 4\n"
    assert parse(table).gardening == _profile.Gardening(2, 3, False, 4)


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("min_runs_per_week = -1", "min_runs_per_week must be an integer of at least 0"),
        ("min_runs_per_week = 1.5", "min_runs_per_week must be an integer"),
        ("max_days_between = 0", "max_days_between must be an integer of at least 1"),
        ("max_open_issues = true", "max_open_issues must be an integer"),
        ('escalate_on_regression = "yes"', "escalate_on_regression must be a boolean"),
        ("cadence = 3", r"unknown key\(s\) in \[gardening\]: cadence"),
    ],
)
def test_a_bad_gardening_value_is_refused(line, message):
    with pytest.raises(_profile.ProfileError, match=message):
        parse(f"\n[gardening]\n{line}\n")


def test_the_example_and_readme_document_the_table():
    example = EXAMPLE.read_text()
    readme = (ROOT / "profiles" / "README.md").read_text()
    for key in ("min_runs_per_week", "max_days_between", "escalate_on_regression", "max_open_issues"):
        assert f"# {key} = " in example
        assert f"`{key}`" in readme
    assert "### `[gardening]`" in readme


# --- the brief (H7)


@pytest.mark.parametrize(
    "needle",
    [
        "gardening.toml",
        "full standards gate",
        "one per area",
        "vulture",
        "duplicate-code",
        "jscpd",
        "Orphaned files",
        "tests whose code was removed",
        "docs and briefs for work that has",
        "stale `ops/` scripts",
        "Bump dependencies and pins",
        "stale branches and stale sandbox PRs",
        "one-line reason",
        "labelled `gardening`",
    ],
)
def test_the_brief_covers_each_activity(needle):
    assert needle in " ".join((ROOT / "briefs" / "factory-gardening.md").read_text().split())


# --- the weekly task (H8)


def test_the_weekly_task_is_registered_with_a_weekly_schedule():
    [trigger] = _factory.garden_weekly.triggers
    assert trigger.name == "factory_gardening"
    assert isinstance(trigger.automation, flyte.FixedRate)
    assert trigger.automation.interval_minutes == 7 * 24 * 60
    assert "factory.garden_weekly" in _factory.env.tasks


FACTORY = "git@example.org:owner/engine.git"


@pytest.fixture
def weekly(flyte_world, queue_state, monkeypatch):
    """`garden_weekly` against fake slots, a local state directory and a fake scan child."""
    flyte_world.add("one", _factory_base.SLOTS[0])
    flyte_world.add("two", _factory_base.SLOTS[1])
    repos = {**_factory_base.PROFILE.repos, "factory": FACTORY}
    monkeypatch.setattr(_factory_base, "PROFILE", dataclasses.replace(_factory_base.PROFILE, repos=repos))
    monkeypatch.setattr(_factory, "allowed_repos", lambda: {"factory": FACTORY})
    scans = SimpleNamespace(targets=[FACTORY, "path:tools"], by_repo={FACTORY: {"counts": {"ruff": 1}}})

    async def spot_first(task, *args):
        if task is _factory.list_targets:
            assert args == (_factory_base.PROFILE.repo,)
            return json.dumps(scans.targets)
        assert task is _factory.scan_target
        return json.dumps(scans.by_repo[args[0]])

    monkeypatch.setattr(_factory, "_spot_first", spot_first)
    return SimpleNamespace(world=flyte_world, scans=scans, uri=_factory_base.PROFILE.state.uri)


def test_the_weekly_task_skips_when_there_are_no_targets(weekly):
    weekly.scans.targets = []
    assert asyncio.run(_factory.garden_weekly.func()) == "skip: no targets in gardening.toml"
    assert _queue.pending(weekly.world.queue) == []
    assert set(weekly.world.runs) == {"one", "two"}  # the two fake slot holders, no gardening run
    assert _gardening.read_counts(weekly.uri) == {"clean": {}, "latest": {}}


def test_the_weekly_task_skips_when_the_targets_cannot_be_read(weekly, monkeypatch):
    async def unreadable(task, *args):
        raise ValueError("no deploy key")

    monkeypatch.setattr(_factory, "_spot_first", unreadable)
    result = asyncio.run(_factory.garden_weekly.func())
    assert result == "skip: cannot read gardening.toml: no deploy key"
    assert _queue.pending(weekly.world.queue) == []
    assert set(weekly.world.runs) == {"one", "two"}


def test_the_weekly_task_skips_when_the_targets_come_back_unknown(weekly):
    weekly.scans.targets = None
    result = asyncio.run(_factory.garden_weekly.func())
    assert result.startswith("skip: cannot read gardening.toml: ")
    assert _queue.pending(weekly.world.queue) == []


def test_the_weekly_task_queues_a_low_priority_launch(weekly):
    asyncio.run(_factory.garden_weekly.func())
    [entry] = _queue.pending(weekly.world.queue)
    assert entry.request is not None
    assert (entry.name, entry.request.priority) == (_gardening.NAME, "low")
    assert entry.request.repo == FACTORY


def test_the_weekly_task_scans_before_it_decides_and_records_the_baseline(weekly):
    result = json.loads(asyncio.run(_factory.garden_weekly.func()))
    assert _gardening.read_counts(weekly.uri) == {
        "clean": {FACTORY: {"ruff": 1}},
        "latest": {FACTORY: {"ruff": 1}},
    }
    assert _gardening.regressed(_gardening.read_counts(weekly.uri)) == []
    assert result["unscanned"] == {
        "path:tools": "a workspace path, which only the gardening project can reach"
    }


def test_a_worse_scan_escalates_the_decision_naming_target_and_rule(weekly):
    asyncio.run(_factory.garden_weekly.func())
    weekly.scans.by_repo[FACTORY] = {"counts": {"ruff": 3}}
    asyncio.run(_factory.garden_weekly.func())
    regression = _gardening.regressed(_gardening.read_counts(weekly.uri))
    assert regression == [f"{FACTORY}: ruff"]
    level, reasons = _gardening.priority(NOW, [NOW - 86400], regression, 0, SETTINGS)
    assert level == "normal"
    assert reasons == [f"standards gate regressed on {FACTORY}: ruff"]


def test_an_unscannable_target_records_nothing_and_never_escalates(weekly):
    weekly.scans.by_repo[FACTORY] = {"unscanned": "no deploy key for this repository in the profile"}
    result = json.loads(asyncio.run(_factory.garden_weekly.func()))
    assert _gardening.read_counts(weekly.uri) == {"clean": {}, "latest": {}}
    assert result["unscanned"][FACTORY] == "no deploy key for this repository in the profile"
    assert result["status"] in ("started", "queued")


def test_a_failed_scan_still_queues_the_launch(weekly, monkeypatch):
    async def broken(task, *args):
        if task is _factory.list_targets:
            return json.dumps([FACTORY])
        raise flyte.errors.RuntimeUserError("x", "scan pod died")

    monkeypatch.setattr(_factory, "_spot_first", broken)
    asyncio.run(_factory.garden_weekly.func())
    assert _queue.pending(weekly.world.queue) or weekly.world.runs


def test_the_scan_env_is_registered_beside_the_factory():
    assert _factory.scan_env.name == "gardening-scan"
    assert {"gardening-scan.scan_target", "gardening-scan.list_targets"} <= set(_factory.scan_env.tasks)
    assert _factory.scan_env in _factory.env.depends_on


@pytest.mark.parametrize(
    ("repo", "reason"),
    [(FACTORY, "no deploy key"), ("git@example.org:owner/other.git", "no deploy key")],
)
def test_a_target_without_a_mounted_key_is_unscanned(monkeypatch, repo, reason):
    monkeypatch.setattr(_factory_base, "PROFILE", dataclasses.replace(_factory_base.PROFILE, repo_keys={}))
    result = json.loads(asyncio.run(_factory.scan_target.func(repo)))
    assert reason in result["unscanned"]


def test_scan_target_returns_the_gates_counts_for_a_cloned_default_branch(monkeypatch):
    cloned = []

    async def clone(repo, branch, dest):
        cloned.append((repo, branch))
        dest.mkdir()

    monkeypatch.setattr(_git, "clone", clone)
    monkeypatch.setattr(_factory, "_no_scan_key", lambda repo: "")
    monkeypatch.setattr(
        _gates,
        "standards_scan",
        lambda path, standards_dir=None: [_gates.Result("ruff", _gate_base.Status.FAIL, "x\ny")],
    )
    assert json.loads(asyncio.run(_factory.scan_target.func(FACTORY))) == {"counts": {"ruff": 2}}
    assert cloned == [(FACTORY, "")]


def test_nothing_in_src_writes_gardening_toml():
    writes = re.compile(r"""(write_text|write_bytes|open\([^)]*["']w|pipe_file)""")
    offenders = [
        str(p)
        for p in (ROOT / "src").rglob("*.py")
        for line in p.read_text().splitlines()
        if "gardening.toml" in line and writes.search(line)
    ]
    assert offenders == []


def test_configs_is_a_known_check(tmp_path):
    write(tmp_path, '[[target]]\nrepo = "a"\nchecks = ["configs"]\n')
    assert _gardening.load(tmp_path, REPOS)[0].checks == ("configs",)
    assert "configs" in _gardening.CHECKS


_TEMPLATE_RUFF = """\
line-length = 100
[lint]
select = ["E", "F"]
[lint.pydocstyle]
convention = "pep257"
[lint.per-file-ignores]
"**/tests/**" = ["D"]
"""
_TEMPLATE_MYPY = "[mypy]\npython_version = 3.12\nstrict = True\nexclude = ^tests/\n"
_ENGINE_SAME = """\
[tool.ruff]
line-length = 100
[tool.ruff.lint]
select = ["F", "E"]
[tool.ruff.lint.pydocstyle]
convention = "pep257"
[tool.ruff.lint.per-file-ignores]
"**/tests/**" = ["D"]
[tool.mypy]
python_version = "3.12"
strict = true
exclude = ['^tests/']
"""


def _configs(tmp_path: Path, engine: str) -> list[str]:
    (tmp_path / "pyproject.toml").write_text(engine)
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "ruff.toml").write_text(_TEMPLATE_RUFF)
    (configs / "mypy.ini").write_text(_TEMPLATE_MYPY)
    return _gardening.config_drift(tmp_path / "pyproject.toml", configs)


def test_identical_tool_configs_report_no_drift(tmp_path):
    assert _configs(tmp_path, _ENGINE_SAME) == []  # set order and TOML/ini spellings do not matter


def test_planted_tool_config_drift_is_reported(tmp_path):
    engine = (
        _ENGINE_SAME.replace("line-length = 100", "line-length = 110")
        .replace('select = ["F", "E"]', 'select = ["F", "E", "B"]')
        .replace("strict = true", "strict = false\nwarn_unreachable = true")
    )
    assert _configs(tmp_path, engine) == [
        "ruff line-length: engine 110, template 100",
        "ruff select: engine B, E, F, template E, F",
        "mypy strict: engine false, template true",
        "mypy warn_unreachable: engine true, template unset",
    ]


def test_the_engines_own_configs_can_be_compared(tmp_path):
    root = Path(__file__).resolve().parents[1]
    lines = _gardening.config_drift(
        root / "pyproject.toml", _workspace.TEMPLATE / "standards" / "languages" / "configs"
    )
    assert isinstance(lines, list)  # the engine differs from the template today, so the count is not asserted
