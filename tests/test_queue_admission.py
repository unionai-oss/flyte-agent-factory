"""Queue admission: a hold that does not apply to `low` work does not delay it; every entry says why."""

import asyncio
import dataclasses
import json
from typing import cast

import flyte.errors
import pytest
from obstore.exceptions import GenericError

from flyte_agent_factory import _factory as factory
from flyte_agent_factory import _factory_base, _planner, _queue
from tests.test_factory import projects

ENGINE = "git@example.org:team/engine.git"
SANDBOX = "git@example.org:team/sandbox.git"


@pytest.fixture
def lane(monkeypatch):
    """The engine repository is configured; one project on it at a time."""
    profile = _factory_base.PROFILE
    monkeypatch.setattr(
        _factory_base,
        "PROFILE",
        dataclasses.replace(profile, repos={**profile.repos, "factory": ENGINE}, factory_projects=1),
    )


def enqueue(world, name, at, priority="normal", repo=SANDBOX, kind="new", policy=None):
    request = _queue.Request(name, 1, kind, "b", "opus", repo, "", policy or {}, at, priority=priority)
    _queue.enqueue(world.queue, request)


def rows():
    return {row["name"]: row for row in factory.queue_view()["queue"]}


def occupy_lane(world, slot=0):
    world.add("engine-run", _factory_base.SLOTS[slot], inputs={"repo": ENGINE})


def test_a_lane_capped_normal_launch_does_not_hold_a_low_sandbox_launch(flyte_world, lane):
    occupy_lane(flyte_world)
    enqueue(flyte_world, "engine-work", 1.0, repo=ENGINE)
    enqueue(flyte_world, "sandbox", 2.0, "low")
    assert projects(flyte_world, factory.dispatch()) == ["sandbox"]
    assert [e.name for e in _queue.pending(flyte_world.queue)] == ["engine-work"]


def test_the_freed_lane_goes_to_the_normal_engine_launch_before_low_work(flyte_world, lane):
    occupy_lane(flyte_world)
    flyte_world.add(
        "other", _factory_base.SLOTS[1]
    )  # occupies the second slot; the freed one is the only free
    enqueue(flyte_world, "engine-work", 2.0, repo=ENGINE)
    enqueue(flyte_world, "sandbox", 1.0, "low")
    flyte_world.runs["engine-run"].phase = "SUCCEEDED"
    assert projects(flyte_world, factory.dispatch()) == ["engine-work"]
    assert [e.name for e in _queue.pending(flyte_world.queue)] == ["sandbox"]


def test_a_normal_resume_starts_before_a_low_launch_that_arrived_later(flyte_world):
    """Passes on main; the #121 incident reproduction is the slot-specific reserve test."""
    flyte_world.add("one", _factory_base.SLOTS[0])
    enqueue(flyte_world, "resumed", 1.0, kind="resume")
    enqueue(flyte_world, "garden", 2.0, "low")
    assert projects(flyte_world, factory.dispatch()) == ["resumed"]
    assert [e.name for e in _queue.pending(flyte_world.queue)] == ["garden"]


def test_a_resume_waiting_for_its_own_run_does_not_hold_low_work(flyte_world):
    flyte_world.add("old", _factory_base.SLOTS[0], labels={_factory_base.LABEL_PROJECT: "resumed"})
    enqueue(flyte_world, "resumed", 1.0, kind="resume")
    enqueue(flyte_world, "garden", 2.0, "low")
    assert projects(flyte_world, factory.dispatch()) == ["garden"]


def test_a_real_resume_queued_after_a_low_launch_takes_the_slot_that_frees(flyte_world):
    """Passes on main; the #121 incident reproduction is the slot-specific reserve test."""
    # `proj` ran gen 1 and ended; its gen 2 resume queues while every slot is busy, after a low launch.
    first = factory.launch_project("brief", "proj")
    flyte_world.runs[first["run"]].phase = "SUCCEEDED"
    flyte_world.add("one", _factory_base.SLOTS[0])
    flyte_world.add("two", _factory_base.SLOTS[1])
    enqueue(flyte_world, "garden", 0.5, "low")
    assert factory.launch_project("more", "proj")["status"] == "queued"
    assert factory.dispatch() == []
    flyte_world.runs["one"].phase = "SUCCEEDED"
    assert projects(flyte_world, factory.dispatch()) == ["proj"]
    assert [e.name for e in _queue.pending(flyte_world.queue)] == ["garden"]


