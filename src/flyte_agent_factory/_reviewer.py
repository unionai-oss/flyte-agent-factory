"""Calling a reviewer sub-agent: the routed, bounded attempt and the errors a review round can end in."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import flyte
import flyte.errors
from flyte.io import Dir

from flyte_agent_factory import _policy, _timeline, _usage
from flyte_agent_factory._errors import REMOTE_ERRORS

#: How long a reviewer may wait to be placed before it is moved to on-demand, and
#: then given up on. In a past run three reviewers sat in WAITING_FOR_RESOURCES
#: for over twenty minutes (25 placement attempts) and the run, which only
#: learns of a reviewer by its result, sat "idle" with a finished project.
_REVIEWER_QUEUE_BOUND = timedelta(minutes=5)


async def _call_reviewer(
    purpose: str = "",
    round_: int = 0,
    timeline: _timeline._Timeline | None = None,
    on_engine: Callable[[str, str], object] | None = None,
    **kwargs: Any,
) -> tuple[str, Dir]:
    """Return a sub-agent call, metered under `purpose` (judge name, planner, arbiter, contract).

    Codex is the second engine: a role can be put on it outright (`_engine_for`), and a call
    the Claude account refuses with its usage limit is retried there before anyone waits --
    a separate meter keeps review moving through a Claude limit.
    """
    engine = kwargs.pop("engine", None)
    # A pre-routed Codex delegation passes Codex settings, which cannot run on Claude.
    claude_setting: dict[str, Any] = (
        {} if engine == "codex" else {k: kwargs[k] for k in ("model", "effort") if k in kwargs}
    )
    # A role whose static engine is Claude keeps its Claude effort when the router moves it to Codex.
    preferred_engine = engine or _usage._engine_for(purpose)

    def settle(target: str) -> tuple[str, str]:
        """Point the call at `target` with that engine's model and effort; returns them."""
        nonlocal engine
        engine = target
        if target == "codex":
            setting = _policy.resolve_codex(
                purpose,
                claude_effort=kwargs.get("effort", "") if preferred_engine != "codex" else "",
                warn=timeline.row if timeline else lambda icon, message: _timeline._log_line(message),
            )
            kwargs.update(model=setting.model, effort=setting.effort)
        else:
            for k in ("model", "effort"):
                kwargs.pop(k, None)
            kwargs.update(model=_policy.DEFAULTS["author"].model, effort=_policy.DEFAULTS["author"].effort)
            kwargs.update(claude_setting)
        return kwargs.get("model", ""), kwargs.get("effort", "")

    def announce() -> None:
        if on_engine:
            model = kwargs.get("model", _policy.DEFAULTS["author"].model if engine == "claude" else "")
            on_engine(engine, f"{engine} {model or 'default'}/{kwargs.get('effort') or 'default'}")
        if timeline:
            timeline.row(
                "🔧",
                f"{purpose or 'subagent'}: {engine} "
                f"{kwargs.get('model') or 'default'}/{kwargs.get('effort') or 'default'}",
            )

    if engine and not _usage._engine_exhausted(engine):
        # A delegation may have waited on its snapshot/upload since choosing this engine.
        if engine == "codex":
            settle("codex")
    else:
        decision = _usage._route(purpose, timeline, pin=engine or "", setting=settle)
        if decision.deferred:
            raise _usage._deferred(decision)
    suffix = ""
    announce()
    try:
        text, out = await _call_reviewer_raw(engine=engine, routed=True, **kwargs)
    except REMOTE_ERRORS as e:  # what a failed sub-agent action raises
        limited = _usage.USAGE_LIMIT_MARK in str(e)
        # A cross-check is preferred, not required: when the other engine fails outright, the review
        # runs on the builder's engine instead of `_one_judge` retrying the failing one and ending
        # unreviewed. A limit goes through the router, whose reason says the same. A hard pin never moves.
        builder = _usage._author_engine
        cross_check = (
            purpose in _usage._QA_ROLES
            and _usage._qa_differs()
            and engine != builder
            and not _usage._hard_pins
        )
        if not limited and (
            not cross_check or builder not in _usage._author_engines() or _usage._engine_exhausted(builder)
        ):
            raise
        if limited:
            _usage._note_engine_limit(engine, str(e))
            # Ask again with the refusing engine out: the other one may be exhausted too, and a call to
            # an engine the router knows is full only burns a queue slot to be refused.
            refused = engine
            decision = _usage._route(purpose or "subagent", timeline, exclude=(refused,), setting=settle)
            if decision.deferred:
                raise _usage._deferred(decision) from e
            suffix = f"+{decision.engine}"
        else:
            message = f"{purpose}: cross-check unavailable: {engine} failing; reviewed on {builder}"
            if timeline:
                timeline.row("🔀", f"{message} ({type(e).__name__}: {e})")
            else:
                _timeline._log_line(f"[router] {message}")
            settle(builder)
            suffix = f"+{builder}"
        announce()
        try:
            text, out = await _call_reviewer_raw(engine=engine, routed=True, **kwargs)
        except Exception as retry_error:
            if _usage.USAGE_LIMIT_MARK in str(retry_error):
                _usage._note_engine_limit(engine, str(retry_error))
            raise
    if suffix:
        purpose = f"{purpose or 'subagent'}{suffix}"
    clean = _usage._ingest_subagent_usage(
        text,
        purpose or kwargs.get("role") or "subagent",
        round_,
        timeline,
        engine=engine,
        effort=kwargs.get("effort", ""),
    )
    return clean, out


