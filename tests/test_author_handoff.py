"""Author engine transitions preserve the work item and use fresh conversations."""

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace

import flyte
import flyte.report
import pytest

from flyte_agent_factory import (
    _author,
    _checkpoint,
    _inbox,
    _meter,
    _pins,
    _session_config,
    _session_setup,
    _supervise,
    _supervise_support,
    _timeline,
    _usage,
    agent,
)
from flyte_agent_factory._author_session import AuthorSession
from flyte_agent_factory._codex_process import CodexAuthor
from tests import test_codex_author
from tests._wait import wait_for
from tests.test_nudge import _agent_time

# `project.func` with its cloud edges replaced, as the Codex author tests use it.
project = test_codex_author.project


def _delivered_prompt(workspace, log):
    return (log.read_text() if log.exists() else "") + "".join(
        path.read_text() for path in sorted((workspace / ".claude-flyte" / "inbox").glob("message-*.md"))
    )


class _Session(AuthorSession):
    """The supervisor sets `on_limit` on the real session."""

    on_limit: Callable[..., Awaitable[bool]]


class Process:
    returncode: int | None = None
    limit_hit_at = None
    last_submission = None

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.stopped = False

    async def submit(self, text: str, *, label: str = "") -> None:
        self.messages.append(text)

    async def stop(self, *, grace: float = 20.0) -> None:
        self.stopped = True
        self.returncode = 0


async def test_paused_messages_are_durable_and_transferred_once(tmp_path):
    old = Process()
    session = AuthorSession(old, tmp_path / "pending.json")
    session.pause()
    await session.submit("pending direction")
    await session.submit("open findings")
    assert old.messages == []
    assert json.loads((tmp_path / "pending.json").read_text()) == ["pending direction", "open findings"]
    restored = AuthorSession(old, tmp_path / "pending.json")
    assert restored.pending == session.pending
    new = Process()
    await session.replace(new, "HANDOFF")
    assert old.stopped
    assert new.messages == ["HANDOFF\n\npending direction\n\nopen findings"]
    assert session.pending == []
    assert json.loads((tmp_path / "pending.json").read_text()) == []


async def test_waiter_does_not_treat_reaped_author_as_project_completion(tmp_path):
    old = Process()
    session = AuthorSession(old, tmp_path / "pending.json")
    waiter = asyncio.create_task(session.wait_for_exit())
    new = Process()
    await session.replace(new, "handoff")
    await asyncio.sleep(0.02)
    assert not waiter.done()
    new.returncode = 7
    assert await asyncio.wait_for(waiter, 30) == 7


@pytest.mark.parametrize("source,target", [("claude", "codex"), ("codex", "claude")])
async def test_refusal_routes_to_other_credential(monkeypatch, source, target):
    monkeypatch.setattr(_usage, "_author_engine", source)
    monkeypatch.setattr(_usage, "_author_engines", lambda: ("claude", "codex"))
    monkeypatch.setattr(_usage, "_hard_pins", False)
    monkeypatch.setattr(_usage, "_child_meter", lambda _: None)
    decision = _author._author_refusal_route(
        _pins.Pin("", False, ""), _timeline._Timeline(), source, 100000000000
    )
    assert decision.engine == target


@pytest.mark.parametrize(
    "available,pin,hard", [(("claude",), "", False), (("claude", "codex"), "claude", True)]
)
def test_pin_or_missing_credential_waits(monkeypatch, available, pin, hard):
    monkeypatch.setattr(_usage, "_author_engine", "claude")
    monkeypatch.setattr(_usage, "_author_engines", lambda: available)
    monkeypatch.setattr(_usage, "_hard_pins", hard)
    monkeypatch.setattr(_usage, "_child_meter", lambda _: None)
    decision = _author._author_refusal_route(
        _pins.Pin(pin, hard, "test"), _timeline._Timeline(), "claude", 100000000000
    )
    assert decision.deferred


def test_both_exhausted_wait_for_earliest_reset(monkeypatch):
    now = _agent_time().time()
    monkeypatch.setattr(_usage, "_author_engines", lambda: ("claude", "codex"))
    monkeypatch.setattr(_usage, "_hard_pins", False)
    monkeypatch.setattr(
        _usage, "_child_meter", lambda e: _meter.Meter(e, "test", now, limited_until=now + 100)
    )
    decision = _author._author_refusal_route(
        _pins.Pin("", False, ""), _timeline._Timeline(), "claude", now + 200
    )
    assert decision.deferred and decision.retry_at == now + 100


async def test_saved_owner_wins_over_older_transcripts(tmp_path, monkeypatch):
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(_author, "has_conversation", lambda _: True)
    (tmp_path / "author-conversation.json").write_text(json.dumps({"engine": "claude"}))
    monkeypatch.setattr(CodexAuthor, "saved_thread", lambda _: "older-codex-thread")
    assert (
        await _author._choose_author_engine(
            _pins.Pin("", False, ""), ("claude", "codex"), True, _timeline._Timeline()
        )
        == "claude"
    )
    monkeypatch.setattr(_usage, "_hard_pins", False)
    monkeypatch.setattr(_usage, "_meters", None)
    (tmp_path / "author-conversation.json").write_text(json.dumps({"engine": "claude", "pin": "codex"}))
    # An explicit pin beats the owner's stickiness (it used to keep a handoff's engine): the pinned
    # engine takes the project back unless it is exhausted (see test_pin_and_credential.py).
    assert (
        await _author._choose_author_engine(
            _pins.Pin("codex", False, "engine input"), ("claude", "codex"), True, _timeline._Timeline()
        )
        == "codex"
    )


async def test_handoff_checkpoint_keeps_git_visible_work_and_excludes_private_files(tmp_path, monkeypatch):
    import shutil
    import subprocess

    from flyte_agent_factory import _state

    root = tmp_path / "state"
    work = root / "workspace"
    work.mkdir(parents=True)
    subprocess.run(["git", "init", str(work)], check=True, capture_output=True)
    (work / ".gitignore").write_text("dataset/\n.claude-flyte/\n")
    (work / "dataset").mkdir()
    (work / "dataset" / "large").write_text("ignored data")
    (work / "patch.py").write_text("uncommitted work")
    (work / ".claude-flyte").mkdir()
    (work / ".claude-flyte" / "BRIEF.md").write_text("brief")
    (root / "codex").mkdir()
    (root / "codex" / "auth.json").write_text("secret")
    captured = tmp_path / "captured"

    async def save(path):
        shutil.copytree(path, captured)

    monkeypatch.setattr(_state, "_checkpoint", lambda: SimpleNamespace(save=save))
    await _state.save_checkpoint(_state.ProjectState(root, ""), exclude_private=True)
    assert (captured / "workspace" / "patch.py").read_text() == "uncommitted work"
    assert (captured / "workspace" / ".claude-flyte" / "BRIEF.md").is_file()
    assert not (captured / "workspace" / "dataset").exists()
    # Kept: it is the rotated login, and the mounted secret may already be spent (#64).
    assert (captured / "codex" / "auth.json").read_text() == "secret"
    assert (root / "codex" / "auth.json").read_text() == "secret"


