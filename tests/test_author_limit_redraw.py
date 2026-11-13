"""A redrawn usage-limit notice must not leave a dual-engine author paused for good."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import flyte.report
import pytest

from flyte_agent_factory import (
    _author,
    _checkpoint,
    _inbox,
    _session_config,
    _supervise_support,
    _timeline,
    _usage,
)
from flyte_agent_factory._author_session import AuthorSession
from tests._wait import wait_for
from tests.test_limit_resume import FINDINGS, WALL0, _clock, finish, rows, start, tick
from tests.test_nudge import _agent_time, _Proc

pytestmark = pytest.mark.real_reset

NOTICE = "resets 8:40pm (UTC)"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    async def _aio(*_a, **_k) -> None:
        return None

    monkeypatch.setattr(flyte.report, "replace", type("R", (), {"aio": staticmethod(_aio)}))
    monkeypatch.setattr(_session_config, "POLL_EVERY_S", 0.0)
    monkeypatch.setattr(_supervise_support, "_in_flight_children", lambda: (0, ""))
    monkeypatch.setattr(_session_config, "NUDGE_AFTER_S", 1e12)


class _Engine(_Proc):
    output_count = 0
    returncode: int | None = None

    async def stop(self, *, grace: float = 20.0) -> None:
        pass


class _Session(AuthorSession):
    """The supervisor sets `on_limit` on the real session; the tests count its calls."""

    on_limit_calls: int
    on_limit: Callable[..., Awaitable[bool]]


def _session(tmp_path):
    session = _Session(_Engine(), tmp_path / "author-pending.json")
    session.on_limit_calls = 0

    async def on_limit(reset_at, interrupted):
        # Same engine, nothing to hand off to: the pause just waits for its own reset.
        session.on_limit_calls += 1
        session.retry_at = reset_at
        return False

    session.on_limit = on_limit
    return session


def _refuse(session, now, text=NOTICE):
    session.current.limit_hit_at = now["t"]
    session.current.limit_text = text


async def _paused_then_resumed(session, now):
    """Refusal, then the pump's resume once LIMIT_RETRY_S has passed."""
    _bridge, tl, stop, pump = await start(session)
    await session.submit(FINDINGS)
    now["t"] += 33
    _refuse(session, now)
    session.current.last_output = now["t"] - 120
    await wait_for(lambda: rows(tl, "usage limit reached"))
    await wait_for(lambda: session._paused)
    await session.submit("review findings / direction")
    assert session.pending == ["review findings / direction"]
    now["t"] += 46 * 60  # past LIMIT_RETRY_S and past the notice's reset
    # resume() carries the pending message, so it is delivered here already.
    await wait_for(lambda: any("review findings" in s for s in session.current.submitted))
    return tl, stop, pump


@pytest.mark.asyncio
async def test_redrawn_notice_after_resume_does_not_repause(tmp_path, monkeypatch):
    now = _clock(monkeypatch, WALL0)
    session = _session(tmp_path)
    tl, stop, pump = await _paused_then_resumed(session, now)
    calls = session.on_limit_calls
    now["t"] += 1
    _refuse(session, now)  # the TUI redraws the notice that was already handled
    session.current.output_count = 20000
    now["t"] += 120
    await tick(0.1)
    await session.submit("later direction")
    await finish(stop, pump)
    assert not session._paused
    assert session.pending == []
    assert session.on_limit_calls == calls
    delivered = session.current.submitted
    assert sum("review findings / direction" in s for s in delivered) == 1
    assert delivered[-1] == "later direction"


@pytest.mark.asyncio
async def test_running_again_unpauses_and_drains_pending_once(tmp_path, monkeypatch):
    now = _clock(monkeypatch, WALL0)
    session = _session(tmp_path)
    _bridge, tl, stop, pump = await start(session)
    await session.submit(FINDINGS)
    now["t"] += 33
    _refuse(session, now)
    session.current.last_output = now["t"] - 120
    await wait_for(lambda: session._paused)
    # The session recovers on its own before any resume is due.
    await session.submit("queued direction")
    session.current.output_count = 20000
    now["t"] += 46 * 60  # past the notice's reset; the work output arrived on its own
    session.current.last_submission = ("x", now["t"], 0)
    session.current.last_output = now["t"]  # not quiet, so no resume is sent
    await wait_for(lambda: rows(tl, "running again"))
    await wait_for(lambda: not session._paused)
    await tick(0.1)
    await finish(stop, pump)
    assert session.pending == []
    assert sum("queued direction" in s for s in session.current.submitted) == 1


