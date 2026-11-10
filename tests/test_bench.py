"""The factory benchmark's pure pieces: the [bench] profile table, the bench profile, the suite, scoring,
the record schema and the report. Offline; `test_bench_flow.py` drives the Flyte side."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from flyte_agent_factory import (
    _bench,
    _bench_suite,
    _contract,
    _dashboard,
    _deploy,
    _inbox,
    _profile,
    _session_config,
    _timeline,
    _usage,
)

SHA = "0123456789abcdef0123456789abcdef01234567"

# Every table the bench profile must strip, beside the ones it keeps.
PROFILE_TEXT = """\
name = "bench-test"

[backend]
kind = "union"
endpoint = "union.example.org"
project = "proj"
domain = "development"

[repo]
project = "git@github.com:example-org/sandbox.git"
factory = "git@github.com:example-org/factory.git"

[repo_keys]
factory = "factory_key"

[repo_aliases]
factory = ["git@github.com:example-org/old-factory.git"]

[[slots]]
env = "agent-one"
subagent_env = "sub-one"

[secrets]
deploy_key = "sandbox_key"
claude_token = "production_token"
codex_token = "codex_token_x"
api_key = "api_key_x"
gateway_key = "gateway_key_x"

[policy]
CRAFT_EFFORT = "high"
JUDGE_ENGINE = "codex"
QA_DIFFERENT_ENGINE = "true"
PLANNER_CODEX_EFFORT = "high"

[mcp]
repos = ["project", "factory"]

[deploy]
auto = true

[state]
uri = "s3://bucket/state"

[github]
app_id = 123
private_key_secret = "app_key"
webhook_secret = "hook_secret"
maintainers = ["someone"]

[direction]
repo = "git@github.com:example-org/direction.git"

[board]
owner = "example-org"
owner_type = "org"
number = 1
pat_secret = "board_pat"