async def test_handoff_checkpoint_never_carries_the_setup_token(tmp_path, monkeypatch):
    import shutil

    from flyte_agent_factory import _state

    root = tmp_path / "state"
    state = _state.ProjectState(root, "")
    (root / "workspace").mkdir(parents=True)
    state.config_dir.mkdir(parents=True)
    (root / "author-conversation.json").write_text(json.dumps({"engine": "claude"}))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-setup-token")
    captured = tmp_path / "captured"

    async def save(path):
        shutil.copytree(path, captured)

    monkeypatch.setattr(_state, "_checkpoint", lambda: SimpleNamespace(save=save))
    await _state.save_checkpoint(state, exclude_private=True)
    assert not any(
        "sk-setup-token" in p.read_text(errors="ignore") for p in captured.rglob("*") if p.is_file()
    )


@pytest.mark.parametrize("source,target", [("claude", "codex"), ("codex", "claude")])
@pytest.mark.parametrize("wait_for_reset", [False, True])
async def test_handoff_uses_fresh_process_with_findings_and_direction(
    tmp_path, monkeypatch, source, target, wait_for_reset
):
    import os
    import subprocess
    import sys
    from pathlib import Path

    from flyte_agent_factory import _state
    from flyte_agent_factory._codex_process import CodexAuthor
    from flyte_agent_factory._process import ClaudeProcess

    work = tmp_path / "workspace"
    work.mkdir()
    subprocess.run(["git", "init", str(work)], check=True, capture_output=True)
    config = tmp_path / "claude"
    config.mkdir()
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(_session_config, "WORKSPACE", work)
    monkeypatch.setattr(_session_config, "CONFIG_DIR", config)
    monkeypatch.setattr(_session_config, "CODEX_HOME_DIR", tmp_path / "codex")
    monkeypatch.setattr(_author, "_CODEX_THREAD_FILE", tmp_path / "codex-thread")
    for name, value in (
        ("_author_engine", source),
        ("_meters", None),
        ("_hard_pins", False),
    ):
        monkeypatch.setattr(_usage, name, value)
    monkeypatch.setattr(_inbox, "_directions", ["Union: Keep the public API"])
    monkeypatch.setattr(_usage, "_author_engines", lambda: ("claude", "codex"))
    monkeypatch.setattr(_session_setup, "_log_session_line", lambda _: None)
    monkeypatch.setattr(
        _author, "_CODEX_COMMAND", (sys.executable, str(Path(__file__).with_name("fake_codex.py")))
    )
    monkeypatch.setenv(_session_config.CODEX_TOKEN_ENV_VAR, "{}")
    monkeypatch.setattr(agent, "assert_claude_available", lambda: None)
    script = tmp_path / "script.json"
    script.write_text("[]")
    log = tmp_path / "claude.log"
    starts = []

    async def start(candidates, **kwargs):
        starts.append(candidates)
        current = ClaudeProcess(
            [sys.executable, str(Path(__file__).with_name("fake_claude.py"))],
            cwd=work,
            env={**os.environ, "FAKE_CLAUDE_SCRIPT": str(script), "FAKE_CLAUDE_LOG": str(log)},
            prompt_rules=[],
            inbox_messages=True,
        )
        await current.start()
        return current

    monkeypatch.setattr(_author, "_start_session", start)
    snapshots = []

    async def checkpoint(state, **kwargs):
        snapshots.append(json.loads((tmp_path / "author-conversation.json").read_text()))
        return True

    monkeypatch.setattr(_state, "save_checkpoint", checkpoint)
    if source == "codex":
        old = _author._start_codex_author(_timeline._Timeline(), model="", effort="")
        old._env["FAKE_CODEX_LIMIT"] = "1"
        await old.submit("interrupted direction")
        await old.wait()
        old_id = old.thread_id
    else:
        old = await start([])
        old_id = None
    session = AuthorSession(old, tmp_path / "author-pending.json")
    session.pause()
    await session.submit("queued human message")
    timeline = _timeline._Timeline()
    timeline.open_findings = ["Fix the race in shutdown"]
    callback = _author._author_handoff(
        session,
        _state.ProjectState(tmp_path, ""),
        timeline,
        brief="Build the feature",
    )
    try:
        now = _agent_time().time()
        if wait_for_reset:
            monkeypatch.setattr(
                _usage, "_child_meter", lambda e: _meter.Meter(e, "test", now, limited_until=now + 10)
            )
            assert not await callback(now + 3600, "interrupted direction")
            assert session.retry_at == now + 10
            assert session.pending.count("queued human message") == 1
            assert session.pending.count("interrupted direction") == 1
            assert snapshots[-1]["engine"] == source
            monkeypatch.setattr(_agent_time(), "time", lambda: now + 20)
        with pytest.raises(_author.AuthorHandoffRestart):
            await callback(now + 3600, "interrupted direction")
        assert session.current is old
        assert snapshots[-1]["engine"] == target and snapshots[-1]["fresh"] is True
        if target == "codex":
            current = _author._start_codex_author(timeline, model="", effort="")
        else:
            owner = json.loads((tmp_path / "author-conversation.json").read_text())
            current = await start([["claude", "--session-id", owner["thread"]]])
        session = AuthorSession(current, tmp_path / "author-pending.json")
        await _author._resume_author_pending(session, target)
        await _state.save_checkpoint(None)  # type: ignore[arg-type]  # the test's flyte world ignores the state
        if target == "codex":
            assert isinstance(session.current, CodexAuthor)
            await session.current.wait()
            assert session.current.thread_id != old_id
            prompt = next((tmp_path / "codex" / "threads").iterdir()).read_text()
        else:
            async with asyncio.timeout(30):
                while "AUTHOR HANDOFF" not in _delivered_prompt(work, log):
                    await asyncio.sleep(0.05)
            prompt = _delivered_prompt(work, log)
            assert all("--resume" not in argv and "--continue" not in argv for argv in starts[-1])
        for text in (
            "AUTHOR HANDOFF",
            "UNVERIFIED",
            "Build the feature",
            "Fix the race in shutdown",
            "Keep the public API",
        ):
            assert text in prompt
        assert prompt.count("queued human message") == 1
        assert prompt.count("interrupted direction") == 1
        assert session.pending == []
        assert snapshots[-2]["engine"] == target and snapshots[-2]["fresh"] is True
        assert snapshots[-1]["engine"] == target and "fresh" not in snapshots[-1]
        assert snapshots[-1]["thread"]
        assert _session_config._author_handoff_records()[0]["new_thread"] == snapshots[-1]["thread"]
        assert any(f"author handoff {source} -> {target}" in row for _, _, row in timeline._rows)
        assert (
            await _author._choose_author_engine(_pins.Pin("", False, ""), ("claude", "codex"), True, timeline)
            == target
        )
    finally:
        await session.stop(grace=1)


