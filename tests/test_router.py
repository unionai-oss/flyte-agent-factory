"""The engine router: a pure decision table, and proof that every call path goes through it."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import flyte
import pytest

from flyte_agent_factory import (
    _meter,
    _policy,
    _review,
    _reviewer,
    _router,
    _session_config,
    _session_setup,
    _subagent_run,
    _supervise_support,
    _timeline,
    _usage,
    agent,
)
from flyte_agent_factory._delegation import Delegations

NOW = 1_800_000_000.0
LATER = NOW + 6 * 3600


def claude(five=None, week=None, *, five_resets=LATER, week_resets=NOW + 3 * 86400, at=NOW, limited=None):
    return _meter.Meter("claude work", "oauth-usage", at, five, five_resets, week, week_resets, [], limited)


def codex(five=None, week=None, *, five_resets=LATER, week_resets=NOW + 3 * 86400, at=NOW, limited=None):
    return _meter.Meter("codex", "app-server", at, five, five_resets, week, week_resets, [], limited)


def route(role="planner", meters=None, **kwargs):
    kwargs.setdefault("available", ("claude", "codex"))
    kwargs.setdefault("now", NOW)
    return _router.route(role, meters or {}, **kwargs)


# --------------------------------------------------------------------------- #
# The decision table
# --------------------------------------------------------------------------- #


def test_both_engines_with_headroom_pick_the_one_with_more_left():
    assert route(meters={"claude": claude(97), "codex": codex(12)}).engine == "codex"
    assert route(meters={"claude": claude(10), "codex": codex(60)}).engine == "claude"


def test_the_tightest_window_decides_the_headroom():
    # Claude's 5h window is fine, its weekly is nearly gone: Codex has more left.
    decision = route(meters={"claude": claude(5, 93), "codex": codex(40, 20)})
    assert decision.engine == "codex"


def test_a_tie_keeps_the_preferred_engine():
    meters = {"claude": claude(50), "codex": codex(50)}
    assert route(meters=meters, preferred="codex").engine == "codex"
    assert route(meters=meters, preferred="claude").engine == "claude"
    assert route(meters=meters).engine == "claude", "no preference: the first available engine"


@pytest.mark.parametrize("used", [95.0, 99.0, 99.9])
def test_there_is_no_reserve_an_engine_is_usable_until_it_is_full(used):
    decision = route(meters={"claude": claude(used), "codex": None})
    assert decision.engine == "claude"
    assert not decision.deferred


def test_only_one_engine_with_headroom_gets_the_call():
    decision = route(meters={"claude": claude(100, limited=NOW + 900), "codex": codex(30)})
    assert decision.engine == "codex"
    assert "claude" in decision.reason and "exhausted" in decision.reason
    decision = route(meters={"claude": claude(30), "codex": codex(10, 100)})
    assert decision.engine == "claude"


def test_an_engine_without_a_login_is_not_a_candidate():
    decision = route(
        meters={"claude": claude(100, limited=NOW + 900), "codex": codex(0)}, available=("claude",)
    )
    assert decision.deferred, "codex has headroom but is not configured"


def test_both_exhausted_defers_until_the_earliest_reset_that_helps():
    soon, later = NOW + 1800, NOW + 7200
    decision = route(
        meters={"claude": claude(100, limited=later), "codex": codex(100, limited=soon)},
    )
    assert decision.deferred and decision.engine is None
    assert decision.retry_at == soon
    assert "both engines exhausted" in decision.reason


def test_a_refused_engine_is_back_at_its_refusals_reset_whatever_its_windows_say():
    # Owner rule 2: only the refusal blocks. Claude's refusal clears in 30 min; its weekly window reads
    # full for three days, which is a hint and does not hold it out longer. This used to assert that a
    # full weekly window kept the engine blocked until it reset (NOW + 7200, Codex's reset, came first).
    decision = route(
        meters={
            "claude": claude(100, 100, week_resets=NOW + 3 * 86400, limited=NOW + 1800),
            "codex": codex(100, limited=NOW + 7200),
        },
    )
    assert decision.retry_at == NOW + 1800


def test_a_recorded_refusal_blocks_until_its_reset_time():
    decision = route(meters={"claude": claude(10, limited=NOW + 900), "codex": codex(limited=NOW + 1800)})
    assert decision.deferred and decision.retry_at == NOW + 900
    expired = route(meters={"claude": claude(10, limited=NOW - 1), "codex": codex(limited=NOW + 1800)})
    assert expired.engine == "claude"


def test_a_deferral_without_a_known_reset_retries_after_the_poll_interval():
    # A refusal passed in as `exclude` has no reset of its own.
    decision = route(
        meters={"claude": claude(10), "codex": codex(10)}, exclude=("claude", "codex"), retry_s=123.0
    )
    assert decision.deferred and decision.retry_at == NOW + 123.0


def test_a_window_that_has_reset_is_unknown_not_exhausted():
    old = claude(100, five_resets=NOW - 5)
    decision = route(meters={"claude": old, "codex": codex(limited=NOW + 900)})
    assert decision.engine == "claude"
    assert "unavailable" in decision.reason


def test_a_stale_reading_is_unknown_but_a_full_window_stays_full_until_its_reset():
    stale = NOW - 3 * 3600
    # Owner rule 1: a stale reading is unknown, and unknown is available; Codex's fresher meter does not
    # out-rank it. This used to assert codex.
    stale_claude = route(meters={"claude": claude(10, at=stale), "codex": codex(50)})
    assert stale_claude.engine == "claude"
    assert next(c for c in stale_claude.candidates if c.engine == "claude").state == "unknown"
    full = route(meters={"claude": claude(100, at=stale), "codex": codex(90)})
    assert full.engine == "codex" and full.retry_at is None


def test_an_unknown_meter_does_not_lose_to_known_headroom():
    # Owner rules 1 and 4: an unknown meter is not exhausted, so the preference stands. This was
    # `test_known_headroom_beats_an_unknown_meter`, which asserted claude.
    assert route(meters={"claude": claude(80), "codex": None}, preferred="codex").engine == "codex"


# --- pins ------------------------------------------------------------------ #


def test_an_explicit_pin_wins_over_better_headroom_and_is_logged_as_a_pin():
    decision = route(meters={"claude": claude(10), "codex": codex(90)}, pin="claude", preferred="claude")
    assert decision.engine == "claude" and decision.pinned
    assert "pinned" in decision.reason
    assert "pinned" in decision.row()


def test_a_pin_on_an_exhausted_engine_gives_way_and_says_so():
    decision = route(
        meters={"claude": claude(30), "codex": codex(limited=NOW + 900)}, pin="codex", preferred="codex"
    )
    assert decision.engine == "claude" and not decision.pinned
    assert "pin" in decision.reason and "codex" in decision.reason


def test_a_pin_on_an_engine_without_a_login_is_ignored():
    decision = route(meters={"claude": claude(30)}, available=("claude",), pin="codex")
    assert decision.engine == "claude" and "codex" in decision.reason


def test_a_pin_on_an_unknown_meter_still_wins():
    assert route(meters={}, pin="codex").engine == "codex"


# --- QA prefers a different engine ------------------------------------------ #


def test_qa_prefers_an_engine_other_than_the_builders_even_with_less_headroom():
    decision = route("correctness", {"claude": claude(5), "codex": codex(80)}, builders=("claude",))
    assert decision.engine == "codex"
    assert "differs from builder claude" in decision.reason
    decision = route("correctness", {"claude": claude(80), "codex": codex(5)}, builders=("codex",))
    assert decision.engine == "claude"


def test_qa_runs_on_the_builders_engine_when_it_is_the_only_one_with_headroom():
    decision = route("craft", {"claude": claude(40), "codex": codex(limited=NOW + 900)}, builders=("claude",))
    assert decision.engine == "claude"
    assert "differs" not in decision.reason
    assert "only claude" in decision.reason


def test_qa_with_a_mixed_builder_has_no_different_engine_and_says_so():
    decision = route("completeness", {"claude": claude(20), "codex": codex(60)}, builders=("claude", "codex"))
    assert decision.engine == "claude", "ordinary ranking: 80% left beats 40%"
    assert "mixed" in decision.reason


def test_a_role_without_builders_is_not_treated_as_qa():
    decision = route("planner", {"claude": claude(20), "codex": codex(60)}, builders=())
    assert "differs" not in decision.reason


def test_qa_with_unknown_meters_keeps_the_static_choice():
    decision = route("correctness", {}, preferred="codex", builders=("claude",))
    assert decision.engine == "codex"


# --- meters unavailable ------------------------------------------------------ #


def test_without_any_meter_the_static_choice_is_used_and_the_reason_says_why():
    decision = route(meters={}, preferred="codex")
    assert decision.engine == "codex"
    assert "meters unavailable" in decision.reason
    assert route(meters={"claude": None, "codex": None}, preferred="claude").engine == "claude"
    assert route(meters={}, preferred="codex", available=("claude",)).engine == "claude"


def test_a_meter_with_no_window_readings_is_unavailable():
    blank = _meter.Meter("claude work", "oauth-usage", NOW)
    assert route(meters={"claude": blank, "codex": blank}, preferred="codex").engine == "codex"


# --- the record -------------------------------------------------------------- #


def test_the_row_names_the_role_the_engine_the_meters_and_the_reason():
    decision = route("correctness", {"claude": claude(97), "codex": codex(12)}, builders=("claude",))
    assert decision.row() == (
        "router: correctness -> codex (claude 5h 97%, codex 5h 12%; QA differs from builder claude)"
    )


def test_the_deferred_row_says_when_to_retry():
    decision = route(meters={"claude": claude(limited=NOW + 900), "codex": codex(limited=NOW + 900)})
    assert decision.row().startswith("router: planner -> deferred (")
    assert "retry" in decision.row()


def test_no_mounted_credentials_defers_with_a_reason():
    decision = route(available=())
    assert decision.deferred and decision.reason == "no engines configured"
    assert decision.retry_at == NOW + _router.DEFAULT_RETRY_S
    assert all(candidate.state == "unavailable" for candidate in decision.candidates)


def test_the_record_is_json_and_carries_what_the_report_and_benchmark_need():
    decision = route("correctness", {"claude": claude(97, 40), "codex": None}, builders=("claude",))
    record = json.loads(json.dumps(decision.record()))
    # Changed on purpose (item 6): codex's meter is unknown, not exhausted, so QA still goes to the
    # engine that did not build the work. It used to stay on the builder's claude.
    assert record["role"] == "correctness" and record["engine"] == "codex"
    assert record["at"] == NOW and record["builders"] == ["claude"]
    assert record["pinned"] is False and record["deferred"] is False
    by_engine = {c["engine"]: c for c in record["candidates"]}
    assert by_engine["claude"]["five_hour_pct"] == 97 and by_engine["claude"]["weekly_pct"] == 40
    assert by_engine["claude"]["state"] == "usable"
    assert by_engine["codex"]["state"] == "unknown"
    assert record["reason"]


def test_the_record_never_holds_a_credential():
    text = json.dumps(route(meters={"claude": claude(1), "codex": codex(1)}).record())
    # `oauth-usage` is the name of where a reading came from (it is recorded on purpose), not a credential.
    text = text.replace("oauth-usage", "usage-api")
    for word in ("token", "secret", "auth", "password"):
        assert word not in text.lower()


# --- round 2 ------------------------------------------------------------------ #


def test_a_refused_engine_is_excluded_and_a_limited_one_defers():
    decision = route(
        "planner",
        {"claude": claude(10), "codex": codex(100, limited=NOW + 1800)},
        exclude=("claude",),
        builders=("claude",),
    )
    assert decision.deferred
    assert decision.reason == "claude refused; codex exhausted"
    assert decision.retry_at == NOW + _router.DEFAULT_RETRY_S, "claude's refusal has no reset: poll"
    noted = route(
        "planner",
        {"claude": claude(10, limited=NOW + 900), "codex": codex(limited=NOW + 1800)},
        exclude=("claude",),
    )
    assert noted.deferred and noted.retry_at == NOW + 900, "a recorded refusal time is used"
    assert decision.builders == ("claude",) and len(decision.candidates) == 2


def test_a_refused_engine_moves_the_call_to_the_other_when_it_has_room():
    decision = route(meters={"claude": claude(10), "codex": codex(40)}, exclude=("claude",))
    assert decision.engine == "codex" and "claude refused" in decision.reason
    unknown = route(meters={"claude": claude(10)}, exclude=("claude",))
    assert unknown.engine == "codex", "an unknown meter is not a known-exhausted one"


def test_refusing_the_only_engine_defers_with_its_name():
    decision = route(meters={"claude": claude(10)}, available=("claude",), exclude=("claude",))
    assert decision.deferred and decision.reason == "claude refused"


def test_a_full_window_with_no_known_reset_and_an_old_reading_counts_as_unknown():
    stale = NOW - 3 * 3600
    decision = route(meters={"claude": claude(100, five_resets=None, at=stale), "codex": codex(100)})
    assert decision.engine == "claude", "one bounded attempt lets the engine recover"
    assert not decision.deferred
    # Owner rule 2: a recent full reading is a hint, not a limit. This used to assert it deferred.
    fresh = route(meters={"claude": claude(100, five_resets=None), "codex": codex(100)})
    assert fresh.engine == "claude" and not fresh.deferred


def test_a_stale_full_window_with_a_known_reset_still_ranks_as_full_but_does_not_block():
    # Owner rule 2: the full window is still a hint until its reset, however old the reading, so it
    # loses a tie; it no longer defers the call. This used to assert both full deferred.
    stale = NOW - 3 * 3600
    assert route(meters={"claude": claude(100, at=stale), "codex": codex(100)}).engine == "claude"
    assert route(meters={"claude": claude(100, at=stale), "codex": codex(50)}).engine == "codex"


def test_an_unknown_other_engine_keeps_qa_different_from_the_builder():
    decision = route("correctness", {"claude": claude(10)}, builders=("claude",), preferred="claude")
    assert decision.engine == "codex"
    assert decision.reason == "codex meter unknown; QA differs from builder claude"


def test_an_exhausted_other_engine_is_not_chosen_for_qa_diversity():
    decision = route(
        "correctness", {"claude": claude(10), "codex": codex(limited=NOW + 900)}, builders=("claude",)
    )
    assert decision.engine == "claude"


def test_one_configured_engine_says_its_own_name_when_exhausted():
    decision = route(meters={"claude": claude(limited=NOW + 900)}, available=("claude",))
    assert decision.deferred and decision.reason == "claude exhausted"


def test_reasons_do_not_repeat_an_exhaustion_note():
    limited = claude(100, limited=NOW + 900)
    pinned = route(meters={"claude": limited, "codex": codex(30)}, pin="claude")
    assert pinned.engine == "codex"
    assert pinned.reason.count("claude") == 1, pinned.reason
    both = route(meters={"claude": limited, "codex": codex(100, limited=NOW + 900)}, pin="claude")
    assert both.reason == "both engines exhausted"
    only = route(meters={"claude": limited, "codex": codex(30)})
    assert only.reason == "claude exhausted; only codex has headroom"


def test_the_record_names_the_meter_source_age_and_login():
    record = route(meters={"claude": claude(10, at=NOW - 90), "codex": codex(20)}, now=NOW).record()
    by_engine = {c["engine"]: c for c in record["candidates"]}
    assert by_engine["claude"]["source"] == "oauth-usage" and by_engine["claude"]["age_s"] == 90
    assert by_engine["claude"]["login"] == "work"
    assert by_engine["codex"]["source"] == "app-server" and by_engine["codex"]["login"] == "codex"
    blank = route(meters={}).record()
    assert all(c["source"] is None and c["age_s"] is None for c in blank["candidates"])
    assert record["model"] == "" and record["effort"] == ""


# --- owner rules: policy chooses, only a real limit moves a call ------------- #


@pytest.mark.parametrize(
    "missing",
    [None, claude(10, at=NOW - 3 * 3600), _meter.Meter("claude work", "oauth-usage", NOW)],
    ids=["no-meter", "stale", "no-windows"],
)
def test_an_unavailable_meter_leaves_the_preferred_engine_chosen(missing):
    # A setup-token deployment has no Claude usage endpoint: that is not a lack of headroom.
    decision = route(meters={"claude": missing, "codex": codex(10)}, preferred="claude")
    assert decision.engine == "claude"
    assert "has headroom" not in decision.reason
    assert "only codex" not in decision.reason


def test_an_unavailable_meter_on_the_preferred_codex_leaves_codex_chosen():
    decision = route(meters={"claude": claude(10), "codex": None}, preferred="codex")
    assert decision.engine == "codex"


def test_headroom_never_overrides_the_preferred_engine():
    assert route(meters={"claude": claude(90), "codex": codex(5)}, preferred="claude").engine == "claude"
    assert route(meters={"claude": claude(5), "codex": codex(90)}, preferred="codex").engine == "codex"
    assert route(meters={"claude": claude(99, 99), "codex": codex(0)}, preferred="claude").engine == "claude"


def test_a_limit_moves_the_preferred_engines_call_to_the_other():
    decision = route(meters={"claude": claude(limited=NOW + 900), "codex": codex(10)}, preferred="claude")
    assert decision.engine == "codex"
    assert "claude exhausted" in decision.reason


def test_both_limited_waits_for_the_earliest_reset():
    decision = route(
        meters={"claude": claude(limited=NOW + 900), "codex": codex(limited=NOW + 300)}, preferred="claude"
    )
    assert decision.deferred and decision.retry_at == NOW + 300
    unknown = route(meters={"claude": claude(limited=NOW + 900)}, exclude=("codex",), retry_s=60.0)
    assert unknown.deferred and unknown.retry_at == NOW + 60.0, "a refusal with no reset is polled"


@pytest.mark.parametrize("builder", ["claude", "codex"])
def test_the_cross_check_falls_back_to_the_builders_engine_when_the_other_is_limited(builder):
    other = "codex" if builder == "claude" else "claude"
    meters = {builder: None, other: (codex if other == "codex" else claude)(limited=NOW + 600)}
    decision = route("correctness", meters, builders=(builder,), preferred=other)
    assert decision.engine == builder and not decision.deferred
    assert f"cross-check unavailable: {other} limited" in decision.reason
    refused = route("correctness", {}, builders=(builder,), preferred=other, exclude=(other,))
    assert refused.engine == builder
    assert f"cross-check unavailable: {other} limited" in refused.reason


def test_without_limits_the_builder_side_stays_on_claude_and_the_reviewer_goes_to_codex():
    meters = {"claude": None, "codex": codex(10)}
    for role in ("author", "planner", "delegate"):
        decision = route(role, meters, preferred="claude")
        assert decision.engine == "claude", role
    review = route("correctness", meters, builders=("claude",), preferred="codex")
    assert review.engine == "codex"
    assert "QA differs from builder claude" in review.reason


def test_headroom_breaks_a_tie_only_between_measured_engines():
    assert route(meters={"claude": claude(80), "codex": codex(10)}).engine == "codex", "no preference"
    # An unmeasured engine is not out-ranked by a measured one: the profile order stands.
    assert route(meters={"claude": None, "codex": codex(10)}).engine == "claude"
    assert (
        route(meters={"claude": claude(10), "codex": None}, available=("codex", "claude")).engine == "codex"
    )


# --------------------------------------------------------------------------- #
# Every call path goes through the router
# --------------------------------------------------------------------------- #


class Rows(_timeline._Timeline):
    """Records rows only; a `_Timeline` by type so it can be passed where the agent expects one."""

    def __init__(self) -> None:  # deliberately not calling super: none of the real state is wanted
        self.rows: list[tuple[str, str]] = []  # the fake's own row log

    def row(self, icon: str, text: str) -> None:
        self.rows.append((icon, text))

    def routing(self) -> list[str]:
        return [text for icon, text in self.rows if icon == "🔀" and text.startswith("router:")]


def _fake_engines_first(monkeypatch, bin_dir):
    """Point bare `claude`/`codex` at the fakes in `bin_dir`.

    The child's PATH puts the pod's own tool directories first, so on a machine with a real `claude` or
    `codex` a bare name would reach it, and could spend quota.
    """
    real_exec = asyncio.create_subprocess_exec

    async def fake_exec(program, *args, **kwargs):
        if program in ("claude", "codex"):
            program = str(bin_dir / program)
        return await real_exec(program, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """The agent with the router's inputs set up and the child action replaced by a recorder."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    monkeypatch.setattr(
        _session_config, "_token_meter_account", ""
    )  # taken from the token above, then put back
    store = _meter.MeterStore(tmp_path / "meters")
    now = time.time()
    store.update(_meter.Meter(_session_config._claude_account(), "oauth-usage", now, 97, now + 3600))
    store.update(_meter.Meter("codex", "app-server", now, 12, now + 3600))
    calls = []

    async def raw(**kwargs):
        calls.append(kwargs)
        return "answer", None

    monkeypatch.setattr(_reviewer, "_call_reviewer_raw", raw)
    monkeypatch.setattr(_usage, "_record_usage", lambda *a, **k: None)
    monkeypatch.setattr(_usage, "_meters", store)
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "codex_token")
    monkeypatch.setattr(_session_config, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(_session_config, "_routes", [])
    for key in ("PLANNER", "DELEGATE", "CORRECTNESS", "CRAFT", "COMPLETENESS", "QA_DIFFERENT"):
        monkeypatch.delenv(f"AGENT_FACTORY_{key}_ENGINE", raising=False)
    monkeypatch.delenv("AGENT_FACTORY_QA_DIFFERENT_ENGINE", raising=False)
    # `agent.project` sets this in os.environ itself, so an earlier project test in the same worker
    # leaves its author engine behind as the builder a direct QA child cross-checks.
    monkeypatch.delenv("AGENT_FACTORY_CHOSEN_AUTHOR_ENGINE", raising=False)
    return SimpleNamespace(store=store, calls=calls, now=now)


def spy_on_router(monkeypatch, engine="codex"):
    seen = []
    real = _router.route

    def spy(role, meters, **kwargs):
        seen.append(role)
        return real(role, meters, **kwargs)

    monkeypatch.setattr(_router, "route", spy)
    return seen


async def test_a_judge_is_routed_and_the_decision_is_logged_with_its_reason(wired, monkeypatch):
    seen = spy_on_router(monkeypatch)
    rows = Rows()
    await _reviewer._call_reviewer(purpose="correctness", timeline=rows)
    assert seen == ["correctness"]
    assert [c["engine"] for c in wired.calls] == ["codex"]
    (row,) = rows.routing()
    assert (
        "correctness -> codex" in row and "claude 5h 97%" in row and "QA differs from builder claude" in row
    )
    (record,) = _session_config._routes
    assert record["role"] == "correctness" and record["engine"] == "codex" and record["reason"]


async def test_a_pinned_judge_is_logged_as_pinned(wired, monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_CRAFT_ENGINE", "claude")
    rows = Rows()
    await _reviewer._call_reviewer(purpose="craft", timeline=rows)
    assert [c["engine"] for c in wired.calls] == ["claude"]
    assert "pinned" in rows.routing()[0]
    assert _session_config._routes[0]["pinned"] is True


async def test_a_judge_with_both_engines_exhausted_defers_without_calling_either(wired):
    wired.store.latest["claude"].limited_until = wired.now + 3600
    wired.store.latest["codex"].limited_until = wired.now + 3600
    rows = Rows()
    with pytest.raises(_router.RouterDeferred) as caught:
        await _reviewer._call_reviewer(purpose="correctness", timeline=rows)
    assert wired.calls == []
    assert _usage.USAGE_LIMIT_MARK in str(caught.value), "the existing limit wait understands it"
    assert "deferred" in rows.routing()[0]
    assert _session_config._routes[0]["deferred"] is True


async def test_an_explicit_engine_is_not_routed_a_second_time(wired, monkeypatch):
    seen = spy_on_router(monkeypatch)
    await _reviewer._call_reviewer(purpose="delegate", engine="claude")
    assert seen == [], "the caller already routed it"
    assert [c["engine"] for c in wired.calls] == ["claude"]


async def test_a_delegation_is_routed_when_it_starts_and_logged(wired, monkeypatch, tmp_path):
    seen = spy_on_router(monkeypatch)
    rows = Rows()
    chosen = await _usage._delegation_engine(rows)()
    # Delegates sit on the builder side and stay on Claude unless policy pins them (owner, inbox 038
    # rule 4); this used to expect codex.
    assert chosen == "claude" and seen == ["delegate"]
    (row,) = rows.routing()
    assert "delegate -> claude" in row
    assert _session_config._routes[-1]["role"] == "delegate"


class _StopWaiting(Exception):
    """Raised by a test's injected sleep: what the job recorded before it would sleep is the result."""


async def test_a_deferred_delegation_fails_the_job_with_the_retry_time(wired, tmp_path, monkeypatch):
    # The name is from before #125, kept so the test stays traceable. #125 (D2, decision-001: an explicit
    # pin is hard) reversed the failing half: a deferred delegation used to fail the job with the retry time
    # in its error, and now waits for that time with its slot released, saying so in the answer
    # `delegate wait` reads. Every other assertion of the original is kept; the reversed one is inverted.
    from flyte_agent_factory import _bridge

    wired.store.latest["claude"].limited_until = wired.now + 3600
    wired.store.latest["codex"].limited_until = wired.now + 3600
    rows = Rows()
    root, work = tmp_path / "bridge", tmp_path / "work"
    for sub in ("requests", "answers"):
        (root / sub).mkdir(parents=True)
    work.mkdir()

    async def worker(*a, **k):
        raise AssertionError("no engine has headroom")

    supervisor = Delegations(root, work, worker, _usage._delegation_engine(rows), lambda *a: None)
    monkeypatch.setattr(supervisor, "queue_notice", lambda *a, **k: None)
    slept = []

    async def sleep(seconds):
        slept.append(seconds)
        raise _StopWaiting

    supervisor.sleep = sleep
    with pytest.raises(_StopWaiting):
        await supervisor.service({"id": "delegate-1", "brief": "do it"})
    answer = json.loads((root / "answers/delegate-1.json").read_text())
    assert answer["state"] == "waiting"
    assert answer["until"] == pytest.approx(wired.now + 3600)
    assert len(slept) == 1 and slept[0] > 0
    assert not (supervisor.jobs / "delegate-1/error.txt").exists()
    assert "deferred" in rows.routing()[0]
    assert _bridge  # the answer file is the same one `delegate wait` reads


async def test_the_delegation_service_asks_the_router_for_its_engine(tmp_path, monkeypatch):
    asked = []
    root, work = tmp_path / "bridge", tmp_path / "work"
    for sub in ("requests", "answers"):
        (root / sub).mkdir(parents=True)
    work.mkdir()

    def engine():
        asked.append(1)
        raise _router.RouterDeferred("router: delegate -> deferred (test)")

    supervisor = Delegations(root, work, cast(Any, None), engine, lambda *a: None)
    monkeypatch.setattr(supervisor, "queue_notice", lambda *a, **k: None)

    async def sleep(seconds):
        # A deferral now waits and asks the router again (#125); one question is all this test needs.
        raise _StopWaiting

    supervisor.sleep = sleep
    with pytest.raises(_StopWaiting):
        await supervisor.service({"id": "delegate-2", "brief": "x"})
    assert asked == [1]


def test_the_project_builds_its_delegations_with_the_routed_engine():
    """`project` must hand `Delegations` the router, not the static `_engine_for` it used to."""
    import inspect

    source = inspect.getsource(agent.project.func)
    assert "_delegation_engine(timeline)" in source
    assert '_engine_for("delegate")' not in source


async def test_a_direct_subagent_is_routed_and_logged(wired, monkeypatch, tmp_path):
    """`subagent(...)` called without an engine (not through `_call_reviewer`) asks the router."""
    import flyte.report

    seen = spy_on_router(monkeypatch)
    ran = []
    reports = []

    async def log_report(text, **kwargs):
        reports.append(text)

    monkeypatch.setattr(flyte.report.log, "aio", log_report)

    async def run_codex(work, base, brief, **kwargs):
        ran.append(kwargs)
        return "done"

    async def direct_meters():
        return dict(wired.store.latest)

    # Owner rules 2 and 4: neither 97% nor a full window is a limit, so the dev role would stay on its
    # preferred Claude. A recorded refusal is what moves it to Codex (this test used to rely on 97% vs
    # 12% headroom, then on a full window).
    wired.store.latest["claude"].limited_until = wired.now + 3600
    monkeypatch.setattr(_subagent_run, "_run_codex", run_codex)
    monkeypatch.setattr(_session_setup, "_install_bridge_commands", lambda: None)
    monkeypatch.setattr(_session_setup, "_exclude_harness", lambda work: None)
    monkeypatch.setattr(_subagent_run, "_read_direct_meters", direct_meters)
    monkeypatch.setattr(tempfile, "mkdtemp", lambda prefix="": str(tmp_path / "slot"))
    monkeypatch.setenv(_session_config.CODEX_TOKEN_ENV_VAR, "{}")
    (tmp_path / "slot").mkdir()
    summary, _ = await agent.subagent.func(brief="b", engine="", role="dev")
    assert seen == ["dev"], "a direct entry is routed like every other call"
    assert summary == "done"
    _, rec = _usage._split_usage(reports[-1])
    assert rec["route"]["role"] == "dev" and rec["route"]["engine"] == "codex"
    assert rec["route"]["model"] == ran[0]["model"] and rec["route"]["row"].startswith("router: dev -> codex")
    assert _session_config._routes[-1]["role"] == "dev" and _session_config._routes[-1]["engine"] == "codex"
    assert ran[0]["model"] != _policy.DEFAULTS["author"].model, "Codex setting, not the Claude default"


async def test_an_explicit_engine_on_a_direct_subagent_is_a_pin_and_is_logged_as_one(
    wired, monkeypatch, tmp_path
):
    ran = []

    async def run_codex(work, base, brief, **kwargs):
        ran.append(1)
        return "done"

    monkeypatch.setattr(_subagent_run, "_run_codex", run_codex)
    monkeypatch.setattr(_session_setup, "_install_bridge_commands", lambda: None)
    monkeypatch.setattr(_session_setup, "_exclude_harness", lambda work: None)

    async def no_meters():
        return {}

    monkeypatch.setattr(_subagent_run, "_read_direct_meters", no_meters)
    monkeypatch.setattr(tempfile, "mkdtemp", lambda prefix="": str(tmp_path / "slot"))
    monkeypatch.setenv(_session_config.CODEX_TOKEN_ENV_VAR, "{}")
    (tmp_path / "slot").mkdir()
    await agent.subagent.func(brief="b", engine="codex")
    assert ran == [1]
    assert _session_config._routes[-1]["pinned"] is True and _session_config._routes[-1]["engine"] == "codex"


async def test_codex_only_direct_child_with_unknown_meters_succeeds(wired, monkeypatch, tmp_path):
    import flyte.report

    ran = []

    async def run_codex(*args, **kwargs):
        ran.append(kwargs)
        return "done"

    async def report(*args, **kwargs):
        pass

    async def unreadable_meter(*args, **kwargs):
        raise _meter.MeterError("unavailable")

    monkeypatch.setattr(_subagent_run, "_run_codex", run_codex)
    monkeypatch.setattr(_session_setup, "_install_bridge_commands", lambda: None)
    monkeypatch.setattr(_session_setup, "_exclude_harness", lambda work: None)
    monkeypatch.setattr(_subagent_run, "_direct_meters", None)
    for var in ("CLAUDE_CODE_OAUTH_TOKEN", "AGENT_FACTORY_CLAUDE_TOKEN_SECRET"):
        monkeypatch.delenv(var, raising=False)  # the pod holds a Codex token only
    monkeypatch.setattr(_meter, "read_codex", unreadable_meter)
    monkeypatch.setattr(flyte.report.log, "aio", report)
    monkeypatch.setattr(tempfile, "mkdtemp", lambda prefix="": str(tmp_path / "slot"))
    monkeypatch.setenv(_session_config.CODEX_TOKEN_ENV_VAR, "{}")
    (tmp_path / "slot").mkdir()
    summary, _ = await agent.subagent.func(brief="b", role="dev")
    assert summary == "done" and len(ran) == 1
    record = _session_config._routes[-1]
    assert record["engine"] == "codex"
    assert {candidate["engine"]: candidate["state"] for candidate in record["candidates"]} == {
        "claude": "unavailable",
        "codex": "unknown",
    }


def test_the_snapshot_carries_the_routing_records(wired):
    wired.store.latest["claude"].five_hour_pct = 10
    _usage._record_route(_router.route("planner", {}, available=("claude",), now=wired.now), None)
    timeline = _timeline._Timeline()
    assert timeline.snapshot()["routing"] == _session_config._routes
    assert timeline.snapshot()["routing"][0]["role"] == "planner"


def test_the_records_are_bounded():
    saved = list(_session_config._routes)
    try:
        _session_config._routes.clear()
        for _ in range(_usage._ROUTES_KEEP + 25):
            _usage._record_route(_router.route("planner", {}, available=("claude",), now=NOW), None)
        assert len(_session_config._routes) == _usage._ROUTES_KEEP
    finally:
        _session_config._routes[:] = saved


async def test_the_decision_a_direct_subagent_returns_reaches_the_timeline_and_snapshot(wired, monkeypatch):
    decision = _router.route("dev", dict(wired.store.latest), available=("claude", "codex"), now=wired.now)
    blob = {**decision.record(), "row": decision.row()}

    async def raw(**kwargs):
        return _usage._with_route("answer", blob), None

    monkeypatch.setattr(_reviewer, "_call_reviewer_raw", raw)
    rows = Rows()
    text, _ = await _reviewer._call_reviewer(purpose="planner", timeline=rows, engine="claude")
    assert text == "answer"
    assert any(r.startswith("router: dev -> codex") for r in rows.routing())
    assert [r["role"] for r in _session_config._routes if r["role"] == "dev"] == ["dev"]


async def test_concurrent_direct_subagents_share_one_meter_read(monkeypatch):
    reads = []

    async def read_claude(token, account, setup_token=False):
        reads.append((token, account, setup_token))
        await asyncio.sleep(0.01)
        return _meter.Meter(account, "oauth-usage", time.time(), 10, time.time() + 3600)

    async def read_codex(*args, **kwargs):
        raise AssertionError("a Codex access token has no usage endpoint to probe")

    monkeypatch.setattr(_subagent_run, "_direct_meters", None)
    monkeypatch.setattr(_meter, "SETUP_TOKEN_USAGE_SUPPORTED", True)
    monkeypatch.setattr(_meter, "read_claude", read_claude)
    monkeypatch.setattr(_meter, "read_codex", read_codex)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    monkeypatch.setenv(_session_config.CODEX_TOKEN_ENV_VAR, "tok")
    results = await asyncio.gather(*(_subagent_run._read_direct_meters() for _ in range(5)))
    assert len(reads) == 1, "one probe of the work token, however many sub-agents ask"
    assert reads[0][0] == "t" and reads[0][2] is True
    assert all(set(r) == {"claude"} for r in results), "Codex is never probed, i.e. unknown"
    # Past the guard interval it reads again.
    assert _subagent_run._direct_meters is not None
    start = _subagent_run._direct_meters[0]
    monkeypatch.setattr(
        _subagent_run, "_direct_meters", (start - _meter.MIN_INTERVAL_S - 1, _subagent_run._direct_meters[1])
    )
    await _subagent_run._read_direct_meters()
    assert len(reads) == 2


async def test_the_recorded_decision_names_the_resolved_model_and_effort(wired):
    await _reviewer._call_reviewer(purpose="correctness", timeline=Rows())
    record = _session_config._routes[-1]
    assert record["engine"] == "codex"
    assert record["model"] == wired.calls[0]["model"] and record["effort"] == wired.calls[0]["effort"]
    assert record["effort"], "the Codex effort the policy resolved"


@pytest.mark.parametrize("pin", ["claude", "codex"])
def test_a_hard_pin_waits_for_its_engine(pin):
    meters = {"claude": claude(10), "codex": codex(10)}
    meters[pin].limited_until = LATER
    decision = route(meters=meters, pin=pin, hard_pin=True)
    assert decision.deferred and decision.retry_at == LATER
    assert decision.pinned and pin in decision.reason


@pytest.mark.parametrize("pin", ["claude", "codex"])
async def test_a_direct_pin_falls_over_with_the_target_engines_defaults(wired, monkeypatch, pin):
    async def meters():
        return dict(wired.store.latest)

    monkeypatch.setattr(_subagent_run, "_read_direct_meters", meters)
    monkeypatch.setenv(_session_config.CODEX_TOKEN_ENV_VAR, "{}")
    wired.store.latest[pin].limited_until = wired.now + 3600
    target = "codex" if pin == "claude" else "claude"
    chosen, model, effort, record = await _subagent_run._route_direct_subagent(
        "dev", pin, "pinned-model", "max"
    )
    expected = _policy.resolve_codex("dev") if target == "codex" else _policy.DEFAULTS["author"]
    assert (chosen, model, effort) == (target, *expected)
    assert (record["model"], record["effort"]) == expected


async def test_delegation_route_records_its_actual_setting(wired):
    chosen = await _usage._delegation_engine(Rows())()
    setting = _policy.resolve_codex("delegate") if chosen == "codex" else _policy.DEFAULTS["author"]
    assert (_session_config._routes[-1]["model"], _session_config._routes[-1]["effort"]) == setting


async def test_default_claude_judge_records_resolved_defaults(wired, monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_CORRECTNESS_ENGINE", "claude")
    await _reviewer._call_reviewer(purpose="correctness")
    assert _session_config._routes[-1]["model"] == _policy.DEFAULTS["author"].model
    assert _session_config._routes[-1]["effort"] == _policy.DEFAULTS["author"].effort


async def test_author_child_report_is_ingested_once_without_exposing_the_trailer(
    wired, monkeypatch, tmp_path
):
    import os

    import flyte.report
    from flyte import remote

    reports = []

    async def log_report(text, **kwargs):
        reports.append(text)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "codex"
    executable.write_text(
        "#!/bin/sh\n"
        'out=""; while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done\n'
        'echo answer > "$out"\n'
        'echo \'{"type":"turn.completed","usage":{"output_tokens":7}}\'\n'
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    _fake_engines_first(monkeypatch, bin_dir)
    roles = tmp_path / "roles"
    roles.mkdir()
    (roles / "dev.md").write_text("Implement the brief.")
    monkeypatch.setattr(_session_setup, "AGENT_ROLES_DIR", str(roles))

    async def meters():
        return dict(wired.store.latest)

    # Owner rules 2 and 4: the dev role prefers Claude and neither 97% nor a full window is a limit; a
    # recorded refusal sends it to the fake Codex (this test used to rely on 97% vs 12% headroom, then
    # on a full window).
    wired.store.latest["claude"].limited_until = wired.now + 3600
    monkeypatch.setattr(flyte.report.log, "aio", log_report)
    monkeypatch.setattr(_subagent_run, "_read_direct_meters", meters)
    monkeypatch.setattr(_session_setup, "_install_bridge_commands", lambda: None)
    monkeypatch.setattr(_session_setup, "_exclude_harness", lambda work: None)
    monkeypatch.setattr(tempfile, "mkdtemp", lambda prefix="": str(tmp_path / "slot"))
    monkeypatch.setenv(_session_config.CODEX_TOKEN_ENV_VAR, "{}")
    (tmp_path / "slot").mkdir()
    text, _ = await agent.subagent.func(brief="b", role="dev")
    assert text == "answer", "the author must never receive internal telemetry"
    _session_config._routes.clear()  # The child and supervisor have separate Python processes in production.
    action = SimpleNamespace(
        name="child", task_name="env.subagent", phase="SUCCEEDED", get_report=lambda: "".join(reports)
    )
    monkeypatch.setattr(remote.Action, "listall", lambda **kwargs: [action])
    monkeypatch.setattr(flyte, "ctx", lambda: SimpleNamespace(action=SimpleNamespace(run_name="run")))
    timeline = _timeline._Timeline()
    await _supervise_support._refresh_child_usage(timeline)
    await _supervise_support._refresh_child_usage(timeline)
    assert [row["role"] for row in timeline.snapshot()["routing"]] == ["dev"]
    assert any("router: dev -> codex" in text for _, _, text in timeline._rows)


async def test_benchmark_role_pin_defers_on_refusal(wired, monkeypatch):
    monkeypatch.setattr(_usage, "_hard_pins", True)
    monkeypatch.setenv("AGENT_FACTORY_PLANNER_ENGINE", "claude")

    async def refuse(**kwargs):
        assert kwargs["engine"] == "claude"
        raise RuntimeError(_usage.USAGE_LIMIT_MARK)

    monkeypatch.setattr(_reviewer, "_call_reviewer_raw", refuse)
    with pytest.raises(_router.RouterDeferred):
        await _reviewer._call_reviewer(purpose="planner")
    assert _session_config._routes[-1]["pinned"] and _session_config._routes[-1]["deferred"]
    assert wired.store.latest["claude"].limited_until is not None


def test_child_usage_replay_is_idempotent_after_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(_session_config, "_usage", {})
    monkeypatch.setattr(_session_config, "_usage_loaded", True)
    rec = {"call_id": "run/child", "output_tokens": 7}
    _usage._record_usage("dev", 0, rec)
    _usage._record_usage("dev", 0, rec)
    assert _session_config._usage["dev@0"]["calls"] == 1
    monkeypatch.setattr(_session_config, "_usage", {})
    monkeypatch.setattr(_session_config, "_usage_loaded", False)
    _usage._record_usage("dev", 0, rec)
    assert _session_config._usage["dev@0"]["calls"] == 1
    assert _session_config._usage["dev@0"]["output_tokens"] == 7


def test_a_hard_pin_never_moves_to_a_configured_alternative():
    decision = route(available=("claude",), pin="codex", hard_pin=True)
    assert decision.deferred and decision.pinned
    assert "not configured" in decision.reason


async def test_a_deferred_direct_child_reports_its_route(wired, monkeypatch):
    import flyte.report
    from flyte import remote

    reports = []

    async def log_report(text, **kwargs):
        reports.append(text)

    async def meters():
        return dict(wired.store.latest)

    wired.store.latest["claude"].limited_until = wired.now + 3600
    wired.store.latest["codex"].limited_until = wired.now + 3600
    monkeypatch.setattr(_subagent_run, "_read_direct_meters", meters)
    monkeypatch.setattr(flyte.report.log, "aio", log_report)
    monkeypatch.setenv(_session_config.CODEX_TOKEN_ENV_VAR, "{}")
    with pytest.raises(_router.RouterDeferred):
        await agent.subagent.func(brief="b")
    _session_config._routes.clear()
    action = SimpleNamespace(
        name="deferred", task_name="env.subagent", phase="FAILED", get_report=lambda: "".join(reports)
    )
    monkeypatch.setattr(remote.Action, "listall", lambda **kw: [action])
    monkeypatch.setattr(flyte, "ctx", lambda: SimpleNamespace(action=SimpleNamespace(run_name="run")))
    timeline = _timeline._Timeline()
    await _supervise_support._refresh_child_usage(timeline)
    assert timeline.snapshot()["routing"][0]["deferred"]
    assert any("router: subagent -> deferred" in text for _, _, text in timeline._rows)


@pytest.fixture
def direct_cli(wired, monkeypatch, tmp_path):
    import os
    import sys

    import flyte.report

    reports = []
    calls = tmp_path / "calls.jsonl"
    refusals = tmp_path / "refusals.json"
    refusals.write_text("[]")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = (
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        "engine = pathlib.Path(sys.argv[0]).name\n"
        "if sys.argv[1:2] == ['login']:  # `codex login --with-access-token`: not a turn, not logged\n"
        "    sys.exit(0)\n"
        f"with open({str(calls)!r}, 'a') as log:\n"
        "    log.write(json.dumps({'engine': engine, 'argv': sys.argv[1:]}) + '\\n')\n"
        f"if engine in json.loads(pathlib.Path({str(refusals)!r}).read_text()):\n"
        "    if engine == 'claude':\n"
        "        print(json.dumps({'is_error': True, 'api_error_status': 429, 'result': 'usage limit'}))\n"
        "    else:\n"
        "        print(json.dumps({'type': 'error', 'message': 'usage limit'}))\n"
        "    sys.exit(1)\n"
        "if engine == 'claude':\n"
        "    print(json.dumps({'result': 'answer'}))\n"
        "else:\n"
        "    pathlib.Path(sys.argv[sys.argv.index('-o') + 1]).write_text('answer')\n"
        "    print(json.dumps({'type': 'turn.completed', 'usage': {'output_tokens': 7}}))\n"
    )
    for name in ("claude", "codex"):
        executable = bin_dir / name
        executable.write_text(script)
        executable.chmod(0o755)
    roles = tmp_path / "roles"
    roles.mkdir()
    for role in ("dev", "qa"):
        (roles / f"{role}.md").write_text("Carry out the brief.")

    async def meters():
        return dict(wired.store.latest)

    async def report(text, **kwargs):
        reports.append(text)

    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    _fake_engines_first(monkeypatch, bin_dir)
    monkeypatch.setenv(_session_config.CODEX_TOKEN_ENV_VAR, "{}")
    monkeypatch.setenv("AGENT_FACTORY_DEV_CODEX_MODEL", "codex-test-model")
    monkeypatch.setattr(_session_setup, "AGENT_ROLES_DIR", str(roles))
    monkeypatch.setattr(_subagent_run, "_read_direct_meters", meters)
    monkeypatch.setattr(_session_setup, "_install_bridge_commands", lambda: None)
    monkeypatch.setattr(_session_setup, "_exclude_harness", lambda work: None)
    monkeypatch.setattr(_session_setup, "_seed_team", lambda work: {"dev", "qa"})
    monkeypatch.setattr(flyte.report.log, "aio", report)
    monkeypatch.setattr(tempfile, "mkdtemp", lambda prefix="": str(tmp_path / "slot"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return SimpleNamespace(calls=calls, refusals=refusals, reports=reports)


@pytest.mark.parametrize("first", ["claude", "codex"])
@pytest.mark.parametrize("outcome", ["fallback", "hard-pin", "exhausted", "second-refusal"])
async def test_direct_child_routes_after_cli_refusal(wired, direct_cli, monkeypatch, first, outcome):
    other = "codex" if first == "claude" else "claude"
    direct_cli.refusals.write_text(json.dumps([first, other] if outcome == "second-refusal" else [first]))
    if outcome == "exhausted":
        wired.store.latest[other].limited_until = wired.now + 3600
    if outcome == "fallback":
        summary, _ = await agent.subagent.func(
            brief="b", role="dev", engine=first, model="custom-model", effort="high"
        )
        assert summary == "answer"
    else:
        with pytest.raises(_router.RouterDeferred, match=_usage.USAGE_LIMIT_MARK):
            await agent.subagent.func(brief="b", role="dev", engine=first, hard_pin=outcome == "hard-pin")
    calls = [json.loads(line) for line in direct_cli.calls.read_text().splitlines()]
    expected = [first, other] if outcome in ("fallback", "second-refusal") else [first]
    assert [call["engine"] for call in calls] == expected
    assert len(_session_config._routes) == (3 if outcome == "second-refusal" else 2)
    assert _session_config._routes[1]["candidates"][0 if first == "claude" else 1]["usable_at"] > wired.now
    if outcome == "fallback":
        setting: _policy.Setting = (
            _policy.resolve_codex("dev") if other == "codex" else _policy.DEFAULTS["author"]
        )
        model = setting.model  # before `==` narrows `setting` to a plain tuple
        assert (_session_config._routes[-1]["model"], _session_config._routes[-1]["effort"]) == setting
        assert model in calls[-1]["argv"]
    else:
        assert _session_config._routes[-1]["deferred"]
    _, record = _usage._split_usage(direct_cli.reports[-1])
    assert len(record["routes"]) == len(_session_config._routes)
    assert set(record["refusals"]) == set(expected if outcome == "second-refusal" else [first])

    from flyte import remote

    decisions = list(_session_config._routes)
    _session_config._routes.clear()
    action = SimpleNamespace(
        name="child",
        task_name="env.subagent",
        phase="SUCCEEDED" if outcome == "fallback" else "FAILED",
        get_report=lambda: "".join(direct_cli.reports),
    )
    monkeypatch.setattr(remote.Action, "listall", lambda **kwargs: [action])
    monkeypatch.setattr(flyte, "ctx", lambda: SimpleNamespace(action=SimpleNamespace(run_name="run")))
    timeline = _timeline._Timeline()
    await _supervise_support._refresh_child_usage(timeline)
    await _supervise_support._refresh_child_usage(timeline)
    assert timeline.snapshot()["routing"] == decisions
    for refused, until in record["refusals"].items():
        assert wired.store.latest[refused].limited_until == until


@pytest.mark.parametrize("builder", ["claude", "codex"])
async def test_direct_qa_prefers_the_other_builder_engine(wired, direct_cli, monkeypatch, builder):
    wired.store.latest["claude"].five_hour_pct = 10
    wired.store.latest["codex"].five_hour_pct = 20
    monkeypatch.setenv("AGENT_FACTORY_CHOSEN_AUTHOR_ENGINE", builder)
    summary, _ = await agent.subagent.func(brief="review", role="qa")
    assert summary == "answer"
    calls = [json.loads(line) for line in direct_cli.calls.read_text().splitlines()]
    assert [call["engine"] for call in calls] == ["codex" if builder == "claude" else "claude"]
    assert _session_config._routes[-1]["builders"] == [builder]


@pytest.mark.parametrize("first", ["claude", "codex"])
async def test_unpinned_direct_refusal_uses_target_defaults(wired, direct_cli, first):
    other = "codex" if first == "claude" else "claude"
    wired.store.latest[first].five_hour_pct = 10
    wired.store.latest[other].five_hour_pct = 20
    direct_cli.refusals.write_text(json.dumps([first]))
    # Owner rule 4: headroom no longer picks the first engine, policy does. The dev role prefers Claude
    # and a QA role on Claude-built work prefers Codex; this used to be role "dev" for both, with
    # Codex reached first on its 10% vs Claude's 20%.
    role = "dev" if first == "claude" else "qa"
    summary, _ = await agent.subagent.func(brief="b", role=role)
    assert summary == "answer"
    setting: _policy.Setting = _policy.resolve_codex(role) if other == "codex" else _policy.DEFAULTS["author"]
    calls = [json.loads(line) for line in direct_cli.calls.read_text().splitlines()]
    assert [call["engine"] for call in calls] == [first, other]
    model = setting.model  # before `==` narrows `setting` to a plain tuple
    assert (_session_config._routes[-1]["model"], _session_config._routes[-1]["effort"]) == setting
    assert model in calls[-1]["argv"]


# --------------------------------------------------------------------------- #
# Owner rules through the agent: an unknown meter is not a limit, a refusal is
# --------------------------------------------------------------------------- #


@pytest.fixture
def unmetered_claude(wired):
    """A setup-token deployment: no Claude usage meter at all, Codex measured at 10%."""
    del wired.store.latest["claude"]
    del wired.store.readings[_session_config._claude_account()]
    wired.store.latest["codex"].five_hour_pct = 10
    return wired


async def test_an_unknown_claude_meter_keeps_claude_roles_on_claude_and_reviewers_on_codex(unmetered_claude):
    rows = Rows()
    await _reviewer._call_reviewer(purpose="planner", timeline=rows)
    await _reviewer._call_reviewer(purpose="correctness", timeline=rows)
    assert [c["engine"] for c in unmetered_claude.calls] == ["claude", "codex"]
    planner, correctness = rows.routing()
    assert "planner -> claude" in planner and "has headroom" not in planner
    assert "correctness -> codex" in correctness and "QA differs from builder claude" in correctness


async def test_a_limit_error_fails_over_and_marks_the_engine_limited_until_its_reset(
    unmetered_claude, monkeypatch
):
    reset = unmetered_claude.now + 5400
    monkeypatch.setattr(_usage, "_reset_at", lambda text, now=None: reset)
    calls = []

    async def raw(**kwargs):
        calls.append(kwargs["engine"])
        if kwargs["engine"] == "claude":
            raise RuntimeError(f"{_usage.USAGE_LIMIT_MARK} claude usage limit")
        return "answer", None

    monkeypatch.setattr(_reviewer, "_call_reviewer_raw", raw)
    text, _ = await _reviewer._call_reviewer(purpose="planner", timeline=Rows())
    assert text == "answer" and calls == ["claude", "codex"]
    assert unmetered_claude.store.latest["claude"].limited_until == reset
    retry = _session_config._routes[-1]
    assert retry["engine"] == "codex" and "claude refused" in retry["reason"]
    # The next call knows Claude is limited and does not ask it again.
    await _reviewer._call_reviewer(purpose="planner", timeline=Rows())
    assert calls == ["claude", "codex", "codex"]
    assert "claude exhausted" in _session_config._routes[-1]["reason"]


async def test_both_engines_refusing_waits_for_the_earliest_reset(unmetered_claude, monkeypatch):
    resets = {"claude": unmetered_claude.now + 5400, "codex": unmetered_claude.now + 1200}
    monkeypatch.setattr(
        _usage, "_reset_at", lambda text, now=None: resets["codex" if "codex" in text else "claude"]
    )
    calls = []

    async def raw(**kwargs):
        calls.append(kwargs["engine"])
        raise RuntimeError(f"{_usage.USAGE_LIMIT_MARK} {kwargs['engine']} usage limit")

    monkeypatch.setattr(_reviewer, "_call_reviewer_raw", raw)
    with pytest.raises(RuntimeError, match=_usage.USAGE_LIMIT_MARK):
        await _reviewer._call_reviewer(purpose="planner", timeline=Rows())
    assert calls == ["claude", "codex"]
    with pytest.raises(_router.RouterDeferred):
        await _reviewer._call_reviewer(purpose="planner", timeline=Rows())
    assert calls == ["claude", "codex"], "a call to an engine known to be limited is not made"
    assert (
        _session_config._routes[-1]["deferred"] and _session_config._routes[-1]["retry_at"] == resets["codex"]
    )


async def test_a_reviewer_runs_on_the_builders_engine_when_the_other_is_limited(unmetered_claude):
    unmetered_claude.store.note_limit("codex", unmetered_claude.now + 600)
    rows = Rows()
    await _reviewer._call_reviewer(purpose="correctness", timeline=rows)
    assert [c["engine"] for c in unmetered_claude.calls] == ["claude"]
    (row,) = rows.routing()
    assert "correctness -> claude" in row and "cross-check unavailable: codex limited" in row


# --------------------------------------------------------------------------- #
# QA findings on the owner rules: a full meter is a hint, a failing cross-check is not a stall
# --------------------------------------------------------------------------- #


def test_a_full_meter_with_a_future_reset_is_a_hint_not_a_limit():
    # Owner rule 2: only a recorded refusal (or `exclude`) puts an engine out; a 100% reading is a hint.
    both_full = {"claude": claude(100), "codex": codex(100, 100)}
    decision = route(meters=both_full, preferred="claude")
    assert decision.engine == "claude" and not decision.deferred
    assert route(meters=both_full, preferred="codex").engine == "codex"
    assert not _router.exhausted(claude(100), NOW)
    assert not _router.exhausted(claude(5, 100), NOW)
    assert _router.exhausted(claude(100, limited=NOW + 600), NOW), "a refusal still blocks"
    # As a hint it only breaks a tie: with no preference the engine with room left wins.
    assert route(meters={"claude": claude(100), "codex": codex(30)}).engine == "codex"


async def test_both_engines_at_a_full_meter_still_route_every_role(wired):
    wired.store.latest["claude"].five_hour_pct = 100
    wired.store.latest["codex"].five_hour_pct = 100
    rows = Rows()
    assert not _usage._route("planner", rows).deferred
    await _reviewer._call_reviewer(purpose="planner", timeline=rows)
    await _reviewer._call_reviewer(purpose="correctness", timeline=rows)
    assert [c["engine"] for c in wired.calls] == ["claude", "codex"]


async def test_a_failing_cross_check_engine_reviews_on_the_builders_engine(wired, monkeypatch):
    # Owner rule 3: the cross-check is preferred, not required. Codex failing for a reason other than a
    # limit must not leave the judge unreviewed after retrying Codex alone.
    wired.store.latest["claude"].five_hour_pct = 10
    calls = []

    async def raw(**kwargs):
        calls.append(kwargs["engine"])
        if kwargs["engine"] == "codex":
            raise RuntimeError("codex service unavailable")
        return "VERDICT: PASS", None

    monkeypatch.setattr(_reviewer, "_call_reviewer_raw", raw)
    monkeypatch.setattr(_usage, "_author_engine", "claude")
    outcomes: dict[str, _review._JudgeOutcome] = {}
    timeline = _timeline._Timeline()
    _, passed, _ = await _review._one_judge(
        "correctness", "i", cast(Any, None), "b", "", 1, timeline, outcomes=outcomes
    )
    assert calls == ["codex", "claude"]
    assert passed and outcomes["correctness"].outcome == _review.PASS
    rows = [text for _, _, text in timeline._rows]
    assert any("cross-check unavailable: codex failing; reviewed on claude" in row for row in rows), rows


async def test_a_failing_builder_engine_is_not_retried_as_a_cross_check(wired, monkeypatch):
    # Only a cross-check falls back: a call already on the builder's engine raises as before.
    async def raw(**kwargs):
        raise RuntimeError("claude service unavailable")

    monkeypatch.setattr(_reviewer, "_call_reviewer_raw", raw)
    with pytest.raises(RuntimeError, match="claude service unavailable"):
        await _reviewer._call_reviewer(purpose="planner", timeline=Rows())


# --------------------------------------------------------------------------- #
# Pausing (#125): what a deferral means, for the timeline, status and human-needed text
# --------------------------------------------------------------------------- #


def test_deferred_carries_retry_at():
    reset = NOW + 4 * 86400
    decision = route(meters={"claude": claude(limited=reset), "codex": codex(limited=NOW + 86400)})
    assert decision.deferred
    exc = _usage._deferred(decision)
    assert exc.retry_at == NOW + 86400 and _router.RouterDeferred("x").retry_at is None


def test_paused_names_engine_until_and_reasons():
    reset = NOW + 4 * 86400
    decision = route(
        "author",
        meters={"claude": claude(limited=reset), "codex": codex(limited=NOW + 6 * 86400)},
        preferred="claude",
        pin="claude",
        hard_pin=True,
    )
    paused = _router.paused(decision, now=NOW)
    assert (paused.engine, paused.until, paused.needs_human) == ("claude", reset, "")
    assert paused.others == {"codex": "pinned to claude (ENGINE_FALLBACK unset)"}
    assert paused.as_dict() == {
        "engine": "claude",
        "until": reset,
        "others": paused.others,
        "needs_human": "",
    }


def test_paused_unpinned_waits_for_the_earliest_reset_and_says_why_the_other_is_out():
    decision = route(meters={"claude": claude(limited=NOW + 7200), "codex": codex(limited=NOW + 86400)})
    paused = _router.paused(decision, now=NOW)
    assert (paused.engine, paused.until) == ("claude", NOW + 7200)
    assert paused.others == {"codex": "limited until 2027-01-16 08:00 UTC"} and not paused.needs_human


def test_paused_with_no_known_reset_waits_without_a_time():
    decision = route(exclude=("claude", "codex"))
    paused = _router.paused(decision, now=NOW)
    assert paused.engine == "claude" and paused.until is None and paused.needs_human == ""


def test_paused_needs_human_only_when_nothing_can_run():
    # Not configured and a rejected login need someone to act ...
    dead = _meter.Meter(
        "codex", "limit-text", NOW, limited_until=NOW + 6 * 3600, error="codex login unusable"
    )
    decision = route(
        "author", meters={"codex": dead}, available=("codex",), pin="codex", preferred="codex", hard_pin=True
    )
    paused = _router.paused(decision, now=NOW)
    assert paused.engine == "" and paused.until is None
    assert "codex: login rejected (codex login unusable) -- replace the [secrets] codex_token secret" in (
        paused.needs_human
    )
    assert "claude: no credential" in paused.needs_human

    unconfigured = route("author", available=("claude",), pin="codex", preferred="codex", hard_pin=True)
    assert unconfigured.reason == "pin codex not configured"
    text = _router.paused(unconfigured, now=NOW).needs_human
    assert (
        'codex: no credential -- add the [secrets] codex_token secret, or set ENGINE_FALLBACK="allow"' in text
    )

    nothing = _router.paused(route(available=()), now=NOW)
    assert "claude: no credential" in nothing.needs_human and "codex: no credential" in nothing.needs_human

    # ... an engine at its limit does not, whether or not its reset is known.
    for meters in (
        {"claude": claude(limited=NOW + 3600), "codex": codex(limited=NOW + 7200)},
        {"claude": claude(limited=NOW + 3600)},
    ):
        assert _router.paused(route(meters=meters, exclude=("codex",)), now=NOW).needs_human == ""
    # A Claude login that works leaves a configured, limited engine waitable even if Codex is dead.
    waitable = route(meters={"claude": claude(limited=NOW + 3600), "codex": dead})
    assert _router.paused(waitable, now=NOW).engine == "claude"


def _deferred(*candidates, pin=""):
    """A deferral built by hand, for the corners `route` does not produce."""
    return _router.Decision("author", None, "deferred", NOW, candidates=candidates, pin=pin)


def test_paused_describes_an_engine_that_is_neither_awaited_nor_out_of_the_way():
    limited = _router.Candidate("claude", "exhausted", usable_at=NOW + 3600)
    fine = _router.Candidate("codex", "usable", headroom=0.5, five_hour_pct=40.0, weekly_pct=10.0)
    paused = _router.paused(_deferred(limited, fine), now=NOW)
    assert paused.engine == "claude" and paused.until == NOW + 3600
    assert paused.others == {"codex": "codex 5h 40% weekly 10%"}


def test_paused_says_a_pin_to_an_unknown_engine_is_not_configured():
    claude_ok = _router.Candidate("claude", "usable", headroom=1.0)
    paused = _router.paused(_deferred(claude_ok, pin="gemini"), now=NOW)
    assert paused.engine == ""
    assert paused.others == {"claude": "pinned to gemini (ENGINE_FALLBACK unset)", "gemini": "not configured"}
    assert paused.needs_human == 'claude: not used, pinned to gemini -- set ENGINE_FALLBACK="allow" to use it'


def test_unblock_describes_a_usable_engine_the_pin_does_not_forbid():
    usable = _router.Candidate("codex", "usable", headroom=0.5, five_hour_pct=40.0)
    assert _router._unblock(usable, "codex", {"codex": usable}, "lift the pin") == "codex: codex 5h 40%"


def test_a_candidate_carries_its_meter_error():
    meter = _meter.Meter("codex", "refresh-failed", NOW, error="http 401")
    decision = route(meters={"codex": meter}, available=("claude", "codex"))
    assert {c.engine: c.error for c in decision.candidates}["codex"] == "http 401"


@pytest.mark.parametrize("preferred", ["", "claude", "codex"])
@pytest.mark.parametrize("pin", ["", "codex"])
def test_exhausted_engine_never_chosen_across_attempts(preferred, pin):
    """#125 criterion 13: with Codex out for days, every attempt, preferred or soft-pinned, takes Claude."""
    meters = {"claude": claude(20), "codex": codex(limited=NOW + 5 * 86400)}
    for attempt in range(5):
        decision = route("author", meters=meters, now=NOW + attempt * 1800, preferred=preferred, pin=pin)
        assert decision.engine == "claude", (attempt, decision.row())
