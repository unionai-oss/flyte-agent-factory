"""`/stop` works without an author: before the session exists, the inbox is already listening."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import cast

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
    _timeline,
    agent,
)
from flyte_agent_factory._bridge import Bridge
from flyte_agent_factory._delegation import Delegations
from tests._wait import wait_for


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """`project.func` with every collaborator faked; the real `_inbox_loop` reads `messages`."""
    workspace = tmp_path / "workspace"
    (workspace / _session_config._INBOX_DIR).mkdir(parents=True)
    timeline = _timeline._Timeline()
    events = SimpleNamespace(
        timeline=timeline,
        messages=asyncio.Queue(),
        session_may_start=asyncio.Event(),
        submitted=[],
        opened=[],
        session_started=False,
        start_error=None,
        stopped=False,
    )

    async def noop(*args, **kwargs):
        pass

    async def forever(*args, **kwargs):
        await asyncio.Event().wait()

    class Process:
        returncode = None
        tail = ()

        async def submit(self, text, **kwargs):
            events.submitted.append(text)

        async def wait(self):
            await asyncio.Event().wait()

        async def stop(self):
            events.stopped = True
            self.returncode = 0

    async def start(*args, **kwargs):
        await events.session_may_start.wait()
        if events.start_error:
            raise events.start_error
        events.session_started = True
        return Process()

    async def open_condition(base, seq, **kwargs):
        assert base == "direct"
        events.opened.append(seq)

        async def wait():
            return await events.messages.get()

        return SimpleNamespace(wait=SimpleNamespace(aio=wait)), seq

    async def restore(**kwargs):
        return SimpleNamespace(resumed=False)

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
    monkeypatch.setattr(_inbox, "open_condition", open_condition)
    monkeypatch.setattr(_supervise, "_pump", forever)
    monkeypatch.setattr(_inbox, "_watch_app_messages", forever)
    monkeypatch.setattr(_checkpoint, "_checkpoint_loop", forever)
    monkeypatch.setattr(Bridge, "run", forever)
    monkeypatch.setattr(Delegations, "run", forever)
    return events


async def until(predicate: Callable[[], object]) -> None:
    await wait_for(predicate)


async def test_stop_before_the_session_exists_ends_the_run_without_an_author_turn(harness):
    """A `/stop` sent before the session exists ends the run, and no author turn is spent."""
    run = asyncio.create_task(agent.project.func("brief", browser=False))
    await until(lambda: harness.opened)  # listening although the session cannot start yet
    assert not harness.session_started
    await harness.messages.put("/stop")
    harness.session_may_start.set()  # the session finishing its start later must not matter
    result, output = await asyncio.wait_for(run, 30)

    assert (
        "stopped on request" in result and cast(object, output) == "published-workspace"
    )  # the fake publish returns a str
    assert harness.submitted == [], "no turn may be spent on a run that was stopped"
    assert harness.stopped


async def test_stop_while_the_session_is_still_starting_does_not_wait_for_it(harness):
    """A `/stop` sent while the session is still starting does not wait for the start."""
    run = asyncio.create_task(agent.project.func("brief", browser=False))
    await until(lambda: harness.opened)
    await harness.messages.put("/stop")
    # The session start never completes: the run must end without it.
    result, _ = await asyncio.wait_for(run, 30)
    assert "stopped on request" in result
    assert not harness.session_started
    assert harness.submitted == []


async def test_a_message_then_stop_while_the_session_starts_is_heard_without_the_session(harness):
    """A message followed by `/stop` while the session starts is heard by the inbox alone."""
    run = asyncio.create_task(agent.project.func("brief", browser=False))
    await until(lambda: harness.opened)
    await harness.messages.put("please use port 9090")
    await until(lambda: len(harness.opened) >= 2)  # listening again although the message is undelivered
    await harness.messages.put("/stop")
    result, _ = await asyncio.wait_for(run, 30)
    assert "stopped on request" in result
    assert not harness.session_started
    assert harness.submitted == []


async def test_a_message_written_before_a_failed_session_start_is_delivered_on_the_retry(harness):
    """A message written before a failed session start is delivered once the retry succeeds."""
    harness.start_error = RuntimeError("claude would not start")
    run = asyncio.create_task(agent.project.func("brief", browser=False))
    await until(lambda: harness.opened)
    await harness.messages.put("please use port 9090")
    await until(lambda: len(harness.opened) >= 2)
    harness.session_may_start.set()
    with pytest.raises(RuntimeError, match="would not start"):
        await asyncio.wait_for(run, 30)
    assert harness.submitted == []

    harness.start_error = None
    harness.opened.clear()
    retry = asyncio.create_task(agent.project.func("brief", browser=False))
    await until(lambda: any("New direction arrived" in text for text in harness.submitted))
    await until(lambda: harness.opened)
    await harness.messages.put("/stop")
    await asyncio.wait_for(retry, 30)
    delivered = [text for text in harness.submitted if "New direction arrived" in text]
    assert len(delivered) == 1 and "001.md" in delivered[0]


async def test_a_message_before_the_session_is_delivered_once_after_it_starts(harness):
    """A message written before the session starts is delivered exactly once after it starts."""
    run = asyncio.create_task(agent.project.func("brief", browser=False))
    await until(lambda: harness.opened)
    await harness.messages.put("please use port 9090")
    await asyncio.sleep(0.05)
    assert harness.submitted == [], "there is no session to deliver to yet"
    harness.session_may_start.set()
    await until(lambda: any("New direction arrived" in text for text in harness.submitted))
    await until(lambda: len(harness.opened) >= 2)  # and it went back to listening
    await harness.messages.put("/stop")
    await asyncio.wait_for(run, 30)

    delivered = [text for text in harness.submitted if "New direction arrived" in text]
    assert len(delivered) == 1
