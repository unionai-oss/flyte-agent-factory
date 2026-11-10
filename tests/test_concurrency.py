"""`[concurrency]`: a token profile without `[[slots]]` runs its projects on one shared environment."""

import asyncio
import dataclasses
import json
import re
from typing import Any

import pytest

from flyte_agent_factory import _factory as factory
from flyte_agent_factory import _factory_base, _profile, _queue
from tests.conftest import TEST_PROFILE
from tests.test_queue import request as _request

# `tests.test_queue.request` is untyped; calling it through `Any` keeps that out of this file's typed calls.
request: Any = _request
WORK = TEST_PROFILE.read_text() if hasattr(TEST_PROFILE, "read_text") else open(TEST_PROFILE).read()


def token_text(concurrency: str = "") -> str:
    """Return the suite profile as a setup-token deployment: no slots, the Codex token."""
    text = WORK[: WORK.index("[[slots]]")] + WORK[WORK.index("[secrets]") :]
    text = text.replace("[secrets]", '[secrets]\ncodex_token = "codex_token_x"')
    return text + concurrency


def token_profile(concurrency: str = "") -> _profile.Profile:
    """Parse the suite profile as a setup-token deployment (see `token_text`)."""
    return _profile.parse(token_text(concurrency))


@pytest.fixture
def shared_world(flyte_world, monkeypatch):
    """Return a deployer of the fake Flyte world from a token profile (default `[concurrency]`)."""

    def deploy(concurrency: str = ""):
        profile = token_profile(concurrency)
        monkeypatch.setattr(
            _factory_base, "PROFILE", dataclasses.replace(profile, state=_factory_base.PROFILE.state)
        )
        monkeypatch.setattr(_factory_base, "SLOTS", profile.tasks)
        return flyte_world

    return deploy


SHARED_TASK = _profile.SHARED_SLOT.task


# --- the profile -----------------------------------------------------------------------------


def test_concurrency_table_is_parsed():
    """The `[concurrency]` table is parsed."""
    profile = token_profile("\n[concurrency]\nmax_projects = 6\nper_engine = { claude = 3, codex = 2 }\n")
    assert profile.concurrency == _profile.Concurrency(6, {"claude": 3, "codex": 2})
    assert profile.cap == 6 and profile.shared


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ("max_projects = 0", "max_projects must be an integer of at least 1"),
        ("max_projects = true", "max_projects must be an integer of at least 1"),
        ('max_projects = "4"', "max_projects must be an integer of at least 1"),
        ("max_projects = 2\nper_engine = { claude = 3 }", "per_engine claude must be an integer from 1 to"),
        ("per_engine = { claude = 0 }", "per_engine claude must be an integer from 1 to"),
        ("per_engine = { gemini = 1 }", "per_engine names 'gemini'"),
        ('per_engine = "claude"', "per_engine must be a table"),
        ("max_project = 3", "unknown key(s): max_project"),
    ],
)
def test_invalid_concurrency_is_refused_with_the_key_named(table, message):
    """An invalid `[concurrency]` table is refused with the offending key named."""
    with pytest.raises(_profile.ProfileError, match=re.escape(message)):
        token_profile(f"\n[concurrency]\n{table}\n")


def test_a_token_profile_without_slots_deploys_one_shared_environment():
    """A token profile without slots deploys one shared environment."""
    profile = token_profile()
    assert profile.slots == (_profile.SHARED_SLOT,)
    assert profile.tasks == (SHARED_TASK,)
    from flyte_agent_factory import _deploy

    assert _deploy.slot_envs(profile) == {"agent-shared", "agent-shared-sub", "factory-setup"}
    assert _deploy.slot_table(profile) == {"agent-shared": "agent-shared-sub"}


