"""CodexProcess against tests/fake_codex.py: turns, resume, interrupt, refusal, crash, bounded tail."""

import asyncio
import sys
import time
from pathlib import Path
from typing import Any, cast

import pytest

from flyte_agent_factory._codex_process import CodexProcess

FAKE = Path(__file__).parent / "fake_codex.py"


def make(tmp_path, *, tail_lines=400, **env) -> CodexProcess:
    return CodexProcess(
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "CODEX_HOME": str(tmp_path / "home"), **env},
        command=(sys.executable, str(FAKE)),
        tail_lines=tail_lines,
    )


def _running(proc: CodexProcess) -> bool:
    """`proc.running` through a call, so mypy does not keep it narrowed to True after `interrupt()`."""
    return proc.running


async def turn(proc: CodexProcess, text: str) -> None:
    await proc.submit(text)
    await asyncio.wait_for(proc.wait(), 30)


async def test_first_turn_starts_a_thread_and_the_second_resumes_it(tmp_path):
    proc = make(tmp_path)
    before = proc.thread_id  # read once: mypy would keep `proc.thread_id` narrowed to None across the turn
    assert before is None
    await turn(proc, "one")
    first = proc.thread_id
    assert first and proc.last_message == f"turn 1 on {first}: one"
    await turn(proc, "two")
    assert proc.thread_id == first
    assert proc.last_message == f"turn 2 on {first}: two"
    assert proc.returncode is None and proc.limit_hit_at is None


async def test_setting_thread_id_resumes_after_a_restart(tmp_path):
    before = make(tmp_path)
    await turn(before, "one")
    # A restarted pod: a new object, the same $CODEX_HOME, the id from the checkpoint.
    after = make(tmp_path)
    after.thread_id = before.thread_id
    await turn(after, "again")
    assert after.thread_id == before.thread_id
    assert after.last_message == f"turn 2 on {before.thread_id}: again"


async def test_an_unknown_thread_is_refused_not_started_afresh(tmp_path):
    proc = make(tmp_path)
    proc.thread_id = "no-such-thread"
    await turn(proc, "hello")
    assert proc.thread_id == "no-such-thread"
    assert proc.last_message == ""
    assert proc.returncode == 1
    assert any("no recorded session" in line for line in proc.tail)


async def test_interrupt_stops_a_long_turn(tmp_path):
    proc = make(tmp_path, FAKE_CODEX_SLEEP="60")
    await proc.submit("slow")
    while proc.thread_id is None:
        await asyncio.sleep(0.05)
    # The fake announces the thread before it records the turn; interrupt only once the turn is recorded.
    recorded = Path(proc._env["CODEX_HOME"]) / "threads" / proc.thread_id
    for _ in range(600):
        if recorded.is_file() and recorded.read_text().strip():
            break
        await asyncio.sleep(0.05)
    assert _running(proc)
    proc.interrupt()
    # The turn sleeps for 60 s: finishing inside 45 s means the interrupt ended it.
    await asyncio.wait_for(proc.wait(), 45)
    assert not proc.running
    # Interrupted is not crashed, and the thread is still there to resume.
    assert proc.returncode is None
    thread = proc.thread_id
    proc._env["FAKE_CODEX_SLEEP"] = "0"
    await turn(proc, "after")
    assert proc.thread_id == thread and proc.last_message.startswith("turn 2")


async def test_a_usage_refusal_sets_limit_hit_at_and_limit_text(tmp_path):
    proc = make(tmp_path, FAKE_CODEX_LIMIT="1")
    before = time.monotonic()
    await turn(proc, "hello")
    assert proc.limit_hit_at is not None and proc.limit_hit_at >= before
    assert "usage limit" in proc.limit_text
    assert proc.returncode is None  # a refusal is not a crash
    assert proc.meter is not None and proc.meter.five_hour_pct == 100.0


async def test_a_crash_sets_returncode(tmp_path):
    proc = make(tmp_path, FAKE_CODEX_CRASH="1")
    await turn(proc, "hello")
    assert proc.returncode == 3
    assert proc.limit_hit_at is None
    assert any("fake codex crashed" in line for line in proc.tail)


