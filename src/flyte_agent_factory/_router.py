"""Engine router: which engine (Claude or Codex) takes a call.

Pure: no I/O, no clock, no environment. The caller reads the meters (`_meter`), passes them
in with the time, and acts on the `Decision`. There is no LLM here and no reserve.

Every role can run on either engine, so the router needs no meter to choose: role policy
does. A meter is only a hint; the signal that moves a call is a real usage-limit refusal,
which the caller records (`limited_until`) or passes in (`exclude`). Rules, in order (see
`.briefs/design-state.md`, section A):

1. An engine is out only when it is exhausted: a refusal whose reset is still ahead, or an
   `exclude`. A window at 100% is a hint that only ranks it last. An unknown, stale or
   unreadable meter is available, never out.
2. An explicit pin wins while the pinned engine is not exhausted.
3. A QA judge prefers an engine other than the builder's. With that one exhausted it reviews
   on the builder's engine and says so, rather than wait (`_reviewer._call_reviewer` does the same
   when that engine fails outright).
4. Otherwise the caller's preferred engine wins. Headroom only breaks a tie when there is no
   preference, and only between engines that are both measured.
5. With every engine exhausted the call is deferred, with the earliest time one reopens.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

# The runtime import is absolute (see `_webhook` for why); mypy resolves `flyte_agent_factory` to the install,
# which has no py.typed marker, so it checks against the relative import instead.
if TYPE_CHECKING:
    from ._meter import MIN_INTERVAL_S, ROUTE_AT_PCT, Meter
else:
    from flyte_agent_factory._meter import MIN_INTERVAL_S, ROUTE_AT_PCT, Meter

#: A reading older than this says nothing about now. The supervisor re-reads every
#: `MIN_INTERVAL_S` (`_usage._METER_EVERY_S`), so this is two of its intervals.
STALE_AFTER_S = 2 * MIN_INTERVAL_S
#: Used when every engine is exhausted and no reset time is known.
DEFAULT_RETRY_S = 300.0


class RouterDeferred(RuntimeError):
    """Every engine is exhausted; the call was not made."""


@dataclass(frozen=True)
class Candidate:
    """One engine as the meters see it at decision time."""

    engine: str
    #: usable (headroom known), exhausted, unknown (no fresh reading) or unavailable (no login)
    state: str
    headroom: float | None = None
    five_hour_pct: float | None = None
    weekly_pct: float | None = None
    #: When an exhausted engine reopens; None if a blocking window has no known reset.
    usable_at: float | None = None
    #: Where the reading came from, how old it was, and the (non-secret) login name; None with no meter.
    source: str | None = None
    age_s: float | None = None
    login: str | None = None

    def describe(self) -> str:
        if self.state == "unavailable":
            return f"{self.engine} not configured"
        if self.state == "unknown":
            return f"{self.engine} meter unavailable"
        if self.state == "exhausted" and self.five_hour_pct is None and self.weekly_pct is None:
            return f"{self.engine} at its limit"
        parts = [f"{self.engine} 5h {self.five_hour_pct:.0f}%"] if self.five_hour_pct is not None else []
        if self.weekly_pct is not None:
            parts.append(f"weekly {self.weekly_pct:.0f}%")
        return " ".join(parts) or f"{self.engine} meter unavailable"

    def record(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "state": self.state,
            "headroom_pct": self.headroom,
            "five_hour_pct": self.five_hour_pct,
            "weekly_pct": self.weekly_pct,
            "usable_at": self.usable_at,
            "source": self.source,
            "age_s": self.age_s,
            "login": self.login,
        }


@dataclass(frozen=True)
class Decision:
    role: str
    #: None when deferred
    engine: str | None
    reason: str
    at: float
    pinned: bool = False
    retry_at: float | None = None
    builders: tuple[str, ...] = ()
    candidates: tuple[Candidate, ...] = ()
    #: What the caller resolved for the chosen engine; the router itself holds no model names.
    model: str = ""
    effort: str = ""

    @property
    def deferred(self) -> bool:
        return self.engine is None

    def row(self) -> str:
        """Return the timeline row text, e.g. `router: correctness -> codex (claude 5h 97%, ...; reason)`."""
        meters = "".join(f"{c.describe()}, " for c in self.candidates).removesuffix(", ")
        meters = f"{meters}; " if meters else ""
        target = "deferred" if self.deferred else self.engine
        reason = self.reason
        if self.retry_at is not None:
            reason += f"; retry at {datetime.fromtimestamp(self.retry_at, timezone.utc):%H:%M} UTC"
        return f"router: {self.role} -> {target} ({meters}{reason})"

    def record(self) -> dict[str, Any]:
        """Return the machine-readable form for the run report and the benchmark. No credentials."""
        return {
            "at": self.at,
            "role": self.role,
            "engine": self.engine,
            "deferred": self.deferred,
            "pinned": self.pinned,
            "reason": self.reason,
            "retry_at": self.retry_at,
            "builders": list(self.builders),
            "candidates": [c.record() for c in self.candidates],
            "model": self.model,
            "effort": self.effort,
        }


def _window(pct: float | None, resets_at: float | None, now: float) -> tuple[float | None, float | None]:
    """Return (used %, reset time) of a window if the reading still describes the running window."""
    if pct is None or not math.isfinite(pct):
        return None, None
    if resets_at is not None and resets_at <= now:
        return None, None
    return pct, resets_at


def _candidate(engine: str, meter: Meter | None, available: bool, now: float) -> Candidate:
    if not available:
        return Candidate(engine, "unavailable")
    if meter is None:
        return Candidate(engine, "unknown")
    seen: dict[str, Any] = {
        "source": meter.source,
        "age_s": max(0.0, now - meter.observed_at),
        "login": meter.account.partition(" ")[2] or meter.account,
    }
    five, five_at = _window(meter.five_hour_pct, meter.five_hour_resets_at, now)
    week, week_at = _window(meter.weekly_pct, meter.weekly_resets_at, now)
    # Only a refusal puts an engine out. A full window is a hint that it may refuse, so it ranks
    # last on headroom but is still tried: the meter can be wrong, and the refusal is what counts.
    if meter.limited_until is not None and meter.limited_until > now:
        return Candidate(engine, "exhausted", 0.0, five, week, meter.limited_until, **seen)
    # A full window stays full until its reset, however old the reading; anything else is only
    # evidence while it is recent.
    fresh = now - meter.observed_at <= STALE_AFTER_S
    full = any(
        pct is not None and pct >= ROUTE_AT_PCT and at is not None
        for pct, at in ((five, five_at), (week, week_at))
    )
    if not fresh and not full:
        return Candidate(engine, "unknown", None, five, week, **seen)
    known = [100.0 - pct for pct in (five, week) if pct is not None]
    if not known:
        return Candidate(engine, "unknown", **seen)
    return Candidate(engine, "usable", max(0.0, min(known)), five, week, **seen)


def exhausted(meter: Meter | None, now: float) -> bool:
    """Return True only when the engine refused and its reset is still ahead; a full window is not enough."""
    return _candidate("", meter, True, now).state == "exhausted"


def route(
    role: str,
    meters: Mapping[str, Meter | None],
    *,
    available: Sequence[str],
    now: float,
    preferred: str = "",
    pin: str = "",
    hard_pin: bool = False,
    builders: Sequence[str] = (),
    exclude: Sequence[str] = (),
    retry_s: float = DEFAULT_RETRY_S,
) -> Decision:
    """Pick the engine for one call.

    `available` is the engines with a login configured, in profile order. `preferred` is the
    caller's static choice (ties and no-meter fallback); `pin` is an explicit pin from policy
    or the caller. `hard_pin` waits for that engine instead of moving. `builders` is the engines
    that wrote the work, given only for a QA judge.
    `exclude` is engines that just refused this call: they are out whatever their meter says,
    which is how a retry asks again without calling an engine it knows cannot answer.
    """
    candidates = tuple(_candidate(e, meters.get(e), e in available, now) for e in ("claude", "codex"))
    # Profile order, not alphabetical: `available` says which engine comes first.
    order = {e: i for i, e in enumerate(available)}
    refused = tuple(e for e in exclude if e in order)
    candidates = tuple(
        # A refusal may predate any meter reading, so it has no known reset of its own.
        Candidate(
            c.engine, "exhausted", 0.0, c.five_hour_pct, c.weekly_pct, c.usable_at, c.source, c.age_s, c.login
        )
        if c.engine in refused and c.state != "exhausted"
        else c
        for c in candidates
    )
    states = {c.engine: c for c in candidates}
    builders = tuple(builders)

    def decide(engine: str | None, reason: str, **extra: Any) -> Decision:
        return Decision(role, engine, reason, now, builders=builders, candidates=candidates, **extra)

    notes: list[str] = []
    named: set[str] = set()  # engines a note already says are out, so the reason says it once
    for e in refused:
        notes.append(f"{e} refused")
        named.add(e)
    if pin:
        if pin not in order:
            if hard_pin:
                return decide(None, f"pin {pin} not configured", pinned=True, retry_at=now + retry_s)
            notes.append(f"pin {pin} ignored: not configured")
        elif pin in refused:
            pass
        elif states[pin].state != "exhausted":
            return decide(pin, f"pinned to {pin}", pinned=True)
        else:
            notes.append(f"pin {pin} exhausted")
            named.add(pin)

    if hard_pin and pin in order:
        retry_at = states[pin].usable_at or now + retry_s
        return decide(None, "; ".join(notes), pinned=True, retry_at=retry_at)

    if not order:
        return decide(None, "no engines configured", retry_at=now + retry_s)

    usable = [c for c in candidates if c.engine in order and c.state != "exhausted"]
    if not usable:
        reopen = [c.usable_at for c in candidates if c.engine in order]
        known = [t for t in reopen if t is not None]
        # An engine with no known reset could reopen any time, so poll if that comes first.
        if len(known) < len(reopen):
            known.append(now + retry_s)
        retry_at = min(known)
        # The pin is one of the engines said here, so its own note would repeat it.
        notes = [n for n in notes if not n.endswith(" exhausted")]
        if refused:
            notes.extend(f"{e} exhausted" for e in order if e not in refused)
        else:
            notes.append("both engines exhausted" if len(order) > 1 else f"{next(iter(order))} exhausted")
        return decide(None, "; ".join(notes), retry_at=retry_at)

    # A single builder asks for a cross-check. It is preferred, not required: when the other engine
    # is out the review runs on the builder's engine instead of stalling.
    cross_check = len(set(builders)) == 1
    other = [c for c in usable if c.engine not in builders] if cross_check else []
    if cross_check and not other:
        for e in order:
            if e not in builders and states[e].state == "exhausted":
                notes.append(f"cross-check unavailable: {e} limited")
                named.add(e)

    for c in candidates:
        if c.engine in order and c.state == "exhausted" and c.engine not in named:
            notes.append(f"{c.engine} exhausted")
            named.add(c.engine)

    if other and len(usable) > 1:
        unverified = [c for c in other if c.state != "usable"]
        notes.extend(f"{c.engine} meter unknown" for c in unverified)
        notes.append(f"QA differs from builder {'+'.join(builders)}")
        return _best(decide, other, preferred, order, notes)
    if builders and not cross_check:
        notes.append("builders mixed")
    if not any(c.state == "usable" for c in usable):
        # Nothing measured: the choice is the policy's alone, which it is anyway unless there is a tie.
        return _best(decide, usable, preferred, order, [*notes, "meters unavailable, static choice"])
    if len(usable) == 1:
        notes.append(f"only {usable[0].engine} has headroom")
    return _best(decide, usable, preferred, order, notes)


def _best(
    decide: Callable[..., Decision],
    pool: list[Candidate],
    preferred: str,
    order: Mapping[str, int],
    notes: list[str],
) -> Decision:
    """Return the preferred engine in `pool`; with no preference, the one with the most headroom.

    Headroom only ranks engines that are all measured. An unmeasured engine is assumed available, so
    a measured one does not out-rank it: the profile order decides instead.
    """
    if any(c.engine == preferred for c in pool):
        return decide(preferred, "; ".join(notes) or f"preferred {preferred}")
    if len(pool) > 1 and all(c.state == "usable" for c in pool):
        best = min(pool, key=lambda c: (-(c.headroom or 0.0), order[c.engine]))
        return decide(best.engine, "; ".join(notes) or "most headroom")
    best = min(pool, key=lambda c: order[c.engine])
    return decide(best.engine, "; ".join(notes) or "profile order")
