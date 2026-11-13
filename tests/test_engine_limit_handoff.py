"""The coordinator's usage-limit handoff: routing, pins, directions, visibility and the switch (#125)."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import subprocess
from typing import Any

import flyte.report
import pytest

from flyte_agent_factory import (
    _author,
    _checkpoint,
    _inbox,
    _meter,
    _pins,
    _router,
    _session_config,
    _state,
    _subagent_run,
    _supervise,
    _supervise_support,
    _timeline,
    _usage,
    agent,  # also sets `_author.project_task`, whose retry budget the handoff reads
)
from tests._wait import wait_for
from tests.test_author_limit_redraw import _Session, _Stoppable
from tests.test_limit_resume import FINDINGS, WALL0, _clock, finish, rows, tick
from tests.test_nudge import _agent_time, _Queue

pytestmark = pytest.mark.real_reset

CLAUDE_ACCOUNT = "claude test-token"


def _at(hour: int, minute: int = 0, day: int = 19) -> float:
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=dt.timezone.utc).timestamp()


CLAUDE_RESET = _at(22, 40)  # "resets 10:40pm (UTC)" seen on Mon 19 Oct
CLAUDE_NOTICE = "You've hit your limit · resets 10:40pm (UTC)"
CODEX_WEEKLY = _at(9, 0, day=24)  # five days ahead
CODEX_NOTICE = "You've hit your usage limit. Try again at Oct 24th, 2026 9:00 AM."


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    async def _aio(*_a, **_k) -> None:
        return None

    monkeypatch.setattr(flyte.report, "replace", type("R", (), {"aio": staticmethod(_aio)}))
    monkeypatch.setattr(_session_config, "POLL_EVERY_S", 0.0)
    monkeypatch.setattr(_supervise_support, "_in_flight_children", lambda: (0, ""))
    monkeypatch.setattr(_session_config, "NUDGE_AFTER_S", 1e12)
    for key in (
        "AGENT_FACTORY_COORDINATOR_ENGINE",
        "AGENT_FACTORY_AUTHOR_ENGINE",
        "AGENT_FACTORY_ENGINE_FALLBACK",
        "AGENT_FACTORY_ADMITTED_ENGINE",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(_pins, "engine_input", "")


class _Owner:
    """A bridge that records what the supervisor asked the owner."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def notify_owner(self, text: str, timeout_s: float) -> str:
        self.asked.append(text)
        return "q-1"


class _Rig:
    def __init__(
        self, session: _Session, timeline: _timeline._Timeline, handoff: Any, checkpoints: list[Any]
    ):
        self.session, self.timeline, self.handoff, self.checkpoints = session, timeline, handoff, checkpoints

    @property
    def proc(self) -> Any:
        """The fake engine process, with the attributes a test sets on it."""
        return self.session.current

    def refuse(self, text: str) -> None:
        """The engine process shows a limit notice now, as the TUI or Codex's event stream would."""
        self.proc.limit_hit_at = _agent_time().monotonic()
        self.proc.limit_text = text

    def records(self) -> list[dict[str, Any]]:
        return _session_config._author_handoff_records()

    def rows(self, text: str) -> list[str]:
        return list[str](rows(self.timeline, text))


def _rig(tmp_path, monkeypatch, *, engine: str, limits: dict[str, float] | None = None, store: bool = False):
    """A session whose `on_limit` is the real `_author_handoff`, on one shared timeline.

    `limits` maps engine -> limited_until as the meters would report it; with `store`, a real `MeterStore`
    holds the meters instead (refusals are written to it, and it never shortens a known reset).
    """
    work = tmp_path / "workspace"
    work.mkdir()
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(_session_config, "WORKSPACE", work)
    monkeypatch.setattr(_session_config, "CONFIG_DIR", tmp_path / "claude")
    monkeypatch.setattr(_session_config, "CODEX_HOME_DIR", tmp_path / "codex")
    monkeypatch.setattr(_session_config, "_claude_account", lambda: CLAUDE_ACCOUNT)
    monkeypatch.setattr(_author, "_CODEX_THREAD_FILE", tmp_path / "codex-thread")
    monkeypatch.setattr(_usage, "_author_engine", engine)
    monkeypatch.setattr(_usage, "_hard_pins", False)
    monkeypatch.setattr(_usage, "_author_engines", lambda: ("claude", "codex"))
    monkeypatch.setattr(_inbox, "_directions", [])

    async def no_meter_read(*_a, **_k) -> None:
        return None

    monkeypatch.setattr(_usage, "_read_meters", no_meter_read)
    if store:
        meters = _meter.MeterStore(tmp_path / "meters", clock=lambda: _agent_time().time())
        monkeypatch.setattr(_usage, "_meters", meters)
    else:
        monkeypatch.setattr(_usage, "_meters", None)
        known = limits if limits is not None else {}
        monkeypatch.setattr(
            _usage,
            "_child_meter",
            lambda e: _meter.Meter(e, "test", _agent_time().time(), limited_until=known.get(e)),
        )
    checkpoints: list[bool] = []

    async def checkpoint(*_a, **_k):
        checkpoints.append(True)
        return True

    async def no_git(*_a, **_k):
        return ""

    monkeypatch.setattr(_state, "save_checkpoint", checkpoint)
    monkeypatch.setattr(_checkpoint, "_handoff_git_out", no_git)
    timeline = _timeline._Timeline()
    session = _Session(_Stoppable(), tmp_path / "author-pending.json")
    handoff = _author._author_handoff(session, _state.ProjectState(tmp_path, ""), timeline, brief="brief")
    session.on_limit_calls = 0

    async def on_limit(reset_at, interrupted):
        session.on_limit_calls += 1
        return await handoff(reset_at, interrupted)

    session.on_limit = on_limit
    return _Rig(session, timeline, handoff, checkpoints)


async def _start_pump(rig: _Rig):
    stop = asyncio.Event()
    pump = asyncio.create_task(_supervise._pump(_Queue(), rig.session, rig.timeline, stop))  # type: ignore[arg-type]
    return stop, pump


async def _refused_in_pump(rig: _Rig, now: dict[str, float], text: str):
    """Start the pump, send the findings, then have the engine refuse them; returns (stop, pump)."""
    stop, pump = await _start_pump(rig)
    await rig.session.submit(FINDINGS)
    now["t"] += 33
    rig.refuse(text)
    rig.proc.last_output = now["t"] - 120
    await wait_for(lambda: rig.session.on_limit_calls == 1)
    await wait_for(lambda: rig.session._paused)
    return stop, pump


def _resumes(rig: _Rig) -> list[str]:
    return [s for s in rig.proc.submitted if s.startswith("Your usage limit")]


async def _settle(n: int = 20) -> None:
    for _ in range(n):
        await tick(0.005)


# --------------------------------------------------------------------------- #
# (a) the router decides every resume


@pytest.mark.asyncio
async def test_unpinned_codex_limit_resumes_on_claude(tmp_path, monkeypatch):
    now = _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="codex")
    stop, pump = await _start_pump(rig)
    await rig.session.submit(FINDINGS)
    now["t"] += 33
    rig.refuse(CODEX_NOTICE)
    rig.proc.last_output = now["t"] - 120
    with pytest.raises(_author.AuthorHandoffRestart):
        await asyncio.wait_for(pump, timeout=30)
    (record,) = rig.records()
    assert (record["from"], record["to"]) == ("codex", "claude")
    assert record["reset_at"] == CODEX_WEEKLY
    assert rig.session.pending[0].startswith("AUTHOR HANDOFF: codex -> claude")
    assert _resumes(rig) == []  # nothing was ever sent to the limited engine


