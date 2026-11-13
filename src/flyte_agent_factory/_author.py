"""The author's engine: which CLI runs it, how a session starts, the engine handoff and the Codex login."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
import subprocess
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import flyte
import flyte.errors
import flyte.remote
import flyte.report
import flyte.sandbox

from flyte_agent_factory import (
    _auth,
    _checkpoint,
    _codex_auth,
    _inbox,
    _meter,
    _pins,
    _policy,
    _router,
    _secret_env,
    _session_config,
    _session_log,
    _session_setup,
    _state,
    _timeline,
    _usage,
)
from flyte_agent_factory._author_session import AuthorSession
from flyte_agent_factory._bridge import (
    DEFAULT_ASK_TIMEOUT_S,
    _write_atomic,
)

# Imported here, not where used: the deploy bundles only modules loaded at import time, and
# a lazy import left `_delegation` out of the bundle (every project died at startup).
from flyte_agent_factory._codex_process import CodexAuthor
from flyte_agent_factory._errors import REMOTE_ERRORS
from flyte_agent_factory._process import ClaudeProcess


def has_conversation(config_dir: Path) -> bool:
    """Whether there is actually a transcript for `--continue` to resume.

    Restored state only proves a config dir came back, not that a conversation
    got far enough to be resumable. Passing `--continue` without one makes
    Claude Code print "No conversation found to continue" and exit within
    milliseconds -- which, under `retries`, is a crash loop rather than a
    failure.
    """
    projects = config_dir / "projects"
    if not projects.is_dir():
        return False
    return any(p.stat().st_size > 0 for p in projects.rglob("*.jsonl"))


def session_argv_candidates(base: list[str], *, resume: bool) -> list[list[str]]:
    """Argv variants to try, most-preferred first.

    Resuming is attempted first when there is something to resume, with a plain
    start as the fallback -- the on-disk transcript layout is a Claude Code
    internal, so `has_conversation` is a good guess rather than a guarantee.
    """
    if resume:
        return [[*base, "--continue"], list(base)]
    return [list(base)]


# Startup failures worth retrying with a different argv rather than failing the
# task. Matched against the terminal tail.
_RECOVERABLE_START = re.compile(r"no conversation found|could not find conversation", re.IGNORECASE)


#: How long a headless resume is watched for an immediate "no conversation" exit.
_HEADLESS_START_CHECK_S = 5.0


async def _check_headless_start(proc: ClaudeProcess) -> None:
    """Raise if a headless session exits at once, so `_start_session` can try the next argv.

    A headless session has nothing to wait for, so this is the only place an immediate exit is seen.
    Only called when a fallback argv exists, and a session that stays up is left alone.
    """
    deadline = time.monotonic() + _HEADLESS_START_CHECK_S
    while proc.returncode is None and time.monotonic() < deadline:
        await asyncio.sleep(0.2)
    if proc.returncode is not None:
        raise RuntimeError("Claude Code exited at startup:\n" + "\n".join(list(proc.tail)[-40:]))


def _author_argv(project_name: str, mode: str, model: str, effort: str) -> list[str]:
    """Return the author session's argv before the browser, resume and session-id flags are added."""
    return [
        "claude",
        "--name",
        project_name,
        "--permission-mode",
        mode,
        *_policy.argv(_policy.Setting(model, effort)),
    ]


async def _start_session(
    candidates: list[list[str]], *, env: dict[str, str], timeline: _timeline._Timeline
) -> ClaudeProcess:
    """Start Claude Code, stepping past a startup failure we know how to fix."""
    last: BaseException | None = None
    for index, argv in enumerate(candidates):
        timeline.row("🚀", " ".join(shlex.quote(a) for a in argv))
        capture = _session_setup._log_session_line(timeline)
        proc = ClaudeProcess(
            argv,
            cwd=_session_config.WORKSPACE,
            env=env,
            on_line=capture,
            inbox_messages=True,
            token=os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip(),
        )
        try:
            await proc.start()
        except BaseException:
            capture.close()
            raise
        try:
            if index + 1 < len(candidates):
                await _check_headless_start(proc)
            return proc
        except BaseException as e:
            last = e
            tail = "\n".join(proc.tail)
            try:
                await proc.stop(grace=5.0)
            finally:
                capture.close()
            if (
                isinstance(e, RuntimeError)
                and index + 1 < len(candidates)
                and _RECOVERABLE_START.search(tail)
            ):
                timeline.row("↻", "nothing to resume; starting a fresh session")
                continue
            raise
    raise last if last else RuntimeError("no session argv to try")


class _NoSession:
    """The author of a run that was stopped before its session started: nothing runs, nothing to stop."""

    returncode = None
    tail: tuple[str, ...] = ()

    async def submit(self, text: str, **kwargs: object) -> None:
        pass

    async def wait(self) -> int:
        await asyncio.Event().wait()  # never exits by itself; the stop that made this ends the run
        return 0

    async def stop(self, **kwargs: object) -> None:
        pass


_CODEX_THREAD_FILE = _session_config.STATE_ROOT / "codex-thread"
#: What runs a Codex turn; the tests point it at `tests/fake_codex.py`.
_CODEX_COMMAND: tuple[str, ...] = ("codex",)


def _author_account(engine: str) -> str:
    """Return the meter account the author session of `engine` runs on; empty for Codex's engine-wide one."""
    return _session_config._claude_account() if engine == "claude" else _usage._limit_account("codex")


def _author_meter(engine: str) -> _meter.Meter | None:
    """Return the latest meter of the credential the author session of `engine` runs on."""
    account = _author_account(engine)
    own = _usage._meters.readings.get(account) if _usage._meters and account else None
    return own if own is not None else _usage._child_meter(engine)


