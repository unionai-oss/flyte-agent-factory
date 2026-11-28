"""The structured author session: `claude -p` with stream-json in and out (`_stream_process`).

The fake below stands in for Claude Code's side of the protocol. It is deliberately small -- what is being
tested is this package's side of the pipes: what reaches the process, and what its events do to the state
the supervisor reads. Each test scripts it with a scenario file (turns of steps), and the fake logs every
line it receives, so a test can assert on the exact bytes that arrived.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import types
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from flyte_agent_factory import _author, _permission_policy, _stream_process
from flyte_agent_factory._stream_process import StreamClaudeProcess
from tests._wait import wait_for

FAKE_CLAUDE = r"""
import json, os, select, sys, time

scenario = json.load(open(sys.argv[1]))
log = open(scenario["log"], "a")
start = scenario.get("start") or {}
if "exit" in start:
    # As a refused `--resume` does: a while loading, an error `result` (no `init`), the reason, the exit.
    time.sleep(start.get("delay", 0))
    if "result" in start:
        sys.stdout.write(json.dumps({"type": "result", "is_error": True, **start["result"]}) + "\n")
        sys.stdout.flush()
        time.sleep(start.get("after_result", 0))
    sys.stderr.write(start.get("stderr", ""))
    sys.stderr.flush()
    sys.exit(start["exit"])

session = scenario.get("session_id", "sess-1")
fd = sys.stdin.fileno()
buf = b""
pending = []
eof = False


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def read_lines(timeout):
    global buf, eof
    if eof:
        time.sleep(timeout)
        return
    ready, _, _ = select.select([fd], [], [], timeout)
    if not ready:
        return
    chunk = os.read(fd, 1 << 16)
    if not chunk:
        eof = True
        return
    buf += chunk
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        log.write(line.decode() + "\n")
        log.flush()
        pending.append(json.loads(line))


def ack(msg):
    emit({"type": "control_response",
          "response": {"subtype": "success", "request_id": msg["request_id"], "response": {}}})


def take_interrupt():
    for msg in list(pending):
        if msg.get("type") == "control_request" and msg["request"]["subtype"] == "interrupt":
            pending.remove(msg)
            ack(msg)
            return True
    return False


def run_turn(spec, msg, asks):
    interrupted = False
    for step in spec.get("steps", []):
        if "emit" in step:
            emit(step["emit"])
        elif "sleep" in step:
            end = time.time() + step["sleep"]
            while time.time() < end and not interrupted:
                read_lines(0.05)
                interrupted = take_interrupt()
        elif "ask" in step:
            asks[0] += 1
            rid = "ask-%d" % asks[0]
            emit({"type": "control_request", "request_id": rid, "request": {
                "subtype": "can_use_tool", "tool_name": step["ask"], "input": step.get("input", {})}})
            while not eof:
                found = [m for m in pending if m.get("type") == "control_response"
                         and m["response"].get("request_id") == rid]
                if found:
                    pending.remove(found[0])
                    break
                read_lines(0.05)
        if interrupted:
            break
    text = msg["message"]["content"]
    emit({"type": "assistant", "session_id": session,
          "message": {"content": [{"type": "text", "text": "echo: " + text.splitlines()[0]}]}})
    result = {"type": "result", "subtype": "error_during_execution" if interrupted else "success",
              "is_error": interrupted, "num_turns": 1, "total_cost_usd": 0.01, "queued_turn_count": 0,
              "usage": {"input_tokens": 10, "output_tokens": 5}, "session_id": session, "result": "ok"}
    result.update(spec.get("result", {}))
    emit(result)


turns = scenario.get("turns", [])
turn = 0
asks = [0]
while True:
    while not pending and not eof:
        read_lines(0.05)
    if not pending:
        break
    msg = pending.pop(0)
    if msg.get("type") == "control_request":
        ack(msg)
        if msg["request"]["subtype"] == "initialize":
            emit({"type": "system", "subtype": "init", "session_id": session, "model": "fake-model"})
    elif msg.get("type") == "user":
        run_turn(turns[turn] if turn < len(turns) else {}, msg, asks)
        turn += 1
if scenario.get("ignore_eof"):
    time.sleep(300)
