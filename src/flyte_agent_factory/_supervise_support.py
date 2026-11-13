"""The supervisor's helpers: board, pull request, preflight, escalation steps and the dashboard loops."""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import json
import os
import re
import shutil
import signal
import threading
import time
import traceback
from collections import Counter
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import flyte
import flyte.errors
import flyte.remote
import flyte.report
import flyte.sandbox

from flyte_agent_factory import (
    _author,
    _board,
    _checkpoint,
    _dashboard,
    _gate_tests,
    _gates,
    _inbox,
    _instructions,
    _issues,
    _policy,
    _pull_requests,
    _roles,
    _session_config,
    _session_setup,
    _timeline,
    _usage,
)

# Imported here, not where used: the deploy bundles only modules loaded at import time, and
# a lazy import left `_delegation` out of the bundle (every project died at startup).
from flyte_agent_factory._errors import REMOTE_ERRORS
from flyte_agent_factory._process import ClaudeProcess


def _tell_human_first(what: str) -> str:
    """Return the clause that has the coordinator say `what`, ending where its `ask-human` call begins.

    A Codex coordinator has no `PushNotification` tool, so it puts the line in the `ask-human` message.
    """
    if _usage._author_engine == "codex":
        return f"Say {what} in the first line of the message you send with"
    return f"Send a PushNotification saying {what}, then"


async def _grant_round(proc: ClaudeProcess, timeline: _timeline._Timeline, message: str) -> None:
    """`/review-again [why]`: one more review round after an escalation, at most twice.

    A direction, from a human or the operator. The session is not told how to signal
    one, and `flyte signal` is in its denied tools (the Python client stays reachable,
    so this is a guard, not a wall; the cost of a breach is capped at two rounds).
    """
    global _escalated, _preflight_escalated, _preflight_resumes
    if _escalated and _preflight_escalated:
        _escalated = False
        _preflight_escalated = False
        _preflight_resumes += 1
        timeline.row("🔁", "pre-review checks resumed; bounce budget reset")
        await proc.submit(
            "Pre-review checks resumed. Fix the failing checks, push, and declare DONE again.",
            label="Pre-review checks resumed",
        )
        return
    if not _escalated:
        timeline.row("⚠️", "/review-again refused: review has not escalated, so there is no round to grant")
        return
    if _session_config._granted_rounds >= _session_config._MAX_GRANTED_ROUNDS:
        timeline.row(
            "⚠️", f"/review-again refused: {_session_config._MAX_GRANTED_ROUNDS} extra rounds already granted"
        )
        return
    _session_config._granted_rounds += 1
    _escalated = False
    why = message.split(None, 1)[1] if len(message.split(None, 1)) > 1 else ""
    timeline.row(
        "🔁",
        f"one more review round granted ({_session_config._max_rounds()} in "
        f"all){': ' + why[:200] if why else ''}",
    )
    await proc.submit(
        f"One more review round has been granted (round {_session_config._max_rounds()} is now the last; the "
        "loop may stop sooner if review stops converging). "
        "Fix the findings from the last review, with a test for each, push, and declare DONE again. "
        + (f"Note from the operator: {why}" if why else ""),
        label="Extra review round granted",
    )


_NUDGE = (
    "You have gone quiet without saying you are done. If the brief is complete, "
    'run `flyte-note "DONE: <what was delivered and where>"`. If it is not, say in one line '
    "what is blocking you and then carry on with it. Nobody is watching this run, so waiting "
    "achieves nothing."
)


#: How many times in a row a DONE may be bounced by the preflight before the
#: reviewers run anyway. A check that can say no forever is a way to deadlock a
#: run on its own bug, and the reviewers will judge the work regardless.
_PREFLIGHT_MAX_BOUNCES = 5
_CHECKS_TIMEOUT_S = 15 * 60
# How long a finished checks run may keep flushing output before its pipe is closed (a seam for tests).
_READER_CLOSE_WAIT_S = 5
_board_worker: _board.Supervisor | None = None


def _checks_log_rel(name: str) -> str:
    return (
        f".claude-flyte/{name}/checks.log"
        if _session_config._safe_project_dir(name)
        else ".claude-flyte/checks.log"
    )


async def _done_preflight(workspace: Path) -> str:
    """Why a DONE is not ready to be reviewed, or "" if it is.

    Mechanical facts only -- no judgement, which is the reviewers' job. These
    are the things a session skips when handing back is the easy path: it
    declares DONE with the criteria unwritten, or with the work committed
    nowhere but this pod. Each one launches three reviewers in three
    containers to be told something a `git status` already knew, so the
    answer comes back in seconds, with the commands to run, and does not
    consume a review round.
    """
    problems: list[str] = []
    live = _session_config._acceptance_path(workspace)
    if not live.is_file() or not live.read_text(errors="replace").strip():
        problems.append(
            f"`{_session_config._acceptance_rel(_session_config._project_name)}` is missing or empty. Write "
            f"the checkable criteria the brief "
            "implies there in the ignored harness directory, "
            "and go through them one at a time."
        )
    code, _ = await _checkpoint._git_out(workspace, "rev-parse", "--git-dir")
    if code != 0:
        return " ".join(problems)

    _, dirty = await _checkpoint._git_out(workspace, "status", "--porcelain", "--untracked-files=no")
    if dirty:
        count = len(dirty.splitlines())
        problems.append(f"{count} tracked file(s) have uncommitted changes (`git status`); commit them.")
    if _gates.untracked_checks(workspace, _session_config._project_name, _session_config._layout()):
        problems.append(
            f"`{_session_config._checks_rel(_session_config._project_name, _session_config._layout())}` is "
            f"not committed; the harness directory is "
            f"git-ignored, so `git add -f "
            f"{_session_config._checks_rel(_session_config._project_name, _session_config._layout())}` and "
            f"commit it."
        )

    _, remotes = await _checkpoint._git_out(workspace, "remote")
    if remotes:
        push = _instructions._push_command(
            _session_setup._pr.branch if _session_setup._pr else "", _session_setup._default_branch
        )
        code, ahead = await _checkpoint._git_out(workspace, "rev-list", "--count", "@{u}..HEAD")
        if code != 0:
            _, branch = await _checkpoint._git_out(workspace, "rev-parse", "--abbrev-ref", "HEAD")
            problems.append(
                f"branch `{branch}` has no upstream, so nothing is pushed: "
                f"`{push if _session_setup._pr else push.replace('git push ', 'git push -u ')}`."
            )
        elif ahead != "0":
            problems.append(
                f"{ahead} commit(s) are not pushed, so they exist only on this pod: "
                f"`{push}`, then check "
                "`git rev-list --count @{u}..HEAD` prints 0."
            )
    return " ".join(problems)