def _restore_refusal(session: AuthorSession, owner: dict[str, Any]) -> None:
    """Pause a restarted author whose saved refusal has not reset yet.

    The refusal belongs to the credential that refused: a saved one from another account (a swapped
    token) or from before accounts were saved says nothing about this one.
    """
    same_account = owner.get("account", "") == _author_account(_usage._author_engine)
    if owner.get("limited_until", 0) > time.time() and same_account:
        session.pause()
        current: Any = session.current  # limit_restored is set on the engine process, not in its protocol
        current.limit_hit_at = time.monotonic()
        current.limit_text = "Usage limit recorded before restart"
        # The notice carries no reset time. The saved one is the truth; deriving one from the notice
        # restarted the clock at now + LIMIT_RETRY_S on every restart and postponed the work for good.
        current.limit_restored = (current.limit_hit_at, owner["limited_until"])


def _author_refusal_route(
    pin: _pins.Pin, timeline: _timeline._Timeline, refused: str, reset_at: float | None
) -> _router.Decision:
    available = _usage._author_engines()
    meters = {e: _author_meter(e) for e in available}
    meter = _author_meter(refused) or _meter.Meter(refused, "refusal", time.time())
    meters[refused] = replace(meter, limited_until=max(reset_at or 0.0, meter.limited_until or 0.0) or None)
    # Never prefer the engine that refused: once its remembered reset passed, "preferred wins" kept the
    # author on it every 30 minutes while the other engine sat free (#125).
    others = [e for e in available if e != refused]
    return _router.route(
        "author",
        meters,
        available=available,
        now=time.time(),
        preferred=pin.engine or (others[0] if others else refused),
        pin=pin.engine,
        hard_pin=pin.hard,
        retry_s=_session_config.LIMIT_RETRY_S,
    )


def _when(at: float) -> str:
    """Return `at` as a person reads a reset that may be days away, e.g. `Mon 16 Nov 00:40 UTC`."""
    return f"{datetime.fromtimestamp(at, timezone.utc):%a %d %b %H:%M} UTC"


def _clock(at: float) -> str:
    """Return `at` as the router rows show a retry time, e.g. `22:40 UTC`."""
    return f"{datetime.fromtimestamp(at, timezone.utc):%H:%M} UTC"


def _pin_why(pin: _pins.Pin) -> str:
    """Say what holds `pin`, for the paused row's reason not to use another engine."""
    if pin.source == "direction":
        return "operator direction"
    if pin.source == "benchmark hard_pin":
        return "benchmark hard_pin"
    if pin.source == "allowance":
        return "no engine switch left in this run"
    fallback = _policy.read("ENGINE_FALLBACK").strip()
    return f'ENGINE_FALLBACK="{fallback}"' if fallback else "ENGINE_FALLBACK unset"


def _pin_fix(pin: _pins.Pin) -> str:
    """Say what would lift `pin`, for the text that asks a human to unblock the coordinator."""
    if pin.source == "direction":
        return "send a direction to resume on the other engine (`resume on <engine>`)"
    if pin.source == "benchmark hard_pin":
        return "rerun the benchmark without hard_pin"
    if pin.source == "allowance":
        return "relaunch the project (this run has used its engine switches)"
    return 'set ENGINE_FALLBACK="allow"'


def _ask_human(timeline: _timeline._Timeline, text: str) -> None:
    """Say on the timeline that the coordinator cannot continue on its own, and notify the owner."""
    timeline.row("🚨", f"a human needs to look: {text}")
    if timeline.bridge is not None:
        try:
            timeline.bridge.notify_owner(
                f"The coordinator is paused and cannot continue on its own: {text}", DEFAULT_ASK_TIMEOUT_S
            )
        except OSError as exc:
            timeline.row("⚠️", f"could not ask the owner: {type(exc).__name__}: {exc}")


def _permitted(pin: _pins.Pin, engine: str) -> bool:
    """Whether the coordinator may run on `engine` under `pin`: a hard pin allows only its own engine."""
    return not (pin.engine and pin.hard) or engine == pin.engine


def _reset_known(
    decision: _router.Decision, pin: _pins.Pin, refused: str, refused_known: bool, refused_until: float
) -> bool:
    """Whether an engine the coordinator may use has a known reset still ahead.

    The refused engine's own time counts only when a notice, meter or saved record gave it: the
    LIMIT_RETRY_S fallback is a guess, and counting it hid an engine that refuses for good (#125). The
    permitted engines come from the pin, not from `decision.pin`, which the router sets only on a deferral.
    """
    now = time.time()
    if refused_known and refused_until > now:
        return True
    return any(
        c.usable_at is not None and c.usable_at > now
        for c in decision.candidates
        if c.engine != refused and _permitted(pin, c.engine)
    )


def _known_resets(pin: _pins.Pin, refused: str, refused_known: bool, refused_until: float) -> list[float]:
    """Return the resets, past or ahead, that the meters know for the engines the coordinator may use.

    Read live, so a reset a meter learns while a finished session waits out its backoff ends the wait. The
    refused engine's own LIMIT_RETRY_S guess is left out: the handoff notes it on the meter too, and it is
    not a reset.
    """
    resets = [refused_until] if refused_known else []
    for engine in _usage._author_engines():
        meter = _author_meter(engine) if _permitted(pin, engine) else None
        at = meter.limited_until if meter else None
        if at is None or (engine == refused and not refused_known and at == refused_until):
            continue
        resets.append(at)
    return resets


def _no_reset_text(engine: str, pin: _pins.Pin | None = None) -> str:
    """Return the human-needed text for an engine refusing a finished session with no reset time.

    `pin` is the one the last route ran under, which is the allowance pin once the switches are used up.
    """
    text = f"{engine} keeps refusing with no reset time; restore its quota or {_router._FIX[engine]}"
    pin = pin or _pins.author_pin()
    others = [e for e in _usage._author_engines() if e != engine]
    if others and pin.engine == engine and pin.hard:
        text += f", or {_pin_fix(pin)} to use {' or '.join(_with_limit(e) for e in others)}"
    return text


def _with_limit(engine: str) -> str:
    """`engine`, with the reset it waits for when it is limited too: lifting a pin alone would not run it."""
    meter = _author_meter(engine)
    until = meter.limited_until if meter else None
    return f"{engine} (limited until {_when(until)})" if until and until > time.time() else engine


