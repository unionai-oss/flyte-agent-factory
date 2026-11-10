"""Durable admission uses GitHub CAS, independent of Flyte listing visibility."""

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import pytest
from flyte import remote

from flyte_agent_factory import _admission, _factory, _github, _issues


def no_run(_name: str) -> Any:
    """A lookup that finds no run for the holder."""
    return None


class Git:
    def __init__(self):
        self.refs: dict[str, str] = {}
        self.commits: dict[str, Any] = {"base": {"tree": {"sha": "tree"}, "message": "{}"}}

    def _call(self, repo, method, path, json=None):
        if method == "GET" and "/ref/" in path:
            ref = path.split("/ref/")[1]
            if ref not in self.refs:
                raise _github.GithubError("missing", 404)
            return {"object": {"sha": self.refs[ref]}}
        if method == "GET" and "/commits/" in path:
            return self.commits[path.rsplit("/", 1)[1]]
        if method == "POST" and path.endswith("/commits"):
            sha = str(len(self.commits))
            self.commits[sha] = {**json, "sha": sha}
            return {"sha": sha}
        if method == "POST" and path.endswith("/refs"):
            ref = json["ref"].removeprefix("refs/")
            if ref in self.refs:
                raise _github.GithubError("exists", 422)
            self.refs[ref] = json["sha"]
            return {}
        if method == "PATCH":
            ref = path.split("/refs/")[1]
            if self.refs[ref] not in self.commits[json["sha"]]["parents"]:
                raise _github.GithubError("not fast forward", 422)
            self.refs[ref] = json["sha"]
            return {}
        if method == "DELETE":
            del self.refs[path.split("/refs/")[1]]
            return {}
        raise AssertionError((method, path))


def test_record_create_and_update_are_exclusive():
    from flyte_agent_factory._admission import GitRecord

    git: Any = Git()  # stands in for the GithubApp the records call through
    first = GitRecord(git, "team/direction", "factory/intents/issue-1", "base")
    second = GitRecord(git, "team/direction", "factory/intents/issue-1", "base")
    assert first.read() == second.read() == {}
    first.write({"state": "pending"})
    with pytest.raises(_admission.AdmissionInvariant):
        second.write({"state": "pending"})
    assert second.read() == {"state": "pending"}
    first.write({"state": "submitted"})
    with pytest.raises(_admission.AdmissionInvariant):
        second.write({"state": "submitted"})


def test_planner_lock_reclaims_only_authoritative_terminal_holder():
    from flyte_agent_factory._admission import GitRecord, acquire_lock

    git: Any = Git()  # stands in for the GithubApp the records call through

    def record():
        return GitRecord(git, "team/direction", "factory/locks/planner", "base")

    phase = {"first": "RUNNING"}

    def lookup(name):
        return SimpleNamespace(phase=phase[name])

    assert acquire_lock(record(), "first", lookup)
    assert not acquire_lock(record(), "second", lookup)
    phase["first"] = "SUCCEEDED"
    assert acquire_lock(record(), "second", lookup)
    value = json.loads(git.commits[git.refs["factory/locks/planner"]]["message"])
    assert value["run"] == "second"


def test_unreadable_lock_holder_fails_closed():
    from flyte_agent_factory._admission import GitRecord, acquire_lock

    git: Any = Git()  # stands in for the GithubApp the records call through

    def record():
        return GitRecord(git, "team/direction", "factory/locks/planner", "base")

    assert acquire_lock(record(), "missing", no_run)
    with pytest.raises(AttributeError):
        acquire_lock(record(), "new", no_run)


@pytest.fixture
def runtime(monkeypatch):
    from flyte_agent_factory import _planner_runtime as runtime

    git: Any = Git()  # stands in for the GithubApp the records call through
    monkeypatch.setattr(runtime, "_intents", lambda *_: [])
    monkeypatch.setattr(remote.Run, "listall", lambda **_: [])
    monkeypatch.setattr(_factory, "_resolve_repo", lambda repo, **_: repo)
    monkeypatch.setattr(_issues, "repo_targets", lambda _: {"team/repo": ("team/repo", "team/repo")})
    git.default_branch_sha = lambda *_: ("main", "base")
    run = SimpleNamespace(name="project-run", url="https://console.example/run", phase="RUNNING")
    submitted = []

    def submit(*args, **kwargs):
        submitted.append(kwargs)
        return run

    monkeypatch.setattr(_factory, "launch_on_slot", submit)
    monkeypatch.setattr(runtime, "_matches", lambda _: [run])
    board = SimpleNamespace(set_status=lambda *_: None, set_text=lambda *_: None)
    from flyte_agent_factory import _planner

    item = _planner.BoardItem("I1", 1, "team/repo", status="Ready", body="## Files\n- a.py")
    facts = _planner.Facts(items=(item,), capacity=(_planner.Capacity("claude", "login", slot="slot"),))
    rows = [{"node_id": "I1", "repo": "team/repo", "number": 1, "item_id": "card", "status": "Ready"}]
    return runtime, git, board, facts, rows, submitted, run


