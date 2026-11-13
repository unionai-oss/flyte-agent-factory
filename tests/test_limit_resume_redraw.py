"""Regression coverage for refused resumes and stale terminal redraws."""

from __future__ import annotations

import asyncio

import flyte
import pytest

from flyte_agent_factory import _session_config, _supervise_support, _timeline, _usage
from tests._wait import wait_for
from tests.test_limit_resume import FINDINGS, WALL0, _clock, finish, rows, start, tick
from tests.test_nudge import _Proc, _pump, _Queue

pytestmark = pytest.mark.real_reset


@pytest.fixture(autouse=True)
def offline_report(monkeypatch):
    async def _aio(*_a, **_k) -> None:
        return None

    monkeypatch.setattr(flyte.report, "replace", type("R", (), {"aio": staticmethod(_aio)}))
    monkeypatch.setattr(_session_config, "POLL_EVERY_S", 0.0)
    monkeypatch.setattr(_supervise_support, "_in_flight_children", lambda: (0, ""))
    # These tests are about the usage-limit pause and used to run attended, which is never nudged;
    # their clocks jump by hours, which would trigger the idle nudge every run now gets.
    monkeypatch.setattr(_session_config, "NUDGE_AFTER_S", 1e12)


class _LimitProc(_Proc):
    """Models ClaudeProcess's hit re-arm rules faithfully."""

    def __init__(self):
        super().__init__()
        self.output_count = 0
        self._submitted_since_limit = True

    async def submit(self, text, **kwargs):
        await super().submit(text)
        self._submitted_since_limit = True

    def notice(self, text, now):
        if self._submitted_since_limit or self.limit_hit_at is None or (text and text != self.limit_text):
            self.limit_hit_at = now
            self.limit_text = text
            self._submitted_since_limit = False
            return True
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["resets Oct 22, 9am (UTC)", "resets 8:40pm (UTC)"])
async def test_refused_carry_on_then_redraw_tail_is_not_running_again(monkeypatch, text):
    now = _clock(monkeypatch, WALL0)
    proc = _LimitProc()
    _bridge, tl, stop, pump = await start(proc)
    await proc.submit(FINDINGS)
    now["t"] += 33
    proc.notice(text, now["t"])
    proc.last_output = now["t"] - 120
    await wait_for(lambda: rows(tl, "usage limit reached"))
    # Past 30 min and past the stated reset + 60 s: 8:40pm today, or Oct 22 9am, which is now read as that
    # date rather than an unknown time, so the resume waits for it.
    wall = vars(_usage)["time"].time()  # the fake wall clock `_clock` installed
    reset = _usage._reset_at(text, wall)
    assert reset is not None
    now["t"] += max(46 * 60, reset + 61 - wall)
    await wait_for(lambda: len(proc.submitted) == 2)
    # refusal: our carry-on echoes and the notice is redrawn (genuine refusal)
    now["t"] += 1
    proc.output_count += 200
    assert proc.notice(text, now["t"])
    proc.last_output = now["t"]
    await tick()
    # the rest of the redraw (prompt box) lands just after the pump saw the notice
    proc.output_count += 300
    await tick()
    now["t"] += 120
    await tick(0.1)
    ended = rows(tl, "running again")
    # an hour later it should still be paused and retrying
    now["t"] += _session_config.LIMIT_RETRY_S
    await tick(0.1)
    n = len(proc.submitted)
    await finish(stop, pump)
    assert not ended, "refused carry-on read as running again"
    assert n == 3, "never retried after a refused carry-on"


@pytest.mark.asyncio
async def test_back_to_back_episodes_note_end_then_different_text(monkeypatch):
    now = _clock(monkeypatch, WALL0)
    proc = _LimitProc()
    bridge, tl, stop, pump = await start(proc)
    await proc.submit(FINDINGS)
    now["t"] += 33
    proc.notice("resets 8:40pm (UTC)", now["t"])
    proc.last_output = now["t"] - 120
    await wait_for(lambda: rows(tl, "usage limit reached"))
    now["t"] += 46 * 60
    await wait_for(lambda: len(proc.submitted) == 2)
    now["t"] += 30
    bridge.push("working on FINDING-A")
    await wait_for(lambda: rows(tl, "running again"))
    # works for 5h on its own, then hits a genuine new limit
    now["t"] += 5 * 3600
    proc.output_count += 900_000
    assert proc.notice("resets 3am (UTC)", now["t"])
    proc.last_output = now["t"]
    await wait_for(lambda: len(rows(tl, "usage limit reached")) == 2)
    now["t"] += 120
    proc.last_output = now["t"] - 120
    now["t"] += 6 * 3600
    await wait_for(lambda: len(proc.submitted) == 3)
    assert "Carry on" in proc.submitted[2]
    await finish(stop, pump)


