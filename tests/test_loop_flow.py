"""One PR through the loop using an offline App and benchmark."""

import asyncio
import json
from dataclasses import replace
from typing import Any

import flyte
import pytest
from flyte import remote

from flyte_agent_factory import _loop, _loop_flow, _profile
from tests.conftest import TEST_PROFILE
from tests.test_loop import verdict

BENCHMARK = _loop_flow._benchmark
ADMISSION = _loop_flow._admission


class App:
    def __init__(self):
        self.head, self.main = "a" * 40, "b" * 40
        self.state = "open"
        self.posted, self.merges, self.closes, self.labels = [], [], [], []
        self.files = [{"filename": "src/flyte_agent_factory/_dashboard.py"}]
        self.review = "success"
        self.health = "ok"

    def app_login(self):
        return "factory[bot]"

    def pull(self, repo, number):
        return {
            "state": self.state,
            "head": {"sha": self.head, "repo": {"full_name": repo}},
            "base": {"ref": "main"},
            "draft": False,
        }

    def default_branch_sha(self, repo):
        return "main", self.main

    def pull_files(self, repo, number):
        return self.files

    def status_at(self, repo, sha, context):
        assert sha == self.head and context == "factory/verifier"
        return self.review

    def commit_health(self, repo, sha):
        return self.health, "checks"

    def comments(self, repo, number):
        return self.posted

    def comment(self, repo, number, body):
        self.posted.append({"id": len(self.posted) + 1, "body": body, "user": {"login": self.app_login()}})

    def update_comment(self, repo, comment_id, body):
        next(comment for comment in self.posted if comment["id"] == comment_id)["body"] = body

    def merge_pull(self, repo, number, sha):
        self.merges.append(sha)
        self.state = "closed"
        return {"merged": True}

    def close_pull(self, repo, number):
        self.closes.append(number)
        self.state = "closed"

    def add_labels(self, repo, number, labels):
        self.labels.extend(labels)


@pytest.fixture
def world(monkeypatch):
    app = App()
    profile = replace(
        _profile.parse(TEST_PROFILE.read_text()),
        loop=_profile.Loop(enabled=True, repo="acme/factory", merge="on"),
    )
    monkeypatch.setattr(_loop_flow, "PROFILE", profile)
    monkeypatch.setattr(_loop_flow, "_app", lambda: app)
    monkeypatch.setattr(_loop, "killed", lambda *args: False)
    monkeypatch.setattr(_loop, "loop_pull", lambda *args: True)
    monkeypatch.setattr(_loop_flow, "_admission", lambda app: _loop.Budget("run"))
    monkeypatch.setattr(_loop_flow, "_revert_breaker", lambda app, now: "")

    async def report(*args, **kwargs):
        pass

    monkeypatch.setattr(_loop_flow, "_initialize_spend", report)
    monkeypatch.setattr(flyte.report.replace, "aio", report)
    calls = []

    async def benchmark(head, main):
        calls.append((head, main))
        return json.dumps({"verdict": verdict(), "engine_sha": head, "baseline_sha": main})

    monkeypatch.setattr(_loop_flow, "_benchmark", benchmark)
    return app, calls


def run():
    return asyncio.run(_loop_flow.loop_run.func(42))


def test_merge_and_repeat(world):
    app, calls = world
    assert run() == "merge"
    run()
    assert len(app.posted) == len(app.merges) == len(calls) == 1


@pytest.mark.parametrize("changed", ["head", "main"])
def test_stale(world, monkeypatch, changed):
    app, _ = world
    original = _loop_flow._benchmark

    async def benchmark(head, main):
        result = await original(head, main)
        setattr(app, changed, "c" * 40)
        return result

    monkeypatch.setattr(_loop_flow, "_benchmark", benchmark)
    assert run() == "stale"
    assert not app.merges
    assert "stale" in app.posted[0]["body"]


def test_killed_immediately_before_merge(world, monkeypatch):
    app, _ = world
    checks = iter([False, True])
    monkeypatch.setattr(_loop, "killed", lambda *args: next(checks))
    assert run() == "killed"
    assert not app.merges


def test_dry_run(world, monkeypatch):
    app, _ = world
    monkeypatch.setattr(
        _loop_flow,
        "PROFILE",
        replace(_loop_flow.PROFILE, loop=replace(_loop_flow.PROFILE.loop, merge="dry-run")),
    )
    assert run() == "merge"
    run()
    assert not app.merges and len(app.posted) == 1
    assert "dry-run" in app.posted[0]["body"]


def test_missing_aa(world, monkeypatch):
    app, _ = world

    async def benchmark(head, main):
        return json.dumps(
            {
                "verdict": {**verdict(), "verdict": "inconclusive", "reason": "no A/A band"},
                "engine_sha": head,
                "baseline_sha": main,
            }
        )

    monkeypatch.setattr(_loop_flow, "_benchmark", benchmark)
    assert run() == "inconclusive"
    assert not app.merges and "loop:inconclusive" in app.labels