"""

Launch = Callable[..., Awaitable[tuple[StreamClaudeProcess, Callable[[], list[dict[str, Any]]]]]]


@pytest.fixture
async def launch(tmp_path: Path) -> AsyncIterator[Launch]:
    """Start the fake with a scenario; return the process and a reader of what the fake received."""
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE)
    started: list[StreamClaudeProcess] = []

    async def start(
        scenario: dict[str, Any] | None = None, **kwargs: Any
    ) -> tuple[StreamClaudeProcess, Callable[[], list[dict[str, Any]]]]:
        index = len(started)
        log = tmp_path / f"received-{index}.jsonl"
        log.touch()
        path = tmp_path / f"scenario-{index}.json"
        path.write_text(json.dumps({**(scenario or {}), "log": str(log)}))
        proc = StreamClaudeProcess(
            [sys.executable, str(script), str(path)],
            cwd=tmp_path,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            **kwargs,
        )
        started.append(proc)
        await proc.start()

        def received() -> list[dict[str, Any]]:
            return [json.loads(line) for line in log.read_text().splitlines()]

        return proc, received

    yield start
    for proc in started:
        await proc.stop(grace=2.0)


def _user_texts(received: list[dict[str, Any]]) -> list[str]:
    return [m["message"]["content"] for m in received if m.get("type") == "user"]


# -- input ------------------------------------------------------------------ #


async def test_long_multiline_message_arrives_whole(launch: Launch) -> None:
    text = (
        "Direction from the owner:\n\n" + "\n".join(f"line {i}: " + "x" * 40 for i in range(12)) + "\n\tend"
    )
    assert len(text) > 400 and text.count("\n") > 10
    proc, received = await launch()
    await proc.submit(text)
    await wait_for(lambda: proc.results >= 1, what="the turn's result")

    # One user message, byte-identical: not torn at the newlines, not swapped for an inbox-file pointer.
    assert _user_texts(received()) == [text]
    assert proc.sent_texts[-1] == text
    assert proc.last_submission is not None and proc.last_submission[0] == text
    assert not (proc._cwd / ".claude-flyte" / "inbox").exists()


async def test_message_sent_mid_turn_is_answered_as_the_next_turn(launch: Launch) -> None:
    proc, received = await launch({"turns": [{"steps": [{"sleep": 1.0}]}]})
    await proc.submit("first")
    await wait_for(lambda: _user_texts(received()) == ["first"], what="the first message")
    assert proc.busy
    await proc.submit("second, while the first runs")
    assert proc.results == 0, "the slow turn ended before the mid-turn message was sent"

    await wait_for(lambda: proc.results >= 2, what="both results")
    assert _user_texts(received()) == ["first", "second, while the first runs"]
    assert any(line == "echo: second, while the first runs" for line in proc.tail)
    assert not proc.busy


async def test_permission_requests_are_decided_by_the_policy(launch: Launch) -> None:
    notices: list[tuple[str, str]] = []
    steps = [{"ask": "Bash", "input": {"command": f"rm -rf /tmp/x{i}"}} for i in range(3)]
    steps.append({"ask": "Read", "input": {"file_path": "/etc/hostname"}})
    proc, received = await launch(
        {"turns": [{"steps": steps}]},
        on_notice=lambda emoji, text: notices.append((emoji, text)),
        policy=_permission_policy.Policy(allow=frozenset({"Read"})),
    )
    await proc.submit("go")
    await wait_for(lambda: proc.results >= 1, what="the turn's result")

    answers = {
        m["response"]["request_id"]: m["response"]
        for m in received()
        if m.get("type") == "control_response" and m["response"]["request_id"].startswith("ask-")
    }
    assert set(answers) == {"ask-1", "ask-2", "ask-3", "ask-4"}
    for rid in ("ask-1", "ask-2", "ask-3"):
        assert answers[rid]["subtype"] == "success"
        decision = answers[rid]["response"]
        assert decision["behavior"] == "deny"
        assert "nobody can approve" in decision["message"]
    assert "interrupt" not in answers["ask-1"]["response"]
    assert "interrupt" not in answers["ask-2"]["response"]
    assert answers["ask-3"]["response"]["interrupt"] is True
    assert "denied 3 times" in answers["ask-3"]["response"]["message"]
    assert answers["ask-4"]["response"] == {
        "behavior": "allow",
        "updatedInput": {"file_path": "/etc/hostname"},
    }

    assert [tool for _, tool in proc.permission_denials] == ["Bash"] * 3
    assert notices == [("🚫", "Bash denied by the factory's permission policy")] * 3


async def test_unknown_control_request_gets_an_error_response(monkeypatch: pytest.MonkeyPatch) -> None:
    proc, sent = _detached(monkeypatch)
    await proc.handle(
        {"type": "control_request", "request_id": "r1", "request": {"subtype": "hook_callback"}}
    )
    assert sent == [
        {
            "type": "control_response",
            "response": {
                "subtype": "error",
                "request_id": "r1",
                "error": "unsupported control request: hook_callback",
            },
        }
    ]


# -- detached: events applied with no process ---------------------------------- #


class _Clock:
    """Stands in for the module's `time`, so liveness and episodes do not depend on a loaded machine."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.wall = time.time()

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.wall + self.now


