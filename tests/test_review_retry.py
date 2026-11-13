"""A review the usage limit deferred, or that came back incomplete, re-runs by itself (#151, #169, #87)."""

from __future__ import annotations

import asyncio
import calendar
import time

import pytest

from flyte_agent_factory import (
    _inbox,
    _review,
    _reviewer,
    _session_config,
    _supervise_support,
    _timeline,
)
from tests._wait import wait_for
from tests.test_nudge import _criteria_path, _git, _Proc, _pump, _Queue, repo  # noqa: F401 -- the fixture

PASS = (True, "", None, "")


@pytest.fixture
def workspace(request):
    return request.getfixturevalue("repo")


def _commit_criteria(path):
    _criteria_path(path).write_text("- [ ] x\n")
    _git(path, "add", _session_config._acceptance_rel(_session_config._project_name))
    _git(path, "commit", "-m", "c")
    _git(path, "push")


def _fast(monkeypatch):
    """No slack and no minimum wait, so a retry time a fraction of a second ahead is what is waited for."""
    monkeypatch.setattr(_session_config, "POLL_EVERY_S", 0.0)
    monkeypatch.setattr(_supervise_support, "_REVIEW_RETRY_MIN_S", 0.0)
    monkeypatch.setattr(_supervise_support, "_REVIEW_RETRY_SLACK_S", 0.0)
    monkeypatch.setattr(_supervise_support, "_REVIEW_INCOMPLETE_RETRY_S", 0.0)


def _record_phases(timeline):
    """Every `phase(...)` call as (kind, limited_until, time it was made)."""
    seen = []
    original = timeline.phase

    def phase(kind, *, round_=0, limited_until=None):
        seen.append((kind, limited_until, time.time()))
        original(kind, round_=round_, limited_until=limited_until)

    timeline.phase = phase
    return seen


def _limited(seen):
    return [(until, at) for kind, until, at in seen if kind == "limited"]


@pytest.mark.asyncio
async def test_a_deferred_review_reruns_at_the_routers_retry_time(workspace, monkeypatch):
    _commit_criteria(workspace)
    _fast(monkeypatch)
    calls = []
    retry_at = 0.0

    async def review(ws, brief, acc, round_, tl):
        nonlocal retry_at
        calls.append((round_, time.time()))
        if len(calls) == 1:
            # Ahead of the minimum-wait floor (zero here) even on a loaded machine, so the router's time wins.
            retry_at = time.time() + 1.5
            raise _reviewer.ReviewLimited("retry at 06:00 UTC", retry_at=retry_at)
        return PASS

    monkeypatch.setattr(_review, "_review_round", review)
    bridge, proc, timeline, stop = _Queue(), _Proc(), _timeline._Timeline(), asyncio.Event()
    seen = _record_phases(timeline)
    pump = asyncio.create_task(_pump(bridge, proc, timeline, stop, workspace=workspace, brief="b"))
    bridge.push("DONE: built it")
    await wait_for(lambda: len(calls) == 2)  # no second DONE, no operator
    assert [r for r, _ in calls] == [1, 1], "the deferral used no round"
    assert calls[1][1] >= retry_at, "not before the router's time"
    assert _limited(seen)[0][0] == pytest.approx(retry_at, abs=0.01), "status shows the router's time"
    kinds = [kind for kind, *_ in seen]
    assert "review" in kinds[kinds.index("limited") :], "the phase is review again once it re-runs"
    await wait_for(lambda: any("passed your work" in m for m in proc.submitted))
    stop.set()
    await asyncio.wait_for(pump, timeout=30)


@pytest.mark.asyncio
async def test_a_review_deferred_after_an_earlier_pass_still_reruns(workspace, monkeypatch):
    """`declared_done` is true after any pass, and used to clear the retry on the next tick."""
    _commit_criteria(workspace)
    _fast(monkeypatch)
    _inbox._save_pass(1, "abc", "", timeline=_timeline._Timeline())  # a pass restored at startup
    calls = []

    async def review(ws, brief, acc, round_, tl):
        calls.append(round_)
        if len(calls) == 1:
            raise _reviewer.ReviewLimited("retry at 06:00 UTC", retry_at=time.time() + 0.3)
        return PASS

    monkeypatch.setattr(_review, "_review_round", review)
    bridge, proc, timeline, stop = _Queue(), _Proc(), _timeline._Timeline(), asyncio.Event()
    pump = asyncio.create_task(_pump(bridge, proc, timeline, stop, workspace=workspace, brief="b"))
    bridge.push("DONE: changed it again")
    await wait_for(lambda: len(calls) == 2, timeout=10)
    assert calls == [2, 2]
    stop.set()
    await asyncio.wait_for(pump, timeout=30)