async def test_the_tail_is_bounded(tmp_path):
    proc = make(tmp_path, tail_lines=10, FAKE_CODEX_LINES="50")
    await turn(proc, "hello")
    assert len(proc.tail) == 10
    assert "noise 0" not in proc.tail
    assert "turn.completed" in proc.tail[-1]


async def test_a_second_turn_cannot_start_while_one_runs(tmp_path):
    proc = make(tmp_path, FAKE_CODEX_SLEEP="60")
    await proc.submit("slow")
    with pytest.raises(RuntimeError, match="already running"):
        await proc.submit("again")
    await proc.stop(grace=5)
    assert not proc.running


async def test_concurrent_process_submissions_cannot_start_two_turns(tmp_path):
    proc = make(tmp_path, FAKE_CODEX_SLEEP="0.2")
    results = await asyncio.gather(proc.submit("first"), proc.submit("second"), return_exceptions=True)
    await proc.wait()
    assert sum(isinstance(result, RuntimeError) for result in results) == 1
    assert len(list((tmp_path / "home" / "threads").iterdir())) == 1


async def test_a_turn_that_fails_after_speaking_is_a_crash(tmp_path):
    proc = make(tmp_path, FAKE_CODEX_FAIL_AFTER_MESSAGE="1")
    await turn(proc, "hello")
    assert proc.last_message and not proc.turn_completed
    assert proc.returncode == 3 and proc.limit_hit_at is None


async def test_stop_skips_a_reused_exited_leaders_group(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace

    proc = make(tmp_path)
    exited = SimpleNamespace(
        pid=40001, returncode=0, _transport=SimpleNamespace(get_pipe_transport=lambda fd: None)
    )
    monkeypatch.setattr(proc, "_proc", exited)
    proc._turn = asyncio.create_task(asyncio.sleep(0.1))
    sent = []
    monkeypatch.setattr(os, "getpgid", lambda pid: 49999)
    monkeypatch.setattr(os, "killpg", lambda group, sig: sent.append((group, sig)))
    await proc.stop(grace=0)
    assert sent == []


async def test_stop_is_bounded_when_a_detached_child_holds_stdout(tmp_path):
    import os
    import signal

    pidfile = tmp_path / "stdout.pid"
    proc = make(tmp_path, FAKE_CODEX_LIMIT="1", FAKE_CODEX_BG_STDOUT=str(pidfile))
    child_fd = None
    try:
        await proc.submit("launch")
        async with asyncio.timeout(30):
            while not pidfile.exists():
                await asyncio.sleep(0.02)
        child_fd = os.pidfd_open(int(pidfile.read_text()))
        async with asyncio.timeout(30):
            await proc.stop(grace=0.1)
        assert not proc.running
        assert cast(Any, proc._proc)._transport.get_pipe_transport(1).is_closing()  # asyncio internals
        signal.pidfd_send_signal(child_fd, 0)  # an ordinary stop preserves the detached service
    finally:
        if child_fd is not None:
            signal.pidfd_send_signal(child_fd, signal.SIGKILL)
            os.close(child_fd)
        await proc.stop(grace=0.1)


async def test_streamed_refusal_keeps_its_identity_when_stdout_closes(tmp_path):
    import os
    import signal

    pidfile = tmp_path / "stdout.pid"
    proc = make(tmp_path, FAKE_CODEX_LIMIT="1", FAKE_CODEX_BG_STDOUT=str(pidfile))
    child_fd = None
    try:
        await proc.submit("launch")
        async with asyncio.timeout(30):
            while not pidfile.exists() or proc.limit_hit_at is None:
                await asyncio.sleep(0.02)
        child_fd = os.pidfd_open(int(pidfile.read_text()))
        hit = proc.limit_hit_at
        signal.pidfd_send_signal(child_fd, signal.SIGKILL)
        os.close(child_fd)
        child_fd = None
        await asyncio.wait_for(proc.wait(), 30)
        assert proc.limit_hit_at == hit
    finally:
        if child_fd is not None:
            signal.pidfd_send_signal(child_fd, signal.SIGKILL)
            os.close(child_fd)
        await proc.stop(grace=0.1)