@pytest.mark.asyncio
async def test_back_to_back_same_text_no_new_submit(monkeypatch):
    """Day 2: same clock text, 24h later, nothing submitted by supervisor in between."""
    now = _clock(monkeypatch, WALL0)
    proc = _LimitProc()
    bridge, tl, stop, pump = await start(proc)
    await proc.submit(FINDINGS)
    now["t"] += 33
    proc.notice("resets 8:40pm (UTC)", now["t"])
    proc.last_output = now["t"] - 120
    await wait_for(lambda: rows(tl, "usage limit reached"))
    now["t"] += 46 * 60
    await wait_for(lambda: len(proc.submitted) == 2)
    now["t"] += 30
    bridge.push("working on FINDING-A")
    await wait_for(lambda: rows(tl, "running again"))
    now["t"] += 24 * 3600 - 30 * 60
    for _ in range(3):
        bridge.push("progress")
        await tick()
    proc.output_count += 2_000_000
    proc.notice("resets 8:40pm (UTC)", now["t"])
    proc.last_output = now["t"] - 120
    await tick(0.1)
    eps = len(rows(tl, "usage limit reached"))
    now["t"] += 3600
    await tick(0.1)
    n = len(proc.submitted)
    await finish(stop, pump)
    assert eps == 2, "genuine day-2 limit classified as stale redraw"
    assert n == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["review_submit_same_text", "autonomous_new_text"])
async def test_genuine_after_stale_ignored(monkeypatch, kind):
    now = _clock(monkeypatch, WALL0)
    proc = _LimitProc()
    _bridge, tl, stop, pump = await start(proc)
    await proc.submit(FINDINGS)
    now["t"] += 33
    proc.notice("resets 8:40pm (UTC)", now["t"])
    proc.last_output = now["t"] - 120
    await wait_for(lambda: rows(tl, "usage limit reached"))
    now["t"] += 46 * 60
    await wait_for(lambda: len(proc.submitted) == 2)
    now["t"] += 61
    proc.output_count += 6000
    proc.last_output = now["t"]
    await wait_for(lambda: rows(tl, "running again"))
    # stale redraw (pump re-armed the proc)
    now["t"] += 30
    assert proc.notice("resets 8:40pm (UTC)", now["t"])
    await tick(0.1)
    assert len(rows(tl, "usage limit reached")) == 1
    if kind == "review_submit_same_text":
        now["t"] += 60
        await proc.submit("Not done yet. Round 2 findings: FINDING-B")
        now["t"] += 1
        assert proc.notice("resets 8:40pm (UTC)", now["t"])
    else:
        now["t"] += 2 * 3600
        proc.output_count += 300_000
        assert proc.notice("resets 1am (UTC)", now["t"])
    proc.last_output = now["t"] - 120
    await wait_for(lambda: len(rows(tl, "usage limit reached")) == 2)
    now["t"] += 6 * 3600
    await wait_for(lambda: len(proc.submitted) >= 3 + (kind == "review_submit_same_text"))
    if kind == "review_submit_same_text":
        assert "FINDING-B" in proc.submitted[-1]
    await finish(stop, pump)


