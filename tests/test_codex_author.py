"""A Codex author session under the project supervisor, offline.

`CodexAuthor` runs against tests/fake_codex.py; the pump, bridge, DONE gate and timeline are the
production ones. `project` itself is driven the way tests/test_supervision.py drives it: its cloud
edges replaced, the engine choice and session start real.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import flyte.errors
import pytest
from flyte import remote

from flyte_agent_factory import (
    _author as _author_module,
)
from flyte_agent_factory import (
    _checkpoint,
    _inbox,
    _meter,
    _pins,
    _profile,
    _review,
    _session_config,
    _session_setup,
    _state,
    _supervise,
    _timeline,
    _usage,
    agent,
)
from flyte_agent_factory._bridge import Bridge
from flyte_agent_factory._codex_process import CodexAuthor
from tests._wait import wait_for

FAKE = Path(__file__).parent / "fake_codex.py"
WORK = Path(__file__).parent / "profiles" / "work.toml"


def _thread(home: Path, proc) -> list[str]:
    path = home / "threads" / str(proc.thread_id)
    return path.read_text().splitlines() if path.is_file() else []


def _author(tmp_path, **env) -> CodexAuthor:
    return CodexAuthor(
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"], "CODEX_HOME": str(tmp_path / "home"), **env},
        command=(sys.executable, str(FAKE)),
        thread_file=tmp_path / "state" / "codex-thread",
    )


# --------------------------------------------------------------------------- #
# The session object
# --------------------------------------------------------------------------- #


async def test_a_message_sent_during_a_turn_is_queued_and_goes_out_when_it_ends(tmp_path):
    """A message sent during a turn is queued and goes out when it ends."""
    proc = _author(tmp_path, FAKE_CODEX_SLEEP="0.5")
    await proc.submit("first")
    await proc.submit("second")
    await proc.submit("third")
    assert proc.running
    await wait_for(lambda: "third" in "\n".join(_thread(tmp_path / "home", proc)) and not proc.running)
    lines = _thread(tmp_path / "home", proc)
    assert lines[0] == "first"
    # Both waiting messages arrive as one later turn, on the same thread.
    assert lines.count("Messages that arrived while you were working:") == 1
    assert lines.index("second") < lines.index("third")
    assert proc.returncode is None


async def test_the_thread_is_saved_and_a_new_object_resumes_it(tmp_path):
    """The thread is saved and a new object resumes it."""
    before = _author(tmp_path)
    await before.submit("one")
    await before.wait()
    assert (tmp_path / "state" / "codex-thread").read_text() == before.thread_id
    # A restarted pod: nothing in memory, the thread file and $CODEX_HOME restored.
    after = _author(tmp_path)
    assert after.thread_id == before.thread_id
    await after.submit("two")
    await after.wait()
    assert after.last_message == f"turn 2 on {before.thread_id}: two"


async def test_a_refused_turn_keeps_the_queue_until_the_next_turn_ends(tmp_path):
    """A refused turn keeps the queue until the next turn ends."""
    proc = _author(tmp_path, FAKE_CODEX_LIMIT="1", FAKE_CODEX_SLEEP="0.2")
    await proc.submit("first")
    await proc.submit("waiting")
    await proc.wait()
    await asyncio.sleep(0.3)
    assert proc.limit_hit_at is not None
    assert not proc.running, "a refused turn must not be followed by a turn that is refused again"


# --------------------------------------------------------------------------- #
# The pump with a Codex author
# --------------------------------------------------------------------------- #


@pytest.fixture
def pumped(tmp_path, monkeypatch, bridge_root):
    """Build a Codex author under the real pump, with a git workspace like tests/test_e2e_session.py's."""

    def git(cwd: Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    work = tmp_path / "work"
    git(tmp_path, "clone", str(bare), str(work))
    git(work, "config", "user.email", "t@example.com")
    git(work, "config", "user.name", "t")
    (work / "app.py").write_text("print('v0')\n")
    git(work, "add", "app.py")
    git(work, "commit", "-m", "init")
    git(work, "push", "-u", "origin", "HEAD:main")

    monkeypatch.setattr(_session_config, "POLL_EVERY_S", 0.05)
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(_usage, "_author_engine", "codex")
    monkeypatch.setattr(_session_config, "_project_name", "rehearsal")
    monkeypatch.setattr(_usage, "_meters", _meter.MeterStore(tmp_path / "meters"))
    bin_dir = tmp_path / "bin"
    _session_setup._install_bridge_commands(bin_dir)

    class Run:
        workspace = work
        timeline = _timeline._Timeline()
        stop = asyncio.Event()
        bridge = Bridge(bridge_root)
        home = tmp_path / "home"
        proc: Any = None  # a CodexAuthor once started; the pump types its process as a ClaudeProcess

        def note(self, text):
            subprocess.run(
                ["flyte-note", text],
                cwd=work,
                check=True,
                env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
            )

        def thread(self):
            return _thread(self.home, self.proc)

        async def start(self, **env):
            self.proc = CodexAuthor(
                cwd=work,
                env={"PATH": os.environ["PATH"], "CODEX_HOME": str(self.home), **env},
                command=(sys.executable, str(FAKE)),
                thread_file=tmp_path / "state" / "codex-thread",
            )
            self.pump = asyncio.create_task(
                _supervise._pump(
                    self.bridge,
                    self.proc,
                    self.timeline,
                    self.stop,
                    workspace=work,
                    brief="Build the thing.",
                    pinned_acceptance=tmp_path / "pinned",
                )
            )

        async def finish(self):
            self.stop.set()
            await asyncio.wait_for(self.pump, 30)
            await self.proc.stop(grace=5)

    return Run()


async def test_the_brief_and_a_later_message_arrive_as_turns(pumped):
    """The brief and a later message arrive as turns."""
    await pumped.start()
    await pumped.proc.submit("Read the brief and get started.", label="Project kickoff")
    await wait_for(lambda: len(pumped.thread()) == 1 and not pumped.proc.running)
    # What the inbox loop sends for a direction from Union.
    await pumped.proc.submit(
        "New direction arrived from Flyte. Read .claude-flyte/inbox/001.md and act on it."
    )
    await wait_for(lambda: len(pumped.thread()) == 2 and not pumped.proc.running)
    await pumped.finish()
    first, second = pumped.thread()
    assert first == "Read the brief and get started."
    assert "New direction arrived from Flyte" in second
    assert pumped.proc.returncode is None


async def test_the_done_gate_and_a_review_round_run_unchanged(pumped, monkeypatch, capsys):
    """The done gate and a review round run unchanged."""
    rounds: list[int] = []

    async def reviewers(workspace, brief, acceptance, round_, timeline):
        rounds.append(round_)
        return True, ""

    monkeypatch.setattr(_review, "_evaluate", reviewers)
    await pumped.start()
    await pumped.proc.submit("Read the brief and get started.")
    await wait_for(lambda: len(pumped.thread()) == 1 and not pumped.proc.running)

    # Done too early: the preflight bounces it without spending a round, and says so in the next turn.
    pumped.note("DONE: first attempt")
    await wait_for(lambda: any("no review round was used" in line for line in pumped.thread()))
    assert rounds == []

    work = pumped.workspace
    (work / "rehearsal").mkdir()
    (work / "rehearsal" / "ACCEPTANCE.md").write_text("- [ ] app prints v1\n")
    (work / "rehearsal" / "checks.sh").write_text("python3 app.py\n")
    (work / "app.py").write_text("print('v1')\n")
    for args in (
        ["add", "-A", "--", "rehearsal/ACCEPTANCE.md", "rehearsal/checks.sh", "app.py"],
        ["commit", "-qm", "work"],
        ["push", "-q"],
    ):
        subprocess.run(["git", *args], cwd=work, check=True, capture_output=True)
    pumped.note("DONE: criteria written")
    await wait_for(lambda: any("passed your work" in line for line in pumped.thread()), timeout=60)
    await pumped.finish()

    assert rounds == [1]
    assert "passed review in 1 round(s)" in capsys.readouterr().out


async def test_a_codex_refusal_moves_the_timeline_to_limited_and_records_the_codex_meter(pumped):
    """A codex refusal moves the timeline to limited and records the codex meter."""
    await pumped.start(FAKE_CODEX_LIMIT="1")
    await pumped.proc.submit("Read the brief and get started.")
    await wait_for(lambda: pumped.timeline._phase["kind"] == "limited")
    await pumped.finish()

    rows = [text for _, _, text in pumped.timeline._rows]
    assert any(text.startswith("Codex usage limit reached") for text in rows)
    assert _usage._meters is not None
    meter = _usage._meters.latest["codex"]
    # The reset the refusal's own rate limits reported (the fake's 5h window resets in 30 minutes).
    assert meter.limited_until and meter.limited_until > time.time()
    assert meter.five_hour_pct == 100.0


# --------------------------------------------------------------------------- #
# Choosing the engine
# --------------------------------------------------------------------------- #


def _store(tmp_path: Path, claude: float | None = None, codex: float | None = None) -> Any:
    store = _meter.MeterStore(tmp_path / "meters")
    now = time.time()
    if claude is not None:
        store.update(_meter.Meter(_session_config._claude_account(), "oauth-usage", now, claude, now + 3600))
    if codex is not None:
        store.update(_meter.Meter("codex", "app-server", now, codex, now + 3600))
    return store


@pytest.fixture
def choose(tmp_path, monkeypatch):
    """Return a runner of the author-engine choice, with the routing state reset and meters stubbed."""
    monkeypatch.setattr(_session_config, "_routes", [])
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "codex_token")
    monkeypatch.setattr(_session_config, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(_author_module, "_CODEX_THREAD_FILE", tmp_path / "codex-thread")
    monkeypatch.setattr(_usage, "_meters", None)

    async def noop(_timeline):
        pass

    monkeypatch.setattr(_usage, "_read_meters", noop)

    monkeypatch.setattr(_pins, "engine_input", "")  # `_engine_input_pin` sets it, as `project` does

    async def run(pin="", available=("claude", "codex"), resumed=False):
        timeline = _timeline._Timeline()
        engine = await _author_module._choose_author_engine(
            _author_module._engine_input_pin(pin), available, resumed, timeline
        )
        return engine, [text for _, icon, text in timeline._rows if icon == "🔀"]

    return run


async def test_a_pin_input_selects_the_engine(choose):
    """A pin input selects the engine."""
    assert (await choose(pin="codex"))[0] == "codex"
    assert (await choose(pin="claude"))[0] == "claude"
    engine, rows = await choose(pin="codex")
    assert rows and "pinned to codex" in rows[0], "the choice is recorded like any route"


async def test_a_policy_pin_selects_the_engine(choose, monkeypatch):
    """A policy pin selects the engine."""
    monkeypatch.setenv("AGENT_FACTORY_AUTHOR_ENGINE", "codex")
    assert (await choose())[0] == "codex"


async def test_an_unknown_engine_pin_is_refused(choose):
    """An unknown engine pin is refused."""
    with pytest.raises(ValueError, match="engine must be"):
        await choose(pin="gemini")


async def test_a_codex_only_slot_never_routes_the_author_to_claude(choose, tmp_path, monkeypatch):
    # Even with a Claude meter that looks better than Codex's. A Claude pin used to be ignored and ran
    # on Codex; it is now refused, since the pin is the owner's explicit choice.
    """A codex only slot never routes the author to claude."""
    monkeypatch.setattr(_usage, "_meters", _store(tmp_path, claude=5, codex=90))
    for pin in ("", "codex"):
        engine, rows = await choose(pin=pin, available=("codex",))
        assert engine == "codex"
        assert rows
    with pytest.raises(flyte.errors.NonRecoverableError, match="pinned to claude"):
        await choose(pin="claude", available=("codex",))


async def test_a_claude_only_slot_never_routes_the_author_to_codex(choose, monkeypatch):
    """A claude only slot never routes the author to codex."""
    monkeypatch.setenv("AGENT_FACTORY_AUTHOR_ENGINE", "codex")
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "")
    # The pin names Codex, which the slot has no login for: refused rather than run on Claude, from the
    # policy as from the input. (It used to be ignored, with a "pin codex ignored" row.)
    with pytest.raises(flyte.errors.NonRecoverableError, match="no codex login mounted"):
        await choose(available=("claude",))
    with pytest.raises(flyte.errors.NonRecoverableError, match="no codex login mounted"):
        await choose(pin="codex", available=("claude",))
    monkeypatch.delenv("AGENT_FACTORY_AUTHOR_ENGINE")
    assert (await choose(available=("claude",)))[0] == "claude"