@pytest.mark.asyncio
async def test_a_second_deferral_moves_the_time_and_never_shows_a_past_one(workspace, monkeypatch):
    _commit_criteria(workspace)
    _fast(monkeypatch)
    calls = []

    async def review(ws, brief, acc, round_, tl):
        calls.append(time.time())
        if len(calls) <= 2:
            raise _reviewer.ReviewLimited("retry at 06:00 UTC", retry_at=time.time() + 0.3)
        return PASS

    monkeypatch.setattr(_review, "_review_round", review)
    bridge, proc, timeline, stop = _Queue(), _Proc(), _timeline._Timeline(), asyncio.Event()
    seen = _record_phases(timeline)
    pump = asyncio.create_task(_pump(bridge, proc, timeline, stop, workspace=workspace, brief="b"))
    bridge.push("DONE: built it")
    await wait_for(lambda: len(calls) == 3, timeout=10)
    shown = _limited(seen)
    assert len({until for until, _ in shown}) == 2, shown
    assert all(until >= at for until, at in shown), "a past time was shown"
    stop.set()
    await asyncio.wait_for(pump, timeout=30)


@pytest.mark.asyncio
async def test_status_keeps_the_review_time_when_an_author_limit_clears(workspace, monkeypatch):
    """The author-limit paths set done/building; the pending review retry must still show."""
    _commit_criteria(workspace)
    _fast(monkeypatch)
    retry_at = time.time() + 3600

    async def review(ws, brief, acc, round_, tl):
        raise _reviewer.ReviewLimited("retry at 06:00 UTC", retry_at=retry_at)

    monkeypatch.setattr(_review, "_review_round", review)
    bridge, proc, timeline, stop = _Queue(), _Proc(), _timeline._Timeline(), asyncio.Event()
    pump = asyncio.create_task(_pump(bridge, proc, timeline, stop, workspace=workspace, brief="b"))
    bridge.push("DONE: built it")
    await wait_for(lambda: timeline._phase["kind"] == "limited")
    timeline.phase("building")  # what an author limit clearing does
    await wait_for(lambda: timeline._phase["kind"] == "limited")
    assert timeline._phase["limited_until"] == pytest.approx(retry_at)
    assert "re-runs automatically at" in (timeline._activity or "")
    assert "UTC" in (timeline._activity or "")
    stop.set()
    await asyncio.wait_for(pump, timeout=30)


@pytest.mark.asyncio
async def test_a_restart_while_the_review_waits_resumes_the_wait(workspace, monkeypatch):
    _commit_criteria(workspace)
    _fast(monkeypatch)
    calls = []

    async def review(ws, brief, acc, round_, tl):
        calls.append(round_)
        if len(calls) == 1:
            raise _reviewer.ReviewLimited("retry at 06:00 UTC", retry_at=time.time() + 1.5)
        return PASS

    monkeypatch.setattr(_review, "_review_round", review)
    bridge, proc, timeline, stop = _Queue(), _Proc(), _timeline._Timeline(), asyncio.Event()
    pump = asyncio.create_task(_pump(bridge, proc, timeline, stop, workspace=workspace, brief="b"))
    bridge.push("DONE: built it")
    await wait_for(lambda: timeline._phase["kind"] == "limited")
    stop.set()  # the pod goes away with the retry only on disk
    await asyncio.wait_for(pump, timeout=30)
    record = _session_config._load_pass_record()
    assert record and record["review_retry_at"] is not None
    assert record["review_retry_why"] == "the usage limit"
    assert len(calls) == 1

    bridge, proc, timeline, stop = _Queue(), _Proc(), _timeline._Timeline(), asyncio.Event()
    pump = asyncio.create_task(_pump(bridge, proc, timeline, stop, workspace=workspace, brief="b"))
    await wait_for(lambda: len(calls) == 2, timeout=10)
    assert calls == [1, 1]
    stop.set()
    await asyncio.wait_for(pump, timeout=30)


def test_an_old_pass_record_has_no_retry():
    path = _session_config._passed_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"round": 2, "sha": "x"}')
    record = _session_config._load_pass_record()
    assert record and record["review_retry_at"] is None