def test_board_failure_retains_intent_and_never_relaunches(runtime, monkeypatch):
    module, git, board, facts, rows, submitted, run = runtime

    def broken(*_):
        raise OSError("board unavailable")

    board.set_status = broken
    with pytest.raises(OSError):
        module.execute(None, git, "team/direction", "base", board, rows, facts)
    assert len(submitted) == 1
    ref = next(ref for ref in git.refs if ref.startswith("factory/intents/"))
    record = _admission.GitRecord(git, "team/direction", ref, "base")
    intent = record.read()
    assert intent["run"] == run.name
    monkeypatch.setattr(module, "_intents", lambda *_: [(record, record.read())])
    board.set_status = lambda *_: None
    assert module.execute(None, git, "team/direction", "base", board, rows, facts) == []
    assert len(submitted) == 1


def test_ambiguous_project_submission_blocks_until_label_resolves(runtime, monkeypatch):
    module, git, board, facts, rows, submitted, run = runtime

    def ambiguous(*args, **kwargs):
        submitted.append(kwargs)
        raise TimeoutError("accepted without acknowledgement")

    monkeypatch.setattr(module._factory, "launch_on_slot", ambiguous)
    with pytest.raises(TimeoutError):
        module.execute(None, git, "team/direction", "base", board, rows, facts)
    ref = next(ref for ref in git.refs if ref.startswith("factory/intents/"))
    record = _admission.GitRecord(git, "team/direction", ref, "base")
    monkeypatch.setattr(module, "_intents", lambda *_: [(record, record.read())])
    monkeypatch.setattr(module, "_matches", lambda _: [])
    with pytest.raises(_admission.UnresolvedSubmission):
        module.execute(None, git, "team/direction", "base", board, rows, facts)
    monkeypatch.setattr(module, "_matches", lambda _: [run])
    module.execute(None, git, "team/direction", "base", board, rows, facts)
    assert len(submitted) == 1


def test_terminal_generation_history_prevents_relaunch_after_intent_cleanup(runtime, monkeypatch):
    module, git, board, facts, rows, submitted, run = runtime
    run.phase = "SUCCEEDED"
    module.execute(None, git, "team/direction", "base", board, rows, facts)
    assert git.refs
    ref = next(iter(git.refs))
    record = _admission.GitRecord(git, "team/direction", ref, "base")
    intent = record.read()
    rows[0].update(status="In flight", linked_run=run.url)
    module.reconcile(record, intent, board, rows)
    assert not git.refs
    monkeypatch.setattr(module.remote.Run, "listall", lambda **_: [run])
    module.execute(None, git, "team/direction", "base", board, rows, facts)
    assert len(submitted) == 1


async def test_event_burst_submits_each_eligible_generation_once(runtime):
    import asyncio

    from tests.test_admission import Backend, Records

    module, git, board, facts, rows, submitted, _ = runtime

    class PlannerBackend(Backend):
        async def submit(self, correlation):
            module.execute(None, git, "team/direction", "base", board, rows, facts)
            return await super().submit(correlation)

    backend = PlannerBackend()
    records = Records()
    consumer = _admission.Consumer(records, backend)
    task = asyncio.create_task(consumer.run())
    try:
        await asyncio.wait_for(backend.entered.wait(), 30)

        async def wake():
            await asyncio.sleep(0)
            consumer.wake()

        await asyncio.gather(*(wake() for _ in range(100)))
        backend.finished.set()
        for _ in range(1000):
            if len(backend.submissions) == 2 and records.value.get("done"):
                break
            await asyncio.sleep(0)
        assert len(backend.submissions) == 2
        assert len(submitted) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_concurrent_planners_contend_for_one_pass(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    git: Any = Git()  # stands in for the GithubApp the records call through
    call = git._call
    serial = threading.Lock()
    snapshot = threading.Barrier(2)

    def github(*args, **kwargs):
        with serial:
            return call(*args, **kwargs)

    monkeypatch.setattr(git, "_call", github)

    class Contender(_admission.GitRecord):
        def read(self):
            value = super().read()
            snapshot.wait(timeout=5)
            return value

    def enter(name):
        record = Contender(git, "team/direction", "factory/locks/planner", "base")
        return _admission.acquire_lock(record, name, no_run)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(enter, ("first", "second"))) == [False, True]