def _latest_claude_thread() -> str:
    """Return the id of the newest Claude conversation in this workspace, or "" when there is none."""
    transcripts = _inbox._session_transcripts(_session_config.CONFIG_DIR, _session_config.WORKSPACE)
    if not transcripts:
        return ""
    return max(transcripts, key=lambda p: p.stat().st_mtime).stem


def _direct_engine(session: asyncio.Future[Any], engine: str) -> str:
    """Pin the coordinator to `engine` as the operator directed; return the row that says what happens.

    A paused session re-routes on the pump's next tick instead of at the next reset; a running one keeps
    its engine until it next pauses or the pod restarts.
    """
    _pins.set_direction_pin(engine)
    proc = session.result() if session.done() and not session.cancelled() else None
    if isinstance(proc, AuthorSession) and proc._paused:
        proc.reroute_requested = True
        return f"engine direction: resume on {engine}; re-routing the paused session now"
    return f"engine direction: next pause or restart runs on {engine}"


def _pin_row(pin: _pins.Pin, until: str, others: list[str]) -> str:
    """Return the row that says once per pause why a pinned coordinator waits instead of moving."""
    instead = f" instead of moving to {' or '.join(others)}" if others else ""
    if pin.source == "direction":
        return f"coordinator directed to resume on {pin.engine}, so it waits until {until}{instead}"
    if pin.source == "benchmark hard_pin":
        return f"coordinator pinned to {pin.engine} by hard_pin, so it waits until {until}{instead}"
    return (
        f'coordinator pinned to {pin.engine} by {pin.source}; ENGINE_FALLBACK is not "allow", '
        f"so it waits for {pin.engine} until {until}{instead}"
    )


#: The `project` task, set by agent.py: the handoff reads its retry budget, and this module cannot
#: import agent (the same hook as `_reviewer.subagent_task`).
project_task: Any = None

# Each handoff uses one of project's 20 Flyte retries. Leave twelve for infrastructure
# failures; once this run's allowance is spent, the author waits for its own reset.
_AUTHOR_HANDOFF_MAX_RESTARTS = 8
_AUTHOR_HANDOFF_MIN_RETRIES_LEFT = 2  # the handoff's own retry, plus one margin

# Bound on each handoff step that talks to the outside (credential persistence, checkpoint): a
# stall here must still end in the restart.
_HANDOFF_STEP_TIMEOUT_S = 30


class AuthorHandoffRestart(RuntimeError):
    """A recoverable task failure: only a fresh pod can isolate the next author."""