@pytest.mark.asyncio
async def test_router_consulted_every_resume(tmp_path, monkeypatch):
    """Codex's reset is unknown and Claude is limited for 40 minutes. Codex is retried once its guessed
    reset passes; when it refuses again, the next wake is Claude's reset, not another Codex retry."""
    now = _clock(monkeypatch, WALL0)
    claude_until = WALL0 + 40 * 60
    rig = _rig(tmp_path, monkeypatch, engine="codex", limits={"claude": claude_until})
    stop, pump = await _refused_in_pump(rig, now, "You've hit your usage limit.")
    assert rig.session.retry_at == pytest.approx(WALL0 + 33 + _session_config.LIMIT_RETRY_S)
    now["t"] += 32 * 60  # past the guessed Codex reset; Claude is still limited
    await wait_for(lambda: len(_resumes(rig)) == 1)
    assert rig.session.on_limit_calls == 2
    assert rig.records() == []
    now["t"] += 400  # well clear of the resume, so this is a new refusal and not its redraw
    rig.refuse("Codex: usage limit reached again.")
    await wait_for(lambda: rig.session.on_limit_calls == 3)
    await wait_for(lambda: rig.session._paused)
    assert rig.session.retry_at == claude_until  # Claude resets before Codex's fresh 30-minute guess
    now["t"] = claude_until - (WALL0 - 10_000.0) + _session_config.LIMIT_RETRY_S
    with pytest.raises(_author.AuthorHandoffRestart):
        await asyncio.wait_for(pump, timeout=30)
    assert len(_resumes(rig)) == 1  # Codex was not retried a second time
    assert rig.records()[-1]["to"] == "claude"


@pytest.mark.asyncio
async def test_wakes_at_other_engines_reset(tmp_path, monkeypatch):
    now = _clock(monkeypatch, WALL0)
    codex_until = _at(21, 0)
    rig = _rig(tmp_path, monkeypatch, engine="claude", limits={"codex": codex_until})
    stop, pump = await _refused_in_pump(rig, now, CLAUDE_NOTICE)
    assert rig.session.retry_at == codex_until  # Codex resets before Claude's 22:40
    offset = WALL0 - 10_000.0  # wall minus monotonic in the fake clock
    now["t"] = codex_until - offset + 30
    await _settle()
    assert rig.session.on_limit_calls == 1 and rig.records() == []
    now["t"] = codex_until - offset + 61
    with pytest.raises(_author.AuthorHandoffRestart):
        await asyncio.wait_for(pump, timeout=30)
    assert rig.records()[0]["to"] == "codex"
    assert _resumes(rig) == []


def test_refused_engine_is_not_preferred(tmp_path, monkeypatch):
    """With no meters at all and the refusal's reset already past, both engines look usable: the router
    must not stay on the one that just refused (#125, R1)."""
    _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="codex", limits={})
    decision = _author._author_refusal_route(_pins.Pin("", False, ""), rig.timeline, "codex", WALL0 - 60)
    assert decision.engine == "claude"
    decision = _author._author_refusal_route(_pins.Pin("", False, ""), rig.timeline, "claude", WALL0 - 60)
    assert decision.engine == "codex"


# --------------------------------------------------------------------------- #
# R2: a known reset is never shortened


@pytest.mark.asyncio
async def test_repeat_refusal_keeps_known_weekly_reset(tmp_path, monkeypatch):
    now = _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="codex", store=True)
    meters = _usage._meters
    assert meters is not None
    meters.note_limit("codex", CODEX_WEEKLY, "")  # the weekly refusal a delegate saw earlier
    meters.note_limit("claude", CODEX_WEEKLY + 86400, CLAUDE_ACCOUNT)  # Claude is out for longer still
    rig.refuse("You've hit your usage limit.")
    assert await rig.handoff(WALL0 + 3600, "") is False  # a 5-hour notice must not shorten the week
    assert meters.latest["codex"].limited_until == CODEX_WEEKLY
    assert rig.session.retry_at == CODEX_WEEKLY
    owner = json.loads((tmp_path / "author-conversation.json").read_text())
    assert owner["limited_until"] == CODEX_WEEKLY
    now["t"] += 600
    rig.refuse("You've hit your usage limit. Try again later.")
    assert await rig.handoff(None, "") is False  # a refusal with no time at all: no 30-minute guess
    assert meters.latest["codex"].limited_until == CODEX_WEEKLY
    assert rig.session.retry_at == CODEX_WEEKLY
    assert rig.rows("waiting for codex until Sat 24 Oct 09:00 UTC")


@pytest.mark.asyncio
async def test_pump_passes_fresh_reset_not_retry_at(tmp_path, monkeypatch):
    """The pump hands the handoff each refusal's own reset, never the retry time of the last deferral,
    and None when it re-evaluates a deferral or the notice's time is already past."""
    now = _clock(monkeypatch, WALL0)
    session = _Session(_Stoppable(), tmp_path / "author-pending.json")
    calls: list[float | None] = []

    async def on_limit(reset_at, interrupted):
        calls.append(reset_at)
        # A refusal defers 100 s (the other engine's reset); the re-evaluation then resumes this one.
        session.retry_at = _agent_time().time() + 100 if reset_at is not None or len(calls) == 1 else 0
        return False

    session.on_limit = on_limit
    rig = _Rig(session, _timeline._Timeline(), None, [])
    stop, pump = await _start_pump(rig)
    await session.submit(FINDINGS)
    now["t"] += 33
    rig.refuse("resets 8:40pm (UTC)")
    rig.proc.last_output = now["t"] - 120
    await wait_for(lambda: len(calls) == 1)
    assert calls == [_at(20, 40)]
    now["t"] += 200
    await wait_for(lambda: len(_resumes(rig)) == 1)
    assert calls[1:] == [None]
    now["t"] += 400
    rig.refuse("resets 11:15pm (UTC)")
    await wait_for(lambda: len(calls) == 3)
    assert calls[2] == _at(23, 15)  # not the 100-second retry time of the deferral before
    now["t"] += _session_config.LIMIT_RETRY_S + 60  # a second try in this episode waits a full interval
    await wait_for(lambda: len(calls) == 4)
    assert calls[3] is None
    await wait_for(lambda: len(_resumes(rig)) == 2)
    now["t"] += 400
    rig.refuse("resets 8:00pm (UTC)")  # seen at ~20:45: already past, so stale
    await wait_for(lambda: len(calls) == 5)
    await finish(stop, pump)
    assert calls[4] is None


# --------------------------------------------------------------------------- #
# Pins: COORDINATOR_ENGINE is hard unless ENGINE_FALLBACK="allow"


def _routes(monkeypatch) -> list[dict[str, Any]]:
    """Record the keyword arguments of every router call."""
    seen: list[dict[str, Any]] = []
    route = _router.route

    def spy(*args, **kwargs):
        seen.append(kwargs)
        return route(*args, **kwargs)

    monkeypatch.setattr(_router, "route", spy)
    return seen


@pytest.mark.asyncio
async def test_handoff_reads_coordinator_engine_not_author_alias(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_COORDINATOR_ENGINE", "claude")
    _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="claude", limits={})
    rig.refuse(CLAUDE_NOTICE)
    assert await rig.handoff(CLAUDE_RESET, "") is False  # Codex is free, but the pin holds
    assert rig.records() == []
    assert rig.rows("coordinator pinned to claude by COORDINATOR_ENGINE")


