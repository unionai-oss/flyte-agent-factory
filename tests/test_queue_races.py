"""Dispatcher races the launch queue must survive (QA round 2 scenarios)."""

from typing import Any

import pytest

from flyte_agent_factory import _factory as factory
from flyte_agent_factory import _factory_base, _queue
from tests.fake_flyte import FakeRun

S0, S1 = _factory_base.SLOTS[0], _factory_base.SLOTS[1]


def req(name, gen=1, at=1.0):
    return _queue.Request(name, gen, "new", "b", "opus", "r", "", {}, at)


@pytest.fixture
def no_lease(monkeypatch):
    monkeypatch.setattr(_queue, "take_lease", lambda *a, **kw: None)


@pytest.fixture
def lagging_abort(monkeypatch):
    """Server-side abort is asynchronous: an aborted run is still listed live until `settle()`."""
    pending = []

    def abort(self, reason):
        self.abort_reason = reason
        pending.append(self)

    monkeypatch.setattr(FakeRun, "abort", abort)

    def settle():
        for r in pending:
            r.phase = "ABORTED"
        pending.clear()

    return settle


def test_same_request_both_newcomers_yield_with_lagging_abort(
    flyte_world, no_lease, lagging_abort, monkeypatch
):
    _queue.enqueue(flyte_world.queue, req("alpha"))
    flyte_world.add("busy", S1)
    # D2 must not see D1's run in its reconcile/busy look (stale reads, the case the dedupe exists for)
    real_lab, real_live = _factory_base._labelled_runs, factory.operated_live
    d1: dict[str, str] = {}
    hide = {"on": False}

    def lab(name, gen=None, **kw):
        return [r for r in real_lab(name, gen, **kw) if not (hide["on"] and r.name == d1.get("run"))]

    def live():
        rs = real_live()
        if hide["on"] and hide.get("first", True):
            hide["first"] = False
            return [r for r in rs if r.name != d1.get("run")]
        return rs

    monkeypatch.setattr(_factory_base, "_labelled_runs", lab)
    monkeypatch.setattr(factory, "operated_live", live)

    def d2():
        d1["run"] = flyte_world.created[-1]["name"]
        hide["on"] = True
        flyte_world.d2 = factory.dispatch()
        hide["on"] = False

    flyte_world.after_create = d2
    factory.dispatch()
    lagging_abort()
    factory.dispatch()  # next sweep
    e = _queue.entries(flyte_world.queue, "alpha")[0]
    assert e.closed != "failed", "request permanently failed because two yields overwrote one attempt marker"


def test_different_requests_both_yield_then_recover(flyte_world, no_lease, lagging_abort, monkeypatch):
    _queue.enqueue(flyte_world.queue, req("alpha", at=1.0))
    _queue.enqueue(flyte_world.queue, req("bravo", at=2.0))
    flyte_world.add("busy", S1)
    # D2 sees only bravo (alpha taken off its stale list) and S0 free
    real_pending = _queue.pending

    def d2():
        import flyte_agent_factory._queue as q

        q.pending = lambda queue, name=None: [e for e in real_pending(queue, name) if e.name == "bravo"]
        real_live = factory.operated_live
        n = {"k": 0}

        def live():
            n["k"] += 1
            rs: list[Any] = real_live()  # FakeRun carries `task`, the typed remote.Run does not
            return [r for r in rs if r.task != S0] if n["k"] == 1 else rs

        factory.operated_live = live
        try:
            flyte_world.d2 = factory.dispatch()
        finally:
            q.pending = real_pending
            factory.operated_live = real_live

    flyte_world.after_create = d2
    factory.dispatch()
    lagging_abort()
    out = factory.dispatch()
    assert out and flyte_world.runs[out[0]].pb2.labels[_factory_base.LABEL_PROJECT] == "alpha"


def test_post_create_valueerror_does_not_mark_failed_with_a_live_run(flyte_world, monkeypatch):
    _queue.enqueue(flyte_world.queue, req("alpha"))
    real = factory.operated_live
    n = []

    def live():
        n.append(1)
        if len(n) == 2:
            raise KeyError("listing parse")
        return real()

    monkeypatch.setattr(factory, "operated_live", live)
    factory.dispatch()
    e = _queue.entries(flyte_world.queue, "alpha")[0]
    lives = [r.name for r in flyte_world.runs.values() if r.phase == "RUNNING"]
    assert not (e.closed == "failed" and lives), "request closed .failed while its run is live"


def test_a_duplicate_that_has_not_started_never_wins_over_an_older_run(flyte_world, no_lease):
    # `action.start_time` of a run that has not started reads as the epoch, so ordering by it would keep
    # the newest, unstarted duplicate and abort the run already working. Creation order decides instead.
    from datetime import datetime, timezone

    _queue.enqueue(flyte_world.queue, req("alpha"))

    def unstarted_dup():
        dup = flyte_world.add(
            "zzz-dup", S1, labels={_factory_base.LABEL_PROJECT: "alpha", _factory_base.LABEL_GEN: "1"}
        )
        dup.phase = "QUEUED"
        dup.action.start_time = datetime(1970, 1, 1, tzinfo=timezone.utc)

    flyte_world.after_create = unstarted_dup
    [mine] = factory.dispatch()
    assert flyte_world.runs[mine].phase == "RUNNING"
    assert flyte_world.runs["zzz-dup"].phase == "ABORTED"