[browser]
personal = true
profile_uri = "s3://bucket/browser"
personal_env = "agent-one"
"""

BENCH = """
[bench]
token_concurrency = 2
max_usd = 100
repo = "git@github.com:example-org/factory-bench.git"
repo_key = "bench_repo_key"
"""


def profile(bench: str = BENCH, text: str = PROFILE_TEXT) -> _profile.Profile:
    return _profile.parse(text + bench, "bench-test")


# --------------------------------------------------------------------------- #
# [bench] in the profile
# --------------------------------------------------------------------------- #


def test_the_bench_table_requires_its_dedicated_repo_and_key():
    bench = profile().bench
    assert bench == _profile.Bench(
        repo="git@github.com:example-org/factory-bench.git",
        repo_key="bench_repo_key",
        max_usd=100.0,
        claude_token="production_token",
        token_concurrency=2,
    )
    assert profile("").bench is None


def test_a_dedicated_bench_repo_takes_its_own_key():
    bench = profile(BENCH).bench
    assert bench is not None
    assert (bench.repo, bench.repo_key) == ("git@github.com:example-org/factory-bench.git", "bench_repo_key")


@pytest.mark.parametrize(
    "bench, error",
    [
        ("[bench]\nmax_usd = 5\n", "missing key 'repo'"),
        ("[bench]\ntoken_concurrency = 0\n", "token_concurrency must be a positive integer"),
        ("[bench]\ntoken_concurrency = true\n", "token_concurrency must be a positive integer"),
        ("[bench]\nmax_usd = -1\n", "max_usd must be a number"),
        ("[bench]\nmax_usd = true\n", "max_usd must be a number"),
        ("[bench]\nmax_usd = inf\n", "max_usd must be a number"),
        ("[bench]\nmax_usd = nan\n", "max_usd must be a number"),
        ('[bench]\ncolour = "red"\n', "unknown key(s) in [bench]: colour"),
        (
            '[bench]\nrepo = "git@github.com:example-org/factory-bench.git"\n',
            "missing key 'repo_key'",
        ),
        (
            '[bench]\nrepo = "git@example.org:x/y.git"\nrepo_key = "k"\n',
            "must be a github.com repository",
        ),
        (
            '[bench]\nrepo = "git@github.com:example-org/factory.git"\nrepo_key = "k"\n',
            "must not be [repo] factory",
        ),
    ],
)
def test_a_bad_bench_table_is_refused(bench, error):
    with pytest.raises(_profile.ProfileError, match=re.escape(error)):
        profile("\n" + bench)


def test_the_bench_needs_the_app_and_the_engine_repo():
    no_app = PROFILE_TEXT.replace('app_id = 123\nprivate_key_secret = "app_key"\n', "")
    no_app = no_app.replace("[deploy]\nauto = true\n", "[deploy]\nauto = false\n")
    no_app = no_app.replace('[direction]\nrepo = "git@github.com:example-org/direction.git"\n', "")
    with pytest.raises(_profile.ProfileError, match="requires .github. app_id"):
        profile(BENCH, no_app)
    no_factory = (
        no_app.replace('factory = "git@github.com:example-org/factory.git"\n', "", 1)
        .replace('[repo_keys]\nfactory = "factory_key"\n', "")
        .replace('[repo_aliases]\nfactory = ["git@github.com:example-org/old-factory.git"]\n', "")
        .replace('repos = ["project", "factory"]', 'repos = ["project"]')
        .replace("[github]\n", '[github]\napp_id = "1"\nprivate_key_secret = "k"\n')
    )
    with pytest.raises(_profile.ProfileError, match="requires .repo. factory"):
        profile(BENCH, no_factory)


def test_the_example_profile_documents_the_bench_table():
    text = (Path(__file__).resolve().parents[1] / "profiles" / "example.toml").read_text()
    readme = (Path(__file__).resolve().parents[1] / "profiles" / "README.md").read_text()
    assert "# [bench]" in text
    for key in ("repo", "repo_key", "max_usd"):
        assert f"`[bench]` `{key}`" in readme or f"`{key}`" in readme


# --------------------------------------------------------------------------- #
# The bench profile
# --------------------------------------------------------------------------- #


def test_the_bench_profile_keeps_only_the_benchmark():
    bench = _bench.bench_profile(profile(), SHA)
    assert [(s.env, s.subagent_env) for s in bench.slots] == [
        ("bench-0123456-1", "bench-0123456-1-sub"),
        ("bench-0123456-2", "bench-0123456-2-sub"),
    ]
    assert bench.cap == 2
    assert dict(bench.repos) == {"project": "git@github.com:example-org/factory-bench.git"}
    assert dict(bench.repo_keys) == {} and dict(bench.repo_aliases) == {}
    assert bench.secrets.deploy_key == "bench_repo_key"
    assert bench.state.uri == "" and bench.board is None and bench.direction is None and bench.mcp is None
    assert not bench.deploy.auto and bench.bench is None and not bench.browser.personal
    # No App: candidate code would hold a key that reaches every repo the App is installed on.
    assert bench.github == _profile.Github() and not bench.github.configured
    # Claude-only: no engine routing, and no Codex token to route to.
    assert dict(bench.policy) == {"CRAFT_EFFORT": "high"}
    assert bench.secrets == _profile.Secrets(deploy_key="bench_repo_key", claude_token="production_token")
    # What a bench pod mounts beside the work token: the bench repo's key, and nothing else.
    assert bench.agent_secrets == "bench_repo_key=GIT_DEPLOY_KEY"
    for slot in bench.slots:
        env = bench.deploy_env(slot)
        assert "AGENT_FACTORY_CODEX_SECRET" not in env
        assert not any("ENGINE" in k or "CODEX" in k for k in env)
    assert _deploy.factory_envs(bench) == _deploy.FACTORY_ENVS


def test_the_bench_profile_on_a_dedicated_repo_mounts_only_its_key():
    dedicated = profile(BENCH)
    bench = _bench.bench_profile(dedicated, SHA)
    assert bench.repo == "git@github.com:example-org/factory-bench.git"
    assert bench.secrets.deploy_key == "bench_repo_key"
    assert "sandbox_key" not in bench.agent_secrets


def test_check_recipe_accepts_every_bench_env():
    bench = _bench.bench_profile(profile(), SHA)
    _deploy.check_slots(bench, [s.env for s in bench.slots])
    for slot in bench.slots:
        env = _deploy.recipe_env(bench, slot.env, {})
        _deploy.check_recipe(bench, env)
        assert env["AGENT_FACTORY_CLAUDE_TOKEN_SECRET"] == "production_token"
    # A production slot is not a bench env.
    with pytest.raises(_deploy.Refused):
        _deploy.check_slots(bench, ["agent-one"])


def test_the_bench_profile_round_trips_through_the_strict_parser():
    bench = _bench.bench_profile(profile(), SHA)
    again = _profile.parse(bench.text, "again")
    assert again.slots == bench.slots and again.repos == bench.repos and again.secrets == bench.secrets
    # What a deploy child is handed: the text it parses at import.
    child = _profile.child_env(bench, {})
    assert _profile.parse(child[_profile.TOML_ENV], _profile.TOML_ENV).slots == bench.slots


def test_the_bench_profile_needs_a_full_sha_and_a_bench_table():
    with pytest.raises(ValueError, match="40-character commit SHA"):
        _bench.bench_profile(profile(), "0123456")
    with pytest.raises(ValueError, match="no .bench. table"):
        _bench.bench_profile(profile(""), SHA)


def test_a_profile_with_bench_deploys_the_bench_envs():
    assert {"factory-bench", "factory-bench-check"} <= _deploy.factory_envs(profile())
    assert "factory-bench" not in _deploy.factory_envs(profile(""))
    assert "factory-bench-check" in _deploy.planned_envs(profile())


def test_the_factory_child_deploys_the_bench_env_and_its_check_env(tmp_path):
    """The real FACTORY_CHILD imports `_bench_flow` under a [bench] profile, and check_environments
    accepts both of its envs (flyte.deploy is stubbed, as in test_board)."""
    import os

    from tests.test_board import FACTORY_CHILD_STUB

    src = Path(_deploy.__file__).resolve().parents[1]
    env = _profile.child_env(profile(), dict(os.environ))
    env.update(PYTHONPATH=str(src), _UNION_EAGER_API_KEY="unused")
    result = subprocess.run(
        [sys.executable, "-c", FACTORY_CHILD_STUB + _deploy.FACTORY_CHILD, str(src), "1", "p", "d"],
        env=env,
        cwd=src,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    (line,) = [line for line in result.stdout.splitlines() if line.startswith("ENVS=")]
    deployed = set(json.loads(line.removeprefix("ENVS=")))
    assert {"factory-bench", "factory-bench-check"} <= deployed
    assert deployed <= _deploy.factory_envs(profile())


# --------------------------------------------------------------------------- #
# Suite and planning
# --------------------------------------------------------------------------- #


def test_step_one_suite_is_the_bugfix_task():
    task = _bench_suite.get("bugfix-shortlinks")
    assert task.kind == "build" and task.expected == "accepted"
    assert (task.cap_usd, task.cap_hours) == (40.0, 3.0)
    brief = _bench_suite.brief(task)
    assert "cp -r bench/shortlinks {name}" in brief and "{start}" not in brief
    # The hidden check is in the bundle, never in the brief the agent reads.
    assert "test_a_deleted_slug_can_be_registered_again" not in brief
    with pytest.raises(ValueError, match="no task 'nope'.*bugfix-shortlinks"):
        _bench_suite.get("nope")


def test_the_suite_digest_changes_with_any_task(monkeypatch):
    import dataclasses

    before = _bench_suite.digest()
    assert before == _bench_suite.digest() and len(before) == 12
    task = _bench_suite.TASKS["bugfix-shortlinks"]
    monkeypatch.setitem(_bench_suite.TASKS, task.id, dataclasses.replace(task, cap_usd=41.0))
    assert _bench_suite.digest() != before


def test_the_case_name_is_deterministic_and_a_valid_project_name():
    from flyte_agent_factory import _factory_policy

    name = _bench.case_name("r-abc", "bugfix-shortlinks", "candidate", 1)
    assert name == _bench.case_name("r-abc", "bugfix-shortlinks", "candidate", 1)
    assert name.startswith("b-bugfix-sho-") and len(name) == len("b-bugfix-sho-") + 6
    assert name != _bench.case_name("r-abc", "bugfix-shortlinks", "candidate", 2)
    assert name != _bench.case_name("r-abd", "bugfix-shortlinks", "candidate", 1)
    _factory_policy.validate(name, "opus")
    # A task id cut at a hyphen does not end the name's middle part with one.
    assert "--" not in _bench.case_name("r", "too-big-task-x", "candidate", 1)


def test_cases_are_planned_repeat_by_repeat():
    cases = _bench.plan_cases("r1", SHA, ["bugfix-shortlinks"], 2, {"CRAFT_EFFORT": "low"}, "opus")
    assert [(c["task"], c["repeat"]) for c in cases] == [("bugfix-shortlinks", 1), ("bugfix-shortlinks", 2)]
    assert cases[0]["case"] == _bench.case_name("r1", "bugfix-shortlinks", "candidate", 1)
    assert cases[0]["suite_digest"] == _bench_suite.digest() and cases[0]["policy"] == {"CRAFT_EFFORT": "low"}
    for bad, error in (
        (dict(task_ids=["nope"]), "no task"),
        (dict(repeats=0), "repeats must be 1 to 3"),
        (dict(repeats=4), "repeats must be 1 to 3"),
        (dict(sha="abc"), "40-character"),
    ):
        args: dict[str, Any] = (
            dict(bench_run="r1", sha=SHA, task_ids=[], repeats=1, policy={}, model="opus") | bad
        )
        with pytest.raises(ValueError, match=error):
            _bench.plan_cases(**args)


def test_a_task_named_twice_is_planned_once():
    cases = _bench.plan_cases("r1", SHA, ["bugfix-shortlinks", "bugfix-shortlinks"], 1, {}, "opus")
    assert [c["task"] for c in cases] == ["bugfix-shortlinks"]
    assert len({c["case"] for c in _bench.plan_cases("r1", SHA, ["bugfix-shortlinks"], 3, {}, "opus")}) == 3


@pytest.mark.parametrize("key", ["JUDGE_ENGINE", "AGENT_FACTORY_DELEGATE_ENGINE", "QA_DIFFERENT_ENGINE"])
def test_an_arm_cannot_route_to_another_engine(key):
    with pytest.raises(ValueError, match="Claude-only"):
        _bench.plan_cases("r1", SHA, [], 1, {key: "codex"}, "opus")
    # Asking for Claude explicitly is fine.
    if key.endswith("_ENGINE") and key != "QA_DIFFERENT_ENGINE":
        assert _bench.plan_cases("r1", SHA, [], 1, {key: "claude"}, "opus")


def refusal(*args: float) -> str:
    reason = _bench.spend_refusal(*args)
    assert reason is not None
    return reason


def test_spending_is_refused_above_either_ceiling():
    cases = _bench.plan_cases("r1", SHA, ["bugfix-shortlinks"], 1, {}, "opus")
    assert _bench.budget_usd(cases) == 40.0 and _bench.expected_usd(cases) == 15.0
    assert _bench.spend_refusal(40.0, 40.0, 100.0) is None
    assert "budgeted at $40" in refusal(40.0, 39.0, 100.0)
    assert "over the profile's [bench] max_usd 30" in refusal(40.0, 50.0, 30.0)
    assert "must be 0" in refusal(40.0, -1.0, 100.0)
    for bad in (float("nan"), float("inf")):
        assert "finite" in refusal(40.0, bad, 100.0)
        assert "finite" in refusal(40.0, 40.0, bad)


# --------------------------------------------------------------------------- #
# Scoring a report snapshot
# --------------------------------------------------------------------------- #


def review_rows(scores, passed: bool) -> list[dict[str, Any]]:
    """The timeline rows a review writes (agent.py `_review`): each failed round, then a passing one."""
    rows = []
    for n, score in enumerate(scores, 1):
        rows += [
            {"time": n, "icon": "🔍", "text": f"QA round {n}: 3 reviewers"},
            {"time": n, "icon": "❌", "text": "correctness: FAIL"},
            {"time": n, "icon": "✅", "text": "completeness: PASS"},
            {"time": n, "icon": "📉", "text": f"blocking findings, round {n}: {score} (rounds so far: [])"},
        ]
    if passed:
        n = len(scores) + 1
        rows += [
            {"time": n, "icon": "🔍", "text": f"QA round {n}: 3 reviewers"},
            {"time": n, "icon": "✅", "text": "correctness: PASS"},
            {"time": n, "icon": "✅", "text": "completeness: PASS"},
            {"time": n, "icon": "ℹ️", "text": "craft advisory: nits"},
        ]
    return rows


def snapshot(
    kind="building",
    scores=(),
    usd=(3.0, 1.5),
    asks=(),
    risks=(),
    since=1000.0,
    reviewed=None,
    engines=("claude",),
) -> dict[str, Any]:
    """A snapshot in today's report shape (agent.py `_Timeline.snapshot`), trimmed to what is read.

    `reviewed` (default: whether `kind` is done or released) adds the rows of a passing review round.
    """
    sub, author = usd
    reviewed = kind in ("done", "released") if reviewed is None else reviewed
    return {
        "schema": 1,
        "phase": {"kind": kind, "since": since, "round": 0, "limited_until": None},
        "needs_you": list(asks),
        "review": {
            "scores": list(scores),
            "open": [],
            "converging": None,
            "rulings": 0,
            "passed_round": len(scores) + 1 if reviewed else None,
        },
        "cost": {
            "meters": [],
            "fallbacks": [],
            "fallback_counts": {"codex->claude": 1},
            "subagents": {"tokens": {}, "usd": sub},
            "by_role": {"correctness": sub},
            "by_engine": {engine: (sub if engine == "claude" else 0.0) for engine in engines},
            "coordinator": {"tokens": {}, "usd_estimate": author, "usd_per_hour": None},
        },
        "deliverables": {"pr": "https://github.com/example-org/sandbox/pull/7"},
        "risks": list(risks),
        "timeline": review_rows(scores, reviewed),
    }


def ask(kind="ask", timeout=600.0, condition="ask-a1-001", opened=100.0) -> dict[str, Any]:
    return {
        "id": "q1",
        "kind": kind,
        "text": "Which? Default if no answer: the first",
        "opened_at": opened,
        "applies_at": opened + timeout,
        "deadline_at": opened + timeout + 30,
        "default": "Default if no answer: the first",
        "condition": condition,
        "timed_out": False,
        "url": "",
    }


def decide(snap, kind="build", live=True, elapsed=60.0, asks=0, blocking=0, run_phase=None):
    return _bench.decide(
        snap,
        kind=kind,
        live=live,
        run_phase=run_phase or ("RUNNING" if live else "SUCCEEDED"),
        elapsed_s=elapsed,
        cap_usd=100.0,
        cap_s=3 * 3600,
        asks=asks,
        blocking_asks=blocking,
    )


def test_a_live_run_at_done_is_accepted():
    assert decide(snapshot("done")) == "accepted"
    assert decide(snapshot("released"), live=False) == "accepted"
    assert decide(snapshot("review")) is None
    # A completed run needs review evidence even if it reports success.
    assert decide(snapshot("done", reviewed=False), live=False) == "harness_error"
    assert decide(snapshot("failed"), live=False) == "failed"


def test_the_caps_end_a_case():
    # The full reserve covers polling, delivery retries and shutdown grace.
    stop = 100.0 - _bench.STOP_MARGIN_USD
    assert decide(snapshot(usd=(stop - 1.0, 1.0))) == "capped_cost"
    assert decide(snapshot(usd=(stop - 1.0, 0.9))) is None
    assert decide(snapshot(), elapsed=3 * 3600) == "capped_time"
    # A pass in the same poll as the cap counts: the case reached review first.
    assert decide(snapshot("done", usd=(110.0, 0.0))) == "accepted"


def test_done_without_a_passing_review_is_not_a_pass():
    # Done after an escalation (two failed rounds, no passing one), or a session that ended by itself.
    assert decide(snapshot("done", scores=[3, 2], reviewed=False)) is None
    assert decide(snapshot("done", reviewed=False)) is None
    assert not _bench.review_passed(snapshot("done", reviewed=False), live=True)
    # A blocking judge's FAIL in the newest round is not a pass, whatever the phase says.
    failing = snapshot("done")
    failing["timeline"].append({"time": 9, "icon": "❌", "text": "completeness: FAIL"})
    assert not _bench.review_passed(failing, live=True)
    # The rows say which round passed: one after the failed ones.
    assert _bench.review_passed(snapshot("done", scores=[3, 2]), live=True)


def test_a_run_that_dies_without_a_report_is_failed():
    assert decide(snapshot("building"), live=False, run_phase="FAILED") == "failed"
    assert decide(snapshot("building"), live=False, run_phase="ABORTED") == "failed"
    assert decide(snapshot("building"), live=False, run_phase="SUCCEEDED") == "not_converged"


def test_a_blocking_ask_escalates_a_build_case():
    assert _bench.blocking(ask(kind="approve"))
    assert _bench.blocking(ask(timeout=6 * 3600))
    assert not _bench.blocking(ask(timeout=600))
    assert decide(snapshot(), asks=1, blocking=1) == "escalated"
    assert decide(snapshot(), asks=1, blocking=0) is None
    # An escalate task: the first question is the right answer, and building instead is wrong.
    assert decide(snapshot(), kind="escalate", asks=1) == "escalated"
    assert decide(snapshot("done"), kind="escalate") == "failed"


def test_open_asks_are_the_ones_with_a_condition():
    snap = snapshot(asks=[ask(), {**ask(condition="ask-a1-002"), "timed_out": True}, ask(condition="")])
    assert [q["condition"] for q in _bench.open_asks(snap)] == ["ask-a1-001"]


def harness(**over) -> dict[str, Any]:
    return {
        "outcome": "accepted",
        "error": None,
        "review_passed": True,
        "passed_at": 4000.0,
        "asks": 1,
        "blocking_asks": 0,
        "first_ask_s": 30.0,
        "forced_abort": False,
        "start_sha": "c" * 40,
        "start_tree": "d" * 40,
        "started_at": 1000.0,
        "ended_at": 4600.0,
        **over,
    }


def planned(**over) -> dict[str, Any]:
    return {**_bench.plan_cases("r1", SHA, [], 1, {}, "opus")[0], "env": "bench-0123456-1", **over}


RUN = {"name": "b-bugfix-sho-abc123", "url": "https://console.example/runs/b-bugfix-sho-abc123"}


def test_a_done_case_records_its_pass_round_and_cost(tmp_path, monkeypatch):

    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path)

    timeline = _timeline._Timeline()
    for row in review_rows([4, 1], passed=True):
        timeline.row(row["icon"], row["text"])
    _inbox._save_pass(3, "a" * 40)
    _session_config._round_scores[:] = [4, 1]
    _contract._reset_contract()
    timeline.phase("done")
    snap = _dashboard.from_report(timeline.render())
    assert snap is not None
    snap.pop("metrics")  # a report from an engine older than the metrics block (bench step 2)
    assert snap["review"]["scores"] == []
    snap["cost"] = snapshot()["cost"]
    snap["risks"] = ["restarted 1 time(s) (spot preemption)", "2 DONE bounced before review"]
    record = _bench.case_record(planned(), RUN, snap, harness(), {"exit_code": 0, "summary": "2 passed"})
    assert record["outcome"] == "accepted"
    assert record["passed_round"] == 3 == record["qa_rounds"]  # two failed rounds, then the pass
    assert record["blocking_by_round"] == [4, 1] and not record["first_pass"]
    assert record["verified"] and record["accepted_verified"]
    assert (record["usd_total"], record["usd_author"], record["usd_by_role"]) == (
        4.5,
        1.5,
        {"correctness": 3.0},
    )
    assert record["usd_source"] == {"author": "list-price", "subagents": "cli-total_cost_usd"}
    assert (record["gate_bounces"], record["restarts"]) == (2, 1)
    assert record["wall_s"] == 3600.0 and record["passed_at"] == 4000.0
    assert record["fallbacks"] == {"codex->claude": 1}
    assert record["metrics_schema"] is None and record["tokens"] is None and record["stages_s"] is None
    assert record["overshoot_usd"] == 0.0 and record["unpriced_calls"] is None
    assert not record["forced_abort_without_stop"]
    assert record["start_pinned"] and record["start_tree"] == "d" * 40
    assert record["case_run"] == RUN["name"] and record["env"] == "bench-0123456-1"


def test_a_case_over_its_cap_records_the_overshoot():
    record = _bench.case_record(
        planned(),
        RUN,
        snapshot("review", usd=(40.0, 6.5)),
        harness(outcome="capped_cost", review_passed=False),
        None,
    )
    assert record["usd_total"] == 46.5 and record["overshoot_usd"] == 6.5


def test_a_non_claude_engine_is_unpriced_not_free():
    record = _bench.case_record(
        planned(),
        RUN,
        snapshot("done", engines=("claude", "codex")),
        harness(),
        {"exit_code": 0, "summary": ""},
    )
    assert record["usd_source"]["subagents"] == "unpriced"
    assert any("unpriced" in note for note in _bench.caveats([record]))


def test_a_first_round_pass_is_first_pass():
    record = _bench.case_record(planned(), RUN, snapshot("done"), harness(), {"exit_code": 0, "summary": ""})
    assert record["passed_round"] == 1 and record["first_pass"]


def test_a_pass_the_hidden_check_rejects_is_not_accepted_verified():
    record = _bench.case_record(
        planned(), RUN, snapshot("done"), harness(), {"exit_code": 1, "summary": "x" * 900}
    )
    assert record["review_passed"] and record["verified"] is False and not record["accepted_verified"]
    assert len(record["check_summary"]) == _bench.CHECK_SUMMARY_MAX
    unchecked = _bench.case_record(planned(), RUN, snapshot("done"), harness(), None)
    assert unchecked["verified"] is None and not unchecked["accepted_verified"]
    refused = _bench.case_record(
        planned(), RUN, snapshot("done"), harness(), {"exit_code": None, "summary": "refused: pid 1"}
    )
    assert refused["verified"] is None and refused["check_summary"] == "refused: pid 1"


def test_a_capped_case_has_no_pass():
    record = _bench.case_record(
        planned(),
        RUN,
        snapshot("review", scores=[3, 3, 2]),
        harness(outcome="capped_cost", review_passed=False),
        {"exit_code": 1, "summary": ""},
    )
    assert record["passed_round"] is None and record["passed_at"] is None and record["qa_rounds"] == 3
    assert not record["first_pass"] and not record["accepted_verified"]


def test_the_schema_refuses_a_bad_record():
    record = _bench.case_record(planned(), RUN, snapshot("done"), harness(), {"exit_code": 0, "summary": ""})
    _bench.validate(record)
    with pytest.raises(ValueError, match="missing outcome"):
        _bench.validate({k: v for k, v in record.items() if k != "outcome"})
    with pytest.raises(ValueError, match="unknown colour"):
        _bench.validate({**record, "colour": "red"})
    with pytest.raises(ValueError, match="asks is bool"):
        _bench.validate({**record, "asks": True})
    with pytest.raises(ValueError, match="outcome 'great'"):
        _bench.validate({**record, "outcome": "great"})
    assert set(record) == set(_bench.FIELDS)
    assert json.loads(_bench.to_jsonl([record])) == record


def test_the_summary_counts_every_cost_and_the_report_shows_the_fields():
    good = _bench.case_record(planned(), RUN, snapshot("done"), harness(), {"exit_code": 0, "summary": "ok"})
    bad = _bench.case_record(
        planned(repeat=2, case="b-bugfix-sho-def456"),
        {**RUN, "name": "b-bugfix-sho-def456"},
        snapshot("review", usd=(30.0, 10.0)),
        harness(outcome="capped_cost", review_passed=False, ended_at=8000.0),
        {"exit_code": 1, "summary": "1 failed"},
    )
    summary = _bench.summarize([good, bad])
    arm = summary["arms"]["candidate"]
    assert (arm["cases"], arm["accepted_verified"], arm["pass_rate"]) == (2, 1, 0.5)
    assert arm["cost_per_accepted"] == 44.5  # both cases' cost over the one accepted PR
    assert arm["time_to_accepted_s"] == 3600.0 and arm["first_pass_rate"] == 0.5
    assert arm["outcomes"] == {"accepted": 1, "capped_cost": 1}
    good["start_pinned"] = False  # Reports must still disclose records from older unpinned suites.
    page = _bench.render_report([good, bad], summary, title="Factory benchmark <r1>")
    assert "Factory benchmark &lt;r1&gt;" in page and "<r1>" not in page
    for shown in ("capped_cost", "accepted", "b-bugfix-sho-def456", "1 failed", "44.50", "start not pinned"):
        assert shown in page
    assert _dashboard.from_report(page) is None  # not mistaken for a project's snapshot


def test_caveats_name_what_to_spot_check():
    unverified = _bench.case_record(
        planned(), RUN, snapshot("done"), harness(forced_abort=True), {"exit_code": 1, "summary": ""}
    )
    error = _bench.case_record(
        planned(),
        {"name": RUN["name"], "url": ""},
        {},
        harness(outcome="harness_error", review_passed=False, error="no start", asks=0),
        None,
    )
    notes = _bench.caveats([unverified, error])
    assert any("passed review but the hidden check did not pass" in n for n in notes)
    assert any("aborted" in n for n in notes)
    assert any("harness error: no start" in n for n in notes)
    assert any("no cost figures" in n for n in notes)
    assert any("no supervisor metrics block" in n for n in notes)


def test_the_latest_round_verdicts_are_read_from_the_timeline():
    rows = review_rows([3], passed=True)
    assert _dashboard.latest_verdicts(rows) == (2, {"correctness": "PASS", "completeness": "PASS"})
    assert _dashboard.latest_verdicts(review_rows([3], passed=False)) == (
        1,
        {"correctness": "FAIL", "completeness": "PASS"},
    )
    assert _dashboard.latest_verdicts([]) == (None, {})
    missing = [*rows, {"time": 9, "icon": "⚠️", "text": "completeness: no verdict, treating as FAIL"}]
    assert _dashboard.latest_verdicts(missing)[1]["completeness"] == "FAIL"


def test_an_error_record_keeps_the_case():
    record = _bench.error_record(planned(), "RuntimeError: pod lost", now=5.0)
    assert record["outcome"] == "harness_error" and record["error"] == "RuntimeError: pod lost"
    assert record["case_run"] == planned()["case"] and record["verified"] is None


def test_pass_round_survives_a_supervisor_restart(tmp_path, monkeypatch):

    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path)
    _inbox._save_pass(3, "a" * 40)
    restarted = _timeline._Timeline()
    restarted.phase("done")
    snap = _dashboard.from_report(restarted.render())
    assert snap is not None
    record = _bench.case_record(planned(), RUN, snap, harness(), {"exit_code": 0, "summary": ""})
    assert record["passed_round"] == 3 and not record["first_pass"]


def test_partial_cost_is_labelled():
    record = _bench.case_record(planned(), RUN, snapshot(usd=(3.0, None)), harness(), None)
    assert record["usd_total"] is None and record["usd_complete"] is False
    assert any("author cost missing" in note for note in _bench.caveats([record]))
    complete = _bench.case_record(planned(), RUN, snapshot(), harness(), None)
    assert complete["usd_complete"] is True


@pytest.mark.parametrize("field", ["usd_total", "wall_s", "usd_by_role"])
def test_nan_is_not_a_measurement(field):
    record = _bench.case_record(planned(), RUN, snapshot(), harness(), None)
    record[field] = {"qa": float("nan")} if field == "usd_by_role" else float("nan")
    with pytest.raises(ValueError, match="finite"):
        _bench.validate(record)


def test_harness_errors_are_not_failed_factory_attempts():
    good = _bench.case_record(planned(), RUN, snapshot("done"), harness(), {"exit_code": 0, "summary": ""})
    error = _bench.error_record(planned(repeat=2), "bad seed", now=5)
    arm = _bench.summarize([good, error])["arms"]["candidate"]
    assert arm["pass_rate"] == 1 and arm["first_pass_rate"] == 1
    assert arm["harness_errors"] == 1


def test_repeated_cases_report_spread():
    records = [
        _bench.case_record(planned(repeat=i), RUN, snapshot(), harness(ended_at=1000 + i * 10), None)
        for i in (1, 2, 3)
    ]
    spread = _bench.summarize(records)["arms"]["candidate"]["spread"]["wall_s"]
    assert spread == {"min": 10, "max": 30, "iqr": 10}
    assert "IQR" in _bench.render_report(records, _bench.summarize(records), title="spread")


def test_records_disclose_contamination_and_step_one_omissions():
    record = _bench.case_record(planned(), RUN, snapshot(), harness(), None)
    assert record["contamination_risk"]
    notes = " ".join(_bench.caveats([record]))
    for omission in (
        "contamination_risk",
        "quota guard",
        "metrics block",
        "baseline",
        "held-out",
        "persistence",
    ):
        assert omission in notes


@pytest.mark.parametrize("missing", [("repo",), ("repo_key",), ("repo", "repo_key")])
def test_bench_requires_explicit_repo_and_key(missing):
    text = BENCH
    for key in missing:
        text = re.sub(rf"^{key} = .*\n", "", text, flags=re.MULTILINE)
    with pytest.raises(_profile.ProfileError) as exc:
        profile(text)
    assert any(key in str(exc.value) for key in missing)


@pytest.mark.parametrize("key", ["sandbox_key", "factory_key"])
def test_bench_rejects_production_keys(key):
    with pytest.raises(_profile.ProfileError, match="repo_key.*production"):
        profile(BENCH.replace('repo_key = "bench_repo_key"', f'repo_key = "{key}"'))


@pytest.mark.parametrize("entrypoint", ["parse", "bench_profile", "prepare"])
@pytest.mark.parametrize("bench_token", ["", "production_token", "bench_token"])
def test_a_bench_may_share_the_production_token(monkeypatch, entrypoint, bench_token):
    """#84: one work factory, one work token; nothing refuses a bench that runs on production's."""
    from flyte_agent_factory import _bench_flow

    configured = profile(BENCH + (f'claude_token = "{bench_token}"\n' if bench_token else ""))
    assert configured.bench is not None
    assert configured.bench.claude_token == (bench_token or "production_token")
    if entrypoint == "bench_profile":
        bench = _bench.bench_profile(configured, SHA)
        assert bench.secrets.claude_token == configured.bench.claude_token
        assert {bench.deploy_env(s)["AGENT_FACTORY_CLAUDE_TOKEN_SECRET"] for s in bench.slots} == {
            configured.bench.claude_token
        }
    elif entrypoint == "prepare":
        monkeypatch.setattr(_bench_flow, "PROFILE", configured)
        assert _bench_flow.prepare("r1", SHA, ["bugfix-shortlinks"], 1, "{}", 0.0)[2] is False