def _detached(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock | None = None, **kwargs: Any
) -> tuple[StreamClaudeProcess, list[dict[str, Any]]]:
    """Return a session with no process whose writes are collected instead of sent."""
    if clock is not None:
        monkeypatch.setattr(
            _stream_process, "time", types.SimpleNamespace(monotonic=clock.monotonic, time=clock.time)
        )
    proc = StreamClaudeProcess(["claude"], cwd=Path("."), env={}, **kwargs)
    sent: list[dict[str, Any]] = []

    async def send(obj: dict[str, Any]) -> bool:
        sent.append(obj)
        return True

    monkeypatch.setattr(proc, "_send", send)
    return proc, sent


def _rejected(resets_at: float, kind: str = "five_hour") -> dict[str, Any]:
    return {
        "type": "rate_limit_event",
        "rate_limit_info": {"status": "rejected", "resetsAt": resets_at, "rateLimitType": kind},
    }


async def test_rejected_rate_limit_records_a_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    proc, _ = _detached(monkeypatch, clock)
    reset = 1794994800
    await proc.handle(_rejected(reset))
    assert proc.limit_hit_at == clock.now
    assert proc.limit_seen_wall == clock.time()
    assert proc.limit_reset_at == reset
    assert proc.limit_text == "resets 2026-11-18 09:40 UTC (five_hour)"
    assert proc.rate_limits["five_hour"]["status"] == "rejected"


async def test_reset_in_milliseconds_is_scaled(monkeypatch: pytest.MonkeyPatch) -> None:
    proc, _ = _detached(monkeypatch)
    await proc.handle(_rejected(1794994800_000, "seven_day"))
    assert proc.limit_reset_at == 1794994800
    assert proc.limit_text.endswith("(seven_day)")


async def test_allowed_rate_limit_records_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    proc, _ = _detached(monkeypatch)
    await proc.handle(
        {
            "type": "rate_limit_event",
            "rate_limit_info": {"status": "allowed", "resetsAt": 1794994800, "rateLimitType": "five_hour"},
        }
    )
    assert (proc.limit_hit_at, proc.limit_seen_wall, proc.limit_reset_at, proc.limit_text) == (
        None,
        None,
        None,
        "",
    )
    assert proc.rate_limits["five_hour"]["status"] == "allowed"


async def test_a_repeated_refusal_is_one_episode_until_something_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    proc, _ = _detached(monkeypatch, clock)
    reset = clock.time() + 3600
    await proc.handle(_rejected(reset))
    first = proc.limit_hit_at

    clock.now += 60
    await proc.handle(_rejected(reset))
    # The assistant's own refusal right after the event is the same episode too.
    await proc.handle({"type": "assistant", "error": "rate_limit", "message": {"content": []}})
    assert proc.limit_hit_at == first

    clock.now += 60
    await proc.submit("try again")
    await proc.handle(_rejected(reset))
    assert proc.limit_hit_at == clock.now != first


