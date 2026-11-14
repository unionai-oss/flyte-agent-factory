"""Direction from the human: the Union inbox, messages typed into the session, and their readers."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import json
import re
import threading
import time
import traceback
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from flyte_agent_factory import _session_config, _session_log, _timeline
from flyte_agent_factory._conditions import open_condition
from flyte_agent_factory._process import ClaudeProcess

if TYPE_CHECKING:
    from flyte_agent_factory._grants_supervisor import Credentials


def _session_feed(resume: bool) -> _session_log.TranscriptTail:
    """Return the tail of the session's transcript; same folder `_author_usage` and the checkpoint use."""
    folder = (
        _session_config.CONFIG_DIR / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(_session_config.WORKSPACE))
    )
    return _session_log.TranscriptTail(folder, skip_existing=resume)


#: What ends the project, from any channel the owner types into.
_STOP_WORDS = ("/stop", "stop", "/exit")


#: Bound on stopping the author. An exhausted or wedged session cannot take `/exit`, and a stop that
#: waits for it would leave the owner's `/stop` unanswered.
_AUTHOR_STOP_TIMEOUT_S = 60.0


def _is_stop(text: str) -> bool:
    return text.strip().lower() in _STOP_WORDS


#: A direction to run the author on another engine: `/engine X`, `/resume [on] X`, `resume [now] on X`,
#: `switch to X`. Only the whole message counts, so "please resume on claude when you can" stays ordinary
#: direction for the session rather than an instruction the harness acts on.
ENGINE_DIRECTION = re.compile(
    r"^\s*(?:/engine\s+|/resume\s+(?:on\s+)?|resume\s+(?:now\s+)?on\s+|switch\s+to\s+)(claude|codex)\s*[.!]?\s*$",
    re.I,
)


def engine_direction(text: str) -> str | None:
    """Return the engine `text` directs the author to, lower case, or None when it is not that command."""
    match = ENGINE_DIRECTION.match(text)
    return match[1].lower() if match else None


class _Stoppable(Protocol):
    async def stop(self) -> None: ...


async def _stop_author(proc: _Stoppable, timeline: _timeline._Timeline) -> None:
    try:
        await asyncio.wait_for(proc.stop(), timeout=_AUTHOR_STOP_TIMEOUT_S)
    except asyncio.TimeoutError:
        timeline.row("⚠️", f"the author did not stop in {_AUTHOR_STOP_TIMEOUT_S:.0f}s; going on without it")