@pytest.mark.parametrize("restart", [False, True])
async def test_large_queued_messages_have_a_bounded_handoff_and_a_full_artifact(tmp_path, restart):
    session = AuthorSession(Process(), tmp_path / "pending.json")
    session.pause()
    message = "direction " * 5000
    await session.submit(message)
    new = Process()
    if restart:
        session = AuthorSession(new, tmp_path / "pending.json")
        await session.resume("HANDOFF")
    else:
        await session.replace(new, "HANDOFF")
    assert len(new.messages[0]) < 17000
    assert next(tmp_path.glob("handoff-messages-*.md")).read_text() == message
    assert session.pending == []


async def test_an_unknown_reset_does_not_checkpoint_on_every_pump_tick(tmp_path, monkeypatch):
    from flyte_agent_factory import _state

    now = _agent_time().time()
    clock = [now]
    monkeypatch.setattr(_agent_time(), "time", lambda: clock[0])
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(_usage, "_author_engine", "claude")
    monkeypatch.setattr(_usage, "_meters", None)
    # Owner rule 2: full meters no longer make a deferral, so the retry with no known reset comes from a
    # hard pin to an engine this slot cannot use. This used two 100% readings with no reset.
    monkeypatch.setattr(_usage, "_hard_pins", True)
    monkeypatch.setattr(_pins, "engine_input", "codex")
    monkeypatch.setattr(_usage, "_author_engines", lambda: ("claude",))
    monkeypatch.setattr(_usage, "_child_meter", lambda e: _meter.Meter(e, "test", now, 100))
    snapshots = []

    async def save(*args, **kwargs):
        snapshots.append(clock[0])
        return True

    monkeypatch.setattr(_state, "save_checkpoint", save)
    session = AuthorSession(Process(), tmp_path / "pending.json")
    callback = _author._author_handoff(
        session, _state.ProjectState(tmp_path, ""), _timeline._Timeline(), brief="brief"
    )
    assert not await callback(now + 1000, "interrupted")
    retry_at = session.retry_at
    clock[0] += 1
    assert not await callback(now + 1000, "interrupted")
    assert session.retry_at == retry_at
    assert len(snapshots) == 1
    assert session.pending == ["interrupted"]


async def test_a_stalled_checkpoint_on_a_deferred_limit_is_bounded_and_retried(tmp_path, monkeypatch):
    """A checkpoint stalled behind a deferred limit gives up after a bound and is retried."""
    from flyte_agent_factory import _state

    now = _agent_time().time()
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(_usage, "_author_engine", "claude")
    monkeypatch.setattr(_usage, "_meters", None)
    monkeypatch.setattr(_usage, "_hard_pins", True)
    monkeypatch.setattr(_pins, "engine_input", "codex")
    monkeypatch.setattr(_usage, "_author_engines", lambda: ("claude",))
    monkeypatch.setattr(_usage, "_child_meter", lambda e: _meter.Meter(e, "test", now, 100))
    monkeypatch.setattr(_author, "_HANDOFF_STEP_TIMEOUT_S", 0.2)
    calls = []

    async def stall(*args, **kwargs):
        calls.append(1)
        await asyncio.sleep(3600)

    monkeypatch.setattr(_state, "save_checkpoint", stall)
    session = AuthorSession(Process(), tmp_path / "pending.json")
    timeline = _timeline._Timeline()
    callback = _author._author_handoff(session, _state.ProjectState(tmp_path, ""), timeline, brief="b")
    async with asyncio.timeout(30):
        assert not await callback(now + 1000, "")
        # Not remembered as saved, so the next tick tries again.
        assert not await callback(now + 1000, "")
    assert len(calls) == 2
    assert any("checkpoint" in text and "TimeoutError" in text for _, _, text in timeline._rows)


# --------------------------------------------------------------------------- #
# QA findings on the first handoff chunk
# --------------------------------------------------------------------------- #


async def _restart_paused(tmp_path, monkeypatch, now):
    """One supervisor restart of a paused token-backed author, run through the real pump.

    Returns the session and the meter store after the pump has re-routed the saved refusal.
    """
    from tests.test_nudge import _Proc, _Queue

    class Restored(_Proc):
        returncode: int | None = None
        output_count = 0

        async def stop(self, *, grace: float = 20.0) -> None:
            self.returncode = 0

    session = _Session(Restored(), tmp_path / "author-pending.json")
    _author._restore_refusal(session, json.loads((tmp_path / "author-conversation.json").read_text()))
    from flyte_agent_factory import _state

    session.on_limit = _author._author_handoff(
        session, _state.ProjectState(tmp_path, ""), _timeline._Timeline(), brief="b"
    )
    stop = asyncio.Event()
    pump = asyncio.create_task(_supervise._pump(_Queue(), session, _timeline._Timeline(), stop))  # type: ignore[arg-type]  # fakes
    try:
        await wait_for(lambda: getattr(session, "retry_at", None) is not None)
        await asyncio.sleep(0.05)
    finally:
        stop.set()
        await asyncio.wait_for(pump, 30)
    return session


