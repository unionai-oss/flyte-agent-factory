"""Results storage against a real local origin, without GitHub or Flyte calls."""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
import time
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, cast

import flyte
import pytest

from flyte_agent_factory import _bench, _bench_flow, _bench_store, _github
from flyte_agent_factory._github import GithubApp
from tests.test_bench import SHA, profile

NOW = 1_800_000_000.0
DAY = 86400


def field(rows: Iterable[Mapping[str, Any]], name: str) -> list[Any]:
    """Return the sorted `name` values of store records, which are typed as `object` fields."""
    return sorted(r[name] for r in rows)


def git(path, *args):
    return subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def origin(tmp_path):
    bare = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(bare))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", bare.as_uri(), str(seed))
    git(seed, "config", "user.name", "Test")
    git(seed, "config", "user.email", "test@example.org")
    (seed / "private.txt").write_text("direction only")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "seed")
    git(seed, "push", "origin", "HEAD:refs/heads/main")
    return bare


def record(run="r1", repeat=1, **over):
    """A measured case (outcome `failed`); pass `outcome="harness_error"` for a harness fault."""
    case = _bench.plan_cases(run, SHA, ["bugfix-shortlinks"], repeat, {}, "opus")[-1]
    return {
        **_bench.error_record({**case, "env": "bench-test"}, "test", now=NOW),
        "outcome": "failed",
        **over,
    }


def test_orphan_layout_resume_and_idempotency(origin):
    main = git(origin, "rev-parse", "main")
    row = record()
    date = datetime.fromtimestamp(NOW, timezone.utc).date().isoformat()
    with _bench_store.Store(origin.as_uri()) as store:
        assert store.completed("r1") == set()
        store.append_case(row)
        store.append_case(row)
        store.write_summary("r1", {"cases": 1})
        assert store.completed("r1") == {row["case_run"]}
        assert store.records("r1") == [row]
    assert git(origin, "rev-parse", "main") == main
    assert git(origin, "rev-list", "--max-parents=0", "bench-results") != main
    paths = git(origin, "ls-tree", "-r", "--name-only", "bench-results").splitlines()
    assert paths == [f"runs/{date}-r1/cases.jsonl", f"runs/{date}-r1/summary.json"]
    assert json.loads(git(origin, "show", f"bench-results:runs/{date}-r1/summary.json")) == {"cases": 1}
    with _bench_store.Store(origin.as_uri()) as store:
        assert store.records("r1") == [row]


def test_validation_and_push_ref_guard(origin):
    with _bench_store.Store(origin.as_uri()) as store:
        with pytest.raises(ValueError, match="missing"):
            store.append_case({})
        with pytest.raises(_bench_store.StoreError, match="refs/heads/bench-results"):
            store._push("refs/heads/main")
        with pytest.raises(ValueError):
            store.append_case(record(bench_run="../../main"))
    assert git(origin, "for-each-ref", "--format=%(refname)") == "refs/heads/main"


def test_default_branch_is_never_the_store(origin):
    git(origin, "symbolic-ref", "HEAD", "refs/heads/bench-results")
    with pytest.raises(_bench_store.StoreError, match="default branch"):
        with _bench_store.Store(origin.as_uri()) as store:
            store.append_case(record())


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("midnight", [False, True])
def test_concurrent_writers_keep_both_records(origin, monkeypatch, existing, midnight):
    if existing:
        with _bench_store.Store(origin.as_uri()) as store:
            store.append_case(record("seed"))
    # Both clones start before either pushes, including when the branch does not exist yet.
    with _bench_store.Store(origin.as_uri()) as first, _bench_store.Store(origin.as_uri()) as second:
        barrier = threading.Barrier(2)
        for store in (first, second):
            push = store._push
            calls = [0]

            def race(ref, push=push, calls=calls):
                calls[0] += 1
                if calls[0] == 1:
                    barrier.wait(timeout=10)
                return push(ref)

            monkeypatch.setattr(store, "_push", race)
        with ThreadPoolExecutor(2) as pool:
            futures = [
                pool.submit(s.append_case, record(repeat=i, started_at=NOW + (i - 1) * DAY * midnight))
                for i, s in enumerate((first, second), 1)
            ]
            for future in futures:
                future.result(timeout=20)
    with _bench_store.Store(origin.as_uri()) as store:
        assert field(store.records("r1"), "repeat") == [1, 2]
        store.append_case(record(repeat=3))
        assert len(list((store.tree / "runs").glob("*-r1"))) == 1


def test_missing_contents_write_is_clear_and_does_not_leak_url(origin):
    hook = origin / "hooks" / "pre-receive"
    hook.write_text('#!/bin/sh\necho "Permission denied" >&2\nexit 1\n')
    hook.chmod(0o755)
    with _bench_store.Store(origin.as_uri()) as store:
        with pytest.raises(_bench_store.StoreError, match="contents: write") as error:
            store.append_case(record())
        assert origin.as_uri() not in str(error.value)