@pytest.mark.parametrize("engine", ["claude", "codex"])
def test_author_admission_persists_the_routed_engine_and_login(runtime, engine):
    from flyte_agent_factory import _planner

    module, git, board, facts, rows, submitted, _ = runtime
    facts = replace(facts, capacity=(_planner.Capacity(engine, f"{engine}-login", slot="slot"),))
    assert module.execute(None, git, "team/direction", "base", board, rows, facts) == ["project-run"]
    intent = _admission.GitRecord(git, "team/direction", next(iter(git.refs)), "base").read()
    assert (intent["engine"], intent["login"]) == (engine, f"{engine}-login")
    # Admission's choice goes under its own key: AGENT_FACTORY_COORDINATOR_ENGINE is the user's pin, and the
    # pod lets a pin beat the engine a resumed conversation is on.
    assert submitted[0]["policy"][module.ADMITTED_KEY] == engine
    assert "AGENT_FACTORY_COORDINATOR_ENGINE" not in submitted[0]["policy"]


@pytest.mark.parametrize("terminal_phase", ["SUCCEEDED", "FAILED", "ABORTED", "TIMED_OUT"])
def test_surviving_children_hold_the_parent_repository_until_terminal(monkeypatch, terminal_phase):
    from flyte_agent_factory import _planner, _planner_facts

    parent = SimpleNamespace(
        name="old-parent",
        phase="FAILED",
        pb2=SimpleNamespace(labels={"factory-issue": "old-issue"}),
        inputs=lambda: {"repo": "target"},
    )
    child = SimpleNamespace(name="a1", parent_name="a0", phase="RUNNING")
    profile = SimpleNamespace(
        slots=[SimpleNamespace(task="slot")],
        repo="team/default",
        repos={"target": "team/repo"},
    )
    queries = []

    def parents(**kwargs):
        queries.append(kwargs)
        return [parent]

    monkeypatch.setattr(remote.Run, "listall", parents)
    root = SimpleNamespace(name="a0", parent_name=None, phase="RUNNING")
    monkeypatch.setattr(remote.Action, "listall", lambda **_: [root, child])
    item = _planner.BoardItem("new", 2, "team/repo", status="Ready", body="## Files\n- other.py")

    # `profile` is a stub of the fields `live_children` reads
    def plan():
        facts = _planner.facts_from(
            [],
            [],
            [],
            [_planner.Capacity("claude", "login")],
            [],
            default_repo="team/repo",
        )
        return _planner.plan(
            replace(facts, items=(item,), active_runs=tuple(_planner_facts.live_children(cast(Any, profile))))
        )

    assert not plan().launches
    assert all("in_phase" not in query and "updated_at" not in query for query in queries)
    child.phase = terminal_phase
    assert len(plan().launches) == 1


def test_an_exhausted_codex_only_slot_waits_without_creating_an_intent(runtime):
    from flyte_agent_factory import _meter, _planner

    module, git, board, facts, rows, submitted, _ = runtime
    # Owner rule 2: exhausted is a refusal (`limited_until`); this used a 100% reading alone.
    meter = _meter.Meter(
        "codex codex-login", "app-server", facts.now, 100, facts.now + 3600, limited_until=facts.now + 3600
    )
    facts = replace(facts, capacity=(_planner.Capacity("codex", "codex-login", meter, slot="slot"),))
    assert module.execute(None, git, "team/direction", "base", board, rows, facts) == []
    assert submitted == [] and not git.refs