def _pre_review_row(total: float, checks: float, work: _gate_tests.GateWork) -> str:
    """Return the timeline row for one pre-review: the total, and where the time went.

    The gate's time is what it spent outside the base runs; `base` is those, or "cached" when every
    one of them was read back from an earlier gate run.
    """
    lane = f" (slow lane {work.slow_seconds:.0f}s)" if work.slow_seconds else ""
    gate = work.seconds["gate"] - work.seconds["base"]
    parts = [
        f"checks {checks:.0f}s{lane}",
        f"gate {gate:.0f}s" + (" (pytest reused)" if work.pytest_reused else ""),
    ]
    if work.base_runs:
        parts.append(
            "base cached" if work.base_hits == work.base_runs else f"base {work.seconds['base']:.0f}s"
        )
    return f"pre-review {total:.0f}s: " + ", ".join(parts)


async def _run_declared_checks(
    workspace: Path,
    timeline: _timeline._Timeline,
    junit_dir: Path | None = None,
    *,
    script: str | None = None,
) -> str:
    started = time.monotonic()
    timeline.phase("checks")
    timeline.activity(f"running declared checks ({_CHECKS_TIMEOUT_S}s limit)")
    tail = bytearray()
    code = 1
    timed_out = False
    script = script or _session_config._checks_rel(_session_config._project_name, _session_config._layout())
    log_rel = _checks_log_rel(_session_config._project_name)
    log = workspace / log_rel
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_bytes(b"")

    async def capture() -> None:
        assert proc.stdout is not None
        while chunk := await proc.stdout.read(8192):
            tail.extend(chunk)
            del tail[:-12000]

    async def finish_reader() -> None:
        try:
            await asyncio.wait_for(asyncio.shield(reader), _READER_CLOSE_WAIT_S)
        except asyncio.TimeoutError:
            pass
        finally:
            reader.cancel()
            # StreamReader has no public close; escaped children may retain the
            # write end forever, so closing the subprocess pipe is necessary.
            proc._transport.close()  # type: ignore[attr-defined]  # as above: not in typeshed
            with contextlib.suppress(asyncio.CancelledError):
                await reader

    env = _session_setup._session_env(dict(os.environ))
    if junit_dir is not None:
        env[_gate_tests.PYTEST_JUNIT_ENV] = str(junit_dir)
        env[_gate_tests.SLOW_LANE_ENV] = "child"
        # In a subdirectory of the junit directory, which is removed with it. The same for every project.
        # The same lookup as the gate's own lanes: the run's image, since FLYTE_INTERNAL_IMAGE here lacks git.
        image = await asyncio.to_thread(
            _gate_tests._run_image, workspace, _gate_tests._launcher_command(Path(), [])[1]
        )
        env[_gate_tests.TEST_LANE_ENV] = str(_gate_tests.write_lane_launcher(junit_dir / "lane", image))
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash",
            script,
            cwd=str(workspace),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        tail.extend(str(exc).encode())
        log.write_bytes(tail)
    else:
        # Drain as it runs: communicate() would retain an unbounded build log.
        reader = asyncio.create_task(capture())
        try:

            async def wait_for_exit() -> int:
                # asyncio's pending wait() can also wait for pipe EOF. Polling
                # returncode lets us observe bash exiting independently of it.
                while proc.returncode is None:
                    try:
                        await asyncio.wait_for(proc.wait(), 0.05)
                    except asyncio.TimeoutError:
                        pass
                return await proc.wait()

            code = await asyncio.wait_for(wait_for_exit(), _CHECKS_TIMEOUT_S)
            try:
                await asyncio.wait_for(asyncio.shield(reader), 2)
            except asyncio.TimeoutError:
                pass
        except asyncio.TimeoutError:
            timed_out = True
            code = 124
        finally:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            await finish_reader()
            await proc.wait()
            log.write_bytes(tail)
    duration = time.monotonic() - started
    timeline.checks = {"ok": not code, "duration_s": round(duration, 1), "at": time.time()}
    timeline.phase("building")
    status = "failed" if code else "passed"
    excerpt = "".join(c for c in tail.decode(errors="replace")[-200:] if ord(c) >= 0x20)
    timeline.row("🚧" if code else "✅", f"{script} {status} in {duration:.1f}s: {excerpt}")
    if not code:
        return ""
    timeout = f" (timed out after {_CHECKS_TIMEOUT_S}s)" if timed_out else ""
    return f"`{script}` failed with exit code {code}{timeout}. Output tail: {log_rel}"