@pytest.mark.parametrize("age,found", [(0, True), (7 * DAY - 1, True), (7 * DAY, False), (-1, False)])
def test_baseline_age_boundaries(origin, monkeypatch, age, found):
    monkeypatch.setattr(time, "time", lambda: NOW)
    row = record(ended_at=NOW - age)
    with _bench_store.Store(origin.as_uri()) as store:
        store.append_case(row)
        result = store.baseline_records(SHA, {}, row["suite_digest"], row["task"], 1, 7)
        assert (result == row) if found else result is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("engine_sha", "b" * 40),
        ("policy", {"X": "y"}),
        ("suite_digest", "other"),
        ("task", "other"),
        ("repeat", 2),
    ],
)
def test_baseline_requires_every_key(origin, field, value):
    row = record(**{field: value})
    with _bench_store.Store(origin.as_uri()) as store:
        store.append_case(row)
        wanted = record()
        assert store.baseline_records(SHA, {}, wanted["suite_digest"], wanted["task"], 1, 100000) is None


def test_aa_history_filters_and_orders_runs(origin, monkeypatch):
    monkeypatch.setattr(time, "time", lambda: NOW)
    with _bench_store.Store(origin.as_uri()) as store:
        for run, age, digest, model, logins in [
            ("new", 1, "suite", "opus", ["one"]),
            ("old", 20 * DAY, "suite", "opus", ["one"]),
            ("expired", 28 * DAY, "suite", "opus", ["one"]),
            ("digest", 1, "other", "opus", ["one"]),
            ("model", 1, "suite", "sonnet", ["one"]),
            ("login", 1, "suite", "opus", ["two"]),
        ]:
            store.write_aa(
                run,
                [record(run, ended_at=NOW - age)],
                sigma_aa=0.1,
                suite_digest=digest,
                model=model,
                logins=logins,
            )
        history = store.aa_history("suite", "opus", ["one"])
        assert [rows[0]["bench_run"] for rows in history] == ["old", "new"]
        assert store.aa_history("suite", "opus", ["one"], max_merges=1) == history[-1:]
        assert store.aa_history("suite", "opus", ["one"], max_merges=0) == []


def test_bench_profile_cannot_reach_direction_store():
    selected = profile()
    stripped = _bench.bench_profile(selected, SHA)
    assert stripped.direction is None and stripped.bench is None
    assert stripped.github.private_key_secret == ""
    assert selected.direction is not None
    assert selected.direction.repo not in stripped.text
    assert "bench-results" not in stripped.text


@pytest.mark.parametrize("all_completed", [False, True])
def test_benchmark_resumes_completed_cases_and_includes_them(origin, monkeypatch, all_completed):
    monkeypatch.setattr(_bench_flow, "PROFILE", profile())
    monkeypatch.setattr(_bench_flow, "_app", lambda: object())
    monkeypatch.setattr(_bench_flow, "_store", lambda app: _bench_store.Store(origin.as_uri()))
    monkeypatch.setattr(_bench_flow, "resolve_sha", lambda *args: SHA)
    cases = _bench.plan_cases("r1", SHA, ["bugfix-shortlinks"], 2, {}, "opus")
    saved = record()
    with _bench_store.Store(origin.as_uri()) as store:
        store.append_case(saved)
        if all_completed:
            store.append_case(record(repeat=2))
    monkeypatch.setattr(_bench_flow, "prepare", lambda *args: (cases, {}, True))
    monkeypatch.setattr(
        flyte,
        "ctx",
        lambda: type("Ctx", (), {"action": type("Action", (), {"run_name": "r1"})()})(),
    )
    recovered = []

    def stop_cases(rows):
        recovered.extend(rows)
        return {}

    monkeypatch.setattr(_bench_flow, "_stop_cases", stop_cases)

    async def deploy(*args):
        assert not all_completed, "a completed benchmark must not deploy an engine"
        return ["bench-test"]

    async def run(pending, envs, save):
        assert pending == cases[1:]
        save(record(repeat=2))
        return [record(repeat=2)]

    async def report(*args, **kwargs):
        pass

    monkeypatch.setattr(_bench_flow, "deploy_engine", deploy)
    monkeypatch.setattr(_bench_flow, "_run_all", run)
    monkeypatch.setattr(flyte.report.replace, "aio", report)
    summary, jsonl = asyncio.run(_bench_flow.benchmark.func(SHA, max_usd=100))
    assert json.loads(summary)["bench_run"] == "r1"
    rows = [json.loads(line) for line in jsonl.splitlines()]
    assert rows == [saved, record(repeat=2)]
    assert recovered == ([] if all_completed else cases[1:])
    with _bench_store.Store(origin.as_uri()) as store:
        assert store.records("r1") == rows