def test_loop_admission_labels_launch_and_surviving_intent(runtime, monkeypatch):
    module, git, board, facts, rows, submitted, _ = runtime
    monkeypatch.setattr(module._loop, "killed", lambda *_: False)
    monkeypatch.setattr(module._factory, "owner_demand", lambda: False)
    monkeypatch.setattr(
        module._factory_meters, "_loop_slot_budget", lambda *_, **__: module._loop.Budget("run")
    )
    item = replace(facts.items[0], labels=("loop",))
    facts = replace(facts, items=(item,), loop_enabled=True)
    assert module.execute(None, git, "team/direction", "base", board, rows, facts) == ["project-run"]
    label = module._issues.issue_label("team/repo#1")
    assert submitted[0]["labels"]["factory-loop"] == label
    ref = next(iter(git.refs))
    record = _admission.GitRecord(git, "team/direction", ref, "base")
    active = module.reconcile(record, record.read(), board, rows)
    assert active.labels["factory-loop"] == label


def _loop_facts(facts):
    item = replace(facts.items[0], labels=("loop",))
    return replace(facts, items=(item,), loop_enabled=True)


def test_loop_launch_rechecks_the_kill_switch_at_submission(runtime, monkeypatch):
    module, git, board, facts, rows, submitted, _ = runtime
    monkeypatch.setattr(module._loop, "killed", lambda *_: True)
    monkeypatch.setattr(module._factory, "owner_demand", lambda: False)
    monkeypatch.setattr(
        module._factory_meters, "_loop_slot_budget", lambda *_, **__: module._loop.Budget("run")
    )
    assert module.execute(None, git, "team/direction", "base", board, rows, _loop_facts(facts)) == []
    assert submitted == [] and not git.refs


def test_loop_launch_fails_closed_when_the_kill_check_errors(runtime, monkeypatch):
    module, git, board, facts, rows, submitted, _ = runtime

    def broken(*_):
        raise _github.GithubError("unavailable", 503)

    monkeypatch.setattr(module._loop, "killed", broken)
    monkeypatch.setattr(module._factory, "owner_demand", lambda: False)
    monkeypatch.setattr(
        module._factory_meters, "_loop_slot_budget", lambda *_, **__: module._loop.Budget("run")
    )
    with pytest.raises(_github.GithubError):
        module.execute(None, git, "team/direction", "base", board, rows, _loop_facts(facts))
    assert submitted == [] and not git.refs


def test_loop_launch_obeys_the_spend_guard(runtime, monkeypatch):
    module, git, board, facts, rows, submitted, _ = runtime
    asked = []

    def guard(slot, *, demand):
        asked.append((slot, demand))
        return module._loop.Budget("wait", "caps-unset")

    monkeypatch.setattr(module._loop, "killed", lambda *_: False)
    monkeypatch.setattr(module._factory, "owner_demand", lambda: True)
    monkeypatch.setattr(module._factory_meters, "_loop_slot_budget", guard)
    assert module.execute(None, git, "team/direction", "base", board, rows, _loop_facts(facts)) == []
    assert asked == [("slot", True)]
    assert submitted == [] and not git.refs


def test_loop_label_is_matched_case_insensitively_at_launch(runtime, monkeypatch):
    module, git, board, facts, rows, submitted, _ = runtime
    facts = replace(_loop_facts(facts), items=(replace(facts.items[0], labels=("Loop",)),))
    monkeypatch.setattr(module._loop, "killed", lambda *_: False)
    monkeypatch.setattr(module._factory, "owner_demand", lambda: False)
    monkeypatch.setattr(
        module._factory_meters, "_loop_slot_budget", lambda *_, **__: module._loop.Budget("run")
    )
    assert module.execute(None, git, "team/direction", "base", board, rows, facts) == ["project-run"]
    assert submitted[0]["labels"]["factory-loop"] == module._issues.issue_label("team/repo#1")


def test_author_admission_prefers_the_authors_engine_over_headroom(runtime):
    # Owner rule 4: with no limit, the author goes to its preferred engine (Claude) however the meters
    # compare; headroom only breaks a tie. A Codex reading of 12% must not pull the launch to Codex.
    from flyte_agent_factory import _planner
    from flyte_agent_factory._meter import Meter

    module, git, board, facts, rows, submitted, _ = runtime
    claude = Meter("claude claude-login", "test", 1000, five_hour_pct=97, five_hour_resets_at=9000)
    codex = Meter("codex", "test", 1000, five_hour_pct=12, five_hour_resets_at=9000)
    capacity = (
        _planner.Capacity("claude", "claude-login", claude, slot="slot"),
        _planner.Capacity("codex", "codex-login", codex, slot="slot"),
    )
    facts = replace(facts, capacity=capacity)
    assert module.execute(None, git, "team/direction", "base", board, rows, facts) == ["project-run"]
    assert submitted[0]["policy"][module.ADMITTED_KEY] == "claude"
