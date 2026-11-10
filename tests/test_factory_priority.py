"""Launch priority: a `low` request never takes a slot from a waiting normal one."""

import dataclasses
from typing import Any, cast

import pytest

from flyte_agent_factory import _factory as factory
from flyte_agent_factory import (
    _factory_base,
    _factory_policy,
    _gardening,
    _github,
    _planner,
    _planner_runtime,
    _profile,
    _queue,
)
from tests.test_factory import projects


def enqueue(world, name, at, priority="normal", policy=None):
    request = _queue.Request(name, 1, "new", "b", "opus", "r", "", policy or {}, at, priority=priority)
    _queue.enqueue(world.queue, request)


def test_a_low_request_starts_when_nothing_normal_waits_and_a_slot_is_free(flyte_world):
    enqueue(flyte_world, "garden", 1.0, "low")
    assert projects(flyte_world, factory.dispatch()) == ["garden"]


def test_a_low_request_waits_while_a_normal_one_waits(flyte_world):
    # One runnable normal request takes the slot; another still waits ahead of low.
    flyte_world.add("one", _factory_base.SLOTS[0])
    enqueue(flyte_world, "garden", 1.0, "low")
    enqueue(flyte_world, "work", 2.0)
    enqueue(flyte_world, "next", 3.0)
    assert projects(flyte_world, factory.dispatch()) == ["work"]
    assert [e.name for e in _queue.pending(flyte_world.queue)] == ["garden", "next"]


def test_a_low_request_starts_while_a_normal_one_is_held_by_its_own_missing_login(flyte_world, monkeypatch):
    # The normal request is pinned to an engine this profile has no token for: the hold is its own, so
    # it cannot start and must not keep the free slot from the low one (issues #121, #139).
    monkeypatch.setattr(_factory_policy, "_has_token", lambda engine: engine != "codex")
    enqueue(flyte_world, "stuck", 2.0, policy={"AGENT_FACTORY_AUTHOR_ENGINE": "codex"})
    enqueue(flyte_world, "garden", 1.0, "low")
    assert projects(flyte_world, factory.dispatch()) == ["garden"]
    assert [e.name for e in _queue.pending(flyte_world.queue)] == ["stuck"]


def test_with_one_free_slot_and_both_waiting_the_normal_one_is_admitted(flyte_world):
    flyte_world.add("one", _factory_base.SLOTS[0])
    enqueue(flyte_world, "garden", 1.0, "low")  # older, but low
    enqueue(flyte_world, "work", 2.0)
    assert projects(flyte_world, factory.dispatch()) == ["work"]
    assert [e.name for e in _queue.pending(flyte_world.queue)] == ["garden"]


def test_a_low_request_follows_the_normal_one_when_two_slots_are_free(flyte_world):
    enqueue(flyte_world, "garden", 1.0, "low")
    enqueue(flyte_world, "work", 2.0)
    assert projects(flyte_world, factory.dispatch()) == ["work", "garden"]


def test_an_expedite_request_goes_before_a_normal_one(flyte_world):
    flyte_world.add("one", _factory_base.SLOTS[0])
    enqueue(flyte_world, "work", 1.0)
    enqueue(flyte_world, "urgent", 2.0, "expedite")
    assert projects(flyte_world, factory.dispatch()) == ["urgent"]


def test_a_normal_only_queue_is_served_oldest_first_as_before(flyte_world):
    flyte_world.add("one", _factory_base.SLOTS[0])
    enqueue(flyte_world, "newer", 2.0)
    enqueue(flyte_world, "older", 1.0)
    assert projects(flyte_world, factory.dispatch()) == ["older"]


def test_launch_project_stores_the_priority_and_the_queue_shows_it(flyte_world):
    flyte_world.add("one", _factory_base.SLOTS[0])
    flyte_world.add("two", _factory_base.SLOTS[1])
    result = factory.launch_project("brief", "alpha", priority="low")
    assert result["status"] == "queued"
    [entry] = _queue.pending(flyte_world.queue)
    assert entry.request is not None and entry.request.priority == "low"
    assert factory.queue_view()["queue"][0]["priority"] == "low"


def test_a_launch_defaults_to_normal_priority(flyte_world):
    flyte_world.add("one", _factory_base.SLOTS[0])
    flyte_world.add("two", _factory_base.SLOTS[1])
    factory.launch_project("brief", "alpha")
    request = _queue.pending(flyte_world.queue)[0].request
    assert request is not None and request.priority == "normal"