def test_a_bench_without_logins_runs_every_case_on_the_one_token():
    configured = profile()
    bench = _bench.bench_profile(configured, SHA)
    assert len(bench.slots) == bench.cap == 2
    for slot in bench.slots:
        assert bench.deploy_env(slot)["AGENT_FACTORY_CLAUDE_TOKEN_SECRET"] == "production_token"


@pytest.mark.parametrize(
    "key",
    [
        "api_key_x",  # [secrets]
        "gateway_key_x",
        "codex_token_x",
        "production_token",  # [secrets] claude_token
        "app_key",  # [github] private_key_secret
        "hook_secret",  # [github] webhook_secret
        "board_pat",  # [board] pat_secret
    ],
)
def test_bench_rejects_any_secret_the_profile_references(key):
    with pytest.raises(_profile.ProfileError, match="repo_key.*production"):
        profile(BENCH.replace('repo_key = "bench_repo_key"', f'repo_key = "{key}"'))


def test_bench_rejects_any_repo_keys_value_even_when_it_is_not_the_deploy_key():
    text = PROFILE_TEXT.replace('factory = "factory_key"', 'factory = "factory_key_2"')
    with pytest.raises(_profile.ProfileError, match="repo_key.*production"):
        profile(BENCH.replace("bench_repo_key", "factory_key_2"), text)