@pytest.fixture
def paused_token_author(tmp_path, monkeypatch):
    now = _agent_time().time()
    store = _meter.MeterStore(tmp_path / "meters")
    account = _meter.token_account("secret", "token")
    store.update(_meter.Meter(account, "test", now, limited_until=now + 100))
    store.update(_meter.Meter("codex", "test", now, limited_until=now + 200))
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(_session_config, "POLL_EVERY_S", 0.0)
    for name, value in (
        ("_meters", store),
        ("_author_engine", "claude"),
        ("_hard_pins", False),
    ):
        monkeypatch.setattr(_usage, name, value)
    monkeypatch.setattr(_usage, "_author_engines", lambda: ("claude", "codex"))
    monkeypatch.setattr(_supervise_support, "_in_flight_children", lambda: (0, ""))

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(_usage, "_read_meters", noop)
    monkeypatch.setattr(flyte.report, "replace", type("R", (), {"aio": staticmethod(noop)}))
    from flyte_agent_factory import _state

    monkeypatch.setattr(_state, "save_checkpoint", noop)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "token")
    monkeypatch.setenv("AGENT_FACTORY_CLAUDE_TOKEN_SECRET", "secret")
    (tmp_path / "author-conversation.json").write_text(
        json.dumps({"engine": "claude", "limited_until": now + 100, "account": account})
    )
    return SimpleNamespace(now=now, store=store, account=account)


async def test_restarting_a_paused_author_keeps_the_saved_resets(tmp_path, monkeypatch, paused_token_author):
    now, store = paused_token_author.now, paused_token_author.store
    account = paused_token_author.account
    for _ in range(2):  # each restart used to push the reset further out
        session = await _restart_paused(tmp_path, monkeypatch, now)
        assert session.retry_at == now + 100
        assert store.readings[account].limited_until == now + 100
        assert store.readings["codex"].limited_until == now + 200
        assert json.loads((tmp_path / "author-conversation.json").read_text())["limited_until"] == now + 100


async def test_a_restart_after_the_saved_reset_resumes_claude(tmp_path, monkeypatch, paused_token_author):
    from tests.test_nudge import _Proc

    now = paused_token_author.now
    monkeypatch.setattr(_agent_time(), "time", lambda: now + 101)
    session = AuthorSession(_Proc(), tmp_path / "author-pending.json")  # type: ignore[arg-type]  # restore only reads attrs
    _author._restore_refusal(session, json.loads((tmp_path / "author-conversation.json").read_text()))
    assert not session._paused and session.limit_hit_at is None


