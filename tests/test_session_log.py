"""The task log carries a readable session feed, never the terminal screen."""

import asyncio
import json
import os
from typing import cast

from flyte_agent_factory import _author, _session_log, _session_setup, _timeline
from flyte_agent_factory._bridge import redact_tokens
from flyte_agent_factory._process import ClaudeProcess
from tests._wait import wait_for

# A TUI redraw: cursor moves position the words, so a plain strip glues them together,
# and the same frame is painted over and over while the spinner turns.
REDRAW = (
    "\x1b[2K\x1b[1G\x1b[38;5;208m✻\x1b[39m Thinking…\x1b[1;5H(esc to interrupt)\r"
    "\x1b[2K\x1b[1G\x1b[38;5;208m✶\x1b[39m Thinking…\x1b[1;5H(esc to interrupt)\r"
    "\x1b[2K\x1b[1G✢\r\x1b[2K\x1b[1G·\r"
    "\x1b[3;1H╭──────╮\r\n\x1b[4;1H│ > try │\r\n\x1b[5;1H╰──────╯\r\n"
)


def test_pty_redraws_put_nothing_in_the_task_log(tmp_path, capsys):
    from flyte_agent_factory._session_setup import _log_session_line

    raw = tmp_path / "logs" / "terminal.log"
    proc = ClaudeProcess(
        ["true"],
        cwd=tmp_path,
        env={},
        on_line=_log_session_line(_timeline._Timeline(), raw),
        inbox_messages=True,
    )
    for _ in range(200):
        proc._consume(REDRAW)
    proc._consume("Editing src/x.py\r\n")

    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    # The in-memory tail is exactly what it was: the detection code reads it.
    assert len(proc.tail) >= 200
    # And the file is deduplicated: 200 identical redraws are one set of lines.
    written = raw.read_text().splitlines()
    assert written.count("Thinking…(esc to interrupt)") <= 1
    assert "Editing src/x.py" in written
    assert len(written) < 20


def test_raw_capture_is_bounded(tmp_path):
    raw = tmp_path / "terminal.log"
    capture = _session_log.RawCapture(raw, max_bytes=2000, backups=1)
    for n in range(5000):
        capture(f"distinct line {n} " + "x" * 40)
    capture.close()

    files = sorted(tmp_path.glob("terminal.log*"))
    assert [f.name for f in files] == ["terminal.log", "terminal.log.1"]
    assert sum(f.stat().st_size for f in files) <= 2 * 2000 + 200
    assert "distinct line 4999" in raw.read_text()


def test_raw_capture_redacts_tokens(tmp_path):
    raw = tmp_path / "terminal.log"
    capture = _session_log.RawCapture(raw)
    capture("export KEY=sk-ant-api03-abcdefghijklmnop")
    capture.close()
    assert "sk-ant" not in raw.read_text()


def _entry(kind, content, **extra):
    return (
        json.dumps(
            {
                "type": kind,
                "uuid": extra.pop("uuid", os.urandom(4).hex()),
                "message": {"content": content},
                **extra,
            }
        )
        + "\n"
    )


def test_transcript_becomes_session_lines(tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    transcript = folder / "abc.jsonl"
    transcript.write_text(
        _entry("user", "Read BRIEF.md and start")
        + _entry("assistant", [{"type": "thinking", "thinking": "secret thoughts"}])
        + _entry("assistant", [{"type": "text", "text": "I'll start with the tests. " * 20}])
        + _entry(
            "assistant",
            [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "git push origin HEAD"}}],
        )
        + _entry(
            "assistant",
            [
                {
                    "type": "tool_use",
                    "id": "t2",
                    "name": "Edit",
                    "input": {"file_path": "src/x.py", "old_string": "a" * 500, "new_string": "b"},
                }
            ],
        )
        + _entry(
            "user",
            [
                {"type": "tool_result", "tool_use_id": "t1", "content": "pushed 4000 lines of output " * 100},
                {
                    "type": "tool_result",
                    "tool_use_id": "t2",
                    "is_error": True,
                    "content": "String not found. token ghp_" + "a" * 36,
                },
            ],
        )
    )
    lines = _session_log.TranscriptTail(folder).poll()

    assert lines[0] == "[session] user: Read BRIEF.md and start"
    assert lines[1].startswith("[session] assistant: I'll start with the tests.")
    assert len(lines[1]) < 260
    assert "[session] tool: Bash: git push origin HEAD" in lines
    assert "[session] tool: Edit: src/x.py" in lines
    # A successful result is never logged; a failed one is short, named and redacted.
    assert not any("pushed 4000" in line for line in lines)
    assert not any("secret thoughts" in line for line in lines)
    error = next(line for line in lines if "tool error" in line)
    assert error.startswith("[session] tool error (Edit): String not found.")
    assert "ghp_" not in error