@pytest.mark.asyncio
async def test_no_reset_text(monkeypatch):
    now = _clock(monkeypatch, WALL0)
    proc = _LimitProc()
    _bridge, tl, stop, pump = await start(proc)
    await proc.submit(FINDINGS)
    now["t"] += 33
    proc.notice("", now["t"])
    proc.last_output = now["t"] - 120
    await wait_for(lambda: rows(tl, "usage limit reached"))
    now["t"] += 29 * 60
    await tick()
    assert len(proc.submitted) == 1
    now["t"] += 2 * 60
    await wait_for(lambda: len(proc.submitted) == 2)
    assert FINDINGS in proc.submitted[1]
    # refused
    now["t"] += 1
    assert proc.notice("", now["t"])
    proc.last_output = now["t"]
    await tick()
    now["t"] += 120
    await tick()
    assert not rows(tl, "running again")
    now["t"] += _session_config.LIMIT_RETRY_S
    await wait_for(lambda: len(proc.submitted) == 3)
    # accepted
    now["t"] += 61
    proc.output_count += 6000
    proc.last_output = now["t"]
    await wait_for(lambda: rows(tl, "running again"))
    # redraw of the textless notice while working
    now["t"] += 30
    proc.notice("", now["t"])
    for i in range(10):
        now["t"] += 10 * 60
        proc.output_count += 6000
        proc.last_output = now["t"] - (120 if i % 3 == 2 else 0)
        await tick()
    n = len(proc.submitted)
    await finish(stop, pump)
    assert n == 3


@pytest.mark.asyncio
async def test_done_cap_then_human_accepted_then_new_episode_gets_tries(monkeypatch):
    now = _clock(monkeypatch, WALL0)
    proc = _LimitProc()
    bridge, tl, stop, pump = await start(proc)
    bridge.push("DONE: delivered")
    await wait_for(lambda: "send direction" in (tl._activity or ""))
    await proc.submit("Please wrap up: run end-project")
    now["t"] += 10
    proc.notice("resets 8:40pm (UTC)", now["t"])
    proc.last_output = now["t"] - 120
    for _ in range(6):
        now["t"] += _session_config.LIMIT_RETRY_S + 61
        await tick()
        if len(proc.submitted) > 1:
            proc.notice("resets 8:40pm (UTC)", now["t"])  # each try refused
            proc.last_output = now["t"] - 120
            await tick()
    assert len(rows(tl, "trying to resume")) == 2
    assert rows(tl, "a human needs to look")
    assert "Please wrap up" in proc.submitted[1]
    # human sends direction, accepted
    await proc.submit("Direction from human: just run end-project")
    for _ in range(3):
        now["t"] += 61
        proc.output_count += 500
        proc.last_output = now["t"]
        await tick()
    assert rows(tl, "running again")
    assert "needs a human" not in (tl._activity or "")
    await finish(stop, pump)


@pytest.mark.asyncio
async def test_direction_refused_then_second_direction_accepted(monkeypatch):
    now = _clock(monkeypatch, WALL0)
    proc = _LimitProc()
    _bridge, tl, stop, pump = await start(proc)
    await proc.submit(FINDINGS)
    now["t"] += 33
    proc.notice("resets 8:40pm (UTC)", now["t"])
    proc.last_output = now["t"] - 120
    await wait_for(lambda: rows(tl, "usage limit reached"))
    now["t"] += 10 * 60
    await proc.submit("Direction from human: do FINDING-A first")
    now["t"] += 1
    assert proc.notice("resets 8:40pm (UTC)", now["t"])
    await tick()
    # just after the reset (before reset+60), human sends again, accepted
    now["t"] = 10_000 + 38 * 60 + 26 + 20
    await proc.submit("Direction from human: do FINDING-A first (again)")
    for _ in range(4):
        now["t"] += 61
        proc.output_count += 1500
        proc.last_output = now["t"] - 70
        await tick()
    assert rows(tl, "running again")
    n = len(proc.submitted)
    now["t"] += 3 * _session_config.LIMIT_RETRY_S
    await tick()
    await finish(stop, pump)
    assert n == 3 and len(proc.submitted) == 3


@pytest.mark.asyncio
async def test_direction_refused_then_carry_on_restates_it_and_is_accepted(monkeypatch):
    now = _clock(monkeypatch, WALL0)
    proc = _LimitProc()
    _bridge, tl, stop, pump = await start(proc)
    await proc.submit(FINDINGS)
    now["t"] += 33
    proc.notice("resets 8:40pm (UTC)", now["t"])
    proc.last_output = now["t"] - 120
    await wait_for(lambda: rows(tl, "usage limit reached"))
    now["t"] += 10 * 60
    await proc.submit("Direction from human: do FINDING-A first")
    now["t"] += 1
    proc.output_count += 100
    assert proc.notice("resets 8:40pm (UTC)", now["t"])
    await tick()
    now["t"] += 40 * 60
    await wait_for(lambda: len(proc.submitted) == 3)
    assert "do FINDING-A first" in proc.submitted[2] and FINDINGS not in proc.submitted[2]
    now["t"] += 61
    proc.output_count += 800
    proc.last_output = now["t"]
    await tick()
    now["t"] += 61
    await tick()
    assert rows(tl, "running again")
    # stale redraw afterwards: no new episode, no re-inject
    now["t"] += 5
    proc.notice("resets 8:40pm (UTC)", now["t"])
    for _ in range(6):
        now["t"] += 15 * 60
        proc.output_count += 9000
        proc.last_output = now["t"] - 120
        await tick()
    assert len(proc.submitted) == 3
    assert len(rows(tl, "usage limit reached")) == 1
    await finish(stop, pump)