async def test_a_slot_without_a_codex_login_stays_on_claude_when_its_claude_meter_is_full(
    choose, monkeypatch, tmp_path
):
    """With Codex logins per slot, a pod mounted none never falls back onto another slot's Codex chain."""
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    monkeypatch.setattr(_usage, "_meters", _store(tmp_path, claude=100, codex=1))
    assert _usage._author_engines() == ("claude",)
    assert (await choose(available=_usage._author_engines()))[0] == "claude"


async def test_an_unpinned_claude_only_slot_records_nothing(choose):
    """An unpinned claude only slot records nothing."""
    assert await choose(available=("claude",)) == ("claude", [])


async def test_the_author_moves_off_its_preferred_engine_only_when_that_is_exhausted(
    choose, tmp_path, monkeypatch
):
    # Owner rule 4: headroom does not pick the author's engine, policy does. This was
    # `test_the_router_picks_by_the_meters_when_both_engines_are_open`, which sent claude=97 to codex.
    """The author moves off its preferred engine only when that is exhausted."""
    monkeypatch.setattr(_usage, "_meters", _store(tmp_path, claude=97, codex=12))
    engine, rows = await choose()
    assert engine == "claude" and "author -> claude" in rows[0]
    # Owner rule 2: exhausted is a recorded refusal; a full Claude meter alone keeps the author on Claude.
    store = _store(tmp_path, claude=100, codex=60)
    monkeypatch.setattr(_usage, "_meters", store)
    assert (await choose())[0] == "claude"
    store.latest["claude"].limited_until = time.time() + 3600
    engine, rows = await choose()
    assert engine == "codex" and "author -> codex" in rows[-1]