def _author_handoff(
    session: AuthorSession, state: Any, timeline: _timeline._Timeline, *, brief: str
) -> Callable[..., Awaitable[bool]]:
    refused_engine = ""
    refused_until = 0.0
    refused_known = False
    refused_hit = None
    last_decision: tuple[str | None, float | None] | None = None
    deferred_until = 0.0
    # What this pause already said, so a tick that changes nothing writes nothing. Reset by a new refusal.
    last_paused: tuple[str, float | None] | None = None
    pin_rows: set[tuple[str, str]] = set()
    allowance_noted = False
    # Each distinct "a human needs to look" text is raised once per session, not once per pause.
    asked: set[str] = set()

    def note_pause(decision: _router.Decision, pin: _pins.Pin) -> None:
        """Say who the paused coordinator waits for, until when and why not the other engine."""
        nonlocal last_paused
        p = _router.paused(decision, now=time.time(), pin_why=_pin_why(pin), pin_fix=_pin_fix(pin))
        # The phase carries the pause on every deferral: the pump re-sets the phase after each one.
        timeline.phase("limited", limited_until=session.retry_at, paused=p.as_dict())
        if p.engine and p.engine == refused_engine and not refused_known and p.until == refused_until:
            until = f"its reset (time unknown; checking again at {_when(session.retry_at or refused_until)})"
        else:
            until = _when(p.until) if p.until else "its reset (time unknown)"
        if (p.engine, p.until) != last_paused:
            last_paused = (p.engine, p.until)
            why = "; ".join(f"{e}: {text}" for e, text in p.others.items())
            waiting = f"waiting for {p.engine} until {until}" if p.engine else "no engine can run"
            timeline.row("⏸️", f"coordinator paused, {waiting}" + (f"; not using {why}" if why else ""))
        if decision.pinned and pin.engine and pin.source != "allowance":
            if (pin.engine, pin.source) not in pin_rows:
                pin_rows.add((pin.engine, pin.source))
                others = [e for e in _usage._author_engines() if e != pin.engine]
                timeline.row("⏸️", _pin_row(pin, until, others))
        if p.needs_human and p.needs_human not in asked:
            asked.add(p.needs_human)
            _ask_human(timeline, p.needs_human)

    def known_resets() -> list[float]:
        pin = getattr(session, "limit_pin", None)
        return _known_resets(pin, refused_engine, refused_known, refused_until) if pin else []

    session.known_resets = known_resets

    async def handoff(reset_at: float | None, interrupted: bool, reason: str = "usage limit") -> bool:
        nonlocal refused_engine, refused_until, refused_known, refused_hit, last_decision, deferred_until
        nonlocal last_paused, allowance_noted
        # Also on a re-evaluation at a deferred retry: the other engine may have reset since.
        await _usage._read_meters(timeline)
        # Read on every call, not once at start: a direction typed while paused changes it.
        pin = _pins.author_pin()
        if refused_engine != _usage._author_engine or refused_hit != session.limit_hit_at:
            refused_engine = _usage._author_engine
            refused_hit = session.limit_hit_at
            own_meter = _author_meter(refused_engine)
            # A refusal restored after a restart carries its saved reset only here: the pump passes no
            # reset for it, and a missing or replaced meters file must not turn a weekly wait into 30 minutes.
            restored = getattr(session.current, "limit_restored", None)
            saved = restored[1] if restored and restored[0] == session.limit_hit_at else None
            # The latest reset still ahead: a 5-hour notice must not shorten a known weekly reset (#125).
            future = [
                at
                for at in (reset_at, own_meter.limited_until if own_meter else None, saved)
                if at is not None and at > time.time()
            ]
            refused_known = bool(future)
            refused_until = max(future) if future else time.time() + _session_config.LIMIT_RETRY_S
            if not future and saved is not None:
                # The saved reset passed between the restart and this first route: the engine has reset,
                # which is a known time, not an unknown one to hold for another LIMIT_RETRY_S.
                refused_known, refused_until = True, saved
            last_paused, allowance_noted = None, False
            pin_rows.clear()
            if _usage._meters:
                try:
                    _usage._meters.note_limit(refused_engine, refused_until, _author_account(refused_engine))
                except _usage._METER_ERRORS as exc:
                    _timeline._log_line(f"[meter] {type(exc).__name__}", err=True)
            if isinstance(session.current, CodexAuthor):
                async with session.current._queue_lock:
                    session.pending[:0] = session.current._queued
                    session._save()
                    session.current._queued.clear()
                    session.current._save_pending()
            else:
                # Messages waiting behind a permission menu were never typed, and stopping the process
                # destroys them. They are newer than the interrupted submission and older than anything
                # paused into `pending`.
                held = getattr(session.current, "_held", None) or []
                carried = ([interrupted] if interrupted else []) + [text for text, _ in held]
                if carried:
                    session.pending[:0] = carried
                    session._save()
                held.clear()
            owner_file = _session_config.STATE_ROOT / "author-conversation.json"
            owner = (
                json.loads(owner_file.read_text())
                if owner_file.is_file()
                else {"engine": _usage._author_engine}
            )
            owner["limited_until"] = refused_until
            owner["account"] = _author_account(refused_engine)
            _write_atomic(owner_file, owner)
        decision = _author_refusal_route(pin, timeline, refused_engine, refused_until)
        ctx = flyte.ctx()
        run_name = ctx.action.run_name if ctx and ctx.action else "local"
        handoff_count = sum(
            record.get("run", "local") == run_name for record in _session_config._author_handoff_records()
        )
        # A handoff spends one Flyte retry. Keep one more for ordinary failures, or the restart
        # on the last attempt would end the project with nothing left to resume it.
        attempt = int(getattr(ctx, "attempt_number", 0) or 0)
        retries_left = project_task.retries.count - attempt
        if (
            handoff_count >= _AUTHOR_HANDOFF_MAX_RESTARTS or retries_left < _AUTHOR_HANDOFF_MIN_RETRIES_LEFT
        ) and decision.engine not in (None, _usage._author_engine):
            # Stay on this engine as if hard-pinned: the router then wakes it at its own reset, or polls
            # every LIMIT_RETRY_S when that is unknown, rather than guessing a time (#125).
            pin = _pins.Pin(_usage._author_engine, True, "allowance")
            decision = _author_refusal_route(pin, timeline, refused_engine, refused_until)
            if not allowance_noted:
                allowance_noted = True
                used = min(handoff_count, _AUTHOR_HANDOFF_MAX_RESTARTS)
                timeline.row(
                    "⏸️",
                    f"engine switches used up ({used}/{_AUTHOR_HANDOFF_MAX_RESTARTS}); "
                    f"staying on {_usage._author_engine} until its reset",
                )
        # After the allowance check: once the switches are used up, the other engine is no longer permitted.
        session.limit_pin = pin
        session.reset_known = _reset_known(decision, pin, refused_engine, refused_known, refused_until)
        if decision.deferred:
            # A missing reset is polled on a fixed deadline; recomputing now + retry_s on every
            # pump tick would drift forever and checkpoint the same pause on every tick.
            retry_at = decision.retry_at
            assert retry_at is not None  # the router gives every deferral a retry time
            if deferred_until > time.time():
                retry_at = min(retry_at, deferred_until)
            deferred_until = retry_at
            decision = replace(decision, retry_at=retry_at)
        else:
            deferred_until = 0.0
        key = (decision.engine, decision.retry_at)
        if key != last_decision:
            _usage._record_route(decision, timeline)
            last_decision = key
        else:
            # Every attempt is shown, so a reader can tell the router was asked again; the repeat is short
            # because the candidates and the reason are the row above.
            retry = f"; retry at {_clock(decision.retry_at)}" if decision.retry_at is not None else ""
            timeline.row("🔀", f"router: author -> {decision.engine or 'deferred'} (unchanged{retry})")
        session.retry_at = decision.retry_at or refused_until
        if decision.deferred:
            note_pause(decision, pin)
        if decision.deferred or decision.engine == _usage._author_engine:
            if last_decision != getattr(session, "_checkpointed_limit", None):
                try:
                    await asyncio.wait_for(
                        _state.save_checkpoint(state, exclude_private=True), timeout=_HANDOFF_STEP_TIMEOUT_S
                    )
                except (*REMOTE_ERRORS, TimeoutError) as exc:
                    # Not remembered as saved: the next pump tick tries again.
                    timeline.row("⚠️", f"limit checkpoint failed: {type(exc).__name__}: {exc}")
                else:
                    session._checkpointed_limit = last_decision
            return False

        assert decision.engine is not None  # deferred decisions returned above
        old_engine, new_engine = _usage._author_engine, decision.engine
        if pin.source == "direction" and pin.engine == new_engine:
            reason = f"direction: resume on {new_engine} ({reason})"
        session._switching = True
        path = None

        def build_restart_error(failure: Exception | None) -> AuthorHandoffRestart:
            return AuthorHandoffRestart(
                f"author handoff {old_engine} -> {new_engine}; restart from {path or 'no record'}"
                + (f" (after {type(failure).__name__}: {failure})" if failure else "")
            )

        try:
            # Once the author is stopped, `wait_for_exit` ignores its exit while `_switching` is set,
            # so nothing below may escape or stall: every outcome ends in the restart at the bottom,
            # where the next pod resumes from whatever was recorded. The record is written first so
            # that pod knows what was intended.
            try:
                old_thread = getattr(session.current, "thread_id", None)
                await session.current.stop()
                handoff_dir = _session_config.STATE_ROOT / "author-handoffs"
                handoff_dir.mkdir(parents=True, exist_ok=True)
                number = len(list(handoff_dir.glob("*.json"))) + 1
                record = {
                    "run": run_name,
                    "pending": list(session.pending),
                    "from": old_engine,
                    "to": new_engine,
                    "reason": reason,
                    "reset_at": refused_until,
                    "old_thread": old_thread,
                    "transcripts": [
                        str(p)
                        for p in _inbox._session_transcripts(
                            _session_config.CONFIG_DIR, _session_config.WORKSPACE
                        )
                    ],
                    "at": time.time(),
                }
                path = handoff_dir / f"{number}.json"
                record["findings"] = list(timeline.open_findings)
                record["directions"] = list(_inbox._directions)
                _write_atomic(path, record)
                on_line = getattr(session.current, "_on_line", None)
                if isinstance(on_line, _session_log.RawCapture):
                    on_line.close()
                prompt = await _handoff_prompt(
                    old_engine,
                    new_engine,
                    f"{reason}; reset {_when(refused_until)}",
                    brief,
                    timeline,
                    str(old_thread or ""),
                    declared_done=bool(getattr(session, "declared_done", False)),
                )
                # Persist the first turn before starting either process. If the pod dies here the next
                # attempt starts the chosen engine fresh and delivers this same handoff from the journal.
                session.pending.insert(0, prompt)
                session._save()
                _CODEX_THREAD_FILE.unlink(missing_ok=True)
                owner = _session_config.STATE_ROOT / "author-conversation.json"
                conversation: dict[str, Any] = {"engine": new_engine, "fresh": True, "pin": pin.engine}
                if new_engine == "claude":
                    if earlier := _latest_claude_thread():
                        # Back to Claude after a Codex stint: resume its own conversation, which still
                        # holds everything before the switch; the queued briefing covers the rest.
                        conversation = {"engine": "claude", "thread": earlier, "pin": pin.engine}
                    else:
                        conversation["thread"] = str(uuid.uuid4())
                _write_atomic(owner, conversation)
                timeline.row(
                    "🔀",
                    f"author handoff {old_engine} -> {new_engine}: {reason}, "
                    f"reset {_when(refused_until)}; attempts linked in {path}",
                )
            finally:
                # Best effort, and also on failure: the next pod resumes from this checkpoint. The
                # factory never pushes; the briefing tells the new author to push first.
                try:
                    await asyncio.wait_for(
                        _state.save_checkpoint(state, exclude_private=True), timeout=_HANDOFF_STEP_TIMEOUT_S
                    )
                except (*REMOTE_ERRORS, TimeoutError) as exc:
                    timeline.row("💥", f"author handoff checkpoint failed: {type(exc).__name__}: {exc}")
                else:
                    timeline.row(
                        "💾",
                        f"checkpoint saved before switching to {new_engine}; "
                        "the new author is told to commit and push first",
                    )
        except Exception as exc:
            # The author is already stopped, so any failure must end in the restart.
            timeline.row("💥", f"author handoff failed: {type(exc).__name__}: {exc}")
            raise build_restart_error(exc) from exc
        except asyncio.CancelledError:
            session._switching = False
            raise
        # Flyte retries ordinary task errors on a fresh pod. Do not start another author in
        # this process: detached tools can clear their environment and escape any process scan.
        raise build_restart_error(None)

    return handoff