def test_a_resume_keeps_the_slot_its_own_ending_run_hands_over_from_low_work(flyte_world):
    # The ending run dispatches with its own run excluded: its slot is free, but only for its resume.
    flyte_world.add("old", _factory_base.SLOTS[0], labels={_factory_base.LABEL_PROJECT: "resumed"})
    flyte_world.add("busy", _factory_base.SLOTS[1])
    enqueue(flyte_world, "resumed", 1.0, kind="resume")
    enqueue(flyte_world, "garden", 2.0, "low")
    assert factory.dispatch(exclude_run="old") == []
    assert rows()["resumed"]["wait"]["code"] == "project_running"
    flyte_world.runs["old"].phase = "SUCCEEDED"
    assert projects(flyte_world, factory.dispatch()) == ["resumed"]


@pytest.mark.parametrize(
    "error", [flyte.errors.RuntimeUserError("InvalidArgumentError", "bad input"), ValueError("bad request")]
)
def test_a_launch_that_fails_for_good_does_not_hold_low_work(flyte_world, error):
    enqueue(flyte_world, "bad", 1.0)
    enqueue(flyte_world, "garden", 2.0, "low")
    flyte_world.fail_create = [error]
    assert projects(flyte_world, factory.dispatch()) == ["garden"]
    assert _queue.pending(flyte_world.queue) == []


def test_expedite_goes_first_even_when_a_lane_capped_normal_launch_waits(flyte_world, lane):
    occupy_lane(flyte_world)
    enqueue(flyte_world, "engine-work", 1.0, repo=ENGINE)
    enqueue(flyte_world, "sandbox", 2.0)
    enqueue(flyte_world, "urgent", 3.0, "expedite")
    assert projects(flyte_world, factory.dispatch()) == ["urgent"]


def test_every_waiting_entry_says_why_in_the_queue_json(flyte_world, lane):
    occupy_lane(flyte_world)
    enqueue(flyte_world, "engine-work", 1.0, repo=ENGINE)
    enqueue(flyte_world, "sandbox", 2.0, "low")
    assert projects(flyte_world, factory.dispatch()) == ["sandbox"]
    listed = json.loads(json.dumps(factory.queue_view()))["queue"]
    assert listed and all(row["wait_reason"] for row in listed)
    by_name = {row["name"]: row for row in listed}
    assert by_name["engine-work"]["wait"]["code"] == "engine_busy"
    assert by_name["engine-work"]["wait"]["holders"] == ["engine-run"]
    assert by_name["engine-work"]["wait"]["limit"] == 1
    assert "engine-run" in by_name["engine-work"]["wait_reason"]


def test_a_low_entry_behind_runnable_work_says_how_many(flyte_world):
    flyte_world.add("one", _factory_base.SLOTS[0])
    flyte_world.add("two", _factory_base.SLOTS[1])
    enqueue(flyte_world, "work-a", 1.0)
    enqueue(flyte_world, "work-b", 2.0)
    enqueue(flyte_world, "garden", 3.0, "low")
    assert factory.dispatch() == []
    view = rows()
    assert view["garden"]["wait"] == {"code": "priority_behind", "behind": 2}
    assert "2" in view["garden"]["wait_reason"]
    assert view["work-a"]["wait"]["code"] == "no_free_slot"


def test_a_resume_whose_project_runs_says_so(flyte_world):
    flyte_world.add("old", _factory_base.SLOTS[0], labels={_factory_base.LABEL_PROJECT: "resumed"})
    enqueue(flyte_world, "resumed", 1.0, kind="resume")
    factory.dispatch()
    assert rows()["resumed"]["wait"]["code"] == "project_running"


def test_an_entry_never_swept_still_has_a_reason(flyte_world):
    enqueue(flyte_world, "fresh", 1.0)
    assert rows()["fresh"]["wait_reason"]


# --- the planner path

NOW = 1_000.0
PLANNER_ENGINE = "team/engine"
PLANNER_SANDBOX = "team/sandbox"


def manual(name, priority="normal", repo=PLANNER_SANDBOX):
    return _planner.ManualRequest(name, repo, priority=priority, files=(f"{name}.py",))