#: The `subagent` task. It has to stay in agent.py (its module path is its registered identity) and
#: this module cannot import agent, so agent.py sets this right after defining the task.
subagent_task: Any = None


async def _call_reviewer_raw(**kwargs: Any) -> tuple[str, Dir]:
    """Run the reviewer sub-agent: spot first, bounded; then on-demand, bounded.

    Spot is cheaper and a separate pool, so it is tried first -- but an unplaceable
    action is neither a failure nor progress, so each attempt carries a queue
    bound and a miss falls through to the other pool. A second miss raises, which
    `_one_judge` retries and, if every reviewer misses, turns into ReviewUnavailable.
    """
    bound = flyte.Timeout(max_queued_time=_REVIEWER_QUEUE_BOUND)
    try:
        return await subagent_task.override(interruptible=True, timeout=bound).aio(**kwargs)  # type: ignore[no-any-return]  # Flyte's `.aio` is untyped
    except flyte.errors.MaxQueuedTimeExceededError:
        return await subagent_task.override(interruptible=False, timeout=bound).aio(**kwargs)  # type: ignore[no-any-return]  # as above


#: Every failing judge's findings, every round: (round, judge, findings). Shown to
#: all judges, so one cannot fail what another required without saying so.
_review_ledger: list[tuple[int, str, str]] = []
_LEDGER_ITEM_CHARS = 1500
_LEDGER_CHARS = 9000


def _earlier_requirements(judge: str, round_: int) -> str:
    """Earlier rounds' findings from every judge, minus this judge's last round (shown separately).

    Newest first, so a cap drops the oldest rounds rather than the ones being fixed now.
    """
    parts: list[str] = []
    used = 0
    for r, who, findings in reversed(_review_ledger):
        if r >= round_ or (r == round_ - 1 and who == judge):
            continue
        # Head and verdict tail: the blocking finding is at the start of a report, the verdict at its end.
        text = findings.strip()
        if len(text) > _LEDGER_ITEM_CHARS:
            text = text[: _LEDGER_ITEM_CHARS - 300] + "\n[...]\n" + text[-300:]
        item = f"### Round {r}, {who} reviewer\n\n{text}"
        if used + len(item) > _LEDGER_CHARS:
            parts.append("(older rounds omitted)")
            break
        parts.append(item)
        used += len(item)
    return "\n\n".join(parts)


#: What a sub-agent returns when it ended without a final message: no plan, no verdict.
_NO_FINAL_MESSAGE = "(the sub-agent produced no final message)"


class ReviewUnavailable(Exception):
    """No reviewer examined the work. Not a pass, not a fail, not the author's fault."""


class ReviewLimited(ReviewUnavailable):
    """No reviewer could run because the account is on its usage limit; retried after the reset."""

    def __init__(self, reset_text: str) -> None:
        """Keep `reset_text`, when the usage limit lifts, as the account reports it (may be empty)."""
        super().__init__(f"the reviewers are on the usage limit ({reset_text or 'reset time not shown'})")
        self.reset_text = reset_text


#: judge -> the usage-limit text that stopped it, this round.
_limited_reason: dict[str, str] = {}