def test_an_unknown_priority_is_refused_and_writes_nothing(flyte_world):
    with pytest.raises(ValueError, match="priority"):
        factory.launch_project("brief", "alpha", priority="urgent")
    assert _queue.pending(flyte_world.queue) == []


def test_a_request_stored_before_priorities_existed_reads_as_normal():
    body = b'{"name":"a","gen":1,"kind":"new","brief":"b","model":"m","repo":"r","demo_package":"",'
    body += b'"policy":{},"enqueued_at":1.0}'
    assert _queue.Request.from_json(body).priority == "normal"


# --- planner admission ranks the same way

GIT_ROOT = "team/direction"


class _Refs:
    """The GitHub calls `import_requests` makes when nothing was imported before."""

    def __init__(self):
        self.refs: dict[str, str] = {}

    def _call(self, repo, method, path, json=None):
        if method == "GET" and "/matching-refs/" in path:
            return []
        if method == "GET" and "/ref/" in path:
            raise _github.GithubError("missing", 404)
        if method == "GET" and "/commits/" in path:
            return {"tree": {"sha": "tree"}, "message": "{}"}
        if method == "POST" and path.endswith("/commits"):
            return {"sha": "new"}
        if method == "POST" and path.endswith("/refs"):
            return {}
        raise AssertionError((method, path))


def _planned(world, monkeypatch, *requests, items=()):
    monkeypatch.setattr(
        _factory_base,
        "PROFILE",
        dataclasses.replace(_factory_base.PROFILE, planner=_profile.Planner(admission="planner")),
    )
    monkeypatch.setattr(factory, "gardening_decision", lambda now=None: ("low", []))
    for name, at, priority in requests:
        enqueue(world, name, at, priority)
    manual, _ = _planner_runtime.import_requests(
        cast(_github.GithubApp, _Refs()), GIT_ROOT, "base", cast(Any, None), []
    )
    capacity = (_planner.Capacity("claude", "login", slot="slot"),)
    facts = _planner.Facts(items=items, capacity=capacity, manual_requests=tuple(manual))
    return _planner.plan(facts)


def test_planner_admission_holds_a_low_request_while_a_normal_one_waits(flyte_world, monkeypatch):
    result = _planned(
        flyte_world, monkeypatch, (_gardening.NAME, 1.0, "low"), ("feature", 2.0, "normal"),
        items=(_planner.BoardItem("I1", 1, "other/repo", status="Ready", body="A task"),),
    )  # fmt: skip
    assert [launch.item.name for launch in result.launches] == ["feature"]
    # A separate repository avoids a file hold: the Ready item only lacks capacity.
    assert [
        (w.item.node_id, w.reason.code) for w in result.waiting if isinstance(w.item, _planner.BoardItem)
    ] == [("I1", "no_capacity")]
    assert [
        (w.item.name, w.reason.code, w.reason.behind)
        for w in result.waiting
        if isinstance(w.item, _planner.ManualRequest)
    ] == [(_gardening.NAME, "low_priority_waits", 1)]


def test_planner_admission_lets_a_file_conflict_not_priority_hold_the_low_request(flyte_world, monkeypatch):
    result = _planned(
        flyte_world, monkeypatch, (_gardening.NAME, 1.0, "low"), ("feature", 2.0, "normal"),
        items=(_planner.BoardItem("I1", 1, "r", status="Ready", body="A task"),),
    )  # fmt: skip
    assert [launch.item.name for launch in result.launches] == ["feature"]
    # The Ready item is held by the same overlap with `feature`, so it no longer holds the low request back;
    # the low one still does not start, and now says the real reason (issues #121, #139).
    assert [
        (w.item.name, w.reason.code) for w in result.waiting if isinstance(w.item, _planner.ManualRequest)
    ] == [(_gardening.NAME, "file_conflict")]


def test_planner_admission_serves_an_expedite_request_before_a_ready_item(flyte_world, monkeypatch):
    result = _planned(
        flyte_world, monkeypatch, ("work", 1.0, "normal"), ("urgent", 2.0, "expedite"),
        items=(_planner.BoardItem("I1", 1, "r", status="Ready", body="A task"),),
    )  # fmt: skip
    assert [launch.item.name for launch in result.launches] == ["urgent"]