def facts(*requests, holders=1):
    runs = (
        (_planner.ActiveRun("engine-run", PLANNER_ENGINE, predicted_files=("elsewhere.py",)),)
        if holders
        else ()
    )
    return _planner.Facts(
        capacity=(_planner.Capacity("claude", "login", slot="slot"),),
        manual_requests=tuple(requests),
        active_runs=runs,
        engine_repo=PLANNER_ENGINE,
        engine_projects=1,
        now=NOW,
    )


def test_the_planner_starts_a_low_request_when_the_normal_one_is_lane_capped():
    result = _planner.plan(facts(manual("engine-work", repo=PLANNER_ENGINE), manual("sandbox", "low")))
    assert [cast(_planner.ManualRequest, launch.item).name for launch in result.launches] == ["sandbox"]
    assert [(cast(_planner.ManualRequest, w.item).name, w.reason.code) for w in result.waiting] == [
        ("engine-work", "engine_busy")
    ]


def test_the_planner_gives_the_freed_lane_to_the_normal_request_before_low_work():
    result = _planner.plan(
        facts(manual("sandbox", "low"), manual("engine-work", repo=PLANNER_ENGINE), holders=0)
    )
    assert [cast(_planner.ManualRequest, launch.item).name for launch in result.launches] == ["engine-work"]
    assert [cast(_planner.ManualRequest, w.item).name for w in result.waiting] == [
        "sandbox"
    ]  # the one slot went to the engine launch


def test_the_planner_orders_expedite_before_a_lane_capped_normal_request():
    result = _planner.plan(
        facts(
            manual("engine-work", repo=PLANNER_ENGINE), manual("urgent", "expedite"), manual("sandbox", "low")
        )
    )
    assert [cast(_planner.ManualRequest, launch.item).name for launch in result.launches] == ["urgent"]


def test_the_planner_run_leaves_its_reasons_for_the_queue_list(flyte_world):
    from flyte_agent_factory import _planner_runtime

    for name, priority in (("work", "normal"), ("sandbox", "low")):
        enqueue(flyte_world, name, 1.0, priority)
    # One slot, and the normal request is not held by anything: the low one waits behind it.
    plan = _planner.plan(facts(manual("work"), manual("work-2"), manual("sandbox", "low"), holders=0))
    requests = {e.name: e.request for e in _queue.pending(flyte_world.queue) if e.request}
    _planner_runtime.record_waits(plan, requests)
    listed = rows()
    assert listed["sandbox"]["wait"]["code"] == "low_priority_waits"
    assert listed["sandbox"]["wait"]["behind"] == 1
    assert "behind 1" in listed["sandbox"]["wait_reason"]


def test_the_planner_lane_hold_lists_its_holders_and_limit_in_the_queue_json(flyte_world):
    from flyte_agent_factory import _planner_runtime

    enqueue(flyte_world, "engine-work", 1.0, repo=ENGINE)
    plan = _planner.plan(facts(manual("engine-work", repo=PLANNER_ENGINE)))
    requests = {e.name: e.request for e in _queue.pending(flyte_world.queue) if e.request}
    _planner_runtime.record_waits(plan, requests)
    row = json.loads(json.dumps(factory.queue_view()))["queue"][0]
    assert row["wait"] == {"code": "engine_busy", "holders": ["engine-run"], "limit": 1}
    assert "engine-run" in row["wait_reason"]


def test_the_planner_project_hold_names_the_live_run_as_the_dispatcher_does(flyte_world):
    from flyte_agent_factory import _planner_runtime

    enqueue(flyte_world, "resumed", 1.0, kind="resume")
    old = _planner.ActiveRun("old", PLANNER_SANDBOX, labels={_planner.LABEL_PROJECT: "resumed"})
    plan = _planner.plan(dataclasses.replace(facts(manual("resumed"), holders=0), active_runs=(old,)))
    requests = {e.name: e.request for e in _queue.pending(flyte_world.queue) if e.request}
    _planner_runtime.record_waits(plan, requests)
    assert rows()["resumed"]["wait"] == {"code": "project_running", "holders": ["old"]}


def limit_headroom(monkeypatch, limited):
    """The usage reserve (or a refusal) leaves the slots in `limited` without headroom."""
    from flyte_agent_factory import _factory_meters

    monkeypatch.setattr(
        _factory_meters,
        "slot_headroom",
        lambda slots, **_: {s: _factory_meters.LIMITED if s in limited else None for s in slots},
    )


