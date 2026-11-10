"""Run a Codex author session as one `codex exec --json` process per turn.

`ClaudeProcess` drives a long-lived TUI on a pty. `codex exec` has no such session: each turn is a
process that prints JSONL events and exits, and the conversation lives in a thread Codex stores under
`$CODEX_HOME`. The first turn starts the thread and reports its id in a `thread.started` event; every
later turn is `codex exec resume <id>`. Keeping `thread_id` readable and settable is what lets a
restarted pod (same `$CODEX_HOME`, id from the checkpoint) carry on in the same conversation.

The attributes the supervisor reads (`tail`, `returncode`, `limit_hit_at`, `limit_text`,
`last_output`, `output_count`) and the methods it calls (`submit`, `wait`, `stop`) keep the names
`ClaudeProcess` gives them. Unlike a TUI session, "no process running" is the normal idle state here,
so `returncode` is set only by a crash, never by a turn that finished.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import signal
import struct
import termios
import time
from collections import deque
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Required, TypedDict, Unpack, cast

from . import _codex_auth, _meter

_CODEX_LIMIT = _meter._CODEX_LIMIT

# `codex exec --json` writes one event per line and a single tool output can be large; asyncio's
# default 64 KiB line limit would raise on it and lose the rest of the turn.
_LINE_LIMIT = 1 << 24


class _Item(TypedDict, total=False):
    type: str
    text: str


class _Error(TypedDict, total=False):
    message: str


class _Event(TypedDict, total=False):
    type: str
    thread_id: str
    item: _Item
    message: str
    error: _Error


class _ProcessOptions(TypedDict, total=False):
    cwd: Required[Path]
    env: Required[dict[str, str]]
    command: Sequence[str]
    args: Sequence[str]
    thread_id: str | None
    on_line: Callable[[str], None] | None
    tail_lines: int


class CodexProcess:
    """A Codex author session: `codex exec --json` per turn, resumed through `thread_id`."""

    def __init__(
        self,
        *,
        cwd: Path,
        env: dict[str, str],
        command: Sequence[str] = ("codex",),
        args: Sequence[str] = (),
        thread_id: str | None = None,
        on_line: Callable[[str], None] | None = None,
        tail_lines: int = 400,
    ) -> None:
        self._command = list(command)
        # Options for `exec` and `exec resume` alike (-m, -c, --dangerously-bypass-...): the caller owns
        # model, effort and sandbox policy, as it does for the Claude argv.
        self._args = list(args)
        self._cwd = cwd
        self._env = env
        self._on_line = on_line
        #: The Codex thread, set from the first turn's `thread.started`. The supervisor checkpoints it
        #: and sets it back after a restart so the next turn resumes instead of starting afresh.
        self.thread_id = thread_id
        self.tail: deque[str] = deque(maxlen=tail_lines)
        #: `time.monotonic()` of the last output line, and the total characters, only ever growing.
        self.last_output: float | None = None
        self.output_count = 0
        #: `time.monotonic()` of the last usage refusal and the message Codex gave with it.
        self.limit_hit_at: float | None = None
        self.limit_text = ""
        #: Why the latest turn crashed when Codex could not authenticate (see `_codex_auth`), else ''.
        #: Retrying the same login cannot help, so the supervisor treats it differently from a crash.
        self.auth_failure = ""
        #: The final `agent_message` of the latest turn, and the Codex meter its events carried.
        self.last_message = ""
        #: Whether the latest turn reported `turn.completed`; a turn without it never finished.
        self.turn_completed = False
        self.meter: _meter.Meter | None = None
        self.last_submission: tuple[str, float, int] | None = None
        self._returncode: int | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._turn: asyncio.Task[None] | None = None
        self._interrupted = False
        self._submit_lock = asyncio.Lock()

    # -- turns -------------------------------------------------------------- #

    @property
    def returncode(self) -> int | None:
        """The exit code of a turn that crashed, else None (running, idle, finished or interrupted)."""
        return self._returncode

    @property
    def running(self) -> bool:
        return self._turn is not None and not self._turn.done()

    def _argv(self, text: str) -> list[str]:
        argv = [*self._command, "exec"]
        if self.thread_id:
            argv.append("resume")
        argv += ["--json", "--skip-git-repo-check", *self._args]
        if self.thread_id:
            argv.append(self.thread_id)
        # After `--`: a prompt starting with "-" would otherwise be read as an option.
        return [*argv, "--", text]

    async def submit(self, text: str, *, label: str = "") -> None:
        """Start a turn with `text`; returns once the process is spawned. `wait()` ends with the turn.

        `label` is accepted for `ClaudeProcess.submit` callers and unused: it names the message in
        the terminal the supervisor shows, and there is none here.
        """
        async with self._submit_lock:
            if self.running:
                raise RuntimeError("a Codex turn is already running")
            self._returncode = None
            self._interrupted = False
            self.auth_failure = ""
            self.last_message = ""
            self.turn_completed = False
            self.last_submission = (text, time.monotonic(), self.output_count)
            self._proc = await asyncio.create_subprocess_exec(
                *self._argv(text),
                cwd=str(self._cwd),
                env=self._env,
                stdin=asyncio.subprocess.DEVNULL,  # it otherwise waits for more input on stdin
                stdout=asyncio.subprocess.PIPE,
                # Codex reports refusals on stdout; stderr is for the tail only.
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,  # so interrupt() reaches whatever the turn's tools spawned
                limit=_LINE_LIMIT,
            )
            self._turn = asyncio.create_task(self._read_turn(self._proc), name="codex-turn")

    async def _read_turn(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        events: list[_Event] = []
        refusal = ""
        auth_failure = ""
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip("\r\n")
            self.tail.append(line)
            self.output_count += len(line) + 1
            self.last_output = time.monotonic()
            if self._on_line:
                self._on_line(line)
            if not line.lstrip().startswith("{"):
                # Codex's own stderr (merged into stdout); events carry tool output and model text.
                if _codex_auth.is_auth_failure(line):
                    auth_failure = line
            else:
                with contextlib.suppress(ValueError):
                    event = json.loads(line)
                    if isinstance(event, dict):
                        # Codex JSON events have these optional fields; other fields belong to the meter.
                        event = cast(_Event, event)
                        events.append(event)
                        self._note(event)
                        auth_failure = self._auth_failure(event) or auth_failure
                        # The meter reads the same event objects without changing them.
                        self.meter = (
                            cast(Callable[[list[_Event]], _meter.Meter | None], _meter.from_events)([event])
                            or self.meter
                        )
                        if not refusal:
                            refusal = self._refusal([event])
                            if refusal:
                                self.limit_hit_at = time.monotonic()
                                self.limit_text = refusal
        code = await proc.wait()
        # Adapt the meter's list[dict] annotation, maintained in another chunk.
        self.meter = (
            cast(Callable[[list[_Event]], _meter.Meter | None], _meter.from_events)(events) or self.meter
        )
        if (
            not refusal
            and not self._interrupted
            and not self.turn_completed
            and (code != 0 or self._turn_failed(events))
        ):
            # A turn that never completed crashed, even after it printed an agent message: treating it as
            # done would replay its prompt at once, again and again, with the supervisor none the wiser.
            self._returncode = code or 1
            self.auth_failure = auth_failure

    @staticmethod
    def _auth_failure(event: _Event) -> str:
        """Return the message of an `error` / `turn.failed` event that says the login is dead, else ''."""
        if event.get("type") not in ("error", "turn.failed"):
            return ""
        message = str(event.get("message") or (event.get("error") or {}).get("message") or "")
        return (
            message if _codex_auth.is_auth_failure(message, final=event.get("type") == "turn.failed") else ""
        )

    @staticmethod
    def _turn_failed(events: list[_Event]) -> bool:
        return any(event.get("type") == "turn.failed" for event in events)

    def _note(self, event: _Event) -> None:
        if event.get("type") == "turn.completed":
            self.turn_completed = True
        if event.get("type") == "thread.started" and event.get("thread_id"):
            self.thread_id = str(event["thread_id"])
        item = event.get("item")
        if event.get("type") == "item.completed" and isinstance(item, dict):
            if item.get("type") == "agent_message":
                self.last_message = str(item.get("text") or "")

    @staticmethod
    def _refusal(events: list[_Event]) -> str:
        """Return the message of an `error` / `turn.failed` event that is a usage refusal, else ''."""
        # As in `agent._run_codex`: a refusal arrives as these events, not on stderr.
        for event in events:
            if event.get("type") in ("error", "turn.failed"):
                message = str(event.get("message") or (event.get("error") or {}).get("message") or "")
                if _CODEX_LIMIT.search(message):
                    return message
        return ""

    # -- teardown ----------------------------------------------------------- #

    async def wait(self) -> int:
        """Wait for the running turn (if any); its exit code, or 0 when no turn has run."""
        if self._turn is not None:
            await self._turn
        return self._proc.returncode or 0 if self._proc is not None else 0

    def interrupt(self) -> None:
        """Stop the running turn. The thread stays resumable; this is not a crash."""
        if not self.running or self._proc is None or self._proc.returncode is not None:
            return
        self._interrupted = True
        self._signal(signal.SIGTERM)

    async def stop(self, *, grace: float = 20.0) -> None:
        """Interrupt the turn, bounding the reader even when a detached tool holds stdout."""
        deadline = time.monotonic() + grace
        try:
            if self.running:
                self.interrupt()
                assert self._turn is not None and self._proc is not None
                # Process.wait() can also wait for inherited pipes, so poll the leader's status.
                while not self._turn.done() and self._proc.returncode is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._signal(signal.SIGKILL)
                        # SIGKILL delivery and asyncio's child watcher are asynchronous too.
                        kill_deadline = time.monotonic() + 0.2
                        while self._proc.returncode is None and time.monotonic() < kill_deadline:
                            await asyncio.sleep(0.01)
                        break
                    await asyncio.wait({self._turn}, timeout=min(0.05, remaining))
                if self._proc.returncode is not None:
                    self._drain_output()
                # Let the normal reader finish parsing and durably acknowledge the completed turn.
                await asyncio.wait({self._turn}, timeout=0.2)
        finally:
            if self._turn is not None and not self._turn.done():
                self._turn.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._turn
            if self._proc is not None:
                # StreamReader has no close API; close just its subprocess pipe transport.
                pipe = self._proc._transport.get_pipe_transport(1)  # type: ignore[attr-defined]  # asyncio exposes pipe cleanup only through its private transport.
                if pipe is not None:
                    pipe.close()

    def _drain_output(self) -> None:
        assert self._proc is not None
        pipe = self._proc._transport.get_pipe_transport(1)  # type: ignore[attr-defined]  # asyncio exposes pipe cleanup only through its private transport.
        if pipe is None or pipe.is_closing():
            return
        assert self._proc.stdout is not None
        # StreamReader has no nonblocking drain API. Snapshot the kernel pipe as well as its
        # buffer: a detached tool may keep writing, but cannot extend this stop indefinitely.
        pipe.pause_reading()
        fd = pipe.get_extra_info("pipe").fileno()
        remaining = struct.unpack("i", fcntl.ioctl(fd, termios.FIONREAD, struct.pack("i", 0)))[0]
        while remaining:
            try:
                data = os.read(fd, min(remaining, 65536))
            except BlockingIOError:
                break
            if not data:
                break
            self._proc.stdout.feed_data(data)
            remaining -= len(data)
        pipe.close()
        # EOF makes readline return a final unterminated event through the same parser.
        self._proc.stdout.feed_eof()

    def _signal(self, sig: signal.Signals) -> None:
        assert self._proc is not None
        try:
            os.killpg(os.getpgid(self._proc.pid), sig)
        except (ProcessLookupError, PermissionError):
            pass


class CodexAuthor(CodexProcess):
    """`CodexProcess` as the project's author session, the way the supervisor drives a Claude one.

    Three differences from the bare process. A message submitted while a turn runs is queued and
    goes out when that turn ends (the Claude TUI takes typed input at any time; `codex exec` takes
    one prompt per process). `thread_id` is kept in `thread_file`, which the caller puts inside the
    checkpointed state next to `$CODEX_HOME`, so a restarted pod resumes the same thread. Pending
    messages are journaled beside it until completion, including across stop and restart. It also has
    the attributes the supervisor reads off a TUI session that a Codex session never has.
    """

    #: No permission menu and no `/login` screen to expire.
    menu: str | None = None
    login_expired = False

    def __init__(self, *, thread_file: Path, **kwargs: Unpack[_ProcessOptions]) -> None:
        self._thread_file = thread_file
        if kwargs.get("thread_id") is None:
            kwargs["thread_id"] = self.saved_thread(thread_file)
        super().__init__(**kwargs)
        self._pending_file = thread_file.with_name(thread_file.name + "-pending.json")
        try:
            self._queued: list[str] = json.loads(self._pending_file.read_text())
        except FileNotFoundError:
            self._queued = []
        self._queue_lock = asyncio.Lock()
        self._delivering = 0
        self._completed = False
        self._stopping = False
        self._flush: asyncio.Task[None] | None = None
        if self._queued:
            # Construction can also happen outside an event loop; submit will drain it then.
            with contextlib.suppress(RuntimeError):
                self._flush = asyncio.get_running_loop().create_task(
                    self._submit_queued(None), name="codex-queue"
                )

    @staticmethod
    def saved_thread(thread_file: Path) -> str | None:
        try:
            return thread_file.read_text().strip() or None
        except OSError:
            return None

    def _note(self, event: _Event) -> None:
        if event.get("type") == "turn.completed":
            self._completed = True
        before = self.thread_id
        super()._note(event)
        if self.thread_id and self.thread_id != before:
            # Replaced, not written in place: a pod lost mid-write must not leave half an id behind.
            self._thread_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._thread_file.with_suffix(".tmp")
            tmp.write_text(self.thread_id)
            tmp.replace(self._thread_file)

    def _save_pending(self) -> None:
        self._pending_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._pending_file.with_suffix(".tmp")
        with tmp.open("w") as stream:
            json.dump(self._queued, stream)
            stream.flush()
            os.fsync(stream.fileno())
        tmp.replace(self._pending_file)
        directory = os.open(self._pending_file.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    async def submit(self, text: str, *, label: str = "") -> None:
        async with self._queue_lock:
            self._queued.append(text)
            self._save_pending()
            self._stopping = False
            if not self.running:
                await self._start_pending(label=label)

    async def _start_pending(self, *, label: str = "") -> None:
        self._delivering = len(self._queued)
        self._completed = False
        text = (
            self._queued[0]
            if len(self._queued) == 1
            else ("Messages that arrived while you were working:\n\n" + "\n\n".join(self._queued))
        )
        await super().submit(text, label=label)

    async def _read_turn(self, proc: asyncio.subprocess.Process) -> None:
        refused_before = self.limit_hit_at
        await super()._read_turn(proc)
        async with self._queue_lock:
            if self._completed:
                # Codex has no idempotency key for exec. A crash after it accepts the prompt but
                # before this durable acknowledgement can replay that turn; retaining it avoids
                # losing directions when the process dies before accepting them.
                del self._queued[: self._delivering]
                self._save_pending()
            if (
                self._queued
                and self.limit_hit_at == refused_before
                and self._returncode is None
                and not self._stopping
                and not self._interrupted
            ):
                self._flush = asyncio.create_task(self._submit_queued(self._turn), name="codex-queue")

    async def _submit_queued(self, finished: asyncio.Task[None] | None) -> None:
        if finished is not None:
            await asyncio.wait({finished})
        async with self._queue_lock:
            if self._queued and not self.running and not self._stopping:
                await self._start_pending()

    async def stop(self, *, grace: float = 20.0) -> None:
        async with self._queue_lock:
            self._stopping = True
            if self._flush is not None:
                self._flush.cancel()
        await super().stop(grace=grace)