def _prompt_keys(summary: str) -> tuple[str, str]:
    """Return a permission prompt's command and its rejection reason, normalised for comparison.

    Claude Code draws the command, then a model-written description of it, then (for some
    commands) the reason it asks. The description is reworded on every retry, so it is left
    out: it is the capitalised line with no colon, as is the "Bash command" header. Commands
    starting with environment assignments can also be capitalised, so keep those. A reason
    is a capitalised line with a colon ("Dangerous rm operation on ...: <target>"), and only
    the part before the target counts. Everything but letters and digits is dropped, so a
    redraw whose spaces were squashed out compares equal to a normal one.
    """
    commands, reasons = [], []
    for line in summary.split("\n"):
        line = line.strip()
        if not line[:1].isupper() or re.match(r"[A-Za-z_][A-Za-z0-9_]*=", line):
            commands.append(line)
        elif ":" in line:
            reasons.append(line.partition(":")[0])

    def key(parts: list[str]) -> str:
        return re.sub(r"[^a-z0-9]", "", " ".join(parts).lower())

    return key(commands), key(reasons)


def _same_prompt(a: str, b: str) -> bool:
    """Whether two declined permission prompts are the same one, or a near-identical retry of it.

    The same if the commands match or the rejection reasons do (`rm -rf build/*` and
    `rm -rf "$OUT"` raise the same one). Fuzzily rather than exactly, because the renderer
    drops different letters on every redraw of the same menu (qa: six "different" prompts for
    one retried command). An empty key says nothing, so it matches nothing.
    """

    def close(x: str, y: str) -> bool:
        return bool(x and y) and difflib.SequenceMatcher(None, x, y).ratio() >= 0.8

    (command_a, reason_a), (command_b, reason_b) = _prompt_keys(a), _prompt_keys(b)
    return close(command_a, command_b) or close(reason_a, reason_b)


def _flat(summary: str, limit: int = 0) -> str:
    """Return a prompt summary on one line, cut to `limit` characters when given."""
    text = " ".join(summary.split())
    return text if not limit or len(text) <= limit else text[: limit - 3] + "..."


# Follow-ups that can recur are kept to one line under `ClaudeProcess.submit`'s 400-character
# threshold, so each is typed rather than written to a new inbox file.
_FOLLOW_UP_EXCERPT = 150


def _declined_text(summary: str) -> str:
    return (
        f"A permission prompt for {_flat(summary, _FOLLOW_UP_EXCERPT)} was declined automatically: "
        "nobody answers prompts in this terminal. Use a safer equivalent "
        "(explicit paths inside your scratch directory, no globs at risky roots) "
        "and carry on."
    )


def _change_approach_text(summary: str, count: int) -> str:
    """Return the follow-up once a session keeps retrying a declined prompt, saying what to do instead.

    Sent in full once per streak; later repeats get the one-line `_declined_again_text`.
    """
    if re.search(r"\brm\b", summary):
        alternatives = (
            "- create a fresh directory per run with `D=$(mktemp -d)` and work inside it, so nothing "
            "needs a glob to clean up;\n"
            "- delete explicit absolute paths you have checked exist, never a glob or an unset variable;\n"
            "- or in Python, resolve the path first and use `shutil.rmtree(path)` on that resolved path."
        )
    else:
        alternatives = (
            "- do the same thing with explicit absolute paths inside your scratch directory, no globs;\n"
            "- or do it from a short Python script that resolves its paths first;\n"
            "- or, if the step is not needed for the brief, skip it and carry on."
        )
    return (
        f"The same permission prompt has now been declined {count} times. It read: {_flat(summary)}\n\n"
        "Nobody answers prompts in this terminal, so it will be declined every time. "
        "Do not retry this command or a variant; use a different approach. For example:\n"
        f"{alternatives}"
    )


def _declined_again_text(summary: str, count: int) -> str:
    return (
        f"Declined again ({count} times): {_flat(summary, _FOLLOW_UP_EXCERPT)}. Do not retry this "
        "command or a variant; use a different approach, as the earlier message about it suggested."
    )


def _scattered_declines_text(summary: str, count: int) -> str:
    return (
        f"You keep hitting permission prompts that are declined ({count} in the last "
        f"{_session_config.PROMPT_DECLINE_WINDOW_S / 60:.0f} minutes; the latest: "
        f"{_flat(summary, _FOLLOW_UP_EXCERPT)}). "
        "Do not retry this command or a variant; use a different approach that needs no prompt."
    )


def _nudge_text(count: int, outstanding: str) -> str:
    """Return the first nudge is the generic one; later ones say what is actually open.

    Repeating the same sentence three times is how a stuck session learns to
    ignore it. A concrete fact -- "2 commits unpushed" -- is something it can act on.
    """
    if count <= 1:
        return _NUDGE
    state = outstanding or "nothing mechanical is outstanding, so this is a judgement call: decide it"
    return f"Reminder {count} of {_session_config.MAX_NUDGES}. {_NUDGE} Right now: {state}"


#: Set by the pump when review escalates; a grant is accepted only then, so one
#: cannot be spent early by a stray direction.
_escalated = False
#: Set by the pump when it releases the project after idle, so `project` ends with the
#: right outcome.
_released = False
#: Why, for the run's outcome line; the pump sets it with `_released`.
_release_note = "released after idle (passed review)"
_preflight_escalated = False
_preflight_resumes = 0


#: The least a review stopped by the usage limit waits before it runs again.
_REVIEW_RETRY_MIN_S = 600.0

#: Added to the time a limit lifts (the router's retry time or the account's reset) before the re-run:
#: both are estimates, and asking the instant before the limit lifts only earns another deferral.
_REVIEW_RETRY_SLACK_S = 120.0