def test_a_reserve_hold_on_every_free_slot_is_listed_and_still_holds_low_work(flyte_world, monkeypatch):
    limit_headroom(monkeypatch, set(_factory_base.SLOTS))
    enqueue(flyte_world, "work", 1.0)
    enqueue(flyte_world, "garden", 2.0, "low")
    assert factory.dispatch() == []
    view = rows()
    assert view["work"]["wait"]["code"] == "no_capacity"
    assert "reserve" in view["work"]["wait_reason"]
    assert view["garden"]["wait"] == {"code": "priority_behind", "behind": 1}


def test_a_reserve_hold_on_the_only_slot_a_request_may_use_does_not_hold_low_work(flyte_world, monkeypatch):
    # The normal request runs on Codex, whose one login is on the first slot, and that slot is out of
    # headroom; the second slot is free and has room, so the low request takes it.
    codex_slot = _factory_base.SLOTS[0]
    profile = _factory_base.PROFILE
    slots = tuple(
        dataclasses.replace(s, codex_login="codex-login" if s.task == codex_slot else "")
        for s in profile.slots
    )
    monkeypatch.setattr(
        _factory_base,
        "PROFILE",
        dataclasses.replace(
            profile, slots=slots, secrets=dataclasses.replace(profile.secrets, codex_token="")
        ),
    )
    from flyte_agent_factory import _factory_policy

    monkeypatch.setattr(_factory_policy, "_has_token", lambda engine: True)
    limit_headroom(monkeypatch, {codex_slot})
    enqueue(flyte_world, "codex-work", 1.0, policy={"AGENT_FACTORY_AUTHOR_ENGINE": "codex"})
    enqueue(flyte_world, "garden", 2.0, "low")
    assert projects(flyte_world, factory.dispatch()) == ["garden"]
    assert rows()["codex-work"]["wait"]["code"] == "no_capacity"


def fail_waits_store(monkeypatch):
    """Storing the wait reasons fails as the object store does: obstore's error, which is not an OSError."""
    real = _queue.write_json

    def write_json(path, value):
        if path.endswith("waits.json"):
            raise GenericError("store unavailable")
        real(path, value)

    monkeypatch.setattr(_queue, "write_json", write_json)


def test_a_failed_store_of_the_reasons_does_not_stop_a_dispatch(flyte_world, monkeypatch):
    fail_waits_store(monkeypatch)
    enqueue(flyte_world, "a", 1.0)
    assert projects(flyte_world, factory.dispatch()) == ["a"]


def test_a_failed_store_of_the_reasons_does_not_stop_the_planner(flyte_world, monkeypatch):
    from flyte_agent_factory import _planner_runtime

    fail_waits_store(monkeypatch)
    enqueue(flyte_world, "work", 1.0)
    requests = {e.name: e.request for e in _queue.pending(flyte_world.queue) if e.request}
    _planner_runtime.record_waits(_planner.plan(facts(manual("work"), holders=0)), requests)


def test_launches_behind_a_retried_launch_say_they_wait_for_it_not_for_a_slot(flyte_world):
    enqueue(flyte_world, "first", 1.0)
    enqueue(flyte_world, "second", 2.0)
    flyte_world.fail_create = [flyte.errors.RuntimeSystemError("SystemUnavailableError", "down")]
    assert factory.dispatch() == []
    view = rows()
    assert view["first"]["wait"]["code"] == "launch_retry"
    assert view["second"]["wait"] == {"code": "behind_retry", "holders": ["first"]}


def test_an_engine_limited_launch_says_so_and_does_not_hold_low_work(flyte_world, monkeypatch):
    from tests.test_concurrency import token_profile

    profile = token_profile("\n[concurrency]\nmax_projects = 4\nper_engine = { claude = 1 }\n")
    monkeypatch.setattr(
        _factory_base, "PROFILE", dataclasses.replace(profile, state=_factory_base.PROFILE.state)
    )
    monkeypatch.setattr(_factory_base, "SLOTS", profile.tasks)
    factory.launch_project("brief", "alpha")
    enqueue(flyte_world, "bravo", 1.0)
    enqueue(flyte_world, "garden", 2.0, "low", policy={"AGENT_FACTORY_AUTHOR_ENGINE": "codex"})
    assert projects(flyte_world, factory.dispatch()) == ["garden"]
    listed = json.loads(json.dumps(factory.queue_view()))["queue"]
    assert [(row["name"], row["wait"]["code"]) for row in listed] == [("bravo", "engine_limit")]
    assert "claude" in listed[0]["wait_reason"]