@pytest.mark.asyncio
async def test_an_incomplete_review_reruns_without_telling_the_session(workspace, monkeypatch):
    _commit_criteria(workspace)
    _fast(monkeypatch)
    calls = []

    async def review(ws, brief, acc, round_, tl):
        calls.append(round_)
        if len(calls) <= 2:
            raise _reviewer.ReviewIncomplete({"judge": "NO_VERDICT: no output"})
        return PASS

    monkeypatch.setattr(_review, "_review_round", review)
    bridge, proc, timeline, stop = _Queue(), _Proc(), _timeline._Timeline(), asyncio.Event()
    pump = asyncio.create_task(_pump(bridge, proc, timeline, stop, workspace=workspace, brief="b"))
    bridge.push("DONE: built it")
    await wait_for(lambda: any("passed your work" in m for m in proc.submitted), timeout=10)
    assert calls == [1, 1, 1], "no round consumed"
    assert not any("NOT been reviewed" in m for m in proc.submitted)
    assert any("review incomplete" in t and "automatically at" in t for _, _, t in timeline._rows)
    stop.set()
    await asyncio.wait_for(pump, timeout=30)


@pytest.mark.asyncio
async def test_after_two_incomplete_reruns_the_session_is_told(workspace, monkeypatch):
    _commit_criteria(workspace)
    _fast(monkeypatch)
    calls = []

    async def review(ws, brief, acc, round_, tl):
        calls.append(round_)
        raise _reviewer.ReviewIncomplete({"judge": "NOT_RUN: boom"})

    monkeypatch.setattr(_review, "_review_round", review)
    bridge, proc, timeline, stop = _Queue(), _Proc(), _timeline._Timeline(), asyncio.Event()
    pump = asyncio.create_task(_pump(bridge, proc, timeline, stop, workspace=workspace, brief="b"))
    bridge.push("DONE: built it")
    await wait_for(lambda: any("NOT been reviewed" in m for m in proc.submitted), timeout=10)
    await asyncio.sleep(0.2)
    assert calls == [1, 1, 1], "the first run and two automatic re-runs, none a new round"
    assert sum("NOT been reviewed" in m for m in proc.submitted) == 1
    told = next(m for m in proc.submitted if "NOT been reviewed" in m)
    assert "review incomplete after 2 automatic re-runs" in told and "judge (NOT_RUN: boom)" in told
    assert any("review incomplete after 2 automatic re-runs" in t for _, _, t in timeline._rows)
    assert not any("nobody reviewed" in t for _, _, t in timeline._rows)
    stop.set()
    await asyncio.wait_for(pump, timeout=30)


@pytest.mark.asyncio
async def test_an_incomplete_review_waits_in_review_not_limited(workspace, monkeypatch):
    """A missing verdict involves no limit: the status must not say `limited`; the reason persists."""
    _commit_criteria(workspace)
    _fast(monkeypatch)
    # Long enough to read the saved wait before the re-run replaces it.
    monkeypatch.setattr(_supervise_support, "_REVIEW_INCOMPLETE_RETRY_S", 1.0)
    calls = []

    async def review(ws, brief, acc, round_, tl):
        calls.append(round_)
        if len(calls) == 1:
            raise _reviewer.ReviewIncomplete({"completeness": "NO_VERDICT: no output"})
        return PASS

    monkeypatch.setattr(_review, "_review_round", review)
    bridge, proc, timeline, stop = _Queue(), _Proc(), _timeline._Timeline(), asyncio.Event()
    seen = _record_phases(timeline)
    pump = asyncio.create_task(_pump(bridge, proc, timeline, stop, workspace=workspace, brief="b"))
    try:
        bridge.push("DONE: built it")
        await wait_for(lambda: any("review incomplete" in t for _, _, t in timeline._rows), timeout=10)
        record = _session_config._load_pass_record()
        assert record and record["review_retry_why"] == "an incomplete review"
        assert timeline._phase["kind"] == "review"
        await wait_for(lambda: any("passed your work" in m for m in proc.submitted), timeout=10)
        assert not _limited(seen), "an incomplete review is not a usage limit"
        assert calls == [1, 1]
    finally:
        stop.set()
        await asyncio.wait_for(pump, timeout=30)


def test_the_routers_24_hour_retry_time_is_read_from_its_text():
    """`_usage._reset_at` reads only am/pm resets; the router's row says `retry at 06:00 UTC`."""
    now = float(calendar.timegm((2026, 11, 12, 2, 51, 0)))  # 02:51 UTC
    text = "USAGE_LIMIT: claude 5h at 100%; codex limited until 2026-11-16; retry at 06:00 UTC"
    assert _supervise_support._parse_router_retry_at(text, now) == now + 3 * 3600 + 9 * 60
    # A time already past today is tomorrow's.
    assert _supervise_support._parse_router_retry_at("retry at 01:30 UTC", now) == now + 22 * 3600 + 39 * 60
    assert _supervise_support._parse_router_retry_at("resets 5pm (UTC)", now) is None
    # A time that passed during the round (a judge deferred early) is due now, not tomorrow.
    assert _supervise_support._parse_router_retry_at("retry at 02:40 UTC", now) == now - 11 * 60