def _handoff_rig(tmp_path, monkeypatch, steps, *, source="claude"):
    """The handoff test's fixtures: fake CLIs, patched state paths, a callback built on a session."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    from flyte_agent_factory import _state
    from flyte_agent_factory._process import ClaudeProcess

    work = tmp_path / "workspace"
    work.mkdir()
    subprocess.run(["git", "init", str(work)], check=True, capture_output=True)
    config = tmp_path / "claude"
    config.mkdir()
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(_session_config, "WORKSPACE", work)
    monkeypatch.setattr(_session_config, "CONFIG_DIR", config)
    monkeypatch.setattr(_session_config, "CODEX_HOME_DIR", tmp_path / "codex")
    monkeypatch.setattr(_author, "_CODEX_THREAD_FILE", tmp_path / "codex-thread")
    for name, value in (
        ("_author_engine", source),
        ("_meters", None),
        ("_hard_pins", False),
    ):
        monkeypatch.setattr(_usage, name, value)
    monkeypatch.setattr(_inbox, "_directions", [])
    monkeypatch.setattr(_usage, "_author_engines", lambda: ("claude", "codex"))
    monkeypatch.setattr(_session_setup, "_log_session_line", lambda _: None)
    monkeypatch.setattr(
        _author, "_CODEX_COMMAND", (sys.executable, str(Path(__file__).with_name("fake_codex.py")))
    )
    monkeypatch.setenv(_session_config.CODEX_TOKEN_ENV_VAR, "{}")
    monkeypatch.setattr(agent, "assert_claude_available", lambda: None)
    script = tmp_path / "script.json"
    script.write_text(json.dumps(steps))
    log = tmp_path / "claude.log"

    async def start(candidates, **kwargs):
        current = ClaudeProcess(
            [sys.executable, str(Path(__file__).with_name("fake_claude.py"))],
            cwd=work,
            env={**os.environ, "FAKE_CLAUDE_SCRIPT": str(script), "FAKE_CLAUDE_LOG": str(log)},
            prompt_rules=[],
            inbox_messages=True,
        )
        await current.start()
        return current

    async def checkpoint(state, **kwargs):
        return True

    monkeypatch.setattr(_author, "_start_session", start)
    monkeypatch.setattr(_state, "save_checkpoint", checkpoint)

    def callback(session):
        return _author._author_handoff(
            session,
            _state.ProjectState(tmp_path, ""),
            _timeline._Timeline(),
            brief="Build the feature",
        )

    return SimpleNamespace(work=work, log=log, start=start, callback=callback)


async def test_handoff_carries_messages_held_behind_a_permission_menu_in_order(tmp_path, monkeypatch):
    menu = "printf 'Do you want to proceed?\\n❯ 1. Yes\\n  2. No\\n\\nEsc to cancel\\n'"
    rig = _handoff_rig(tmp_path, monkeypatch, [{"when": "open the menu", "run": [menu]}])
    old = await rig.start([])
    real_stop = old.stop

    async def quick_stop(**kwargs):  # an open menu swallows /exit; do not wait the full grace
        await real_stop(grace=1)

    old.stop = quick_stop
    session = AuthorSession(old, tmp_path / "author-pending.json")
    try:
        await session.submit("open the menu")
        await wait_for(lambda: old.menu is not None)
        await session.submit("FINDING-ONE")
        await session.submit("FINDING-TWO")
        assert [text for text, _ in old._held] == ["FINDING-ONE", "FINDING-TWO"]
        old._scan_for_limit("⎿ You've hit your session limit · resets 7pm (UTC)\n")
        with pytest.raises(_author.AuthorHandoffRestart):
            await rig.callback(session)(_agent_time().time() + 3600, "")
        current = _author._start_codex_author(_timeline._Timeline(), model="", effort="")
        session = AuthorSession(current, tmp_path / "author-pending.json")
        await _author._resume_author_pending(session, "codex")
        await current.wait()
        prompt = next((tmp_path / "codex" / "threads").iterdir()).read_text()
        assert prompt.count("FINDING-ONE") == 1 and prompt.count("FINDING-TWO") == 1
        assert prompt.index("FINDING-ONE") < prompt.index("FINDING-TWO")
        assert "/exit" not in prompt
        assert session.pending == []
    finally:
        await session.stop(grace=1)


def _alive(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"  # a zombie has already run its last instruction


async def test_restoring_a_planned_handoff_conversation_survives_two_restarts(project, monkeypatch, tmp_path):
    """A checkpoint taken between a handoff's journal write and the new engine's start.

    The journal names the Claude conversation the handoff planned. The restored Claude must be that
    conversation, or the next restart resumes an id nobody created and loses the handoff brief.
    """
    import sys
    import uuid
    from pathlib import Path

    from flyte_agent_factory._process import ClaudeProcess

    state = project.state_root
    owner = state / "author-conversation.json"
    owner.write_text(json.dumps({"engine": "claude", "fresh": True, "thread": "planned-handoff-id"}))
    (state / "author-pending.json").write_text(json.dumps(["HANDOFF-BRIEF"]))
    log = tmp_path / "claude.log"
    script = tmp_path / "script.json"
    script.write_text("[]")
    created: set[str] = set()
    starts: list[list[list[str]]] = []

    async def start(candidates, **kwargs):
        # What Claude Code does: `--session-id X` creates X, `--resume X` needs it to exist (else the
        # argv fails and the next candidate is tried), and a plain start makes up its own id.
        starts.append(candidates)
        for argv in candidates:
            if "--resume" in argv:
                wanted = argv[argv.index("--resume") + 1]
                if wanted not in created:
                    continue
                break
            if "--continue" in argv:
                continue
            created.add(argv[argv.index("--session-id") + 1] if "--session-id" in argv else str(uuid.uuid4()))
            break
        proc = ClaudeProcess(
            [sys.executable, str(Path(__file__).with_name("fake_claude.py"))],
            cwd=project.workspace,
            env={**os.environ, "FAKE_CLAUDE_SCRIPT": str(script), "FAKE_CLAUDE_LOG": str(log)},
            prompt_rules=[],
            inbox_messages=True,
        )
        await proc.start()
        return proc

    async def pump(bridge, proc, tl, stop, **kwargs):
        async with asyncio.timeout(30):
            while proc.pending:
                await asyncio.sleep(0.02)
        stop.set()

    monkeypatch.setattr(_author, "_start_session", start)
    monkeypatch.setattr(_supervise, "_pump", pump)
    monkeypatch.setattr(_author, "has_conversation", lambda _: True)
    monkeypatch.setattr(agent, "assert_claude_available", lambda: None)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    project.seen["resumed"] = True

    for restart in (1, 2):
        await agent.project.func("brief", browser=False)
        saved = json.loads(owner.read_text())
        assert saved["thread"] in created, (
            f"restart {restart}: the journal names a conversation Claude never made"
        )
        assert "fresh" not in saved
        # A labelled message reaches Claude as a pointer to an inbox file.
        inbox = sorted((project.workspace / ".claude-flyte" / "inbox").glob("message-*.md"))
        delivered = log.read_text() + "".join(path.read_text() for path in inbox)
        assert delivered.count("HANDOFF-BRIEF") == 1, f"restart {restart}"
    resumed = starts[-1][0]
    assert resumed[resumed.index("--resume") + 1] == "planned-handoff-id"
    assert len(created) == 1


@pytest.mark.parametrize("source", ["claude", "codex"])
async def test_ordinary_stop_preserves_detached_services(tmp_path, monkeypatch, source):

    pidfile = tmp_path / "service.pid"
    tool = f"setsid sh -c 'echo $$ > {pidfile}; exec sleep 30' >/dev/null 2>&1 &"
    rig = _handoff_rig(tmp_path, monkeypatch, [{"when": "launch", "run": [tool]}], source=source)
    monkeypatch.setattr(_usage, "_author_engines", lambda: (source,))
    if source == "claude":
        old = await rig.start([])
    else:
        old = _author._start_codex_author(_timeline._Timeline(), model="", effort="")
        old._env["FAKE_CODEX_BG"] = tool
    pid = None
    try:
        await old.submit("launch")
        await wait_for(lambda: pidfile.exists() and pidfile.read_text().strip())
        pid = int(pidfile.read_text())
        await old.stop(grace=0.2)
        assert _alive(pid), "an ordinary stop killed a detached service"
    finally:
        if pid is not None and _alive(pid):
            os.kill(pid, 9)
        await old.stop(grace=0.1)


async def test_refusal_with_inherited_stdout_hands_off_without_waiting_for_eof(tmp_path, monkeypatch):

    pidfile = tmp_path / "stdout-service.pid"
    rig = _handoff_rig(tmp_path, monkeypatch, [], source="codex")
    old = _author._start_codex_author(_timeline._Timeline(), model="", effort="")
    old._env.update(FAKE_CODEX_LIMIT="1", FAKE_CODEX_BG_STDOUT=str(pidfile))
    session = AuthorSession(old, tmp_path / "pending.json")
    try:
        await old.submit("launch")
        await wait_for(lambda: pidfile.exists() and pidfile.read_text().strip(), timeout=30)
        pid = int(pidfile.read_text())
        await wait_for(lambda: old.limit_hit_at is not None, timeout=30)
        assert _alive(pid)
        async with asyncio.timeout(30):
            with pytest.raises(_author.AuthorHandoffRestart):
                await rig.callback(session)(_agent_time().time() + 3600, "")
        assert session.current is old
        current = await rig.start([])
        session = AuthorSession(current, tmp_path / "pending.json")
        await _author._resume_author_pending(session, "claude")
        await wait_for(lambda: "AUTHOR HANDOFF" in _delivered_prompt(rig.work, rig.log))
        assert (
            "processes the previous author started (servers, watchers) were stopped; "
            "restart any the brief requires" in _delivered_prompt(rig.work, rig.log)
        )
    finally:
        if pid is not None and _alive(pid):
            os.kill(pid, 9)
        await old.stop(grace=0.1)
        await session.stop(grace=0.1)


async def test_handoff_disables_a_blocking_fsmonitor_hook(tmp_path, monkeypatch):
    import subprocess
    import sys

    rig = _handoff_rig(tmp_path, monkeypatch, [], source="codex")
    hook = tmp_path / "fsmonitor"
    hook.write_text(f"#!{sys.executable}\nimport signal\nsignal.pause()\n")
    hook.chmod(0o755)
    subprocess.run(["git", "config", "core.fsmonitor", str(hook)], cwd=rig.work, check=True)
    old = _author._start_codex_author(_timeline._Timeline(), model="", effort="")
    old._env["FAKE_CODEX_LIMIT"] = "1"
    await old.submit("launch")
    await old.wait()
    session = AuthorSession(old, tmp_path / "pending.json")
    try:
        async with asyncio.timeout(30):
            with pytest.raises(_author.AuthorHandoffRestart):
                await rig.callback(session)(_agent_time().time() + 3600, "")
    finally:
        await session.stop(grace=0.1)


@pytest.mark.parametrize("cancel", [False, True])
async def test_handoff_bounds_stuck_git_and_cancels(tmp_path, monkeypatch, cancel):
    import signal
    import sys

    rig = _handoff_rig(tmp_path, monkeypatch, [], source="codex")
    old = _author._start_codex_author(_timeline._Timeline(), model="", effort="")
    old._env["FAKE_CODEX_LIMIT"] = "1"
    await old.submit("launch")
    await old.wait()
    session = AuthorSession(old, tmp_path / "pending.json")
    real_spawn = asyncio.create_subprocess_exec
    spawned = []

    async def spawn(program, *args, **kwargs):
        if program == "git":
            proc = await real_spawn(sys.executable, "-c", "import signal; signal.pause()", **kwargs)
            spawned.append(proc)
            return proc
        return await real_spawn(program, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(_checkpoint, "_HANDOFF_GIT_TIMEOUT_S", 0.1, raising=False)
    task = asyncio.create_task(rig.callback(session)(_agent_time().time() + 3600, ""))
    try:
        if cancel:
            await wait_for(lambda: bool(spawned))
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            async with asyncio.timeout(30):
                with pytest.raises(_author.AuthorHandoffRestart):
                    await task
            current = await rig.start([])
            session = AuthorSession(current, tmp_path / "pending.json")
            await _author._resume_author_pending(session, "claude")
            await wait_for(lambda: "AUTHOR HANDOFF" in _delivered_prompt(rig.work, rig.log))
            assert "Git status:\nUnavailable" in _delivered_prompt(rig.work, rig.log)
            assert "Diff summary:\nUnavailable" in _delivered_prompt(rig.work, rig.log)
        assert spawned and all(proc.returncode == -signal.SIGKILL for proc in spawned)
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await session.stop(grace=0.1)


async def test_handoff_requires_a_fresh_pod_and_records_pending_work(tmp_path, monkeypatch):
    from flyte_agent_factory import _state

    rig = _handoff_rig(tmp_path, monkeypatch, [])
    old = await rig.start([])
    session = AuthorSession(old, tmp_path / "author-pending.json")
    session.pause()
    await session.submit("pending direction")
    snapshots = []

    async def checkpoint(state, **kwargs):
        snapshots.append(json.loads((tmp_path / "author-conversation.json").read_text()))
        return True

    monkeypatch.setattr(_state, "save_checkpoint", checkpoint)
    try:
        with pytest.raises(_author.AuthorHandoffRestart):
            await rig.callback(session)(_agent_time().time() + 3600, "interrupted")
        assert session.current is old
        assert old.returncode is not None
        assert snapshots[-1]["engine"] == "codex" and snapshots[-1]["fresh"]
        record = _session_config._author_handoff_records()[0]
        assert record["pending"] == ["interrupted", "pending direction"]
        assert record["to"] == "codex"
        assert session.pending[0].startswith("AUTHOR HANDOFF")
    finally:
        await session.stop(grace=1)


async def test_handoff_restart_is_fatal_to_the_supervisor():
    error = _author.AuthorHandoffRestart("new pod required")

    async def restart():
        raise error

    fatal: list[BaseException] = []
    stop = asyncio.Event()
    await _supervise_support._supervised(restart(), "pump", _timeline._Timeline(), stop, fatal=fatal)
    assert stop.is_set()
    assert fatal == [error]


@pytest.mark.parametrize("listing", [1, 2])
@pytest.mark.parametrize("failure", ["fsmonitor", "timeout"])
async def test_checkpoint_git_listings_are_bounded(tmp_path, monkeypatch, caplog, listing, failure):
    import shutil
    import subprocess
    import sys

    from flyte_agent_factory import _state

    work = tmp_path / "state" / "workspace"
    work.mkdir(parents=True)
    subprocess.run(["git", "init", str(work)], check=True, capture_output=True)
    (work / ".gitignore").write_text("ignored/\n")
    (work / "ignored").mkdir()
    (work / "ignored" / "data").write_text("ignored")
    (work / "patch.py").write_text("work")
    private = work.parent / "codex" / "auth.json"
    private.parent.mkdir()
    private.write_text("secret")
    hook = tmp_path / "fsmonitor"
    hook.write_text(f"#!{sys.executable}\nimport signal\nsignal.pause()\n")
    hook.chmod(0o755)
    subprocess.run(["git", "config", "core.fsmonitor", str(hook)], cwd=work, check=True)
    real_spawn = asyncio.create_subprocess_exec
    calls = []
    stuck = []

    async def spawn(program, *args, **kwargs):
        calls.append(args)
        assert args[:5] == ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "ls-files")
        if failure == "timeout" and len(calls) == listing:
            proc = await real_spawn(sys.executable, "-c", "import signal; signal.pause()", **kwargs)
            stuck.append(proc)
            return proc
        return await real_spawn(program, *args, **kwargs)

    async def save(path):
        shutil.copytree(path, tmp_path / "snapshot")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    # Only the timeout case needs a listing to time out; with fsmonitor disabled both listings must finish,
    # and a loaded machine can take longer than a tenth of a second to run `git ls-files`.
    monkeypatch.setattr(_state, "_CHECKPOINT_GIT_TIMEOUT_S", 0.1 if failure == "timeout" else 30)
    monkeypatch.setattr(_state, "_checkpoint", lambda: SimpleNamespace(save=save))
    async with asyncio.timeout(60):
        assert await _state.save_checkpoint(_state.ProjectState(work.parent, ""), exclude_private=True)
    assert (tmp_path / "snapshot" / "workspace" / "patch.py").read_text() == "work"
    assert (tmp_path / "snapshot" / "codex" / "auth.json").read_text() == "secret"
    if failure == "timeout":
        assert stuck and all(proc.returncode is not None for proc in stuck)
        assert "timed out" in caplog.text
        assert (tmp_path / "snapshot" / "workspace" / "ignored" / "data").is_file()
    else:
        assert len(calls) == 2
        assert not (tmp_path / "snapshot" / "workspace" / "ignored").exists()


async def test_handoff_retry_allowance_waits_without_using_another_retry(tmp_path, monkeypatch):
    from flyte_agent_factory import _state

    rig = _handoff_rig(tmp_path, monkeypatch, [])
    records = tmp_path / "author-handoffs"
    records.mkdir()
    for number in range(_author._AUTHOR_HANDOFF_MAX_RESTARTS):
        (records / f"{number}.json").write_text(json.dumps({"run": "local"}))
    assert 0 < _author._AUTHOR_HANDOFF_MAX_RESTARTS < agent.project.retries.count  # type: ignore[union-attr]  # stored as a RetryStrategy
    old = await rig.start([])
    session = AuthorSession(old, tmp_path / "author-pending.json")
    snapshots = []

    async def checkpoint(*args, **kwargs):
        snapshots.append(True)

    monkeypatch.setattr(_state, "save_checkpoint", checkpoint)
    reset = _agent_time().time() + 3600
    try:
        callback = rig.callback(session)
        assert not await callback(reset, "pending")
        assert not await callback(reset, "pending")
        assert session.retry_at == reset
        assert old.returncode is None
        assert len(list(records.glob("*.json"))) == _author._AUTHOR_HANDOFF_MAX_RESTARTS
        assert session.pending == ["pending"] and len(snapshots) == 1
        assert json.loads((tmp_path / "author-conversation.json").read_text())["engine"] == "claude"
    finally:
        await old.stop(grace=1)


@pytest.mark.parametrize("failing", ["checkpoint_raises", "checkpoint_stall", "git_raises"])
async def test_handoff_failure_after_stop_ends_in_restart_not_a_hang(tmp_path, monkeypatch, failing):
    from flyte_agent_factory import _state

    rig = _handoff_rig(tmp_path, monkeypatch, [])
    old = await rig.start([])
    session = AuthorSession(old, tmp_path / "author-pending.json")
    monkeypatch.setattr(_author, "_HANDOFF_STEP_TIMEOUT_S", 0.2, raising=False)

    async def stall(*args, **kwargs):
        await asyncio.sleep(3600)

    async def boom(*args, **kwargs):
        raise OSError("disk full")

    if failing == "checkpoint_raises":
        monkeypatch.setattr(_state, "save_checkpoint", boom)
    elif failing == "checkpoint_stall":
        monkeypatch.setattr(_state, "save_checkpoint", stall)
    else:
        monkeypatch.setattr(_checkpoint, "_handoff_git_out", boom)
    fatal: list[BaseException] = []
    stop = asyncio.Event()
    try:
        async with asyncio.timeout(30):
            await _supervise_support._supervised(
                rig.callback(session)(_agent_time().time() + 3600, ""),
                "pump",
                _timeline._Timeline(),
                stop,
                fatal=fatal,
            )
        assert stop.is_set()
        assert len(fatal) == 1 and isinstance(fatal[0], _author.AuthorHandoffRestart)
        # The record is written before anything that can fail, so the next pod knows the intent.
        assert [r["to"] for r in _session_config._author_handoff_records()] == ["codex"]
    finally:
        await session.stop(grace=1)


@pytest.mark.parametrize(
    ("attempts_before_last", "hands_off"),
    [(0, False), (1, False), (2, True)],
    ids=["last", "last-1", "last-2"],
)
async def test_handoff_needs_a_retry_left_beyond_its_own_and_a_margin(
    tmp_path, monkeypatch, attempts_before_last, hands_off
):
    rig = _handoff_rig(tmp_path, monkeypatch, [])
    attempt = agent.project.retries.count - attempts_before_last  # type: ignore[union-attr]  # a RetryStrategy
    monkeypatch.setattr(flyte, "ctx", lambda: SimpleNamespace(attempt_number=attempt, action=None))
    old = await rig.start([])
    session = AuthorSession(old, tmp_path / "author-pending.json")
    reset = _agent_time().time() + 3600
    try:
        callback = rig.callback(session)
        if hands_off:
            with pytest.raises(_author.AuthorHandoffRestart):
                await callback(reset, "pending")
        else:
            assert not await callback(reset, "pending")
            assert old.returncode is None
            assert not session._switching
            assert session.retry_at == reset
            assert not list((tmp_path / "author-handoffs").glob("*.json"))
    finally:
        await session.stop(grace=1)


async def test_a_codex_handoff_conversation_survives_two_project_restarts(project, monkeypatch):
    state = project.state_root
    (state / "author-conversation.json").write_text(json.dumps({"engine": "codex", "fresh": True}))
    (state / "author-pending.json").write_text(json.dumps(["HANDOFF-BRIEF", "queued direction"]))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "token")
    project.seen["resumed"] = True
    project.pump_that("continue")
    for restart in (1, 2):
        await agent.project.func("brief", browser=False)
        thread, prompts = project.thread()
        assert prompts.count("HANDOFF-BRIEF") == 1, f"restart {restart}"
        assert prompts.count("queued direction") == 1
        owner = json.loads((state / "author-conversation.json").read_text())
        assert owner["thread"] == thread and "fresh" not in owner
        assert json.loads((state / "author-pending.json").read_text()) == []


async def test_project_propagates_a_handoff_restart(project, monkeypatch):
    async def pump(bridge, session, timeline, stop, **kwargs):
        await wait_for(lambda: session.thread_id is not None)
        await session.wait()
        await session.on_limit(_agent_time().time() + 3600, "")

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "token")
    monkeypatch.setattr(agent, "assert_claude_available", lambda: None)
    monkeypatch.setenv("AGENT_FACTORY_AUTHOR_ENGINE", "codex")
    # Since #125 a pin is hard unless fallback is allowed, and a hard pin waits instead of handing off.
    monkeypatch.setenv("AGENT_FACTORY_ENGINE_FALLBACK", "allow")
    monkeypatch.setattr(_supervise, "_pump", pump)
    with pytest.raises(_author.AuthorHandoffRestart):
        await agent.project.func("brief", browser=False)
    owner = json.loads((project.state_root / "author-conversation.json").read_text())
    assert owner["engine"] == "claude" and owner["fresh"]
    assert _session_config._author_handoff_records()[0]["to"] == "claude"
    assert project.seen["started"] == []


async def test_restart_isolates_a_detached_tool_with_an_empty_environment(tmp_path):
    import shutil
    import signal
    import sys

    worker = tmp_path / "pod.py"
    worker.write_text(
        r"""import asyncio, json, os, shutil, sys