async def _inbox_loop(
    proc: ClaudeProcess | asyncio.Future[ClaudeProcess],
    timeline: _timeline._Timeline,
    stop: asyncio.Event,
    grant_round: Callable[..., Awaitable[None]],
    credentials: Credentials | None = None,
    *,
    on_engine: Callable[[str], str] | None = None,
    on_late_answer: Callable[[str, str], Awaitable[None]] | None = None,
) -> None:
    """Deliver direction that arrives over Flyte conditions into the session.

    `grant_round` runs `/review-again`. It is passed in because it works on the review state that
    still lives in `agent`, which this module cannot import. `credentials` takes the owner's sealed
    credentials and `/revoke-credential` before anything else sees them (`Credentials.handle`).
    `on_engine` takes an engine direction (`engine_direction`) and returns the timeline row saying what
    was done. Without it, such a message is ordinary direction for the session.
    `on_late_answer` takes `/late-answer <condition> <text>`, the owner's answer to an ask that had already
    closed, which the factory's MCP server reroutes here (`Bridge.late_answer`); it puts it in the inbox
    itself. Without it, such a message is ordinary direction.

    `proc` may be a future for a session that does not exist yet: the loop is started before the
    session so that `/stop` needs no author. Other messages wait for the session, then arrive once.

    One condition at a time, created fresh after each message. A condition with
    no timeout costs nothing while it waits, so this adds exactly one open
    condition to the run -- as opposed to polling with a timeout, which would
    accumulate thousands of condition actions over a months-long project.
    """
    # Seed from the messages already on disk. The inbox is snapshotted, so a
    # resumed project must not overwrite `003.md` with a new message.
    inbox = _session_config.WORKSPACE / _session_config._INBOX_DIR
    seq = max((int(p.stem) for p in inbox.glob("*.md") if p.stem.isdigit()), default=0)

    # Messages go to the session in order by a task of their own, so that waiting for a session that is
    # still starting never keeps this loop from hearing the next condition -- a `/stop` among them.
    deliveries: asyncio.Queue[Callable[[ClaudeProcess], Awaitable[None]]] = asyncio.Queue()
    # A message is pending from the moment it is written until it is submitted. A previous attempt that
    # died before its session started leaves the marker behind, and this one delivers it.
    for marker in sorted(inbox.glob("*.pending")):
        deliveries.put_nowait(_deliver_direction(marker))
    # A group, so that a delivery that fails ends this loop too, and `_supervised` reports it.
    async with asyncio.TaskGroup() as group:
        delivery = group.create_task(_deliver_in_order(proc, deliveries), name="inbox-delivery")
        try:
            while not stop.is_set():
                seq += 1
                prompt = (
                    "### Send direction to this project\n\n"
                    "Signal this condition with a message for the Claude Code session. It is delivered "
                    "into the project's own conversation, so the session keeps its context. The chat agent "
                    "that started the project sends direction the same way.\n\n"
                    "Send `/stop` to finish the project: the session exits cleanly, the workspace is "
                    "snapshotted, and the run completes.\n\n"
                    "```\n"
                    "flyte signal condition <run-name> <action-name> 'your message'\n"
                    "```"
                )
                condition, seq = await open_condition(
                    "direct", seq, prompt=prompt, prompt_type="markdown", data_type=str
                )
                message = (await condition.wait.aio() or "").strip()

                if not message:
                    continue
                if _is_stop(message):
                    timeline.row("🛑", "stop requested")
                    stop.set()
                    return
                if credentials is not None and credentials.handle(message):
                    continue
                if message.split(None, 1)[0].lower() == "/review-again":
                    deliveries.put_nowait(functools.partial(grant_round, timeline=timeline, message=message))
                    continue
                late = message.split(None, 2)
                if on_late_answer is not None and len(late) == 3 and late[0].lower() == "/late-answer":
                    try:
                        await on_late_answer(late[1], late[2])
                    except OSError as e:  # a lost late answer must not end the inbox
                        timeline.row("⚠️", f"late answer for {late[1]} not delivered: {type(e).__name__}: {e}")
                    continue
                if on_engine is not None and (engine := engine_direction(message)):
                    # Acted on here, not delivered: a paused session would only queue it behind the very
                    # limit it asks to leave. Recorded so the handoff prompt tells the next engine.
                    _record_direction(message, "Union", timeline)
                    if row := on_engine(engine):
                        timeline.row("🔁", row)
                    continue

                # Written to a file for the same reason as the brief: the TUI submits on
                # newline, so anything multi-line has to arrive as a pointer.
                rel = f"{_session_config._INBOX_DIR}/{seq:03d}.md"
                (_session_config.WORKSPACE / rel).write_text(message + "\n")
                marker = _session_config.WORKSPACE / f"{_session_config._INBOX_DIR}/{seq:03d}.pending"
                marker.touch()
                _record_direction(message, "Union", timeline)
                deliveries.put_nowait(_deliver_direction(marker))
        finally:
            delivery.cancel()


def _deliver_direction(marker: Path) -> Callable[[ClaudeProcess], Awaitable[None]]:
    """Return the delivery of the inbox message `marker` stands for; the marker goes once it is submitted."""

    async def deliver(session: ClaudeProcess) -> None:
        rel = marker.relative_to(_session_config.WORKSPACE).with_suffix(".md")
        await session.submit(f"New direction arrived from Flyte. Read {rel} and act on it.")
        marker.unlink(missing_ok=True)

    return deliver