@pytest.mark.asyncio
async def test_profile_pin_is_hard_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_COORDINATOR_ENGINE", "claude")
    _clock(monkeypatch, WALL0)
    assert _pins.author_pin() == _pins.Pin("claude", True, "COORDINATOR_ENGINE")
    rig = _rig(tmp_path, monkeypatch, engine="claude", limits={})
    seen = _routes(monkeypatch)
    rig.refuse(CLAUDE_NOTICE)
    await rig.handoff(CLAUDE_RESET, "")
    assert seen and all(call["pin"] == "claude" and call["hard_pin"] is True for call in seen)
    seen.clear()
    assert await _author._choose_author_engine(_pins.author_pin(), ("claude", "codex"), False, rig.timeline)
    assert seen and seen[-1]["hard_pin"] is True


@pytest.mark.asyncio
async def test_hard_pin_never_moves_to_codex(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_COORDINATOR_ENGINE", "claude")
    now = _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="claude", limits={})
    rig.refuse(CLAUDE_NOTICE)
    for _ in range(3):  # the refusal, then re-evaluations at later pump ticks
        assert await rig.handoff(CLAUDE_RESET, "") is False
        assert rig.session.retry_at == CLAUDE_RESET
        now["t"] += 31 * 60
    assert rig.records() == []
    assert not rig.rows("author -> codex")
    assert rig.rows("router: author -> deferred")


@pytest.mark.asyncio
async def test_engine_fallback_allow_restores_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_COORDINATOR_ENGINE", "claude")
    monkeypatch.setenv("AGENT_FACTORY_ENGINE_FALLBACK", "allow")
    _clock(monkeypatch, WALL0)
    assert _pins.author_pin() == _pins.Pin("claude", False, "COORDINATOR_ENGINE")
    rig = _rig(tmp_path, monkeypatch, engine="claude", limits={})
    rig.refuse(CLAUDE_NOTICE)
    with pytest.raises(_author.AuthorHandoffRestart):
        await rig.handoff(CLAUDE_RESET, "")
    assert rig.records()[0]["to"] == "codex"
    assert rig.rows("author -> codex")


@pytest.mark.asyncio
async def test_claude_pinned_waits_through_codex_weekly_and_resumes_at_claude_reset(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_COORDINATOR_ENGINE", "claude")
    now = _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="claude", limits={"codex": CODEX_WEEKLY})
    stop, pump = await _refused_in_pump(rig, now, CLAUDE_NOTICE)
    assert rig.session.retry_at == CLAUDE_RESET
    offset = WALL0 - 10_000.0
    now["t"] = CLAUDE_RESET - offset - 60  # an hour of 30-minute intervals pass first
    await _settle()
    assert _resumes(rig) == []
    now["t"] = CLAUDE_RESET - offset + 61
    await wait_for(lambda: len(_resumes(rig)) == 1)
    assert not pump.done()  # resumed in place: no restart, no handoff
    await finish(stop, pump)
    assert rig.records() == []
    assert not rig.rows("author -> codex")
    assert not rig.rows("a human needs to look")


@pytest.mark.asyncio
async def test_first_pinned_deferral_row_names_pin_and_key(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_COORDINATOR_ENGINE", "claude")
    now = _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="claude", limits={})
    rig.refuse(CLAUDE_NOTICE)
    for _ in range(3):
        await rig.handoff(CLAUDE_RESET, "")
        now["t"] += 60
    assert rig.rows("coordinator pinned") == [
        'coordinator pinned to claude by COORDINATOR_ENGINE; ENGINE_FALLBACK is not "allow", '
        "so it waits for claude until Mon 19 Oct 22:40 UTC instead of moving to codex"
    ]


# --------------------------------------------------------------------------- #
# (c) a direction to resume on another engine


@pytest.mark.asyncio
async def test_resume_on_claude_direction_switches_within_one_interval(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_COORDINATOR_ENGINE", "codex")
    now = _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="codex", limits={})
    stop, pump = await _refused_in_pump(rig, now, CODEX_NOTICE)
    assert rig.session.retry_at == CODEX_WEEKLY  # pinned to Codex: waits out its week
    session_future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    session_future.set_result(rig.session)
    row = _author._direct_engine(session_future, "claude")
    assert row == "engine direction: resume on claude; re-routing the paused session now"
    now["t"] += 5  # far less than one retry interval
    with pytest.raises(_author.AuthorHandoffRestart):
        await asyncio.wait_for(pump, timeout=30)
    (record,) = rig.records()
    assert (record["from"], record["to"]) == ("codex", "claude")
    assert record["reason"] == "direction: resume on claude (usage limit)"
    assert rig.rows("direction: resume on claude")
    assert _resumes(rig) == []


@pytest.mark.asyncio
async def test_resume_on_limited_engine_answers_with_its_reset(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_COORDINATOR_ENGINE", "codex")
    now = _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="codex", limits={"claude": CLAUDE_RESET})
    stop, pump = await _refused_in_pump(rig, now, CODEX_NOTICE)
    session_future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    session_future.set_result(rig.session)
    _author._direct_engine(session_future, "claude")
    now["t"] += 5
    await wait_for(lambda: rig.session.on_limit_calls == 2)
    await wait_for(lambda: rig.session.retry_at == CLAUDE_RESET)
    assert rig.rows(
        "coordinator directed to resume on claude, so it waits until Mon 19 Oct 22:40 UTC "
        "instead of moving to codex"
    )
    assert rig.rows("coordinator paused, waiting for claude until Mon 19 Oct 22:40 UTC")
    assert rig.records() == []
    now["t"] = CLAUDE_RESET - (WALL0 - 10_000.0) + 61
    with pytest.raises(_author.AuthorHandoffRestart):
        await asyncio.wait_for(pump, timeout=30)
    assert rig.records()[0]["to"] == "claude"


def test_direction_while_running_takes_effect_at_the_next_pause(tmp_path, monkeypatch):
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path)
    session = _Session(_Stoppable(), tmp_path / "author-pending.json")
    loop = asyncio.new_event_loop()
    try:
        session_future = loop.create_future()
        session_future.set_result(session)
        assert _author._direct_engine(session_future, "codex") == (
            "engine direction: next pause or restart runs on codex"
        )
    finally:
        loop.close()
    assert not getattr(session, "reroute_requested", False)
    assert _pins.author_pin() == _pins.Pin("codex", True, "direction")


# --------------------------------------------------------------------------- #
# D6: once the switch allowance is spent, wait for the real reset


@pytest.mark.asyncio
async def test_allowance_spent_waits_for_real_reset_not_30_minutes(tmp_path, monkeypatch):
    _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="claude", limits={})
    spent = [{"run": "local", "from": "codex", "to": "claude"}] * _author._AUTHOR_HANDOFF_MAX_RESTARTS
    monkeypatch.setattr(_session_config, "_author_handoff_records", lambda: spent)
    rig.refuse(CLAUDE_NOTICE)
    assert await rig.handoff(CLAUDE_RESET, "") is False  # Codex is free, but no switch is left
    assert rig.session.retry_at == CLAUDE_RESET
    assert rig.rows("engine switches used up") == [
        "engine switches used up (8/8); staying on claude until its reset"
    ]
    await rig.handoff(None, "")
    assert len(rig.rows("engine switches used up")) == 1


@pytest.mark.asyncio
async def test_allowance_spent_with_an_unknown_reset_resumes_the_same_engine(tmp_path, monkeypatch):
    now = _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="claude", limits={})
    spent = [{"run": "local"}] * _author._AUTHOR_HANDOFF_MAX_RESTARTS
    monkeypatch.setattr(_session_config, "_author_handoff_records", lambda: spent)
    rig.refuse("You've hit your limit")
    assert await rig.handoff(None, "") is False
    assert rig.session.retry_at == pytest.approx(WALL0 + _session_config.LIMIT_RETRY_S)
    now["t"] += _session_config.LIMIT_RETRY_S + 60
    # The poll: the same engine is resumed in place, with no restart spent.
    assert await rig.handoff(None, "") is False
    assert rig.session.retry_at < _agent_time().time()