async def test_rate_limit_error_without_an_event_still_records_a_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc, _ = _detached(monkeypatch)
    await proc.handle(
        {
            "type": "assistant",
            "error": "rate_limit",
            "message": {"content": [{"type": "text", "text": "You've hit your limit"}]},
        }
    )
    assert proc.limit_hit_at is not None
    assert proc.limit_reset_at is None
    assert proc.limit_text == "You've hit your limit"


async def test_rate_limit_error_takes_a_known_rejected_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    proc, _ = _detached(monkeypatch, clock)
    reset = clock.time() + 7200
    # An earlier window's rejection is known; the assistant error then names no reset of its own.
    proc.rate_limits["seven_day"] = {"status": "rejected", "resetsAt": reset}
    await proc.handle({"type": "assistant", "error": "rate_limit", "message": {"content": []}})
    assert proc.limit_reset_at == reset


@pytest.mark.parametrize(
    "event",
    [
        {"type": "assistant", "error": "authentication_failed", "message": {"content": []}},
        {"type": "system", "subtype": "api_retry", "error": "authentication_failed", "attempt": 1},
    ],
    ids=["assistant-error", "api-retry"],
)
async def test_auth_error_sets_login_expired(monkeypatch: pytest.MonkeyPatch, event: dict[str, Any]) -> None:
    proc, _ = _detached(monkeypatch)
    await proc.handle(event)
    assert proc.login_expired
    assert any("setup-token no longer works" in line for line in proc.tail)


async def test_other_api_retry_is_not_an_expired_login(monkeypatch: pytest.MonkeyPatch) -> None:
    proc, _ = _detached(monkeypatch)
    await proc.handle({"type": "system", "subtype": "api_retry", "error": "overloaded", "attempt": 1})
    assert not proc.login_expired


async def test_results_are_summed_and_recorded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ledger = tmp_path / "state" / "author-turns.jsonl"
    proc, _ = _detached(monkeypatch, ledger=ledger)
    await proc.handle({"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-test"})
    usage_a = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100}
    usage_b = {"input_tokens": 1, "output_tokens": 2, "cache_creation_input_tokens": 7}
    await proc.handle(
        {
            "type": "result",
            "subtype": "success",
            "uuid": "u1",
            "num_turns": 2,
            "total_cost_usd": 0.02,
            "usage": usage_a,
            "session_id": "s1",
            "model_usage_since_last_result": {"claude-test": {"outputTokens": 5}},
        }
    )
    await proc.handle(
        {
            "type": "result",
            "subtype": "error_max_turns",
            "is_error": True,
            "uuid": "u2",
            "num_turns": 3,
            "total_cost_usd": 0.05,
            "usage": usage_b,
        }
    )
    assert proc.turns == 5 and proc.results == 2 and proc.errors == 1
    assert proc.usage == {
        "input_tokens": 11,
        "output_tokens": 7,
        "cache_read_input_tokens": 100,
        "cache_creation_input_tokens": 7,
    }
    # The cumulative figure: the latest, not a sum.
    assert proc.cost_usd == 0.05

    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert len(rows) == 2
    first, second = rows
    assert set(first) == {
        "at",
        "session_id",
        "model",
        "uuid",
        "subtype",
        "is_error",
        "num_turns",
        "total_cost_usd",
        "usage",
        "modelUsage",
    }
    assert first["session_id"] == "s1" and first["model"] == "claude-test" and first["uuid"] == "u1"
    assert first["is_error"] is False and first["num_turns"] == 2 and first["total_cost_usd"] == 0.02
    assert first["usage"]["input_tokens"] == 10 and first["usage"]["cache_read_input_tokens"] == 100
    assert first["modelUsage"] == {"claude-test": {"outputTokens": 5}}
    # A result without its own session id is the init's session.
    assert (
        second["session_id"] == "s1" and second["is_error"] is True and second["subtype"] == "error_max_turns"
    )
    assert second["usage"]["cache_creation_input_tokens"] == 7