async def _deliver_in_order(
    proc: ClaudeProcess | asyncio.Future[ClaudeProcess],
    deliveries: asyncio.Queue[Callable[[ClaudeProcess], Awaitable[None]]],
) -> None:
    """Hand each queued message to the session, one at a time, once the session exists."""
    while True:
        deliver = await deliveries.get()
        # Shielded: cancelling this task must not cancel the caller's future for the session.
        await deliver(await asyncio.shield(proc) if isinstance(proc, asyncio.Future) else proc)


#: Direction from the human during the run, from Union (`direct-*`) and typed into the session
#: alike: "<source>: <text>". Authority alongside the brief for reviewers and the planner.
#: Persisted under STATE_ROOT so a restart keeps it.
_directions: list[str] = []


_DIRECTIONS_MAX = 30


#: Monotonic time of the last direction from either channel. Release waits `RELEASE_IDLE_S`
#: after it: a human who just wrote is not a human who left.
_last_direction_at: float | None = None


#: Transcript entries already read, by uuid.
_seen_human_entries: set[str] = set()


def _directions_path() -> Path:
    return _session_config.STATE_ROOT / "review" / "directions.json"


def _record_direction(text: str, source: str, timeline: _timeline._Timeline) -> None:
    """One place for human direction, whichever way it arrived."""
    body = text.strip()
    if not body:
        return
    global _last_direction_at
    _last_direction_at = time.monotonic()
    _directions.append(f"{source}: {body[:2000]}")
    # Union direction is deliberate standing direction: never pushed out by app chatter.
    extra = len(_directions) - _DIRECTIONS_MAX
    for i in range(len(_directions)):
        if extra <= 0:
            break
        if not _directions[i].startswith("Union: "):
            _directions[i] = ""
            extra -= 1
    _directions[:] = [d for d in _directions if d]
    timeline.row("📨", f"direction ({source}): {body[:160]}")
    if timeline.bridge is not None:
        # Direction that names a `decision-NNN` the factory made in the owner's place overrules it.
        timeline.bridge.overrule(body, source)
    with contextlib.suppress(OSError):
        _directions_path().parent.mkdir(parents=True, exist_ok=True)
        _directions_path().write_text(
            json.dumps({"directions": _directions, "seen": sorted(_seen_human_entries)})
        )


def _load_directions() -> None:
    try:
        saved = json.loads(_directions_path().read_text())
    except (OSError, ValueError):
        return
    # Merged, not overwritten: the app watcher may have recorded something before this ran.
    _directions[:] = [str(d) for d in saved.get("directions", []) if str(d) not in _directions] + _directions
    _seen_human_entries.update(str(u) for u in saved.get("seen", []))


def _question_registry_path() -> Path:
    """Return the open ask- questions, under STATE_ROOT so they ride the checkpoint; BRIDGE_DIR does not."""
    return _session_config.STATE_ROOT / "bridge" / "questions.json"


def _save_pass(
    round_: int,
    sha: str,
    pr_url: str = "",
    verdict_posted: bool = False,
    *,
    timeline: _timeline._Timeline | None = None,
    passed: bool = True,
    in_review: bool = False,
    review_retry_at: float | None = None,
    review_retry_why: str = "",
    incomplete_reruns: int = 0,
) -> None:
    """Keep a passed review across a pod restart, which would otherwise lose `passed_round`.

    `passed=False` keeps only the review history (verdicts, rounds, PR time) of a round that did not
    pass, or of a pass that was withdrawn: the milestones and the board are derived from it, and a restart
    must not make the next round look like the first. `in_review` preserves a round that started but has
    no verdict yet. A file without `passed` is the previous version's,
    which only ever wrote passes. `review_retry_at` is the wall time a deferred or incomplete review
    re-runs, and `review_retry_why` what it waits on: a restart while it waits would otherwise lose it, and
    nothing would re-run the review. `incomplete_reruns` counts the automatic re-runs of an incomplete
    review since the last DONE, so a restart does not reset their cap.

    Without it a restarted project never releases and holds its slot. Under STATE_ROOT so it rides
    the checkpoint, as `directions.json` does. `verdict_posted` is what makes the save-then-post order
    safe: a crash between the two leaves it false and the restart posts the verdict; a crash after the
    post but before it is set to true posts it a second time, which is harmless (the PR is already
    ready, the status is the same, and the comment is the only duplicate).
    """
    with contextlib.suppress(OSError):
        _session_config._passed_path().parent.mkdir(parents=True, exist_ok=True)
        _session_config._passed_path().write_text(
            json.dumps(
                {
                    "passed": passed,
                    "in_review": in_review,
                    "round": round_,
                    "sha": sha,
                    "at": time.time(),
                    "pr_url": pr_url,
                    "verdict_posted": verdict_posted,
                    "verdicts": list(timeline.verdicts) if timeline else [],
                    "pr_at": timeline.pr_at if timeline else None,
                    "review_round": timeline.review_round if timeline else round_,
                    "review_started_at": timeline.review_started_at if timeline else None,
                    "review_retry_at": review_retry_at,
                    "review_retry_why": review_retry_why if review_retry_at else "",
                    "incomplete_reruns": incomplete_reruns,
                }
            )
        )


