"""`_limit_reset.parse_reset`: when does a usage-limit notice say the limit reopens (#125)."""

from __future__ import annotations

import datetime as dt

import pytest

from flyte_agent_factory import _limit_reset, _meter, _usage


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> float:
    return dt.datetime(year, month, day, hour, minute, second, tzinfo=dt.timezone.utc).timestamp()


def _at(text: str, observed_at: float) -> tuple[dt.datetime, bool]:
    reset = _limit_reset.parse_reset(text, observed_at)
    assert reset is not None, text
    return dt.datetime.fromtimestamp(reset.at, tz=dt.timezone.utc), reset.stale


@pytest.mark.parametrize(
    ("text", "seen", "expected"),
    [
        # Same day, still ahead.
        ("resets 2:50am (UTC)", _utc(2026, 11, 11, 1, 10), _utc(2026, 11, 11, 2, 50)),
        ("You've hit your session limit · resets 6pm (UTC)", _utc(2026, 11, 11, 14), _utc(2026, 11, 11, 18)),
        ("Your limit will reset at 7pm (UTC)", _utc(2026, 11, 11, 14), _utc(2026, 11, 11, 19)),
        ("resets 12:30pm (UTC)", _utc(2026, 11, 11, 9), _utc(2026, 11, 11, 12, 30)),
        ("resets 14:50 (UTC)", _utc(2026, 11, 11, 12), _utc(2026, 11, 11, 14, 50)),
        # No zone is UTC.
        ("resets 2:50 AM", _utc(2026, 11, 11, 1, 10), _utc(2026, 11, 11, 2, 50)),
        # Across midnight: a time that is behind the clock but ahead within the horizon is tomorrow's.
        ("resets 1:00am (UTC)", _utc(2026, 11, 11, 23, 30), _utc(2026, 11, 12, 1, 0)),
        ("resets 12am (UTC)", _utc(2026, 11, 11, 22, 0), _utc(2026, 11, 12, 0, 0)),
        # Dated, from Codex and from the Claude TUI.
        ("try again at Nov 16th, 2026 12:40 AM", _utc(2026, 11, 11, 13), _utc(2026, 11, 16, 0, 40)),
        (
            "You've hit your usage limit. Upgrade to Pro, or try again at Nov 16th, 2026 12:40 AM.",
            _utc(2026, 11, 11, 13),
            _utc(2026, 11, 16, 0, 40),
        ),
        ("resets Oct 24, 3pm (UTC)", _utc(2026, 10, 20, 9), _utc(2026, 10, 24, 15)),
        ("resets 10/24 3pm (UTC)", _utc(2026, 10, 20, 9), _utc(2026, 10, 24, 15)),
        # A yearless date already past is next year's.
        ("resets Jan 2, 3pm (UTC)", _utc(2026, 11, 11, 9), _utc(2027, 1, 2, 15)),
        # 2026-11-11 is a Wednesday.
        ("resets Mon 9am (UTC)", _utc(2026, 11, 11, 13), _utc(2026, 11, 16, 9)),
        ("resets Wednesday 9pm (UTC)", _utc(2026, 11, 11, 13), _utc(2026, 11, 11, 21)),
        # Relative.
        ("try again in 3 days", _utc(2026, 11, 11, 13), _utc(2026, 11, 14, 13)),
        ("resets in 4 hours 5 minutes", _utc(2026, 11, 11, 13), _utc(2026, 11, 11, 17, 5)),
    ],
)
def test_reset_resolves_against_when_it_was_seen(text, seen, expected):
    at, stale = _at(text, seen)
    assert at.timestamp() == expected
    assert stale is False


@pytest.mark.real_reset
@pytest.mark.parametrize(
    ("text", "seen", "expected"),
    [
        # Codex prints a bare clock time only for a reset later the same day, however far ahead.
        (
            "You've hit your usage limit. Try again at 11:40 PM.",
            _utc(2026, 11, 16, 10),
            _utc(2026, 11, 16, 23, 40),
        ),
        ("try again at 9:05 PM", _utc(2026, 11, 16, 0, 30), _utc(2026, 11, 16, 21, 5)),
        # An ISO date is read as its date, never as its clock part alone.
        ("try again at 2026-11-16 00:40", _utc(2026, 11, 11, 13), _utc(2026, 11, 16, 0, 40)),
    ],
)
def test_a_codex_try_again_clock_time_is_later_today(monkeypatch, text, seen, expected):
    """ "Try again at 11:40 PM." seen at 10:00Z is 23:40 that day, not yesterday's and stale."""
    at, stale = _at(text, seen)
    assert at.timestamp() == expected
    assert stale is False
    monkeypatch.setattr(vars(_usage)["time"], "time", lambda: seen)
    assert _usage._refusal_until(text, seen) == expected