#: How long a review that came back incomplete (a judge gave no verdict) waits before asking the
#: missing judges again, and how many times per DONE. The other judges' verdicts are kept, so a re-run
#: is cheap; a judge that is still missing after these is a problem for a human.
_REVIEW_INCOMPLETE_RETRY_S = 180.0
_REVIEW_INCOMPLETE_MAX_RERUNS = 2

#: How long ago a router's `retry at` may be and still be today's: longer than a review round takes.
_ROUTER_RETRY_PAST_S = 3600.0
_ROUTER_RETRY = re.compile(r"\bretry at (\d{1,2}):(\d{2}) UTC\b")


def _parse_router_retry_at(text: str, now: float) -> float | None:
    """Return the wall time of the router's `retry at HH:MM UTC` in `text`.

    For a deferral that reached the supervisor as text only: `_usage` reads the account's am/pm
    resets, not the router's 24-hour wording. A time up to `_ROUTER_RETRY_PAST_S` ago passed while the
    round ran (a judge deferred early) and is returned as is, for the caller's minimum wait to apply; an
    earlier one is tomorrow's.
    """
    found = _ROUTER_RETRY.search(text or "")
    if not found or int(found[1]) > 23 or int(found[2]) > 59:
        return None
    midnight = now - now % 86400
    at = midnight + int(found[1]) * 3600 + int(found[2]) * 60
    return at if at > now - _ROUTER_RETRY_PAST_S else at + 86400


def describe_rejection(qa_round: int, timeline: _timeline._Timeline) -> str:
    """Return the PR status for a rejected round, naming apart from FAIL the judges with no verdict."""
    unheard = sorted(
        j for j, o in timeline.judges.items() if o.get("outcome") in ("NO_VERDICT", "DEFERRED", "NOT_RUN")
    )
    return f"review rejected round {qa_round}" + (
        f"; no verdict from {', '.join(unheard)}" if unheard else ""
    )


_DONE_LINE = re.compile(r"^\s*DONE\b", re.IGNORECASE | re.MULTILINE)
_DONE_MARKER = re.compile(r"\bDONE:")


def _declares_done(note: str) -> bool:
    """Whether a note is the session saying it has finished.

    A note *beginning* with DONE, or any line that does, or an upper-case
    `DONE:` anywhere. The first operated run folded its
    per-criterion verdicts and the DONE into one note -- "Criteria check ...
    DONE: delivered" -- which a begins-with test ignored, so review never
    started and the run sat "idle" for eleven minutes with the work finished.
    The CLAUDE.md asks for two notes; sessions merge them, and the cost of being
    strict about it is a silent stall. The marker is upper-case with a colon so
    prose like "not done yet" does not trip it.
    """
    return bool(_DONE_LINE.search(note) or _DONE_MARKER.search(note))


_COMPACT_TIMEOUT_S = 900


async def _compact_between_rounds(
    proc: ClaudeProcess, timeline: _timeline._Timeline, stop: asyncio.Event
) -> bool:
    if _policy.read("COMPACT_BETWEEN_ROUNDS", "").lower() not in ("1", "true", "yes"):
        return True
    if _usage._author_engine == "codex":
        # `codex exec` takes no slash commands: "/compact" would reach the model as a prompt.
        timeline.row("ℹ️", "COMPACT_BETWEEN_ROUNDS skipped: a Codex coordinator cannot be compacted")
        return True
    started = time.monotonic()
    baseline = getattr(proc, "output_count", 0)
    await proc.submit(
        "/compact Keep the brief, acceptance criteria, binding decisions, and open work.",
        label="Compact before review delivery",
    )
    # A quiet terminal before the command has produced output is not completion.
    # Match the pump's busy/idle signal, including spinner output, rather than
    # depending on a version-specific English completion message.
    while not stop.is_set():
        now = time.monotonic()
        last = proc.last_output
        if (
            getattr(proc, "output_count", 0) > baseline
            and last is not None
            and now - last > _session_config.IDLE_AFTER_S
        ):
            timeline.row(
                "🗜️",
                f"context compacted before review delivery in {now - started:.1f}s; context size unavailable",
            )
            return True
        if now - started > _COMPACT_TIMEOUT_S:
            timeline.row("⚠️", "compaction did not become idle within 15m; delivering findings uncompacted")
            return False
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(0.01, _session_config.POLL_EVERY_S))
        except asyncio.TimeoutError:
            pass
    return False


async def _head_sha(workspace: Path) -> str:
    code, sha = await _checkpoint._git_out(workspace, "rev-parse", "HEAD", timeout=10)
    return sha.strip() if code == 0 else ""


BOARD_MOVE_TIMEOUT_S = 30.0