def _clear_pass_keep_history() -> None:
    """Withdraw a saved pass without losing the review history needed after a restart."""
    record = _session_config._load_pass_record()
    if record and record["passed"]:
        record["passed"] = False
        with contextlib.suppress(OSError):
            _session_config._passed_path().write_text(json.dumps(record))


def _unpass(
    round_: int,
    timeline: _timeline._Timeline,
    review_retry_at: float | None = None,
    review_retry_why: str = "",
    incomplete_reruns: int = 0,
) -> None:
    """Return the work is no longer passed: a restart must not release it, but it keeps the review history.

    `review_retry_at` (and what it waits on) is saved with it when the review is to re-run by itself; None
    clears a saved one.
    """
    timeline.passed_at = None
    _save_pass(
        round_,
        "",
        timeline.pr_url,
        True,
        timeline=timeline,
        passed=False,
        review_retry_at=review_retry_at,
        review_retry_why=review_retry_why,
        incomplete_reruns=incomplete_reruns,
    )


def _in_daemon_thread(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> asyncio.Future[Any]:
    """Run `fn` on a daemon thread, awaitable.

    Unlike `asyncio.to_thread`, a call that hangs past its `wait_for` does not hold the pod open:
    `asyncio.run` and interpreter exit wait for executor threads, never for daemon ones.
    """
    loop = asyncio.get_running_loop()
    future: Any = loop.create_future()  # Any: `thread` is attached to it below
    context = contextvars.copy_context()  # as `to_thread` does: Flyte's task context comes along

    def settle(callback: Callable[..., object]) -> None:
        # A call that outlived its `wait_for` may finish after the loop closed: nobody is waiting.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(callback)

    def run() -> None:
        try:
            result = context.run(fn, *args, **kwargs)
        except BaseException as e:  # noqa: BLE001 -- handed to the awaiting coroutine, which decides
            # Bound now: `e` is unset when the except block ends, before the loop runs the callback.
            settle(lambda err=e: future.done() or future.set_exception(err))
        else:
            settle(lambda: future.done() or future.set_result(result))

    thread = threading.Thread(target=run, daemon=True, name=f"daemon-{getattr(fn, '__name__', 'call')}")
    thread.start()
    future.thread = thread  # lets a caller see a call still hanging
    return cast(asyncio.Future[Any], future)


#: Byte offset already read, per transcript file: the scan parses only what is new.
_transcript_offsets: dict[str, int] = {}


_transcript_heads: dict[str, bytes] = {}


#: Supervisor control-tag prefixes Claude Code writes as user turns.
_CONTROL_PREFIXES = ("<command-", "<local-command-", "<system-reminder", "<bash-")


#: Too short to stand alone as direction ("ok", "Merged", "*wave*"): the session still sees it.
_MIN_DIRECTION_WORDS = 3


def _session_transcripts(config_dir: Path, workspace: Path) -> list[Path]:
    """Return the TUI session's own transcripts.

    The workspace's project folder, not subagents or other cwds.
    """
    folder = config_dir / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(workspace))
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.glob("*.jsonl") if p.is_file())


