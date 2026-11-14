"""The supervisor loop: `_pump` watches the author session, answers prompts and drives the review rounds."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from flyte_agent_factory import (
    _author,
    _checkpoint,
    _contract,
    _demo_checks,
    _gate_tests,
    _gates,
    _inbox,
    _review,
    _review_wait,
    _reviewer,
    _session_config,
    _session_setup,
    _supervise_support,
    _timeline,
    _usage,
)
from flyte_agent_factory._author_session import AuthorSession
from flyte_agent_factory._bridge import (
    Bridge,
)

# Imported here, not where used: the deploy bundles only modules loaded at import time, and
# a lazy import left `_delegation` out of the bundle (every project died at startup).
from flyte_agent_factory._delegation import (
    Delegations,
)
from flyte_agent_factory._errors import REMOTE_ERRORS
from flyte_agent_factory._process import ClaudeProcess

#: The longest wait between a finished session's capped rounds of resumes. Each round doubles it from
#: LIMIT_RETRY_S, so an engine that keeps refusing is tried a few times a day, not every 30 minutes.
_CAP_BACKOFF_MAX_S = 4 * 3600.0


async def _pump(
    bridge: Bridge,
    proc: ClaudeProcess,
    timeline: _timeline._Timeline,
    stop: asyncio.Event,
    *,
    kickoff: str = "",
    workspace: Path | None = None,
    brief: str = "",
    demo_package: str = "",
    pinned_acceptance: Path | None = None,
    delegations: Delegations | None = None,
) -> None:
    """Move notes into the report, and keep an activity line up to date.

    The activity line exists because `RUNNING` says nothing about whether the
    agent is working or finished an hour ago. Without it the only way to tell
    was to read the transcript, and a project you cannot read at a glance is a
    project you cannot decide about.
    """
    timeline.phase("building")
    _inbox._load_directions()
    if workspace is not None:
        # Under STATE_ROOT, which the checkpoint saves: beside the pinned criteria it was lost
        # on every pod restart and re-frozen differently (qa).
        _contract._contract_path = _session_config.STATE_ROOT / "review" / "contract.md"
    # When the open menu was first seen (None: no menu), and when it was last declined.
    menu_seen_at: float | None = None
    declined_at: float | None = None
    # The current streak of declines of one prompt (see `PROMPT_DECLINE_WINDOW_S`): the last
    # prompt's text, how many, and whether a human has been told about this streak.
    streak_menu: str | None = None
    streak_count = 0
    streak_escalated = False
    # Every decline in the window, whichever prompt, and whether a human has been told of them.
    decline_times: list[float] = []
    scattered_escalated = False
    prompt_stuck = False
    # The message to send about the last decline, and its label, and when it was declined.
    permission_followup: tuple[str, str, float] | None = None
    last_seen = time.monotonic()
    nudges = 0
    warned_disk = False
    warned_memory = False
    in_flight = 0
    # -inf, not 0: `time.monotonic()` counts from boot, and 0 held the first listing on a fresh host.
    last_children_check = float("-inf")
    # The listing in flight: its daemon-thread future (whose `.thread` may outlive the timeout),
    # the task bounding it, and since when listings have failed without a good result.
    children_call: asyncio.Future[Any] | None = None
    listing_threads: list[asyncio.Future[Any]] = []  # listing calls whose thread may still be alive
    children_started_at = float("-inf")
    children_task: asyncio.Task[tuple[int, str]] | None = None
    children_failing_since: float | None = None
    children_error_reported = False
    ever_noted = False
    kickoff_retries = 0
    qa_round = 0
    # Restored after a pod restart. Quiet time starts over from here (conservative), and
    # `declared_done` follows from the pass so the session is not nudged for finished work.
    restored = _session_config._load_pass_record()
    passed_round: int | None = restored["round"] if restored and restored["passed"] else None
    declared_done = passed_round is not None
    if restored:
        timeline.review_passed = restored["passed"]
        if restored["passed"]:
            timeline.phase("done")  # what the dashboard showed before the restart, not "building"
        qa_round = restored["round"]  # the verdict comment and the round ceiling carry on from here
        # A pass in a granted round means those grants were used: without them the restored round sits at
        # the ceiling and every /review-again is spent raising it back to where it already was.
        _session_config._granted_rounds = max(
            _session_config._granted_rounds,
            min(_session_config._MAX_GRANTED_ROUNDS, qa_round - _session_config._QA_MAX_ROUNDS),
        )
        timeline.pr_url = timeline.pr_url or restored["pr_url"]
        timeline.verdicts = restored["verdicts"]
        timeline.pr_at = restored["pr_at"]
        timeline.review_round = restored["review_round"]
        timeline.review_started_at = restored["review_started_at"]
        if restored["in_review"]:
            timeline.phase("review", round_=timeline.review_round)
            timeline._phase["since"] = timeline.review_started_at
        if restored["passed"] and not restored["verdict_posted"] and _session_setup._pr is not None:
            # The pod died between saving the pass and posting it: the PR is still a draft with no status.
            try:
                # The one caller of `_pump` always passes WORKSPACE.
                await _supervise_support._post_verdict(
                    cast(Path, workspace), timeline, restored["sha"], restored["round"]
                )
            except REMOTE_ERRORS as e:  # best effort at startup; a crash here would recur on every restart
                timeline.row("⚠️", f"could not post the saved verdict: {type(e).__name__}: {e}"[:300])
            _inbox._save_pass(
                restored["round"], restored["sha"], timeline.pr_url, verdict_posted=True, timeline=timeline
            )
    review_sha = ""  # the commit the review in flight is judging
    started_at = time.time()
    qa_task: asyncio.Task[tuple[bool, str, bool | None, str]] | None = None
    compact_task = None
    compact_round = None
    preflight_bounces = 0
    exhausted_announced = False
    parked_at: float | None = None
    parked_wall = 0.0
    park_notice_id = ""  # the owner notice's own question: open by design, so it must not hold the slot
    last_heartbeat = time.monotonic()

    seen_preflight_resumes = _supervise_support._preflight_resumes
    seen_grants = _session_config._granted_rounds
    seen_limit: float | None = None
    limited_since: float | None = None
    last_resume = 0.0
    output_at_resume = 0
    retries = 0  # "carry on" sent in this limit episode
    reset_at: float | None = None  # wall time the limit resets, when the notice says
    ended_at: float | None = None
    ended_limit_text = ""  # the notice of the episode that last ended: a redraw of it is stale
    review_wait = _review_wait.ReviewWait.restore_from(restored, workspace)  # a review that re-runs by itself
    cap_announced = False
    # A finished session's capped rounds in this episode, which double the wait before the next one, and
    # how many of them in a row found no known reset anywhere (#125).
    cap_rounds = 0
    unknown_rounds = 0
    no_reset_asked = False  # the "keeps refusing with no reset time" question is asked once per episode
    last_noted_submission = None
    interrupted_message = ""
    ended_submission = None
    resume_submission = None
    resumed_notice: str | None = None  # the notice the last AuthorSession resume answered
    resumed_at = 0.0
    resumed_after_reset = True  # that resume was sent at or after the refused engine's own reset
    refused_reset: float | None = None  # the refused engine's own reset, from its notice
    handled_hit = None  # the refusal the AuthorSession block last acted on

    def holds_slot() -> bool:
        """Whether something a human or a job is still waiting on keeps this session from releasing.

        Shared by both releases (parked, and idle after review), so they cannot drift apart again.
        """
        return bool(
            qa_task is not None
            or compact_task is not None
            or review_wait.at is not None
            or in_flight
            or _supervise_support._in_flight_error
            # A pending listing means the count above may be from before it: wait for it.
            or children_task is not None
            or (delegations is not None and (delegations.active or delegations.pending))
            or menu_open
            or permission_followup
            # Past its deadline nobody is deciding it, even if its close failed.
            or any(
                not q["timed_out"] and q.get("deadline_at", float("inf")) > time.time()
                for q_id, q in bridge.questions.items()
                if q_id != park_notice_id
            )
        )

    try:
        while not stop.is_set():
            notes = bridge.drain_notes()
            if isinstance(proc, AuthorSession):
                proc.declared_done = declared_done  # the handoff's briefing says whether the work was done
            hit = getattr(proc, "limit_hit_at", None)
            if hit is not None and hit != seen_limit:
                seen_limit = hit
                text_now = getattr(proc, "limit_text", "")
                submission = getattr(proc, "last_submission", None)
                notice_reset = _usage._reset_at(text_now)
                same_notice = _usage._same_limit_notice(text_now, ended_limit_text)
                if (
                    limited_since is None
                    and ended_at is not None
                    and same_notice
                    and (
                        time.monotonic() - ended_at <= 300
                        or notice_reset is not None
                        and notice_reset <= time.time()
                    )
                    and (submission == ended_submission or submission == resume_submission)
                ):
                    # A recent redraw is ambiguous; a later refused direction is a new episode.
                    pass
                elif limited_since is None:
                    interrupted_message = ""
                    if (
                        submission
                        and submission != last_noted_submission
                        and submission != resume_submission
                        and getattr(proc, "output_count", 0) - submission[2]
                        < _session_config.LIMIT_RESUMED_CHARS
                        and not submission[0].startswith("Your usage limit")
                        and "Reply with just: ok" not in submission[0]
                        and _supervise_support._NUDGE not in submission[0]
                    ):
                        interrupted_message = submission[0]
                    limited_since = time.monotonic()
                    last_resume = time.monotonic()
                    output_at_resume = getattr(proc, "output_count", 0)
                    retries = 0
                    cap_announced = False
                    cap_rounds = unknown_rounds = 0
                    no_reset_asked = False
                    restored = getattr(proc, "limit_restored", None)
                    from_restart = bool(restored and restored[0] == hit)
                    # A refusal saved by a previous run keeps its reset and its meters as they are.
                    reset_at = (
                        cast(tuple[float, float], restored)[1]
                        if from_restart
                        else _usage._reset_at(getattr(proc, "limit_text", ""))
                    )
                    timeline.phase("limited", limited_until=reset_at)
                    if not from_restart:
                        # Only a reset still ahead is recorded: a clock time already past is stale, and
                        # recording it would mark the engine usable at once (#125).
                        fresh = _usage._refusal_until(
                            getattr(proc, "limit_text", ""), getattr(proc, "limit_seen_wall", None)
                        )
                        _usage._note_session_limit(
                            fresh, timeline, getattr(proc, "meter", None), getattr(proc, "limit_text", "")
                        )
                    reset = getattr(proc, "limit_text", "") or "reset time not shown"
                    timeline.row(
                        "🚨",
                        f"{_usage._author_engine.capitalize()} usage limit reached ({reset}); "
                        "the session is paused and will be resumed automatically after it resets",
                    )
                elif submission and submission != resume_submission and submission[1] <= hit:
                    # A direction refused during the pause supersedes the original findings.
                    interrupted_message = (
                        submission[0]
                        if submission != last_noted_submission
                        and getattr(proc, "output_count", 0) - submission[2]
                        < _session_config.LIMIT_RESUMED_CHARS
                        and not submission[0].startswith("Your usage limit")
                        and "Reply with just: ok" not in submission[0]
                        and _supervise_support._NUDGE not in submission[0]
                        else ""
                    )
                if limited_since is not None and retries:
                    # Ignore the refusal's echo, but allow subsequent work to end the pause.
                    output_at_resume = getattr(proc, "output_count", 0)
            if (
                limited_since is not None
                and isinstance(proc, AuthorSession)
                and proc.limit_hit_at != proc._resumed_hit
                and proc.limit_hit_at != handled_hit
            ):
                # Once per distinct refusal: the handoff's answer does not change between ticks.
                handled_hit = proc.limit_hit_at
                limit_text = getattr(proc, "limit_text", "")
                notice_reset = _usage._reset_at(limit_text)
                if (
                    resumed_notice is not None
                    and getattr(proc, "last_submission", None) == resume_submission
                    and resumed_after_reset
                    and _usage._same_limit_notice(limit_text, resumed_notice)
                    and (
                        time.monotonic() - resumed_at <= 300
                        or notice_reset is not None
                        and notice_reset <= time.time()
                    )
                ):
                    # Same ambiguity as `same_notice` above: the TUI redrawing the notice that the resume
                    # already handled is not a refusal. Pausing on it would leave nothing to deliver again.
                    proc.unpause()
                else:
                    if getattr(proc, "last_submission", None) == resume_submission:
                        # The resume itself was refused, so what it drained was never delivered.
                        proc.restore_drained()
                    refused_reset = notice_reset
                    proc.pause()
                    # This refusal's own reset, never `reset_at`: after a deferral that is the retry time,
                    # often already past, and it overwrote the engine's known reset (#125).
                    fresh = _usage._refusal_until(limit_text, getattr(proc, "limit_seen_wall", None))
                    if await proc.on_limit(fresh, interrupted_message):
                        limited_since = None
                        seen_limit = None
                        ended_at = None
                        interrupted_message = ""
                        last_seen = time.monotonic()
                        timeline.phase("done" if declared_done else "building")
                    else:
                        interrupted_message = ""
                        reset_at = proc.retry_at
                        if fresh is not None:
                            cap_rounds = unknown_rounds = 0  # a real reset to wait for: no backoff needed
                        timeline.phase(
                            "limited", limited_until=reset_at, paused=timeline._phase.get("paused")
                        )
            if limited_since is not None:
                # A small redraw after a refusal is not evidence of progress. A later
                # unrefused submission can still receive a short, valid answer.
                submission = getattr(proc, "last_submission", None)
                if submission and submission[1] > last_resume and submission[1] > seen_limit:
                    last_resume = submission[1]
                    output_at_resume = submission[2]
                    interrupted_message = ""
                grown = getattr(proc, "output_count", 0) - output_at_resume
                # After a "carry on", any answer with no fresh notice for a minute also ends it: a
                # finished session answers briefly, and under 4 KB that used to keep the episode
                # (and a carry-on every 30 minutes) alive forever (qa on limit-resume-after-done).
                # A refusal echoes and redraws the notice, so a short answer only counts
                # when the latest submission followed that notice.
                # Never before the stated reset: a past run read a carry-on at 23:09 as a resume
                # when the limit lifted at 1:50am, and stopped retrying.
                after_reset = reset_at is None or time.time() >= reset_at
                limit_seen_at = cast(float, seen_limit)  # set by the same pass that set `limited_since`
                answered = (
                    grown > 0
                    and bool(submission and submission[1] > seen_limit)
                    and time.monotonic() - max(last_resume, limit_seen_at) > 60
                    and after_reset
                )
                # Once work has resumed, a new apparent episode may itself be a
                # redraw. Substantial autonomous output can clear it without a submit.
                if (
                    hit == seen_limit
                    and (
                        (
                            grown >= _session_config.LIMIT_RESUMED_CHARS
                            and (
                                ended_at is not None
                                or retries > 0
                                or bool(submission and submission[1] > seen_limit)
                            )
                        )
                        or answered
                    )
                    and after_reset
                    and time.monotonic() - limit_seen_at > 60
                ):
                    limited_since = None
                    proc._submitted_since_limit = True  # re-arm: the next notice is a new episode
                    timeline.row("✅", "the session is running again after the usage limit")
                    timeline.phase("done" if declared_done else "building")
                    if isinstance(proc, AuthorSession):
                        # The pause outlives the notice it came from; without this every later
                        # submit only queues. Delivered before `ended_submission` is read, so the
                        # drain counts as the submission a redraw of the old notice is judged against.
                        if proc.pending:
                            await proc.resume(
                                "Your usage limit has cleared; here are the messages queued meanwhile.",
                                label="Usage limit cleared: delivering queued messages",
                            )
                        else:
                            proc.unpause()
                    ended_at = time.monotonic()
                    ended_limit_text = getattr(proc, "limit_text", "")
                    ended_submission = getattr(proc, "last_submission", None)
                    if declared_done:
                        timeline.activity("done -- the usage limit has cleared")
            if limited_since is not None:
                # Set by the author handoff when it deferred: which engine is awaited, until when, and why.
                paused = timeline._phase.get("paused") or {}
                awaited = (
                    f"; waiting for {paused['engine']} until "
                    + (_author._when(paused["until"]) if paused.get("until") else "its reset")
                    if paused.get("engine")
                    else ""
                )
                timeline.activity(
                    f"paused on the {_usage._author_engine.capitalize()} usage limit for "
                    f"{(time.monotonic() - limited_since) / 60:.0f}m "
                    f"({getattr(proc, 'limit_text', '') or 'reset time not shown'}{awaited}) -- "
                    + (
                        "needs a human: two resumes did not take"
                        if cap_announced
                        else "resumes automatically"
                    )
                )
                out_at = getattr(proc, "last_output", None)
                terminal_quiet = out_at is None or time.monotonic() - out_at > 60
                # Even when declared done: a limit notice only follows something we sent, so the
                # session was mid-task. A past run had passed review,
                # hit the limit handling a wrap-up direction, and sat paused 3+ hours past the reset.
                # A finished session gets two tries at a time. Past that the router is asked again; a human
                # is called only when no engine can run, never while one can or has a known reset (#125).
                capped = declared_done and retries >= 2
                # Set when the cap's routing call is this attempt's, so the resume below does not repeat it.
                routed_at_cap = False
                cap_wait = min(_session_config.LIMIT_RETRY_S * 2**cap_rounds, _CAP_BACKOFF_MAX_S)
                # The backoff is for a reset nobody knows. One a meter learns during the wait, for an engine
                # the coordinator may use, ends it a minute after that reset rather than hours later.
                since_resume = time.monotonic() - last_resume
                resumed_wall = time.time() - since_resume
                known_resets: Callable[[], list[float]] | None = (
                    getattr(proc, "known_resets", None) if capped else None
                )
                reset_passed = known_resets is not None and any(
                    resumed_wall < at <= time.time() - 60 for at in known_resets()
                )
                if capped and (since_resume >= cap_wait or reset_passed):
                    last_resume = time.monotonic()
                    cap_rounds += 1
                    if not isinstance(proc, AuthorSession):
                        # No router to ask: this process has only its own engine.
                        if not cap_announced:
                            cap_announced = True
                            timeline.row(
                                "🚨", "still on the usage limit after two tries; a human needs to look"
                            )
                    elif await proc.on_limit(None, ""):
                        # The other engine was open: the switch restarts the pod, so this returns only when
                        # the handoff chose to stay after all.
                        limited_since = None
                        seen_limit = None
                        timeline.phase("done" if declared_done else "building")
                    else:
                        deferred = (proc.retry_at or 0) > time.time()
                        # Only a deferral records a current pause; after a decision to stay it is stale.
                        paused = (timeline._phase.get("paused") or {}) if deferred else {}
                        if paused.get("needs_human"):
                            if not cap_announced:
                                cap_announced = True
                                timeline.row(
                                    "🚨",
                                    "still on the usage limit after two tries; a human needs to look: "
                                    + paused["needs_human"],
                                )
                        else:
                            # An engine can run now, or has a reset to wait for: two more tries once it does.
                            # With no known reset anywhere, the engine may refuse for good: after three such
                            # rounds a human is told once, and the backed-off retries go on.
                            unknown_rounds = 0 if getattr(proc, "reset_known", True) else unknown_rounds + 1
                            if unknown_rounds >= 3 and not no_reset_asked:
                                no_reset_asked = True
                                _author._ask_human(
                                    timeline,
                                    _author._no_reset_text(
                                        _usage._author_engine, getattr(proc, "limit_pin", None)
                                    ),
                                )
                            cap_announced = False
                            retries = 0
                            capped = False
                            routed_at_cap = True
                            if deferred:
                                reset_at = proc.retry_at
                                timeline.phase("limited", limited_until=reset_at, paused=paused)
                # A direction to resume on another engine re-routes now, not at the next reset.
                reroute = isinstance(proc, AuthorSession) and getattr(proc, "reroute_requested", False)
                due = (
                    reroute
                    or routed_at_cap
                    or time.monotonic() - last_resume >= _session_config.LIMIT_RETRY_S
                    or (reset_at is not None and time.time() >= reset_at + 60 and retries == 0)
                )
                resume_now = (
                    due
                    and not notes
                    and terminal_quiet
                    and not capped
                    and (reroute or reset_at is None or time.time() >= reset_at + 60)
                )
                if resume_now:
                    if isinstance(proc, AuthorSession) and not routed_at_cap:
                        # `reset_at` may be the other engine's reset, so this engine can still be limited.
                        # Re-route first: hand over if the other engine is open now, and resume this one
                        # only when its own reset has arrived.
                        last_resume = time.monotonic()  # also keeps a deferral from re-routing every tick
                        proc.reroute_requested = False
                        if await proc.on_limit(None, ""):
                            limited_since = None
                            seen_limit = None
                            ended_at = None
                            interrupted_message = ""
                            last_seen = time.monotonic()
                            timeline.phase("done" if declared_done else "building")
                            resume_now = False
                        elif (proc.retry_at or 0) > time.time():
                            reset_at = proc.retry_at
                            # Keep the pause the handoff recorded: a phase without it clears it.
                            timeline.phase(
                                "limited", limited_until=reset_at, paused=timeline._phase.get("paused")
                            )
                            resume_now = False
                if resume_now:
                    retries += 1
                    last_resume = time.monotonic()
                    output_at_resume = getattr(proc, "output_count", 0)
                    timeline.row("🔁", "trying to resume after the usage limit")
                    message = "Your usage limit should have reset now. Carry on exactly where you left off."
                    if interrupted_message:
                        # submit() stores long messages in the inbox, including a replay.
                        previous = interrupted_message
                        message = (
                            "Your usage limit has reset. You were interrupted before acting on my last "
                            "message; here it is again:\n\n" + previous
                        )
                    if isinstance(proc, AuthorSession):
                        resumed_notice = getattr(proc, "limit_text", "")
                        resumed_at = time.monotonic()
                        resumed_after_reset = refused_reset is None or time.time() >= refused_reset
                        await proc.resume(message, label="Usage limit reset: resuming")
                    else:
                        await proc.submit(message, label="Usage limit reset: resuming")
                    resume_submission = getattr(proc, "last_submission", None)
            if _supervise_support._preflight_resumes != seen_preflight_resumes:
                seen_preflight_resumes = _supervise_support._preflight_resumes
                preflight_bounces = 0
                declared_done = False
                passed_round = None
                timeline.review_passed = False
                _inbox._clear_pass_keep_history()
                exhausted_announced = False
                nudges = 0
                timeline.activity("pre-review checks resumed -- the session is fixing")
            if _session_config._granted_rounds != seen_grants:
                # A granted round re-opens the work: the session is fixing again, so
                # status and nudging must come back rather than stay "stuck".
                seen_grants = _session_config._granted_rounds
                declared_done = False
                passed_round = None
                timeline.review_passed = False
                _inbox._clear_pass_keep_history()
                exhausted_announced = False
                nudges = 0
                timeline.phase("building")
                timeline.activity(
                    f"extra review round granted -- the session is fixing (round "
                    f"{_session_config._max_rounds()} is last)"
                )
            for note in notes:
                timeline.row("📝", note)
                last_noted_submission = getattr(proc, "last_submission", None)
                last_seen = time.monotonic()
                if limited_since is not None and not isinstance(proc, AuthorSession):
                    limited_since = None  # it spoke, so the limit has lifted
                    if hasattr(proc, "_submitted_since_limit"):
                        proc._submitted_since_limit = True  # re-arm: the next notice is a new episode
                    timeline.row("✅", "the session is running again after the usage limit")
                    timeline.phase("building")
                    ended_at = time.monotonic()
                    ended_limit_text = getattr(proc, "limit_text", "")
                    ended_submission = getattr(proc, "last_submission", None)
                # A declared completion outranks the idle heuristic: it is the
                # agent's own statement rather than an inference from silence.
                # The agent spoke, so it is not parked -- whatever it says. Any
                # earlier nudges were answered and the allowance is restored.
                nudges = 0
                exhausted_announced = False
                prompt_stuck = False
                ever_noted = True
                if _supervise_support._declares_done(note):
                    # The work changed after any pass, and a bounce below sends no review: release
                    # then needs a review that passes.
                    passed_round = None
                    timeline.review_passed = False
                    _inbox._clear_pass_keep_history()
                    # Mechanical readiness before the expensive judgement.
                    if workspace and qa_task is None and not _supervise_support._escalated:
                        checks_started = time.monotonic()
                        # A repair attempt needs the whole checklist, even when the first check fails.
                        problems: list[str] = []
                        # A bounce is counted once: preflight when a declared check or the gate failed,
                        # otherwise "done" (not ready: no criteria, no checks script, demo package).
                        check_failed = False
                        demo_problem = await _demo_checks._demo_package_problem(workspace, demo_package)
                        if demo_problem:
                            problems.append(demo_problem)
                        not_ready = await _supervise_support._done_preflight(workspace)
                        if not_ready:
                            problems.append(not_ready)
                        # checks.sh leaves its pytest run here; the gate reads it instead of repeating it,
                        # unless this round changed what decides that run's results (checks.sh, conftest).
                        harness = _gates.changed_test_harness(
                            workspace,
                            _session_config._project_name,
                            _gates.resolve_base(workspace, _session_config._project_name),
                        )
                        work = _gate_tests.GateWork(
                            junit_dir=Path(tempfile.mkdtemp(prefix="factory-junit-")),
                            checks_started=time.time(),
                            reuse_refused=", ".join(harness),
                            child_lanes=True,
                        )
                        checks_seconds = 0.0
                        if (
                            workspace
                            / _session_config._checks_rel(
                                _session_config._project_name, _session_config._layout()
                            )
                        ).is_file():
                            checks_problem = await _supervise_support._run_declared_checks(
                                workspace, timeline, work.junit_dir
                            )
                            checks_seconds = time.monotonic() - checks_started
                            if checks_problem:
                                problems.append(checks_problem)
                                check_failed = True
                        else:
                            checks_rel = _session_config._checks_rel(
                                _session_config._project_name, _session_config._layout()
                            )
                            problems.append(
                                f"Write `{checks_rel}` with the bash commands "
                                "that prove the build and tests pass, run from the workspace root."
                            )
                        # Fetching vendored upstream blocks: it is done here, off the loop, so the standards
                        # selection below (on the loop) only reads the cache.
                        await asyncio.to_thread(_session_config.warm_vendored_cache)
                        gate_problem = await asyncio.to_thread(
                            _gates.run,
                            workspace,
                            _session_config._project_name,
                            _session_config._project_brief,
                            _session_config._demo_kind,
                            note,
                            _session_config._applicable_standards(),
                            timeline,
                            _session_config._layout(),
                            list(_inbox._directions),
                            work,
                            _session_config._project_repo,
                        )
                        if work.junit_dir:
                            shutil.rmtree(work.junit_dir, ignore_errors=True)
                        if gate_problem:
                            problems.append(gate_problem)
                            check_failed = True
                        timeline.row(
                            "🚧",
                            _supervise_support._pre_review_row(
                                time.monotonic() - checks_started, checks_seconds, work
                            ),
                        )
                        if work.harness_faults and not problems:
                            declared_done = True
                            await _supervise_support._escalate_harness_fault(
                                proc, timeline, "\n".join(work.harness_faults)
                            )
                            continue
                        if problems and work.harness_faults:
                            problems.append(
                                "The gate also hit a fault of the factory's (not yours; the next DONE reruns "
                                "the gate): " + "; ".join(work.harness_faults)
                            )
                        if problems:
                            failures = "\n".join(f"{i}. {problem}" for i, problem in enumerate(problems, 1))
                            if preflight_bounces >= _supervise_support._PREFLIGHT_MAX_BOUNCES:
                                declared_done = True
                                await _supervise_support._escalate_preflight(
                                    proc, timeline, "pre-review checks", failures
                                )
                                continue
                            preflight_bounces += 1
                            if check_failed:
                                timeline.preflight_bounces += 1
                                kind = "pre-review checks failed"
                            else:
                                kind = "DONE bounced before review"  # counted as a "done" bounce
                            timeline.row("🚧", f"{kind} ({preflight_bounces}): {failures[:300]}")
                            await proc.submit(
                                "Not done yet, and no review round was used:\n"
                                f"{failures}\nFix these, then say DONE again.",
                                label="Pre-review checks failed",
                            )
                            continue
                        preflight_bounces = 0
                    if (
                        workspace
                        and qa_task is None
                        and qa_round >= _session_config._max_rounds()
                        and not _supervise_support._escalated
                    ):
                        await _supervise_support._escalate_review_budget(proc, timeline)
                        continue
                    # The session does not get to decide it is finished. A review
                    # runs in its own container against the brief, and only its
                    # verdict ends the work -- started here rather than awaited,
                    # so a review taking twenty minutes does not deafen this loop
                    # to `/stop`.
                    if (
                        workspace
                        and qa_task is None
                        and qa_round < _session_config._max_rounds()
                        and not _supervise_support._escalated
                    ):
                        review_wait = _review_wait.ReviewWait()  # this review supersedes a waiting one
                        qa_round += 1
                        passed_round = None
                        timeline.phase("review", round_=qa_round)
                        _inbox._save_pass(
                            qa_round,
                            "",
                            timeline.pr_url,
                            True,
                            timeline=timeline,
                            passed=False,
                            in_review=True,
                        )
                        await _supervise_support._move_board(
                            "In review",
                            timeline,
                            reopen_done=_supervise_support._has_passed(timeline),
                            Engine=_supervise_support._engines_used(),
                        )
                        await _supervise_support._publish_pull_request(workspace, brief, timeline)
                        review_sha, timeline.reviewed_sha = await _supervise_support._head_sha(workspace), ""
                        acceptance = (
                            _contract._pin_acceptance(workspace, pinned_acceptance, timeline)
                            if pinned_acceptance
                            else ""
                        )
                        qa_task = asyncio.create_task(
                            _review._review_round(workspace, brief, acceptance, qa_round, timeline),
                            name=f"qa-{qa_round}",
                        )
                        timeline.activity(f"declared done -- QA round {qa_round} reviewing")
                    elif not workspace:
                        declared_done = True
                        timeline.activity(f"{note.strip()} -- send direction, or `/stop` to finish")

            # Not `declared_done`: that stays true after any earlier pass, and cleared the retry of every
            # review deferred after one. A new review launch clears it (above); escalation supersedes it.
            if review_wait.at is not None and _supervise_support._escalated:
                review_wait.at = None
                review_wait.save(qa_round, timeline)
            if review_wait.at is not None and qa_task is None and workspace and time.time() >= review_wait.at:
                review_wait.at = None
                qa_round += 1
                timeline.phase("review", round_=qa_round)
                _inbox._save_pass(
                    qa_round,
                    "",
                    timeline.pr_url,
                    True,
                    timeline=timeline,
                    passed=False,
                    in_review=True,
                    incomplete_reruns=review_wait.incomplete_reruns,
                )
                await _supervise_support._move_board(
                    "In review",
                    timeline,
                    reopen_done=_supervise_support._has_passed(timeline),
                    Engine=_supervise_support._engines_used(),
                )
                review_sha, timeline.reviewed_sha = await _supervise_support._head_sha(workspace), ""
                acceptance = (
                    _contract._pin_acceptance(workspace, pinned_acceptance, timeline)
                    if pinned_acceptance
                    else ""
                )
                timeline.row("🔁", f"re-running the review after {review_wait.why}")
                qa_task = asyncio.create_task(
                    _review._review_round(workspace, brief, acceptance, qa_round, timeline),
                    name=f"qa-{qa_round}",
                )
            if qa_task is not None and qa_task.done() and (compact_task is None or compact_task.done()):
                try:
                    passed, findings, converging, circling_why = qa_task.result()
                    # What `_evaluate` snapshotted; the HEAD read at launch if it never got that far.
                    review_sha = timeline.reviewed_sha or review_sha
                except _reviewer.ReviewLimited as e:
                    # Not a round and not a human's problem: the review runs again after the reset.
                    qa_task = None
                    qa_round -= 1
                    last_seen = time.monotonic()
                    review_wait.wait_for_limit(e, qa_round, timeline)
                    continue
                except _reviewer.ReviewUnavailable as e:
                    qa_task = None
                    qa_round -= 1
                    last_seen = time.monotonic()
                    if review_wait.schedule_incomplete_rerun(e, qa_round, timeline):
                        continue
                    # Blocks and escalates, and is not a round: the author did
                    # nothing wrong, so it must not spend one of their three.
                    why = review_wait.explain_unreviewed(e)
                    timeline.row("🚨", why if why != str(e) else f"nobody reviewed the work: {e}")
                    timeline.phase("waiting")
                    review_wait.at = None
                    review_wait.save(qa_round, timeline)
                    timeline.activity("review unavailable -- nobody has reviewed this work; needs a human")
                    await proc.submit(
                        f"Your DONE has NOT been reviewed: {why}. This is an infrastructure failure, not a "
                        "finding, and it did not use a review round. "
                        + _supervise_support._tell_human_first("nobody has reviewed the work")
                        + ' `ask-human "Review could not run (<reason>). Please retry it or review the '
                        'branch yourself. Summary of what was delivered: <...>"` and wait. Once a human '
                        "says to retry, declare DONE again.",
                        label="Review unavailable",
                    )
                    continue
                if (not passed or findings) and compact_round != qa_round:
                    compact_round = qa_round
                    compact_task = asyncio.create_task(
                        asyncio.wait_for(
                            _supervise_support._compact_between_rounds(proc, timeline, stop),
                            timeout=_supervise_support._COMPACT_TIMEOUT_S
                            + max(0.01, _session_config.POLL_EVERY_S),
                        )
                    )
                if compact_task is not None:
                    if not compact_task.done():
                        continue
                    try:
                        compact_task.result()
                    except Exception as exc:  # noqa: BLE001 -- compaction must not lose review findings
                        traceback.print_exc()
                        timeline.row(
                            "⚠️", f"compaction failed ({type(exc).__name__}); delivering findings uncompacted"
                        )
                    compact_task = None
                qa_task = None
                last_seen = time.monotonic()
                if not passed:
                    _inbox._unpass(qa_round, timeline)
                if passed:
                    timeline.review_passed = True
                    passed_round = qa_round
                    sha = (
                        await _checkpoint._git_out(workspace, "rev-parse", "HEAD", timeout=10)
                        if workspace
                        else (1, "")
                    )
                    pass_sha = review_sha or (sha[1] if sha[0] == 0 else "")
                    _inbox._save_pass(qa_round, pass_sha, timeline.pr_url, timeline=timeline)
                    _contract._reset_contract()
                    if workspace:
                        await _demo_checks._publish_demo_reports(
                            workspace, "reviewed and passed", timeline, demo_package, publish=True
                        )
                    declared_done = True
                    timeline.phase("done")
                    timeline.passed_at = timeline._phase["since"]
                    timeline.activity(f"done -- passed review in {qa_round} round(s)")
                    await _supervise_support._post_verdict(
                        cast(Path, workspace), timeline, review_sha, qa_round
                    )
                    _inbox._save_pass(
                        qa_round, pass_sha, timeline.pr_url, verdict_posted=True, timeline=timeline
                    )
                    pr_line = (
                        f" Include the pull request in that message: {timeline.pr_url}"
                        if timeline.pr_url
                        else ""
                    )
                    await proc.submit(
                        "An independent reviewer passed your work, so this is finished. Tell the "
                        "human now: "
                        + (
                            "put one line on what was delivered at the top of your message, and "
                            if _usage._author_engine == "codex"
                            else "send a PushNotification with one line on what was delivered, then "
                        )
                        + f'`ask-human --timeout {_session_config.RELEASE_IDLE_S} "<what you delivered, '
                        f"and anything "
                        f"they should decide> Default if no answer: the project is released; relaunch "
                        f'{_session_config._project_name} to continue."` and wait. Do not start new work.'
                        + pr_line
                        + (f" {findings}" if findings else ""),
                        label="Review passed",
                    )
                elif (
                    qa_round >= _session_config._max_rounds()
                    and converging is True
                    and not _review._stalled(_session_config._round_scores)
                    and _session_config._granted_rounds < _session_config._MAX_GRANTED_ROUNDS
                    and not _supervise_support._escalated
                ):
                    # The ceiling, but the planner says review is converging: grant the round
                    # ourselves. A past run escalated at rounds 6 and 7 only for the operator to
                    # read the narrowing findings and grant exactly this, twice.
                    _session_config._granted_rounds += 1
                    await _supervise_support._post_verdict(
                        cast(Path, workspace),
                        timeline,
                        review_sha,
                        qa_round,
                        _supervise_support.describe_rejection(qa_round, timeline),
                    )
                    timeline.row(
                        "🔁",
                        f"round {qa_round} was the ceiling, but the planner says review is converging; "
                        f"granted round {_session_config._max_rounds()} automatically",
                    )
                    timeline.phase("building")
                    timeline.activity(
                        f"QA round {qa_round} failed -- back to the session (extra round granted)"
                    )
                    await proc.submit(
                        "Not done yet. Review rejected the work again, but it is converging, so one more "
                        f"round has been granted (round {_session_config._max_rounds()} is now the last). "
                        f"Work through the "
                        "plan below -- root causes first, a test for each fix -- then declare DONE again.\n\n"
                        f"{findings}",
                        label=f"Review round {qa_round} rejected your work",
                    )
                elif qa_round >= _session_config._max_rounds() or (
                    qa_round >= _session_config._CONVERGENCE_FROM_ROUND
                    and (
                        converging is False
                        or (converging is None and _review._stalled(_session_config._round_scores))
                    )
                ):
                    # Stalled: two rounds in a row without a 30% drop in blocking findings, measured
                    # against the frozen contract -- the planner's verdict is the other signal.
                    if _review._stalled(_session_config._round_scores) and not circling_why:
                        circling_why = f"blocking findings stopped falling: {_session_config._round_scores}"
                    # Escalation only when the loop cannot close this itself: the planner
                    # says review is circling (from round _CONVERGENCE_FROM_ROUND), or the
                    # hard ceiling is reached. A converging loop keeps going on its own.
                    declared_done = True
                    _supervise_support._escalated = True
                    # The only failure status for this round: a rejected round that also stops
                    # converging is one verdict, with the more specific reason.
                    await _supervise_support._post_verdict(
                        cast(Path, workspace),
                        timeline,
                        review_sha,
                        qa_round,
                        f"review did not converge in {qa_round} rounds",
                    )
                    grantable = _session_config._granted_rounds < _session_config._MAX_GRANTED_ROUNDS
                    timeline.row(
                        "🚨",
                        f"QA did not converge in {qa_round} rounds; escalating"
                        + (f" (planner: {circling_why[:200]})" if circling_why else ""),
                    )
                    timeline.phase("waiting")
                    timeline.activity(
                        f"stuck after {qa_round} QA rounds -- needs a human"
                        + (
                            " (send `/review-again` to let review continue while it converges)"
                            if grantable
                            else ""
                        )
                    )
                    await proc.submit(
                        f"Review has now failed {qa_round} times, so stop looping and escalate. "
                        + _supervise_support._tell_human_first("review did not converge")
                        + ' `ask-human "<what is still failing, what you have tried, what you need '
                        'decided>"` and wait for an answer. Tell the operator to resume with '
                        "`/review-again`.\n\n"
                        + (
                            f"The planner's view of what needs deciding: {circling_why}\n\n"
                            if circling_why
                            else ""
                        )
                        + f"The last findings:\n\n{findings}",
                        label="Review escalated: not converging",
                    )
                else:
                    await _supervise_support._post_verdict(
                        cast(Path, workspace),
                        timeline,
                        review_sha,
                        qa_round,
                        _supervise_support.describe_rejection(qa_round, timeline),
                    )
                    timeline.phase("building")
                    timeline.activity(f"QA round {qa_round} failed -- back to the session")
                    await proc.submit(
                        "Not done yet. An independent reviewer went through your work against the "
                        f"brief and rejected it (round {qa_round} of at most "
                        f"{_session_config._max_rounds()}). Work "
                        "through the plan below if there is one -- root causes first, a test for each "
                        "fix -- or if you believe a finding is wrong, say why in your next "
                        "`flyte-note` rather than ignoring it -- then declare DONE again.\n\n"
                        f"{findings}",
                        label=f"Review round {qa_round} rejected your work",
                    )

            # Terminal activity is read from `proc.last_output` below. It used to be
            # `len(proc.tail)` changing, but the tail is a bounded deque: once full its
            # length never changes, and every heartbeat read `tail_lines=400`.

            requested = bridge.stop_requested()
            if requested is not None:
                timeline.row("🛑", f"end-project: {requested}")
                timeline.activity(f"finishing: {requested}")
                await timeline.flush(force=True)
                stop.set()
                return

            # Checked here as well as at startup: on a project that runs for
            # weeks the credential can lapse long after the session connected, and
            # silence from a logged-out session looks exactly like idleness.
            _author._assert_logged_in(proc)

            warned_disk, warned_memory = _supervise_support._warn_on_low_headroom(
                timeline, warned_disk, warned_memory
            )

            now = time.monotonic()
            out_at = getattr(proc, "last_output", None)
            menu = getattr(proc, "menu", None)
            if menu is None:
                menu_seen_at = None
                prompt_stuck = False
                # Whatever was submitted while a menu was open goes out now (`ClaudeProcess.submit`
                # holds it rather than type into the menu). The test fakes have no hold.
                deliver_held = getattr(proc, "deliver_held", None)
                if deliver_held is not None:
                    await deliver_held()
            elif menu_seen_at is None:
                menu_seen_at = now
            declined_this_menu = (
                menu_seen_at is not None and declined_at is not None and declined_at >= menu_seen_at
            )
            # A menu already declined is not "open" for idle and nudge purposes: if it is
            # still (or again) on screen, the session is stuck and must be nudged and
            # escalated like any other stall, not read as busy.
            menu_open = menu is not None and not declined_this_menu
            if permission_followup and now - permission_followup[2] > _session_config.PROMPT_FOLLOWUP_HOLD_S:
                _timeline._log_line(
                    f"[pump] dropped a follow-up held {_session_config.PROMPT_FOLLOWUP_HOLD_S:.0f}s on an "
                    f"open menu"
                )
                permission_followup = None
            # Menu time must not spend the idle-nudge allowance. Change-approach follow-ups are
            # the exception: declines repeat every few minutes for as long as a menu stays open,
            # and if they held the clock the idle nudges -- and the "parked" row after them --
            # would never come.
            repeating = streak_count >= _session_config.PROMPT_REPEAT_AFTER or (
                sum(1 for t in decline_times if now - t < _session_config.PROMPT_DECLINE_WINDOW_S)
                >= _session_config.PROMPT_ANY_CAP
            )
            if menu_open or (permission_followup and not repeating):
                last_seen = now
            if limited_since is None:
                if permission_followup and menu is None and now - permission_followup[2] >= 1:
                    text, label, _ = permission_followup
                    await proc.submit(text, label=label)
                    permission_followup = None
                    if not repeating:
                        last_seen = now
                elif menu is not None and (
                    (out_at is not None and now - out_at >= 20)
                    or now - cast(float, menu_seen_at) >= _session_config.PROMPT_BUSY_DECLINE_S
                ):
                    if (
                        declined_this_menu
                        and now - cast(float, declined_at) < _session_config.PROMPT_REDECLINE_S
                    ):
                        pass  # backing off
                    else:
                        # The menu advertises Esc to cancel; Enter could approve its default.
                        proc.type("\x1b")
                        # The same menu still open, or one like it back within the window.
                        if streak_menu is not None and (
                            declined_this_menu
                            or (
                                now - cast(float, declined_at) < _session_config.PROMPT_DECLINE_WINDOW_S
                                and _supervise_support._same_prompt(menu, streak_menu)
                            )
                        ):
                            streak_count += 1
                        else:
                            streak_count, streak_escalated = 1, False
                        # The latest text, so a streak follows redraw damage that accumulates.
                        streak_menu, declined_at = menu, now
                        decline_times = [
                            t for t in decline_times if now - t < _session_config.PROMPT_DECLINE_WINDOW_S
                        ] + [now]
                        if len(decline_times) < _session_config.PROMPT_ANY_CAP:
                            scattered_escalated = False
                        shown = _supervise_support._flat(menu)
                        if streak_count >= _session_config.PROMPT_REPEAT_AFTER:
                            # Rows once per streak, not per repeat, and no human until the nudges
                            # have had their chance: the session can fix this itself.
                            if streak_count == _session_config.PROMPT_REPEAT_AFTER:
                                timeline.row(
                                    "🔁",
                                    f"declined the same permission prompt {streak_count} times; told the "
                                    f"session to change approach: {shown}",
                                )
                                text = _supervise_support._change_approach_text(menu, streak_count)
                            else:
                                text = _supervise_support._declined_again_text(menu, streak_count)
                            if streak_count >= _session_config.PROMPT_DECLINE_CAP and not streak_escalated:
                                streak_escalated = prompt_stuck = True
                                timeline.row(
                                    "🚨",
                                    f"a permission prompt keeps coming back after "
                                    f"{_session_config.PROMPT_REPEAT_NUDGES} "
                                    "change-approach nudges; a human needs to look (still declining it, "
                                    "so the session is not blocked)",
                                )
                            label = "Permission prompt declined again: change approach"
                        elif len(decline_times) >= _session_config.PROMPT_ANY_CAP:
                            timeline.row("⚠️", f"declined a permission prompt: {shown}")
                            if not scattered_escalated:
                                scattered_escalated = prompt_stuck = True
                                timeline.row(
                                    "🚨",
                                    f"a permission prompt keeps coming back ({len(decline_times)} different "
                                    f"ones declined in {_session_config.PROMPT_DECLINE_WINDOW_S / 60:.0f}m); "
                                    f"a human needs "
                                    "to look (still declining them, so the session is not blocked)",
                                )
                            text = _supervise_support._scattered_declines_text(menu, len(decline_times))
                            label = "Permission prompts keep being declined: change approach"
                        else:
                            timeline.row("⚠️", f"declined a permission prompt: {shown}")
                            text, label = (
                                _supervise_support._declined_text(menu),
                                "Permission prompt declined",
                            )
                        permission_followup = (text, label, now)
            spoke_for = now - last_seen
            # Terminal output is activity: a turn in progress -- its own subagents
            # included -- keeps the spinner writing, and an idle prompt writes
            # nothing. A past run ran three qa subagents for
            # 12 minutes while this read "idle -- send direction", and the operator
            # interrupted a session doing exactly the right thing.
            out_at = getattr(proc, "last_output", None)
            terminal_busy = out_at is not None and now - out_at < _session_config.IDLE_AFTER_S
            quiet_for = min(spoke_for, now - out_at) if out_at is not None else spoke_for
            # But a hung tool call keeps a spinner going forever too, so output alone
            # holds off nudges only for so long after the session last said anything.
            nudge_quiet = (
                spoke_for if terminal_busy and spoke_for > _session_config.BUSY_CEILING_S else quiet_for
            )
            idle = quiet_for > _session_config.IDLE_AFTER_S
            if menu_open or permission_followup:
                idle = False
                nudge_quiet = 0
            # Nothing below is typed while a menu is open, declined or not: text or Enter landing
            # on it could approve the command. Notices stay queued and nudges wait for it to close.
            menu_closed = getattr(proc, "menu", None) is None
            if idle and limited_since is None and delegations is not None and menu_closed:
                notices = delegations.drain_notices()
                if notices:
                    try:
                        await proc.submit(
                            "Delegation finished: " + " ".join(notices), label="Delegation finished"
                        )
                    except Exception:
                        delegations.requeue_notices()
                        raise
                    last_seen = now
                    idle = False
                    nudge_quiet = 0
            # Refreshed on every pass while idle, not only on the transition. The
            # transition-only version froze the text at the moment the session went
            # quiet, so a run stalled for half an hour still read "idle for 3m" --
            # which is the opposite of what the line exists to tell you.
            # A declared completion outranks the idle heuristic -- it is the
            # agent's own statement rather than an inference from silence -- so once
            # DONE is posted the status stays as posted. Without this guard the very
            # next pass replaced it with "working", and the pass after that with
            # "idle", burying the one line that said what was delivered.
            # Silence while children run is not idleness, it is waiting. Checked
            # only when the session looks idle, and at most once a minute, because
            # it costs an API call: three of the first five nudges went to sessions
            # legitimately blocked on an image build or a GPU sweep, and "idle for
            # 18m" is actively misleading when nine child actions are training.
            # A hung call is abandoned, not killed, so a new listing waits for the last one's
            # thread -- one more per minute would add up -- unless it has hung past
            # CHILDREN_STALE_S, when one more may try (the control plane may be back). Never
            # more than two threads at once.
            listing_threads = [call for call in listing_threads if call.thread.is_alive()]  # type: ignore[attr-defined]  # set by _inbox._in_daemon_thread
            may_list = not listing_threads or (
                len(listing_threads) < 2
                and time.monotonic() - children_started_at > _session_config.CHILDREN_STALE_S
            )
            if idle and children_task is None and may_list and time.monotonic() - last_children_check > 60:
                last_children_check = time.monotonic()
                # On a daemon thread: the listing is a blocking client call, and the pump is the
                # event loop every other supervisor job shares. Not `asyncio.to_thread`, whose
                # executor every hung call would hold a worker of -- starving the board, the PR
                # and the gates once they were all taken -- and whose threads interpreter exit
                # waits on. And in the background: awaiting it here held every other job on this
                # loop for up to the whole timeout, on every pass the control plane was slow.
                # A quick listing is still used on this pass.
                children_call = _inbox._in_daemon_thread(_supervise_support._in_flight_children)
                listing_threads.append(children_call)
                children_started_at = time.monotonic()
                children_task = asyncio.create_task(
                    asyncio.wait_for(children_call, timeout=_session_config.CHILDREN_LIST_TIMEOUT_S)
                )
                await asyncio.wait({children_task}, timeout=_session_config.CHILDREN_LIST_WAIT_S)
            if children_task is not None and children_task.done():
                try:
                    count, error = children_task.result()
                except asyncio.TimeoutError:
                    count, error = (
                        0,
                        f"listing child actions timed out after "
                        f"{_session_config.CHILDREN_LIST_TIMEOUT_S:.0f}s",
                    )
                except Exception as e:  # noqa: BLE001 -- listing boundary: a status heuristic must never end the loop
                    traceback.print_exc()
                    # `_in_flight_children` catches the remote errors it knows; anything else it
                    # raises is a failed listing too, reported below and bounded by CHILDREN_STALE_S.
                    count, error = 0, f"{type(e).__name__}: {e}"[:300]
                children_task = None
                _supervise_support._set_in_flight_error(error)
                if not error:
                    in_flight, children_failing_since = count, None
                elif children_failing_since is None:
                    # The last good count stands for now (a failure's 0 is a worse guess), but
                    # only for `CHILDREN_STALE_S`: past that the gates below treat it as unknown.
                    children_failing_since = time.monotonic()
                if error and not children_error_reported:
                    children_error_reported = True
                    timeline.row("⚠️", f"cannot see child actions, so 'idle' may be wrong: {error}")
                elif not error and children_error_reported:
                    children_error_reported = False  # re-armed: a later failure is reported again
                    timeline.row("✅", "child actions visible again")
            children_unknown = (
                children_failing_since is not None
                and time.monotonic() - children_failing_since >= _session_config.CHILDREN_STALE_S
            )
            # What the nudge, park and kickoff gates and the status line go by.
            waiting_on = 0 if children_unknown else in_flight

            review_waiting = review_wait.at is not None and qa_task is None and limited_since is None
            if review_waiting:
                review_wait.show(timeline)
            if review_waiting or (not declared_done and limited_since is None):
                if review_wait.at is not None and qa_task is None:
                    timeline.activity(review_wait.describe_activity())
                elif qa_task is not None:
                    # A review in flight is not idleness. The idle line used to
                    # replace "QA round 1 reviewing" within a pass, so a project
                    # whose reviewers could not be placed read "idle for 21m" with
                    # nothing saying a review was what it was waiting on.
                    timeline.activity(f"QA round {qa_round} reviewing ({quiet_for / 60:.0f}m so far)")
                elif prompt_stuck:
                    timeline.activity("a permission prompt keeps coming back; a human needs to look")
                elif exhausted_announced:
                    # Sticky until the session speaks: the idle line below would
                    # otherwise overwrite the one status that says a human is needed.
                    timeline.activity(
                        f"idle for {quiet_for / 60:.0f}m -- parked after {_session_config.MAX_NUDGES} "
                        f"unanswered "
                        "nudges; needs a human"
                    )
                elif idle and waiting_on:
                    timeline.activity(
                        f"waiting on {waiting_on} child action{'s' if waiting_on > 1 else ''} "
                        f"({quiet_for / 60:.0f}m since the session last said anything)"
                    )
                elif idle and children_unknown:
                    timeline.activity(
                        f"idle for {quiet_for / 60:.0f}m -- child actions unknown: listing them has failed "
                        f"for {(time.monotonic() - cast(float, children_failing_since)) / 60:.0f}m"
                    )
                elif idle:
                    timeline.activity(
                        f"idle for {quiet_for / 60:.0f}m -- send direction, or `/stop` to finish"
                    )
                elif terminal_busy and spoke_for > _session_config.IDLE_AFTER_S:
                    timeline.activity(
                        f"working -- no note for {spoke_for / 60:.0f}m, but the terminal is active "
                        "(likely its own subagents or a long command)"
                    )
                else:
                    timeline.activity("working")

            # A run that goes quiet for a long time without declaring
            # itself done has not finished -- it has parked. That has happened for
            # reasons a single line of prompting fixes: mistaking another project's
            # committed work in the shared repo for its own, or treating a timed-out
            # question as a reason to wait. Nudge rather than let the run burn to
            # its deadline.
            # A session that has never said anything and is already idle probably
            # never received the brief: typed input can be swallowed while the TUI
            # is still settling through its first-run dialogs. One run sat for
            # eighteen minutes and only started when the nudge happened to resend
            # something. Resending the kickoff at the idle threshold costs three
            # minutes instead of fifteen, and is separate from the nudge allowance
            # because it is a different failure.
            if (
                idle
                and limited_since is None
                and kickoff
                and not ever_noted
                and not waiting_on
                and menu_closed
                and kickoff_retries < 2
            ):
                kickoff_retries += 1
                timeline.row("📨", f"the brief got no response; resending it ({kickoff_retries}/2)")
                await proc.submit(kickoff, label="Project kickoff")
                last_seen = time.monotonic()
                continue

            if (
                nudge_quiet > _session_config.NUDGE_AFTER_S
                and limited_since is None
                and not declared_done
                and qa_task is None
                and not waiting_on
                and review_wait.at is None
                and menu_closed
                and nudges < _session_config.MAX_NUDGES
            ):
                nudges += 1
                timeline.row("👋", f"idle and unattended; nudging ({nudges}/{_session_config.MAX_NUDGES})")
                outstanding = (
                    await _supervise_support._done_preflight(workspace) if workspace and nudges > 1 else ""
                )
                await proc.submit(_supervise_support._nudge_text(nudges, outstanding), label="Idle reminder")
                last_seen = time.monotonic()
            elif (
                nudge_quiet > _session_config.NUDGE_AFTER_S
                and limited_since is None
                and not declared_done
                and qa_task is None
                and not waiting_on
                and review_wait.at is None
                and nudges >= _session_config.MAX_NUDGES  # not merely held back by an open menu
                and not exhausted_announced
            ):
                # Giving up on prodding used to be silent: the run kept saying
                # "idle" and looked like every other idle run. Parked with the
                # allowance spent is a different state and wants a human.
                exhausted_announced = True
                parked_at, parked_wall = time.monotonic(), time.time()
                timeline.row(
                    "🚨",
                    f"parked: {_session_config.MAX_NUDGES} nudges went unanswered and the session has been "
                    f"quiet "
                    f"for {quiet_for / 60:.0f}m; it needs a human",
                )
                grace = _checkpoint._park_release_s(timeline.row)
                notify = getattr(bridge, "notify_owner", None)  # the test fakes have none
                if grace and notify is not None:
                    park_notice_id = notify(
                        f'"{_session_config._project_name}" is parked: {_session_config.MAX_NUDGES} '
                        f"reminders went unanswered. Reply here, "
                        f"or send direction, within {grace / 60:.0f}m to keep it; otherwise it releases "
                        f'its slot and checkpoints, and launching "{_session_config._project_name}" again '
                        f"resumes it.",
                        grace,
                    )

            # A parked session holds a slot nobody is using: after the grace period it ends the way
            # a released one does (final checkpoint, published workspace, slot handed to the next
            # launch), and launching the same name resumes it from that workspace.
            if (
                exhausted_announced
                and parked_at is not None
                and limited_since is None
                and _checkpoint._park_release_s()
            ):
                answered = getattr(bridge, "answered_since", None)
                if (_inbox._last_direction_at is not None and _inbox._last_direction_at > parked_at) or (
                    answered is not None and answered(parked_wall)
                ):
                    timeline.row("✅", "owner replied; the parked session keeps its slot")
                    nudges, exhausted_announced, parked_at, last_seen = 0, False, None, now
                elif now - parked_at >= _checkpoint._park_release_s() and not holds_slot():
                    timeline.row(
                        "💤",
                        f"parked for {(now - parked_at) / 60:.0f}m with no reply; releasing its slot "
                        f'-- launch "{_session_config._project_name}" again to resume',
                    )
                    timeline.phase("released")
                    timeline.activity("released while parked -- launch the same name again to continue")
                    _supervise_support._release_note = "released while parked (no reply to the nudges)"
                    _supervise_support._released = True
                    await timeline.flush(force=True)
                    stop.set()
                    return

            # Every blocker is something a human or a job is still waiting on; passing review is
            # what makes a quiet project finished rather than stuck. `holds_slot` has them all;
            # `_in_flight_error` blocks too: with the child listing failing, "no children" is a guess.
            if (
                passed_round is not None
                and limited_since is None
                and not holds_slot()
                and (
                    _inbox._last_direction_at is None
                    or now - _inbox._last_direction_at >= _session_config.RELEASE_IDLE_S
                )
                and quiet_for >= _session_config.RELEASE_IDLE_S
            ):
                timeline.row(
                    "💤",
                    f"released after {_session_config.RELEASE_IDLE_S // 60}m idle having passed review in "
                    f"round "
                    f'{passed_round}; launch "{_session_config._project_name}" again to resume',
                )
                timeline.phase("released")
                timeline.activity("released -- launch the same name again to continue")
                # As `_board.desired`: a pull request still awaits its merge, which makes the card Done.
                has_pr = bool(timeline.snapshot()["deliverables"]["pr"])
                await _supervise_support._move_board(
                    "In review" if has_pr else "Done", timeline, Engine=_supervise_support._engines_used()
                )
                _supervise_support._release_note = "released after idle (passed review)"
                _supervise_support._released = True
                await timeline.flush(force=True)
                stop.set()
                return

            if time.monotonic() - last_heartbeat >= _session_config.HEARTBEAT_EVERY_S:
                last_heartbeat = time.monotonic()
                _timeline._log_line(
                    f"[pump] heartbeat status={timeline._activity!r} quiet={quiet_for:.0f}s "
                    f"children={waiting_on} nudges={nudges}/{_session_config.MAX_NUDGES} qa_round={qa_round} "
                    f"done={declared_done} output_chars={getattr(proc, 'output_count', 0)}"
                )

            await timeline.flush()
            try:
                await asyncio.wait_for(stop.wait(), timeout=_session_config.POLL_EVERY_S)
                return
            except asyncio.TimeoutError:
                continue
    finally:
        if children_task is not None:
            children_task.cancel()
            await asyncio.gather(children_task, return_exceptions=True)
        if compact_task is not None:
            compact_task.cancel()
            await asyncio.gather(compact_task, return_exceptions=True)
        if qa_task is not None:
            qa_task.cancel()
            await asyncio.gather(qa_task, return_exceptions=True)
        if getattr(proc, "returncode", None) is not None:
            human_exit = proc.returncode == 0 and _inbox._human_exit(
                _session_config.CONFIG_DIR,
                workspace or _session_config.WORKSPACE,
                list(getattr(proc, "sent_texts", [])),
                started_at,
            )
            ended = "session ended by the human" if human_exit else "the session process exited"
            if passed_round is not None:
                ended += f" after passing review in round {passed_round}"
            elif qa_round:
                when = "during" if qa_task is not None else "after"
                ended += f" {when} review round {qa_round}; not passed by review"
            else:
                ended += "; never reviewed"
            if proc.returncode == 0:
                timeline.session_end = ended
                if not stop.is_set():
                    timeline.row("🏁", ended)
                    timeline.activity(ended)
                    timeline.session_end_written = True
            elif not stop.is_set():
                timeline.row(
                    "⚠️", f"session process died with code {proc.returncode} during review round {qa_round}"
                )