# --------------------------------------------------------------------------- #
# Last resort and visibility


@pytest.mark.asyncio
async def test_needs_human_not_raised_while_reset_known(tmp_path, monkeypatch):
    now = _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="claude", limits={"codex": CODEX_WEEKLY})
    owner = _Owner()
    rig.timeline.bridge = owner
    rig.refuse(CLAUDE_NOTICE)
    for _ in range(3):
        assert await rig.handoff(CLAUDE_RESET, "") is False
        now["t"] += 31 * 60
    assert owner.asked == []
    assert not rig.rows("a human needs to look")


@pytest.mark.asyncio
async def test_needs_human_names_each_engine_and_fix(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_COORDINATOR_ENGINE", "codex")
    now = _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="codex", store=True)
    meters = _usage._meters
    assert meters is not None
    meters.note_limit("codex", WALL0 + 3600, "", error="401 Unauthorized")
    owner = _Owner()
    rig.timeline.bridge = owner
    rig.refuse("You've hit your usage limit.")
    for _ in range(3):
        assert await rig.handoff(None, "") is False
        now["t"] += 60
    expected = (
        'claude: not used, pinned to codex -- set ENGINE_FALLBACK="allow" to use it; '
        "codex: login rejected (401 Unauthorized) -- replace the [secrets] codex_token secret"
    )
    assert rig.rows("a human needs to look") == [f"a human needs to look: {expected}"]
    assert len(owner.asked) == 1 and expected in owner.asked[0]
    assert rig.timeline._phase["paused"]["needs_human"] == expected


@pytest.mark.asyncio
async def test_paused_row_names_engine_until_and_reasons(tmp_path, monkeypatch):
    now = _clock(monkeypatch, WALL0)
    codex_until = _at(9, 0, day=23)
    rig = _rig(tmp_path, monkeypatch, engine="claude", limits={"codex": codex_until})
    rig.refuse(CLAUDE_NOTICE)
    for _ in range(3):  # re-evaluations that change nothing write nothing
        await rig.handoff(CLAUDE_RESET, "")
        now["t"] += 60
    assert rig.rows("coordinator paused") == [
        "coordinator paused, waiting for claude until Mon 19 Oct 22:40 UTC; "
        "not using codex: limited until 2026-10-23 09:00 UTC"
    ]
    assert rig.timeline._phase["kind"] == "limited"
    assert rig.timeline._phase["limited_until"] == CLAUDE_RESET
    assert rig.timeline._phase["paused"] == {
        "engine": "claude",
        "until": CLAUDE_RESET,
        "others": {"codex": "limited until 2026-10-23 09:00 UTC"},
        "needs_human": "",
    }


@pytest.mark.asyncio
async def test_unknown_own_reset_says_so_and_when_it_checks_again(tmp_path, monkeypatch):
    _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="claude", limits={"codex": CODEX_WEEKLY})
    rig.refuse("You've hit your limit")
    await rig.handoff(None, "")
    (row,) = rig.rows("coordinator paused")
    assert row.startswith(
        "coordinator paused, waiting for claude until its reset (time unknown; checking again at"
    )


@pytest.mark.asyncio
async def test_pump_activity_names_the_awaited_engine_and_date(tmp_path, monkeypatch):
    now = _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="codex", limits={"claude": CODEX_WEEKLY + 3600})
    stop, pump = await _refused_in_pump(rig, now, CODEX_NOTICE)
    await wait_for(lambda: "waiting for codex until Sat 24 Oct 09:00 UTC" in rig.timeline._activity)
    await finish(stop, pump)


# --------------------------------------------------------------------------- #
# The switch keeps the work and the context


@pytest.mark.asyncio
async def test_switch_writes_checkpoint_row_and_never_pushes(tmp_path, monkeypatch):
    _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="claude", limits={})
    monkeypatch.setattr(_checkpoint, "_handoff_git_out", _real_handoff_git_out)
    argvs: list[tuple[str, ...]] = []
    exec_ = asyncio.create_subprocess_exec

    async def recording_exec(*argv, **kwargs):
        argvs.append(tuple(map(str, argv)))
        return await exec_(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording_exec)
    rig.refuse(CLAUDE_NOTICE)
    with pytest.raises(_author.AuthorHandoffRestart):
        await rig.handoff(CLAUDE_RESET, "")
    assert rig.checkpoints == [True]
    assert rig.rows("checkpoint saved before switching to codex") == [
        "checkpoint saved before switching to codex; the new author is told to commit and push first"
    ]
    assert argvs, "the handoff's git reads were not recorded"
    assert not [argv for argv in argvs if "push" in argv]
    assert "Before anything else: commit tracked work and push your branch" in rig.session.pending[0]


_real_handoff_git_out = _checkpoint._handoff_git_out


def _git(cwd, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.mark.asyncio
async def test_handoff_prompt_has_branch_head_unpushed_round_and_push_first(tmp_path, monkeypatch):
    work = tmp_path / "workspace"
    work.mkdir()
    monkeypatch.setattr(_session_config, "WORKSPACE", work)
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(_inbox, "_directions", ["use the staging bucket"])
    _git(work, "init", "-q", "-b", "factory/x")
    (work / "a.txt").write_text("a\n")
    _git(work, "add", "a.txt")
    _git(work, "commit", "-q", "-m", "one")
    tl = _timeline._Timeline()
    tl.phase("review", round_=2)
    tl.open_findings = ["FINDING-A: x is broken"]

    prompt = await _author._handoff_prompt("claude", "codex", "usage limit", "the brief", tl, "thread-1")
    lines = prompt.splitlines()
    assert lines[0] == "AUTHOR HANDOFF: claude -> codex (usage limit)."
    assert lines[1].startswith("Before anything else: commit tracked work and push your branch")
    head = _git(work, "rev-parse", "--short", "HEAD")
    assert f"Branch: factory/x  HEAD: {head}  Unpushed commits: unknown" in lines
    assert "Review: round 2, passed=False" in lines
    for part in ("FINDING-A: x is broken", "use the staging bucket", "thread thread-1", "the brief"):
        assert part in prompt
    assert str(work / _session_config._BRIEF_FILE) in prompt

    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "-q", "-u", "origin", "factory/x")
    (work / "a.txt").write_text("b\n")
    _git(work, "commit", "-q", "-am", "two")
    prompt = await _author._handoff_prompt("claude", "codex", "usage limit", "the brief", tl)
    head = _git(work, "rev-parse", "--short", "HEAD")
    assert f"Branch: factory/x  HEAD: {head}  Unpushed commits: 1" in prompt.splitlines()


def test_switch_shows_takeover_row_not_starting_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path)
    assert _author._takeover_row("codex") == ""  # a genuine first start keeps "starting fresh"
    handoffs = tmp_path / "author-handoffs"
    handoffs.mkdir()
    (handoffs / "9.json").write_text(json.dumps({"from": "codex", "to": "claude", "old_thread": "t9"}))
    (handoffs / "10.json").write_text(json.dumps({"from": "claude", "to": "codex", "old_thread": "t10"}))
    # "10" is the newest record though it sorts before "9" as text.
    assert _author._takeover_row("codex") == "taking over from claude with a handoff briefing (thread t10)"
    assert _author._takeover_row("claude") == ""
    (handoffs / "10.json").write_text(
        json.dumps({"from": "claude", "to": "codex", "old_thread": "t10", "new_thread": "n"})
    )
    assert _author._takeover_row("codex") == ""  # already delivered: this restart is not a takeover