def test_a_codex_clock_time_seen_just_after_midnight_is_yesterdays():
    seen = _utc(2026, 11, 17, 0, 10)
    at, stale = _at("Try again at 11:50 PM.", seen)
    assert at.timestamp() == _utc(2026, 11, 16, 23, 50) and stale


def test_a_clock_time_well_past_is_stale_not_tomorrow():
    """llm-gateway-probe: "resets 2:50am (UTC)" seen at 04:31Z waited 22 h for a limit already over."""
    seen = _utc(2026, 11, 11, 4, 31)
    at, stale = _at("resets 2:50am (UTC)", seen)
    assert at == dt.datetime(2026, 11, 11, 2, 50, tzinfo=dt.timezone.utc)
    assert stale and at.timestamp() <= seen


def test_a_clock_time_just_passed_is_now_and_stale():
    seen = _utc(2026, 11, 11, 2, 51)
    at, stale = _at("resets 2:50am (UTC)", seen)
    assert at.timestamp() == _utc(2026, 11, 11, 2, 50) and stale


def test_a_time_seen_a_day_late_is_yesterdays():
    # Beyond the horizon ahead: the notice was written yesterday, so it has reset.
    seen = _utc(2026, 11, 11, 3, 0)
    at, stale = _at("resets 11pm (UTC)", seen)
    assert at.timestamp() == _utc(2026, 11, 10, 23) and stale


def test_a_reset_just_before_midnight_seen_after_it_is_stale_yesterday():
    seen = _utc(2026, 10, 19, 0, 10)
    at, stale = _at("resets 11:30pm (UTC)", seen)
    assert at.timestamp() == _utc(2026, 10, 18, 23, 30) and stale


def test_a_dated_reset_already_past_is_stale():
    reset = _limit_reset.parse_reset("try again at Nov 6th, 2026 12:40 AM", _utc(2026, 11, 11, 13))
    assert reset is not None and reset.stale and reset.at == _utc(2026, 11, 6, 0, 40)


@pytest.mark.parametrize(
    "text",
    [
        "resets 3pm (PDT)",
        "resets 3pm PST",
        "resets 3pm (America/Los_Angeles)",
        "resets soon",
        "resets 1:99am (UTC)",
        "resets 13pm (UTC)",
        "resets Feb 30 3pm (UTC)",
        "",
        "limit reached",
    ],
)
def test_unknown_or_non_utc_text_is_none(text):
    assert _limit_reset.parse_reset(text, _utc(2026, 11, 11, 13)) is None


def test_a_meter_report_blob_carries_its_own_reset():
    until = _utc(2026, 11, 20, 8)
    meter = _meter.Meter("claude", "limit-text", _utc(2026, 11, 11, 13), limited_until=until)
    blob = _meter.report_comment(meter)
    reset = _limit_reset.parse_reset(f"Run failed {blob}", _utc(2026, 11, 11, 13))
    assert reset is not None and reset.at == until and not reset.stale


@pytest.mark.parametrize(
    ("text", "seen", "expected"),
    [
        # A yearless date just behind the clock is this year's and has reset, not next year's (a usage
        # limit is never a year away, and the router never shortens a known limit).
        ("resets Nov 10, 3pm (UTC)", _utc(2026, 11, 11, 9), _utc(2026, 11, 10, 15)),
        ("resets Oct 24, 3pm (UTC)", _utc(2026, 10, 25, 9), _utc(2026, 10, 24, 15)),
        # Across New Year the nearest year still wins.
        ("resets Dec 31, 11pm (UTC)", _utc(2027, 1, 1, 9), _utc(2026, 12, 31, 23)),
        # A weekday already past today is today's, so it has reset, not next week's.
        ("resets Wed 9am (UTC)", _utc(2026, 11, 11, 10, 30), _utc(2026, 11, 11, 9)),
    ],
)
def test_a_date_or_weekday_just_past_is_stale_not_a_year_or_week_ahead(text, seen, expected):
    at, stale = _at(text, seen)
    assert at.timestamp() == expected
    assert stale is True


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("resets Jan 2, 3pm (UTC)", _utc(2027, 1, 2, 15)),
        ("try again in 1h 30m", _utc(2026, 11, 11, 14, 30)),
        ("try again in 2 weeks", _utc(2026, 11, 25, 13)),
        ("try again in 45 seconds", _utc(2026, 11, 11, 13, 0, 45)),
        ("resets 3pm (Etc/UTC)", _utc(2026, 11, 11, 15)),
        ("resets 3pm (UTC+0)", _utc(2026, 11, 11, 15)),
        ("resets 3pm (UTC+00:00)", _utc(2026, 11, 11, 15)),
    ],
)
def test_more_spellings_of_a_reset_still_ahead(text, expected):
    at, stale = _at(text, _utc(2026, 11, 11, 13))
    assert at.timestamp() == expected
    assert stale is False


