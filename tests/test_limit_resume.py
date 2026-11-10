"""Usage-limit recovery with wall and monotonic clocks advancing together."""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import cast

import flyte.report
import pytest

from flyte_agent_factory import _session_config, _supervise, _supervise_support, _timeline, _usage
from flyte_agent_factory._bridge import Bridge
from flyte_agent_factory._process import ClaudeProcess
from tests._wait import wait_for
from tests.conftest import patch_supervisor_clock
from tests.test_nudge import _Proc as _BaseProc
from tests.test_nudge import _Queue

pytestmark = pytest.mark.real_reset


class _Proc(_BaseProc):
    """The nudge tests' session, with the redraw counter these tests advance and read."""

    output_count = 0


@pytest.fixture(autouse=True)
def offline_report(monkeypatch):
    async def _aio(*_a, **_k) -> None:
        return None

    monkeypatch.setattr(flyte.report, "replace", type("R", (), {"aio": staticmethod(_aio)}))
    monkeypatch.setattr(_session_config, "POLL_EVERY_S", 0.0)
    monkeypatch.setattr(_supervise_support, "_in_flight_children", lambda: (0, ""))


def _clock(monkeypatch, wall_start):
    """Fake monotonic AND wall clock, advancing together."""
    import time as real_time

    now = {"t": 10_000.0}
    off = wall_start - now["t"]

    class _Clock:
        def monotonic(self):
            return now["t"]

        def time(self):
            return now["t"] + off

        def __getattr__(self, name):
            return getattr(real_time, name)

    clock = _Clock()
    patch_supervisor_clock(monkeypatch, clock)
    monkeypatch.setattr(_usage, "time", clock)  # the limit notice's reset time reads the wall clock here
    return now


WALL0 = dt.datetime(2026, 10, 19, 20, 1, 34, tzinfo=dt.timezone.utc).timestamp()
FINDINGS = "Not done yet. An independent reviewer rejected it (round 1 of at most 8). FINDING-A: x is broken."


@pytest.fixture(autouse=True)
def no_nudges(monkeypatch):
    """These tests are about the usage-limit pause; they used to run attended, which is never nudged.

    Their clocks jump by hours, which would otherwise trigger the idle nudge that every run now gets.
    """
    monkeypatch.setattr(_session_config, "NUDGE_AFTER_S", 1e12)


def rows(tl, s):
    return [t for _, _, t in tl._rows if s in t]


async def tick(n=0.03):
    await asyncio.sleep(n)


async def start(proc: _BaseProc):
    bridge, tl, stop = _Queue(), _timeline._Timeline(), asyncio.Event()
    pump = asyncio.create_task(_supervise._pump(cast(Bridge, bridge), cast(ClaudeProcess, proc), tl, stop))
    return bridge, tl, stop, pump


async def finish(stop, pump):
    stop.set()
    await asyncio.wait_for(pump, timeout=30)


@pytest.mark.asyncio
async def test_live_timeline(monkeypatch):
    now = _clock(monkeypatch, WALL0)
    proc = _Proc()
    proc.output_count = 0
    _bridge, tl, stop, pump = await start(proc)
    await proc.submit(FINDINGS)
    now["t"] += 33  # 20:02:07
    proc.limit_hit_at = now["t"]
    proc.limit_text = "resets 8:40pm (UTC)"
    proc.last_output = now["t"]
    await wait_for(lambda: rows(tl, "usage limit reached"))
    # Redraw output before reset: must not end anything, nothing submitted
    for _ in range(10):
        now["t"] += 120
        proc.output_count += 5000
        proc.last_output = now["t"]
        await tick()
    assert len(proc.submitted) == 1, proc.submitted
    assert not rows(tl, "running again")
    # 20:45:24, redraw continues (terminal never quiet)
    now["t"] = 10_000 + 33 + (43 * 60 + 17)
    proc.output_count += 9000
    proc.last_output = now["t"]
    await tick()
    assert len(proc.submitted) == 1
    now["t"] += 61
    await wait_for(lambda: len(proc.submitted) == 2)
    assert FINDINGS in proc.submitted[1]
    assert len(rows(tl, "trying to resume")) == 1
    # It works; lots of output
    for _ in range(5):
        now["t"] += 61
        proc.output_count += 5000
        proc.last_output = now["t"]
        await tick()
    assert rows(tl, "running again")
    now["t"] += 3 * _session_config.LIMIT_RETRY_S
    await tick()
    assert len(proc.submitted) == 2
    await finish(stop, pump)