def test_tail_is_incremental_and_waits_for_whole_lines(tmp_path):
    folder = tmp_path
    path = folder / "s.jsonl"
    tail = _session_log.TranscriptTail(folder)
    whole = _entry("user", "first", uuid="u1")
    half = _entry("user", "second", uuid="u2")
    path.write_text(whole + half[:20])
    assert tail.poll() == ["[session] user: first"]
    assert tail.poll() == []
    with path.open("a") as fh:
        fh.write(half[20:])
    assert tail.poll() == ["[session] user: second"]


def test_tail_survives_rotation_compaction_and_replays(tmp_path):
    path = tmp_path / "s.jsonl"
    tail = _session_log.TranscriptTail(tmp_path)
    path.write_text(_entry("user", "one", uuid="u1") + _entry("user", "two", uuid="u2"))
    assert len(tail.poll()) == 2

    # Compaction rewrites the file shorter: the summary is not logged as news,
    # and a record already shown (same uuid) is not shown twice.
    path.write_text(_entry("user", "two", uuid="u2"))
    assert tail.poll() == []
    with path.open("a") as fh:
        fh.write(_entry("user", "three", uuid="u3"))
    assert tail.poll() == ["[session] user: three"]

    # A replaced file (new inode) is read from its start.
    path.unlink()
    path.write_text(_entry("user", "four", uuid="u4") + _entry("user", "five and some padding", uuid="u5"))
    assert tail.poll() == ["[session] user: four", "[session] user: five and some padding"]


def test_replaced_file_that_reuses_the_inode_is_read_from_its_start(tmp_path):
    # A filesystem may hand a freed inode to the next file created (this pod's overlay /tmp does), so
    # "replaced" cannot mean only "new inode". Truncating in place makes the reuse certain.
    path = tmp_path / "s.jsonl"
    tail = _session_log.TranscriptTail(tmp_path)
    path.write_text(_entry("user", "one", uuid="u1") + _entry("user", "two", uuid="u2"))
    assert len(tail.poll()) == 2
    inode = path.stat().st_ino

    path.write_text(_entry("user", "four", uuid="u4") + _entry("user", "five and some padding", uuid="u5"))
    assert path.stat().st_ino == inode
    assert tail.poll() == ["[session] user: four", "[session] user: five and some padding"]
    # And a file that merely grew is still read only from where it was.
    with path.open("a") as fh:
        fh.write(_entry("user", "six", uuid="u6"))
    assert tail.poll() == ["[session] user: six"]


def _real_format(kind, content, uuid):
    # Claude Code's own field order: ~70 identical bytes before anything that tells two files apart.
    return (
        json.dumps(
            {
                "parentUuid": None,
                "isSidechain": False,
                "userType": "external",
                "cwd": "/home/flyte/state/workspace",
                "sessionId": "s",
                "type": kind,
                "uuid": uuid,
                "message": {"content": content},
            }
        )
        + "\n"
    )


def test_a_replacement_in_claude_codes_own_format_is_read_from_its_start(tmp_path):
    path = tmp_path / "s.jsonl"
    tail = _session_log.TranscriptTail(tmp_path)
    path.write_text(_real_format("user", "one", "u1") + _real_format("user", "two", "u2"))
    assert len(tail.poll()) == 2
    path.write_text("".join(_real_format("user", f"new {n}", f"n{n}") for n in (1, 2, 3)))
    assert tail.poll() == ["[session] user: new 1", "[session] user: new 2", "[session] user: new 3"]


def test_resumed_session_does_not_replay_history(tmp_path):
    path = tmp_path / "old.jsonl"
    path.write_text(_entry("user", "from last week", uuid="u1"))
    tail = _session_log.TranscriptTail(tmp_path, skip_existing=True)
    assert tail.poll() == []
    with path.open("a") as fh:
        fh.write(_entry("user", "today", uuid="u2"))
    (tmp_path / "new.jsonl").write_text(_entry("user", "new file", uuid="u3"))
    assert sorted(tail.poll()) == ["[session] user: new file", "[session] user: today"]