@pytest.mark.asyncio
async def test_on_limit_runs_once_per_refusal(tmp_path, monkeypatch):
    now = _clock(monkeypatch, WALL0)
    session = _session(tmp_path)
    _bridge, tl, stop, pump = await start(session)
    await session.submit(FINDINGS)
    now["t"] += 33
    _refuse(session, now)
    session.current.last_output = now["t"] - 120
    await wait_for(lambda: session.on_limit_calls)
    for _ in range(20):
        await tick(0.005)
    await finish(stop, pump)
    assert session.on_limit_calls == 1


@pytest.mark.asyncio
async def test_a_new_refusal_after_resume_still_pauses(tmp_path, monkeypatch):
    now = _clock(monkeypatch, WALL0)
    session = _session(tmp_path)
    tl, stop, pump = await _paused_then_resumed(session, now)
    assert not session._paused
    calls = session.on_limit_calls
    now["t"] += 5
    _refuse(session, now, "resets 11:15pm (UTC)")  # a different notice: the resume was refused
    await wait_for(lambda: session.on_limit_calls == calls + 1)
    assert session._paused
    await session.submit("held back")
    await finish(stop, pump)
    # The refused resume had drained the first direction; it is restored ahead of the new one.
    assert session.pending == ["review findings / direction", "held back"]


@pytest.mark.asyncio
async def test_claude_only_slot_is_unchanged(monkeypatch):
    """No AuthorSession: the pump never touches pause state, and a speaking session ends the pause."""
    now = _clock(monkeypatch, WALL0)
    proc = _Engine()
    _bridge, tl, stop, pump = await start(proc)
    await proc.submit(FINDINGS)
    now["t"] += 33
    proc.limit_hit_at, proc.limit_text = now["t"], NOTICE
    proc.last_output = now["t"] - 120
    await wait_for(lambda: rows(tl, "usage limit reached"))
    now["t"] += 46 * 60
    await wait_for(lambda: len(proc.submitted) == 2)
    assert not hasattr(proc, "_paused")
    await finish(stop, pump)
    assert len(rows(tl, "usage limit reached")) == 1


# --------------------------------------------------------------------------- #
# A deferred handoff is re-routed when its retry arrives (QA finding on chunk 2)
# --------------------------------------------------------------------------- #


class _Stoppable(_Engine):
    async def stop(self, *, grace: float = 20.0) -> None:
        self.returncode = 0


def _dual_engine_rig(tmp_path, monkeypatch, limits):
    """A session whose on_limit is the real `_author_handoff`; `limits` maps engine -> limited_until."""
    from flyte_agent_factory import _meter, _state

    work = tmp_path / "workspace"
    work.mkdir()
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(_session_config, "WORKSPACE", work)
    monkeypatch.setattr(_session_config, "CONFIG_DIR", tmp_path / "claude")
    monkeypatch.setattr(_session_config, "CODEX_HOME_DIR", tmp_path / "codex")
    monkeypatch.setattr(_author, "_CODEX_THREAD_FILE", tmp_path / "codex-thread")
    for name, value in (
        ("_author_engine", "claude"),
        ("_meters", None),
        ("_hard_pins", False),
    ):
        monkeypatch.setattr(_usage, name, value)
    monkeypatch.setattr(_inbox, "_directions", [])
    monkeypatch.setattr(_usage, "_author_engines", lambda: ("claude", "codex"))
    monkeypatch.setattr(
        _usage,
        "_child_meter",
        lambda e: _meter.Meter(e, "test", _agent_time().time(), limited_until=limits.get(e)),
    )

    async def noop(*_a, **_k):
        return True

    async def no_git(*_a, **_k):
        return ""

    monkeypatch.setattr(_state, "save_checkpoint", noop)
    monkeypatch.setattr(_checkpoint, "_handoff_git_out", no_git)
    session = _Session(_Stoppable(), tmp_path / "author-pending.json")
    handoff = _author._author_handoff(
        session, _state.ProjectState(tmp_path, ""), _timeline._Timeline(), brief="brief"
    )
    session.on_limit_calls = 0

    async def on_limit(reset_at, interrupted):
        session.on_limit_calls += 1
        return await handoff(reset_at, interrupted)

    session.on_limit = on_limit
    return session