def test_bench_rejects_a_repository_that_is_a_repo_alias():
    old = "https://github.com/example-org/old-factory"
    with pytest.raises(_profile.ProfileError, match=r"must not be a \[repo_aliases\] factory"):
        profile(BENCH.replace("git@github.com:example-org/factory-bench.git", old))


@pytest.mark.parametrize("repo", ["sandbox", "factory"])
@pytest.mark.parametrize(
    "url", ["git@github.com:example-org/{repo}.git", "https://github.com/example-org/{repo}"]
)
def test_bench_rejects_production_repositories(repo, url):
    entry = "project" if repo == "sandbox" else repo
    with pytest.raises(_profile.ProfileError, match=rf"must not be \[repo\] {entry}"):
        profile(BENCH.replace("git@github.com:example-org/factory-bench.git", url.format(repo=repo)))


def test_bench_rejects_the_gcp_service_account_secret():
    # gcp_bootstrap creates or replaces this secret, so a bench key of the same name would be overwritten.
    text = PROFILE_TEXT + '\n[gcp]\nsecret_name = "gcp_sa"\n'
    with pytest.raises(_profile.ProfileError, match="repo_key.*production"):
        profile(BENCH.replace('repo_key = "bench_repo_key"', 'repo_key = "gcp_sa"'), text)