async def _handoff_prompt(
    old_engine: str,
    new_engine: str,
    reason: str,
    brief: str,
    timeline: _timeline._Timeline,
    old_thread: str = "",
    *,
    declared_done: bool | None = None,
) -> str:
    """Return the first turn of an author that takes over from `old_engine`: what it needs to continue.

    `declared_done` is None when the caller cannot know it (a project resumed on another engine).
    """
    status = await _checkpoint._handoff_git_out(_session_config.WORKSPACE, "status", "--short")
    diff = await _checkpoint._handoff_git_out(
        _session_config.WORKSPACE, "diff", *_secret_env.GIT_NO_DRIVERS, "HEAD", "--stat"
    )
    branch = await _checkpoint._handoff_git_out(
        _session_config.WORKSPACE, "rev-parse", "--abbrev-ref", "HEAD"
    )
    head = await _checkpoint._handoff_git_out(_session_config.WORKSPACE, "rev-parse", "--short", "HEAD")
    unpushed = await _checkpoint._handoff_git_out(
        _session_config.WORKSPACE, "rev-list", "--count", "@{u}..HEAD"
    )
    # No upstream (a branch never pushed) makes git fail; "unknown" says so without a git error message.
    unpushed = unpushed.strip() if unpushed.strip().isdigit() else "unknown"
    return (
        f"AUTHOR HANDOFF: {old_engine} -> {new_engine} ({reason}).\n"
        # The factory never pushes on the author's behalf, so what the previous author committed but did
        # not push exists only in this checkpoint until the new author pushes it.
        "Before anything else: commit tracked work and push your branch (the factory does not push for "
        "you), then continue.\n"
        f"Branch: {branch.strip()}  HEAD: {head.strip()}  Unpushed commits: {unpushed}\n"
        f"Review: round {timeline.review_round}, passed={timeline.review_passed}\n"
        # A switch while finished work waits on a wrap-up must not start the work over (#125).
        f"DONE declared before the switch: "
        f"{'not recorded' if declared_done is None else 'yes' if declared_done else 'no'}\n"
        "Continue this same work item. Partial work is UNVERIFIED; hand it to QA.\n"
        "processes the previous author started (servers, watchers) were stopped; "
        "restart any the brief requires\n"
        f"Original brief (full brief: "
        f"{_session_config.WORKSPACE / _session_config._BRIEF_FILE}):\n{brief[:12000]}\n\n"
        f"Git status:\n{status[:6000]}\nDiff summary:\n{diff[:6000]}\n"
        f"Open review findings:\n{chr(10).join(timeline.open_findings)[:8000]}\n"
        f"Explicit direction:\n{chr(10).join(_inbox._directions)[:8000]}\n"
        f"Full transcripts: {_session_config.CONFIG_DIR / 'projects'} and {_session_config.CODEX_HOME_DIR}.\n"
        + (f"The {old_engine} author's conversation: thread {old_thread}.\n" if old_thread else "")
        + f"Review contract, findings and directions: {_session_config.STATE_ROOT / 'review'}.\n"
        f"Pending questions: {_inbox._question_registry_path()}. "
        f"Artifacts and child action ids: {_session_config.WORKSPACE}.\n"
        "Preserve completed children and attach to active children; do not relaunch "
        "running work. "
        "Never abort a0 or an action you did not start; stop your own children only with "
        "`python -m flyte_agent_factory.child abort <exact name>`."
    )


