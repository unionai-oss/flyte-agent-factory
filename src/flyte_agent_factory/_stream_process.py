"""Run the Claude author session headless: `claude -p` with stream-json in and out, on pipes.

`ClaudeProcess` drives the interactive TUI on a pty and reads its screen. This drives the same binary,
with the same setup-token, as one long-lived process that takes user messages as JSON lines on stdin and
reports everything as JSON events on stdout (docs/headless-author-discovery.md):

- direction is a `user` message, of any length and with its newlines, at any time: one written while a
  turn runs is queued by Claude Code and answered next;
- a tool call that needs approval arrives as a `can_use_tool` control request, decided in code by
  `_permission_policy` (`--permission-prompt-tool stdio`);
- a usage limit is a `rate_limit_event` with status `rejected` and `resetsAt` in Unix seconds, an expired
  login an `authentication_failed` error, a finished turn a `result` with its cost, tokens and turns.

The attributes the supervisor reads keep the names `ClaudeProcess` and `CodexAuthor` give them (`tail`,
`returncode`, `limit_hit_at`, `limit_seen_wall`, `limit_text`, `last_output`, `output_count`,
`last_submission`, `sent_texts`, `login_expired`, `menu`), and `limit_reset_at` adds the reset as a
number so nothing parses notice text. `last_output` reads "now" while a turn is in flight and the time of
the last event otherwise: a turn that runs a 20-minute test suite emits nothing meanwhile, and it is busy.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import time
import uuid
from collections import deque
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from . import _meter, _permission_policy, _unprivileged
from ._auth import token_fd

#: Flags that make `claude` the structured session. `--verbose` is required with stream-json output;
#: `--permission-prompt-tool stdio` sends approvals here as control requests instead of denying them.
STREAM_FLAGS = (
    "-p",
    "--input-format",
    "stream-json",
    "--output-format",
    "stream-json",
    "--verbose",
    "--permission-prompt-tool",
    "stdio",
    "--include-hook-events",
)

# One event per line, and a single tool result can be large: asyncio's 64 KiB default would raise and
# lose the rest of the session.
_LINE_LIMIT = 1 << 26

#: `assistant.error` / `system/api_retry.error` values that mean the credential no longer works.
_AUTH_ERRORS = frozenset({"authentication_failed", "oauth_org_not_allowed", "account_on_hold"})

#: What handling one event can raise: an event whose fields are not the shape expected (Claude Code's
#: schema is not ours), or a write that fails (the ledger, the session log, a control response). Narrowed
#: rather than `Exception` so a bug in this module still surfaces in tests instead of becoming a log line.
_EVENT_ERRORS = (AttributeError, LookupError, TypeError, ValueError, ArithmeticError, RecursionError, OSError)

#: How long a turn in flight may send nothing and still count as working. A long tool call (a test suite, a
#: build) sends nothing until it ends; an hour of nothing is a hang.
BUSY_SILENCE_S = 3600.0

#: Callback for a timeline row: (emoji, text).
Notice = Callable[[str, str], None]


def _describe_reset(at: float | None, kind: str = "") -> str:
    """Return a readable reset for the report, e.g. `resets 2026-11-18 10:00 UTC (five_hour)`."""
    if at is None:
        return f"reset time not reported ({kind})" if kind else ""
    when = datetime.fromtimestamp(at, UTC).strftime("%Y-%m-%d %H:%M UTC")
    return f"resets {when}" + (f" ({kind})" if kind else "")


class StreamClaudeProcess:
    """A Claude Code `-p` stream-json process with structured state for the supervisor."""

    #: Never set: approvals are control requests answered in code, so nothing can be "open on screen".
    menu: str | None = None
    #: Read by the inbox through `AuthorSession.__getattr__`: a stream-json message takes any text, so
    #: direction goes whole instead of as a pointer to its file.
    accepts_whole_messages: ClassVar[bool] = True
    #: There is no input box; the session is ready once it reported `system/init`.
    reached_prompt = False

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: dict[str, str],
        on_line: Callable[[str], None] | None = None,
        on_notice: Notice | None = None,
        policy: _permission_policy.Policy | None = None,
        ledger: Path | None = None,
        on_rate_limit: Callable[[dict[str, Any]], None] | None = None,
        tail_lines: int = 400,
        token: str = "",
    ) -> None:
        self._argv = list(argv)
        self._cwd = cwd
        self._env = env
        self._on_line = on_line
        self._on_notice = on_notice
        self._on_rate_limit = on_rate_limit
        self._policy = policy or _permission_policy.Policy()
        self._token = token
        self._proc: asyncio.subprocess.Process | None = None
        self._readers: list[asyncio.Task[None]] = []
        self._write_lock = asyncio.Lock()

        self.tail: deque[str] = deque(maxlen=tail_lines)
        self.session_id: str | None = None
        self.model = ""
        self.login_expired = False
        self.output_count = 0
        self._last_event: float | None = None
        self._started_at = time.monotonic()
        #: Messages written whose turn has not reported a `result` yet.
        self._outstanding = 0
        #: Set by content events with nothing outstanding: a turn the session began itself (a background
        #: task finished). Cleared by the `result` that ends it.
        self._autonomous = False

        self.limit_hit_at: float | None = None
        self.limit_seen_wall: float | None = None
        self.limit_reset_at: float | None = None
        self.limit_text = ""
        self._submitted_since_limit = True
        #: The latest `rate_limit_info` per window (`five_hour`, `seven_day`, ...), as Claude Code sent it.
        self.rate_limits: dict[str, dict[str, Any]] = {}

        self.last_submission: tuple[str, float, int] | None = None
        self.sent_texts: deque[str] = deque(maxlen=500)
        self.permission_denials: deque[tuple[float, str]] = deque(maxlen=100)
        self._denied_tool_ids: set[str] = set()
        #: Structured totals for this process: turns, tokens, and the conversation's cost estimate.
        self.turns = 0
        self.results = 0
        self.errors = 0
        self.usage: dict[str, float] = {}
        self.cost_usd = 0.0
        self.last_result: dict[str, Any] | None = None
        self._ledger = ledger

    # -- lifecycle ---------------------------------------------------------- #

    async def start(self) -> None:
        """Spawn the process; the setup-token goes through a pipe (`_auth.token_fd`), never `env`."""
        with token_fd(self._env, self._token) as (env, pass_fds):
            self._proc = await _unprivileged.spawn(
                "Claude Code",
                *self._argv,
                cwd=str(self._cwd),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                pass_fds=pass_fds,
                limit=_LINE_LIMIT,
            )
        assert self._proc.stdout is not None and self._proc.stderr is not None
        self._readers = [
            asyncio.create_task(self._read_events(self._proc.stdout)),
            asyncio.create_task(self._read_stderr(self._proc.stderr)),
        ]
        # The handshake that turns on the control protocol; its answer lists commands and agents.
        await self._send(
            {
                "type": "control_request",
                "request_id": self._new_request_id(),
                "request": {"subtype": "initialize"},
            }
        )

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode if self._proc else None

    @property
    def busy(self) -> bool:
        """Whether a turn is in flight: one we sent, or one the session started on its own."""
        return self._outstanding > 0 or self._autonomous

    @property
    def last_output(self) -> float | None:
        """`time.monotonic()` of the session's last sign of work: now while a turn is in flight.

        A turn that has sent nothing for `BUSY_SILENCE_S` is no longer taken as working: a hung request
        then reads as silence, and the supervisor's idle handling sees it as it saw a frozen screen.
        """
        now = time.monotonic()
        sent = self.last_submission[1] if self.last_submission else self._started_at
        silent_since = max(self._last_event or self._started_at, sent)
        if self.busy and self.returncode is None and now - silent_since < BUSY_SILENCE_S:
            return now
        return self._last_event

    async def wait(self) -> int:
        """Wait for the process to exit and for its output to be read to the end."""
        assert self._proc is not None
        code = await self._proc.wait()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.gather(*self._readers, return_exceptions=True), timeout=5)
        return code

    async def stop(self, *, grace: float = 20.0) -> None:
        """End the session: interrupt a running turn, close stdin so it exits cleanly, then insist."""
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        if self.busy:
            # `_send` already reports a closed pipe; what is left is the lock or drain outlasting the wait.
            with contextlib.suppress(asyncio.TimeoutError, OSError):
                await asyncio.wait_for(self.interrupt(), timeout=min(grace, 5.0))
        # End of input is the clean exit: Claude Code flushes its transcript, which a signal would skip.
        if proc.stdin is not None and not proc.stdin.is_closing():
            proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
            return
        except asyncio.TimeoutError:
            pass
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(proc.pid), sig)
            except (ProcessLookupError, PermissionError):
                return
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
                return
            except asyncio.TimeoutError:
                continue

    # -- input -------------------------------------------------------------- #

    async def submit(self, text: str, *, label: str = "") -> bool:
        """Send one user message, whole: any length, newlines kept, queued by Claude Code mid-turn.

        Returns whether it was written; False means it was dropped and the caller still owns it.
        """
        if not text.strip():
            return False
        # Remembered before it is written, so the supervisor never reads its own message in the transcript as
        # a human's; taken back if it was not written.
        self.sent_texts.append(text)
        sent = await self._send({"type": "user", "message": {"role": "user", "content": text}})
        if not sent and self.sent_texts and self.sent_texts[-1] is text:
            self.sent_texts.pop()
        if sent:
            # Only a message that left counts as ours: liveness reads `last_submission` as "busy".
            self.last_submission = (text, time.monotonic(), self.output_count)
            self._submitted_since_limit = True
            self._outstanding += 1
            self._log_line(f"[sent] {label or text.splitlines()[0][:120]}")
        return sent

    async def interrupt(self) -> None:
        """Stop the running turn (what Escape did in the TUI); the session stays up for the next one."""
        await self._send(
            {
                "type": "control_request",
                "request_id": self._new_request_id(),
                "request": {"subtype": "interrupt"},
            }
        )

    async def _send(self, obj: dict[str, Any]) -> bool:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.returncode is not None or proc.stdin.is_closing():
            self._log_line("[stream] not sent: the session is not running")
            return False
        async with self._write_lock:
            try:
                proc.stdin.write((json.dumps(obj) + "\n").encode())
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                self._log_line(f"[stream] not sent: {exc}")
                return False
        return True

    @staticmethod
    def _new_request_id() -> str:
        return f"factory-{uuid.uuid4().hex[:12]}"

    # -- output ------------------------------------------------------------- #

    async def _read_events(self, stream: asyncio.StreamReader) -> None:
        while True:
            try:
                raw = await stream.readline()
            except ValueError:  # a line over the limit: skip it rather than lose the session
                self._log_line("[stream] skipped an event over the line limit")
                continue
            if not raw:
                return
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except (ValueError, RecursionError):
                # Claude Code prints some startup errors as plain text on stdout.
                self._log_line(line[:500])
                continue
            if isinstance(event, dict):
                try:
                    await self.handle(event, size=len(line))
                except _EVENT_ERRORS as exc:  # one bad event must not end the reader
                    self._log_line(f"[stream] could not handle a {event.get('type')} event: {exc!r}")

    async def _read_stderr(self, stream: asyncio.StreamReader) -> None:
        while raw := await stream.readline():
            text = raw.decode(errors="replace").rstrip()
            if text:
                self._log_line(text)

    async def handle(self, event: dict[str, Any], *, size: int = 0) -> None:
        """Apply one event to the session's state (public so tests can drive it without a process)."""
        kind = event.get("type")
        if kind not in ("control_response", "keep_alive"):
            self.output_count += max(size, 1)
            self._last_event = time.monotonic()
        if kind == "system":
            self._handle_system_event(event)
        elif kind == "assistant":
            self._note_activity()
            self._handle_assistant_event(event)
        elif kind == "user":
            self._note_activity()
            self._handle_tool_results(event)
        elif kind == "stream_event":
            self._note_activity()
        elif kind == "rate_limit_event":
            self._handle_rate_limit_event(event.get("rate_limit_info") or {})
            if self._on_rate_limit is not None:
                # The credential's windows, for the meter (`_usage.ingest_author_rate_limit`).
                self._on_rate_limit(event)
        elif kind == "result":
            self._handle_result(event)
        elif kind == "control_request":
            await self._answer_control_request(event)
        elif kind == "control_response" and (event.get("response") or {}).get("subtype") == "error":
            self._log_line(f"[stream] control request failed: {event['response'].get('error')}")

    def _note_activity(self) -> None:
        if self._outstanding == 0:
            self._autonomous = True

    def _handle_system_event(self, event: dict[str, Any]) -> None:
        sub = event.get("subtype")
        if sub == "init":
            self.reached_prompt = True
            self.session_id = event.get("session_id") or self.session_id
            self.model = str(event.get("model") or self.model)
        elif sub == "api_retry":
            if event.get("error") in _AUTH_ERRORS:
                self._mark_login_expired(str(event.get("error")))
            else:
                attempt = f"{event.get('attempt')}/{event.get('max_retries')}"
                self._log_line(f"[api retry] {event.get('error')} attempt {attempt}")
        elif sub == "permission_denied":
            # A deny rule or hook refused a call without asking: policy working, recorded for the report.
            tool_id = event.get("tool_use_id")
            if isinstance(tool_id, str) and tool_id in self._denied_tool_ids:
                return
            if isinstance(tool_id, str) and tool_id:
                self._denied_tool_ids.add(tool_id)
            self._record_denial(str(event.get("tool_name")), str(event.get("message") or ""), asked=False)
        elif sub == "hook_response" and event.get("exit_code") not in (0, None):
            self._log_line(f"[hook] {event.get('hook_name')} exited {event.get('exit_code')}")
        elif sub in ("task_started", "task_notification"):
            self._log_line(f"[task] {sub}: {event.get('description') or event.get('summary') or ''}"[:200])
        elif sub == "compact_boundary":
            self._log_line("[session] context compacted")

    def _handle_assistant_event(self, event: dict[str, Any]) -> None:
        error = event.get("error")
        message = event.get("message") or {}
        texts = [c.get("text", "") for c in message.get("content") or [] if c.get("type") == "text"]
        if error in _AUTH_ERRORS:
            self._mark_login_expired(str(error))
        elif error == "rate_limit":
            # Refused on the limit. The `rate_limit_event` that said so (status `rejected`) usually came
            # first; without one the reset is the latest known for a window that is exhausted, if any.
            self._record_limit_hit(self._find_known_exhausted_reset(), " ".join(texts)[:200])
        for content in message.get("content") or []:
            if content.get("type") == "text" and content.get("text", "").strip():
                self._log_line(content["text"].strip().splitlines()[0][:300])
            elif content.get("type") == "tool_use":
                self._log_line(f"[tool] {content.get('name')} {json.dumps(content.get('input'))[:200]}")

    def _handle_tool_results(self, event: dict[str, Any]) -> None:
        content = (event.get("message") or {}).get("content")
        if not isinstance(content, list):
            return
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_result" and item.get("is_error"):
                self._log_line(f"[tool error] {str(item.get('content'))[:200]}")

    def _handle_rate_limit_event(self, info: dict[str, Any]) -> None:
        kind = str(info.get("rateLimitType") or info.get("rate_limit_type") or "unknown")
        self.rate_limits[kind] = info
        if (info.get("status") or "") == "rejected":
            self._record_limit_hit(
                _meter.parse_event_reset(info.get("resetsAt", info.get("resets_at"))), kind=kind
            )

    def _find_known_exhausted_reset(self) -> float | None:
        now = time.time()
        resets = [
            at
            for info in self.rate_limits.values()
            if info.get("status") == "rejected"
            and (at := _meter.parse_event_reset(info.get("resetsAt", info.get("resets_at")))) is not None
            and at > now
        ]
        return max(resets) if resets else None

    def _record_limit_hit(self, reset_at: float | None, detail: str = "", *, kind: str = "") -> None:
        """Record a refusal on the usage limit, once per episode.

        A new episode is one after something was submitted, the first one, or one with a different
        reset: the same refusal reported twice (the event and then the assistant error) is one.
        """
        changed = reset_at is not None and reset_at != self.limit_reset_at
        if not (self._submitted_since_limit or self.limit_hit_at is None or changed):
            return
        self.limit_hit_at = time.monotonic()
        self.limit_seen_wall = time.time()
        self.limit_reset_at = reset_at
        self.limit_text = _describe_reset(reset_at, kind) or detail or "reset time not reported"
        self._submitted_since_limit = False
        self._log_line(f"[limit] usage limit reached: {self.limit_text}")

    def _mark_login_expired(self, error: str) -> None:
        if not self.login_expired:
            self.login_expired = True
            self._log_line(f"[auth] {error}: the setup-token no longer works")

    def _handle_result(self, event: dict[str, Any]) -> None:
        denials = event.get("permission_denials")
        if isinstance(denials, list):
            for denial in denials:
                if not isinstance(denial, dict):
                    continue
                tool_id = denial.get("tool_use_id")
                if isinstance(tool_id, str) and tool_id in self._denied_tool_ids:
                    continue
                if isinstance(tool_id, str) and tool_id:
                    self._denied_tool_ids.add(tool_id)
                try:
                    self._record_denial(str(denial.get("tool_name") or ""), "", asked=False)
                except _EVENT_ERRORS as exc:
                    # A failed report must not discard the turn or its usage ledger entry.
                    self.tail.append(f"[stream] could not report a denial: {exc!r}")
        self._denied_tool_ids.clear()
        self.results += 1
        self._outstanding = max(0, self._outstanding - 1)
        queued = event.get("queued_turn_count")
        if isinstance(queued, int) and not isinstance(queued, bool):
            # Messages queued mid-turn can be answered together: what Claude Code still holds is the count.
            # One written after it reported is not lost: its turn's events set `_autonomous`.
            self._outstanding = min(self._outstanding, queued)
        self._autonomous = False
        self.last_result = event
        self.turns += int(event.get("num_turns") or 0)
        usage = event.get("usage") or {}
        for field in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ):
            self.usage[field] = self.usage.get(field, 0.0) + float(usage.get(field) or 0)
        # The conversation's running total, carried across `--resume`: the latest is the figure.
        self.cost_usd = max(self.cost_usd, float(event.get("total_cost_usd") or 0))
        if event.get("is_error"):
            self.errors += 1
            errors = event.get("errors") or [event.get("result") or event.get("subtype")]
            self._log_line(f"[turn error] {event.get('subtype')}: {'; '.join(map(str, errors))[:300]}")
        self._log_line(
            f"[turn end] {event.get('subtype')} turns={event.get('num_turns')} "
            f"out={usage.get('output_tokens')} cost≈${float(event.get('total_cost_usd') or 0):.2f}"
        )
        self._record(event)

    def _record(self, event: dict[str, Any]) -> None:
        """Append the turn's figures to the ledger the usage report reads (`_usage.read_author_ledger`).

        `at` is Unix seconds (UTC, `time.time()`), `total_cost_usd` is Claude Code's USD list-price
        estimate, and the token fields are counts.
        """
        if self._ledger is None:
            return
        usage = event.get("usage") or {}
        row = {
            "at": time.time(),
            "session_id": event.get("session_id") or self.session_id,
            "model": self.model,
            "uuid": event.get("uuid"),
            "subtype": event.get("subtype"),
            "is_error": bool(event.get("is_error")),
            "num_turns": int(event.get("num_turns") or 0),
            "total_cost_usd": float(event.get("total_cost_usd") or 0),
            "usage": {k: usage.get(k, 0) for k in self.usage},
            "modelUsage": event.get("model_usage_since_last_result") or {},
        }
        try:
            self._ledger.parent.mkdir(parents=True, exist_ok=True)
            with self._ledger.open("a") as out:
                out.write(json.dumps(row) + "\n")
        except OSError as exc:
            self._log_line(f"[stream] could not record turn usage: {exc}")

    # -- permissions -------------------------------------------------------- #

    async def _answer_control_request(self, event: dict[str, Any]) -> None:
        request_id = event.get("request_id")
        request = event.get("request")
        if not isinstance(request, dict):
            # Malformed: answered as unsupported below, so Claude Code is never left waiting.
            request = {}
        if request.get("subtype") == "can_use_tool":
            tool = str(request.get("tool_name") or "")
            decision = self._policy.decide(tool, request.get("input") or {})
            if decision.behavior == "deny":
                tool_id = request.get("tool_use_id")
                if not isinstance(tool_id, str) or tool_id not in self._denied_tool_ids:
                    if isinstance(tool_id, str) and tool_id:
                        self._denied_tool_ids.add(tool_id)
                    self._record_denial(tool, decision.message, asked=True)
            body: dict[str, Any] = {
                "subtype": "success",
                "request_id": request_id,
                "response": decision.to_control_response(),
            }
        else:
            # Hook callbacks and SDK MCP servers are not registered by this session.
            body = {
                "subtype": "error",
                "request_id": request_id,
                "error": f"unsupported control request: {request.get('subtype')}",
            }
        await self._send({"type": "control_response", "response": body})

    def _record_denial(self, tool: str, message: str, *, asked: bool) -> None:
        tool = "".join(char for char in tool if char.isprintable())[:120] or "unknown"
        message = "".join(char for char in message if char.isprintable())
        self.permission_denials.append((time.monotonic(), tool))
        how = "denied by the factory's permission policy" if asked else "blocked by a deny rule or hook"
        self._log_line(f"[permission] {tool} {how}: {message[:200]}")
        if self._on_notice:
            self._on_notice("🚫", f"{tool} {how}")

    # -- logging ------------------------------------------------------------ #

    def _log_line(self, line: str) -> None:
        self.tail.append(line)
        if self._on_line:
            self._on_line(line)