def test_garbage_lines_are_ignored(tmp_path):
    (tmp_path / "s.jsonl").write_text(
        'not json\n[1,2]\n{"type":"user","message":"str"}\n' + _entry("user", "ok", uuid="u")
    )
    assert _session_log.TranscriptTail(tmp_path).poll() == ["[session] user: ok"]


async def test_feed_loop_emits_and_stops(tmp_path):
    (tmp_path / "s.jsonl").write_text(_entry("user", "hello", uuid="u"))
    out: list[str] = []
    stop = asyncio.Event()
    task = asyncio.create_task(
        _session_log.feed_loop(_session_log.TranscriptTail(tmp_path), out.append, stop, 0.05)
    )
    await wait_for(lambda: out)
    stop.set()
    await asyncio.wait_for(task, 30)
    assert out == ["[session] user: hello"]


def test_redact_tokens_formats():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc_DEF-123"  # gitleaks:allow
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"  # gitleaks:allow
    for secret in (
        "sk-ant-api03-AAAAAAAAAAAAAAAA",
        "ghp_" + "A" * 36,
        "github_pat_" + "B" * 30,
        "xoxb-1234567890-abcdefghij",
        "AIza" + "C" * 35,
        jwt,
        "tskey-auth-kABCDEF123456",
        "Authorization: Bearer abcdef0123456789abcdef",
        pem,
    ):
        cleaned = redact_tokens(f"before {secret} after")
        assert "<redacted>" in cleaned, secret
        assert cleaned.startswith("before ") or "Authorization" in cleaned
    assert redact_tokens("Bearer of bad news and sk-learn") == "Bearer of bad news and sk-learn"
    assert redact_tokens(f"x {pem} y") == "x <redacted> y"


def test_oversized_record_does_not_block_tail(tmp_path):
    raw = _entry("user", "x" * 1_200_000)
    (tmp_path / "s.jsonl").write_text(raw + _entry("user", "after"))
    tail = _session_log.TranscriptTail(tmp_path)
    lines = [line for _ in range(4) for line in tail.poll()]
    assert f"[session] (record of {len(raw.encode())} bytes skipped)" in lines
    assert "[session] user: after" in lines


def test_redact_before_truncating_and_multiline_capture(tmp_path):
    secret = "ghp_" + "A" * 36
    lines = _session_log.session_lines(json.loads(_entry("user", "x" * 190 + secret)), {})
    assert "ghp_" not in "".join(lines)
    for token in ("xoxp-1234567890-abcdefghij", "AKIA" + "A" * 16):
        assert token not in redact_tokens(token)
    capture = _session_log.RawCapture(tmp_path / "raw")
    for line in ("-----BEGIN RSA PRIVATE KEY-----", "MIIsecret", "-----END RSA PRIVATE KEY-----", "after"):
        capture(line)
    capture.close()
    assert "MIIsecret" not in (tmp_path / "raw").read_text()


async def test_feed_recovers_from_poll_error():
    stop = asyncio.Event()
    calls = 0

    def poll():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("temporary")
        stop.set()
        return ["recovered"]

    from types import SimpleNamespace

    out: list[str] = []
    # A stub with only `poll`, which is all feed_loop calls.
    tail = cast(_session_log.TranscriptTail, SimpleNamespace(poll=poll))
    await asyncio.wait_for(_session_log.feed_loop(tail, out.append, stop, 0.01), 30)
    assert "recovered" in out


async def test_start_failure_closes_capture(monkeypatch, tmp_path):

    capture = _session_log.RawCapture(tmp_path / "raw")
    monkeypatch.setattr(_session_setup, "_log_session_line", lambda _: capture)

    class Process:
        tail = ()

        def __init__(self, *args, **kwargs):
            pass

        async def start(self):
            raise OSError("cannot start")

    monkeypatch.setattr(_author, "ClaudeProcess", Process)
    import pytest

    with pytest.raises(OSError):
        await _author._start_session([["claude"]], env={}, timeline=_timeline._Timeline())
    assert capture._handler.stream is None


def test_closed_capture_cannot_reopen_from_late_pty_output(tmp_path):
    capture = _session_log.RawCapture(tmp_path / "raw")
    capture.close()
    capture("late output")
    assert capture._handler.stream is None