def test_a_hand_over_keeps_only_its_own_slot_from_low_work(flyte_world):
    # The second slot is already free: `low` work takes it; the handed-over slot stays for the resume.
    flyte_world.add("old", _factory_base.SLOTS[0], labels={_factory_base.LABEL_PROJECT: "resumed"})
    enqueue(flyte_world, "resumed", 1.0, kind="resume")
    enqueue(flyte_world, "garden", 2.0, "low")
    [run] = factory.dispatch(exclude_run="old")
    assert flyte_world.runs[run].task == _factory_base.SLOTS[1]
    assert rows()["resumed"]["wait"]["code"] == "project_running"
    flyte_world.runs["old"].phase = "SUCCEEDED"
    assert projects(flyte_world, factory.dispatch()) == ["resumed"]


def test_a_hand_over_taken_by_another_normal_project_still_keeps_a_slot_from_low_work(flyte_world):
    # Another normal project takes the handed-over slot: the resume still needs one, so the last free slot is
    # kept from `low` work.
    flyte_world.add("old", _factory_base.SLOTS[0], labels={_factory_base.LABEL_PROJECT: "resumed"})
    enqueue(flyte_world, "resumed", 1.0, kind="resume")
    enqueue(flyte_world, "other", 1.5)
    enqueue(flyte_world, "garden", 2.0, "low")
    assert projects(flyte_world, factory.dispatch(exclude_run="old")) == ["other"]
    flyte_world.runs["old"].phase = "SUCCEEDED"
    assert projects(flyte_world, factory.dispatch()) == ["resumed"]


def test_a_hand_over_on_the_engine_repo_still_holds_the_lane_against_low_work(flyte_world, lane):
    # The ending run is on the engine repository: for `low` work it still holds the lane, so a low engine
    # launch neither exceeds the cap nor takes the lane before the resume.
    flyte_world.add(
        "old",
        _factory_base.SLOTS[0],
        labels={_factory_base.LABEL_PROJECT: "resumed"},
        inputs={"repo": ENGINE},
    )
    enqueue(flyte_world, "resumed", 1.0, kind="resume", repo=ENGINE)
    enqueue(flyte_world, "low-eng", 2.0, "low", repo=ENGINE)
    assert factory.dispatch(exclude_run="old") == []
    assert rows()["low-eng"]["wait"] == {"code": "engine_busy", "holders": ["old"], "limit": 1}
    flyte_world.runs["old"].phase = "SUCCEEDED"
    assert projects(flyte_world, factory.dispatch()) == ["resumed"]


def test_a_launch_cancelled_after_its_create_does_not_hold_low_work(flyte_world):
    enqueue(flyte_world, "alpha", 1.0)
    enqueue(flyte_world, "garden", 2.0, "low")
    flyte_world.after_create = lambda: factory.cancel("alpha")
    assert projects(flyte_world, factory.dispatch()) == ["garden"]


def test_a_launch_cancelled_after_its_dedupe_does_not_hold_low_work(flyte_world, monkeypatch):
    from flyte_agent_factory import _factory_recreate

    real = _factory_recreate._keep_oldest

    def keep_oldest(entry, run):  # a cancel lands after the dedupe, before the dispatcher's last look
        keeper = real(entry, run)
        if entry.name == "alpha":
            factory.cancel("alpha")
        return keeper

    monkeypatch.setattr(_factory_recreate, "_keep_oldest", keep_oldest)
    enqueue(flyte_world, "alpha", 1.0)
    enqueue(flyte_world, "garden", 2.0, "low")
    assert projects(flyte_world, factory.dispatch()) == ["garden"]


def test_a_launch_that_lost_to_an_older_twin_does_not_hold_low_work(flyte_world, monkeypatch):
    from flyte_agent_factory import _factory_recreate

    real = _factory_recreate._keep_oldest

    def keep_oldest(entry, run):  # another dispatcher's run of the same request is older
        if entry.name != "alpha":
            return real(entry, run)
        twin = flyte_world.add(
            "older-twin",
            _factory_base.SLOTS[1],
            labels={_factory_base.LABEL_PROJECT: entry.name, _factory_base.LABEL_GEN: str(entry.gen)},
        )
        run.abort(reason=_factory_base.DUPLICATE_REASON)
        return twin

    monkeypatch.setattr(_factory_recreate, "_keep_oldest", keep_oldest)
    enqueue(flyte_world, "alpha", 1.0)
    enqueue(flyte_world, "garden", 2.0, "low")
    [run] = factory.dispatch()
    assert flyte_world.runs[run].task == _factory_base.SLOTS[0] and run.startswith("garden")
    assert flyte_world.runs[run].phase == "RUNNING"


