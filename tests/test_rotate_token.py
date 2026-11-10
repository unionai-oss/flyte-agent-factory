"""`factory.rotate_token`: a token change reaches running work by stopping each live project with a checkpoint
and resuming it, in the same lineage, on a new pod; and a resume never starts beside its project's old run."""

import asyncio
import dataclasses
import json
from types import SimpleNamespace
from typing import Any

import pytest
from flyte import remote

from flyte_agent_factory import _factory as factory
from flyte_agent_factory import _factory_base, _queue
from ops import operate
from tests.test_concurrency import token_profile
from tests.test_factory import add_labelled, projects
from tests.test_queue import request as _request

# `tests.test_queue.request` is untyped; calling it through `Any` keeps that out of this file's typed calls.
request: Any = _request


@pytest.fixture
def conditions(monkeypatch):
    """Serve open `direct-` conditions by run name from a fake `Condition.listall`; record the sends."""
    world = SimpleNamespace(open={}, signals=[])

    def condition(run_name, name):
        def signal(payload):
            world.signals.append((run_name, name, payload))
            world.open[run_name].remove(name)  # an answered condition is no longer PAUSED

        return SimpleNamespace(name=name, phase="PAUSED", signal=signal)

    def listall(*, run_name):
        return [condition(run_name, name) for name in list(world.open.get(run_name, []))]

    monkeypatch.setattr(remote.Condition, "listall", listall)
    return world


def launch_live(world, conditions, *names):
    """Launch each project through the queue and give its run an open `direct-` condition."""
    runs = {}
    for name in names:
        factory.launch_project(f"build {name}", name)
        runs[name] = world.runs[_queue.marker(world.queue, name, 1, "started")["run"]]
        conditions.open[runs[name].name] = ["ask-a0-001", "direct-a0-001"]
    return runs


def pending(world, name=None):
    return [e for e in _queue.pending(world.queue, name) if e.request is not None]


def test_each_live_project_is_stopped_once_and_resumed_once_in_its_own_lineage(flyte_world, conditions):
    runs = launch_live(flyte_world, conditions, "alpha", "beta")
    created = list(flyte_world.created)

    result = factory.cycle_projects()

    assert result["status"] == "rotating"
    assert sorted(conditions.signals) == sorted(
        (run.name, "direct-a0-001", "/stop") for run in runs.values()
    )  # the direct- inbox only: the ask- question is not answered
    waiting = pending(flyte_world)
    assert sorted((e.name, e.gen, e.request.kind) for e in waiting) == [
        ("alpha", 2, "resume"),
        ("beta", 2, "resume"),
    ]
    assert {e.request.reason for e in waiting} == {"token rotated"}
    assert all(e.request.base.startswith("build ") for e in waiting)  # the project's brief so far
    assert flyte_world.created == created  # no project was started; the resumes wait for the old runs
    assert {e.name for e in _queue.entries(flyte_world.queue)} == {"alpha", "beta"}  # no new project name


def test_a_second_call_while_the_resumes_wait_queues_nothing_more(flyte_world, conditions):
    launch_live(flyte_world, conditions, "alpha", "beta")
    factory.cycle_projects()
    before = [(e.name, e.gen) for e in pending(flyte_world)]

    again = factory.cycle_projects()

    assert [(e.name, e.gen) for e in pending(flyte_world)] == before
    assert {p["status"] for p in again["projects"]} == {"already queued"}


def test_the_reason_is_shown_in_the_queue_the_overview_and_the_status(flyte_world, conditions):
    run = launch_live(flyte_world, conditions, "alpha")["alpha"]
    factory.cycle_projects()

    [row] = factory.queue_view()["queue"]
    assert row["reason"] == "token rotated"
    assert row["waiting_for"] == "waiting for alpha's current run to finish"
    assert "token rotated" in operate.queue_lines([row])[0]
    view = operate.build_overview([run], queued=[row])
    assert "token rotated" in operate.overview_text(view)
    assert "token rotated" in operate.overview_html(view)
    assert factory.read_status("alpha")["queued"] == {
        "reason": "token rotated",
        "waiting_for": "waiting for alpha's current run to finish",
    }


def test_no_live_project_is_a_clear_nothing_to_rotate(flyte_world, conditions):
    result = factory.cycle_projects()
    assert result["status"] == "nothing to rotate"
    assert "no live project" in result["message"]
    assert result["projects"] == [] and pending(flyte_world) == []
    assert conditions.signals == []