def test_project_start_uses_the_takeover_row():
    """The start rows in `agent.project` say a handoff is being taken over, for either engine."""
    import inspect

    source = inspect.getsource(agent.project.func)
    assert 'taking_over or "restored state holds no Codex thread; starting fresh"' in source
    assert 'taking_over or "restored state holds no conversation; starting fresh"' in source


@pytest.mark.asyncio
async def test_switch_back_to_claude_resumes_earlier_thread(tmp_path, monkeypatch):
    _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="codex", limits={})
    projects = tmp_path / "claude" / "projects"
    munged = "".join(c if c.isalnum() else "-" for c in str(_session_config.WORKSPACE))
    (projects / munged).mkdir(parents=True)
    (projects / munged / "older.jsonl").write_text("{}\n")
    (projects / munged / "earlier-thread.jsonl").write_text("{}\n")
    os.utime(projects / munged / "older.jsonl", (1, 1))
    rig.refuse(CODEX_NOTICE)
    with pytest.raises(_author.AuthorHandoffRestart):
        await rig.handoff(CODEX_WEEKLY, "")
    owner = json.loads((tmp_path / "author-conversation.json").read_text())
    assert owner == {"engine": "claude", "thread": "earlier-thread", "pin": ""}


@pytest.mark.asyncio
async def test_switch_to_claude_without_a_transcript_starts_fresh(tmp_path, monkeypatch):
    _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="codex", limits={})
    rig.refuse(CODEX_NOTICE)
    with pytest.raises(_author.AuthorHandoffRestart):
        await rig.handoff(CODEX_WEEKLY, "")
    owner = json.loads((tmp_path / "author-conversation.json").read_text())
    assert owner["engine"] == "claude" and owner["fresh"] is True and owner["thread"]


@pytest.mark.asyncio
async def test_an_unreadable_own_notice_is_quoted_on_the_timeline(tmp_path, monkeypatch):
    """Criterion 11: the row says the reset is unknown, quotes the notice and names the retry interval."""
    now = _clock(monkeypatch, WALL0)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
    rig = _rig(tmp_path, monkeypatch, engine="claude", store=True)
    assert _usage._meters is not None
    _usage._meters.note_limit("codex", CODEX_WEEKLY, "")
    stop, pump = await _refused_in_pump(rig, now, "You've hit your limit")
    minutes = round(_session_config.LIMIT_RETRY_S / 60)
    await wait_for(
        lambda: rig.rows(
            f'claude reset time unknown ("You\'ve hit your limit"); retrying after {minutes} minutes'
        )
    )
    meter = _usage._meters.readings[CLAUDE_ACCOUNT]
    wall = _agent_time().time()
    assert wall < meter.limited_until <= wall + _session_config.LIMIT_RETRY_S
    await finish(stop, pump)


@pytest.mark.asyncio
async def test_an_unreadable_codex_notice_is_quoted_on_the_timeline(tmp_path, monkeypatch):
    """Codex's refusal with no reset time anywhere: the row quotes the notice and names the retry interval."""
    now = _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="codex", store=True)
    assert _usage._meters is not None
    _usage._meters.note_limit("claude", CODEX_WEEKLY, CLAUDE_ACCOUNT)  # nowhere to switch to
    stop, pump = await _refused_in_pump(rig, now, "You've hit your usage limit.")
    minutes = round(_session_config.LIMIT_RETRY_S / 60)
    await wait_for(
        lambda: rig.rows(
            f'codex reset time unknown ("You\'ve hit your usage limit."); retrying after {minutes} minutes'
        )
    )
    await finish(stop, pump)


# --------------------------------------------------------------------------- #
# Review findings: a finished session's cap, a restored reset, one row per attempt, the reasons given


@pytest.mark.asyncio
async def test_done_cap_keeps_waiting_while_an_engine_has_a_known_reset(tmp_path, monkeypatch):
    """Criterion 18: two resumes of a finished session that do not take are not a permanent stop while
    an engine can run or has a known reset; the session keeps being re-routed and resumed."""
    now = _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="claude", limits={"codex": CODEX_WEEKLY})
    owner = _Owner()
    owner.questions = {}  # type: ignore[attr-defined]  # the dashboard lists the open ones
    rig.timeline.bridge = owner
    q = _Queue()
    stop = asyncio.Event()
    pump = asyncio.create_task(_supervise._pump(q, rig.session, rig.timeline, stop))  # type: ignore[arg-type]
    q.push("DONE: delivered")
    await wait_for(lambda: "send direction" in (rig.timeline._activity or ""))
    await rig.session.submit("Please wrap up: run end-project")
    now["t"] += 10
    rig.refuse(CLAUDE_NOTICE)
    rig.proc.last_output = now["t"] - 120
    for _ in range(16):  # past Claude's 22:40 reset, then several retry intervals
        now["t"] += _session_config.LIMIT_RETRY_S + 61
        await _settle(5)
    await finish(stop, pump)
    assert not rig.rows("a human needs to look")
    assert owner.asked == []
    assert len(_resumes(rig)) > 2  # it went on resuming after the first two tries


@pytest.mark.parametrize("store", [False, True])
@pytest.mark.asyncio
async def test_restored_pinned_codex_keeps_weekly(tmp_path, monkeypatch, store):
    """After a restart the saved reset stands: with no meter holding it, the handoff must not shorten it
    to now + LIMIT_RETRY_S and retry Codex every 30 minutes."""
    monkeypatch.setenv("AGENT_FACTORY_COORDINATOR_ENGINE", "codex")
    _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="codex", limits={}, store=store)
    owner = {"engine": "codex", "limited_until": CODEX_WEEKLY, "account": _author._author_account("codex")}
    (tmp_path / "author-conversation.json").write_text(json.dumps(owner))
    _author._restore_refusal(rig.session, owner)
    stop, pump = await _start_pump(rig)
    await wait_for(lambda: rig.session.on_limit_calls >= 1)
    await _settle()
    await finish(stop, pump)
    assert rig.session.retry_at == CODEX_WEEKLY
    assert json.loads((tmp_path / "author-conversation.json").read_text())["limited_until"] == CODEX_WEEKLY
    assert not rig.rows("time unknown")
    assert rig.rows("waiting for codex until Sat 24 Oct 09:00 UTC")


@pytest.mark.asyncio
async def test_needs_human_names_a_usable_engine_the_pin_forbids(tmp_path, monkeypatch):
    """Criterion 19: Claude could run now; the text says the fallback key would let it."""
    monkeypatch.setenv("AGENT_FACTORY_COORDINATOR_ENGINE", "codex")
    _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="codex", store=True)
    meters = _usage._meters
    assert meters is not None
    meters.update(_meter.Meter(CLAUDE_ACCOUNT, "test", WALL0, five_hour_pct=10.0, weekly_pct=20.0))
    meters.note_limit("codex", WALL0 + 3600, "", error="401 Unauthorized")
    rig.refuse("You've hit your usage limit.")
    assert await rig.handoff(None, "") is False
    expected = (
        'claude: not used, pinned to codex -- set ENGINE_FALLBACK="allow" to use it; '
        "codex: login rejected (401 Unauthorized) -- replace the [secrets] codex_token secret"
    )
    assert rig.rows("a human needs to look") == [f"a human needs to look: {expected}"]


