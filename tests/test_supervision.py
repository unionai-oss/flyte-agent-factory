"""The run's senses and controls must not fail silently.

All four background loops were gathered with `return_exceptions=True` at
shutdown, so a crash mid-run surfaced nowhere. A run was observed with no open
direction condition -- its inbox loop had stopped, which meant nothing could be
sent to it, `/stop` included, and the report said nothing at all.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import flyte
import pytest

from flyte_agent_factory import (
    _author,
    _checkpoint,
    _inbox,
    _session_config,
    _session_setup,
    _state,
    _supervise,
    _supervise_support,
    _timeline,
    agent,
)
from flyte_agent_factory._bridge import Bridge


async def _boom():
    raise RuntimeError("the control plane went away")


async def _forever():
    await asyncio.sleep(3600)


@pytest.mark.asyncio
async def test_a_dead_loop_is_reported():
    timeline = _timeline._Timeline()
    stop = asyncio.Event()

    await _supervise_support._supervised(_boom(), "checkpoint", timeline, stop)

    rendered = timeline.render()
    assert "the checkpoint loop stopped" in rendered
    assert "the control plane went away" in rendered


@pytest.mark.asyncio
async def test_losing_the_checkpoint_loop_does_not_end_the_run():
    """Bad, but the session is still working and still steerable."""
    timeline = _timeline._Timeline()
    stop = asyncio.Event()

    await _supervise_support._supervised(_boom(), "checkpoint", timeline, stop)

    assert not stop.is_set()


@pytest.mark.asyncio
async def test_losing_the_inbox_ends_the_run_cleanly():
    """Without the inbox the run cannot be steered or stopped.

    A human would have to abort, and an abort discards everything since the
    last checkpoint. Ending deliberately publishes instead.
    """
    timeline = _timeline._Timeline()
    stop = asyncio.Event()

    await _supervise_support._supervised(_boom(), "inbox", timeline, stop)

    assert stop.is_set()
    assert "cannot be steered or stopped" in timeline.render()


@pytest.mark.asyncio
async def test_cancellation_is_not_an_error():
    """Shutdown cancels these deliberately; that must not look like a crash."""
    timeline = _timeline._Timeline()
    stop = asyncio.Event()

    task = asyncio.create_task(_supervise_support._supervised(_forever(), "pump", timeline, stop))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "stopped" not in timeline.render()
    assert not stop.is_set()


async def _run_project(tmp_path, monkeypatch, pump):
    """Run `agent.project` to a clean exit on fakes; `pump(timeline)` runs the session, then it exits."""
    from types import SimpleNamespace

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    timeline = _timeline._Timeline()
    exited = asyncio.Event()

    async def noop(*args, **kwargs):
        pass

    async def forever(*args, **kwargs):
        await asyncio.Event().wait()

    async def session(*args, **kwargs):
        try:
            await pump(timeline)
        finally:
            exited.set()

    class Process:
        returncode = None
        tail = ()

        async def submit(self, text, **kwargs):
            pass

        async def wait(self):
            await exited.wait()
            self.returncode = 0
            return 0

        async def stop(self):
            self.returncode = 0

    async def start(*args, **kwargs):
        return Process()

    state = SimpleNamespace(resumed=False)

    async def restore(**kwargs):
        return state

    async def publish(*args):
        return "published-workspace"

    monkeypatch.setattr(_session_config, "WORKSPACE", workspace)
    monkeypatch.setattr(_timeline, "_Timeline", lambda: timeline)
    monkeypatch.setattr(timeline, "flush", noop)
    monkeypatch.setattr(flyte, "ctx", lambda: None)
    monkeypatch.setattr(_state, "restore", restore)
    monkeypatch.setattr(_state, "save_checkpoint", noop)
    monkeypatch.setattr(_state, "publish", publish)
    for name in (
        "assert_claude_available",
        "write_settings",
        "mark_trusted",
    ):
        monkeypatch.setattr(agent, name, lambda *args, **kwargs: None)
    for name in ("_write_managed_block", "_install_bridge_commands"):
        monkeypatch.setattr(_session_setup, name, lambda *args, **kwargs: None)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
    monkeypatch.setattr(_session_setup, "_prepare_workspace", noop)
    monkeypatch.setattr(agent, "write_context", lambda *args: workspace / "context")
    monkeypatch.setattr(_author, "_start_session", start)
    monkeypatch.setattr(_supervise, "_pump", session)
    for name in ("_inbox_loop", "_watch_app_messages"):
        monkeypatch.setattr(_inbox, name, forever)
    monkeypatch.setattr(_checkpoint, "_checkpoint_loop", forever)
    monkeypatch.setattr(Bridge, "run", forever)

    result, output = cast(tuple[str, Any], await agent.project.func("brief", browser=False))
    return timeline, result, output


@pytest.mark.asyncio
async def test_clean_exit_still_has_a_final_row_after_pump_failure(tmp_path, monkeypatch):
    async def broken_pump(timeline):
        raise ValueError("pump failed before the session exited")

    timeline, result, output = await _run_project(tmp_path, monkeypatch, broken_pump)

    assert "session ended cleanly" in result
    assert output == "published-workspace"
    assert "the pump loop stopped" in timeline.render()
    assert [text for _, icon, text in timeline._rows if icon == "🏁"] == ["session ended cleanly"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "verdicts, moved",
    [
        ([], ["Stopped"]),  # /stop before review
        ([{"round": 1, "verdict": "FAIL"}], ["Stopped"]),  # /stop after a failed round
        ([{"round": 1, "verdict": "PASS"}], []),  # passed: the end is not a stop
    ],
)
async def test_a_clean_end_without_a_pass_moves_the_card_to_stopped(tmp_path, monkeypatch, verdicts, moved):
    """#48: a session that exits cleanly before review passed is unfinished, not Done."""
    calls: list[str] = []

    async def move(status, timeline, **kwargs):
        calls.append(status)

    async def reviewed(timeline):
        timeline.verdicts = list(verdicts)

    monkeypatch.setattr(_supervise_support, "_move_board", move)
    timeline, result, _ = await _run_project(tmp_path, monkeypatch, reviewed)

    assert "session ended cleanly" in result
    assert [status for status in calls if status == "Stopped"] == moved
    assert timeline.snapshot()["phase"]["kind"] == "done"