def test_the_default_is_four_projects_and_no_engine_cap():
    """The default is four projects and no per-engine cap."""
    profile = token_profile()
    assert profile.concurrency == _profile.Concurrency() == _profile.Concurrency(4, {})
    assert profile.cap == _profile.DEFAULT_MAX_PROJECTS == 4


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda t: t.replace('claude_token = "claude_token_x"', ""), "claude_token is required"),
        (lambda t: "cap = 2\n" + t, "cap needs \\[\\[slots\\]\\]"),
        (lambda t: t + '\n[planner]\nadmission = "planner"\n', "admission = planner needs \\[\\[slots\\]\\]"),
    ],
)
def test_what_a_shared_environment_cannot_do_is_refused(edit, message):
    """What a shared environment cannot do is refused."""
    with pytest.raises(_profile.ProfileError, match=message):
        _profile.parse(edit(token_text()))


def test_concurrency_with_slots_is_refused():
    """`[concurrency]` together with `[[slots]]` is refused."""
    with pytest.raises(_profile.ProfileError, match=r"\[concurrency\] is for a profile with"):
        _profile.parse(WORK + "\n[concurrency]\nmax_projects = 2\n")


def test_slot_profiles_are_unchanged():
    """Slot profiles are unchanged by the `[concurrency]` table."""
    profile = _profile.parse(WORK)
    assert not profile.shared and profile.concurrency is None
    assert [s.env for s in profile.slots] == ["agent-one", "agent-two"]
    assert profile.cap == 2


def test_the_example_profile_documents_the_table():
    """The example profile documents the `[concurrency]` table."""
    example = (TEST_PROFILE.parent.parent.parent / "profiles" / "example.toml").read_text()
    assert "[concurrency]" in example and "max_projects" in example


# --- admission -------------------------------------------------------------------------------


def test_a_slot_profile_still_admits_one_project_per_slot(flyte_world):
    """A slot profile still admits one project per slot."""
    results = [factory.launch_project("brief", n) for n in ("alpha", "bravo", "charlie")]
    assert [r["status"] for r in results] == ["started", "started", "queued"]
    assert _factory_base.cap() == 2


def test_launches_are_admitted_under_the_cap_and_queued_over_it(shared_world):
    """Launches are admitted under the cap and queued over it."""
    world = shared_world("\n[concurrency]\nmax_projects = 2\n")
    results = [factory.launch_project("brief", n) for n in ("alpha", "bravo", "charlie")]
    assert [r["status"] for r in results] == ["started", "started", "queued"]
    assert {r["slot"] for r in results[:2]} == {SHARED_TASK}  # both on the one environment
    assert [c["task"] for c in world.created] == [SHARED_TASK, SHARED_TASK]
    assert results[2]["position"] == 1


def test_the_default_cap_admits_four(shared_world):
    """The default cap admits four projects."""
    shared_world()
    results = [factory.launch_project("brief", f"p{i}") for i in range(5)]
    assert [r["status"] for r in results] == ["started"] * 4 + ["queued"]


def test_a_per_engine_cap_queues_only_that_engine(shared_world):
    """A per-engine cap queues only launches of that engine."""
    shared_world("\n[concurrency]\nmax_projects = 4\nper_engine = { claude = 1 }\n")
    codex = {"AUTHOR_ENGINE": "codex"}
    first = factory.launch_project("brief", "alpha")
    second = factory.launch_project("brief", "bravo")  # Claude again: over its cap
    third = factory.launch_project("brief", "charlie", policy=codex)  # Codex has no cap of its own
    assert [r["status"] for r in (first, second, third)] == ["started", "queued", "started"]


def test_the_profiles_author_engine_counts_toward_its_cap(shared_world):
    """The profile author's engine counts toward its cap."""
    world = shared_world("\n[concurrency]\nmax_projects = 4\nper_engine = { codex = 1 }\n")
    _factory_base.PROFILE = dataclasses.replace(_factory_base.PROFILE, policy={"COORDINATOR_ENGINE": "codex"})
    assert [factory.launch_project("brief", n)["status"] for n in ("alpha", "bravo")] == ["started", "queued"]
    assert world.created[0]["context"]["labels"][_factory_base.LABEL_ENGINE] == "codex"