async def _move_board(
    status: str, timeline: _timeline._Timeline, *, reopen_done: bool = False, **texts: str
) -> None:
    """Write the move now; a failed write is kept in memory and retried by `_board_loop`."""
    if _board_worker is None:
        return
    # Only a move that reopens Done says so: the board's Done is otherwise final (`_board._write`).
    options = {"reopen_done": True} if reopen_done else {}
    try:
        # Bounded, and on a daemon thread, for the reasons the child listing is: the pump awaits
        # this, so a hung board write would stall the supervisor loop with it.
        await asyncio.wait_for(
            _inbox._in_daemon_thread(_board_worker.move, status, **options, **texts),
            timeout=BOARD_MOVE_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        timeline.row("⚠️", f"board update to {status} timed out after {BOARD_MOVE_TIMEOUT_S:.0f}s")
    except REMOTE_ERRORS as exc:  # the board must never stop project work
        timeline.row("⚠️", f"could not update the board: {type(exc).__name__}")


def _has_passed(timeline: _timeline._Timeline) -> bool:
    """Whether this run's own review passed once: its Done card is the run's to reopen."""
    return any(row.get("verdict") == "PASS" for row in timeline.verdicts)


def _board_issue(issue: str, emit: Callable[[str, str], object]) -> str:
    """Return the run's issue as `owner/repo#N`, or "" -- no board updates -- when it cannot name one."""
    if not issue or not (_session_config._PROFILE and _session_config._PROFILE.board):
        return issue
    try:
        normalised: str = _board.normalise_issue(issue, _session_config._PROFILE.repos.values())
        return normalised
    except ValueError as exc:
        emit("⚠️", f"{exc}; this project's board item will not be updated")
        return ""


def _engines_used() -> str:
    """Return the board's Engine field: the session's own engine, plus any a review or delegation ran on."""
    _session_config._load_usage()
    engines: str = _board.engines_used(
        e for row in _session_config._usage.values() for e in row.get("engines", [])
    )
    return engines


async def _start_board(issue: str, project_name: str, token: str, timeline: _timeline._Timeline) -> None:
    """Set up this project's board item and move it to In flight; nothing without `[board]`."""
    global _board_worker
    if not (_session_config._PROFILE and _session_config._PROFILE.board):
        _board_worker = None
        return
    if not issue:
        # A factory launch carries its issue in the queue request, not as an input: run labels
        # cannot hold `/` or `#`. `timeline.issues` has already read it when the App is configured.
        issue = timeline.issues.issue if timeline.issues else ""
        issue = issue or await asyncio.to_thread(_issues.linked_issue, _session_config._PROFILE, project_name)
    if not issue and _issues.is_framed(_session_config._project_brief):
        timeline.row("⚠️", "this brief came from a GitHub issue, but no issue is linked; no board updates")
    issue = _board_issue(issue, timeline.row)
    _board_worker = _board.supervisor(_session_config._PROFILE, token, issue, timeline.row)
    await _move_board(
        "In flight", timeline, Flyte=timeline.run_url, Branch=timeline.branch, Engine=_engines_used()
    )


_BOARD_EVERY_S = 60
#: The longest the end of a run waits for its last milestones; what is still unposted then is the
#: reconciler's (`factory-reconcile`), which derives the same ones from the run's report.
_MILESTONE_FLUSH_S = 30


async def _board_loop(timeline: _timeline._Timeline, stop: asyncio.Event) -> None:
    """Every minute, retry the board move and the issue milestones that GitHub refused earlier."""
    milestones = getattr(timeline, "issues", None)
    if _board_worker is None and milestones is None:
        return
    crashed = False
    while not stop.is_set():
        try:
            if _board_worker is not None:
                await asyncio.to_thread(_board_worker.tick)
            if milestones is not None and milestones.pending:
                await asyncio.to_thread(milestones.flush)
            crashed = False
        except (*REMOTE_ERRORS, _board.BoardError) as exc:  # a failed board tick does not stop work
            timeline.row("⚠️", f"board tick failed: {type(exc).__name__}")
        except Exception as exc:  # noqa: BLE001 -- a board bug must not end board updates; said once per streak
            traceback.print_exc()
            if not crashed:
                timeline.row("💥", f"board tick crashed: {type(exc).__name__}: {exc}")
            crashed = True
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=_BOARD_EVERY_S)


async def _flush_milestones(timeline: _timeline._Timeline) -> None:
    """Post what is left of the run's milestones, for at most `_MILESTONE_FLUSH_S`. Never raises."""
    if timeline.issues is None:
        return
    # A daemon thread, not the loop's executor: `asyncio.run` waits for executor threads on its way
    # out, so a hung post would hold the run open past the bound.
    flush = threading.Thread(target=timeline.issues.flush, name="milestones-final", daemon=True)
    flush.start()
    await asyncio.to_thread(flush.join, _MILESTONE_FLUSH_S)


async def _publish_pull_request(workspace: Path, brief: str, timeline: _timeline._Timeline) -> None:
    """Open the project's draft PR, or refresh it, just before reviewers run. Never raises."""
    if _session_setup._pr is None:
        return
    criteria = _session_config._acceptance_path(workspace)
    text = criteria.read_text(errors="replace") if criteria.is_file() else ""
    url = await asyncio.to_thread(_session_setup._pr.publish, brief, text)
    if url:
        timeline.pr_url = url
        await _move_board("In review", timeline, Engine=_engines_used())


async def _post_verdict(
    workspace: Path, timeline: _timeline._Timeline, sha: str, rounds: int, why: str = ""
) -> None:
    """Tell GitHub how review ended on `sha`: a pass when `why` is empty, else a failure for that reason."""
    if _session_setup._pr is None:
        return
    if why:
        await asyncio.to_thread(_session_setup._pr.failed, sha, why)
        return
    criteria = _session_config._acceptance_path(workspace)
    text = criteria.read_text(errors="replace") if criteria.is_file() else ""
    comment = _pull_requests.verdict_comment(
        rounds,
        sha,
        timeline.run_url,
        _dashboard.parse_criteria(text),
        _dashboard.criterion_results(timeline.notes),
    )
    await asyncio.to_thread(_session_setup._pr.passed, sha, rounds, comment)