async def test_an_unreadable_claude_meter_still_counts_a_limited_codex(choose, tmp_path, monkeypatch):
    # The Claude meter is missing, which is not a limit; Codex's refusal still is one. Owner rule 2: this
    # was `..._counts_a_full_codex`, where a full window alone was the limit.
    """An unreadable claude meter still counts a limited codex."""
    store = _store(tmp_path, codex=100)
    store.latest["codex"].limited_until = time.time() + 3600
    monkeypatch.setattr(_usage, "_meters", store)
    engine, rows = await choose()
    assert engine == "claude" and "codex exhausted" in rows[0]


async def test_an_unreadable_claude_meter_keeps_the_author_on_claude(choose, tmp_path, monkeypatch):
    # A setup-token has no usage endpoint: with only Codex readable it must not win by default.
    """An unreadable claude meter keeps the author on claude."""
    monkeypatch.setattr(_usage, "_meters", _store(tmp_path, codex=5))
    assert (await choose())[0] == "claude"


async def test_a_resumed_project_stays_on_the_engine_that_holds_its_conversation(
    choose, tmp_path, monkeypatch
):
    """A resumed project stays on the engine that holds its conversation."""
    (tmp_path / "codex-thread").write_text("thread-1")
    assert (await choose(resumed=True))[0] == "codex"
    # A pin still wins: the other engine starts afresh rather than being sent a foreign thread.
    assert (await choose(resumed=True, pin="claude"))[0] == "claude"
    (tmp_path / "codex-thread").unlink()
    monkeypatch.setattr(_author_module, "has_conversation", lambda _dir: True)
    monkeypatch.setattr(_usage, "_meters", _store(tmp_path, claude=99, codex=1))
    assert (await choose(resumed=True))[0] == "claude"


def test_a_slot_is_offered_only_the_engines_it_has_a_credential_for(monkeypatch, tmp_path):
    """A slot is offered only the engines it has a credential for."""
    monkeypatch.setattr(_session_config, "CONFIG_DIR", tmp_path / "config")
    for var in ("CLAUDE_CODE_OAUTH_TOKEN", "AGENT_FACTORY_CLAUDE_TOKEN_SECRET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "codex_token")
    assert _usage._author_engines() == ("codex",)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    assert _usage._author_engines() == ("claude", "codex")
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "")
    assert _usage._author_engines() == ("claude",)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")
    assert _usage._author_engines() == ()


# --------------------------------------------------------------------------- #
# `project` on Codex
# --------------------------------------------------------------------------- #


@pytest.fixture
def project(tmp_path, monkeypatch):
    """`project.func` with its cloud edges replaced and the session start real."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()
    timeline = _timeline._Timeline()
    seen: dict[str, Any] = {"started": [], "resumed": False}

    async def noop(*args, **kwargs):
        pass

    async def forever(*args, **kwargs):
        await asyncio.Event().wait()

    async def restore(**kwargs):
        return SimpleNamespace(resumed=seen["resumed"], resumed_from="checkpoint" if seen["resumed"] else "")

    async def publish(*args):
        return "published-workspace"

    async def claude_session(*args, **kwargs):
        seen["started"].append("claude")
        raise AssertionError("a Claude session was started")

    # `project` assigns these; patching them first makes monkeypatch put them back.
    monkeypatch.setattr(_usage, "_author_engine", "claude")
    monkeypatch.setattr(_usage, "_meters", None)
    monkeypatch.setattr(_session_config, "WORKSPACE", workspace)
    monkeypatch.setattr(_session_config, "STATE_ROOT", state_root)
    monkeypatch.setattr(_session_config, "CODEX_HOME_DIR", state_root / "codex")
    monkeypatch.setattr(_author_module, "_CODEX_THREAD_FILE", state_root / "codex-thread")
    monkeypatch.setattr(_session_config, "CONFIG_DIR", state_root / "claude")
    monkeypatch.setattr(_author_module, "_CODEX_COMMAND", (sys.executable, str(FAKE)))
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "codex_token")
    monkeypatch.setattr(_timeline, "_Timeline", lambda: timeline)
    monkeypatch.setattr(timeline, "flush", noop)
    monkeypatch.setattr(flyte, "ctx", lambda: None)
    monkeypatch.setattr(_state, "restore", restore)
    monkeypatch.setattr(_state, "save_checkpoint", noop)
    monkeypatch.setattr(_state, "publish", publish)
    monkeypatch.setattr(_usage, "_read_meters", noop)
    for name in ("mark_trusted", "write_settings"):
        monkeypatch.setattr(agent, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(_session_setup, "_install_bridge_commands", lambda *a, **k: None)
    monkeypatch.setattr(_session_setup, "_prepare_workspace", noop)
    monkeypatch.setattr(agent, "write_context", lambda *args: workspace / "context")
    monkeypatch.setattr(_author_module, "_start_session", claude_session)
    monkeypatch.setattr(_usage, "_meter_loop", forever)
    for name in ("_inbox_loop", "_watch_app_messages"):
        monkeypatch.setattr(_inbox, name, forever)
    monkeypatch.setattr(_checkpoint, "_checkpoint_loop", forever)
    monkeypatch.setattr(Bridge, "run", forever)
    for var in ("CLAUDE_CODE_OAUTH_TOKEN", "AGENT_FACTORY_CLAUDE_TOKEN_SECRET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(_session_config.CODEX_TOKEN_ENV_VAR, "codex-access-token")
    # Claude's own preflight must not be reached on a Codex author.

    def refuse(*args, **kwargs):
        raise AssertionError("assert_claude_available ran for a Codex author")

    monkeypatch.setattr(agent, "assert_claude_available", refuse)

    def pump_that(*messages):
        """Return a pump that waits for the first turn, sends `messages` as later turns, then ends the run."""

        async def pump(bridge, proc, tl, stop, *, kickoff="", **kwargs):
            seen["kickoff"] = kickoff
            while kickoff and proc.thread_id is None:  # the first turn has started
                await asyncio.sleep(0.01)
            await proc.wait()
            for message in messages:
                await proc.submit(message)
                await proc.wait()
            seen["thread_id"] = proc.thread_id
            stop.set()

        monkeypatch.setattr(_supervise, "_pump", pump)

    def thread():
        files = list((state_root / "codex" / "threads").glob("*"))
        assert len(files) == 1, files
        return files[0].name, files[0].read_text().splitlines()

    return SimpleNamespace(
        timeline=timeline,
        seen=seen,
        pump_that=pump_that,
        thread=thread,
        state_root=state_root,
        workspace=workspace,
    )


async def test_a_codex_only_profile_starts_the_author_on_codex_with_the_brief_as_its_first_turn(project):
    """A codex only profile starts the author on codex with the brief as its first turn."""
    project.pump_that()
    result, output = await agent.project.func("brief", browser=False)

    assert (
        "stopped on request" in result and cast(object, output) == "published-workspace"
    )  # the fake `publish` returns a string
    assert project.seen["started"] == [], "no Claude session"
    _, prompts = project.thread()
    assert len(prompts) == 1
    assert prompts[0].startswith("Read .claude-flyte/BRIEF.md and get started on it now.")
    rows = [text for _, _, text in project.timeline._rows]
    assert any(text.startswith("router: author -> codex") for text in rows)
    assert _usage._author_engine == "codex" and os.environ["AGENT_FACTORY_CHOSEN_AUTHOR_ENGINE"] == "codex"
    # The standing instructions went to Codex's home, not into the project's own files.
    assert "agent-factory" in (project.state_root / "codex" / "AGENTS.md").read_text()
    assert not (project.workspace / "CLAUDE.md").exists()
    assert not (project.workspace / "AGENTS.md").exists()


async def test_the_thread_is_resumed_after_a_restart_from_the_checkpoint(project):
    """The thread is resumed after a restart from the checkpoint."""
    project.pump_that()
    await agent.project.func("brief", browser=False)
    thread_id, prompts = project.thread()
    assert project.seen["kickoff"]

    # A new pod: the checkpoint brought back $CODEX_HOME and the thread file; nothing else survives.
    project.seen["resumed"] = True
    project.pump_that("New direction arrived from Flyte.")
    await agent.project.func("brief", browser=False)

    assert project.seen["kickoff"] == "", "a resumed thread is not sent the brief again"
    assert project.seen["thread_id"] == thread_id
    again_id, again = project.thread()
    assert again_id == thread_id
    assert again[:1] == prompts and again[1:] == ["New direction arrived from Flyte."]


async def test_a_fresh_project_does_not_inherit_a_thread(project):
    """A fresh project does not inherit a thread."""
    project.state_root.joinpath("codex-thread").write_text("left-over")
    project.pump_that()
    await agent.project.func("brief", browser=False)
    assert project.seen["thread_id"] != "left-over"


async def test_the_input_pin_selects_the_author_engine(project, monkeypatch):
    # Claude is available too, and is not what the pin asked for.
    """The input pin selects the author engine."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    project.pump_that()
    await agent.project.func("brief", browser=False, engine="codex")
    assert project.seen["started"] == [] and project.thread()


