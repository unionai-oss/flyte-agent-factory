"""The pty runner: first-run prompts and typed input.

The fake below stands in for the Claude Code TUI. It is deliberately crude --
what is being tested is this package's side of the terminal, not Claude Code.
"""

import asyncio
import os
import sys

import pytest

from flyte_agent_factory import _author
from flyte_agent_factory._process import ClaudeProcess, default_prompt_rules, strip_ansi

FAKE_TUI = r"""
import sys
print("\x1b[1mDo you trust the files in this folder?\x1b[0m [y/N]", flush=True)
print(f"TRUST_ANSWER={sys.stdin.readline().strip()!r}", flush=True)
for _ in range(2):
    line = sys.stdin.readline()
    if not line:
        break
    print(f"SUBMITTED={line.strip()!r}", flush=True)
print("BYE", flush=True)
"""


def test_strip_ansi_keeps_text_and_tabs():
    assert strip_ansi("\x1b[1mbold\x1b[0m\x1b]0;title\x07 plain\ttab") == "bold plain\ttab"


def test_prompt_rules_fire_once():
    (trust, *_) = default_prompt_rules()
    assert trust.match("Do you trust the files in this folder?")
    # A one-shot rule must not answer the same prompt twice, or a repainting
    # TUI would receive a stream of stray keystrokes.
    assert not trust.match("Do you trust the files in this folder?")


@pytest.fixture
def fake_tui(tmp_path):
    script = tmp_path / "fake_tui.py"
    script.write_text(FAKE_TUI)
    return script


@pytest.mark.asyncio
async def test_session_lifecycle(fake_tui, tmp_path):
    lines: list[str] = []
    proc = ClaudeProcess(
        [sys.executable, str(fake_tui)],
        cwd=tmp_path,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
        on_line=lines.append,
        inbox_messages=True,
    )
    await proc.start()
    # The fake reads its first stdin line as the trust answer, so submit only once that is answered.
    async with asyncio.timeout(30):
        while not any("TRUST_ANSWER" in line for line in lines):
            await asyncio.sleep(0.05)

    await proc.submit("read BRIEF.md and get started")
    # Multi-line input must arrive as one message: in the TUI a newline
    # submits, so the raw string would be torn into three half-messages.
    await proc.submit("multi\nline\nmessage")
    assert proc.last_submission is not None
    text, submitted_at, output_count = proc.last_submission
    assert text == "multi\nline\nmessage"
    assert submitted_at > 0
    assert 0 <= output_count <= proc.output_count
    code = await proc.wait()

    log = "\n".join(lines)
    assert "TRUST_ANSWER='y'" in log, "the trust prompt was not auto-answered"
    assert "SUBMITTED='read BRIEF.md and get started'" in log
    pointer = proc.sent_texts[-1]
    assert f"SUBMITTED={pointer!r}" in log
    assert (tmp_path / ".claude-flyte/inbox/message-0001.md").read_text() == "multi\nline\nmessage"
    assert "\x1b[" not in log, "escape sequences leaked into the log"
    assert code == 0


def test_a_default_launch_never_passes_remote_control():

    for mode in ("bypassPermissions", "acceptEdits"):
        argv = _author._author_argv("demo", mode, "", "")
        assert argv[:3] == ["claude", "--name", "demo"]
        assert not any("remote" in arg for arg in argv), argv


@pytest.mark.asyncio
async def test_exit_is_reported(tmp_path):
    proc = ClaudeProcess(
        [sys.executable, "-c", "raise SystemExit(7)"],
        cwd=tmp_path,
        env=dict(os.environ),
    )
    await proc.start()
    assert await proc.wait() == 7


@pytest.mark.asyncio
async def test_stop_kills_a_session_that_ignores_exit(tmp_path):
    # Ignores stdin entirely, so `/exit` can't work and `stop` has to escalate.
    proc = ClaudeProcess(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        cwd=tmp_path,
        env=dict(os.environ),
    )
    await proc.start()
    await proc.stop(grace=1.0)
    assert proc.returncode is not None


def test_bypass_permissions_menu_is_accepted_not_declined():
    """The one screen where a bare Enter is the wrong answer.

    `bypassPermissions` shows a two-option menu with "No, exit" highlighted.
    Pressing Enter quits Claude Code, so an unattended session parks at startup.
    The selection has to move to
    "Yes, I accept" first.
    """
    screen = (
        "Claude Code running in Bypass Permissions mode\n"
        "In Bypass Permissions mode, Claude Code will not ask for your approval\n"
        "❯ No, exit\n"
        "  Yes, I accept\n"
        "Enter to confirm · Esc to cancel\n"
    )

    matched = [rule for rule in default_prompt_rules() if rule.pattern.search(screen)]
    assert matched, "nothing answers the bypassPermissions screen"
    for rule in matched:
        assert rule.response == "\x1b[B\r", f"{rule.pattern.pattern!r} would answer with {rule.response!r}"


def test_no_rule_blindly_confirms_an_unknown_screen():
    # A catch-all Enter-presser eventually confirms a dialog whose default is
    # the destructive option.
    for rule in default_prompt_rules():
        if rule.response == "\r":
            assert rule.pattern.search("Choose a theme"), (
                f"{rule.pattern.pattern!r} sends a bare Enter but is not tied to a known screen"
            )