def test_a_launch_that_started_before_its_error_keeps_its_slot(flyte_world, monkeypatch):
    real = factory.launch_on_slot

    def launch_on_slot(slot, brief, name, *args, **kwargs):
        run = real(slot, brief, name, *args, **kwargs)
        if name == "alpha":
            raise ValueError("unreadable response")  # after the run exists: the request did start
        return run

    monkeypatch.setattr(factory, "launch_on_slot", launch_on_slot)
    enqueue(flyte_world, "alpha", 1.0)
    enqueue(flyte_world, "garden", 2.0, "low")
    alpha, run = factory.dispatch()  # alpha did start: it is reported, and counted, like any start
    assert flyte_world.runs[alpha].task == _factory_base.SLOTS[0]
    assert run.startswith("garden") and flyte_world.runs[run].phase == "RUNNING"
    assert flyte_world.runs[run].task == _factory_base.SLOTS[1]
    assert _queue.pending(flyte_world.queue) == []


def test_a_launch_that_started_before_its_error_still_counts_toward_the_lane(flyte_world, monkeypatch, lane):
    real = factory.launch_on_slot

    def launch_on_slot(slot, brief, name, *args, **kwargs):
        run = real(slot, brief, name, *args, **kwargs)
        if name == "eng-a":
            raise ValueError("unreadable response")  # after the run exists: the request did start
        return run

    monkeypatch.setattr(factory, "launch_on_slot", launch_on_slot)
    enqueue(flyte_world, "eng-a", 1.0, repo=ENGINE)
    enqueue(flyte_world, "eng-b", 2.0, repo=ENGINE)
    factory.dispatch()
    assert [e.name for e in _queue.pending(flyte_world.queue)] == ["eng-b"]
    assert rows()["eng-b"]["wait"]["code"] == "engine_busy"


def test_the_oldest_low_launch_takes_the_slot_a_lane_capped_launch_cannot_use(flyte_world, lane):
    occupy_lane(flyte_world)
    enqueue(flyte_world, "engine-work", 1.0, repo=ENGINE)
    enqueue(flyte_world, "a", 2.0, "low")
    enqueue(flyte_world, "b", 3.0, "low")
    assert projects(flyte_world, factory.dispatch()) == ["a"]
    assert [e.name for e in _queue.pending(flyte_world.queue)] == ["engine-work", "b"]
    assert rows()["b"]["wait"]["code"] == "no_free_slot"


def test_the_oldest_normal_launch_takes_the_slot_a_lane_capped_launch_cannot_use(flyte_world, lane):
    """Passes on main: the order rule among same-priority launches is unchanged; this is coverage."""
    occupy_lane(flyte_world)
    enqueue(flyte_world, "engine-work", 1.0, repo=ENGINE)
    enqueue(flyte_world, "a", 2.0)
    enqueue(flyte_world, "b", 3.0)
    assert projects(flyte_world, factory.dispatch()) == ["a"]
    assert [e.name for e in _queue.pending(flyte_world.queue)] == ["engine-work", "b"]


def test_a_launch_with_no_codex_slot_says_so_and_does_not_hold_low_work(flyte_world, monkeypatch):
    from flyte_agent_factory import _factory_policy

    monkeypatch.setattr(_factory_policy, "_has_token", lambda engine: True)
    monkeypatch.setattr(type(_factory_base.PROFILE), "codex_secret", lambda self, slot: "")
    enqueue(flyte_world, "stuck", 1.0, policy={"AGENT_FACTORY_AUTHOR_ENGINE": "codex"})
    enqueue(flyte_world, "garden", 2.0, "low")
    assert projects(flyte_world, factory.dispatch()) == ["garden"]
    assert rows()["stuck"]["wait"] == {"code": "no_codex_slot"}


