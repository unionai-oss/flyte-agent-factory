"""Direction while the author is paused on a usage limit: the engine grammar, `/stop`, and queued text."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from flyte_agent_factory import _inbox, _session_config, _timeline
from flyte_agent_factory._author_session import AuthorSession
from tests._wait import wait_for


@pytest.mark.parametrize(
    ("text", "engine"),
    [
        ("/engine claude", "claude"),
        ("/engine Codex", "codex"),
        ("/resume claude", "claude"),
        ("/resume on codex", "codex"),
        ("resume on claude", "claude"),
        ("Resume now on Claude.", "claude"),
        ("switch to codex!", "codex"),
        ("  SWITCH TO CLAUDE  ", "claude"),
        # Near-misses: anything more than the bare command is an ordinary direction for the session.
        ("please resume on claude when you can", None),
        ("resume on claude when you can", None),
        ("switch to codex for the tests", None),
        ("don't switch to codex", None),
        ("/engine gpt", None),
        ("/engine", None),
        ("resume on", None),
        ("resume", None),
        ("", None),
    ],
)
def test_engine_direction_grammar(text, engine):
    assert _inbox.engine_direction(text) == engine


class Engine:
    """An engine process that records what it is sent; paused sessions must not send it anything."""

    returncode: int | None = None
    limit_hit_at: float | None = None

    def __init__(self) -> None:
        self.submitted: list[str] = []
        self.stopped = False

    async def submit(self, text: str, *, label: str = "") -> None:
        self.submitted.append(text)

    async def stop(self, **kwargs: float) -> None:
        self.stopped = True
        self.returncode = 0


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    """The real `_inbox_loop` over a throwaway workspace; `messages` feeds its `direct-` conditions."""
    workspace = tmp_path / "ws"
    (workspace / _session_config._INBOX_DIR).mkdir(parents=True)
    monkeypatch.setattr(_session_config, "WORKSPACE", workspace)
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(_inbox, "_directions", [])
    monkeypatch.setattr(_inbox, "_seen_human_entries", set())
    events = SimpleNamespace(messages=asyncio.Queue(), opened=[], dir=workspace / _session_config._INBOX_DIR)

    async def open_condition(base, seq, **kwargs):
        assert base == "direct"
        events.opened.append(seq)

        async def wait():
            return await events.messages.get()

        return SimpleNamespace(wait=SimpleNamespace(aio=wait)), seq

    monkeypatch.setattr(_inbox, "open_condition", open_condition)
    return events


def paused_session(tmp_path) -> tuple[AuthorSession, Engine, _timeline._Timeline]:
    """A session paused on a limit an hour away, as the pump leaves it."""
    engine = Engine()
    session = AuthorSession(engine, tmp_path / "state" / "pending.json")
    session.pause()
    timeline = _timeline._Timeline()
    timeline.phase("limited", limited_until=time.time() + 3600)
    return session, engine, timeline


def queued(seq: int) -> str:
    """What the inbox hands the session for message `seq`."""
    return f"New direction arrived from Flyte. Read {_session_config._INBOX_DIR}/{seq:03d}.md and act on it."


async def grant_round(*args, **kwargs):
    raise AssertionError("no /review-again was sent")


def loop(session, timeline, stop, on_engine=None) -> asyncio.Task[None]:
    return asyncio.create_task(_inbox._inbox_loop(session, timeline, stop, grant_round, on_engine=on_engine))


async def stop_loop(inbox, task: asyncio.Task[None], stop: asyncio.Event) -> None:
    await inbox.messages.put("/stop")
    await asyncio.wait_for(task, 30)
    assert stop.is_set()


async def test_stop_while_paused_ends_session(inbox, tmp_path):
    """`/stop` while limited ends the session at once, as it does while running, without a turn."""
    session, engine, timeline = paused_session(tmp_path)
    stop = asyncio.Event()
    task = loop(session, timeline, stop, on_engine=lambda e: pytest.fail("not an engine direction"))
    await wait_for(lambda: inbox.opened)

    started = time.monotonic()
    await stop_loop(inbox, task, stop)
    await _inbox._stop_author(session, timeline)

    assert time.monotonic() - started < 60, "the stop must not wait for the reset an hour away"
    assert engine.stopped and engine.returncode == 0
    assert engine.submitted == [] and session.pending == []
    assert any(text == "stop requested" for _, _, text in timeline._rows)


async def test_engine_direction_calls_on_engine_and_is_not_delivered(inbox, tmp_path):
    """`resume on claude` is acted on and recorded, and the session never sees it."""
    session, engine, timeline = paused_session(tmp_path)
    calls: list[str] = []

    def on_engine(name: str) -> str:
        calls.append(name)
        return f"direction: resume on {name}; switching when the next attempt starts"

    stop = asyncio.Event()
    task = loop(session, timeline, stop, on_engine=on_engine)
    await wait_for(lambda: inbox.opened)
    await inbox.messages.put("Resume now on Claude.")
    await wait_for(lambda: len(inbox.opened) >= 2, what="the next direct- condition")

    assert calls == ["claude"]
    texts = [text for _, _, text in timeline._rows]
    assert "direction: resume on claude; switching when the next attempt starts" in texts
    assert _inbox._directions == ["Union: Resume now on Claude."]  # the handoff prompt shows it
    assert engine.submitted == [] and session.pending == []
    assert list(inbox.dir.iterdir()) == [], "no inbox file and no .pending marker"
    await stop_loop(inbox, task, stop)


async def test_near_miss_is_an_ordinary_queued_direction(inbox, tmp_path):
    """A sentence that merely mentions resuming is queued for the session like any other direction."""
    session, engine, timeline = paused_session(tmp_path)
    stop = asyncio.Event()
    task = loop(session, timeline, stop, on_engine=lambda e: pytest.fail(f"took {e} as an engine direction"))
    await wait_for(lambda: inbox.opened)
    await inbox.messages.put("please resume on claude when you can")
    await wait_for(lambda: session.pending, what="the direction queued while paused")

    assert (inbox.dir / "001.md").read_text() == "please resume on claude when you can\n"
    assert session.pending == [queued(1)]
    assert engine.submitted == []
    await stop_loop(inbox, task, stop)


async def test_engine_direction_without_a_handler_is_an_ordinary_direction(inbox, tmp_path):
    """A caller that cannot switch engines (no `on_engine`) still gets the text, as before."""
    session, engine, timeline = paused_session(tmp_path)
    stop = asyncio.Event()
    task = loop(session, timeline, stop)
    await wait_for(lambda: inbox.opened)
    await inbox.messages.put("switch to codex")
    await wait_for(lambda: session.pending)
    assert (inbox.dir / "001.md").read_text() == "switch to codex\n"
    await stop_loop(inbox, task, stop)


async def test_other_direction_while_paused_delivered_once_on_resume(inbox, tmp_path):
    """Text sent while paused reaches the engine once, with the resume, and never again."""
    session, engine, timeline = paused_session(tmp_path)
    stop = asyncio.Event()
    task = loop(session, timeline, stop, on_engine=lambda e: pytest.fail("not an engine direction"))
    await wait_for(lambda: inbox.opened)
    await inbox.messages.put("use port 9090")
    await wait_for(lambda: session.pending and not list(inbox.dir.glob("*.pending")))
    assert engine.submitted == [], "nothing reaches a paused engine"
    await stop_loop(inbox, task, stop)

    await session.resume("Your usage limit has reset. Continue.")
    await session.resume("Continue.")  # a second resume carries nothing already delivered
    delivered = [text for text in engine.submitted if "inbox/001.md" in text]
    assert len(delivered) == 1 and delivered[0] is engine.submitted[0]
    # A restart after the resume finds nothing left to deliver, in either queue.
    assert AuthorSession(Engine(), tmp_path / "state" / "pending.json").pending == []
    assert not list(inbox.dir.glob("*.pending"))


async def test_next_direct_condition_opens_after_paused_delivery(inbox, tmp_path):
    """A direction queued while paused does not stop the loop from listening for the next one."""
    session, engine, timeline = paused_session(tmp_path)
    stop = asyncio.Event()
    task = loop(session, timeline, stop, on_engine=lambda e: pytest.fail("not an engine direction"))
    await wait_for(lambda: inbox.opened)
    await inbox.messages.put("first")
    await wait_for(lambda: len(session.pending) == 1)
    await wait_for(lambda: len(inbox.opened) >= 2, what="a new direct- condition after a paused delivery")
    await inbox.messages.put("second")
    await wait_for(lambda: len(session.pending) == 2)
    await wait_for(lambda: len(inbox.opened) >= 3)

    assert not task.done(), "the loop is still listening"
    assert session.pending == [queued(1), queued(2)]
    await stop_loop(inbox, task, stop)


async def test_pending_marker_survives_handoff_restart(inbox, tmp_path):
    """A direction that arrives while the author is being replaced is delivered once by the next attempt."""
    replacing: asyncio.Future[AuthorSession] = asyncio.get_running_loop().create_future()
    timeline = _timeline._Timeline()
    stop = asyncio.Event()
    task = loop(replacing, timeline, stop)
    await wait_for(lambda: inbox.opened)
    await inbox.messages.put("use port 9090")
    await wait_for(lambda: len(inbox.opened) >= 2)
    # `AuthorHandoffRestart` ends the attempt: the loop is cancelled before any session took the message.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not replacing.cancelled(), "the loop must not cancel the caller's future for the session"
    assert (inbox.dir / "001.pending").exists()

    session, engine, timeline = paused_session(tmp_path)
    stop = asyncio.Event()
    inbox.opened.clear()
    task = loop(session, timeline, stop)
    await wait_for(lambda: session.pending and not (inbox.dir / "001.pending").exists())
    await wait_for(lambda: inbox.opened)
    assert inbox.opened == [2], "the next message must not overwrite 001.md"
    assert session.pending == [queued(1)]
    await stop_loop(inbox, task, stop)


async def test_engine_direction_the_handler_has_nothing_to_say_about_adds_no_row(inbox, tmp_path):
    """`on_engine` returns "" when the direction changes nothing (say, that engine is already running)."""
    session, engine, timeline = paused_session(tmp_path)
    calls: list[str] = []

    def on_engine(name: str) -> str:
        calls.append(name)
        return ""

    stop = asyncio.Event()
    task = loop(session, timeline, stop, on_engine=on_engine)
    await wait_for(lambda: inbox.opened)
    await inbox.messages.put("Resume now on Claude.")
    await wait_for(lambda: len(inbox.opened) >= 2, what="the next direct- condition")

    assert calls == ["claude"]
    # Only the recorded direction itself: no row for what the handler did.
    assert [text for _, _, text in timeline._rows] == ["direction (Union): Resume now on Claude."]
    assert engine.submitted == [] and session.pending == []
    await stop_loop(inbox, task, stop)


async def test_review_again_is_handed_to_grant_round_and_its_failure_ends_the_loop(inbox, tmp_path):
    session, _, timeline = paused_session(tmp_path)
    task = loop(session, timeline, asyncio.Event())
    await wait_for(lambda: inbox.opened)
    await inbox.messages.put("/review-again")
    with pytest.raises(ExceptionGroup) as failure:
        await asyncio.wait_for(task, 30)
    assert [str(e) for e in failure.value.exceptions] == ["no /review-again was sent"]