@pytest.mark.asyncio
@pytest.mark.parametrize("output", [20_000])
async def test_stale_notice_after_carry_on_does_not_repeat_resume(monkeypatch, output):
    """Carry-on accepted, session works, TUI redraws the OLD notice (same text) once."""
    now = _clock(monkeypatch, WALL0)
    proc = _Proc()
    proc.output_count = 0
    _bridge, tl, stop, pump = await start(proc)
    await proc.submit(FINDINGS)
    now["t"] += 33
    proc.limit_hit_at = now["t"]
    proc.limit_text = "resets 8:40pm (UTC)"
    proc.last_output = now["t"] - 120
    await wait_for(lambda: rows(tl, "usage limit reached"))
    now["t"] += 45 * 60
    await wait_for(lambda: len(proc.submitted) == 2)
    # the real ClaudeProcess sets _submitted_since_limit on submit, so a redraw of the
    # old notice right after registers as a new hit with the same text
    now["t"] += 2
    proc.limit_hit_at = now["t"]
    # session works for 2 hours, with quiet gaps (thinking / long command) but no note
    for i in range(8):
        now["t"] += 15 * 60
        proc.output_count += output
        proc.last_output = now["t"] - (120 if i % 2 else 0)
        await tick()
    ended = bool(rows(tl, "running again"))
    resends = len(rows(tl, "trying to resume"))
    await finish(stop, pump)
    assert ended and resends == 1, (ended, resends)
    assert len(proc.submitted) == 2


@pytest.mark.asyncio
async def test_stale_notice_after_episode_end_within_300s(monkeypatch):
    """Episode ends at reset+~2min; the old notice is redrawn at reset+4min while it works."""
    now = _clock(monkeypatch, WALL0)
    proc = _Proc()
    proc.output_count = 0
    _bridge, tl, stop, pump = await start(proc)
    await proc.submit(FINDINGS)
    now["t"] += 33
    proc.limit_hit_at = now["t"]
    proc.limit_text = "resets 8:40pm (UTC)"
    proc.last_output = now["t"] - 120
    await wait_for(lambda: rows(tl, "usage limit reached"))
    reset_mono = 10_000 + (38 * 60 + 26)
    now["t"] = reset_mono + 61
    await wait_for(lambda: len(proc.submitted) == 2)
    now["t"] += 61
    proc.output_count += 5000
    proc.last_output = now["t"]
    await wait_for(lambda: rows(tl, "running again"))
    # working session; old notice redrawn (pump re-armed _submitted_since_limit)
    now["t"] = reset_mono + 240
    proc.limit_hit_at = now["t"]
    proc.output_count += 6000
    proc.last_output = now["t"]
    await tick(0.1)
    for _ in range(80):
        now["t"] += 30
        proc.output_count += 6000
        proc.last_output = now["t"] - (120 if _ % 5 == 4 else 0)
        await tick()
    n = len(proc.submitted)
    await finish(stop, pump)
    assert n == 2, f"injected {n - 2} extra message(s) into a working session"
    assert len(rows(tl, "usage limit reached")) == 1


@pytest.mark.asyncio
async def test_second_episode_after_answered_resume(monkeypatch):
    now = _clock(monkeypatch, WALL0)
    proc = _Proc()
    proc.output_count = 0
    _bridge, tl, stop, pump = await start(proc)
    await proc.submit(FINDINGS)
    now["t"] += 33
    proc.limit_hit_at = now["t"]
    proc.limit_text = "resets 8:40pm (UTC)"
    proc.last_output = now["t"] - 120
    await wait_for(lambda: rows(tl, "usage limit reached"))
    now["t"] += 45 * 60
    await wait_for(lambda: len(proc.submitted) == 2)
    now["t"] += 61
    proc.output_count += 50_000
    proc.last_output = now["t"]
    await wait_for(lambda: rows(tl, "running again"))
    # works for 4 hours without a note, then hits a new limit on its own
    now["t"] += 4 * 3600
    proc.output_count += 500_000
    proc.limit_hit_at = now["t"]
    proc.limit_text = "resets 2am (UTC)"
    proc.last_output = now["t"] - 120
    await wait_for(lambda: len(rows(tl, "usage limit reached")) == 2)
    now["t"] += 6 * 3600
    await wait_for(lambda: len(proc.submitted) == 3)
    third = proc.submitted[2]
    await finish(stop, pump)
    assert third.count("here it is again") <= 1, "nested restatement"
    assert FINDINGS not in third, "stale findings (already re-driven and worked on for 4h) restated again"


@pytest.mark.asyncio
async def test_work_already_started_does_not_restate_old_message(monkeypatch):
    """Last supervisor message was hours ago and visibly acted on (lots of output), no notes."""
    now = _clock(monkeypatch, WALL0)
    proc = _Proc()
    proc.output_count = 0
    _bridge, tl, stop, pump = await start(proc)
    await proc.submit("Reminder: you have not declared DONE ...")
    now["t"] += 3 * 3600
    proc.output_count += 800_000
    proc.limit_hit_at = now["t"]
    proc.limit_text = "resets 11pm (UTC)"
    proc.last_output = now["t"] - 120
    await wait_for(lambda: rows(tl, "usage limit reached"))
    now["t"] += 4 * 3600
    await wait_for(lambda: len(proc.submitted) == 2)
    msg = proc.submitted[1]
    await finish(stop, pump)
    assert "here it is again" not in msg