def _human_exit(config_dir: Path, workspace: Path, sent: list[str], since: float) -> bool:
    # Claude Code records local exit commands only in history, not in the transcript.
    transcripts = _session_transcripts(config_dir, workspace)
    try:
        current = max(transcripts, key=lambda p: p.stat().st_mtime) if transcripts else None
        session_id = current.stem if current and current.stat().st_mtime >= since else None
        lines = (config_dir / "history.jsonl").read_text().splitlines()
    except OSError:
        return False
    newest = None
    for line in lines:
        try:
            entry = json.loads(line)
            if not isinstance(entry, dict) or entry.get("project") != str(workspace):
                continue
            if session_id is not None and entry.get("sessionId") != session_id:
                continue
            stamp = float(entry.get("timestamp", 0)) / 1000
            display = entry.get("display", "")
            if not isinstance(display, str) or stamp <= since:
                continue
            if newest is None or stamp >= newest[0]:
                newest = (stamp, display.strip().casefold())
        except (ValueError, TypeError):
            continue
    ours = {text.strip().casefold() for text in sent}
    return newest is not None and newest[1] in ("exit", "/exit") and newest[1] not in ours


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # text parts of a message with attachments
        return "\n".join(
            str(p.get("text", "")) for p in content if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


def _direction_text(text: str) -> str:
    # Claude Code adds paste wrappers to long submitted prompts, sometimes across text parts.
    text = re.sub(r"</?pasted_content\b[^>]*>|\[Pasted text #\d+[^\]]*\]", "", text)
    return " ".join(text.split())


def _human_messages(
    config_dir: Path, sent: list[str], since: float = 0.0, workspace: Path | None = None
) -> list[tuple[str, str]]:
    """Return new (uuid, text) of messages a person typed into the session: direction."""
    return [
        (uid, text) for uid, text, _, short in _human_turns(config_dir, sent, since, workspace) if not short
    ]


def _human_turns(
    config_dir: Path, sent: list[str], since: float = 0.0, workspace: Path | None = None
) -> list[tuple[str, str, float, bool]]:
    """Return new (uuid, text, typed-at, short) of messages a person typed into the session.

    Strict, because the judges treat these as authority over the brief (qa): only entries
    Claude Code itself marks as a human turn in the interactive CLI (`origin.kind == "human"`,
    `entrypoint == "cli"`) -- which excludes `claude -p` runs (entrypoint sdk-cli),
    context-compaction summaries, task notifications, meta and sidechain turns -- and never
    one of the supervisor's own submitted texts. Supervisor entries are marked seen too.

    `short` is a message under `_MIN_DIRECTION_WORDS`: too little to be direction, and already
    marked seen, but still a reply -- "yes" is how a question is most often answered.
    """
    ours = {_direction_text(t) for t in sent}
    found: list[tuple[str, str, float, bool]] = []
    for path in _session_transcripts(config_dir, workspace or _session_config.WORKSPACE):
        key = str(path)
        try:
            with path.open("rb") as fh:
                head = fh.read(256)
                # Shrunk or rewritten (its start changed): read again from the top rather than
                # resume mid-file and skip lines (qa).
                if (
                    path.stat().st_size < _transcript_offsets.get(key, 0)
                    or _transcript_heads.get(key, head) != head
                ):
                    _transcript_offsets[key] = 0
                _transcript_heads[key] = head
                fh.seek(_transcript_offsets.get(key, 0))
                chunk = fh.read()
        except OSError:
            continue
        # Only whole lines: a line still being written is read next time.
        end = chunk.rfind(b"\n") + 1
        _transcript_offsets[key] = _transcript_offsets.get(key, 0) + end
        for line in chunk[:end].decode(errors="replace").splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("isSidechain") or d.get("isMeta") or d.get("entrypoint") != "cli":
                continue
            if d.get("isCompactSummary") or d.get("isVisibleInTranscriptOnly"):
                continue
            att = d.get("attachment") or {}
            if d.get("type") == "user" and (d.get("origin") or {}).get("kind") == "human":
                raw = _content_text((d.get("message") or {}).get("content")).strip()
            elif (
                # Typed while the agent was mid-turn: stored only as a queued command, never as a
                # user turn (qa: six of the user's messages, including the request for this feature).
                d.get("type") == "attachment"
                and att.get("type") == "queued_command"
                and att.get("commandMode") == "prompt"
                and (att.get("origin") or {}).get("kind") == "human"
            ):
                raw = str(att.get("prompt") or "").strip()
            else:
                continue
            uid = str(d.get("uuid") or "")
            if not uid or uid in _seen_human_entries:
                continue
            text = _direction_text(raw)
            if not text:
                # A marker may expand later under the same uuid; never consume it as direction.
                continue
            wrapped = raw != re.sub(r"</?pasted_content\b[^>]*>|\[Pasted text #\d+[^\]]*\]", "", raw)
            for submitted in sorted(ours, key=len, reverse=True):
                if not submitted:
                    continue
                if text == submitted or (wrapped and len(text) >= 80 and text in submitted):
                    text = ""
                    break
                if len(submitted) >= 80 and submitted in text:
                    text = text.replace(submitted, "").strip()
                    # Claude Code can prefix a submitted prompt with this context wrapper.
                    text = re.sub(r"^Additional session context:\s*", "", text)
            short = len(text.split()) < _MIN_DIRECTION_WORDS
            if short:
                _seen_human_entries.add(uid)
                if not text:
                    continue
            stamp = _iso_ts(d.get("timestamp"))
            if since and stamp < since:
                continue  # history from before this process: a resumed conversation, not new direction
            if text.startswith(_CONTROL_PREFIXES):
                continue
            found.append((uid, text, stamp, short))
    return found


def _iso_ts(value: object) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


async def _watch_app_messages(
    proc: ClaudeProcess,
    timeline: _timeline._Timeline,
    stop: asyncio.Event,
    credentials: Credentials | None = None,
) -> None:
    """Record what the human types into the session as direction, the same as from Union.

    It also closes the `ask-` questions it answers (ask-a0-011 was answered in the app
    and stayed PAUSED in Union as unanswered). Union `direct-` messages do not: they are
    signalled in Union itself, beside the open question, so leaving it open there was a choice.
    """
    since = time.time()
    failing = False
    while not stop.is_set():
        try:
            new = await asyncio.to_thread(
                _human_turns, _session_config.CONFIG_DIR, list(getattr(proc, "sent_texts", [])), since
            )
            for uid, text, stamp, short in new:
                if credentials is not None and credentials.handle(text, "the Claude app"):
                    # Not direction, and not an answer to an open question: see `Credentials.handle`.
                    _seen_human_entries.add(uid)
                    continue
                if _is_stop(text):
                    # Acted on here, never typed into the session: one that is out of usage or
                    # logged out cannot take a turn, and /stop must still work.
                    _seen_human_entries.add(uid)
                    timeline.row("🛑", "stop requested (Claude app)")
                    stop.set()
                    return
                if not short:
                    _seen_human_entries.add(uid)
                    _record_direction(text, "Claude app", timeline)
                if timeline.bridge:
                    if short:
                        # Short text is not direction (`_record_direction` is the long-text path), but
                        # "Overrule decision-003." is an explicit act whatever its length; text that
                        # names no decision is ignored by `overrule`.
                        timeline.bridge.overrule(text, "Claude app")
                    await timeline.bridge.close_answered_in_session(text, "Claude app", stamp or time.time())
            failing = False
        except Exception as e:  # noqa: BLE001 -- said once, not silently, and retried
            traceback.print_exc()
            if not failing:
                timeline.row("⚠️", f"reading Claude-app direction failed: {type(e).__name__}: {e}")
            failing = True
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=15)