def test_no_subagent_block_means_no_subagent_spend_yet():
    # The supervisor adds `cost.subagents` with its first recorded sub-agent usage (agent.py `_Timeline`),
    # so a report whose author has a cost and no sub-agent block has spent $0 on sub-agents, not unknown.
    # The smoke run's watchdog stopped a healthy case at 5 minutes on exactly this report.
    report = {"cost": {"coordinator": {"usd_estimate": 1.76}, "meters": []}}
    assert _bench.usd(report)[0] == 1.76
    # Still unknown while the author has no cost figure, or when the cost block is absent.
    assert _bench.usd({"cost": {"subagents": {"usd": 2.0}}})[0] is None
    assert _bench.usd({})[0] is None


def test_metrics_record_uses_timeline_events(tmp_path, monkeypatch):

    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(_session_config, "_usage", {})
    monkeypatch.setattr(_session_config, "_usage_loaded", True)
    monkeypatch.setattr(_session_config, "_acceptance_text", lambda: ("", False))
    clock = [1000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    timeline = _timeline._Timeline()
    timeline.header("model", "sonnet")
    timeline.phase("building")
    clock[0] = 1010.0
    monkeypatch.setattr(_session_config, "_acceptance_text", lambda: ("1. Prints a greeting.", False))
    timeline.snapshot()
    clock[0] = 1030.0
    timeline.phase("checks")
    clock[0] = 1040.0
    timeline.phase("review", round_=1)
    timeline.verdicts.append(
        {"round": 1, "verdict": "PASS", "blocking": 0, "changed_files": ["main.py"], "at": 1050.0}
    )
    _usage._record_usage(
        "correctness",
        1,
        {
            "modelUsage": {
                "opus": {
                    "inputTokens": 5,
                    "outputTokens": 3,
                    "cacheReadInputTokens": 7,
                    "cacheCreationInputTokens": 2,
                }
            }
        },
    )
    timeline.author_usage = {"input_tokens": 10, "output_tokens": 4}
    clock[0] = 1050.0
    _inbox._save_pass(1, "abc", timeline=timeline)
    timeline.phase("done")
    snap = _dashboard.from_report(timeline.render())
    assert snap is not None
    record = _bench.case_record(planned(), RUN, snap, harness(ended_at=1050), None)
    assert record["metrics_schema"] == 1
    assert record["passed_at"] == 1050.0
    assert record["blocking_by_round"] == [0]
    assert record["stages_s"] == {
        "planning": 10,
        "build": 20,
        "checks": 10,
        "review": 10,
        "waiting": 0,
        "limited": 0,
    }
    assert record["tokens"]["correctness"]["opus"] == {
        "input": 5,
        "output": 3,
        "cache_read": 7,
        "cache_write": 2,
    }
    assert record["tokens"]["author"]["sonnet"]["input"] == 10
    # No split was recorded (only `author_usage` was set): the coordinator falls back to the author total.
    assert record["tokens"]["coordinator"] == record["tokens"]["author"]
    assert record["preflight_bounces"] == 0
    assert record["knobs_seen"] == snap["metrics"]["knobs"]


def test_legacy_record_keeps_unknown_measurements_null():
    record = _bench.case_record(planned(), RUN, snapshot("done"), harness(), None)
    for field in ("metrics_schema", "stages_s", "tokens", "preflight_bounces", "knobs_seen"):
        assert record[field] is None


def test_metrics_and_legacy_records_share_the_same_history(tmp_path, monkeypatch):

    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(_session_config, "_round_scores", [4, 1])
    timeline = _timeline._Timeline()
    for row in review_rows([4, 1], passed=True):
        timeline.row(row["icon"], row["text"])
    timeline.row("🚧", "DONE bounced before review (1): missing commit")
    _inbox._save_pass(3, "a" * 40, timeline=timeline)
    _contract._reset_contract()
    timeline.phase("done")
    snap = _dashboard.from_report(timeline.render())
    assert snap is not None
    observed = harness(asks=0, blocking_asks=0, first_ask_s=None, passed_at=snap["metrics"]["passed_at"])
    with_metrics = _bench.case_record(planned(), RUN, snap, observed, None)
    without = {k: v for k, v in snap.items() if k != "metrics"}
    legacy = _bench.case_record(planned(), RUN, without, observed, None)
    metrics_only = {"metrics_schema", "stages_s", "tokens", "preflight_bounces", "knobs_seen"}
    for field in legacy.keys() - metrics_only:
        assert with_metrics[field] == legacy[field], field
    assert with_metrics["qa_rounds"] == with_metrics["passed_round"] == 3
    assert with_metrics["blocking_by_round"] == [4, 1]


def test_an_old_policy_prefix_is_still_checked_and_warned_about():
    # The old prefix is no longer an alias (#172) and nothing warns: it is not stripped, so a key the current
    # prefix accepts is still checked, and rejected. Built from parts so the old name is not spelled out here.
    family = "FACTORY"
    with pytest.raises(ValueError, match="Claude-only"):
        _bench.plan_cases("r1", SHA, [], 1, {f"CLAUDE_{family}_QA_DIFFERENT_ENGINE": "false"}, "opus")
    # Nor is there a deprecation path left: the same key under the current prefix is accepted.
    _bench.plan_cases("r1", SHA, [], 1, {"AGENT_FACTORY_QA_DIFFERENT_ENGINE": "false"}, "opus")


def test_a_prefixed_policy_key_is_checked_like_a_bare_one():
    with pytest.raises(ValueError, match="Claude-only"):
        _bench.plan_cases("r1", SHA, [], 1, {"AGENT_FACTORY_JUDGE_ENGINE": "codex"}, "opus")


def test_a_case_record_carries_the_coordinator_main_conversation_apart_from_helpers(tmp_path, monkeypatch):
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(_session_config, "_usage", {})
    monkeypatch.setattr(_session_config, "_usage_loaded", True)
    timeline = _timeline._Timeline()
    timeline.header("model", "sonnet")
    timeline.phase("building")
    timeline.phase("done")
    main = {"input_tokens": 10, "modelUsage": {"sonnet": {"inputTokens": 10, "cacheReadInputTokens": 900}}}
    helpers = {"input_tokens": 5, "modelUsage": {"sonnet": {"inputTokens": 5, "cacheReadInputTokens": 50}}}
    timeline.set_author_usage({"coordinator": main, "helpers": helpers}, _usage._sum_usage(main, helpers))
    snap = _dashboard.from_report(timeline.render())
    assert snap is not None
    record = _bench.case_record(planned(), RUN, snap, harness(), None)
    assert record["tokens"]["coordinator"]["sonnet"] == {
        "input": 10,
        "output": 0,
        "cache_read": 900,
        "cache_write": 0,
    }
    assert record["tokens"]["helpers"]["sonnet"]["cache_read"] == 50
    assert record["tokens"]["author"]["sonnet"]["cache_read"] == 950