@pytest.mark.asyncio
async def test_kickoff_interrupted_before_any_note(monkeypatch):
    now = _clock(monkeypatch, WALL0)
    proc = _LimitProc()
    tl = _timeline._Timeline()

    bridge, stop = _Queue(), asyncio.Event()
    kickoff = "Read BRIEF.md and start. " + "x" * 500
    await proc.submit(kickoff)
    pump = asyncio.create_task(_pump(bridge, proc, tl, stop, kickoff=kickoff))
    now["t"] += 20
    proc.output_count += 1000
    proc.notice("resets 8:40pm (UTC)", now["t"])
    proc.last_output = now["t"]
    await wait_for(lambda: rows(tl, "usage limit reached"))
    for _ in range(30):
        now["t"] += 60
        await tick()
        # refusals of anything submitted while limited
        proc.notice("resets 8:40pm (UTC)", now["t"])
    before_reset = len(proc.submitted)
    now["t"] = 10_000 + 38 * 60 + 26 + 61 + 33
    await tick(0.1)
    after = list(proc.submitted)
    await finish(stop, pump)
    assert before_reset == 1
    assert not rows(tl, "the brief got no response")
    assert any("Carry on" in s or "here it is again" in s for s in after[1:]), "never resumed"
    resumes = [s for s in after[1:] if "here it is again" in s or "Carry on" in s]
    assert len(resumes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("redraw", ["rsets 8:40pm (UTC)", "resets 8:4"])
async def test_stale_redraw_with_dropped_letter(monkeypatch, redraw):
    now = _clock(monkeypatch, WALL0)
    proc = _LimitProc()
    _bridge, tl, stop, pump = await start(proc)
    await proc.submit(FINDINGS)
    now["t"] += 33
    proc.notice("resets 8:40pm (UTC)", now["t"])
    proc.last_output = now["t"] - 120
    await wait_for(lambda: rows(tl, "usage limit reached"))
    now["t"] += 46 * 60
    await wait_for(lambda: len(proc.submitted) == 2)
    now["t"] += 61
    proc.output_count += 6000
    proc.last_output = now["t"]
    await wait_for(lambda: rows(tl, "running again"))
    now["t"] += 120
    proc.notice(redraw, now["t"])
    for i in range(12):
        now["t"] += 5 * 60
        proc.output_count += 6000
        proc.last_output = now["t"] - (120 if i % 3 == 2 else 0)
        await tick()
    n = len(proc.submitted)
    await finish(stop, pump)
    assert n == 2, "carry-on injected into a working session"


@pytest.mark.asyncio
async def test_new_apparent_episode_clears_on_autonomous_output(monkeypatch):
    now = _clock(monkeypatch, WALL0)
    proc = _LimitProc()
    bridge, tl, stop, pump = await start(proc)
    await proc.submit(FINDINGS)
    now["t"] += 33
    proc.notice("resets 8:40pm (UTC)", now["t"])
    await wait_for(lambda: rows(tl, "usage limit reached"))
    bridge.push("working again")
    await wait_for(lambda: rows(tl, "running again"))
    now["t"] += 3600
    proc.notice("reset time unavailable", now["t"])
    await wait_for(lambda: len(rows(tl, "usage limit reached")) == 2)
    now["t"] += 61
    proc.output_count += _session_config.LIMIT_RESUMED_CHARS
    proc.last_output = now["t"]
    await wait_for(lambda: len(rows(tl, "running again")) == 2)
    now["t"] += _session_config.LIMIT_RETRY_S
    await tick()
    await finish(stop, pump)
    assert proc.submitted == [FINDINGS]