@pytest.mark.parametrize("text", ["try again in a while", "try again in 3 fortnights", "resets 3pm (UTC+2)"])
def test_a_relative_or_zoned_reset_it_cannot_read_is_none_not_now(text):
    assert _limit_reset.parse_reset(text, _utc(2026, 11, 11, 13)) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Only the duration right after "in" counts, not numbers later in the notice.
        ("try again in 22 minutes (the limit resets every 5h)", _utc(2026, 11, 11, 13, 22)),
        ("try again in 30 minutes. Plan: 20 m tokens/week", _utc(2026, 11, 11, 13, 30)),
        ("resets in 4 hours, 5 minutes", _utc(2026, 11, 11, 17, 5)),
        ("resets in 4 hours and 5 minutes", _utc(2026, 11, 11, 17, 5)),
    ],
)
def test_a_relative_reset_reads_only_its_own_duration(text, expected):
    at, stale = _at(text, _utc(2026, 11, 11, 13))
    assert at.timestamp() == expected and stale is False


def test_a_yearless_feb_29_with_no_near_year_is_none():
    assert _limit_reset.parse_reset("resets Feb 29, 3pm (UTC)", _utc(2027, 3, 1, 9)) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("try again in 1h30m", _utc(2026, 11, 11, 14, 30)),
        ("try again in 2.5 hours", _utc(2026, 11, 11, 15, 30)),
        ("resets in 1 hr. 20 min", _utc(2026, 11, 11, 14, 20)),
    ],
)
def test_compact_and_fractional_durations(text, expected):
    at, stale = _at(text, _utc(2026, 11, 11, 13))
    assert at.timestamp() == expected and stale is False


@pytest.mark.parametrize("text", ["resets 3pm UTC-5", "resets 3pm GMT+2"])
def test_an_unbracketed_offset_other_than_zero_is_none(text):
    assert _limit_reset.parse_reset(text, _utc(2026, 11, 11, 13)) is None


def test_an_unbracketed_zero_offset_is_utc():
    at, _ = _at("resets 3pm UTC+0", _utc(2026, 11, 11, 13))
    assert at.timestamp() == _utc(2026, 11, 11, 15)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # A full stop ends the duration: the next sentence's numbers are not part of it.
        ("resets in 4 hours. 2 days until your plan renews", _utc(2026, 11, 11, 17)),
        ("try again in 2 hours. 30 minutes of usage remain", _utc(2026, 11, 11, 15)),
    ],
)
def test_a_relative_reset_ends_at_the_sentence(text, expected):
    at, _ = _at(text, _utc(2026, 11, 11, 13))
    assert at.timestamp() == expected