async def test_a_missing_codex_login_fails_at_start_with_a_clear_error(project, monkeypatch):
    """A missing codex login fails at start with a clear error."""
    monkeypatch.delenv(_session_config.CODEX_TOKEN_ENV_VAR)
    project.pump_that()
    with pytest.raises(RuntimeError, match="no Codex credential available for the author session"):
        await agent.project.func("brief", browser=False)
    assert project.seen["started"] == []


async def test_a_missing_claude_login_still_fails_at_start_with_the_claude_error(project, monkeypatch):
    """A missing claude login still fails at start with the claude error."""
    monkeypatch.setattr(agent, "assert_claude_available", lambda: None)
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "")
    with pytest.raises(RuntimeError, match="no Claude setup-token available"):
        await agent.project.func("brief", browser=False)


async def test_a_claude_credential_starts_a_claude_session_as_before(project, monkeypatch):
    """A claude credential starts a claude session as before."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "")
    monkeypatch.setattr(agent, "assert_claude_available", lambda: None)
    with pytest.raises(AssertionError, match="a Claude session was started"):
        await agent.project.func("brief", browser=False)
    assert project.seen["started"] == ["claude"]
    assert _usage._author_engine == "claude"
    # No engine row: a Claude-only slot is not routed.
    assert not [1 for _, icon, _ in project.timeline._rows if icon == "🔀"]


async def test_a_coordinator_engine_pin_starts_the_codex_cli_with_the_coordinator_setup(project, monkeypatch):
    """`COORDINATOR_ENGINE=codex` runs the project's coordinator on the Codex CLI, set up the Codex way."""
    # Claude is available too and is what an unpinned run would start on.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    monkeypatch.setenv("AGENT_FACTORY_COORDINATOR_ENGINE", "codex")
    record = project.state_root / "record.jsonl"
    monkeypatch.setenv("FAKE_CODEX_RECORD", str(record))

    async def browser(timeline):
        _session_setup._write_mcp_config("http://127.0.0.1:9")
        return True, None

    monkeypatch.setattr(_session_setup, "_start_browser", browser)
    project.pump_that("Direction from the inbox.")
    await agent.project.func("brief", browser=True)

    assert project.seen["started"] == [] and _usage._author_engine == "codex"
    turns = [json.loads(line) for line in record.read_text().splitlines()]
    assert len(turns) == 2
    home = project.state_root / "codex"
    for turn in turns:
        assert turn["cwd"] == str(project.workspace)
        assert "--dangerously-bypass-approvals-and-sandbox" in turn["argv"]
        assert "CODEX_HOME" in turn["env"]
        # The token is installed into $CODEX_HOME by `codex login`; the session never sees it raw.
        assert _session_config.CODEX_TOKEN_ENV_VAR not in turn["env"]
    assert (home / "auth.json").is_file()
    assert "[mcp_servers.browser]" in (home / "config.toml").read_text()
    # The coordinator instructions, in Codex's own file, say what Codex lacks.
    memory = (home / "AGENTS.md").read_text()
    assert "Codex has no subagent roles" in memory and "delegate" in memory
    assert not (project.workspace / "CLAUDE.md").exists()
    rows = [text for _, _, text in project.timeline._rows]
    assert any(text.startswith("codex coordinator: no .claude/agents sub-agent roles") for text in rows)
    # Inbox direction reached the running coordinator's thread as a later turn.
    _, prompts = project.thread()
    assert prompts[0].startswith("Read .claude-flyte/BRIEF.md") and prompts[1:] == [
        "Direction from the inbox."
    ]