@pytest.mark.asyncio
async def test_every_resume_attempt_writes_a_routing_row(tmp_path, monkeypatch):
    """Criterion 2: N attempts give N routing rows, the repeats in a short form."""
    monkeypatch.setenv("AGENT_FACTORY_COORDINATOR_ENGINE", "claude")
    now = _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="claude", limits={})
    rig.refuse(CLAUDE_NOTICE)
    for _ in range(3):
        assert await rig.handoff(CLAUDE_RESET, "") is False
        now["t"] += 31 * 60
    routed = rig.rows("router: author -> deferred")
    assert len(routed) == 3
    assert routed[1:] == ["router: author -> deferred (unchanged; retry at 22:40 UTC)"] * 2


@pytest.mark.asyncio
async def test_handoff_prompt_says_done_and_shows_the_reset_as_utc(tmp_path, monkeypatch):
    """Criterion 23: the new engine learns whether DONE was declared; the reset reads as a time."""
    _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="claude", limits={})
    rig.session.declared_done = True
    rig.refuse(CLAUDE_NOTICE)
    with pytest.raises(_author.AuthorHandoffRestart):
        await rig.handoff(CLAUDE_RESET, "")
    lines = rig.session.pending[0].splitlines()
    assert lines[0] == "AUTHOR HANDOFF: claude -> codex (usage limit; reset Mon 19 Oct 22:40 UTC)."
    assert "DONE declared before the switch: yes" in lines
    assert not rig.rows(f"reset {CLAUDE_RESET}")
    assert rig.rows("author handoff claude -> codex: usage limit, reset Mon 19 Oct 22:40 UTC")


@pytest.mark.parametrize(
    ("setup", "why"),
    [
        ("unset", "pinned to claude (ENGINE_FALLBACK unset)"),
        ("wait", 'pinned to claude (ENGINE_FALLBACK="wait")'),
        ("direction", "pinned to claude (operator direction)"),
        ("hard_pin", "pinned to claude (benchmark hard_pin)"),
    ],
)
@pytest.mark.asyncio
async def test_paused_reason_names_what_holds_the_pin(tmp_path, monkeypatch, setup, why):
    """Criterion 20: the reason for not using the other engine names the pin's actual source."""
    monkeypatch.setenv("AGENT_FACTORY_COORDINATOR_ENGINE", "claude")
    _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="claude", limits={})
    if setup == "wait":
        monkeypatch.setenv("AGENT_FACTORY_ENGINE_FALLBACK", "wait")
    elif setup == "direction":
        _pins.set_direction_pin("claude")
    elif setup == "hard_pin":
        monkeypatch.setattr(_usage, "_hard_pins", True)
    rig.refuse(CLAUDE_NOTICE)
    assert await rig.handoff(CLAUDE_RESET, "") is False
    assert rig.timeline._phase["paused"]["others"] == {"codex": why}


async def _done_session_pump(rig: _Rig, now: dict[str, float], notice: str):
    """A session that declared DONE, then hit the limit on its wrap-up; returns (stop, pump)."""
    q = _Queue()
    stop = asyncio.Event()
    pump = asyncio.create_task(_supervise._pump(q, rig.session, rig.timeline, stop))  # type: ignore[arg-type]
    q.push("DONE: delivered")
    await wait_for(lambda: "send direction" in (rig.timeline._activity or ""))
    await rig.session.submit("Please wrap up: run end-project")
    now["t"] += 10
    rig.refuse(notice)
    rig.proc.last_output = now["t"] - 120
    return stop, pump


def _gaps(times: list[float]) -> list[float]:
    return [b - a for a, b in zip(times, times[1:], strict=False)]


@pytest.mark.asyncio
async def test_done_session_refused_without_a_reset_backs_off_and_asks_once(tmp_path, monkeypatch):
    """A finished session on its only engine, refused at every resume with no reset time: the 30-minute
    fallback is not a known reset. The capped rounds back off to about 4 hours, and after three of them a
    human is asked once, naming what would unblock it, while the retries go on."""
    now = _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="claude", limits={})
    monkeypatch.setattr(_usage, "_author_engines", lambda: ("claude",))
    owner = _Owner()
    owner.questions = {}  # type: ignore[attr-defined]  # the dashboard lists the open ones
    rig.timeline.bridge = owner
    stop, pump = await _done_session_pump(rig, now, "You've hit your limit")
    resumed_at: list[float] = []
    resumes_before_ask = None
    for _ in range(90):  # 30 hours in 20-minute steps
        now["t"] += 1200
        await _settle(5)
        if resumes_before_ask is None and rig.rows("a human needs to look"):
            resumes_before_ask = len(resumed_at)
        if len(_resumes(rig)) > len(resumed_at):
            resumed_at.append(_agent_time().time())
            now["t"] += 400  # clear of the resume, so this is a new refusal and not its redraw
            rig.refuse("You've hit your limit")
            await _settle(5)
    await finish(stop, pump)
    gaps = _gaps(resumed_at)
    assert len(resumed_at) < 25, len(resumed_at)  # every 30 minutes would be about 40
    assert max(gaps) >= 4 * 3600 - 900 and max(gaps) <= 4 * 3600 + 2 * 1000
    expected = (
        "claude keeps refusing with no reset time; restore its quota or replace CLAUDE_CODE_OAUTH_TOKEN"
    )
    asks = rig.rows("a human needs to look")
    assert asks == [f"a human needs to look: {expected}"]
    assert len(owner.asked) == 1 and expected in owner.asked[0]
    assert resumes_before_ask is not None and len(resumed_at) > resumes_before_ask  # retries go on after it


@pytest.mark.asyncio
async def test_done_cap_asks_the_router_once_per_attempt_and_backs_off(tmp_path, monkeypatch):
    """The notice is never cleared, Codex is out for the week. The cap's own routing call is the
    attempt's: the resume after it does not ask again. The capped rounds back off, and Codex's known
    reset means nobody is asked."""
    now = _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="claude", limits={"codex": CODEX_WEEKLY})
    owner = _Owner()
    owner.questions = {}  # type: ignore[attr-defined]
    rig.timeline.bridge = owner
    stop, pump = await _done_session_pump(rig, now, CLAUDE_NOTICE)
    resumed_at: list[float] = []
    for _ in range(60):  # about 30 hours
        now["t"] += _session_config.LIMIT_RETRY_S + 61
        await _settle(5)
        while len(resumed_at) < len(_resumes(rig)):
            resumed_at.append(_agent_time().time())
    await finish(stop, pump)
    assert rig.session.on_limit_calls <= len(resumed_at) + 1  # the refusal, then one per attempt
    assert len(resumed_at) < 30, len(resumed_at)
    assert max(_gaps(resumed_at)) >= 4 * 3600 - 2 * _session_config.LIMIT_RETRY_S
    assert not rig.rows("a human needs to look") and owner.asked == []


@pytest.mark.asyncio
async def test_restored_reset_passed_before_the_first_route_is_not_held_30_minutes(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_COORDINATOR_ENGINE", "codex")
    now = _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="codex", limits={})
    saved = WALL0 + 60
    owner = {"engine": "codex", "limited_until": saved, "account": _author._author_account("codex")}
    (tmp_path / "author-conversation.json").write_text(json.dumps(owner))
    _author._restore_refusal(rig.session, owner)
    now["t"] += 120  # the saved reset passes before the handoff first routes
    assert await rig.handoff(None, "") is False
    assert rig.session.retry_at is not None and rig.session.retry_at <= _agent_time().time()
    assert not rig.rows("time unknown")
    assert not rig.rows("coordinator paused")


def _both_engines_owner(rig: _Rig, monkeypatch) -> _Owner:
    monkeypatch.setattr(_usage, "_author_engines", lambda: ("claude", "codex"))
    owner = _Owner()
    owner.questions = {}  # type: ignore[attr-defined]  # the dashboard lists the open ones
    rig.timeline.bridge = owner  # type: ignore[assignment]
    return owner


