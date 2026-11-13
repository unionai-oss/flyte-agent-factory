"""Read when a usage-limit notice says the limit reopens.

Pure: the text and the time it was *seen* go in, a `Reset` comes out. The same text parsed an hour
later must give the same answer, so nothing here reads the clock. Claude words its notice as a clock
time ("resets 2:50am (UTC)"), Codex as a date ("try again at Nov 16th, 2026 12:40 AM"), and a weekly
limit as a weekday or a date.

A clock time with no date is read against `observed_at`: the occurrence from just behind it (the grace)
to half a day ahead. Claude prints a bare time for any reset under a day away, a weekly limit's last day
included, so it can be well beyond the five-hour window. A time already well past means the notice is old
and the limit has reset, not that it resets tomorrow (`stale`). Text alone cannot tell a reset 23 h ahead
from a notice seen an hour after it reset; reading it as tomorrow is the 22 h stall that
test_a_clock_time_well_past_is_stale_not_tomorrow pins, while reading it as stale costs one refused retry
per LIMIT_RETRY_S (#194).

Codex's "try again at" prints a bare clock time only when the reset is later the same day, so that one is
today's however far ahead. Text with no zone is UTC; a named zone other than UTC is not converted, because
a wrong guess would wait for the wrong hour, so it gives None. A numeric offset in an ISO-8601
timestamp names its instant exactly, so it is converted.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from flyte_agent_factory import _meter

#: A clock time this far past is "just reset", still today's.
GRACE_S = 3600.0
#: A clock-only reset further ahead than this was written yesterday. Not the five-hour window: a weekly
#: limit's last day prints a bare time too. Half a day: "3pm" seen at 08:00 is today's, "11pm" at 03:00
#: last night's.
HORIZON_S = 12 * 3600.0

_DAY = dt.timedelta(days=1)
_HALF_YEAR_S = 183 * 86400.0
_MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
_WEEKDAYS = {d: i for i, d in enumerate("mon tue wed thu fri sat sun".split())}

# What the notice says it is about; the reset time is searched only after it, so a stray "3:00" in the
# text around a limit is never read as one. So a date with no lead word ("Oct 24, 3pm (UTC)") is None by
# design: neither engine prints one bare, and a date quoted elsewhere is not a reset (#194).
_LEAD = re.compile(r"resets?|try\s+again|available", re.IGNORECASE)
_RELATIVE = re.compile(r"\s*(?:at\s+)?in\s+(?=\d)", re.IGNORECASE)
_RELATIVE_PART = re.compile(
    # A full stop ends it ("in 4 hours. 2 days until ..."), except after an abbreviation ("1 hr. 20").
    r"(?:\s*,?\s*(?:and\s+)?)?(\d+(?:\.\d+)?)\s*(?:(week|day|hour|minute|second)s?|(wk|hr|h|min|m|sec|s)s?\.?)(?![a-z])",
    re.IGNORECASE,
)
_MONTH_DAY = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b",
    re.IGNORECASE,
)
# An ISO-8601 timestamp with its zone (`2026-11-16T00:40:00Z`, `...+05:00`) is one exact instant.
_ISO_INSTANT = re.compile(
    r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}(?::?\d{2})?)(?![\w:])", re.IGNORECASE
)
# The date, and the `T` of a timestamp with no zone, so the clock after it is read on its own.
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})(?:T(?=\d)|\b)", re.IGNORECASE)
_NUMERIC_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\b")
_WEEKDAY = re.compile(r"\b(mon|tue|wed|thu|fri|sat|sun)(?:day|sday|nesday|rsday|urday|s)?\b", re.IGNORECASE)
# `3pm`, `2:50 AM` or `14:50`; a bare number is not a time, so "16" in "Nov 16th" never is.
_CLOCK = re.compile(r"\b(\d{1,2})(?::(\d{2})(?::\d{2}(?:\.\d+)?)?)?(?:\s*(am|pm)\b)?", re.IGNORECASE)
_ZONE = re.compile(
    r"\s*(?:\(\s*([^)]*?)\s*\)|\b((?:UTC|GMT)(?:[+-]\d{1,2}(?::?\d{2})?)?|Z|[PMCE][SD]T)\b)",
    re.IGNORECASE,
)
_UTC = {"", "UTC", "GMT", "Z"}
# `Etc/UTC`, `UTC+0` and `GMT+00:00` are UTC too.
_UTC_SPELLING = re.compile(r"(?:ETC/)?(UTC|GMT|Z)?(?:[+-]0{1,2}(?::?00)?)?")
_UNIT_S = {
    "week": 7 * 86400,
    "wk": 7 * 86400,
    "day": 86400,
    "hour": 3600,
    "hr": 3600,
    "h": 3600,
    "minute": 60,
    "min": 60,
    "m": 60,
    "second": 1,
    "sec": 1,
    "s": 1,
}


@dataclass(frozen=True)
class Reset:
    at: float  # epoch seconds UTC
    stale: bool  # True: the time named has already passed, so the limit has reset


def parse_reset(text: str, observed_at: float) -> Reset | None:
    """Return when `text` says the limit reopens, judged against `observed_at`; None if it does not say.

    A meter-report blob (a failed Flyte action carries its reset that way) wins over free text.
    """
    carried = [m.limited_until for m in _meter.from_report(text) if m.limited_until is not None]
    if carried:
        return _reset(max(carried), observed_at)
    lead = _LEAD.search(text or "")
    if not lead:
        return None
    rest = text[lead.end() :]
    if relative := _RELATIVE.match(rest):
        # Only the run of durations right after "in": a later "every 5h" is not part of it.
        parts, at = [], relative.end()
        while part := _RELATIVE_PART.match(rest, at):
            parts.append((part.group(1), part.group(2) or part.group(3)))
            at = part.end()
        if not parts:  # "in 3 fortnights": a unit it cannot read is no time, not "now"
            return None
        return _reset(observed_at + sum(float(n) * _UNIT_S[unit.lower()] for n, unit in parts), observed_at)
    return _dated(rest, observed_at, same_day=lead.group()[0].lower() == "t")


def _reset(at: float, observed_at: float) -> Reset:
    return Reset(at, at <= observed_at)


def _dated(rest: str, observed_at: float, *, same_day: bool = False) -> Reset | None:
    """Resolve a clock time, optionally with a date or weekday, in `rest`.

    `same_day`: a bare clock time is today's (Codex's wording), not placed within the horizon.
    """
    if instant := _ISO_INSTANT.search(rest):
        try:
            at = dt.datetime.fromisoformat(instant.group().upper())
        except ValueError:  # "2026-02-30T..." or a "+25:00" offset
            return None
        return _reset(at.timestamp(), observed_at)
    seen = dt.datetime.fromtimestamp(observed_at, tz=dt.timezone.utc)
    date: tuple[int, int, int | None] | None = None  # month, day, year
    weekday: int | None = None
    span: re.Match[str] | None = None
    if span := _ISO_DATE.search(rest):
        date = (int(span.group(2)), int(span.group(3)), int(span.group(1)))
    elif span := _MONTH_DAY.search(rest):
        date = (
            _MONTHS[span.group(1).lower()],
            int(span.group(2)),
            int(span.group(3)) if span.group(3) else None,
        )
    elif span := _NUMERIC_DATE.search(rest):
        date = (int(span.group(1)), int(span.group(2)), int(span.group(3)) if span.group(3) else None)
    elif span := _WEEKDAY.search(rest):
        weekday = _WEEKDAYS[span.group(1).lower()]
    # Blank the date out, so its day and year are not read as the clock.
    clock_text = (
        rest
        if span is None
        else rest[: span.start()] + " " * (span.end() - span.start()) + rest[span.end() :]
    )
    clock = next((m for m in _CLOCK.finditer(clock_text) if m.group(2) or m.group(3)), None)
    if clock is None:
        return None
    hour, minute = int(clock.group(1)), int(clock.group(2) or 0)
    meridiem = (clock.group(3) or "").lower()
    if minute > 59 or (hour not in range(1, 13) if meridiem else hour > 23):
        return None
    if meridiem:
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    zone = _ZONE.match(clock_text, clock.end())
    if zone and not _UTC_SPELLING.fullmatch(
        (zone.group(1) if zone.group(1) is not None else zone.group(2)).upper()
    ):
        return None
    try:
        today = seen.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if date:
            month, day, year = date
            if year is not None:
                at = today.replace(year=year, month=month, day=day)
            else:
                # The nearest year: a date just behind the clock has reset, it is not next year's.
                at = min(
                    (today.replace(year=y, month=month, day=day) for y in _years(seen.year, month, day)),
                    key=lambda c: abs(c.timestamp() - observed_at),
                )
                if abs(at.timestamp() - observed_at) > _HALF_YEAR_S:  # Feb 29 with no leap year near
                    return None
        elif weekday is not None:
            # This week's occurrence, from today on; today's already past has reset (a weekly notice
            # naming today means the window it ends is over, not that the next one is a week away).
            at = today + dt.timedelta(days=(weekday - seen.weekday()) % 7)
        else:
            return _clock_only(today, observed_at, same_day=same_day)
    except ValueError:
        return None
    return _reset(at.timestamp(), observed_at)


def _years(year: int, month: int, day: int) -> list[int]:
    """Return the years near `year` that have `month`/`day` (Feb 29 skips the others); ValueError if none."""
    years = [y for y in (year - 1, year, year + 1) if _exists(y, month, day)]
    if not years:
        raise ValueError(f"no {month}/{day}")
    return years


def _exists(year: int, month: int, day: int) -> bool:
    try:
        dt.date(year, month, day)
    except ValueError:
        return False
    return True


def _clock_only(today: dt.datetime, observed_at: float, *, same_day: bool = False) -> Reset:
    """Place a bare clock time: the occurrence within [grace behind, horizon ahead], else the latest past.

    With `same_day` it is today's occurrence, ahead or (stale) behind, unless yesterday's is within the
    grace: a notice seen just after midnight names the reset that has just passed.
    """
    if same_day:
        yesterday = (today - _DAY).timestamp()
        return _reset(yesterday if yesterday >= observed_at - GRACE_S else today.timestamp(), observed_at)
    candidates = sorted(c.timestamp() for c in (today - _DAY, today, today + _DAY))
    window = [c for c in candidates if observed_at - GRACE_S <= c <= observed_at + HORIZON_S]
    if window:
        return _reset(window[0], observed_at)
    # Nothing is near: the notice is older than its own window, so the limit has reset.
    return Reset(max(c for c in candidates if c <= observed_at), True)