@pytest.mark.asyncio
async def test_a_deferral_with_only_the_routers_text_waits_for_that_time(workspace, monkeypatch):
    _commit_criteria(workspace)
    _fast(monkeypatch)

    async def review(ws, brief, acc, round_, tl):
        at = time.gmtime(time.time() + 2 * 3600)
        raise _reviewer.ReviewLimited(
            f"router: all engines limited; retry at {at.tm_hour:02d}:{at.tm_min:02d} UTC"
        )

    monkeypatch.setattr(_review, "_review_round", review)
    bridge, proc, timeline, stop = _Queue(), _Proc(), _timeline._Timeline(), asyncio.Event()
    pump = asyncio.create_task(_pump(bridge, proc, timeline, stop, workspace=workspace, brief="b"))
    try:
        bridge.push("DONE: built it")
        await wait_for(lambda: timeline._phase["kind"] == "limited")
        wait = timeline._phase["limited_until"] - time.time()
        assert 2 * 3600 - 120 < wait <= 2 * 3600, "the router's time, not the 30-minute guess"
    finally:
        stop.set()
        await asyncio.wait_for(pump, timeout=30)


@pytest.mark.asyncio
async def test_a_restart_does_not_reset_the_incomplete_rerun_cap(workspace, monkeypatch):
    _commit_criteria(workspace)
    _fast(monkeypatch)
    monkeypatch.setattr(_supervise_support, "_REVIEW_INCOMPLETE_RETRY_S", 1.0)
    calls = []

    async def review(ws, brief, acc, round_, tl):
        calls.append(round_)
        raise _reviewer.ReviewIncomplete({"completeness": "NO_VERDICT: no output"})

    monkeypatch.setattr(_review, "_review_round", review)
    bridge, proc, timeline, stop = _Queue(), _Proc(), _timeline._Timeline(), asyncio.Event()
    pump = asyncio.create_task(_pump(bridge, proc, timeline, stop, workspace=workspace, brief="b"))
    bridge.push("DONE: built it")
    await wait_for(lambda: any("review incomplete" in t for _, _, t in timeline._rows), timeout=10)
    stop.set()  # the pod restarts during the first automatic wait
    await asyncio.wait_for(pump, timeout=30)

    bridge, proc, timeline, stop = _Queue(), _Proc(), _timeline._Timeline(), asyncio.Event()
    pump = asyncio.create_task(_pump(bridge, proc, timeline, stop, workspace=workspace, brief="b"))
    try:
        await wait_for(lambda: any("NOT been reviewed" in m for m in proc.submitted), timeout=15)
        assert calls == [1, 1, 1], "the first run and two automatic re-runs across the restart"
    finally:
        stop.set()
        await asyncio.wait_for(pump, timeout=30)


@pytest.mark.asyncio
async def test_a_rejected_round_names_the_judge_without_a_verdict_on_the_pr(workspace, monkeypatch):
    _commit_criteria(workspace)
    _fast(monkeypatch)
    posted = []

    async def post(ws, tl, sha, rounds, why=""):
        posted.append(why)

    async def review(ws, brief, acc, round_, tl):
        tl.record_judge_outcome("correctness", outcome="FAIL", engine="claude", attempts=1)
        tl.record_judge_outcome(
            "craft", outcome="NO_VERDICT", reason="no output", engine="claude", attempts=3
        )
        return False, "FINDING: broken", None, ""

    monkeypatch.setattr(_review, "_review_round", review)
    monkeypatch.setattr(_supervise_support, "_post_verdict", post)
    bridge, proc, timeline, stop = _Queue(), _Proc(), _timeline._Timeline(), asyncio.Event()
    pump = asyncio.create_task(_pump(bridge, proc, timeline, stop, workspace=workspace, brief="b"))
    bridge.push("DONE: built it")
    await wait_for(lambda: bool(posted), timeout=10)
    assert posted[0].startswith("review rejected round 1"), posted
    assert "no verdict from craft" in posted[0] and "correctness" not in posted[0], posted
    stop.set()
    await asyncio.wait_for(pump, timeout=30)