async def test_queued_turn_count_caps_what_is_outstanding(monkeypatch: pytest.MonkeyPatch) -> None:
    proc, _ = _detached(monkeypatch)
    for text in ("a", "b", "c"):
        await proc.submit(text)
    assert proc._outstanding == 3
    # Claude Code answered the queued messages together: one result, nothing left queued.
    await proc.handle({"type": "result", "subtype": "success", "queued_turn_count": 0})
    assert (proc._outstanding, proc.busy) == (0, False)

    for text in ("d", "e", "f"):
        await proc.submit(text)
    await proc.handle({"type": "result", "subtype": "success", "queued_turn_count": 1})
    assert (proc._outstanding, proc.busy) == (1, True)
    # Without the count, a result ends one message.
    await proc.handle({"type": "result", "subtype": "success"})
    assert (proc._outstanding, proc.busy) == (0, False)


async def test_events_with_nothing_sent_are_an_autonomous_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    proc, _ = _detached(monkeypatch)
    await proc.handle({"type": "assistant", "message": {"content": [{"type": "text", "text": "task done"}]}})
    assert proc.busy
    await proc.handle({"type": "result", "subtype": "success"})
    assert not proc.busy


async def test_liveness_while_busy_is_capped_by_silence(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(_stream_process, "BUSY_SILENCE_S", 60.0)
    proc, _ = _detached(monkeypatch, clock)
    await proc.handle({"type": "system", "subtype": "init", "session_id": "s1"})
    event_at = clock.now

    clock.now += 10
    await proc.submit("run the suite")
    clock.now += 50
    assert proc.last_output == clock.now  # busy, quiet for 50s since the submission: working

    clock.now += 11
    assert proc.last_output == event_at  # 61s of nothing since the submission: silence

    await proc.handle({"type": "assistant", "message": {"content": [{"type": "text", "text": "still here"}]}})
    clock.now += 30
    assert proc.last_output == clock.now  # a new event restarts the window

    await proc.handle({"type": "result", "subtype": "success"})
    done_at = clock.now
    clock.now += 5
    assert proc.last_output == done_at  # idle: the time of the last event


async def test_keep_alive_and_control_responses_are_not_output(monkeypatch: pytest.MonkeyPatch) -> None:
    proc, _ = _detached(monkeypatch)
    await proc.handle({"type": "keep_alive"}, size=20)
    await proc.handle({"type": "control_response", "response": {"subtype": "success"}}, size=20)
    assert proc.output_count == 0 and proc.last_output is None
    await proc.handle({"type": "system", "subtype": "init"}, size=20)
    assert proc.output_count == 20 and proc.reached_prompt


# -- liveness, stop and exit on a real process --------------------------------- #


async def test_last_output_is_now_while_a_silent_turn_runs(launch: Launch) -> None:
    proc, received = await launch({"turns": [{"steps": [{"sleep": 3.0}]}]})
    await wait_for(lambda: proc.reached_prompt, what="system/init")
    await proc.submit("a long tool call")
    await wait_for(lambda: _user_texts(received()), what="the message")
    quiet_from = proc._last_event
    assert quiet_from is not None
    await wait_for(lambda: time.monotonic() - quiet_from > 0.5, what="half a second of silence")
    asked_at = time.monotonic()
    if proc.results == 0:  # still inside the fake's three silent seconds
        last = proc.last_output
        assert last is not None and last >= asked_at

    await wait_for(lambda: proc.results >= 1, what="the turn's result")
    ended = proc._last_event
    assert proc.last_output == ended
    await wait_for(lambda: time.monotonic() - (ended or 0) > 0.2, what="time to pass")
    assert proc.last_output == ended


async def test_stop_mid_turn_interrupts_and_closes_stdin(launch: Launch) -> None:
    proc, received = await launch({"turns": [{"steps": [{"sleep": 60.0}]}]})
    await proc.submit("a very long turn")
    await wait_for(lambda: _user_texts(received()), what="the message")
    started = time.monotonic()
    await proc.stop(grace=20.0)

    interrupts = [m for m in received() if m.get("request", {}).get("subtype") == "interrupt"]
    assert len(interrupts) == 1
    # It exited on end of input, well before the grace ran out and a signal was needed.
    assert proc.returncode == 0
    assert time.monotonic() - started < 20.0


async def test_stop_kills_a_session_that_ignores_end_of_input(launch: Launch) -> None:
    proc, _ = await launch({"ignore_eof": True})
    await wait_for(lambda: proc.reached_prompt, what="system/init")
    await proc.stop(grace=0.5)
    assert proc.returncode is not None and proc.returncode < 0


async def test_a_bad_resume_exits_with_its_reason_in_the_tail(launch: Launch) -> None:
    proc, _ = await launch({"start": {"exit": 1, "stderr": "No conversation found with session ID: x\n"}})
    assert await proc.wait() == 1
    assert proc.returncode == 1
    assert not proc.reached_prompt
    assert "No conversation found with session ID: x" in proc.tail
    assert _author._RECOVERABLE_START.search("\n".join(proc.tail))


async def test_headless_start_check_raises_with_the_tail(launch: Launch) -> None:
    proc, _ = await launch({"start": {"exit": 1, "stderr": "No conversation found with session ID: x\n"}})
    # `returncode` is set before stderr is drained; the check reads to the end itself
    # (`test_headless_start_check_reads_the_output_to_its_end_before_judging`), and this waits as well.
    await proc.wait()
    with pytest.raises(RuntimeError) as raised:
        await _author._check_headless_start(proc)
    # `_start_session` retries without the resume flag on exactly this.
    assert _author._RECOVERABLE_START.search(str(raised.value))
    assert "before reaching its input prompt" in str(raised.value)


async def test_a_resume_refused_after_the_tui_window_is_still_caught(
    launch: Launch, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The live CLI took up to 4.9s to refuse a resume; the stream session is watched longer than the TUI.
    monkeypatch.setattr(_author, "_HEADLESS_START_CHECK_S", 0.5)
    monkeypatch.setattr(_author, "_STREAM_START_CHECK_S", 60.0)
    reason = "No conversation found with session ID: x\n"
    proc, _ = await launch({"start": {"delay": 1.5, "exit": 1, "stderr": reason}})
    with pytest.raises(RuntimeError) as raised:
        await _author._check_headless_start(proc)
    assert _author._RECOVERABLE_START.search(str(raised.value))


async def test_a_result_before_init_ends_the_start_check_without_the_full_wait(
    launch: Launch, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_author, "_STREAM_START_CHECK_S", 60.0)
    reason = "No conversation found with session ID: x\n"
    start = {"exit": 1, "stderr": reason, "result": {"subtype": "error_during_execution"}, "after_result": 1}
    proc, _ = await launch({"start": start})
    began = time.monotonic()
    with pytest.raises(RuntimeError) as raised:
        await _author._check_headless_start(proc)
    assert time.monotonic() - began < 30
    assert proc.results == 1 and not proc.reached_prompt
    assert _author._RECOVERABLE_START.search(str(raised.value))


async def test_a_session_that_stays_up_passes_the_start_check(
    launch: Launch, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_author, "_STREAM_START_CHECK_S", 1.0)
    proc, _ = await launch()
    await _author._check_headless_start(proc)
    assert proc.returncode is None


async def test_events_that_cannot_be_handled_do_not_end_the_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    proc, _ = _detached(monkeypatch)
    reader = asyncio.StreamReader(limit=_stream_process._LINE_LIMIT)
    reader.feed_data(b'{"type": "result", "num_turns": 1e400}\n')  # int(inf): OverflowError
    reader.feed_data(b"[" * 100_000 + b"\n")  # nested past the parser's recursion limit
    reader.feed_data(b'{"type": "result", "subtype": "success", "num_turns": 2}\n')
    reader.feed_eof()
    await proc._read_events(reader)
    assert proc.last_result is not None and proc.last_result.get("num_turns") == 2
    assert any("could not handle a result event" in line for line in proc.tail)


@pytest.mark.parametrize("request_body", ["not-a-dict", None, ["x"]])
async def test_a_malformed_control_request_is_still_answered(
    monkeypatch: pytest.MonkeyPatch, request_body: Any
) -> None:
    proc, sent = _detached(monkeypatch)
    await proc.handle({"type": "control_request", "request_id": "r9", "request": request_body})
    assert [m["response"]["subtype"] for m in sent] == ["error"]
    assert sent[0]["response"]["request_id"] == "r9"


async def test_submit_to_an_exited_session_returns_false() -> None:
    proc = StreamClaudeProcess([sys.executable, "-c", "pass"], cwd=Path("."), env={})
    await proc.start()
    await proc.wait()
    assert await proc.submit("x") is False
    assert list(proc.sent_texts) == []
    assert proc._outstanding == 0


async def test_result_reports_hook_denials_without_double_counting(monkeypatch: pytest.MonkeyPatch) -> None:
    notices: list[tuple[str, str]] = []
    proc, _ = _detached(monkeypatch, on_notice=lambda emoji, text: notices.append((emoji, text)))
    await proc.handle(
        {"type": "system", "subtype": "permission_denied", "tool_name": "Bash", "tool_use_id": "seen"}
    )
    await proc.handle(
        {
            "type": "result",
            "subtype": "success",
            "permission_denials": [
                {"tool_name": "Bash", "tool_use_id": "seen", "tool_input": {"command": "rm x"}},
                {"tool_name": "Read", "tool_use_id": "hook", "tool_input": {"file_path": "private"}},
            ],
        }
    )
    assert [tool for _, tool in proc.permission_denials] == ["Bash", "Read"]
    assert notices == [
        ("🚫", "Bash blocked by a deny rule or hook"),
        ("🚫", "Read blocked by a deny rule or hook"),
    ]
    assert not any("private" in line or "rm x" in line for line in proc.tail)


async def test_policy_denial_in_result_is_not_counted_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    proc, _ = _detached(monkeypatch)
    await proc.handle(
        {
            "type": "control_request",
            "request_id": "r",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "Bash",
                "tool_use_id": "call-1",
                "input": {"command": "rm -rf /tmp/example"},
            },
        }
    )
    await proc.handle(
        {
            "type": "result",
            "permission_denials": [
                {"tool_name": "Bash", "tool_use_id": "call-1"},
            ],
        }
    )
    assert [tool for _, tool in proc.permission_denials] == ["Bash"]


async def test_policy_and_system_events_for_same_denial_count_once(monkeypatch: pytest.MonkeyPatch) -> None:
    proc, _ = _detached(monkeypatch)
    await proc.handle(
        {
            "type": "control_request",
            "request_id": "r",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "Bash",
                "tool_use_id": "same",
                "input": {"command": "rm -rf /tmp/example"},
            },
        }
    )
    await proc.handle(
        {"type": "system", "subtype": "permission_denied", "tool_name": "Bash", "tool_use_id": "same"}
    )
    await proc.handle(
        {"type": "result", "permission_denials": [{"tool_name": "Bash", "tool_use_id": "same"}]}
    )
    assert [tool for _, tool in proc.permission_denials] == ["Bash"]