@pytest.mark.asyncio
async def test_no_prior_message(monkeypatch):
    now = _clock(monkeypatch, WALL0)
    proc = _Proc()
    proc.output_count = 0
    _bridge, tl, stop, pump = await start(proc)
    proc.limit_hit_at = now["t"]
    proc.limit_text = "resets 8:40pm (UTC)"
    proc.last_output = now["t"] - 120
    await wait_for(lambda: rows(tl, "usage limit reached"))
    now["t"] += 45 * 60
    await wait_for(lambda: len(proc.submitted) == 1)
    assert "Carry on exactly where you left off" in proc.submitted[0]
    await finish(stop, pump)


@pytest.mark.asyncio
async def test_direction_before_reset_accepted(monkeypatch):
    """Human direction during the pause, accepted (limit already lifted early / or refusal notice missed)."""
    now = _clock(monkeypatch, WALL0)
    proc = _Proc()
    proc.output_count = 0
    _bridge, tl, stop, pump = await start(proc)
    await proc.submit(FINDINGS)
    now["t"] += 33
    proc.limit_hit_at = now["t"]
    proc.limit_text = "resets 8:40pm (UTC)"
    proc.last_output = now["t"] - 120
    await wait_for(lambda: rows(tl, "usage limit reached"))
    now["t"] += 20 * 60
    await proc.submit("Direction from human: prioritise FINDING-A")
    for _ in range(12):
        now["t"] += 10 * 60
        proc.output_count += 10_000
        proc.last_output = now["t"] - 120
        await tick()
    ended = rows(tl, "running again")
    sent = len(proc.submitted)
    await finish(stop, pump)
    assert ended
    assert sent == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "direction", ["Direction from human: prioritise FINDING-A", "Reminder: prioritise FINDING-A"]
)
async def test_direction_before_reset_refused(monkeypatch, direction):
    """Human direction during the pause; refused (notice redrawn after submit)."""
    now = _clock(monkeypatch, WALL0)
    proc = _Proc()
    proc.output_count = 0
    _bridge, tl, stop, pump = await start(proc)
    await proc.submit(FINDINGS)
    now["t"] += 33
    proc.limit_hit_at = now["t"]
    proc.limit_text = "resets 8:40pm (UTC)"
    proc.last_output = now["t"] - 120
    await wait_for(lambda: rows(tl, "usage limit reached"))
    now["t"] += 20 * 60
    await proc.submit(direction)
    now["t"] += 1
    proc.limit_hit_at = now["t"]
    await tick()
    now["t"] += 30 * 60
    await wait_for(lambda: len(proc.submitted) == 3)
    msg = proc.submitted[2]
    await finish(stop, pump)
    assert direction in msg
    assert FINDINGS not in msg


@pytest.mark.asyncio
async def test_direction_after_reset_then_works(monkeypatch):
    now = _clock(monkeypatch, WALL0)
    proc = _Proc()
    proc.output_count = 0
    _bridge, tl, stop, pump = await start(proc)
    await proc.submit(FINDINGS)
    now["t"] += 33
    proc.limit_hit_at = now["t"]
    proc.limit_text = "resets 8:40pm (UTC)"
    proc.last_output = now["t"]
    await wait_for(lambda: rows(tl, "usage limit reached"))
    now["t"] = 10_000 + 38 * 60 + 26 + 10  # 10s after reset, not yet +60
    proc.last_output = now["t"]
    await tick()
    await proc.submit("Direction from human: go")
    for _ in range(4):
        now["t"] += 61
        proc.output_count += 5000
        proc.last_output = now["t"]
        await tick()
    n = len(proc.submitted)
    await finish(stop, pump)
    assert rows(tl, "running again")
    assert n == 2


@pytest.mark.asyncio
async def test_done_limit_cap(monkeypatch):
    now = _clock(monkeypatch, WALL0)
    proc = _Proc()
    proc.output_count = 0
    bridge, tl, stop, pump = await start(proc)
    bridge.push("DONE: delivered")
    await wait_for(lambda: "send direction" in (tl._activity or ""))
    await proc.submit("Please wrap up: run end-project")
    now["t"] += 10
    proc.limit_hit_at = now["t"]
    proc.limit_text = "resets 8:40pm (UTC)"
    proc.last_output = now["t"] - 120
    for _ in range(10):
        now["t"] += _session_config.LIMIT_RETRY_S + 61
        await tick()
    resends = rows(tl, "trying to resume")
    await finish(stop, pump)
    assert len(resends) == 2
    assert rows(tl, "a human needs to look")