async def _drive_done_session(
    rig: _Rig, now: dict[str, float], pump, *, steps: int, step: float, notice, hook=None
):
    """Advance a done session's pump; after each resume, refuse with `notice(resumes so far)` unless it
    returns None (the quota is back). Returns the wall times of the resumes."""
    resumed_at: list[float] = []
    taken = 0
    while not pump.done() and taken < steps:
        taken += 1
        now["t"] += step
        if hook:
            hook(resumed_at)
        await _settle(5)
        if not pump.done() and len(_resumes(rig)) > len(resumed_at):
            resumed_at.append(_agent_time().time())
            now["t"] += 400  # clear of the resume, so this is a new refusal and not its redraw
            if (text := notice(len(resumed_at))) is not None:
                rig.refuse(text)
            await _settle(5)
    return resumed_at


@pytest.mark.asyncio
async def test_hard_pinned_engine_without_a_reset_is_unknown_though_the_other_has_one(tmp_path, monkeypatch):
    """Pinned to Claude, which gives no reset: Codex's weekly reset is not one the coordinator may wait for,
    so the reset stays unknown after the 30-minute guess passes, and on every route."""
    monkeypatch.setenv("AGENT_FACTORY_COORDINATOR_ENGINE", "claude")
    now = _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="claude", limits={"codex": CODEX_WEEKLY})
    _both_engines_owner(rig, monkeypatch)
    rig.refuse("You've hit your limit")
    assert await rig.handoff(None, "") is False
    assert rig.session.reset_known is False
    now["t"] += 31 * 60
    assert await rig.handoff(None, "") is False
    assert rig.session.reset_known is False


@pytest.mark.asyncio
async def test_hard_pinned_done_session_without_a_reset_asks_once_though_codex_has_one(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_COORDINATOR_ENGINE", "claude")
    now = _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="claude", limits={"codex": CODEX_WEEKLY})
    owner = _both_engines_owner(rig, monkeypatch)
    stop, pump = await _done_session_pump(rig, now, "You've hit your limit")
    await _drive_done_session(rig, now, pump, steps=90, step=1200, notice=lambda n: "You've hit your limit")
    await finish(stop, pump)
    asks = rig.rows("a human needs to look")
    assert len(asks) == 1, asks
    assert "claude keeps refusing with no reset time" in asks[0]
    assert len(owner.asked) == 1


@pytest.mark.asyncio
async def test_done_cap_ends_at_a_pinned_engines_reset_learned_mid_wait(tmp_path, monkeypatch):
    """Pinned Claude, refused with no reset until the capped rounds back off to 4 hours. Just after a
    capped round starts, the Claude meter reports a reset 40 minutes ahead: the session resumes within one
    LIMIT_RETRY_S of that reset, not at the end of the 4 hours."""
    monkeypatch.setenv("AGENT_FACTORY_COORDINATOR_ENGINE", "claude")
    now = _clock(monkeypatch, WALL0)
    limits = {"codex": CODEX_WEEKLY}
    rig = _rig(tmp_path, monkeypatch, engine="claude", limits=limits)
    _both_engines_owner(rig, monkeypatch)
    stop, pump = await _done_session_pump(rig, now, "You've hit your limit")
    learned: dict[str, float] = {}

    def hook(resumed_at: list[float]) -> None:
        if len(resumed_at) == 12 and "at" not in learned:  # the second try of a round: a 4-hour wait starts
            learned["at"] = limits["claude"] = _agent_time().time() + 2400

    def notice(_: int) -> str | None:
        return None if "at" in learned and _agent_time().time() >= learned["at"] else "You've hit your limit"

    resumed_at = await _drive_done_session(rig, now, pump, steps=400, step=300, notice=notice, hook=hook)
    await finish(stop, pump)
    assert "at" in learned, len(resumed_at)
    after = [t for t in resumed_at if t >= learned["at"]]
    assert after and after[0] <= learned["at"] + _session_config.LIMIT_RETRY_S, (after[:1], learned)


@pytest.mark.asyncio
async def test_done_cap_ends_when_the_other_engines_reset_passes_mid_wait(tmp_path, monkeypatch):
    """Unpinned, Codex out for the week, Claude refusing with no reset. When Codex's reset moves to 40
    minutes into a 4-hour capped wait, the coordinator switches within one LIMIT_RETRY_S of it."""
    now = _clock(monkeypatch, WALL0)
    limits = {"codex": CODEX_WEEKLY}
    rig = _rig(tmp_path, monkeypatch, engine="claude", limits=limits)
    _both_engines_owner(rig, monkeypatch)
    _stop, pump = await _done_session_pump(rig, now, "You've hit your limit")
    learned: dict[str, float] = {}

    def hook(resumed_at: list[float]) -> None:
        if len(resumed_at) == 12 and "at" not in learned:
            learned["at"] = limits["codex"] = _agent_time().time() + 2400

    await _drive_done_session(
        rig, now, pump, steps=400, step=300, notice=lambda n: "You've hit your limit", hook=hook
    )
    switched_by = _agent_time().time()
    # The switch restarts the pod, so the pump has ended on its own: there is nothing left to stop.
    assert "at" in learned
    assert pump.done() and isinstance(pump.exception(), _author.AuthorHandoffRestart)
    assert switched_by <= learned["at"] + _session_config.LIMIT_RETRY_S, (switched_by - learned["at"]) / 60


@pytest.mark.asyncio
async def test_no_reset_question_names_the_other_engine_when_the_switches_are_used_up(tmp_path, monkeypatch):
    monkeypatch.setattr(_author, "_AUTHOR_HANDOFF_MAX_RESTARTS", 0)
    now = _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="claude", limits={})
    owner = _both_engines_owner(rig, monkeypatch)
    stop, pump = await _done_session_pump(rig, now, "You've hit your limit")
    await _drive_done_session(rig, now, pump, steps=90, step=1200, notice=lambda n: "You've hit your limit")
    await finish(stop, pump)
    expected = (
        "claude keeps refusing with no reset time; restore its quota or replace CLAUDE_CODE_OAUTH_TOKEN, "
        "or relaunch the project (this run has used its engine switches) to use codex"
    )
    assert rig.rows("a human needs to look") == [f"a human needs to look: {expected}"]
    assert len(owner.asked) == 1 and expected in owner.asked[0]


@pytest.mark.asyncio
async def test_done_cap_backoff_is_not_cut_short_by_the_refusals_guess_on_the_meter(tmp_path, monkeypatch):
    """With a real meter store, each refusal notes its 30-minute guess on the meter. That guess is not a
    learned reset, so it does not end the capped wait early: the rounds still back off to about 4 hours."""
    now = _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="claude", store=True)
    _both_engines_owner(rig, monkeypatch)
    monkeypatch.setattr(_usage, "_author_engines", lambda: ("claude",))
    stop, pump = await _done_session_pump(rig, now, "You've hit your limit")
    resumed_at = await _drive_done_session(
        rig, now, pump, steps=90, step=1200, notice=lambda n: "You've hit your limit"
    )
    await finish(stop, pump)
    assert _usage._meters is not None
    assert _usage._meters.readings[CLAUDE_ACCOUNT].limited_until is not None  # the guess is on the meter
    assert len(resumed_at) < 25, len(resumed_at)
    assert max(_gaps(resumed_at)) >= 4 * 3600 - 900