@pytest.mark.parametrize("changed", ["engine_sha", "policy", "suite_digest", "model"])
def test_resume_refuses_changed_inputs(origin, monkeypatch, changed):
    monkeypatch.setattr(_bench_flow, "PROFILE", profile())
    monkeypatch.setattr(_bench_flow, "_app", lambda: object())
    monkeypatch.setattr(_bench_flow, "_store", lambda app: _bench_store.Store(origin.as_uri()))
    monkeypatch.setattr(_bench_flow, "resolve_sha", lambda *args: SHA)
    cases = _bench.plan_cases("r1", SHA, ["bugfix-shortlinks"], 1, {}, "opus")
    row = record(**{changed: {"CRAFT_EFFORT": "low"} if changed == "policy" else "other"})
    with _bench_store.Store(origin.as_uri()) as store:
        store.append_case(row)
    monkeypatch.setattr(_bench_flow, "prepare", lambda *args: (cases, {}, True))
    monkeypatch.setattr(
        flyte,
        "ctx",
        lambda: type("Ctx", (), {"action": type("Action", (), {"run_name": "r1"})()})(),
    )

    async def report(*args, **kwargs):
        pass

    monkeypatch.setattr(flyte.report.replace, "aio", report)
    with pytest.raises(_bench_store.StoreError, match="different inputs"):
        asyncio.run(_bench_flow.benchmark.func(SHA, max_usd=100))


def test_completed_case_is_durable_before_other_cases_finish(origin, monkeypatch):
    cases = _bench.plan_cases("r1", SHA, ["bugfix-shortlinks"], 2, {}, "opus")
    monkeypatch.setattr(_bench_flow, "PROFILE", profile())  # `_run_all` reads the token concurrency
    monkeypatch.setattr(_bench_flow, "_case_live", lambda name: False)

    async def exercise():
        published = asyncio.Event()
        loop = asyncio.get_running_loop()

        async def run(task, encoded):
            case = json.loads(encoded)
            if case["repeat"] == 2:
                await published.wait()
                with _bench_store.Store(origin.as_uri()) as store:
                    assert store.completed("r1") == {cases[0]["case"]}
            return json.dumps(record(repeat=case["repeat"]))

        def save(row):
            with _bench_store.Store(origin.as_uri()) as store:
                store.append_case(row)
            loop.call_soon_threadsafe(published.set)

        monkeypatch.setattr(_bench_flow, "_spot_first", run)
        return await asyncio.wait_for(_bench_flow._run_all(cases, ["bench-1", "bench-2"], save), 30)

    assert len(asyncio.run(exercise())) == 2


def test_installation_token_is_for_direction_repo(monkeypatch):
    selected = profile()
    monkeypatch.setattr(_bench_flow, "PROFILE", selected)
    requested = []

    class App:
        def installation_token(self, repo):
            requested.append(repo)
            return "test-token"

    monkeypatch.setattr(_bench_store, "Store", lambda url: url)
    url: object = _bench_flow._store(cast(GithubApp, App()))  # `Store` is stubbed to return the url
    assert requested == ["example-org/direction"]
    assert url == "https://x-access-token:test-token@github.com/example-org/direction.git"


def test_push_retries_are_bounded(origin, monkeypatch):
    with _bench_store.Store(origin.as_uri()) as seed:
        seed.append_case(record("seed"))
    with _bench_store.Store(origin.as_uri()) as store:
        attempts = []

        def reject(ref):
            attempts.append(ref)
            return subprocess.CompletedProcess([], 1, "[rejected] (non-fast-forward)", "")

        monkeypatch.setattr(store, "_push", reject)
        monkeypatch.setattr(time, "sleep", lambda delay: None)
        with pytest.raises(_bench_store.StoreError, match="concurrent-write retries"):
            store.append_case(record())
        assert len(attempts) == _bench_store.MAX_ATTEMPTS
    with _bench_store.Store(origin.as_uri()) as store:
        assert store.completed("r1") == set()


def test_token_failure_is_a_store_error(monkeypatch):
    monkeypatch.setattr(_bench_flow, "PROFILE", profile())

    class App:
        def installation_token(self, repo):
            raise _github.GithubError("access refused")

    with pytest.raises(_bench_store.StoreError, match="installation token"):
        _bench_flow._store(cast(GithubApp, App()))


def test_an_existing_bench_results_branch_must_be_an_orphan(origin, tmp_path):
    # QA: a bench-results branch made from main would carry the direction repo's private files.
    seed = tmp_path / "seed"
    git(seed, "push", "origin", "HEAD:refs/heads/bench-results")
    with pytest.raises(_bench_store.StoreError, match="orphan"):
        _bench_store.Store(origin.as_uri())