async def test_a_coordinator_engine_pin_to_codex_resumes_the_saved_thread_after_a_restart(
    project, monkeypatch
):
    """After a restart from the checkpoint a pinned Codex coordinator resumes its thread, not a new one."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    monkeypatch.setenv("AGENT_FACTORY_COORDINATOR_ENGINE", "codex")
    project.pump_that()
    await agent.project.func("brief", browser=False)
    thread_id, _ = project.thread()
    project.seen["resumed"] = True
    project.pump_that("Direction after the restart.")
    await agent.project.func("brief", browser=False)
    assert project.thread()[0] == thread_id
    assert project.thread()[1][-1] == "Direction after the restart."


async def test_an_unpinned_or_claude_pinned_coordinator_stays_on_claude(choose, monkeypatch):
    """`claude` keeps today's argv: the pin only chooses the engine."""
    monkeypatch.setenv("AGENT_FACTORY_COORDINATOR_ENGINE", "claude")
    assert (await choose(available=("claude", "codex")))[0] == "claude"
    monkeypatch.delenv("AGENT_FACTORY_COORDINATOR_ENGINE")
    assert _author_module._author_argv("p", "bypassPermissions", "sonnet", "") == [
        "claude",
        "--name",
        "p",
        "--permission-mode",
        "bypassPermissions",
        "--model",
        "sonnet",
    ]


async def test_the_coordinator_engine_pin_and_its_alias_choose_the_engine(choose, monkeypatch):
    """The canonical name and the old one pin alike; both set and different is refused."""
    monkeypatch.setenv("AGENT_FACTORY_COORDINATOR_ENGINE", "codex")
    assert (await choose())[0] == "codex"
    monkeypatch.delenv("AGENT_FACTORY_COORDINATOR_ENGINE")
    monkeypatch.setenv("AGENT_FACTORY_AUTHOR_ENGINE", "codex")
    assert (await choose())[0] == "codex"
    monkeypatch.setenv("AGENT_FACTORY_COORDINATOR_ENGINE", "claude")
    with pytest.raises(ValueError, match="disagree"):
        await choose()


async def test_compaction_between_rounds_is_skipped_for_a_codex_coordinator(monkeypatch):
    """`codex exec` takes no slash command, so `/compact` is never sent to it; the timeline says so."""
    from flyte_agent_factory import _supervise_support

    monkeypatch.setenv("AGENT_FACTORY_COMPACT_BETWEEN_ROUNDS", "1")
    monkeypatch.setattr(_usage, "_author_engine", "codex")
    timeline = _timeline._Timeline()

    class Proc:
        async def submit(self, *args, **kwargs):
            raise AssertionError("/compact was sent to a Codex session")

    assert await _supervise_support._compact_between_rounds(Proc(), timeline, asyncio.Event()) is True  # type: ignore[arg-type]
    assert any("cannot be compacted" in text for _, _, text in timeline._rows)


# --------------------------------------------------------------------------- #
# Profiles
# --------------------------------------------------------------------------- #


def _profile_text(secrets: str = "") -> str:
    """Reduce the suite's profile to one slot whose [secrets] extras are given."""
    text = WORK.read_text()
    head = text[: text.index("[[slots]]")]
    return (
        f'{head}[[slots]]\nenv = "agent-one"\nsubagent_env = "sub-one"\n\n'
        f'[secrets]\nclaude_token = "token_x"\ndeploy_key = "deploy_key_x"\n{secrets}\n[policy]\n'
    )


def test_a_profile_with_a_codex_token_exports_both_token_secrets():
    """A profile with a codex token exports both token secrets, and the meters name both accounts."""
    profile = _profile.parse(_profile_text('codex_token = "codex_tok_x"\n'))
    (slot,) = profile.slots
    env = profile.deploy_env(slot)
    assert env["AGENT_FACTORY_CLAUDE_TOKEN_SECRET"] == "token_x"
    assert env["AGENT_FACTORY_CODEX_TOKEN_SECRET"] == "codex_tok_x"
    assert not [key for key in env if "CREDENTIALS_SECRET" in key or key == "AGENT_FACTORY_CODEX_SECRET"]
    assert profile.meter_logins == {"claude token_x": "token_x", "codex codex_tok_x": "codex_tok_x"}
    assert json.dumps(env)  # serialisable


def test_the_deploy_mounts_the_claude_token_and_the_codex_token(monkeypatch):
    """The deploy mounts the claude token and the codex token, each as its own env var."""
    for var in ("AGENT_FACTORY_CLAUDE_TOKEN_SECRET", "AGENT_FACTORY_CODEX_TOKEN_SECRET"):
        monkeypatch.delenv(var, raising=False)
    profile = _profile.parse(_profile_text('codex_token = "codex_tok_x"\n'))
    env = profile.deploy_env(profile.slots[0])
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        _session_config, "CODEX_TOKEN_SECRET", env["AGENT_FACTORY_CODEX_TOKEN_SECRET"]
    )  # read at import
    mounted = [(s.key, s.as_env_var) for s in agent._credentials_secret()]
    assert mounted == [("token_x", "CLAUDE_CODE_OAUTH_TOKEN"), ("codex_tok_x", "CODEX_ACCESS_TOKEN")]


def test_a_profile_without_a_claude_token_is_refused_and_one_without_a_codex_token_has_no_codex():
    """A profile without a claude token is refused; one without a codex token deploys no codex secret."""
    with pytest.raises(_profile.ProfileError, match="claude_token is required"):
        _profile.parse(_profile_text().replace('claude_token = "token_x"\n', ""))
    profile = _profile.parse(_profile_text())
    assert "AGENT_FACTORY_CODEX_TOKEN_SECRET" not in profile.deploy_env(profile.slots[0])
    assert profile.meter_logins == {"claude token_x": "token_x"}


async def test_concurrent_submissions_reach_one_thread_in_order(tmp_path):
    """Concurrent submissions reach one thread in order."""
    proc = _author(tmp_path, FAKE_CODEX_SLEEP="0.2")
    await asyncio.gather(proc.submit("BRIEF"), proc.submit("INBOX"))
    try:
        await wait_for(lambda: "INBOX" in _thread(tmp_path / "home", proc) and not proc.running)
        assert len(list((tmp_path / "home" / "threads").iterdir())) == 1
        assert _thread(tmp_path / "home", proc) == ["BRIEF", "INBOX"]
    finally:
        await proc.stop(grace=1)


async def test_queued_findings_survive_a_killed_turn_and_restart(tmp_path):
    """Queued findings survive a killed turn and restart."""
    import signal

    before = _author(tmp_path, FAKE_CODEX_SLEEP="60")
    await before.submit("BRIEF")
    await wait_for(lambda: before.thread_id is not None)
    await before.submit("QUEUED REVIEW FINDINGS")
    assert before._proc is not None
    os.killpg(before._proc.pid, signal.SIGKILL)
    await before.wait()
    after = _author(tmp_path)
    try:
        await after.submit("RESUME")
        await wait_for(
            lambda: "QUEUED REVIEW FINDINGS" in _thread(tmp_path / "home", after) and not after.running
        )
        assert after.thread_id == before.thread_id
        assert _thread(tmp_path / "home", after).count("QUEUED REVIEW FINDINGS") == 1
        restarted = _author(tmp_path)
        await restarted.submit("NEXT")
        await restarted.wait()
        assert _thread(tmp_path / "home", restarted).count("QUEUED REVIEW FINDINGS") == 1
    finally:
        await after.stop(grace=1)