def test_a_bare_clock_time_later_the_same_day_is_ahead_not_stale():
    """#194 (1): "resets 3pm (UTC)" seen at 08:00Z was read as yesterday's 15:00 and stale (7 h ahead)."""
    at, stale = _at("resets 3pm (UTC)", _utc(2026, 11, 11, 8))
    assert at.timestamp() == _utc(2026, 11, 11, 15) and stale is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # #194 (3): a T separator parsed to None.
        ("try again at 2026-11-16T00:40:00Z", _utc(2026, 11, 16, 0, 40)),
        ("resets 2026-11-16T00:40Z", _utc(2026, 11, 16, 0, 40)),
        ("resets 2026-11-16T00:40:00.5Z", _utc(2026, 11, 16, 0, 40) + 0.5),
        ("resets 2026-11-16T00:40:00+0000", _utc(2026, 11, 16, 0, 40)),
        ("resets 2026-11-16t00:40:00z", _utc(2026, 11, 16, 0, 40)),
        # A numeric offset names the instant exactly, so it is converted, unlike a zone name.
        ("resets 2026-11-16T05:40:00+05:00", _utc(2026, 11, 16, 0, 40)),
        ("resets 2026-11-15T19:40:00-05:00.", _utc(2026, 11, 16, 0, 40)),
        # No zone is UTC, as everywhere else.
        ("try again at 2026-11-16T00:40:00", _utc(2026, 11, 16, 0, 40)),
        ("try again at 2026-11-16T00:40:00 (UTC)", _utc(2026, 11, 16, 0, 40)),
    ],
)
def test_an_iso_timestamp_with_a_t_parses_to_its_instant(text, expected):
    at, stale = _at(text, _utc(2026, 11, 11, 13))
    assert at.timestamp() == expected and stale is False


@pytest.mark.parametrize(
    "text", ["resets 2026-11-16T00:40:00 PST", "resets 2026-11-16T00:40:00 (America/Denver)"]
)
def test_an_iso_timestamp_followed_by_a_named_zone_is_none(text):
    assert _limit_reset.parse_reset(text, _utc(2026, 11, 11, 13)) is None


@pytest.mark.parametrize("text", ["Try  again at 3:00 PM.", "try\nagain at 3:00 PM", "TRY AGAIN at 3:00 PM"])
def test_codex_try_again_is_recognised_however_it_is_spaced(text):
    """#194 (4): `same_day` keyed on the literal "try again"; a double space missed Codex's same-day rule."""
    # Seen at 16:00Z: Codex's bare time is today's 15:00, just past, not tomorrow's.
    at, stale = _at(text, _utc(2026, 11, 16, 16))
    assert at.timestamp() == _utc(2026, 11, 16, 15) and stale
    # And at 10:00Z, 15:00 is ahead the same day, as for the single-spaced spelling.
    assert _at(text, _utc(2026, 11, 16, 10)) == _at("try again at 3:00 PM", _utc(2026, 11, 16, 10))


def test_a_date_with_no_lead_word_is_none():
    """#194 (2), closed: only text after a lead word ("resets", "try again") is read; a quoted date is not."""
    assert _limit_reset.parse_reset("Oct 24, 3pm (UTC)", _utc(2026, 10, 20, 9)) is None


@pytest.mark.real_reset
def test_the_300_s_placeholder_is_only_a_floor_under_the_notice(monkeypatch):
    """#194 (5), closed: a refusal with no reset header waits 300 s unless its notice names a later reset."""
    now = _utc(2026, 11, 11, 13)
    monkeypatch.setattr(vars(_usage)["time"], "time", lambda: now)
    event = {"is_error": True, "api_error_status": 429, "result": "resets 3pm (UTC)"}
    meter = _meter.from_claude_events([event], now=now)
    assert meter is not None and meter.limited_until == now + 300
    assert _usage._keep_later_reset("resets 3pm (UTC)", meter.limited_until) == _utc(2026, 11, 11, 15)
    # A stale notice (12:00 seen at 13:00) names nothing ahead: the floor stands.
    assert _usage._keep_later_reset("resets 12pm (UTC)", meter.limited_until) == now + 300


def test_a_bare_clock_time_early_tomorrow_is_ahead_not_stale():
    """#194 (1): a next-day reset within half a day, "resets 2am" seen at 18:00Z, is tomorrow's, not stale."""
    at, stale = _at("resets 2am (UTC)", _utc(2026, 11, 11, 18))
    assert at.timestamp() == _utc(2026, 11, 12, 2) and stale is False


def test_a_bare_clock_time_an_hour_past_stays_stale_not_tomorrow():
    """#194 decision-001 pins this: "resets 3pm" seen at 16:00Z is today's 15:00, stale, not tomorrow's.

    Text alone cannot tell a reset 23 h ahead from a notice seen an hour after it reset; reading it as
    tomorrow is the 22 h stall `test_a_clock_time_well_past_is_stale_not_tomorrow` pins.
    """
    at, stale = _at("resets 3pm (UTC)", _utc(2026, 11, 11, 16))
    assert at.timestamp() == _utc(2026, 11, 11, 15) and stale