def test_eight_concurrent_writers_lose_nothing(origin):
    # QA: with a fixed backoff, eight writers retried in lockstep and dropped three records.
    stores = [_bench_store.Store(origin.as_uri()) for _ in range(8)]
    try:
        barrier = threading.Barrier(8)

        def write(i, store):
            barrier.wait(timeout=20)
            store.append_case(record(run=f"w{i}"))

        with ThreadPoolExecutor(8) as pool:
            for future in [pool.submit(write, i, s) for i, s in enumerate(stores)]:
                future.result(timeout=300)
    finally:
        for store in stores:
            store.__exit__(None, None, None)
    with _bench_store.Store(origin.as_uri()) as store:
        assert field(store.records(), "bench_run") == [f"w{i}" for i in range(8)]


def test_a_git_timeout_keeps_the_url_out_of_the_whole_exception_chain(monkeypatch):
    secret = "https://x-access-token:SECRET-TOKEN@example.invalid/o/r.git"

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["git", "clone", secret], timeout=1)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(_bench_store.StoreError) as error:
        _bench_store.Store(secret)
    chain: list[str] = []
    seen: BaseException | None = error.value
    while seen is not None:
        chain.append(repr(seen) + repr(getattr(seen, "cmd", "")))
        seen = seen.__cause__ or seen.__context__
    assert "SECRET-TOKEN" not in "".join(chain)


def test_a_harness_fault_is_retried_not_kept(origin, monkeypatch):
    monkeypatch.setattr(time, "time", lambda: NOW + 1)
    fault = record(outcome="harness_error")
    measured = record(outcome="accepted")
    with _bench_store.Store(origin.as_uri()) as store:
        store.append_case(fault)
        assert store.completed("r1") == set()
        assert store.baseline_records(SHA, {}, fault["suite_digest"], fault["task"], 1, 100000) is None
        store.append_case(measured)
        assert store.records("r1") == [measured]
        assert store.completed("r1") == {measured["case_run"]}


def test_baseline_reuse_requires_the_same_model(origin, monkeypatch):
    monkeypatch.setattr(time, "time", lambda: NOW + 1)
    with _bench_store.Store(origin.as_uri()) as store:
        store.append_case(record(model="sonnet"))
        wanted = record()
        assert store.baseline_records(SHA, {}, wanted["suite_digest"], wanted["task"], 1, 100000) is None
        assert (
            store.baseline_records(SHA, {}, wanted["suite_digest"], wanted["task"], 1, 100000, model="sonnet")
            is not None
        )


def test_history_survives_a_schema_change_and_a_corrupt_line(origin):
    # QA: reads validated every field, so the first schema change would make every old record unreadable.
    with _bench_store.Store(origin.as_uri()) as store:
        store.append_case(record())
        path = next((store.tree / "runs").glob("*/cases.jsonl"))
        old = {k: v for k, v in record(repeat=2).items() if k != "spot_check"}
        path.write_text(path.read_text() + json.dumps({**old, "retired_field": 1}) + "\n{not json\n")
        assert field(store.records("r1"), "repeat") == [1, 2]


def test_an_unsaved_record_is_kept_in_the_log(monkeypatch, capsys):
    monkeypatch.setattr(_bench_flow, "PROFILE", profile())

    class Broken:
        def __init__(self, url):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def append_case(self, record):
            raise _bench_store.StoreError("results store push failed")

    monkeypatch.setattr(_bench_flow, "_store", lambda app: Broken(""))
    row = record()
    save = _bench_flow._saver(cast(GithubApp, object()), {})
    with pytest.raises(_bench_store.StoreError):
        save(row)
    assert f'"case_run": "{row["case_run"]}"' in capsys.readouterr().err


def test_suite_versions_do_not_share_baselines_or_aa_history(origin, monkeypatch):
    from flyte_agent_factory import _bench_suite

    monkeypatch.setattr(time, "time", lambda: NOW)
    assert _bench_suite.SUITE == "v3"
    current_digest = _bench_suite.digest()
    monkeypatch.setattr(_bench_suite, "SUITE", "v2")
    previous_digest = _bench_suite.digest()
    assert current_digest != previous_digest
    row = record(suite_digest=previous_digest)
    with _bench_store.Store(origin.as_uri()) as store:
        store.append_case(row)
        store.write_aa("r1", [row], sigma_aa=0.1, suite_digest=previous_digest, model="opus", logins=["one"])
        assert store.baseline_records(SHA, {}, previous_digest, row["task"], 1, 7) == row
        assert store.baseline_records(SHA, {}, current_digest, row["task"], 1, 7) is None
        assert store.aa_history(previous_digest, "opus", ["one"]) == [[row]]
        assert store.aa_history(current_digest, "opus", ["one"]) == []