async def _escalate_preflight(
    proc: ClaudeProcess, timeline: _timeline._Timeline, source: str, problem: str
) -> None:
    """Hand a DONE that the mechanical checks keep rejecting to a human.

    Sending it to review anyway would spend judgement on work that provably fails its own
    checks, and bouncing forever is a loop nobody sees. Escalating is the factory's rule for
    what the loop cannot resolve itself.
    """
    global _escalated, _preflight_escalated
    _escalated = True
    _preflight_escalated = True
    await _post_verdict(
        _session_config.WORKSPACE,
        timeline,
        await _head_sha(_session_config.WORKSPACE),
        0,
        f"{source} failing before review",
    )
    timeline.row("🚨", f"{source} still failing after {_PREFLIGHT_MAX_BOUNCES} bounces; escalating")
    timeline.activity(f"{source} keep failing before review -- needs a human; resume with /review-again")
    await proc.submit(
        f"Your DONE has been bounced {_PREFLIGHT_MAX_BOUNCES} times by the {source} and still fails, "
        "so stop looping and escalate. "
        + _tell_human_first("the pre-review checks do not pass")
        + ' `ask-human "<what is still failing, what you have tried, what you need '
        'decided>"` and wait for an answer. Tell the operator to resume with `/review-again`.\n\n'
        f"The last output:\n\n{problem}",
        label=f"Pre-review {source} escalated",
    )


async def _escalate_review_budget(proc: ClaudeProcess, timeline: _timeline._Timeline) -> None:
    """Return a DONE with every review round already spent: hand it to a human, never go silent.

    The DONE has already cleared any pass, so without this the project would neither be reviewed nor
    released and would hold its slot for good.
    """
    global _escalated
    _escalated = True
    timeline.row(
        "🚨", f"DONE with the review budget spent ({_session_config._max_rounds()} rounds); escalating"
    )
    timeline.phase("waiting")
    grantable = _session_config._granted_rounds < _session_config._MAX_GRANTED_ROUNDS
    resume = (
        "Tell the operator to resume with `/review-again`."
        if grantable
        else "No more review rounds can be granted: tell the operator it needs a human review or a relaunch."
    )
    timeline.activity(
        "review budget spent -- needs a human"
        + (" (`/review-again` grants a round)" if grantable else " (no more rounds can be granted)")
    )
    await proc.submit(
        f"You declared DONE, but all {_session_config._max_rounds()} review rounds (the review budget) are "
        f"used, so this "
        "change cannot be reviewed automatically. "
        + _tell_human_first("the review budget is spent")
        + ' `ask-human "<what changed since the last review and what you need decided>"` and '
        f"wait for an answer. {resume}",
        label="Review budget spent",
    )


async def _flush_loop(timeline: _timeline._Timeline, stop: asyncio.Event, every: float = 30.0) -> None:
    """Keep the report current while a long task runs.

    The bounded tasks only flushed at the end, so a demo that spent an hour
    synthesising narration showed "no report yet" throughout -- and any
    `flyte-note` the session posted was invisible until the run finished. A
    task nobody can see the inside of is a task nobody can judge.
    """
    while not stop.is_set():
        try:
            await timeline.flush()
        except Exception as e:  # noqa: BLE001 -- the report is a courtesy; the next tick tries again
            traceback.print_exc()
            _timeline._log_line(f"[report] periodic flush failed: {type(e).__name__}: {e}", err=True)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=every)
            return


async def _refresh_deliverables(timeline: _timeline._Timeline) -> None:
    """Return the workspace's HEAD and how many commits are not on its upstream, for the dashboard."""
    code, head = await _checkpoint._git_out(
        _session_config.WORKSPACE, "rev-parse", "--short", "HEAD", timeout=10
    )
    if code != 0:
        return
    timeline.commit = head.strip()
    code, ahead = await _checkpoint._git_out(
        _session_config.WORKSPACE, "rev-list", "--count", "@{u}..HEAD", timeout=10
    )
    timeline.unpushed = int(ahead.strip()) if code == 0 and ahead.strip().isdigit() else None


#: How often the dashboard re-reads the launch queue. It costs one listing of the state bucket.
_QUEUE_EVERY_S = 600.0


#: The dashboard's queue read in flight, so a hung one is not started again every refresh.
_queue_read: asyncio.Future[Any] | None = None


async def _refresh_queue(timeline: _timeline._Timeline) -> None:
    """Who is waiting for a slot, for the dashboard. Only where the profile has a `[state] uri`.

    Fail-soft: the queue is information for the human, and an unreadable bucket must not touch the
    run. It shows as empty rather than as a list that may be hours old.
    """
    if not (_session_config._PROFILE and _session_config._PROFILE.state.uri):
        return
    try:
        from flyte_agent_factory import _factory, _factory_base  # lazily: it needs a profile at import

        now = time.time()
        global _queue_read
        if _queue_read is not None and _queue_read.thread.is_alive():  # type: ignore[attr-defined]  # set by _inbox._in_daemon_thread
            # The last read is still hanging: one stuck thread, not one per refresh. Still unreadable.
            timeline.queue = []
            return
        _queue_read = _inbox._in_daemon_thread(_factory.waiting_rows, _factory_base.queue_root(), now)
        rows = await asyncio.wait_for(asyncio.shield(_queue_read), timeout=_checkpoint._QUEUE_READ_TIMEOUT_S)
    except (*REMOTE_ERRORS, ImportError) as e:  # a thread call plus a timeout; the dashboard shows it empty
        _timeline._log_line(f"[dashboard] could not read the launch queue: {type(e).__name__}: {e}")
        timeline.queue = []
        return
    timeline.queue = [
        {"position": r["position"], "name": r["name"], "since": now - r["waiting_s"]}
        for r in rows
        if "error" not in r
    ]


def _author_started_at(config_dir: Path, workspace: Path) -> float | None:
    earliest = None
    for path in _inbox._session_transcripts(config_dir, workspace):
        try:
            with path.open() as transcript:
                for line in transcript:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    stamp = _inbox._iso_ts(entry.get("timestamp")) if isinstance(entry, dict) else 0
                    if stamp > 0:
                        earliest = min(earliest, stamp) if earliest is not None else stamp
                        break
        except OSError:
            continue
    return earliest