async def _limited_with_codex_first(session, now, limits):
    """Claude refuses (reset ~20:40) while Codex is limited until now + 100; a direction is queued."""
    _bridge, tl, stop, pump = await start(session)
    await session.submit(FINDINGS)
    now["t"] += 33
    limits["codex"] = _agent_time().time() + 100
    _refuse(session, now)
    session.current.last_output = now["t"] - 120
    await wait_for(lambda: session.on_limit_calls == 1)
    await wait_for(lambda: session._paused)
    await session.submit("a direction")
    return tl, stop, pump


@pytest.mark.asyncio
async def test_deferred_retry_hands_over_when_the_other_engine_reset_first(tmp_path, monkeypatch):
    now = _clock(monkeypatch, WALL0)
    limits: dict[str, float] = {}
    session = _dual_engine_rig(tmp_path, monkeypatch, limits)
    tl, stop, pump = await _limited_with_codex_first(session, now, limits)
    limits.pop("codex")  # Codex resets; Claude's own reset (~20:40) is still ahead
    now["t"] += 200
    with pytest.raises(_author.AuthorHandoffRestart):
        await asyncio.wait_for(pump, timeout=30)
    assert session.on_limit_calls == 2
    assert _session_config._author_handoff_records()[0]["to"] == "codex"
    assert _session_config._author_handoff_records()[0]["pending"] == [FINDINGS, "a direction"]
    assert session.current.submitted == [FINDINGS]  # nothing was sent to the limited engine


@pytest.mark.asyncio
async def test_deferred_retry_with_both_engines_limited_waits_for_the_next_reset(tmp_path, monkeypatch):
    now = _clock(monkeypatch, WALL0)
    limits: dict[str, float] = {}
    session = _dual_engine_rig(tmp_path, monkeypatch, limits)
    tl, stop, pump = await _limited_with_codex_first(session, now, limits)
    limits["codex"] = _agent_time().time() + 200 + 500  # still limited, with a later reset
    now["t"] += 200
    await wait_for(lambda: session.on_limit_calls == 2)
    for _ in range(20):
        await tick(0.005)
    await finish(stop, pump)
    assert session.on_limit_calls == 2  # re-deferred once, not on every tick
    assert session._paused
    assert session.pending == [FINDINGS, "a direction"]  # the interrupted message is kept too
    assert session.current.submitted == [FINDINGS]
    assert session.retry_at == limits["codex"]


@pytest.mark.asyncio
async def test_refused_resume_puts_what_it_drained_back_exactly_once(tmp_path, monkeypatch):
    import json

    now = _clock(monkeypatch, WALL0)
    session = _session(tmp_path)
    tl, stop, pump = await _paused_then_resumed(session, now)
    assert session.pending == []
    calls = session.on_limit_calls
    now["t"] += 5
    _refuse(session, now, "resets 11:15pm (UTC)")
    await wait_for(lambda: session.on_limit_calls == calls + 1)
    for _ in range(10):
        await tick(0.005)
    await finish(stop, pump)
    assert session.pending == ["review findings / direction"]
    assert json.loads((tmp_path / "author-pending.json").read_text()) == session.pending


@pytest.mark.asyncio
async def test_same_notice_before_the_engines_own_reset_is_a_refusal(tmp_path, monkeypatch):
    now = _clock(monkeypatch, WALL0)
    session = _session(tmp_path)

    async def on_limit(reset_at, interrupted):
        session.on_limit_calls += 1
        # First call: the other engine resets in 100 s. Later: the route says go ahead.
        session.retry_at = _agent_time().time() + 100 if session.on_limit_calls == 1 else 0
        return False

    session.on_limit = on_limit
    _bridge, tl, stop, pump = await start(session)
    await session.submit(FINDINGS)
    now["t"] += 33
    _refuse(session, now)  # the notice's own reset is ~20:40, far ahead
    session.current.last_output = now["t"] - 120
    await wait_for(lambda: session._paused)
    await session.submit("a direction")
    now["t"] += 200
    await wait_for(lambda: any("a direction" in s for s in session.current.submitted))
    calls = session.on_limit_calls
    now["t"] += 1
    _refuse(session, now)  # identical notice, but the resume went out before the reset
    await wait_for(lambda: session.on_limit_calls == calls + 1)
    await finish(stop, pump)
    assert session._paused
    assert session.pending == ["a direction"]