def test_docs_require_green_checks_and_review(world):
    app, calls = world
    app.files = [{"filename": "docs/guide.md"}]
    app.health = "pending"
    assert run() == "skip"
    app.health = "ok"
    assert run() == "merge"
    assert not calls and len(app.merges) == 1


def test_review_failure_skips_benchmark(world):
    app, calls = world
    app.review = "pending"
    assert run() == "skip"
    assert not calls and not app.merges


def test_worse_closes_once(world, monkeypatch):
    app, _ = world

    async def benchmark(head, main):
        return json.dumps(
            {"verdict": {**verdict(), "verdict": "worse", "L": 0.2}, "engine_sha": head, "baseline_sha": main}
        )

    monkeypatch.setattr(_loop_flow, "_benchmark", benchmark)
    assert run() == "close"
    run()
    assert app.closes == [42] and len(app.posted) == 1


def test_owner_paths_never_benchmark_or_merge(world):
    app, calls = world
    app.files = [{"filename": "src/flyte_agent_factory/_gates.py"}]
    assert run() == "needs_owner"
    assert not calls and not app.merges and "needs-owner" in app.labels


def test_forged_comment_is_not_authority(world):
    app, _ = world
    app.posted.append({"body": "<!-- factory-loop:decision {} -->", "user": {"login": "stranger"}})
    assert run() == "merge"
    assert len(app.merges) == 1 and len(app.posted) == 2


def test_retry_after_comment_rechecks_authority(world, monkeypatch):
    app, _ = world
    original = app.merge_pull

    def fail(*args):
        raise RuntimeError("interrupted before merge")

    monkeypatch.setattr(app, "merge_pull", fail)
    with pytest.raises(RuntimeError, match="interrupted"):
        run()
    monkeypatch.setattr(app, "merge_pull", original)
    app.review = "failure"
    assert run() == "skip"
    assert not app.merges
    app.review = "success"
    assert run() == "merge"
    assert len(app.posted) == len(app.merges) == 1


def test_benchmark_submission_uses_deployed_task_and_ceiling(world, monkeypatch):
    from datetime import timedelta
    from types import SimpleNamespace

    from connectrpc.code import Code
    from connectrpc.errors import ConnectError

    events: list[Any] = []

    class Task:
        def __init__(self):
            self.override = SimpleNamespace(aio=self.overridden)

        async def overridden(self, **kwargs):
            events.append(kwargs)
            return self

        async def __call__(self, **kwargs):
            events.append(kwargs)
            return '{"verdict": {}}', ""

    async def missing(name):
        raise ConnectError(Code.NOT_FOUND, "missing")

    async def wait():
        pass

    def context(**kwargs):
        assert kwargs["labels"] == {"factory-loop": "loop"}

        async def submit(actual, **inputs):
            assert actual is task
            result = await actual(**inputs)

            async def outputs():
                return result

            return SimpleNamespace(wait=SimpleNamespace(aio=wait), outputs=SimpleNamespace(aio=outputs))

        return SimpleNamespace(run=SimpleNamespace(aio=submit))

    monkeypatch.setattr(remote.Run, "get", SimpleNamespace(aio=missing))
    monkeypatch.setattr(flyte, "with_runcontext", context)
    monkeypatch.setattr(flyte, "ctx", lambda: SimpleNamespace(action=SimpleNamespace(run_name="loop")))
    task = Task()

    def get_task(name, **kwargs):
        events.append((name, kwargs))
        return task

    monkeypatch.setattr(remote.Task, "get", get_task)
    monkeypatch.setattr(
        _loop_flow,
        "PROFILE",
        replace(_loop_flow.PROFILE, bench=_profile.Bench("repo", "key", 300)),
    )
    # world replaces the benchmark helper; call the production helper retained below.
    assert asyncio.run(BENCHMARK("head", "main")) == '{"verdict": {}}'
    assert events[0] == ("factory-bench.benchmark", {"auto_version": "latest"})
    assert events[1]["timeout"].max_queued_time == timedelta(minutes=10)
    assert events[2] == {"candidate_sha": "head", "baseline_sha": "main", "max_usd": 300}


@pytest.mark.parametrize("changed", ["head", "main"])
def test_movement_while_posting_comment_is_stale(world, monkeypatch, changed):
    app, _ = world
    original = app.comment

    def comment(*args):
        original(*args)
        setattr(app, changed, "d" * 40)

    monkeypatch.setattr(app, "comment", comment)
    assert run() == "stale"
    assert not app.merges