def _latest_handoff() -> tuple[Path, dict[str, Any]] | None:
    """Return the newest author handoff record and its path; numbered, so "10" sorts after "9"."""
    records = sorted(
        (_session_config.STATE_ROOT / "author-handoffs").glob("*.json"), key=lambda path: int(path.stem)
    )
    return (records[-1], json.loads(records[-1].read_text())) if records else None


def _takeover_row(engine: str) -> str:
    """Return the start row of an author taking over from a handoff not yet delivered, else "".

    Its briefing is queued first, so "starting fresh" would wrongly tell the owner the work was lost.
    """
    latest = _latest_handoff()
    if latest is None or latest[1].get("to") != engine or "new_thread" in latest[1]:
        return ""
    record = latest[1]
    thread = f" (thread {record['old_thread']})" if record.get("old_thread") else ""
    return f"taking over from {record.get('from')} with a handoff briefing{thread}"


async def _resume_author_pending(session: AuthorSession, engine: str) -> None:
    await session.resume("Resume the pending work item.", label="Restored author handoff")
    owner_file = _session_config.STATE_ROOT / "author-conversation.json"
    owner = json.loads(owner_file.read_text()) if owner_file.is_file() else {}
    owner.update(engine=engine)
    if engine == "codex":
        async with asyncio.timeout(30):
            while session.thread_id is None and session.returncode is None:
                await asyncio.sleep(0.05)
        owner["thread"] = session.thread_id
    owner.pop("fresh", None)
    _write_atomic(owner_file, owner)
    if latest := _latest_handoff():
        path, record = latest
        if record["to"] == engine and "new_thread" not in record:
            record["new_thread"] = owner.get("thread")
            _write_atomic(path, record)


def _engine_input_pin(engine: str) -> _pins.Pin:
    """Record the `engine` task input as the coordinator's pin and return the pin now in force.

    Checked here because `_pins` reads an unknown engine as no pin, and a typo in the input must fail the
    launch rather than quietly run unpinned.
    """
    engine = engine.strip().lower()
    if engine not in ("", "claude", "codex"):
        raise ValueError(f"engine must be 'claude' or 'codex', not {engine!r}")
    _pins.engine_input = engine
    return _pins.author_pin()


async def _choose_author_engine(
    pin: _pins.Pin, available: tuple[str, ...], resumed: bool, timeline: _timeline._Timeline
) -> str:
    """`claude` or `codex` for the author session.

    A pin wins (`_pins.author_pin`: a direction, the `engine` input, else `COORDINATOR_ENGINE`). It wins
    over the engine a resumed conversation sits on, so a codex-owned project pinned to Claude goes back to
    Claude. A pin to an engine the slot has no credential for is an error. A hard pin starts on its engine
    even when it is exhausted and waits out the limit; a soft one (`ENGINE_FALLBACK=allow`) is used unless
    the router moves off it, and the timeline says so.
    Without a pin a resumed project stays on the engine whose conversation it holds, including after a
    handoff, and otherwise the router picks from the current meters among the engines the slot can use,
    preferring admission's choice (`AGENT_FACTORY_ADMITTED_ENGINE`, never a pin); the decision is a
    timeline row like any other. Claude is metered on the setup-token its author session runs on.
    """
    engine = pin.engine
    if engine and engine not in available and (available or engine == "codex"):
        # Claude's own sign-in names what is missing when nothing at all is configured.
        raise flyte.errors.NonRecoverableError(
            f"the author is pinned to {engine} (the engine input or AGENT_FACTORY_COORDINATOR_ENGINE), but "
            f"this slot has no {engine} login mounted (engines available: {', '.join(available) or 'none'}). "
            "Remove the pin or configure the login; the project will not run on the other engine."
        )
    if not available:
        return "claude"
    if resumed and not engine:
        try:
            owner = json.loads((_session_config.STATE_ROOT / "author-conversation.json").read_text())
        except (OSError, ValueError):
            owner = {}
        if owner.get("engine") in available:
            return str(owner["engine"])
        if CodexAuthor.saved_thread(_CODEX_THREAD_FILE) and "codex" in available:
            return "codex"
        if has_conversation(_session_config.CONFIG_DIR) and "claude" in available:
            return "claude"
    if available == ("claude",) and not engine:
        return "claude"
    await _usage._read_meters(timeline)
    meters = {e: _author_meter(e) for e in available} if _usage._meters else {}
    admitted = _policy.read("ADMITTED_ENGINE").strip().lower()
    preferred = engine or (admitted if admitted in available else available[0])
    decision = _router.route(
        "author",
        meters,
        available=available,
        now=time.time(),
        preferred=preferred,
        pin=engine,
        hard_pin=pin.hard,
    )
    _usage._record_route(decision, timeline)
    if engine and decision.engine and decision.engine != engine:
        timeline.row("🔀", f"pin overridden: {engine} ({decision.reason})")
    # Every engine exhausted, or a hard pin's engine is: start on the preferred one and wait out its limit.
    return decision.engine or preferred


def _record_author_choice(engine: str, resumed: bool, timeline: _timeline._Timeline) -> dict[str, Any]:
    """Write admission's reason for the engine it chose, and move a resumed conversation's owner to `engine`.

    A pin can bring a project back to an engine that is not its conversation's owner. That engine
    resumes its own transcript: the owner's thread and a handoff's `fresh` plan belong to the other
    engine, so they move under `previous` where nothing reads them as this engine's. The refusal stays,
    as `_restore_refusal` applies it only to the account that refused. Returns `previous` when the
    owner moved, else {}.
    """
    if note := _policy.read("ADMISSION_NOTE"):
        if not resumed:
            timeline.row("🔀", f"admission chose {engine}: {note}")
    owner_file = _session_config.STATE_ROOT / "author-conversation.json"
    if not (resumed and owner_file.is_file()):
        return {}
    try:
        owner = json.loads(owner_file.read_text())
    except ValueError:
        return {}
    if owner.get("engine") == engine:
        return {}
    previous = {key: owner.pop(key) for key in ("engine", "thread", "fresh") if key in owner}
    _write_atomic(owner_file, {**owner, "engine": engine, **({"previous": previous} if previous else {})})
    if not previous.get("engine"):
        return {}
    timeline.row(
        "🔀",
        f"author resumes on {engine}, taking over from the {previous['engine']} conversation"
        + (f" (thread {previous['thread']}, kept)" if previous.get("thread") else ""),
    )
    return previous