def test_a_loop_launch_out_of_budget_says_so_and_does_not_hold_low_work(flyte_world, monkeypatch):
    from flyte_agent_factory import _factory_meters, _loop

    profile = _factory_base.PROFILE
    loop = dataclasses.replace(profile.loop, repo="acme/factory")
    monkeypatch.setattr(_factory_base, "PROFILE", dataclasses.replace(profile, loop=loop))
    monkeypatch.setattr(_factory_meters, "_loop_slot_budget", lambda *_, **__: _loop.Budget("wait"))
    request = _queue.Request(
        "stuck", 1, "new", "<!-- loop-source: qa:example -->", "opus", SANDBOX, "", {}, 1.0
    )
    _queue.enqueue(flyte_world.queue, dataclasses.replace(request, issue="acme/factory#42"))
    enqueue(flyte_world, "garden", 2.0, "low")
    assert projects(flyte_world, factory.dispatch()) == ["garden"]
    assert rows()["stuck"]["wait"] == {"code": "loop_budget"}


def test_a_reason_stored_for_another_generation_is_not_shown(flyte_world):
    request = _queue.Request("proj", 2, "resume", "b", "opus", SANDBOX, "", {}, 1.0)
    _queue.enqueue(flyte_world.queue, request)
    stale = {"proj": {"gen": 1, "reason": "engine_busy: x", "wait": {"code": "engine_busy"}}}
    _queue.write_waits(_factory_base.PROFILE.state.uri, stale)
    assert rows()["proj"]["wait"]["code"] == "awaiting_admission"


def test_a_malformed_stored_reason_does_not_break_the_listing(flyte_world):
    enqueue(flyte_world, "a", 1.0)
    _queue.write_waits(_factory_base.PROFILE.state.uri, {"a": {"gen": 1}})
    assert rows()["a"]["wait"]["code"] == "awaiting_admission"


@pytest.mark.parametrize(
    "stored",
    [
        {"gen": 1, "reason": "engine_busy: old", "wait": {}},
        {"gen": 1, "reason": "engine_busy: old", "wait": {"code": None}},
        {"gen": 1, "reason": "engine_busy: old", "wait": {"code": ""}},
        {"gen": 1, "reason": 123, "wait": {"code": "engine_busy"}},
        {"gen": 1, "reason": "", "wait": {"code": "engine_busy"}},
        "engine_busy",
    ],
)
def test_a_stored_reason_without_text_and_a_code_falls_back(flyte_world, stored):
    enqueue(flyte_world, "a", 1.0)
    _queue.write_waits(_factory_base.PROFILE.state.uri, {"a": stored})
    row = rows()["a"]
    assert row["wait"] == {"code": "awaiting_admission"}
    assert row["wait_reason"].startswith("not yet judged")


def test_an_unreadable_request_says_why_it_waits(flyte_world):
    from tests.test_factory import garbage_request

    garbage_request(flyte_world, "bad")
    [row] = factory.queue_view()["queue"]
    assert row["wait"] == {"code": "unreadable_request"}
    assert row["wait_reason"].startswith("unreadable request")


@pytest.fixture
def concurrent_twin(flyte_world, monkeypatch):
    from flyte import remote

    from flyte_agent_factory import _factory_recreate

    def arrange(slot, repo=ENGINE, *, known=False, engine="claude"):
        labels = {_factory_base.LABEL_PROJECT: "alpha", _factory_base.LABEL_GEN: "1"}
        twin = flyte_world.add(
            "older-twin", slot, labels={_factory_base.LABEL_ENGINE: engine}, inputs={"repo": repo}
        )
        visible = known
        listall = remote.Run.listall
        keep_oldest = _factory_recreate._keep_oldest

        def list_runs(**kwargs):
            return [r for r in listall(**kwargs) if visible or r.name != twin.name]

        def dedupe(entry, run):
            nonlocal visible
            visible = True
            twin.pb2.labels.update(labels)
            return keep_oldest(entry, run)

        monkeypatch.setattr(remote.Run, "listall", list_runs)
        monkeypatch.setattr(_factory_recreate, "_keep_oldest", dedupe)
        return twin

    return arrange


def test_a_surviving_twin_holds_the_engine_lane(flyte_world, lane, concurrent_twin):
    twin = concurrent_twin(_factory_base.SLOTS[1])
    enqueue(flyte_world, "alpha", 1.0, repo=ENGINE)
    enqueue(flyte_world, "garden", 2.0, "low", repo=ENGINE)
    assert factory.dispatch() == []
    assert twin.phase == "RUNNING"
    assert len(flyte_world.created) == 1
    assert flyte_world.runs[flyte_world.created[0]["name"]].phase == "ABORTED"
    assert rows()["garden"]["wait"] == {"code": "engine_busy", "holders": [twin.name], "limit": 1}