def test_review_revoked_while_benchmark_runs(world, monkeypatch):
    app, _ = world
    original = _loop_flow._benchmark

    async def benchmark(*args):
        result = await original(*args)
        app.review = "failure"
        return result

    monkeypatch.setattr(_loop_flow, "_benchmark", benchmark)
    assert run() == "skip"
    assert not app.merges


def test_repeated_dry_run_updates_one_decision_comment(world, monkeypatch):
    app, _ = world
    monkeypatch.setattr(
        _loop_flow,
        "PROFILE",
        replace(_loop_flow.PROFILE, loop=replace(_loop_flow.PROFILE.loop, merge="dry-run")),
    )
    assert run() == "merge"

    async def benchmark(head, main):
        return json.dumps(
            {
                "verdict": {**verdict(), "verdict": "inconclusive", "reason": "no A/A band"},
                "engine_sha": head,
                "baseline_sha": main,
            }
        )

    monkeypatch.setattr(_loop_flow, "_benchmark", benchmark)
    assert run() == "inconclusive"
    assert len(app.posted) == 1 and "inconclusive" in app.posted[0]["body"]


def test_review_revoked_while_comment_is_posted(world, monkeypatch):
    app, _ = world
    original = app.comment

    def comment(*args):
        original(*args)
        app.review = "failure"

    monkeypatch.setattr(app, "comment", comment)
    assert run() == "skip"
    assert not app.merges


def test_a_benchmark_of_another_baseline_or_without_shas_never_merges(world, monkeypatch):
    # QA loop L1: a summary for a different baseline, or with no SHAs at all, still merged.
    app, _ = world
    for summary in (
        {"verdict": verdict(), "engine_sha": app.head, "baseline_sha": "f" * 40},
        {"verdict": verdict(), "baseline_sha": app.main},
        {"verdict": verdict(), "engine_sha": app.head},
    ):

        async def benchmark(head, main, summary=summary):
            return json.dumps(summary)

        monkeypatch.setattr(_loop_flow, "_benchmark", benchmark)
        with pytest.raises(ValueError):
            run()
    assert app.merges == []


def test_main_moving_during_the_kill_check_is_stale(world, monkeypatch):
    app, _ = world
    checks = []

    def killed(*args):
        checks.append(1)
        if len(checks) == 2:  # the check just before the merge
            app.main = "c" * 40
        return False

    monkeypatch.setattr(_loop, "killed", killed)
    assert run() == "stale" and app.merges == []


def test_an_inconclusive_head_is_not_benchmarked_again(world, monkeypatch):
    # QA loop L1: each rerun of an inconclusive PR spent another full benchmark.
    app, calls = world
    monkeypatch.setattr(_loop, "decide", lambda *args: "inconclusive")
    assert run() == "inconclusive" and len(calls) == 1
    assert run() == "inconclusive" and len(calls) == 1


async def test_backlog_disabled_never_reads_sources(monkeypatch):
    monkeypatch.setattr(_loop_flow, "PROFILE", replace(_loop_flow.PROFILE, loop=_profile.Loop()))
    monkeypatch.setattr(_loop_flow, "_app", lambda: pytest.fail("disabled loop must not read credentials"))
    assert await _loop_flow.generate_backlog.func() == "skip"


def test_backlog_schedule_is_daily_and_has_a_queue_bound():
    from pathlib import Path

    source = Path(_loop_flow.__file__).read_text().split('name="factory-loop-backlog"', 1)[1]
    assert "flyte.FixedRate(1440)" in source
    assert "max_queued_time=timedelta(minutes=10)" in source


def test_budget_wait_does_not_benchmark_or_merge(world, monkeypatch):
    app, calls = world
    monkeypatch.setattr(_loop_flow, "_admission", lambda app: _loop.Budget("wait", "cap-daily", 123))
    assert run() == "cap-daily"
    assert not calls and not app.merges


def test_revert_breaker_labels_switch_before_benchmark(world, monkeypatch):
    app, calls = world
    issues = []

    def create_issue(repo, title, body):
        issues.append((title, body))
        return {"number": 99}

    app.create_issue = create_issue
    monkeypatch.setattr(_loop_flow, "_revert_breaker", lambda app, now: "reverts")
    assert run() == "killed"
    assert len(issues) == 1 and app.labels == ["loop-kill"]
    assert not calls and not app.merges


def test_unset_caps_block_before_reading_remote_spend(world, monkeypatch):
    monkeypatch.setattr(_loop_flow, "_spend", lambda *args: pytest.fail("unset caps must not read spend"))
    assert ADMISSION(world[0]).action == "wait"


def test_an_ordinary_reviewed_docs_pr_is_never_merged(world, monkeypatch):
    app, calls = world
    app.files = [{"filename": "docs/guide.md"}]
    monkeypatch.setattr(_loop, "loop_pull", lambda *args: False)
    assert run() == "not-loop"
    assert app.merges == [] and app.posted == [] and calls == []
