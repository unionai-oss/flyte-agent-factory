"""Release after idle, and the project's last act (launch queue, chunk C).

The pump tests run `_pump` under a fake clock: `time` is replaced, so a passed review can sit
idle for 30 "minutes" in a fraction of a second. The `project()` tests replace everything that
needs a pod (the session, the checkpoint, the loops) and keep the order of the end of the run real.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import sys
import threading
import time as real_time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import flyte
import pytest

from flyte_agent_factory import (
    _author,
    _bridge,
    _checkpoint,
    _demo_checks,
    _gates,
    _inbox,
    _instructions,
    _issues,
    _profile,
    _review,
    _reviewer,
    _session_config,
    _session_log,
    _session_setup,
    _state,
    _supervise,
    _supervise_support,
    _timeline,
    _usage,
    agent,
)
from flyte_agent_factory._delegation import Delegations
from flyte_agent_factory._process import ClaudeProcess
from tests._wait import wait_for
from tests.conftest import patch_supervisor_clock

HALF_HOUR = 1800


def _own_profile() -> _profile.Profile:
    profile = _session_config._PROFILE
    assert profile is not None
    return profile


class Clock:
    """Stands in for the `time` module inside `agent`."""

    def __init__(self) -> None:
        self.now = 10_000.0

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now

    def __getattr__(self, name: str):
        return getattr(real_time, name)


class Bridge:
    def __init__(self, notes: list[str] | None = None) -> None:
        self._notes = list(notes or [])
        self.questions: dict[str, dict[str, Any]] = {}

    def drain_notes(self) -> list[str]:
        notes, self._notes = self._notes, []
        return notes

    def stop_requested(self) -> str | None:
        return None


class Proc:
    def __init__(self) -> None:
        self.tail: list[str] = []
        self.menu: str | None = None
        self.submitted: list[str] = []
        self.login_expired = False
        self.last_output: float | None = None
        self.limit_hit_at: float | None = None
        self.limit_text = ""
        self.last_submission: tuple[str, float, int] | None = None
        self.typed: list[str] = []

    async def submit(self, text: str, **kwargs) -> None:
        self.submitted.append(text)
        # `_supervise.time` is the Rig's Clock while a Rig is up.
        self.last_submission = (text, cast(Any, _supervise).time.monotonic(), 0)

    def type(self, text: str) -> None:
        self.typed.append(text)


class Rig:
    """One `_pump` that is told DONE at once and whose review passes (or is held)."""

    def __init__(self, monkeypatch, tmp_path: Path) -> None:
        self.clock = Clock()
        self.bridge = Bridge(["DONE: delivered"])
        self.proc = Proc()
        self.timeline = _timeline._Timeline()
        self.stop = asyncio.Event()
        self.children = 0
        self.review_gate: asyncio.Event | None = None
        self.delegations = SimpleNamespace(
            active={}, pending=set(), drain_notices=list, requeue_notices=lambda: None, recent=[]
        )
        self.workspace: Path | None = tmp_path
        self.task: asyncio.Task[Any] | None = None

        async def review_round(workspace, brief, acceptance, round_, timeline):
            if self.review_gate is not None:
                await self.review_gate.wait()
            return True, "", None, ""

        async def nothing(*args, **kwargs):
            return ""

        # The declared checks exist and pass, so a DONE goes straight to review.
        checks = tmp_path / _session_config._checks_rel("demo")
        checks.parent.mkdir(parents=True, exist_ok=True)
        checks.write_text("true\n")
        monkeypatch.setattr(_supervise_support, "_run_declared_checks", nothing)
        patch_supervisor_clock(monkeypatch, self.clock)
        monkeypatch.setattr(_inbox, "time", self.clock)
        monkeypatch.setattr(_usage, "time", self.clock)
        monkeypatch.setattr(_session_config, "POLL_EVERY_S", 0.0)
        monkeypatch.setattr(_review, "_review_round", review_round)
        monkeypatch.setattr(_supervise_support, "_done_preflight", nothing)
        monkeypatch.setattr(_demo_checks, "_demo_package_problem", nothing)
        monkeypatch.setattr(_demo_checks, "_publish_demo_reports", nothing)
        monkeypatch.setattr(_gates, "run", lambda *a, **k: "")
        monkeypatch.setattr(_session_config, "_applicable_standards", list)
        self.listing_error = ""
        self.listing_gate: threading.Event | None = None
        monkeypatch.setattr(_supervise_support, "_in_flight_children", self._listing)
        monkeypatch.setattr(_session_config, "_project_name", "demo")
        monkeypatch.setattr(_inbox, "_directions", [])
        monkeypatch.setattr(_inbox, "_last_direction_at", None, raising=False)
        monkeypatch.setattr(_supervise_support, "_released", False, raising=False)
        monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path / "state")
        monkeypatch.setattr(_supervise_support, "_escalated", False)
        monkeypatch.setattr(_supervise_support, "_in_flight_error", "")

    def _listing(self) -> tuple[int, str]:
        if self.listing_gate is not None:
            self.listing_gate.wait(60)
        return self.children, self.listing_error

    def start(self) -> None:
        self.task = asyncio.create_task(
            _supervise._pump(
                cast(_bridge.Bridge, self.bridge),
                cast(ClaudeProcess, self.proc),
                self.timeline,
                self.stop,
                workspace=self.workspace,
                brief="brief",
                delegations=cast(Delegations, self.delegations),
            )
        )

    async def advance(self, seconds: float, step: float = 30.0) -> None:
        """Move the fake clock on in `step`s, giving the pump a few turns after each."""
        assert self.task is not None
        end = self.clock.now + seconds
        while self.clock.now < end:
            self.clock.now = min(end, self.clock.now + step)
            await asyncio.sleep(0.01)
            if self.task.done():
                self.task.result()
                return

    @property
    def released(self) -> bool:
        return self.stop.is_set() and _supervise_support._released

    def rows(self) -> list[str]:
        return [f"{icon} {text}" for _, icon, text in self.timeline._rows]

    async def finish(self) -> None:
        assert self.task is not None
        self.stop.set()
        await asyncio.wait_for(self.task, 30)


@pytest.fixture(autouse=True)
def offline_report(monkeypatch):
    async def _aio(*_a, **_k) -> None:
        return None

    monkeypatch.setattr(flyte.report, "replace", type("R", (), {"aio": staticmethod(_aio)}))


@pytest.fixture
def rig(monkeypatch, tmp_path):
    return Rig(monkeypatch, tmp_path)


async def settle(rig: Rig, landed: Callable[[], bool]) -> None:
    """Give the DONE path real time to land (it hops through worker threads)."""
    task = rig.task
    assert task is not None
    await wait_for(lambda: landed() or task.done())


async def passed(rig: Rig) -> None:
    """Start, and run until the review has passed."""
    rig.start()
    await rig.advance(60)
    # The DONE path hops through worker threads; under load 20ms of real time is not always enough
    # for it to land. Give it real time (not fake time, which the idle-window tests count) to finish.
    task = rig.task
    assert task is not None
    await wait_for(lambda: rig.timeline._phase["kind"] == "done" or task.done())
    assert rig.timeline._phase["kind"] == "done", rig.rows()


def test_the_idle_window_is_30_minutes():
    assert _session_config.RELEASE_IDLE_S == HALF_HOUR


@pytest.mark.asyncio
async def test_a_passed_review_idle_for_30_minutes_is_released(rig):
    await passed(rig)
    await rig.advance(HALF_HOUR + 120)
    assert rig.released
    assert rig.timeline._phase["kind"] == "released"
    assert (
        '💤 released after 30m idle having passed review in round 1; launch "demo" again to resume'
        in rig.rows()
    )
    await rig.finish()


@pytest.mark.asyncio
async def test_29_minutes_is_not_enough(rig):
    await passed(rig)
    await rig.advance(29 * 60)
    assert not rig.stop.is_set()
    await rig.finish()


@pytest.mark.asyncio
async def test_a_review_in_flight_is_not_released(rig):
    rig.review_gate = asyncio.Event()
    rig.start()
    await rig.advance(HALF_HOUR * 2)
    assert not rig.stop.is_set()
    assert "reviewing" in rig.timeline._activity
    rig.review_gate.set()
    await rig.finish()


@pytest.mark.asyncio
async def test_done_declared_but_never_reviewed_is_not_released(rig):
    """No workspace, so no review runs: DONE is declared and nothing has passed."""
    rig.workspace = None
    rig.start()
    await rig.advance(HALF_HOUR * 2)
    assert not rig.stop.is_set()
    await rig.finish()


@pytest.mark.asyncio
async def test_an_open_question_blocks_release_and_an_expired_one_does_not(rig):
    await passed(rig)
    rig.bridge.questions["q1"] = {"timed_out": False}
    await rig.advance(HALF_HOUR + 600)
    assert not rig.stop.is_set(), "an unexpired ask-human is a human still deciding"
    rig.bridge.questions["q1"]["timed_out"] = True
    await rig.advance(120)
    assert rig.released
    await rig.finish()


@pytest.mark.asyncio
async def test_a_question_past_its_deadline_does_not_hold_release(rig):
    """A restored question whose close failed must not keep a passed project from releasing."""
    await passed(rig)
    rig.bridge.questions["q1"] = {"timed_out": False, "deadline_at": rig.clock.time() - 1}
    await rig.advance(HALF_HOUR + 600)
    assert rig.released
    await rig.finish()


@pytest.mark.asyncio
async def test_a_question_before_its_deadline_still_holds_release(rig):
    await passed(rig)
    rig.bridge.questions["q1"] = {"timed_out": False, "deadline_at": rig.clock.time() + 10 * HALF_HOUR}
    await rig.advance(HALF_HOUR + 600)
    assert not rig.stop.is_set()
    await rig.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["Union", "Claude app"])
async def test_direction_restarts_the_window(rig, source):
    await passed(rig)
    await rig.advance(20 * 60)
    _inbox._record_direction("one more thing", source, rig.timeline)
    await rig.advance(HALF_HOUR - 120)
    assert not rig.stop.is_set(), "the session has been quiet 48m but was directed 28m ago"
    await rig.advance(300)
    assert rig.released
    await rig.finish()


@pytest.mark.asyncio
async def test_child_actions_in_flight_block_release(rig):
    rig.children = 2
    await passed(rig)
    await rig.advance(HALF_HOUR + 600)
    assert not rig.stop.is_set()
    rig.children = 0
    await rig.advance(180)
    assert rig.released
    await rig.finish()


@pytest.mark.asyncio
async def test_a_child_listing_that_fails_blocks_release(rig):
    await passed(rig)
    rig.listing_error = "ConnectionError: no route"
    await rig.advance(HALF_HOUR + 600)
    assert not rig.stop.is_set(), "cannot see the children, so cannot tell the project is idle"
    await rig.finish()


@pytest.mark.asyncio
async def test_release_waits_for_a_listing_still_in_flight(rig):
    """The last count can be a minute and a half old while a listing is pending: not enough to release on."""
    rig.listing_gate = threading.Event()
    try:
        await passed(rig)
        await rig.advance(HALF_HOUR + 600)
        assert not rig.stop.is_set(), "released on a count from before the listing in flight"
    finally:
        rig.listing_gate.set()
    await rig.advance(180)
    # The pending listing lands from a worker thread: under load that takes real time, as in `passed`.
    await wait_for(lambda: rig.released)
    assert rig.released
    await rig.finish()


@pytest.mark.asyncio
async def test_an_active_delegation_blocks_release(rig):
    rig.delegations.active["job-1"] = {"engine": "codex"}
    await passed(rig)
    await rig.advance(HALF_HOUR + 600)
    assert not rig.stop.is_set()
    rig.delegations.active.clear()
    await rig.advance(180)
    assert rig.released
    await rig.finish()


@pytest.mark.asyncio
async def test_the_usage_limit_blocks_release(rig):
    await passed(rig)
    await rig.advance(60)
    rig.proc.limit_hit_at = rig.clock.now
    rig.proc.limit_text = "5-hour limit reached"
    await rig.advance(HALF_HOUR + 600)
    assert not rig.stop.is_set()
    await rig.finish()


@pytest.mark.asyncio
async def test_an_open_menu_blocks_release(rig, monkeypatch):
    monkeypatch.setattr(_session_config, "PROMPT_BUSY_DECLINE_S", 10**9)
    await passed(rig)
    rig.proc.menu = "Do you want to proceed?"
    await rig.advance(HALF_HOUR + 600)
    assert not rig.stop.is_set()
    await rig.finish()


@pytest.mark.asyncio
async def test_a_new_done_after_passing_clears_the_pass(rig):
    """The review re-runs, so the project is not released until it passes again."""
    await passed(rig)
    await rig.advance(20 * 60)
    rig.review_gate = asyncio.Event()
    rig.bridge._notes.append("DONE: one more change")
    await rig.advance(HALF_HOUR + 600)
    assert not rig.stop.is_set(), "the second review is still running"
    rig.review_gate.set()
    await rig.advance(60)
    assert not rig.stop.is_set(), "passed again just now"
    await rig.advance(HALF_HOUR + 120)
    assert rig.released
    assert "having passed review in round 2" in " ".join(rig.rows())
    await rig.finish()


@pytest.mark.asyncio
async def test_the_review_passed_message_asks_with_a_timeout(rig):
    await passed(rig)
    message = next(text for text in rig.proc.submitted if "independent reviewer passed" in text)
    assert "ask-human --timeout 1800" in message
    assert 'Default if no answer: the project is released; relaunch demo to continue."' in message


def test_the_memory_no_longer_says_the_run_stays_alive_on_purpose():
    memory = _instructions._project_memory(False)
    assert "stays alive on purpose" not in memory
    assert "released" in memory


# --- the end of project() --------------------------------------------------------------------- #


class Ending:
    """`project()` with the pod replaced: what is left is the order of the end of the run."""

    def __init__(self, monkeypatch, tmp_path: Path) -> None:
        self.calls: list[str] = []
        self.dispatch_error: Exception | None = None
        self.pump = self.pump_stop_on_request
        self.session_started = False
        state = SimpleNamespace(
            root=tmp_path / "state", resumed=False, resumed_from="", workspace=tmp_path / "state/workspace"
        )
        state.workspace.mkdir(parents=True)
        self.state = state
        calls = self.calls

        async def restore(*, root, workspace_in=None):
            calls.append("restore")
            return state

        async def save_checkpoint(state):
            calls.append("checkpoint")
            return True

        async def publish(state):
            calls.append("publish")
            return "workspace-dir"

        def dispatch(*, exclude_run=""):
            calls.append(f"dispatch({exclude_run})")
            if self.dispatch_error:
                raise self.dispatch_error
            return []

        async def start_session(*a, **k):
            self.session_started = True
            return self.proc

        async def forever(*args, **kwargs):
            await asyncio.Event().wait()

        async def noop(*a, **k):
            return None

        async def pump(*args, **kwargs):
            await self.pump(*args, **kwargs)

        self.proc = SimpleNamespace(
            wait=lambda: asyncio.sleep(3600),
            stop=noop,
            submit=noop,
            tail=[],
            returncode=None,
            _on_line=None,
        )
        monkeypatch.setattr(_state, "restore", restore)
        monkeypatch.setattr(_state, "save_checkpoint", save_checkpoint)
        monkeypatch.setattr(_state, "publish", publish)
        # Imported first: a profile test elsewhere may have dropped the module from `sys.modules`.
        from flyte_agent_factory import _factory

        monkeypatch.setattr(_factory, "dispatch", dispatch)
        monkeypatch.setattr(_author, "_start_session", start_session)
        monkeypatch.setattr(_usage, "_meter_loop", forever)
        monkeypatch.setattr(_supervise_support, "_dashboard_loop", forever)
        for loop in ("_inbox_loop", "_watch_app_messages"):
            monkeypatch.setattr(_inbox, loop, forever)
        monkeypatch.setattr(_checkpoint, "_checkpoint_loop", forever)
        monkeypatch.setattr(_supervise, "_pump", pump)
        monkeypatch.setattr(_bridge.Bridge, "run", forever)
        monkeypatch.setattr(Delegations, "run", forever)
        monkeypatch.setattr(_session_log, "feed_loop", forever)
        monkeypatch.setattr(_inbox, "_session_feed", lambda resume: None)
        monkeypatch.setattr(_session_config, "STATE_ROOT", state.root)
        monkeypatch.setattr(_session_config, "WORKSPACE", state.workspace)
        monkeypatch.setattr(_session_config, "BASE_DIR", tmp_path)
        monkeypatch.setattr(_session_config, "CONFIG_DIR", state.root / "claude")
        monkeypatch.setattr(agent, "BRIDGE_DIR", tmp_path / "bridge")
        monkeypatch.setattr(_session_setup, "_export_runtime_env", dict)
        for name in (
            "assert_claude_available",
            "write_settings",
            "mark_trusted",
        ):
            monkeypatch.setattr(agent, name, lambda *a, **k: None)
        monkeypatch.setattr(_session_setup, "_install_bridge_commands", lambda *a, **k: None)
        monkeypatch.setattr(_session_config, "_applicable_standards", lambda *a, **k: None)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
        monkeypatch.setattr(agent, "write_context", lambda workspace: workspace / "context.json")
        monkeypatch.setattr(_session_setup, "_prepare_workspace", noop)
        monkeypatch.setattr(agent, "usable_repo", lambda *_: False)
        monkeypatch.setattr(_gates, "capture_base", lambda *a, **k: None)
        monkeypatch.setattr(_supervise_support, "_released", False, raising=False)
        monkeypatch.setattr(
            _session_config,
            "_PROFILE",
            dataclasses.replace(_own_profile(), state=_profile.State(uri="file:///state")),
        )
        monkeypatch.setattr(flyte, "ctx", lambda: SimpleNamespace(action=SimpleNamespace(run_name="r-own")))

    async def pump_stop_on_request(self, bridge, proc, timeline, stop, **kwargs):
        stop.set()

    async def pump_release(self, bridge, proc, timeline, stop, **kwargs):
        _supervise_support._released = True
        stop.set()

    async def pump_wait(self, bridge, proc, timeline, stop, **kwargs):
        await stop.wait()

    async def run(self):
        return await agent.project.func("brief", name="demo", browser=False)


@pytest.fixture
def ending(monkeypatch, tmp_path):
    return Ending(monkeypatch, tmp_path)


@pytest.mark.asyncio
async def test_a_stop_checkpoints_publishes_then_hands_over_the_slot(ending):
    summary, _ = await ending.run()
    assert summary == "demo: stopped on request"
    assert ending.calls == [
        "restore",
        "checkpoint",
        "publish",
        "dispatch(r-own)",
    ]


@pytest.mark.asyncio
async def test_a_final_publish_that_object_storage_refuses_fails_the_run_with_the_outcome(
    ending, monkeypatch
):
    from obstore.exceptions import GenericError

    async def publish(state):
        raise GenericError("Generic S3 error: Error performing PUT")

    monkeypatch.setattr(_state, "publish", publish)
    with pytest.raises(RuntimeError, match="stopped on request, but the workspace could not be published"):
        await ending.run()


@pytest.mark.asyncio
async def test_the_ended_marker_is_in_the_final_checkpoint(ending, monkeypatch):
    seen = []
    real_checkpoint = _state.save_checkpoint

    async def checkpoint(state):
        seen.append((ending.state.root / "ended.json").is_file())
        return await real_checkpoint(state)

    monkeypatch.setattr(_state, "save_checkpoint", checkpoint)
    await ending.run()
    assert seen == [True], "a pod lost after the hand-over would otherwise retry into a second session"


@pytest.mark.asyncio
async def test_a_release_ends_the_run_with_its_own_outcome_and_hands_over(ending):
    ending.pump = ending.pump_release
    summary, _ = await ending.run()
    assert summary == "demo: released after idle (passed review)"
    assert ending.calls[-3:] == ["checkpoint", "publish", "dispatch(r-own)"]


@pytest.mark.asyncio
async def test_a_clean_session_exit_hands_over_the_slot(ending):
    async def exits():
        return 0

    ending.proc.wait = exits
    ending.pump = ending.pump_wait
    summary, _ = await ending.run()
    assert summary == "demo: session ended cleanly"
    assert ending.calls.count("dispatch(r-own)") == 1


@pytest.mark.asyncio
async def test_a_dispatch_error_is_a_row_and_the_run_still_returns(ending, monkeypatch):
    ending.dispatch_error = RuntimeError("store unreachable")
    rows = []
    real_row = _timeline._Timeline.row

    def row(self, icon, text):
        rows.append(f"{icon} {text}")
        real_row(self, icon, text)

    monkeypatch.setattr(_timeline._Timeline, "row", row)
    summary, _ = await ending.run()
    assert summary == "demo: stopped on request"
    assert any(r.startswith("⚠️") and "store unreachable" in r for r in rows)


@pytest.mark.asyncio
async def test_a_crash_does_not_dispatch_or_mark_the_project_ended(ending):
    async def crashed():
        return 1

    ending.proc.wait = crashed
    ending.pump = ending.pump_wait
    with pytest.raises(RuntimeError, match="exited with status 1"):
        await ending.run()
    assert not any(c.startswith("dispatch") for c in ending.calls)
    assert not (ending.state.root / "ended.json").exists()


@pytest.mark.asyncio
async def test_a_retried_pod_after_the_end_publishes_without_a_session(ending):
    (ending.state.root / "ended.json").write_text('{"outcome": "stopped on request"}')
    summary, _ = await ending.run()
    assert not ending.session_started
    assert summary == "demo: stopped on request"
    assert "publish" in ending.calls
    assert ending.calls.count("dispatch(r-own)") == 1


@pytest.mark.asyncio
async def test_without_a_state_uri_there_is_no_queue_to_dispatch(ending, monkeypatch):
    monkeypatch.setattr(
        _session_config, "_PROFILE", dataclasses.replace(_own_profile(), state=_profile.State())
    )
    await ending.run()
    assert not any(c.startswith("dispatch") for c in ending.calls)


# --- QA round 1 ------------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ['{"outcome": "stop', "", "[]", "null", '{"released": true}'])
async def test_an_unreadable_ended_marker_still_takes_the_ended_path(ending, content):
    (ending.state.root / "ended.json").write_text(content)
    await ending.run()
    assert not ending.session_started
    assert "publish" in ending.calls
    assert ending.calls.count("dispatch(r-own)") == 1


@pytest.mark.asyncio
async def test_the_ended_path_keeps_a_release_as_released(ending, monkeypatch):
    phases = []
    real_phase = _timeline._Timeline.phase

    def phase(self, kind, **kw):
        phases.append(kind)
        return real_phase(self, kind, **kw)

    monkeypatch.setattr(_timeline._Timeline, "phase", phase)
    ending.pump = ending.pump_release
    await ending.run()
    marker = json.loads((ending.state.root / "ended.json").read_text())
    assert marker["released"] is True
    ending.calls.clear()
    ending.session_started = False
    ending.pump = ending.pump_stop_on_request
    summary, _ = await ending.run()  # the retry
    assert summary == "demo: released after idle (passed review)"
    assert not ending.session_started
    assert phases[-1] == "released"


@pytest.mark.asyncio
async def test_a_stop_is_recorded_as_not_released(ending):
    await ending.run()
    assert json.loads((ending.state.root / "ended.json").read_text())["released"] is False


@pytest.mark.asyncio
async def test_a_local_run_does_not_write_the_ended_marker(ending, monkeypatch):
    monkeypatch.setattr(flyte, "ctx", lambda: None)
    await ending.run()
    assert not (ending.state.root / "ended.json").exists()


@pytest.mark.asyncio
async def test_a_hung_dispatch_is_a_row_and_the_run_returns(ending, monkeypatch):
    release = threading.Event()
    from flyte_agent_factory import _factory

    monkeypatch.setattr(_factory, "dispatch", lambda *, exclude_run="": release.wait(60) and [])
    monkeypatch.setattr(_checkpoint, "_HAND_OVER_DISPATCH_TIMEOUT_S", 0.05)
    rows = []
    real_row = _timeline._Timeline.row

    def row(self, icon, text):
        rows.append(f"{icon} {text}")
        return real_row(self, icon, text)

    monkeypatch.setattr(_timeline._Timeline, "row", row)
    try:
        summary, _ = await asyncio.wait_for(ending.run(), 30)
    finally:
        release.set()
    assert summary == "demo: stopped on request"
    assert any(r.startswith("⚠️") and "next queued project" in r for r in rows)


def with_state_uri(monkeypatch) -> _timeline._Timeline:
    monkeypatch.setattr(
        _session_config,
        "_PROFILE",
        dataclasses.replace(_own_profile(), state=_profile.State(uri="file:///state")),
    )
    return _timeline._Timeline()


@pytest.mark.asyncio
async def test_a_hung_queue_read_does_not_stop_the_dashboard(monkeypatch):
    from flyte_agent_factory import _factory, _factory_base

    release = threading.Event()
    monkeypatch.setattr(_factory_base, "queue_root", lambda: "file:///state/queue")
    monkeypatch.setattr(_factory, "waiting_rows", lambda queue, at: release.wait(60) and [])
    monkeypatch.setattr(_checkpoint, "_QUEUE_READ_TIMEOUT_S", 0.05)
    timeline = with_state_uri(monkeypatch)
    timeline.queue = [{"position": 1, "name": "stale", "since": 0}]
    try:
        await asyncio.wait_for(_supervise_support._refresh_queue(timeline), 30)
    finally:
        release.set()
    assert timeline.queue == []


@pytest.mark.asyncio
async def test_a_queue_module_that_cannot_import_does_not_stop_the_dashboard(monkeypatch):
    import flyte_agent_factory

    monkeypatch.delattr(flyte_agent_factory, "_factory", raising=False)
    monkeypatch.setitem(sys.modules, "flyte_agent_factory._factory", None)
    timeline = with_state_uri(monkeypatch)
    timeline.queue = [{"position": 1, "name": "stale", "since": 0}]
    await _supervise_support._refresh_queue(timeline)  # does not raise
    assert timeline.queue == []


@pytest.mark.asyncio
async def test_a_second_done_that_the_checks_bounce_clears_the_pass(rig, monkeypatch):
    await passed(rig)
    assert rig.workspace and (_session_config.STATE_ROOT / "review" / "passed.json").is_file()
    problems = ["the build is red"]

    async def checks(*a, **k):
        return problems[0] if problems else ""

    monkeypatch.setattr(_supervise_support, "_run_declared_checks", checks)
    rig.bridge._notes.append("DONE: changed something")
    await rig.advance(60)
    record = _session_config._load_pass_record()
    assert record and not record["passed"] and record["review_round"] == 1
    assert _session_config._load_pass() is None
    await rig.advance(HALF_HOUR + 600)
    assert not rig.stop.is_set(), "the work changed after the pass and has not been reviewed"
    await rig.finish()


@pytest.mark.asyncio
async def test_a_delegation_waiting_on_the_job_limit_blocks_release(rig):
    waiting = asyncio.ensure_future(asyncio.sleep(3600))
    rig.delegations.pending.add(waiting)
    await passed(rig)
    await rig.advance(HALF_HOUR + 600)
    assert not rig.stop.is_set()
    rig.delegations.pending.clear()
    waiting.cancel()
    await rig.advance(180)
    assert rig.released
    await rig.finish()


@pytest.mark.asyncio
async def test_the_pass_is_persisted_with_round_sha_and_time(rig):
    await passed(rig)
    saved = json.loads((_session_config.STATE_ROOT / "review" / "passed.json").read_text())
    assert saved["round"] == 1
    assert "sha" in saved
    assert saved["at"] > 0
    await rig.finish()


@pytest.mark.asyncio
async def test_a_restart_after_a_pass_still_releases(rig):
    """The pod restarted after review passed: `passed_round` came back from the checkpoint."""
    review = _session_config.STATE_ROOT / "review"
    review.mkdir(parents=True)
    (review / "passed.json").write_text(json.dumps({"round": 2, "sha": "abc", "at": 1.0}))
    rig.bridge._notes.clear()
    rig.start()
    await rig.advance(HALF_HOUR - 120)
    assert not rig.stop.is_set(), "quiet time restarts at pump start"
    await rig.advance(300)
    assert rig.released
    assert "having passed review in round 2" in " ".join(rig.rows())
    await rig.finish()


@pytest.mark.asyncio
async def test_a_new_review_clears_the_persisted_pass(rig):
    review = _session_config.STATE_ROOT / "review"
    review.mkdir(parents=True)
    (review / "passed.json").write_text(json.dumps({"round": 1, "sha": "abc", "at": 1.0}))
    rig.review_gate = asyncio.Event()
    rig.start()
    await rig.advance(60)
    # The DONE path hops through worker threads: under the gate's 16 workers it can land after this minute.
    await settle(rig, lambda: bool((_session_config._load_pass_record() or {}).get("in_review")))
    record = _session_config._load_pass_record()
    assert record and not record["passed"] and record["in_review"]
    assert record["review_round"] == 2
    assert _session_config._load_pass() is None
    rig.review_gate.set()
    await rig.finish()


@pytest.mark.asyncio
async def test_a_corrupt_persisted_pass_is_no_pass(rig):
    review = _session_config.STATE_ROOT / "review"
    review.mkdir(parents=True)
    (review / "passed.json").write_text("{not json")
    rig.workspace = None
    rig.bridge._notes.clear()
    rig.start()
    await rig.advance(HALF_HOUR * 2)
    assert not rig.stop.is_set()
    await rig.finish()


def test_a_hung_dispatch_does_not_hold_the_process_open(tmp_path):
    # asyncio.run waits for executor threads on the way out (and the interpreter waits again), so a
    # timed-out `to_thread` call would keep the pod -- and its slot -- alive. The daemon thread does not.
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent(
        """
        import asyncio, time
        from flyte_agent_factory import _inbox

        async def main():
            try:
                await asyncio.wait_for(_inbox._in_daemon_thread(time.sleep, 3600), timeout=0.2)
            except asyncio.TimeoutError:
                print("timed out")

        asyncio.run(main())
        """
    )
    # The thread sleeps for an hour: the process exiting inside the timeout proves it was not waited for.
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=True
    )
    assert "timed out" in out.stdout


async def test_the_daemon_thread_hands_back_results_and_errors():
    assert await _inbox._in_daemon_thread(lambda x: x + 1, 1) == 2
    with pytest.raises(ZeroDivisionError):
        await _inbox._in_daemon_thread(lambda: 1 / 0)


def test_an_absurd_round_in_passed_json_is_no_pass(tmp_path, monkeypatch):
    path = tmp_path / "passed.json"
    path.write_text('{"round": 1e400}')
    monkeypatch.setattr(_session_config, "_passed_path", lambda: path)
    assert _session_config._load_pass() is None


async def test_a_restored_pass_shows_as_done_not_building(rig):
    _inbox._save_pass(2, "abc")
    rig.bridge._notes.clear()
    rig.start()
    await rig.advance(1)
    assert rig.timeline._phase["kind"] == "done"
    await rig.finish()


@pytest.mark.parametrize("round_, error_name", [(1, "ReviewUnavailable"), (8, "ReviewLimited")])
async def test_a_refunded_review_round_survives_a_restart(rig, monkeypatch, round_, error_name):
    # Built here, not at collection: a test that reloads `agent` leaves a stale class the pump cannot catch.
    error = getattr(_reviewer, error_name)("offline" if error_name == "ReviewUnavailable" else "")
    attempts = []
    if round_ > 1:
        _inbox._save_pass(round_ - 1, "", passed=False)

    async def review(workspace, brief, acceptance, number, timeline):
        attempts.append(number)
        if len(attempts) == 1:
            raise error
        return True, "", None, ""

    monkeypatch.setattr(_review, "_review_round", review)
    rig.start()
    try:
        # Step the rig's clock rather than wall time, so a loaded machine cannot make this flaky.
        for _ in range(300):
            if rig.timeline._phase["kind"] in {"limited", "waiting"} or rig.task.done():
                break
            await rig.advance(1)
        assert attempts == [round_]
        assert rig.timeline._phase["kind"] in {"limited", "waiting"}, rig.rows()
    finally:
        await rig.finish()

    rig.bridge = Bridge(["DONE: retry after restart"])
    rig.timeline = _timeline._Timeline()
    rig.stop = asyncio.Event()
    try:
        await passed(rig)
        assert attempts == [round_, round_]
        assert not any("budget spent" in text for text in rig.proc.submitted)
    finally:
        await rig.finish()


async def test_a_hung_queue_read_is_not_started_again(monkeypatch):
    # One stuck thread per hang, not one per dashboard refresh.
    import threading as _threading

    from flyte_agent_factory import _factory, _factory_base

    gate = _threading.Event()
    calls = []

    def hang(*_a):
        calls.append(1)
        gate.wait(60)
        return []

    monkeypatch.setattr(_supervise_support, "_queue_read", None)
    monkeypatch.setattr(_checkpoint, "_QUEUE_READ_TIMEOUT_S", 0.05)
    monkeypatch.setattr(
        _session_config,
        "_PROFILE",
        dataclasses.replace(_own_profile(), state=_profile.State(uri="file:///q")),
    )
    monkeypatch.setattr(_factory, "waiting_rows", hang)
    monkeypatch.setattr(_factory_base, "queue_root", lambda: "file:///q/queue")
    timeline = _timeline._Timeline()
    await _supervise_support._refresh_queue(timeline)
    await _supervise_support._refresh_queue(timeline)
    gate.set()
    assert calls == [1]


@pytest.mark.asyncio
async def test_a_refresh_skipped_behind_a_hung_read_shows_no_stale_queue(monkeypatch):
    # The skip path left whatever list was there, so a stuck read froze an hours-old queue on screen.
    import threading as _threading

    from flyte_agent_factory import _factory, _factory_base

    gate = _threading.Event()
    monkeypatch.setattr(_supervise_support, "_queue_read", None)
    monkeypatch.setattr(_checkpoint, "_QUEUE_READ_TIMEOUT_S", 0.05)
    monkeypatch.setattr(
        _session_config,
        "_PROFILE",
        dataclasses.replace(_own_profile(), state=_profile.State(uri="file:///q")),
    )
    monkeypatch.setattr(_factory, "waiting_rows", lambda *_a: gate.wait(60) and [])
    monkeypatch.setattr(_factory_base, "queue_root", lambda: "file:///q/queue")
    timeline = _timeline._Timeline()
    await _supervise_support._refresh_queue(timeline)  # times out; the read keeps hanging
    timeline.queue = [{"position": 1, "name": "stale", "since": 0}]
    try:
        await _supervise_support._refresh_queue(timeline)  # skipped: the hung read is not restarted
    finally:
        gate.set()
    assert timeline.queue == []


def test_a_daemon_call_finishing_after_its_loop_closed_is_silent():
    import threading as _threading

    done = _threading.Event()
    errors = []
    old = _threading.excepthook
    _threading.excepthook = lambda args: errors.append(args.exc_value)
    try:

        def slow():
            real_time.sleep(0.2)
            done.set()

        def inner():
            async def main():
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(_inbox._in_daemon_thread(slow), timeout=0.01)

            asyncio.run(main())

        t = _threading.Thread(target=inner)
        t.start()
        t.join()
        done.wait(2)
        real_time.sleep(0.1)
    finally:
        _threading.excepthook = old
    assert errors == []


def _saved_pass() -> dict[str, Any]:
    saved: dict[str, Any] = json.loads((_session_config.STATE_ROOT / "review" / "passed.json").read_text())
    return saved


@pytest.mark.asyncio
async def test_a_pass_is_saved_unposted_before_the_verdict_and_marked_posted_after(rig, monkeypatch):
    seen = []

    async def verdict(workspace, timeline, sha, rounds, why=""):
        seen.append(_saved_pass())

    monkeypatch.setattr(_supervise_support, "_post_verdict", verdict)
    await passed(rig)
    assert seen and seen[0]["verdict_posted"] is False, "a crash during the post must leave it to redo"
    assert _saved_pass()["verdict_posted"] is True
    await rig.finish()


@pytest.mark.asyncio
async def test_a_restored_pass_with_an_unposted_verdict_posts_it_once(rig, monkeypatch):
    posts = []

    async def verdict(workspace, timeline, sha, rounds, why=""):
        posts.append((sha, rounds, why))

    monkeypatch.setattr(_supervise_support, "_post_verdict", verdict)
    monkeypatch.setattr(_session_setup, "_pr", object())
    _inbox._save_pass(2, "abc123", verdict_posted=False)
    rig.bridge._notes.clear()
    rig.start()
    await rig.advance(60)
    assert posts == [("abc123", 2, "")]
    assert _saved_pass()["verdict_posted"] is True
    await rig.finish()
    # a second restart does not post again
    rig.bridge._notes.clear()
    rig.start()
    await rig.advance(60)
    assert len(posts) == 1
    await rig.finish()


@pytest.mark.asyncio
async def test_an_old_passed_json_without_the_new_keys_is_not_reposted(rig, monkeypatch):
    posts = []

    async def verdict(*a, **k):
        posts.append(a)

    monkeypatch.setattr(_supervise_support, "_post_verdict", verdict)
    monkeypatch.setattr(_session_setup, "_pr", object())
    review = _session_config.STATE_ROOT / "review"
    review.mkdir(parents=True)
    (review / "passed.json").write_text(json.dumps({"round": 2, "sha": "abc", "at": 1.0}))
    rig.bridge._notes.clear()
    rig.start()
    await rig.advance(60)
    assert posts == []
    await rig.finish()


@pytest.mark.asyncio
async def test_a_restored_pass_brings_back_the_pr_link_and_the_round(rig):
    _inbox._save_pass(2, "abc", pr_url="https://github.com/acme/w/pull/7", verdict_posted=True)
    rig.bridge._notes.clear()
    rig.start()
    await rig.advance(1)
    assert rig.timeline.pr_url == "https://github.com/acme/w/pull/7"
    await rig.finish()


@pytest.mark.asyncio
async def test_the_round_ceiling_continues_after_a_restart(rig, monkeypatch):
    rounds = []

    async def review_round(workspace, brief, acceptance, round_, timeline):
        rounds.append(round_)
        return True, "", None, ""

    monkeypatch.setattr(_review, "_review_round", review_round)
    _inbox._save_pass(2, "abc", verdict_posted=True)
    rig.start()  # the Rig's note queue tells the session DONE again
    await rig.advance(60)
    # The DONE path hops through worker threads (as in `passed`): give it real time, not fake time.
    await wait_for(lambda: rounds or rig.task.done())
    assert rounds == [3], "the round count resumes from the saved pass"
    await rig.finish()


@pytest.mark.asyncio
async def test_the_review_passed_ask_names_the_pr(rig):
    rig.timeline.pr_url = "https://github.com/acme/w/pull/7"
    await passed(rig)
    sent = [m for m in rig.proc.submitted if "independent reviewer passed" in str(m)]
    assert sent and "https://github.com/acme/w/pull/7" in str(sent[0])
    await rig.finish()


@pytest.mark.asyncio
async def test_a_done_after_a_pass_at_the_round_limit_escalates_instead_of_going_silent(rig, monkeypatch):
    # qa: with the budget spent, the DONE cleared the pass but started no review, said nothing and never
    # released, holding the slot forever. A restored qa_round makes this reachable after any restart.
    monkeypatch.setattr(_session_config, "_QA_MAX_ROUNDS", 1)
    await passed(rig)
    rig.bridge._notes.append("DONE: one more change")
    await rig.advance(120)
    # The escalation's last step is its message to the session.
    await settle(rig, lambda: any("review budget" in str(text) for text in rig.proc.submitted[-1:]))
    assert _supervise_support._escalated
    assert any("🚨" in row and "review budget" in row for row in rig.rows()), rig.rows()
    assert any("review budget" in text for text in rig.proc.submitted[-1:]), rig.proc.submitted[-1:]
    await rig.advance(HALF_HOUR + 600)
    assert not rig.released, "an escalated project waits for the human, it is not released as passed"
    await rig.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize(("passed_in", "grants_left"), [(9, True), (10, False)])
async def test_a_pass_in_a_granted_round_restores_the_grants(rig, passed_in, grants_left):
    # qa: _granted_rounds was not restored, so after a restart a pass in round 9 needed two /review-again
    # to get one review, and a pass in round 10 could never be reviewed again. The rig's DONE then finds
    # the budget spent, and the escalation says whether a grant is left.
    _inbox._save_pass(passed_in, "A", verdict_posted=True)
    rig.start()
    await rig.advance(60)
    assert _session_config._granted_rounds == passed_in - _session_config._QA_MAX_ROUNDS
    assert _session_config._max_rounds() == passed_in
    await settle(rig, lambda: any("review budget" in str(text) for text in rig.proc.submitted[-1:]))
    message = rig.proc.submitted[-1]
    assert "review budget" in message
    if grants_left:
        assert "/review-again" in message
    else:
        assert "No more review rounds can be granted" in message and "/review-again" not in message
    await rig.finish()


async def test_restart_restores_milestone_history_and_pr_time(rig):
    original = _timeline._Timeline()
    original.verdicts = [
        {"round": 1, "verdict": "FAIL", "blocking": 2, "at": 100},
        {"round": 2, "verdict": "PASS", "blocking": 0, "at": 200},
    ]
    original.pr_at = 150
    original.phase("review", round_=2)
    _inbox._save_pass(2, "abc", "https://github.com/acme/w/pull/7", True, timeline=original)
    rig.bridge._notes.clear()
    rig.start()
    try:
        await rig.advance(1)
        assert rig.timeline.verdicts == original.verdicts
        assert rig.timeline.pr_at == 150
        assert rig.timeline.snapshot()["review"]["round"] == 2
    finally:
        await rig.finish()


async def test_milestone_comment_bound_survives_a_pod_restart(ending, monkeypatch):
    from datetime import datetime, timezone

    started = datetime(2026, 10, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(
        flyte,
        "ctx",
        lambda: SimpleNamespace(action=SimpleNamespace(run_name="r-own"), run_start_time=started),
    )
    bounds = []
    monkeypatch.setattr(_issues, "for_project", lambda *a, **kw: bounds.append(kw["started_at"]))
    await ending.run()
    assert bounds == [started.timestamp()]


async def test_restart_after_a_failed_round_restores_history_without_a_pass(rig):
    original = _timeline._Timeline()
    original.verdicts = [{"round": 1, "verdict": "FAIL", "blocking": 2, "at": 100}]
    original.pr_at = 150
    original.phase("review", round_=1)
    _inbox._unpass(1, original)
    rig.bridge._notes.clear()
    rig.start()
    try:
        await rig.advance(1)
        assert rig.timeline.verdicts == original.verdicts
        assert rig.timeline.pr_at == 150
        assert rig.timeline.snapshot()["review"]["round"] == 1
        assert rig.timeline._phase["kind"] != "done"
        assert not rig.released
    finally:
        await rig.finish()


def test_passed_json_of_the_previous_version_restores_as_passed(tmp_path, monkeypatch):
    path = tmp_path / "passed.json"
    path.write_text('{"round": 2, "sha": "abc", "pr_url": "", "review_round": 2}')
    monkeypatch.setattr(_session_config, "_passed_path", lambda: path)
    record = _session_config._load_pass_record()
    assert record is not None
    assert record["passed"] and record["verdicts"] == [] and record["review_round"] == 2
    assert _session_config._load_pass() == 2


def test_an_unpassed_record_is_no_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(_session_config, "_passed_path", lambda: tmp_path / "passed.json")
    _inbox._unpass(1, _timeline._Timeline())
    assert _session_config._load_pass() is None
    record = _session_config._load_pass_record()
    assert record is not None
    assert record["round"] == 1


@pytest.mark.asyncio
async def test_a_failed_round_persists_the_review_history(rig, monkeypatch):
    async def failing(workspace, brief, acceptance, round_, timeline):
        timeline.verdicts.append({"round": round_, "verdict": "FAIL", "blocking": 1, "at": 1.0})
        return False, "fix it", None, ""

    monkeypatch.setattr(_review, "_review_round", failing)
    rig.start()
    await rig.advance(60)
    # The in-progress record exists before the review finishes; wait for its verdict.
    await wait_for(lambda: (_session_config._load_pass_record() or {}).get("verdicts") or rig.task.done())
    record = _session_config._load_pass_record()
    assert record and not record["passed"] and record["round"] == 1
    assert [v["verdict"] for v in record["verdicts"]] == ["FAIL"]
    assert _session_config._load_pass() is None
    await rig.finish()


@pytest.mark.parametrize("verdict", ["FAIL", "PASS"])
async def test_done_then_restart_mid_review_keeps_history(rig, verdict):
    from flyte_agent_factory import _board

    original = _timeline._Timeline()
    original.verdicts = [
        {"round": 1, "verdict": verdict, "blocking": 2 if verdict == "FAIL" else 0, "at": 100}
    ]
    original.pr_at = 150
    original.phase("review", round_=1)
    _inbox._unpass(1, original)
    rig.review_gate = asyncio.Event()
    rig.start()
    try:
        # The review round is the activity (status line), not a timeline row.
        await rig.advance(60)
        # As in passed(), the DONE path crosses worker threads before publishing this status.
        await wait_for(lambda: "QA round 2 reviewing" in (rig.timeline._activity or "") or rig.task.done())
        assert "QA round 2 reviewing" in (rig.timeline._activity or ""), rig.timeline._activity
        record = _session_config._load_pass_record()
        assert record is not None
        assert record["in_review"] is True
        assert record["review_round"] == 2
        started_at = record["review_started_at"]
        assert started_at is not None
    finally:
        await rig.finish()
    rig.timeline = _timeline._Timeline()
    rig.stop = asyncio.Event()
    rig.bridge._notes.clear()
    rig.start()
    try:
        await rig.advance(1)
        assert rig.timeline.verdicts == original.verdicts
        assert rig.timeline.pr_at == 150
        assert rig.timeline.review_round == 2
        assert rig.timeline.review_started_at == started_at
        assert rig.timeline._phase["kind"] == "review"
        values = _board.desired(True, "https://console.example/runs/r1", rig.timeline.snapshot())
        assert values["Status"] == "In review"
        assert _session_config._load_pass() is None
    finally:
        await rig.finish()


async def test_legacy_pass_survives_snapshot_round_trip(rig):
    from flyte_agent_factory import _board, _dashboard

    _session_config._passed_path().parent.mkdir(parents=True, exist_ok=True)
    _session_config._passed_path().write_text('{"round":2,"sha":"abc"}')
    rig.bridge._notes.clear()
    rig.start()
    try:
        await rig.advance(1)
        data = _dashboard.from_report(_dashboard.status_script(rig.timeline.snapshot()))
        assert data is not None
        assert data["review"]["passed"] is True
        assert _board.desired(True, "https://console.example/runs/r1", data)["Status"] == "Done"
    finally:
        await rig.finish()


def test_withdrawing_a_pass_keeps_its_review_history(tmp_path, monkeypatch):
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path)
    timeline = _timeline._Timeline()
    timeline.verdicts = [{"round": 2, "verdict": "PASS", "blocking": 0, "at": 100}]
    timeline.pr_at = 90
    timeline.phase("review", round_=2)
    _inbox._save_pass(2, "abc", timeline=timeline)
    _inbox._clear_pass_keep_history()
    record = _session_config._load_pass_record()
    assert record is not None
    assert record["passed"] is False
    assert record["verdicts"] == timeline.verdicts
    assert record["pr_at"] == 90
    assert record["review_round"] == 2


@pytest.mark.parametrize("preflight", [False, True])
async def test_review_again_then_restart_keeps_history(rig, monkeypatch, preflight):
    monkeypatch.setattr(_session_config, "_granted_rounds", 0)
    monkeypatch.setattr(_supervise_support, "_preflight_resumes", 0)
    monkeypatch.setattr(_supervise_support, "_preflight_escalated", preflight)
    original = _timeline._Timeline()
    original.verdicts = [{"round": 2, "verdict": "PASS", "blocking": 0, "at": 100}]
    original.pr_at = 90
    original.phase("review", round_=2)
    _inbox._save_pass(2, "abc", timeline=original)
    rig.bridge._notes.clear()
    rig.start()
    try:
        await rig.advance(1)
        monkeypatch.setattr(_supervise_support, "_escalated", True)
        await _supervise_support._grant_round(rig.proc, rig.timeline, "/review-again fix it")
        await rig.advance(1)
        record = _session_config._load_pass_record()
        assert record and not record["passed"]
        assert record["verdicts"] == original.verdicts
        assert record["pr_at"] == 90
        assert record["review_round"] == 2
    finally:
        await rig.finish()
    rig.timeline = _timeline._Timeline()
    rig.stop = asyncio.Event()
    rig.start()
    try:
        await rig.advance(1)
        assert rig.timeline.verdicts == original.verdicts
        assert rig.timeline.pr_at == 90
        assert rig.timeline.review_round == 2
        assert not rig.timeline.review_passed
        assert _session_config._load_pass() is None
    finally:
        await rig.finish()


async def test_round_start_reopens_the_supervisors_done_item(rig, monkeypatch):
    from flyte_agent_factory import _board
    from tests.test_reconcile import ISSUE, URL, Board, board_profile

    board = Board({ISSUE: {"status": "Done", "texts": {"Flyte": URL}}}).install(monkeypatch)
    monkeypatch.setattr(
        _supervise_support, "_board_worker", _board.supervisor(board_profile(), "token", ISSUE)
    )
    original = _timeline._Timeline()
    original.verdicts = [{"round": 1, "verdict": "PASS", "blocking": 0, "at": 100}]
    original.phase("review", round_=1)
    _inbox._save_pass(1, "abc", timeline=original)
    rig.review_gate = asyncio.Event()
    rig.start()
    try:
        await rig.advance(60)
        # The move is written on a daemon thread, which a loaded host can hold past the advance's turns.
        await wait_for(lambda: board.items[ISSUE]["status"] == "In review", what="the Done item reopened")
        assert board.items[ISSUE]["status"] == "In review"
        assert rig.timeline.review_round == 2
    finally:
        await rig.finish()


@pytest.mark.parametrize("pr, status", [("https://github.com/x/y/pull/3", "In review"), ("", "Done")])
async def test_the_release_move_is_in_review_while_its_pull_request_awaits_a_merge(
    rig, monkeypatch, pr, status
):
    from flyte_agent_factory import _board
    from tests.test_reconcile import ISSUE, URL, Board, board_profile

    board = Board({ISSUE: {"status": "In flight", "texts": {"Flyte": URL}}}).install(monkeypatch)
    monkeypatch.setattr(
        _supervise_support, "_board_worker", _board.supervisor(board_profile(), "token", ISSUE)
    )
    await passed(rig)
    rig.timeline.pr_url = pr
    await rig.advance(HALF_HOUR + 120)
    await wait_for(lambda: board.items[ISSUE]["status"] == status, what="the release move")
    assert rig.released
    await rig.finish()


def test_withdrawing_a_legacy_pass_keeps_its_round(tmp_path, monkeypatch):
    path = tmp_path / "passed.json"
    path.write_text('{"round": 2, "sha": "abc", "review_round": 2}')
    monkeypatch.setattr(_session_config, "_passed_path", lambda: path)
    _inbox._clear_pass_keep_history()
    record = _session_config._load_pass_record()
    assert record and not record["passed"] and record["review_round"] == 2
