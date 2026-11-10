"""`/stop` is honoured from every channel the owner uses, without an author turn, and never hangs."""

import asyncio
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from flyte_agent_factory import _inbox, _session_config, _timeline, agent
from flyte_agent_factory._author_session import AuthorSession
from flyte_agent_factory._process import ClaudeProcess


def _app_message(tmp_path: Path, text: str) -> None:
    folder = tmp_path / "claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(tmp_path / "ws"))
    folder.mkdir(parents=True, exist_ok=True)
    # Stamped ahead of now: the watcher only takes messages typed after it started.
    stamp = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    entry = {
        "type": "user",
        "uuid": "u-stop",
        "timestamp": stamp,
        "origin": {"kind": "human"},
        "entrypoint": "cli",
        "message": {"role": "user", "content": text},
    }
    (folder / "s.jsonl").write_text(json.dumps(entry) + "\n")


@pytest.fixture
def supervisor(monkeypatch, tmp_path):
    """`agent` pointed at a throwaway config dir, workspace and state, with no history of app messages."""
    monkeypatch.setattr(_session_config, "CONFIG_DIR", tmp_path / "claude")
    monkeypatch.setattr(_session_config, "WORKSPACE", tmp_path / "ws")
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(_inbox, "_seen_human_entries", set())
    monkeypatch.setattr(_inbox, "_directions", [])
    (tmp_path / "ws" / _session_config._INBOX_DIR).mkdir(parents=True)
    return agent


@pytest.mark.parametrize("text", ["/stop", "stop", "/exit", " Stop "])
async def test_stop_typed_in_the_claude_app_stops_the_run(supervisor, tmp_path, text):
    """A stop typed in the Claude app ends the run without spending an author turn."""
    _app_message(tmp_path, text)
    submitted = []

    async def submit(message, **kwargs):  # an exhausted session can take no turn
        submitted.append(message)

    proc = SimpleNamespace(sent_texts=[], submit=submit)
    stop = asyncio.Event()
    async with asyncio.timeout(30):
        await _inbox._watch_app_messages(cast(Any, proc), _timeline._Timeline(), stop)
    assert stop.is_set()
    assert submitted == []


async def test_other_app_chat_does_not_stop_the_run(supervisor, tmp_path):
    """Chat that merely mentions stopping is not a stop request."""
    _app_message(tmp_path, "please stop using the old API")
    stop = asyncio.Event()
    watcher = asyncio.create_task(
        _inbox._watch_app_messages(cast(Any, SimpleNamespace(sent_texts=[])), _timeline._Timeline(), stop)
    )
    await asyncio.sleep(0.3)
    assert not stop.is_set()
    stop.set()
    await watcher


async def test_a_session_that_never_takes_the_exit_message_is_still_killed(tmp_path, monkeypatch):
    """A session that never takes the exit message is killed once the grace period ends."""
    proc = ClaudeProcess(
        [sys.executable, "-c", "import time; time.sleep(300)"], cwd=tmp_path, env={"PATH": "/usr/bin"}
    )
    await proc.start()

    async def wedged(text, *, label=""):  # e.g. a delivery lock held by a turn that never ends
        await asyncio.sleep(3600)

    monkeypatch.setattr(proc, "submit", wedged)  # the test wedges the real process's submit
    # The process sleeps for 300 s: stop() returning inside 120 s means it was killed after the grace.
    await asyncio.wait_for(proc.stop(grace=1.0), 120)
    assert proc.returncode is not None


async def test_stopping_a_paused_author_whose_stop_hangs_is_bounded(supervisor, monkeypatch, tmp_path):
    """A paused author whose stop hangs is cut off at the stop timeout, so the run still ends."""
    hung = SimpleNamespace(returncode=None, limit_hit_at=1.0)

    async def stop(**kwargs):
        await asyncio.sleep(3600)

    hung.stop = stop
    session = AuthorSession(hung, tmp_path / "pending.json")
    session.pause()
    monkeypatch.setattr(_inbox, "_AUTHOR_STOP_TIMEOUT_S", 0.2, raising=False)
    timeline = _timeline._Timeline()
    async with asyncio.timeout(30):
        await _inbox._stop_author(session, timeline)