@pytest.mark.asyncio
async def test_no_reset_question_says_the_fallback_engine_is_itself_limited(tmp_path, monkeypatch):
    """Claude pinned and refusing with no reset, Codex weekly-limited: lifting the pin alone would run
    nothing until Codex's reset, so the question says so."""
    monkeypatch.setenv("AGENT_FACTORY_COORDINATOR_ENGINE", "claude")
    now = _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="claude", limits={"codex": CODEX_WEEKLY})
    owner = _both_engines_owner(rig, monkeypatch)
    stop, pump = await _done_session_pump(rig, now, "You've hit your limit")
    await _drive_done_session(rig, now, pump, steps=90, step=1200, notice=lambda n: "You've hit your limit")
    await finish(stop, pump)
    (ask,) = rig.rows("a human needs to look")
    assert ask.endswith('set ENGINE_FALLBACK="allow" to use codex (limited until Sat 24 Oct 09:00 UTC)'), ask
    assert len(owner.asked) == 1


@pytest.mark.asyncio
async def test_no_reset_question_is_asked_once_per_episode(tmp_path, monkeypatch):
    """A resume that takes ends the episode; a later episode stuck the same way asks again."""
    now = _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="claude", limits={})
    monkeypatch.setattr(_usage, "_author_engines", lambda: ("claude",))
    owner = _Owner()
    owner.questions = {}  # type: ignore[attr-defined]
    rig.timeline.bridge = owner
    stop, pump = await _done_session_pump(rig, now, "You've hit your limit")
    await _drive_done_session(rig, now, pump, steps=90, step=1200, notice=lambda n: "You've hit your limit")
    assert len(rig.rows("a human needs to look")) == 1
    # The quota comes back: the next resume takes (the session speaks) and the episode ends.
    resumes = len(_resumes(rig))

    def speaks(_n: int) -> None:
        rig.session.current.output_count = getattr(rig.session.current, "output_count", 0) + 9000

    await _drive_done_session(rig, now, pump, steps=30, step=1200, notice=speaks)
    assert len(_resumes(rig)) > resumes
    assert rig.timeline._phase["kind"] != "limited"
    await rig.session.submit("Please wrap up: run end-project")
    now["t"] += 10
    rig.refuse("You've hit your limit")
    rig.proc.last_output = now["t"] - 120
    await _drive_done_session(rig, now, pump, steps=90, step=1200, notice=lambda n: "You've hit your limit")
    await finish(stop, pump)
    assert len(rig.rows("a human needs to look")) == 2
    assert len(owner.asked) == 2


@pytest.mark.asyncio
async def test_done_cap_that_the_router_resolves_puts_the_session_back_to_work(tmp_path, monkeypatch):
    """Two resumes of a finished session did not take. The cap asks the router again, and it says the
    session can run after all (the switch restarts the pod, so a True answer is a decision to stay)."""
    now = _clock(monkeypatch, WALL0)
    session = _Session(_Stoppable(), tmp_path / "author-pending.json")
    calls: list[float | None] = []

    async def on_limit(reset_at, interrupted):
        calls.append(reset_at)
        session.retry_at = 0
        return len(calls) == 4  # the refusal, a re-route before each of two resumes, then the cap's

    session.on_limit = on_limit
    rig = _Rig(session, _timeline._Timeline(), None, [])
    phases_after_cap: list[str] = []
    set_phase = rig.timeline.phase

    def record_phase(kind, **fields):
        if len(calls) >= 4:
            phases_after_cap.append(kind)
        return set_phase(kind, **fields)

    monkeypatch.setattr(rig.timeline, "phase", record_phase)
    stop, pump = await _done_session_pump(rig, now, "You've hit your limit")
    rounds = 0
    while len(calls) != 4:
        assert rounds < 10, "the cap's routing call never happened"
        rounds += 1
        now["t"] += _session_config.LIMIT_RETRY_S + 61
        await _settle(5)
    await wait_for(lambda: len(calls) == 4, what="the cap's routing call")
    # The screen still shows the notice, so the next pass pauses again: the "done" is only ever transient.
    await wait_for(lambda: "done" in phases_after_cap, what="the finished session's phase put back")
    await finish(stop, pump)
    assert calls[-1] is None and len(_resumes(rig)) == 2
    assert not rig.rows("a human needs to look")


@pytest.mark.asyncio
async def test_done_cap_that_the_router_defers_with_a_needs_human_reason_says_so_once(tmp_path, monkeypatch):
    """The cap asks the router again and it defers: no engine can run until someone acts. The row carries
    the router's reason, and stays one row through the later capped rounds."""
    now = _clock(monkeypatch, WALL0)
    session = _Session(_Stoppable(), tmp_path / "author-pending.json")
    reason = "claude: replace CLAUDE_CODE_OAUTH_TOKEN"
    calls: list[float | None] = []
    rig = _Rig(session, _timeline._Timeline(), None, [])

    async def on_limit(reset_at, interrupted):
        calls.append(reset_at)
        if len(calls) < 4:  # the refusal and a re-route before each of two resumes: retry at once
            session.retry_at = 0
            return False
        # What the real handoff does when it defers: a retry in the future and why, on the timeline.
        session.retry_at = _agent_time().time() + 3600
        rig.timeline.phase(
            "limited", paused={"engine": "", "until": None, "others": {}, "needs_human": reason}
        )
        return False

    session.on_limit = on_limit
    stop, pump = await _done_session_pump(rig, now, "You've hit your limit")
    rounds = 0
    while len(calls) < 4:
        assert rounds < 10, "the cap's routing call never happened"
        rounds += 1
        now["t"] += _session_config.LIMIT_RETRY_S + 61
        await _settle(5)
    await wait_for(lambda: rig.rows("a human needs to look"), what="the cap's row")
    for _ in range(20):  # further capped rounds do not repeat it
        now["t"] += _session_config.LIMIT_RETRY_S + 61
        await _settle(5)
    await finish(stop, pump)
    assert len(calls) > 4  # the cap was reached again after the row
    assert rig.rows("a human needs to look") == [
        f"still on the usage limit after two tries; a human needs to look: {reason}"
    ]


def _utc_ts(year: int, month: int, day: int, hour: int, minute: int) -> float:
    return dt.datetime(year, month, day, hour, minute, tzinfo=dt.UTC).timestamp()


@pytest.mark.real_reset
def test_a_stale_notice_time_does_not_replace_the_header_reset(monkeypatch, tmp_path):
    """A refusal whose text names a clock time already past keeps the weekly reset from the API headers.

    Seen at 04:31Z, "resets 2:50am (UTC)" is today's 02:50, gone; the 429 says the 7-day window reopens on
    16 Nov 00:40Z. The error carries 16 Nov, and the meter store records it, not a 30-minute guess.
    """
    seen = _utc_ts(2026, 11, 12, 4, 31)
    weekly = _utc_ts(2026, 11, 16, 0, 40)
    monkeypatch.setattr(vars(_usage)["time"], "time", lambda: seen)
    payload = {
        "is_error": True,
        "api_error_status": 429,
        "result": "You've hit your limit · resets 2:50am (UTC)",
        "headers": {
            "anthropic-ratelimit-unified-7d-status": "rejected",
            "anthropic-ratelimit-unified-7d-reset": str(int(weekly)),
        },
    }
    with pytest.raises(RuntimeError, match=_usage.USAGE_LIMIT_MARK) as refused:
        _subagent_run._raise_on_cli_error(json.dumps(payload))
    assert _usage._refusal_until(str(refused.value)) == weekly

    store = _meter.MeterStore(tmp_path / "meters")
    monkeypatch.setattr(_usage, "_meters", store)
    _usage._note_engine_limit("claude", str(refused.value))
    assert [m.limited_until for m in store.latest.values()] == [weekly]