@pytest.mark.parametrize(
    "purpose",
    ["correctness", "completeness", "craft", "repair", "contract", "arbiter", "delegate", "planner"],
)
def test_every_route_uses_the_codex_only_slots_credential(purpose, tmp_path, monkeypatch):
    """Every route uses the codex only slots credential."""
    monkeypatch.setattr(_session_config, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "codex_token")
    monkeypatch.setattr(_usage, "_author_engine", "codex")
    monkeypatch.setattr(_usage, "_meters", None)
    for var in ("CLAUDE_CODE_OAUTH_TOKEN", "AGENT_FACTORY_CLAUDE_TOKEN_SECRET"):
        monkeypatch.delenv(var, raising=False)
    assert _usage._route(purpose, _timeline._Timeline()).engine == "codex"


def test_codex_admission_uses_the_codex_tokens_capacity(tmp_path, monkeypatch):
    """Codex admission uses the codex token's own meter, with the claude token refused throughout."""
    from flyte_agent_factory import (
        _factory_base,
        _factory_meters,
        _planner,
        _planner_facts,
        _planner_runtime,
    )

    profile = _profile.parse(_profile_text('codex_token = "codex_tok_x"\n'))
    slot = profile.slots[0].task
    monkeypatch.setattr(_factory_base, "PROFILE", profile)
    monkeypatch.setattr(_factory_base, "SLOTS", (slot,))
    monkeypatch.setattr(_factory_base, "cap", lambda: 1)
    now = time.time()
    claude = _meter.Meter("claude token_x", "oauth-usage", now, 100, now + 3600, limited_until=now + 3600)
    # Owner rule 2: a 100% reading is a hint and still admits; only a refusal waits. This looped over
    # (10, 100) and expected 100 to wait.
    for used, limited in ((10, None), (100, None), (100, now + 3600)):
        meter = _meter.Meter("codex codex_tok_x", "app-server", now, used, now + 3600, limited_until=limited)
        reports = {slot: [claude, meter]}
        monkeypatch.setattr(_factory_meters, "slot_meters", lambda *_, reports=reports: reports)
        monkeypatch.setattr(remote.Run, "listall", lambda **_: [])
        capacity = _planner_facts.free_capacity(profile, [], reports)
        assert [(c.engine, c.login) for c in capacity] == [("claude", "token_x"), ("codex", "codex_tok_x")]
        assert all(c.engine in _planner_runtime.AUTHOR_ENGINES for c in capacity)
        codex = tuple(c for c in capacity if c.engine == "codex")
        item = _planner.BoardItem("I1", 1, "team/repo", status="Ready", body="## Files\n- a.py")
        plan = _planner.plan(_planner.Facts(items=(item,), capacity=codex, now=now))
        headroom = _factory_meters.slot_headroom((slot,), now=now)
        if limited:
            assert not plan.launches
            assert plan.waiting[0].reason.code == "no_capacity"
            assert _factory_meters.free_slot([], headroom=headroom) is None
        else:
            assert plan.launches[0].engine == "codex"
            assert _factory_meters.free_slot([], headroom=headroom) == slot
        assert profile.meter_logins == {"claude token_x": "token_x", "codex codex_tok_x": "codex_tok_x"}