def _author_subagent_roles(config_dir: Path, workspace: Path) -> dict[str, int]:
    """Session sub-agents per role, from the transcripts and their meta files; aliases count as their role."""
    folder = config_dir / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(workspace))
    roles: Counter[str] = Counter()
    for transcript in folder.glob("*/subagents/agent-*.jsonl"):
        # Claude Code writes `agent-<id>.meta.json` beside each transcript with the `agentType`
        # it was launched as. A missing or unreadable one still counts, as "unknown".
        try:
            meta = json.loads(transcript.with_suffix(".meta.json").read_text())
            roles[_roles.canonical_role(meta["agentType"])] += 1
        except (OSError, ValueError, KeyError, TypeError):
            roles["unknown"] += 1
    return dict(roles)


def _author_subagents(config_dir: Path, workspace: Path) -> tuple[int, int]:
    """(all session sub-agents, the `verify` ones), for readers of the older two-number fields."""
    roles = _author_subagent_roles(config_dir, workspace)
    return sum(roles.values()), roles.get("verify", 0)


async def _refresh_child_usage(timeline: _timeline._Timeline) -> None:
    from flyte import remote

    ctx = flyte.ctx()
    if ctx is None or ctx.action is None:
        return  # type: ignore[unreachable]  # outside a task `.action` is None; the stub types it as always set
    seen = timeline._child_usage_seen

    def collect() -> list[tuple[str, list[str]]]:
        found: list[tuple[str, list[str]]] = []
        for action in remote.Action.listall(for_run_name=ctx.action.run_name):
            if (
                action.name in seen
                or not str(getattr(action, "task_name", "")).endswith(".subagent")
                or str(action.phase).split(".")[-1] not in ("SUCCEEDED", "FAILED")
            ):
                continue
            try:
                report = action.get_report()
            except REMOTE_ERRORS as exc:
                _timeline._log_line(f"[usage] child report: {type(exc).__name__}", err=True)
                continue
            records = re.findall(r"\n<!--factory-usage (\{.*?\})-->", report or "", re.DOTALL)
            found.append((action.name, records))
        return found

    for name, records in await asyncio.to_thread(collect):
        for raw in records[-1:]:
            rec = json.loads(raw)
            route = rec.get("route")
            if isinstance(route, dict):
                role = route.get("role") or "subagent"
                _usage._ingest_subagent_usage(
                    f"{_usage._USAGE_MARK}{raw}-->",
                    role,
                    0,
                    timeline,
                    engine=route.get("engine", ""),
                    effort=route.get("effort", ""),
                    call_id=f"{ctx.action.run_name}/{name}",
                )
        seen.add(name)


async def _dashboard_loop(timeline: _timeline._Timeline, stop: asyncio.Event, every: float = 120.0) -> None:
    """Refresh what the dashboard shows that nothing else updates, then rewrite the report.

    The author's token total reads the whole transcript and git is a subprocess, so neither
    belongs in `render`, which runs on every status change. The forced flush also keeps the
    "time in phase" and "last activity" ages moving while the session is quiet.
    """
    last_queue = -_QUEUE_EVERY_S
    while not stop.is_set():
        try:
            await _refresh_child_usage(timeline)
        except (*REMOTE_ERRORS, *_usage._USAGE_READ_ERRORS) as exc:
            _timeline._log_line(f"[usage] child reports: {type(exc).__name__}", err=True)
        with contextlib.suppress(*_usage._USAGE_READ_ERRORS):
            split = await asyncio.to_thread(
                _usage._author_usage_split, _session_config.CONFIG_DIR, _session_config.WORKSPACE
            )
            timeline.set_author_usage(
                split,
                _usage._sum_usage(split["coordinator"], split["helpers"]),
                measured=_usage._is_coordinator_measured(split),
            )
            roles = await asyncio.to_thread(
                _author_subagent_roles, _session_config.CONFIG_DIR, _session_config.WORKSPACE
            )
            timeline.internal_by_role = roles
            timeline.internal_subagents, timeline.internal_qa_subagents = (
                sum(roles.values()),
                roles.get("verify", 0),
            )
            # Usage includes restored transcripts, so its rate must include their elapsed time too.
            started_at = await asyncio.to_thread(
                _author_started_at, _session_config.CONFIG_DIR, _session_config.WORKSPACE
            )
            if started_at is not None:
                timeline.started_at = min(timeline.started_at, started_at)
        with contextlib.suppress(OSError):  # `_checkpoint._git_out` already reports a failed git as a code
            await _refresh_deliverables(timeline)
        if time.monotonic() - last_queue >= _QUEUE_EVERY_S:
            last_queue = time.monotonic()
            await _refresh_queue(timeline)
        await timeline.flush(force=True)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=every)