@pytest.mark.parametrize("twin_slot", [0, 1])
def test_a_surviving_twin_holds_only_its_actual_slot(flyte_world, lane, concurrent_twin, twin_slot):
    twin = concurrent_twin(_factory_base.SLOTS[twin_slot])
    enqueue(flyte_world, "alpha", 1.0, repo=ENGINE)
    enqueue(flyte_world, "garden", 2.0, "low")
    [run] = factory.dispatch()
    assert run.startswith("garden") and flyte_world.runs[run].phase == "RUNNING"
    assert flyte_world.runs[run].task == _factory_base.SLOTS[1 - twin_slot]
    assert twin.phase == "RUNNING"
    assert _queue.marker(flyte_world.queue, "alpha", 1, "started")["slot"] == twin.task


def test_a_known_surviving_twin_is_not_counted_twice(flyte_world, monkeypatch, lane, concurrent_twin):
    profile = _factory_base.PROFILE
    monkeypatch.setattr(_factory_base, "PROFILE", dataclasses.replace(profile, factory_projects=2))
    twin = concurrent_twin(_factory_base.SLOTS[1], known=True)
    enqueue(flyte_world, "alpha", 1.0, repo=ENGINE)
    enqueue(flyte_world, "garden", 2.0, "low", repo=ENGINE)
    assert projects(flyte_world, factory.dispatch()) == ["garden"]
    assert twin.phase == "RUNNING"
    assert _queue.pending(flyte_world.queue) == []


def test_a_surviving_twin_counts_against_its_actual_engine(flyte_world, monkeypatch, concurrent_twin):
    from tests.test_concurrency import token_profile

    profile = token_profile("\n[concurrency]\nmax_projects = 3\nper_engine = { claude = 1 }\n")
    monkeypatch.setattr(
        _factory_base, "PROFILE", dataclasses.replace(profile, state=_factory_base.PROFILE.state)
    )
    monkeypatch.setattr(_factory_base, "SLOTS", profile.tasks)
    twin = concurrent_twin(profile.tasks[0], repo=SANDBOX, engine="claude")
    enqueue(flyte_world, "alpha", 1.0, policy={"AGENT_FACTORY_AUTHOR_ENGINE": "codex"})
    enqueue(flyte_world, "garden", 2.0, "low")
    assert factory.dispatch() == []
    assert len(flyte_world.created) == 1
    assert twin.phase == "RUNNING"
    assert rows()["garden"]["wait"]["code"] == "engine_limit"


def test_an_unreadable_wait_cache_leaves_queue_reasons_available(flyte_world, monkeypatch, capsys):
    enqueue(flyte_world, "work", 1.0)
    fs = _queue._fs(_factory_base.PROFILE.state.uri)
    cat_file = fs.cat_file

    def read(path):
        if path.endswith("/waits.json"):
            raise GenericError("store unavailable")
        return cat_file(path)

    monkeypatch.setattr(fs, "cat_file", read)
    [row] = json.loads(json.dumps(factory.queue_view()))["queue"]
    assert row["name"] == "work"
    assert row["wait"] == {"code": "awaiting_admission"}
    assert row["wait_reason"]
    assert "wait reasons" in capsys.readouterr().out


@pytest.mark.parametrize("error", [TypeError("unexpected"), asyncio.CancelledError()])
def test_wait_cache_unexpected_errors_and_cancellation_propagate(flyte_world, monkeypatch, error):
    enqueue(flyte_world, "work", 1.0)
    read_json = _queue.read_json

    def read(path):
        if path.endswith("/waits.json"):
            raise error
        return read_json(path)

    monkeypatch.setattr(_queue, "read_json", read)
    with pytest.raises(type(error)):
        factory.queue_view()


def test_an_unreadable_queue_request_still_raises(flyte_world, monkeypatch):
    enqueue(flyte_world, "work", 1.0)
    fs = _queue._fs(_factory_base.PROFILE.state.uri)
    cat_file = fs.cat_file

    def read(path):
        if path.endswith("/0001.json"):
            raise GenericError("queue unavailable")
        return cat_file(path)

    monkeypatch.setattr(fs, "cat_file", read)
    with pytest.raises(GenericError, match="queue unavailable"):
        factory.queue_view()