from pathlib import Path
from types import SimpleNamespace
from flyte_agent_factory import _author, _pins, _session_config, _session_setup, _state, _timeline
from flyte_agent_factory import _usage, agent
from flyte_agent_factory._author_session import AuthorSession
from flyte_agent_factory._process import ClaudeProcess

# The worker exits without destructing transports, as a pod can disappear with detached tools.
escaped_tools = []

async def main():
    mode, directory, snapshot, fake = sys.argv[1:]
    root = Path(directory)
    work = root / 'workspace'
    work.mkdir(parents=True, exist_ok=True)
    _session_config.STATE_ROOT = root
    _session_config.WORKSPACE = work
    _session_config.CONFIG_DIR = root / 'claude'
    _session_config.CONFIG_DIR.mkdir(exist_ok=True)
    _session_config.CODEX_HOME_DIR = root / 'codex'
    _author._CODEX_THREAD_FILE = root / 'codex-thread'
    _usage._author_engine = 'claude'
    _usage._meters = None
    _usage._hard_pins = False
    _usage._author_engines = lambda: ('claude', 'codex')
    _session_setup._log_session_line = lambda _: None
    _author._CODEX_COMMAND = (sys.executable, str(Path(fake) / 'fake_codex.py'))
    os.environ[_session_config.CODEX_TOKEN_ENV_VAR] = '{}'
    async def noop(*args, **kwargs): pass
    async def save(path): shutil.copytree(path, snapshot)
    _usage._read_meters = noop
    _state._checkpoint = lambda: SimpleNamespace(save=save)
    (root / (mode + '.pid')).write_text(str(os.getpid()))
    if mode == 'old':
        script = root / 'script.json'
        script.write_text('[]')
        old = ClaudeProcess(
            [sys.executable, str(Path(fake) / 'fake_claude.py')], cwd=work,
            env={**os.environ, 'FAKE_CLAUDE_SCRIPT': str(script), 'FAKE_CLAUDE_LOG': str(root / 'log')},
            prompt_rules=[], inbox_messages=True)
        await old.start()
        tool = await asyncio.create_subprocess_exec(
            'env', '-i', 'setsid', sys.executable, '-c',
            "import os, time; from pathlib import Path; "
            f"Path({str(root / 'tool.pid')!r}).write_text(str(os.getpid())); "
            f"exec(\"while not Path({str(root / 'release-tool')!r}).exists(): time.sleep(0.01)\"); "
            # Written aside and renamed in: the test reads `result` as soon as it exists.
            f"Path({str(work / 'result.tmp')!r}).write_text('OLD'); "
            f"os.replace({str(work / 'result.tmp')!r}, {str(work / 'result')!r})",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        escaped_tools.append(tool)
        while not (root / 'tool.pid').exists(): await asyncio.sleep(0.01)
        session = AuthorSession(old, root / 'author-pending.json')
        session.pause()
        await session.submit('queued direction')
        try:
            await _author._author_handoff(session, _state.ProjectState(root, ''), _timeline._Timeline(),
                brief='work')(_author.time.time() + 3600, '')
        except _author.AuthorHandoffRestart:
            return 75
        raise AssertionError('handoff did not require a restart')
    chosen = await _author._choose_author_engine(
        _pins.Pin('', False, ''), ('claude', 'codex'), True, _timeline._Timeline()
    )
    assert chosen == 'codex'
    current = _author._start_codex_author(_timeline._Timeline(), model='', effort='')
    session = AuthorSession(current, root / 'author-pending.json')
    await _author._resume_author_pending(session, 'codex')
    await session.wait()
    (work / 'result').write_text('NEW')
    await session.stop(grace=1)
    return 0

os._exit(asyncio.run(main()))
"""
    )
    old_root, new_root = tmp_path / "old-pod", tmp_path / "new-pod"
    snapshot = tmp_path / "checkpoint"
    fake = Path(__file__).parent.resolve()
    tool_pid = None
    old = new = None
    try:
        old = await asyncio.create_subprocess_exec(
            sys.executable,
            str(worker),
            "old",
            str(old_root),
            str(snapshot),
            str(fake),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        async with asyncio.timeout(30):
            output, error = await old.communicate()
        assert old.returncode == 75, (output.decode(), error.decode())
        tool_pid = int((old_root / "tool.pid").read_text())
        assert _alive(tool_pid), "ordinary stop should leave the detached tool for pod teardown"
        # Separate roots model the fresh pod's filesystem, restored only from the checkpoint.
        # Let the escaped tool write in the old pod to prove it cannot change the new workspace.
        shutil.copytree(snapshot, new_root)
        new = await asyncio.create_subprocess_exec(
            sys.executable,
            str(worker),
            "new",
            str(new_root),
            str(snapshot),
            str(fake),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        async with asyncio.timeout(30):
            output, error = await new.communicate()
        assert new.returncode == 0, (output.decode(), error.decode())
        assert (old_root / "old.pid").read_text() != (new_root / "new.pid").read_text()
        (old_root / "release-tool").touch()
        old_result = old_root / "workspace" / "result"
        await wait_for(lambda: old_result.exists() and old_result.read_text() == "OLD")
        assert old_result.read_text() == "OLD"
        assert (new_root / "workspace" / "result").read_text() == "NEW"
        thread = next((new_root / "codex" / "threads").iterdir()).read_text()
        assert thread.count("AUTHOR HANDOFF") == 1 and thread.count("queued direction") == 1
    finally:
        if tool_pid is not None and _alive(tool_pid):
            os.kill(tool_pid, signal.SIGKILL)
        for proc in (old, new):
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()


def test_a_bridge_that_cannot_reach_the_owner_does_not_stop_the_pause_row():
    class Unreachable:
        def notify_owner(self, text: str, timeout_s: float) -> str:
            raise OSError("socket closed")

    timeline = _timeline._Timeline()
    timeline.bridge = Unreachable()  # type: ignore[assignment]
    _author._ask_human(timeline, "set ENGINE_FALLBACK")
    texts = [text for _, _, text in timeline._rows]
    assert any("a human needs to look: set ENGINE_FALLBACK" in t for t in texts)
    assert any("could not ask the owner: OSError: socket closed" in t for t in texts)