@pytest.mark.asyncio
async def test_long_findings(monkeypatch):
    now = _clock(monkeypatch, WALL0)
    proc = _Proc()
    proc.output_count = 0
    _bridge, tl, stop, pump = await start(proc)
    big = "Not done yet. HEAD-MARK " + ("finding text " * 20_000) + " TAIL-MARK"
    await proc.submit(big)
    now["t"] += 33
    proc.limit_hit_at = now["t"]
    proc.limit_text = "resets 8:40pm (UTC)"
    proc.last_output = now["t"] - 120
    await wait_for(lambda: rows(tl, "usage limit reached"))
    now["t"] += 45 * 60
    await wait_for(lambda: len(proc.submitted) == 2)
    m = proc.submitted[1]
    await finish(stop, pump)
    assert "HEAD-MARK" in m and "TAIL-MARK" in m
    assert m.endswith(big)
    assert "[...truncated...]" not in m
    assert big[:20000] in m
    assert big[-4000:] in m


@pytest.mark.asyncio
async def test_note_during_pause_no_submit(monkeypatch):
    now = _clock(monkeypatch, WALL0)
    proc = _Proc()
    proc.output_count = 0
    bridge, tl, stop, pump = await start(proc)
    await proc.submit(FINDINGS)
    now["t"] += 33
    proc.limit_hit_at = now["t"]
    proc.limit_text = "resets 8:40pm (UTC)"
    proc.last_output = now["t"] - 120
    await wait_for(lambda: rows(tl, "usage limit reached"))
    now["t"] += 45 * 60
    bridge.push("working on FINDING-A")
    await tick(0.1)
    now["t"] += 3 * _session_config.LIMIT_RETRY_S
    await tick(0.1)
    n = len(proc.submitted)
    await finish(stop, pump)
    assert n == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "output"),
    [
        ("Reply with just: ok -- and do not change anything.", 0),
        ("Your usage limit should have reset now. Carry on exactly where you left off.", 0),
        ("Your usage limit has reset. Here it is again: " + FINDINGS, 0),
        (_supervise_support._NUDGE, 0),
        (_supervise_support._nudge_text(2, "outstanding work"), 0),
        (FINDINGS, _session_config.LIMIT_RESUMED_CHARS),
    ],
)
async def test_only_unanswered_task_messages_are_restated(monkeypatch, message, output):
    now = _clock(monkeypatch, WALL0)
    proc = _Proc()
    proc.output_count = 0
    _bridge, tl, stop, pump = await start(proc)
    await proc.submit(message)
    now["t"] += 33
    proc.output_count += output
    proc.limit_hit_at = now["t"]
    proc.limit_text = "resets 8:40pm (UTC)"
    proc.last_output = now["t"] - 120
    await wait_for(lambda: rows(tl, "usage limit reached"))
    now["t"] += 45 * 60
    await wait_for(lambda: len(proc.submitted) == 2)
    assert "Carry on exactly where you left off" in proc.submitted[1]
    assert "here it is again" not in proc.submitted[1]
    await finish(stop, pump)


@pytest.mark.asyncio
async def test_direction_before_reset_without_output_does_not_block_resume(monkeypatch):
    now = _clock(monkeypatch, WALL0)
    proc = _Proc()
    proc.output_count = 0
    _bridge, tl, stop, pump = await start(proc)
    await proc.submit(FINDINGS)
    now["t"] += 33
    proc.limit_hit_at = now["t"]
    proc.limit_text = "resets 8:40pm (UTC)"
    proc.last_output = now["t"] - 120
    await wait_for(lambda: rows(tl, "usage limit reached"))
    now["t"] += 20 * 60
    await proc.submit("Direction from human: continue")
    await tick()
    assert len(proc.submitted) == 2
    now["t"] += 25 * 60
    await wait_for(lambda: len(proc.submitted) == 3)
    assert "Carry on exactly where you left off" in proc.submitted[2]
    assert FINDINGS not in proc.submitted[2]
    await finish(stop, pump)


@pytest.mark.asyncio
async def test_same_notice_after_a_new_direction_starts_an_episode(monkeypatch):
    now = _clock(monkeypatch, WALL0)
    proc = _Proc()
    proc.output_count = 0
    bridge, tl, stop, pump = await start(proc)
    proc.limit_hit_at = now["t"]
    proc.limit_text = "resets 8:40pm (UTC)"
    await wait_for(lambda: rows(tl, "usage limit reached"))
    bridge.push("working again")
    await wait_for(lambda: rows(tl, "running again"))
    now["t"] += 10
    await proc.submit("Direction from human: fix the next issue")
    now["t"] += 1
    proc.limit_hit_at = now["t"]
    await wait_for(lambda: len(rows(tl, "usage limit reached")) == 2)
    await finish(stop, pump)