async def _queue_resume_handoff(
    previous: dict[str, Any], engine: str, brief: str, timeline: _timeline._Timeline
) -> None:
    """Put a handoff briefing first in the author's pending work when a resumed project changed engine.

    That is a pin to the other engine, or the owner's credential gone. The new engine resumes its own
    older conversation, or starts fresh, and knows nothing of what the other did since: it gets the
    briefing a usage-limit handoff gives, which `project` delivers through `_resume_author_pending`.
    Only a slot with both engines keeps pending work (`AuthorSession`).
    """
    if len(_usage._author_engines()) < 2:
        return
    prompt = await _handoff_prompt(
        previous["engine"], engine, f"resumed on {engine}", brief, timeline, previous.get("thread") or ""
    )
    pending_file = _session_config.STATE_ROOT / "author-pending.json"
    try:
        pending = json.loads(pending_file.read_text())
    except FileNotFoundError:
        pending = []
    _write_atomic(pending_file, [prompt, *pending])


def _login_with_access_token(home: Path, token: str) -> None:
    """Install `[secrets] codex_token` into `home` with `codex login --with-access-token`.

    The token goes in on stdin, never argv. Chosen over writing `auth.json` by hand because the file's
    shape for an access token is Codex's to define, and over relying on Codex reading
    `CODEX_ACCESS_TOKEN` itself because `login` says at once, here, when it rejects the token.
    """
    result = subprocess.run(
        [*_CODEX_COMMAND, "login", "--with-access-token", "-c", 'cli_auth_credentials_store="file"'],
        input=token,
        env={**_session_setup._session_env(dict(os.environ)), "CODEX_HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).replace(token, "***")[-300:]
        raise flyte.errors.NonRecoverableError(
            f"codex login --with-access-token rejected the [secrets] codex_token (exit {result.returncode}): "
            f"{detail}\nStore a new access token in that secret."
        )
    auth = home / "auth.json"
    if auth.exists():
        auth.chmod(0o600)  # the token's file stays private whatever mode Codex wrote it with


def _prepare_codex_home() -> Path:
    """`$CODEX_HOME` for the author, with the credential installed, as `_run_codex` prepares a child's.

    An access token is installed with `codex login`. A ChatGPT login (`[[slots]] codex_login`) is written as
    `auth.json`; the author is its one holder allowed to refresh, so it takes the fresher of the mounted
    secret and the copy already here (restored from the checkpoint, or written by this pod): a refresh since
    the pod started spent the mounted copy's refresh token.
    """
    credential = os.environ.get(_session_config.CODEX_TOKEN_ENV_VAR, "").strip()
    if not credential:
        raise RuntimeError(
            "no Codex credential available for the author session "
            "(set [secrets] codex_token or this slot's codex_login, and deploy)"
        )
    home = _session_config.CODEX_HOME_DIR
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)  # the checkpoint carries this directory
    if not _codex_auth.is_login(credential):
        _login_with_access_token(home, credential)
        return home
    auth = home / "auth.json"
    if auth.is_file():
        credential = _codex_auth.pick_fresher_login(credential, auth.read_text())
    _auth.write_private(auth, credential)
    return home


#: How often the author's refreshed Codex login is written back to its secret. Codex rotates the refresh
#: token on every refresh, so a stored copy one rotation behind is dead rather than old.
CODEX_LOGIN_EVERY_S = 60.0

#: Digest of the Codex login last written to the secret by this process.
_stored_codex_digest = ""


async def _persist_codex_login(timeline: _timeline._Timeline) -> None:
    """Write the author's rotated Codex login back to its secret, so the next pod starts on a live chain.

    Only for a ChatGPT login (an access token never refreshes), and only a copy fresher than the mounted
    one that can still refresh: never an older, a stripped or a dead one. A login belongs to one slot
    (`_profile` refuses a shared one), so no second pod refreshes it.
    """
    global _stored_codex_digest
    secret = _session_config.CODEX_TOKEN_SECRET
    mounted = os.environ.get(_session_config.CODEX_TOKEN_ENV_VAR, "")
    if not secret or not _codex_auth.is_login(mounted):
        return
    try:
        raw = (_session_config.CODEX_HOME_DIR / "auth.json").read_text()
    except OSError:
        return
    digest = hashlib.sha256(raw.encode()).hexdigest()
    if digest == _stored_codex_digest or not _codex_auth.has_refresh_token(raw):
        return
    if raw.strip() == mounted.strip() or _codex_auth.pick_fresher_login(raw, mounted) != raw:
        _stored_codex_digest = digest
        return
    try:
        await _replace_secret(secret, raw)
    except REMOTE_ERRORS as exc:  # the session is fine; the next pod would start on a spent login
        timeline.row("⚠️", f"could not store the refreshed Codex login: {type(exc).__name__}: {exc}")
        return
    _stored_codex_digest = digest
    timeline.row("🔑", f"refreshed Codex login stored in {secret!r}")


async def _replace_secret(name: str, value: str) -> None:
    """Store `value` as secret `name`, replacing any secret of that name.

    A backend that refuses to create over an existing secret gets a delete first. A failure in between leaves
    the secret missing, which the next write repairs.
    """
    try:
        await flyte.remote.Secret.create.aio(name=name, value=value)
    except REMOTE_ERRORS:
        await flyte.remote.Secret.delete.aio(name=name)
        await flyte.remote.Secret.create.aio(name=name, value=value)


async def _codex_login_loop(timeline: _timeline._Timeline, stop: asyncio.Event) -> None:
    """Keep the stored Codex login in step with the one this session refreshes."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=CODEX_LOGIN_EVERY_S)
        except asyncio.TimeoutError:
            pass
        await _persist_codex_login(timeline)


def _start_codex_author(
    timeline: _timeline._Timeline, *, model: str, effort: str, extra_env: dict[str, str] | None = None
) -> CodexAuthor:
    """Restore the Codex author session, including any pending directions from the checkpoint.

    `extra_env` is for the session alone (the credential grants' endpoint), not for `os.environ`.
    """
    home = _prepare_codex_home()
    # The child path's options (`_run_codex`): its own container, so no sandbox, and a file login.
    args = ["--dangerously-bypass-approvals-and-sandbox", "-c", 'cli_auth_credentials_store="file"']
    if effort:
        args += ["-c", f'model_reasoning_effort="{effort}"']
    if model:
        args += ["-m", model]
    env = {
        **_session_setup._session_env(_session_setup._export_runtime_env()),
        **(extra_env or {}),
        "CODEX_HOME": str(home),
    }
    proc = CodexAuthor(
        cwd=_session_config.WORKSPACE,
        env=env,
        command=_CODEX_COMMAND,
        args=args,
        thread_file=_CODEX_THREAD_FILE,
        on_line=_session_setup._log_session_line(timeline),
    )
    timeline.row(
        "🚀",
        f"codex author ({model or 'default model'}, {effort or 'default effort'}), "
        + (f"resuming thread {proc.thread_id}" if proc.thread_id else "new thread"),
    )
    return proc


async def _wait_for_codex_crash(proc: CodexAuthor) -> int:
    """Stand in for `ClaudeProcess.wait()`: a Codex session has no process that ends with the project.

    Between turns nothing runs, so the project ends only when a turn crashes (its exit code).
    """
    while proc.returncode is None:
        await asyncio.sleep(1)
    return proc.returncode


_CODEX_SECRET_UNNAMED = "the Codex secret this slot was deployed with"


def _codex_remedy() -> tuple[str, str]:
    """How to fix a refused Codex credential: (short text for timeline rows, long text for the error).

    An access token cannot refresh, so the fix is a new token in its secret. A ChatGPT login whose refresh
    was refused needs a new `codex login --device-auth` stored in the slot's `codex_login` secret.
    """
    if _codex_auth.is_login(os.environ.get(_session_config.CODEX_TOKEN_ENV_VAR, "")):
        short = (
            f"the Codex ChatGPT login in secret `{_session_config.CODEX_TOKEN_SECRET}` was refused; "
            "sign in again with `codex login --device-auth` in a fresh CODEX_HOME and store its auth.json "
            "in that secret"
        )
        return short, f"{short[0].upper()}{short[1:]}."
    secret = (
        f"secret `{_session_config.CODEX_TOKEN_SECRET}`"
        if _session_config.CODEX_TOKEN_SECRET
        else _CODEX_SECRET_UNNAMED
    )
    short = (
        f"the Codex access token in `[secrets] codex_token` ({secret}) was refused; "
        "store a new token in that secret"
    )
    return short, f"{short[0].upper()}{short[1:]} — an access token cannot refresh."


async def _codex_login_dead(proc: CodexAuthor, timeline: _timeline._Timeline) -> None:
    """Handle a Codex author that cannot authenticate: hand over to Claude if possible, else end for good.

    Retrying cannot work (the pod would start on the same spent login), and twenty identical crashes
    end the project with nothing done. So: mark the login broken for routing, move the author to
    Claude through the usual handoff when the slot holds a Claude credential and policy allows
    (that raises `AuthorHandoffRestart` and does not return), otherwise raise a non-retryable error.
    """
    reason = proc.auth_failure
    _usage._note_codex_broken(reason)
    remedy, remedy_long = _codex_remedy()
    timeline.row("🔐", f"the Codex login is unusable: {reason[:300]}; {remedy}")
    claude = "claude" in _usage._author_engines()
    if isinstance(proc, AuthorSession) and claude:
        await proc.on_limit(None, "", reason=_usage._CODEX_LOGIN_UNUSABLE)
    # Still here: the handoff did not move the author. With Claude in the slot that is the pin (or a spent
    # handoff allowance), and saying "no other engine" would send the owner looking for a missing login.
    pin = _pins.author_pin()
    if claude and pin.hard and pin.engine == "codex":
        why = "the run is pinned to Codex, so the author does not move to Claude"
    elif claude:
        why = "the author could not be handed to Claude"
    else:
        why = "no other engine to hand over to"
    timeline.row(
        "🛑",
        f"{why}; ending without retries so the login can be replaced ({remedy})",
    )
    raise flyte.errors.NonRecoverableError(f"{remedy_long}\n\nCodex said: {reason[:500]}")


_LOGIN_EXPIRED_HELP = (
    "Claude Code reports the login has expired, so the session cannot reach the model. The setup-token "
    "was revoked or has lapsed: run `claude setup-token`, store the new token in the Flyte secret named "
    "by `[secrets] claude_token`, and relaunch."
)


def _assert_logged_in(proc: ClaudeProcess) -> None:
    """Fail the run if Claude Code says the credential has expired.

    An expired credential does not crash Claude Code. The TUI answers the first
    prompt with "Login expired - Please run /login" and then goes quiet, which
    the idle heuristic reads as a session waiting for direction. A project can
    therefore sit for hours looking healthy while being unable to reach the
    model at all.

    Non-recoverable on purpose: no number of retries revives a revoked token,
    and under `retries` this would otherwise be a long crash loop.
    """
    if not proc.login_expired:
        return
    raise flyte.errors.NonRecoverableError(
        f"{_LOGIN_EXPIRED_HELP}\n\n"
        "If this run *resumed* an earlier project of the same name, suspect the restored state "
        "first: the Claude config directory lives inside the checkpoint, so a project whose "
        "previous run was aborted mid-start can come back carrying a logged-out config, and "
        "reinstalling the credential does not repair the rest of it. Relaunch under a new "
        "`name` to start from a clean config, and the secret will be used as-is."
    )