async def _supervised(
    coro: Awaitable[object],
    name: str,
    timeline: _timeline._Timeline,
    stop: asyncio.Event,
    *,
    fatal: list[BaseException] | None = None,
) -> None:
    """Run a background loop and say so in the report if it dies.

    Each of these is a sense or a control: the bridge answers questions, the
    inbox receives direction, the pump reports status, the checkpoint loop is
    the only thing making the work survivable. Losing one quietly is worse than
    the failure itself -- the run carries on looking healthy while, for
    instance, no `/stop` can ever reach it again.

    The inbox is special-cased as fatal. Without it the run cannot be steered
    or ended, so a human would have to abort, and an abort discards everything
    since the last checkpoint. Better to end the run cleanly, which publishes.
    """
    # The loop runs as its own task and its failure is read as a value once it ends, not caught: the death of
    # any loop is reported however it died, and a cancellation of this supervisor cancels the loop too.
    loop_task = asyncio.ensure_future(coro)
    try:
        await asyncio.wait([loop_task])
    except asyncio.CancelledError:
        loop_task.cancel()
        raise
    if loop_task.cancelled():
        raise asyncio.CancelledError
    e = loop_task.exception()
    if e is None:
        return
    if isinstance(e, (flyte.errors.NonRecoverableError, _author.AuthorHandoffRestart)):
        # Not "a loop died" -- the run cannot continue, and treating it as one
        # cost twenty minutes of a run that was logged out: the pump raised on
        # the expired login, this handler wrote one row about it, and the run
        # carried on reporting RUNNING with nothing driving it. A fatal error
        # ends the run; it does not get downgraded to a note.
        timeline.row("🛑", f"{type(e).__name__}: {e}")
        if fatal is not None:
            fatal.append(e)
        stop.set()
        await timeline.flush(force=True)
        return
    if not isinstance(e, Exception):
        raise e  # KeyboardInterrupt, SystemExit: not a loop failure to report and survive
    traceback.print_exception(e)
    timeline.row("💥", f"the {name} loop stopped: {type(e).__name__}: {e}")
    if name == "inbox":
        timeline.row(
            "🛑",
            "without the inbox this run cannot be steered or stopped, so it is ending now "
            "while it can still publish",
        )
        stop.set()
    await timeline.flush(force=True)


#: Phases in which a child action is still going to produce something. Not only
#: RUNNING: a child waiting for a node or pulling its image is the commonest
#: long wait of all.
_IN_FLIGHT_PHASES = ("RUNNING", "QUEUED", "WAITING", "INITIALIZING")

#: Why the pump's last child listing failed, or "". Set by the pump only. It reports it once:
#: A past run read "idle for 11m -- send direction, or
#: /stop" while its own child action was RUNNING, and the swallowed failure left
#: no trace of why.
_in_flight_error = ""


def _set_in_flight_error(text: str) -> None:
    global _in_flight_error
    _in_flight_error = text


def _in_flight_children() -> tuple[int, str]:
    """How many child actions of this run are still running or queued, and why not if it cannot say.

    Distinguishes a session waiting on work it submitted from one that has
    stopped. Those look identical from outside -- no terminal output either way
    -- and nudging the first is noise while nudging the second is the only
    thing that restarts it. Blocking; the pump calls it in a thread.

    Returns `(0, error)` on any failure rather than raising: a status heuristic must never
    break the pump. The error comes back with the call rather than through module state,
    because a call the pump abandoned on its timeout can still finish later and would
    overwrite the state a newer call left.
    """
    try:
        from flyte import remote

        ctx = flyte.ctx()
        run_name = ctx.action.run_name
        own = getattr(ctx.action, "name", "a0")
        count = sum(
            1
            for action in remote.Action.listall(for_run_name=run_name)
            if action.name != own and any(p in str(action.phase) for p in _IN_FLIGHT_PHASES)
        )
    except (*REMOTE_ERRORS, AttributeError) as e:  # a status heuristic must never break the pump
        return 0, f"{type(e).__name__}: {e}"[:300]
    return count, ""


def _warn_on_low_headroom(
    timeline: _timeline._Timeline, warned_disk: bool, warned_memory: bool
) -> tuple[bool, bool]:
    """Say so in the report when disk or memory is nearly gone.

    A container killed for exceeding its limits leaves *no application log*:
    one run's first attempt ended after fifty thousand lines with nothing but a
    spinner, and whether that was memory or disk is unknowable after the fact
    without cluster access. This does not prevent the kill -- it makes the next
    one diagnosable, and gives the session a chance to clean up or ask for
    more.

    Warns once per resource, because a run that is genuinely near the limit
    would otherwise fill the report with the same row.
    """
    if not warned_disk:
        try:
            usage = shutil.disk_usage(_session_config.BASE_DIR)
            used = (usage.total - usage.free) / usage.total if usage.total else 0.0
            if used >= _session_config._DISK_WARN_AT:
                timeline.row(
                    "⚠️",
                    f"disk {used:.0%} full ({usage.free // 2**30}GiB free of "
                    f"{usage.total // 2**30}GiB). The pod is evicted without an error message "
                    "if this fills -- clear caches or move large data to a child action.",
                )
                warned_disk = True
        except OSError:
            pass

    if not warned_memory:
        used_fraction = _cgroup_memory_fraction()
        if used_fraction is not None and used_fraction >= _session_config._MEMORY_WARN_AT:
            timeline.row(
                "⚠️",
                f"memory {used_fraction:.0%} of the container limit. Exceeding it kills the pod "
                "with no error message; heavy work belongs in a child action.",
            )
            warned_memory = True

    return warned_disk, warned_memory


def _cgroup_memory_fraction() -> float | None:
    """Memory in use as a fraction of the container's limit, or None.

    Read from cgroup v2 then v1, because `psutil` reports the *node's* memory,
    which on a shared node says nothing about how close this container is to
    being killed.
    """
    for current, limit in (
        ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max"),
        (
            "/sys/fs/cgroup/memory/memory.usage_in_bytes",
            "/sys/fs/cgroup/memory/memory.limit_in_bytes",
        ),
    ):
        try:
            used = int(Path(current).read_text().strip())
            cap_raw = Path(limit).read_text().strip()
        except (OSError, ValueError):
            continue
        if cap_raw == "max":
            return None
        try:
            cap = int(cap_raw)
        except ValueError:
            continue
        # cgroup v1 reports an absurd sentinel when unlimited.
        if cap <= 0 or cap > 2**60:
            return None
        return used / cap
    return None