def test_a_queued_launch_is_admitted_when_a_live_project_ends(shared_world):
    """A queued launch is admitted when a live project ends."""
    world = shared_world("\n[concurrency]\nmax_projects = 2\n")
    for name in ("alpha", "bravo", "charlie"):
        factory.launch_project("brief", name)
    assert json.loads(asyncio.run(factory.sweep.func())) == {"dispatched": []}  # still full
    ended = next(r for r in world.runs.values() if r.name.startswith("alpha-"))
    ended.phase = "SUCCEEDED"
    [run] = json.loads(asyncio.run(factory.sweep.func()))["dispatched"]
    assert run.startswith("charlie-1-0-")
    assert world.runs[run].task == SHARED_TASK


def test_an_engine_capped_launch_does_not_hold_up_the_one_behind_it(shared_world):
    """An engine-capped launch does not hold up the one behind it."""
    shared_world("\n[concurrency]\nmax_projects = 4\nper_engine = { claude = 1 }\n")
    factory.launch_project("brief", "alpha")
    assert factory.launch_project("brief", "bravo")["status"] == "queued"
    _queue.enqueue(
        _factory_base.queue_root(),
        request("charlie", at=1e12, policy={"AGENT_FACTORY_AUTHOR_ENGINE": "codex"}),
    )
    started = factory.dispatch()
    assert [r.split("-")[0] for r in started] == ["charlie"]


# --- racing dispatchers ----------------------------------------------------------------------


def test_two_racing_launches_at_one_below_the_cap_leave_exactly_one_and_requeue_the_other(shared_world):
    """Two racing launches one below the cap leave exactly one running and requeue the other."""
    world = shared_world("\n[concurrency]\nmax_projects = 2\n")
    world.add("holder", SHARED_TASK)  # one below the cap
    queue = _factory_base.queue_root()
    for name, at in (("alpha", 1.0), ("bravo", 2.0)):
        _queue.enqueue(queue, request(name, at=at))
    first, second = _queue.pending(queue)

    # The other dispatcher listed the same room, and creates its run just before this one does.
    def rival_creates_first():
        assert [r.split("-")[0] for r in factory._dispatch_pending(queue, [second], "")] == ["bravo"]

    world.before_create = rival_creates_first
    assert factory._dispatch_pending(queue, [first], "") == []

    live = [r for r in world.runs.values() if r.phase == "RUNNING"]
    assert sorted(r.name.split("-")[0] for r in live) == ["bravo", "holder"]
    [yielded] = [r for r in world.runs.values() if r.phase == "ABORTED"]
    assert yielded.name.startswith("alpha-")
    assert [e.name for e in _queue.pending(queue)] == ["alpha"], "the loser waits for a later sweep"
    assert _queue.pending(queue)[0].attempt == 1


def test_a_per_engine_cap_holds_when_the_create_response_carries_no_labels(shared_world, monkeypatch):
    """A per-engine cap holds when the create response carries no labels."""
    world = shared_world("\n[concurrency]\nmax_projects = 4\nper_engine = { codex = 1 }\n")
    codex = {"AGENT_FACTORY_AUTHOR_ENGINE": "codex"}
    for name, at in (("alpha", 1.0), ("bravo", 2.0)):
        _queue.enqueue(_factory_base.queue_root(), request(name, at=at, policy=codex))
    real = factory.run_project

    class Unlabelled:
        """What `CreateRun` returns: the run, but not the labels the listing shows later."""

        def __init__(self, run: Any) -> None:
            self._run = run
            self.pb2 = type("Pb2", (), {"labels": {}})()

        def __getattr__(self, name):
            return getattr(self._run, name)

    monkeypatch.setattr(factory, "run_project", lambda *args, **kwargs: Unlabelled(real(*args, **kwargs)))
    started = factory.dispatch()
    assert [r.split("-")[0] for r in started] == ["alpha"]
    assert [c["context"]["name"].split("-")[0] for c in world.created] == ["alpha"], "bravo was not created"