def test_resume_only_when_there_is_something_to_resume(tmp_path):
    """`--continue` with no transcript exits in milliseconds, which under
    retries is a crash loop rather than a failure."""
    from flyte_agent_factory._author import has_conversation

    config = tmp_path / "cfg"
    config.mkdir()
    assert not has_conversation(config)

    projects = config / "projects" / "-home-flyte-workspace"
    projects.mkdir(parents=True)
    # A snapshotted config dir can exist with no conversation in it.
    assert not has_conversation(config)

    (projects / "abc.jsonl").write_text("")
    assert not has_conversation(config), "an empty transcript is not resumable"

    (projects / "abc.jsonl").write_text('{"type":"user"}\n')
    assert has_conversation(config)


def test_argv_candidates_fall_back_to_a_fresh_start():
    from flyte_agent_factory._author import session_argv_candidates

    base = ["claude", "--name", "a-project"]

    assert session_argv_candidates(base, resume=False) == [base]

    resumed = session_argv_candidates(base, resume=True)
    assert resumed[0] == [*base, "--continue"], "resume should be tried first"
    assert resumed[-1] == base, "a plain start must remain as the fallback"


def test_expired_login_is_noticed_through_the_tui(tmp_path):
    """An expired credential is reported in text, not by exiting.

    Claude Code answers the first prompt with "Login expired - Please run
    /login" and then goes quiet. Nothing exits and nothing hits stderr, so the
    supervisor's idle heuristic reads it as a session waiting for direction --
    which is how a run sat for hours unable to reach the model at all.

    The screen is replayed as the pty actually delivers it: the TUI positions
    its status bar with cursor-forward escapes rather than spaces, so stripping
    ANSI leaves words run together.
    """
    proc = ClaudeProcess(["true"], cwd=tmp_path, env={})
    assert not proc.login_expired

    proc._consume("\x1b[2m\x1b[33m●\x1b[0m Login expired \x1b[2m·\x1b[0m Please run /login\r\n")
    assert proc.login_expired


def test_expired_login_survives_the_status_bar_losing_its_spaces(tmp_path):
    proc = ClaudeProcess(["true"], cwd=tmp_path, env={})
    # What the status bar reduces to once cursor moves are stripped.
    proc._consume("⏵⏵bypasspermissionson(shift+tabtocycle)Notloggedin·Run/login")
    assert proc.login_expired


def test_a_healthy_session_is_not_flagged_as_logged_out(tmp_path):
    proc = ClaudeProcess(["true"], cwd=tmp_path, env={})
    proc._consume("Claude Code v2.1.287\nSonnet 5.5\nI read BRIEF.md. Starting with the in-range baseline.\n")
    assert not proc.login_expired


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["review " * 1200, "passed\nfindings", "interrupted\rmessage"])
async def test_large_messages_use_complete_inbox_files(tmp_path, monkeypatch, text):
    proc = ClaudeProcess(["true"], cwd=tmp_path, env={}, inbox_messages=True)
    typed: list[str] = []
    monkeypatch.setattr(proc, "type", typed.append)
    inbox = tmp_path / ".claude-flyte/inbox"
    inbox.mkdir(parents=True)
    (inbox / "001.md").write_text("direction")
    (inbox / "message-0001.md").write_text("earlier message")
    await proc.submit(text)
    assert (inbox / "message-0002.md").read_bytes().decode().replace("\n", "") == text.replace("\n", "")
    assert max(map(len, (inbox / "message-0002.md").read_text().splitlines())) <= 1500
    assert (inbox / "001.md").read_text() == "direction"
    assert (inbox / "message-0001.md").read_text() == "earlier message"
    pointer = proc.sent_texts[-1]
    assert typed == [pointer, "\r"]
    assert "message-0002.md" in pointer and "complete message" in pointer
    assert len(pointer) < 400 and "\n" not in pointer
    assert proc.last_submission is not None
    assert proc.last_submission[0] == text


@pytest.mark.asyncio
async def test_short_message_is_submitted_verbatim(tmp_path, monkeypatch):
    proc = ClaudeProcess(["true"], cwd=tmp_path, env={}, inbox_messages=True)
    typed: list[str] = []
    monkeypatch.setattr(proc, "type", typed.append)
    await proc.submit("Continue fixing the findings.")
    assert typed == ["Continue fixing the findings.", "\r"]
    assert not (tmp_path / ".claude-flyte/inbox").exists()


@pytest.mark.asyncio
async def test_login_process_keeps_long_multiline_reply_verbatim(tmp_path, monkeypatch):
    proc = ClaudeProcess(["claude", "auth", "login"], cwd=tmp_path, env={})
    typed: list[str] = []
    monkeypatch.setattr(proc, "type", typed.append)
    reply = "authorization reply " * 200 + "\nsecond line"
    await proc.submit(reply)
    assert typed == [reply, "\r"]
    assert not (tmp_path / ".claude-flyte/inbox").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["Delegation finished: " + "notice " * 1000, "x" * 10000])
async def test_inbox_wraps_without_losing_characters(tmp_path, monkeypatch, text):
    proc = ClaudeProcess(["true"], cwd=tmp_path, env={}, inbox_messages=True)
    monkeypatch.setattr(proc, "type", lambda text: None)
    await proc.submit(text, label="Delegation finished")
    written = (tmp_path / ".claude-flyte/inbox/message-0001.md").read_text()
    assert all(len(line) <= 1500 for line in written.splitlines())
    assert written.replace("\n", "") == text
    assert proc.sent_texts[-1].startswith("Delegation finished. The complete message")