async def test_result_denial_name_cannot_inject_log_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    notices: list[tuple[str, str]] = []
    proc, _ = _detached(monkeypatch, on_notice=lambda emoji, text: notices.append((emoji, text)))
    await proc.handle(
        {"type": "result", "permission_denials": [{"tool_name": "Read\nforged", "tool_use_id": "x"}]}
    )
    assert [tool for _, tool in proc.permission_denials] == ["Readforged"]
    assert all("\n" not in text for _, text in notices)
    assert all("\n" not in line for line in proc.tail)


@pytest.mark.parametrize("callback", ["on_line", "on_notice"])
async def test_result_denial_reporting_failure_does_not_lose_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, callback: str
) -> None:
    def fail(*args: Any) -> None:
        if callback == "on_notice" or args[0].startswith("[permission]"):
            raise OSError("report unavailable")

    if callback == "on_line":
        proc, _ = _detached(monkeypatch, ledger=tmp_path / "ledger", on_line=fail)
    else:
        proc, _ = _detached(monkeypatch, ledger=tmp_path / "ledger", on_notice=fail)
    proc._outstanding = 1
    await proc.handle(
        {
            "type": "result",
            "subtype": "success",
            "num_turns": 2,
            "usage": {"output_tokens": 7},
            "permission_denials": [{"tool_name": "Read", "tool_use_id": "x"}],
        }
    )
    assert proc.results == 1 and proc.turns == 2 and not proc.busy
    assert proc.usage["output_tokens"] == 7
    assert [tool for _, tool in proc.permission_denials] == ["Read"]
    assert len((tmp_path / "ledger").read_text().splitlines()) == 1
    assert not proc._denied_tool_ids