async def test_a_codex_only_judge_runs_through_the_subagent_task(tmp_path, monkeypatch):
    """A codex only judge runs through the subagent task."""
    import flyte.report

    # The in-package roles, not whatever image roles directory the machine happens to have.
    monkeypatch.setattr(_session_setup, "AGENT_ROLES_DIR", str(tmp_path / "no-image-roles"))
    monkeypatch.setattr(_session_config, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "codex_token")
    monkeypatch.setattr(_usage, "_author_engine", "codex")
    monkeypatch.setattr(_usage, "_meters", None)
    for var in ("CLAUDE_CODE_OAUTH_TOKEN", "AGENT_FACTORY_CLAUDE_TOKEN_SECRET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(_session_config.CODEX_TOKEN_ENV_VAR, "codex-access-token")
    monkeypatch.setattr(_author_module, "_CODEX_COMMAND", (sys.executable, str(FAKE)))  # the token login too
    monkeypatch.setenv("FAKE_CODEX_ANSWER", "VERDICT: PASS")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    async def log(*args, **kwargs):
        pass

    monkeypatch.setattr(flyte.report.log, "aio", log)
    real_exec = asyncio.create_subprocess_exec

    async def spawn(program, *args, **kwargs):
        if program == "codex":
            return await real_exec(sys.executable, str(FAKE), *args, **kwargs)
        raise AssertionError(f"unexpected reviewer process: {program}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(agent.subagent, "override", lambda **_: SimpleNamespace(aio=agent.subagent.func))
    outcomes: dict[str, _review._JudgeOutcome] = {}
    timeline = _timeline._Timeline()
    judge, passed, _ = await _review._one_judge(
        "correctness", "Review the change", cast(Any, None), "brief", "", 1, timeline, outcomes=outcomes
    )
    assert judge == "correctness" and passed
    assert outcomes["correctness"].outcome == _review.PASS
    assert any("correctness -> codex" in text for _, _, text in timeline._rows)
    assert len(list((tmp_path / ".codex-subagents").glob("*/threads/*"))) == 1


def test_a_busy_slots_codex_refusal_blocks_the_free_slots_codex_capacity(monkeypatch):
    """A busy slots codex refusal blocks free codex only slots."""
    from flyte_agent_factory import _factory_base, _planner, _planner_facts

    profile = _profile.parse(
        # Every slot shares the one Codex access token, so its refusal holds all of them.
        WORK.read_text().replace("[secrets]\n", '[secrets]\ncodex_token = "codex_tok_x"\n')
    )
    first, second = profile.slots
    monkeypatch.setattr(_factory_base, "cap", lambda: 2)
    now = time.time()
    reports = {
        # Owner rule 2: the refusal is `limited_until`; this used a 100% reading alone.
        first.task: [_meter.Meter("codex", "app-server", now, 100, now + 3600, limited_until=now + 3600)],
        second.task: [_meter.Meter("codex", "app-server", now - 10, 10, now + 3600)],
    }
    live: Any = [
        SimpleNamespace(to_dict=lambda: {"action": {"metadata": {"task": {"id": {"name": first.task}}}}})
    ]  # fake runs
    capacity = _planner_facts.free_capacity(profile, live, reports)
    codex = tuple(c for c in capacity if c.engine == "codex")
    assert [c.slot for c in codex] == [second.task]
    item = _planner.BoardItem("I1", 1, "team/repo", status="Ready", body="## Files\n- a.py")
    plan = _planner.plan(_planner.Facts(items=(item,), capacity=codex, now=now))
    assert not plan.launches
    assert plan.waiting[0].reason.code == "no_capacity"


async def test_stop_preserves_undelivered_messages_and_restore_drains_them(tmp_path):
    """Stop preserves undelivered messages and restore drains them."""
    before = _author(tmp_path, FAKE_CODEX_SLEEP="60")
    await before.submit("BRIEF")
    await wait_for(lambda: before.thread_id is not None)
    await before.submit("FINDINGS")
    await before.stop(grace=1)
    after = _author(tmp_path)
    try:
        await wait_for(lambda: "FINDINGS" in _thread(tmp_path / "home", after) and not after.running)
        assert _thread(tmp_path / "home", after).count("FINDINGS") == 1
    finally:
        await after.stop(grace=1)


# Owner rule 2: a full meter is a hint, so only a refusal makes the loop wait. This was
# [(10, "run"), (100, "wait")], with the 100% reading alone as the limit. The loop is metered on the
# work token (`_loop_slot_budget`).
@pytest.mark.parametrize(
    "used,limited,expected", [(10, False, "run"), (100, False, "run"), (100, True, "wait")]
)
def test_loop_admission_uses_the_work_tokens_meter(monkeypatch, used, limited, expected):
    """Loop admission uses the work token's meter."""
    from dataclasses import replace

    from flyte_agent_factory import _factory_base, _factory_meters, _loop_flow

    profile = _profile.parse(_profile_text())
    profile = replace(profile, loop=replace(profile.loop, daily_usd=100, weekly_usd=500))
    monkeypatch.setattr(_factory_base, "PROFILE", profile)
    now = time.time()
    until = now + 3600 if limited else None
    meter = _meter.Meter("claude token_x", "oauth-usage", now, used, now + 3600, limited_until=until)
    monkeypatch.setattr(_factory_meters, "login_meters", lambda **_: {meter.account: meter})
    monkeypatch.setattr(_loop_flow, "_spend", lambda _: [])
    assert _factory_meters._loop_slot_budget(profile.slots[0].task, demand=False).action == expected


async def test_a_crash_after_an_agent_message_is_not_replayed(tmp_path):
    """A crash after an agent message is not replayed."""
    proc = _author(tmp_path, FAKE_CODEX_FAIL_AFTER_MESSAGE="1")
    await proc.submit("BRIEF")
    await wait_for(lambda: proc.returncode is not None)
    await asyncio.sleep(1.0)
    # One attempt only: the supervisor sees the crash and decides; the prompt stays pending for it.
    assert _thread(tmp_path / "home", proc).count("BRIEF") == 1
    assert proc.returncode == 3


async def test_a_codex_only_judge_finds_its_role_without_the_image_roles_dir(tmp_path, monkeypatch):
    # CI has no /opt/claude-roles: Codex sub-agents fall back to the in-package roles, as Claude's do.
    """A codex only judge finds its role without the image roles dir."""
    monkeypatch.setattr(_session_setup, "AGENT_ROLES_DIR", str(tmp_path / "no-image-roles"))
    await test_a_codex_only_judge_runs_through_the_subagent_task(tmp_path, monkeypatch)


@pytest.mark.parametrize("newline", [True, False])
@pytest.mark.parametrize("force_kill", [True, False])
async def test_stop_drains_final_output_and_acknowledges_with_detached_stdout(tmp_path, newline, force_kill):
    """Stop drains final output and acknowledges with detached stdout."""
    script = tmp_path / "final.py"
    script.write_text(
        "import json, os, signal, subprocess, sys\n"
        "def finish(*args):\n"
        "    events = [\n"
        "        {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'final answer'}},\n"
        "        {'type': 'turn.completed', 'usage': {'input_tokens': 10, 'output_tokens': 2}}]\n"
        f"    newline = {newline!r}\n"
        "    if not newline: events.reverse()\n"
        "    events.insert(0, {'type': 'token_count', 'rate_limits': "
        "{'primary': {'used_percent': 20, 'window_minutes': 300, 'resets_in_seconds': 1800}}})\n"
        "    data = '\\n'.join(json.dumps(event) for event in events) + ('\\n' if newline else '')\n"
        "    os.write(1, data.encode())\n"
        f"    if {force_kill!r}: signal.pause()\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, finish)\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
        "start_new_session=True)\n"
        f"open({str(tmp_path / 'child.pid')!r}, 'w').write(str(child.pid))\n"
        "print(json.dumps({'type': 'thread.started', 'thread_id': 'thread'}), flush=True)\n"
        "signal.pause()\n"
    )
    proc = CodexAuthor(
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"]},
        command=(sys.executable, str(script)),
        thread_file=tmp_path / "state" / "codex-thread",
    )
    try:
        await proc.submit("BRIEF")
        await wait_for(lambda: proc.thread_id is not None)
        async with asyncio.timeout(30):
            await proc.stop(grace=0.3)
        assert proc.last_message == "final answer"
        assert proc.turn_completed
        assert proc.meter is not None
        assert proc._queued == []
        assert json.loads(proc._pending_file.read_text()) == []
        restarted = _author(tmp_path)
        assert restarted._queued == []
    finally:
        child_pid = tmp_path / "child.pid"
        if child_pid.exists():
            os.kill(int(child_pid.read_text()), 9)
        await proc.stop(grace=0.1)


# --------------------------------------------------------------------------- #
# A dead Codex login (#64)
# --------------------------------------------------------------------------- #


async def _idle_pump(*args, **kwargs):
    """Keep the run going until the author itself ends it: the kickoff turn is sent by `project`."""
    await asyncio.Event().wait()


async def test_a_turn_refused_for_a_spent_refresh_token_is_an_auth_failure_not_a_plain_crash(tmp_path):
    """A turn refused for a spent refresh token is an auth failure not a plain crash."""
    proc = _author(tmp_path, FAKE_CODEX_AUTH="reused")
    await proc.submit("go")
    await proc.wait()
    assert proc.returncode == 1
    assert "refresh token was already used" in proc.auth_failure
    # The next turn starts clean.
    proc._env.pop("FAKE_CODEX_AUTH")
    await proc.submit("again")
    await proc.wait()
    assert proc.auth_failure == "" and proc.returncode is None


async def test_a_bare_401_fails_a_turn_only_as_its_final_error(tmp_path):
    """A bare 401 fails a turn only as its final error."""
    proc = _author(tmp_path, FAKE_CODEX_AUTH="401")
    await proc.submit("go")
    await proc.wait()
    assert proc.returncode == 1 and "401 Unauthorized" in proc.auth_failure


async def test_a_reconnect_that_prints_401_and_then_completes_is_no_failure(tmp_path):
    """A reconnect that prints 401 and then completes is no failure."""
    proc = _author(tmp_path, FAKE_CODEX_AUTH="reconnect")
    await proc.submit("go")
    await proc.wait()
    assert proc.returncode is None and proc.auth_failure == ""


async def test_an_ordinary_crash_is_not_an_auth_failure(tmp_path):
    """An ordinary crash is not an auth failure."""
    proc = _author(tmp_path, FAKE_CODEX_CRASH="1")
    await proc.submit("go")
    await proc.wait()
    assert proc.returncode == 3 and proc.auth_failure == ""


async def test_a_dead_token_ends_the_run_without_retries_and_says_to_replace_it(project, monkeypatch):
    """A dead token ends the run without retries and says to store a new one in its secret."""
    import flyte.errors

    monkeypatch.setenv("FAKE_CODEX_AUTH", "reused")
    monkeypatch.setattr(_supervise, "_pump", _idle_pump)
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "cx_mine")
    with pytest.raises(flyte.errors.NonRecoverableError, match="store a new token in that secret") as failure:
        await agent.project.func("brief", browser=False)
    assert "[secrets] codex_token" in str(failure.value) and "cx_mine" in str(failure.value)
    # One attempt only: a single turn reached the (fake) Codex, and the row names the way out.
    assert len(project.thread()[1]) == 1
    rows = [text for _, _, text in project.timeline._rows]
    assert any("Codex login is unusable" in text and "already used" in text for text in rows)
    assert any("ending without retries" in text for text in rows)
    assert all("cx_mine" in text for text in rows if "store a new token" in text)
    assert not any("bootstrap" in text for text in rows)


async def _refuse_codex_token(project, monkeypatch):
    """Run the author on a Codex access token that Codex refuses; return the error and the timeline rows."""
    import flyte.errors

    monkeypatch.setenv("FAKE_CODEX_AUTH", "401")
    monkeypatch.setattr(_supervise, "_pump", _idle_pump)
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "cx_token")
    monkeypatch.setenv(_session_config.CODEX_TOKEN_ENV_VAR, "tok-abc")
    with pytest.raises(flyte.errors.NonRecoverableError) as failure:
        await agent.project.func("brief", browser=False)
    rows = [text for _, icon, text in project.timeline._rows if icon in ("🔐", "🛑")]
    assert len(rows) == 2
    return str(failure.value), rows