def test_a_live_run_the_queue_did_not_start_is_reported_and_left_alone(flyte_world, conditions):
    flyte_world.add("legacy", _factory_base.SLOTS[0], labels={})
    conditions.open["legacy"] = ["direct-a0-001"]
    [skipped] = factory.cycle_projects()["projects"]
    assert skipped["status"] == "skipped" and "launched before the queue" in skipped["error"]
    assert conditions.signals == [] and pending(flyte_world) == []


def test_a_project_without_an_open_direct_condition_keeps_its_resume_and_says_the_stop_failed(
    flyte_world, conditions
):
    run = launch_live(flyte_world, conditions, "alpha")["alpha"]
    conditions.open[run.name] = []
    [row] = factory.cycle_projects()["projects"]
    assert row["status"] == "queued" and "no open direct- condition" in row["error"]
    conditions.open[run.name] = ["direct-a0-002"]
    factory.cycle_projects()  # the retry stops it without queueing a second resume
    assert conditions.signals == [(run.name, "direct-a0-002", "/stop")]
    assert len(pending(flyte_world)) == 1


def test_the_tool_is_a_chat_task_and_returns_json(flyte_world, conditions):
    from flyte_agent_factory import _factory_mcp

    assert "rotate_token" in _factory_mcp.CHAT_TASKS
    assert json.loads(asyncio.run(factory.rotate_token.func()))["status"] == "nothing to rotate"


# --- a request waits for its project's live run ---------------------------------------------------


def test_a_queued_request_waits_while_its_project_has_a_live_run_and_starts_when_it_succeeds(flyte_world):
    old = add_labelled(flyte_world, "alpha-1-0-aaaaaa", "alpha", _factory_base.SLOTS[0])
    _queue.enqueue(flyte_world.queue, request("alpha", gen=2, kind="resume"))

    assert factory.dispatch() == []  # a slot is free, but alpha is still running
    assert [e.name for e in pending(flyte_world)] == ["alpha"]

    old.phase, old.outputs_ = "SUCCEEDED", ("alpha: done", None)
    assert projects(flyte_world, factory.dispatch()) == ["alpha"]


def test_a_waiting_request_does_not_hold_back_the_others(flyte_world):
    add_labelled(flyte_world, "alpha-1-0-aaaaaa", "alpha", _factory_base.SLOTS[0])
    _queue.enqueue(flyte_world.queue, request("alpha", gen=2, kind="resume", at=1.0))
    _queue.enqueue(flyte_world.queue, request("beta", at=2.0))
    assert projects(flyte_world, factory.dispatch()) == ["beta"]


def test_a_queued_request_waits_for_its_project_on_the_shared_environment(flyte_world, monkeypatch):
    profile = token_profile()
    monkeypatch.setattr(
        _factory_base, "PROFILE", dataclasses.replace(profile, state=_factory_base.PROFILE.state)
    )
    monkeypatch.setattr(_factory_base, "SLOTS", profile.tasks)
    old = add_labelled(flyte_world, "alpha-1-0-aaaaaa", "alpha", profile.tasks[0])
    _queue.enqueue(flyte_world.queue, request("alpha", gen=2, kind="resume"))

    assert factory.dispatch() == []  # the cap has room: two pods of one project is what is refused
    old.phase = "SUCCEEDED"
    assert projects(flyte_world, factory.dispatch()) == ["alpha"]


def test_a_hand_over_frees_the_slot_for_other_projects_but_not_for_its_own_resume(flyte_world):
    ending = add_labelled(flyte_world, "alpha-1-0-aaaaaa", "alpha", _factory_base.SLOTS[0])
    add_labelled(flyte_world, "other-1-0-bbbbbb", "other", _factory_base.SLOTS[1])
    _queue.enqueue(flyte_world.queue, request("alpha", gen=2, kind="resume", at=1.0))
    _queue.enqueue(flyte_world.queue, request("gamma", at=2.0))

    started = factory.dispatch(exclude_run=ending.name)

    assert projects(flyte_world, started) == ["gamma"]  # it took the slot alpha's run is handing over
    assert [e.name for e in pending(flyte_world)] == ["alpha"]


def test_the_queue_view_says_what_a_blocked_request_waits_for(flyte_world):
    add_labelled(flyte_world, "alpha-1-0-aaaaaa", "alpha", _factory_base.SLOTS[0])
    _queue.enqueue(flyte_world.queue, request("alpha", gen=2, kind="resume"))
    _queue.enqueue(flyte_world.queue, request("beta"))
    rows = {r["name"]: r for r in factory.queue_view()["queue"]}
    assert rows["alpha"]["waiting_for"] == "waiting for alpha's current run to finish"
    assert "waiting_for" not in rows["beta"]