async def test_a_refused_access_token_says_to_replace_the_token_not_to_sign_in(project, monkeypatch):
    """A refused access token says to replace the token, not to sign in."""
    error, rows = await _refuse_codex_token(project, monkeypatch)
    for text in [error, *rows]:
        assert "cx_token" in text and "access token" in text
        assert "bootstrap" not in text and "secret_name" not in text


async def test_a_dead_login_on_the_codex_engine_input_ends_and_marks_codex_broken(project, monkeypatch):
    """A dead login with the `engine` input codex ends for a human, as hard pins do, and marks codex broken.

    Replaces `..._hands_the_author_to_claude_...` with the same inputs: the `engine` input is a pin, and
    since #125 a pin is hard unless `ENGINE_FALLBACK` is "allow" (the test below keeps the handoff).
    """
    import flyte.errors

    monkeypatch.setenv("FAKE_CODEX_AUTH", "reused")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    monkeypatch.setattr(_supervise, "_pump", _idle_pump)
    with pytest.raises(flyte.errors.NonRecoverableError, match="store a new token"):
        await agent.project.func("brief", browser=False, engine="codex")
    assert not list((project.state_root / "author-handoffs").glob("*.json"))
    assert _usage._meters is not None
    assert _usage._meters.latest["codex"].error == "codex login unusable"
    rows = [text for _, _, text in project.timeline._rows]
    [stop] = [text for _, icon, text in project.timeline._rows if icon == "🛑"]
    assert "pinned to Codex" in stop
    # Codex cannot be waited for, so the pause says a human is needed and how to unblock it.
    assert any("a human needs to look" in text and "codex_token" in text for text in rows)


async def test_a_dead_login_hands_the_author_to_claude_and_marks_codex_broken(project, monkeypatch):
    """A dead login hands the author to claude and marks codex broken, when ENGINE_FALLBACK allows it."""
    monkeypatch.setenv("AGENT_FACTORY_ENGINE_FALLBACK", "allow")
    monkeypatch.setenv("FAKE_CODEX_AUTH", "reused")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    monkeypatch.setattr(_supervise, "_pump", _idle_pump)
    with pytest.raises(_author_module.AuthorHandoffRestart, match="codex -> claude"):
        await agent.project.func("brief", browser=False, engine="codex")
    [record] = [json.loads(p.read_text()) for p in (project.state_root / "author-handoffs").glob("*.json")]
    assert (record["from"], record["to"], record["reason"]) == ("codex", "claude", "codex login unusable")
    assert _usage._meters is not None
    broken = _usage._meters.latest["codex"]
    assert broken.error == "codex login unusable"
    assert broken.limited_until is not None and broken.limited_until > time.time() + 3600
    rows = [text for _, _, text in project.timeline._rows]
    assert any("author handoff codex -> claude: codex login unusable" in text for text in rows)
    # The next pod resumes on Claude, which holds the conversation now.
    assert json.loads((project.state_root / "author-conversation.json").read_text())["engine"] == "claude"


async def test_a_dead_login_does_not_fall_back_when_the_author_is_hard_pinned_to_codex(project, monkeypatch):
    """A dead login does not fall back when the author is hard pinned to codex."""
    import flyte.errors

    monkeypatch.setenv("FAKE_CODEX_AUTH", "reused")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    monkeypatch.setattr(_supervise, "_pump", _idle_pump)
    with pytest.raises(flyte.errors.NonRecoverableError, match="store a new token"):
        await agent.project.func("brief", browser=False, engine="codex", hard_pin=True)
    assert not list((project.state_root / "author-handoffs").glob("*.json"))
    # Claude is right there; the stop row must say why it was not used, not that it does not exist.
    [stop] = [text for _, icon, text in project.timeline._rows if icon == "🛑"]
    assert "pinned to Codex" in stop and "no other engine" not in stop


async def test_an_ordinary_codex_crash_still_retries(project, monkeypatch):
    """An ordinary codex crash still retries."""
    monkeypatch.setenv("FAKE_CODEX_CRASH", "1")
    monkeypatch.setattr(_supervise, "_pump", _idle_pump)
    with pytest.raises(RuntimeError, match="Codex exited with status 3; retrying from the last checkpoint"):
        await agent.project.func("brief", browser=False)
