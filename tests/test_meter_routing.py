"""Capacity-aware routing: engines and launch slots follow the meters, and only when they say so."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import cast

import pytest
from flyte import remote

from flyte_agent_factory import _factory as factory
from flyte_agent_factory import (
    _factory_base,
    _factory_meters,
    _meter,
    _policy,
    _reviewer,
    _router,
    _session_config,
    _timeline,
    _usage,
)

NOW = time.time()


@pytest.fixture(autouse=True)
def _now_is_when_the_test_runs():
    """Readings here are "observed at NOW". NOW taken at collection is many minutes old by the time a test
    runs in the full suite, which makes those readings stale under the two-refresh-interval rule."""
    global NOW
    NOW = time.time()


def _claude(five, resets=NOW + 6 * 3600):
    """A reading of the work token, whose account the agent names from the mounted token."""
    return _meter.Meter(_session_config._claude_account(), "oauth-usage", NOW, five, resets)


def _codex(five, resets=NOW + 6 * 3600):
    return _meter.Meter("codex", "app-server", NOW, five, resets)


class Rows:
    def __init__(self):
        self.rows = []

    def row(self, icon, text):
        self.rows.append((icon, text))

    @property
    def timeline(self) -> _timeline._Timeline:
        """`self` where a `_Timeline` is expected; it fakes only `row`."""
        return cast(_timeline._Timeline, self)


@pytest.fixture
def engines(tmp_path, monkeypatch):
    """`_call_reviewer` with the sub-agent task replaced by a recorder of the engines it is given."""
    store = _meter.MeterStore(tmp_path / "meters")
    calls = []
    refuse: set[str] = set()

    async def raw(**kwargs):
        calls.append(kwargs)
        if kwargs["engine"] in refuse:
            raise RuntimeError(f"{_usage.USAGE_LIMIT_MARK} {kwargs['engine']}: limit, resets 1:50am (UTC)")
        return "answer", None

    monkeypatch.setattr(_reviewer, "_call_reviewer_raw", raw)
    monkeypatch.setattr(_usage, "_record_usage", lambda *a, **k: None)
    monkeypatch.setattr(_usage, "_meters", store)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "{}")
    monkeypatch.setattr(
        _session_config, "_token_meter_account", ""
    )  # taken from the token above, then put back
    monkeypatch.setattr(_session_config, "_routes", [])
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "codex_token")
    monkeypatch.delenv("AGENT_FACTORY_PLANNER_ENGINE", raising=False)
    monkeypatch.delenv("AGENT_FACTORY_DELEGATE_ENGINE", raising=False)
    return SimpleNamespace(store=store, calls=calls, refuse=refuse, used=lambda: [c["engine"] for c in calls])


async def test_an_exhausted_claude_sends_the_call_to_codex_up_front(engines):
    # Owner rule 2: exhausted is a recorded refusal; a full meter alone no longer sends the call away.
    engines.store.update(_claude(100))
    engines.store.note_limit(_session_config._claude_account(), NOW + 3600)
    rows = Rows()
    await _reviewer._call_reviewer(purpose="planner", model="opus", effort="high", timeline=rows.timeline)
    assert engines.used() == ["codex"], "no failing call on Claude first"
    assert engines.calls[0]["model"] != "opus", "the Codex setting, not the Claude one"
    icons = [icon for icon, _ in rows.rows]
    assert "🔀" in icons
    assert "planner -> codex" in rows.rows[icons.index("🔀")][1]


async def test_an_exhausted_codex_sends_a_codex_role_to_claude_with_its_claude_setting(engines, monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_PLANNER_ENGINE", "codex")
    engines.store.update(_codex(10))
    engines.store.note_limit("codex", NOW + 3600)
    rows = Rows()
    await _reviewer._call_reviewer(purpose="planner", model="opus", effort="high", timeline=rows.timeline)
    assert engines.used() == ["claude"]
    assert (engines.calls[0]["model"], engines.calls[0]["effort"]) == ("opus", "high")
    assert any(icon == "🔀" for icon, _ in rows.rows)


async def test_a_delegation_pinned_to_codex_moves_when_codex_is_limited(engines):
    # Owner rule 2: a full Codex meter is a hint; its recorded refusal is what moves the delegation.
    # This was `..._moves_when_the_codex_meter_is_full`, with the meter alone.
    engines.store.update(_codex(100))
    engines.store.note_limit("codex", NOW + 3600)
    await _reviewer._call_reviewer(purpose="delegate", brief="b")
    assert engines.used() == ["claude"]


@pytest.mark.parametrize("claude_pct", [0, 50, 94.9])
async def test_below_the_threshold_the_configured_engine_is_kept(engines, claude_pct):
    engines.store.update(_claude(claude_pct))
    rows = Rows()
    await _reviewer._call_reviewer(purpose="planner", timeline=rows.timeline)
    assert engines.used() == ["claude"]
    # Every decision is logged now; what must not appear is a switch away from the configured engine.
    assert all(text.startswith("router:") and "-> claude" in text for icon, text in rows.rows if icon == "🔀")


async def test_unknown_meters_change_nothing(engines, monkeypatch):
    await _reviewer._call_reviewer(purpose="planner")
    assert engines.used() == ["claude"]
    monkeypatch.setattr(_usage, "_meters", None)  # outside a project there is no record at all
    await _reviewer._call_reviewer(purpose="planner")
    assert engines.used() == ["claude", "claude"]


async def test_a_reading_from_a_window_that_has_reset_does_not_route(engines):
    engines.store.update(_claude(99, resets=NOW - 10))
    await _reviewer._call_reviewer(purpose="planner")
    assert engines.used() == ["claude"]


async def test_both_engines_exhausted_keeps_todays_try_then_wait(engines):
    engines.store.update(_claude(99))
    engines.store.update(_codex(99))
    engines.refuse.update({"claude", "codex"})
    with pytest.raises(RuntimeError, match="USAGE_LIMIT"):
        await _reviewer._call_reviewer(purpose="planner")
    assert engines.used() == ["claude", "codex"], "tried the configured engine first, as before"


async def test_without_a_codex_login_nothing_is_routed_to_codex(engines, monkeypatch):
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "")
    engines.store.update(_claude(99))
    await _reviewer._call_reviewer(purpose="planner")
    assert engines.used() == ["claude"]


async def test_a_routed_call_that_is_refused_goes_back_rather_than_failing(engines):
    # Changed on purpose (round 2, item 1): claude used to be at 100% here and was called anyway; the
    # router now never calls an engine it knows is full (see the deferral test below). Claude has room.
    # Changed again (owner rule 4): headroom no longer moves the planner off its preferred Claude, so
    # Codex is reached first through a role that prefers it. This used to be purpose="planner", then
    # "delegate"; delegates now stay on Claude too (owner rule 4), so a QA judge reaches Codex first.
    engines.store.update(_claude(98))
    engines.store.update(_codex(10))
    engines.refuse.add("codex")
    await _reviewer._call_reviewer(purpose="correctness")
    assert engines.used() == ["codex", "claude"]


async def test_a_refusal_teaches_the_next_call_to_route_around_the_limit(engines):
    engines.refuse.add("claude")
    await _reviewer._call_reviewer(purpose="planner")
    assert engines.used() == ["claude", "codex"]
    limited = engines.store.latest["claude"].limited_until
    assert limited and limited > time.time() - 3600 * 24, "the reset time in the refusal text"
    engines.refuse.clear()
    engines.calls.clear()
    # Whatever the refusal's clock, a limit still in the future routes the next call up front.
    engines.store.note_limit(_session_config._claude_account(), NOW + 3600)
    await _reviewer._call_reviewer(purpose="planner")
    assert engines.used() == ["codex"]


async def test_a_refusal_retry_asks_the_router_and_never_calls_a_known_exhausted_engine(engines):
    engines.store.update(_claude(10))
    engines.store.update(_codex(100))
    # Owner rule 2: Codex is known exhausted by its refusal, not by its full meter.
    engines.store.note_limit("codex", NOW + 3600)
    engines.refuse.add("claude")
    rows = Rows()
    with pytest.raises(RuntimeError, match="USAGE_LIMIT"):
        await _reviewer._call_reviewer(purpose="planner", timeline=rows.timeline)
    assert engines.used() == ["claude"], "codex refused with its reset ahead: deferred, not called"
    last = _session_config._routes[-1]
    assert last["deferred"] is True and last["engine"] is None
    assert last["reason"] == "claude refused; codex exhausted"
    assert {c["engine"] for c in last["candidates"]} == {"claude", "codex"}
    assert [t for i, t in rows.rows if i == "🔀"][-1].startswith("router: planner -> deferred (")


async def test_a_refusal_retry_is_a_full_recorded_decision(engines):
    engines.store.update(_claude(10))
    engines.store.update(_codex(40))
    engines.refuse.add("claude")
    await _reviewer._call_reviewer(purpose="planner", model="opus", effort="high", timeline=Rows().timeline)
    assert engines.used() == ["claude", "codex"]
    last = _session_config._routes[-1]
    assert last["engine"] == "codex" and "claude refused" in last["reason"]
    assert len(last["candidates"]) == 2 and last["builders"] == []
    assert last["model"] == engines.calls[1]["model"] and last["effort"] == engines.calls[1]["effort"]


async def test_rate_limits_in_a_codex_result_update_the_meter(engines, monkeypatch):
    seen = []
    snapshot = _codex(91).to_dict()

    async def raw(**kwargs):
        rec = {"output_tokens": 3, "rate_limits": snapshot}
        return "answer" + _usage._USAGE_MARK + json.dumps(rec) + "-->", None

    monkeypatch.setattr(_reviewer, "_call_reviewer_raw", raw)
    monkeypatch.setattr(_usage, "_record_usage", lambda role, round_, rec: seen.append(rec))
    await _reviewer._call_reviewer(purpose="planner")
    assert engines.store.latest["codex"].five_hour_pct == 91.0
    assert "rate_limits" not in seen[0] and seen[0]["output_tokens"] == 3


# --------------------------------------------------------------------------- #
# Launch slots
# --------------------------------------------------------------------------- #


def _slotted(name, task):
    return SimpleNamespace(
        name=name,
        phase="RUNNING",
        to_dict=lambda: {"action": {"metadata": {"task": {"id": {"name": task}}}}},
    )


def test_the_free_slot_with_the_most_headroom_is_preferred():
    one, two = _factory_base.SLOTS[:2]
    assert _factory_meters.free_slot([], headroom={one: 20.0, two: 70.0}) == two
    assert _factory_meters.free_slot([], headroom={one: 70.0, two: 20.0}) == one


def test_no_headroom_information_keeps_todays_order():
    one, two = _factory_base.SLOTS[:2]
    assert _factory_meters.free_slot([]) == one
    assert _factory_meters.free_slot([], two) == two
    assert _factory_meters.free_slot([], headroom={}) == one
    assert _factory_meters.free_slot([], headroom={one: None, two: None}) == one
    assert _factory_meters.free_slot([], two, headroom={one: 50.0, two: 50.0}) == two, "ties keep the order"


def test_an_unknown_slot_counts_as_a_fresh_window():
    one, two = _factory_base.SLOTS[:2]
    assert _factory_meters.free_slot([], headroom={one: None, two: 30.0}) == one
    assert _factory_meters.free_slot([], headroom={one: 0.0, two: None}) == two


def test_one_project_per_login_stays_absolute_whatever_the_meters_say():
    one, two = _factory_base.SLOTS[:2]
    assert _factory_meters.free_slot([_slotted("busy", two)], headroom={one: 1.0, two: 99.0}) == one
    both = [_slotted("a", one), _slotted("b", two)]
    assert _factory_meters.free_slot(both, headroom={one: 99.0, two: 99.0}) is None


def _token_account():
    """The meter account of the one work token every slot shares."""
    return f"claude {_factory_base.PROFILE.secrets.claude_token}"


def _report_with(account, five, resets):
    return (
        "<table>"
        + _meter.report_comment(_meter.Meter(account, "oauth-usage", NOW, five, resets))
        + "</table>"
    )


def test_slot_headroom_reads_each_slots_newest_run_report(monkeypatch):
    one, two = _factory_base.SLOTS[:2]
    token = _token_account()
    reports = {
        "new-one": _report_with(token, 80.0, NOW + 3600),
        "old-one": _report_with(token, 5.0, NOW + 3600),
        "two": _report_with(token, 99.0, NOW - 60),  # that window has reset
    }
    runs = [_slotted("new-one", one), _slotted("two", two), _slotted("old-one", one)]
    for run in runs:
        run.get_report = lambda name=run.name: reports[name]
    monkeypatch.setattr(remote.Run, "listall", lambda **kw: runs)
    # One token, one reading: the newest report of any slot speaks for every slot.
    assert _factory_meters.slot_headroom([one, two]) == {one: pytest.approx(20.0), two: pytest.approx(20.0)}


def test_slot_headroom_never_raises(monkeypatch):
    def broken(**kw):
        raise RuntimeError("no backend")

    monkeypatch.setattr(remote.Run, "listall", broken)
    assert _factory_meters.slot_headroom(_factory_base.SLOTS) == {}
    run = _slotted("p", _factory_base.SLOTS[0])
    run.get_report = lambda: (_ for _ in ()).throw(RuntimeError("no report"))
    monkeypatch.setattr(remote.Run, "listall", lambda **kw: [run])
    assert _factory_meters.slot_headroom(_factory_base.SLOTS) == {
        _factory_base.SLOTS[0]: None,
        _factory_base.SLOTS[1]: None,
    }


def test_a_dispatched_project_goes_to_the_slot_with_headroom(flyte_world, monkeypatch):
    one, two = _factory_base.SLOTS[:2]
    monkeypatch.setattr(_factory_meters, "slot_headroom", lambda slots: {one: 10.0, two: 90.0})
    factory.launch_project("brief", "valid-name")
    assert [c["task"] for c in flyte_world.created] == [two]


async def test_codex_role_returns_to_own_effort_after_routed_claude_refusal(engines, monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_PLANNER_ENGINE", "codex")
    monkeypatch.delenv("AGENT_FACTORY_PLANNER_CODEX_EFFORT", raising=False)
    engines.store.update(_codex(100))
    engines.store.note_limit("codex", NOW + 3600)  # owner rule 2: the refusal, not the meter, blocks
    engines.refuse.add("claude")
    raw = _reviewer._call_reviewer_raw

    async def refresh_codex_while_claude_refuses(**kwargs):
        # Changed on purpose (round 2, item 1): a full codex is no longer called on the retry, so the
        # test lets its meter reopen (a refresh) while claude's refusal is in flight.
        try:
            return await raw(**kwargs)
        except RuntimeError:
            engines.store.readings["codex"].limited_until = None  # a reading keeps a refusal that is ahead
            engines.store.update(_codex(10))
            raise

    monkeypatch.setattr(_reviewer, "_call_reviewer_raw", refresh_codex_while_claude_refuses)
    await _reviewer._call_reviewer(purpose="planner", model="opus", effort="max")
    assert engines.used() == ["claude", "codex"]
    assert engines.calls[1]["effort"] == "high"


async def test_call_meter_refreshes_header_and_warns_once(engines, monkeypatch):
    snapshot = _codex(91).to_dict()

    async def raw(**kwargs):
        return "answer" + _usage._USAGE_MARK + json.dumps({"rate_limits": snapshot}) + "-->", None

    monkeypatch.setattr(_reviewer, "_call_reviewer_raw", raw)
    timeline = _timeline._Timeline()
    await _reviewer._call_reviewer(purpose="planner", timeline=timeline)
    await _reviewer._call_reviewer(purpose="planner", timeline=timeline)
    assert "codex: 5h 91%" in timeline.render()
    assert [icon for _, icon, _ in timeline._rows].count("⚠️") == 1


@pytest.mark.asyncio
async def test_engine_callback_reports_fallback(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "{}")
    calls = []
    settings = []

    async def raw(**kwargs):
        calls.append(kwargs["engine"])
        if kwargs["engine"] == "codex":
            raise RuntimeError(_usage.USAGE_LIMIT_MARK)
        return "answer", None

    monkeypatch.setattr(_reviewer, "_call_reviewer_raw", raw)
    monkeypatch.setattr(_usage, "_engine_exhausted", lambda e: False)
    await _reviewer._call_reviewer(
        purpose="delegate", engine="codex", on_engine=lambda *args: settings.append(args)
    )
    assert calls == ["codex", "claude"]
    assert [s[0] for s in settings] == calls
    assert settings[-1][1] == f"claude {_policy.DEFAULTS['author'].model}/default"


async def test_both_refusals_are_remembered_for_the_next_call(engines):
    engines.refuse.update({"claude", "codex"})
    with pytest.raises(RuntimeError, match="USAGE_LIMIT"):
        await _reviewer._call_reviewer(purpose="planner")
    assert engines.used() == ["claude", "codex"]
    for engine in ("claude", "codex"):
        assert engines.store.latest[engine].limited_until is not None


def test_weekly_exhaustion_ranks_the_slots_last_but_does_not_close_them(monkeypatch):
    one, two = _factory_base.SLOTS[:2]
    runs = [_slotted("one", one), _slotted("two", two)]
    meter = _meter.Meter(_token_account(), "oauth-usage", NOW, 0, NOW + 3600, 100, NOW + 86400)
    for run in runs:
        run.get_report = lambda: _meter.report_comment(meter)
    monkeypatch.setattr(remote.Run, "listall", lambda **kw: runs)
    headroom = _factory_meters.slot_headroom([one, two], now=NOW)
    assert headroom == {one: 0, two: 0}, "the one token's weekly window is every slot's"
    assert _factory_meters.free_slot([], headroom=headroom) == one
    assert _factory_meters.free_slot([_slotted("busy", one)], headroom=headroom) == two


async def test_codex_delegation_refusal_uses_claude_defaults(engines):
    engines.refuse.add("codex")
    setting = _policy.resolve_codex("delegate")
    await _reviewer._call_reviewer(
        purpose="delegate", engine="codex", model=setting.model, effort=setting.effort
    )
    assert engines.used() == ["codex", "claude"]
    assert (engines.calls[-1]["model"], engines.calls[-1]["effort"]) == _policy.DEFAULTS["author"]


def test_slot_meters_returns_every_reading_in_the_newest_report(monkeypatch):
    one, two = _factory_base.SLOTS[:2]
    run = _slotted("new", one)
    run.get_report = lambda: _report_with("claude login-one", 80.0, NOW + 3600)
    monkeypatch.setattr(remote.Run, "listall", lambda **kw: [run])
    found = _factory_meters.slot_meters([one, two])
    assert [m.account for m in found[one]] == ["claude login-one"]
    assert found[two] == []

    def broken(**kw):
        raise RuntimeError("no backend")

    monkeypatch.setattr(remote.Run, "listall", broken)
    assert _factory_meters.slot_meters([one]) == {}


def _stale_report(account, five, resets, age):
    meter = _meter.Meter(account, "oauth-usage", NOW - age, five, resets)
    return "<table>" + _meter.report_comment(meter) + "</table>"


def test_a_stale_full_window_stays_exhausted_in_slot_ranking(monkeypatch):
    # QA board #15 repro A: a 15-minute-old 100% reading became "unknown" and ranked first.
    one, two = _factory_base.SLOTS[:2]
    token = _token_account()
    reports = {
        "a": _stale_report(token, 100.0, NOW + 3600, 900),
        "b": _report_with(token, 50.0, NOW + 3600),
    }
    runs = [_slotted("a", one), _slotted("b", two)]
    for run in runs:
        run.get_report = lambda name=run.name: reports[name]
    monkeypatch.setattr(remote.Run, "listall", lambda **kw: runs)
    monkeypatch.setattr(_factory_meters, "login_meters", lambda slots, **kw: {})
    headroom = _factory_meters.slot_headroom([one, two], now=NOW)
    assert headroom[one] == 0.0 and headroom[two] == pytest.approx(50.0)
    assert _factory_meters.free_slot([], headroom=headroom) == two


def test_shared_codex_headroom_is_not_credited_to_a_full_claude_token(monkeypatch):
    # QA board #15 repro B: Codex at 10% made an exhausted Claude slot look 90% free.
    one, two = _factory_base.SLOTS[:2]
    monkeypatch.setattr(_factory_meters, "slot_meters", lambda slots: {one: [], two: []})
    monkeypatch.setattr(_factory_base, "PROFILE", _with_codex_token())
    claude, codex = _token_account(), "codex codex_tok"
    cached = {
        claude: _meter.Meter(claude, "oauth-usage", NOW, 100.0, NOW + 3600),
        codex: _meter.Meter(codex, "app-server", NOW, 10.0, NOW + 3600),
    }
    monkeypatch.setattr(_factory_meters, "login_meters", lambda slots, **kw: cached)
    headroom = _factory_meters.slot_headroom([one, two], now=NOW)
    assert headroom[one] == 0.0 and headroom[two] == 0.0


def test_an_exhausted_slot_is_never_chosen():
    # Owner rule 2: exhausted is every engine of the slot refused (`LIMITED`); 0.0 is a full meter,
    # which only ranks last. This used 0.0 for exhausted.
    one, two = _factory_base.SLOTS[:2]
    assert (
        _factory_meters.free_slot([], headroom={one: _factory_meters.LIMITED, two: _factory_meters.LIMITED})
        is None
    )
    assert _factory_meters.free_slot([], headroom={one: _factory_meters.LIMITED, two: 5.0}) == two
    assert _factory_meters.free_slot([], headroom={one: 0.0, two: 0.0}) == one


def test_the_router_treats_a_reading_past_two_refresh_intervals_as_unknown():
    assert _router.STALE_AFTER_S == 2 * _meter.MIN_INTERVAL_S


def _with_codex_token():
    """The suite's profile with a Codex access token, so every slot can run on either engine."""
    import dataclasses

    secrets = dataclasses.replace(_factory_base.PROFILE.secrets, codex_token="codex_tok")
    return dataclasses.replace(_factory_base.PROFILE, secrets=secrets)


def test_a_slot_with_claude_limited_is_free_on_codex(monkeypatch):
    # A project whose Claude token refused can still run on Codex, as route() would pick.
    one = _factory_base.SLOTS[0]
    monkeypatch.setattr(_factory_base, "PROFILE", _with_codex_token())
    monkeypatch.setattr(_factory_base, "SLOTS", (one,))
    monkeypatch.setattr(_factory_meters, "slot_meters", lambda slots: {one: []})
    claude, codex = _token_account(), "codex codex_tok"
    cached = {claude: _meter.Meter(claude, "limit-text", NOW, limited_until=NOW + 3600)}
    monkeypatch.setattr(_factory_meters, "login_meters", lambda slots, **kw: cached)
    headroom = _factory_meters.slot_headroom([one], now=NOW)
    assert _factory_meters.free_slot([], headroom=headroom) == one
    cached[codex] = _meter.Meter(codex, "app-server", NOW, 40.0, NOW + 3600)
    headroom = _factory_meters.slot_headroom([one], now=NOW)
    assert headroom[one] == pytest.approx(60.0), "the headroom is that of the engine it would run on"
    assert _factory_meters.free_slot([], headroom=headroom) == one
    # Only when every engine it can use is actually limited is the slot out.
    cached[codex] = _meter.Meter(codex, "limit-text", NOW, limited_until=NOW + 600)
    headroom = _factory_meters.slot_headroom([one], now=NOW)
    assert _factory_meters.free_slot([], headroom=headroom) is None


def test_a_full_meter_ranks_the_slots_last_but_does_not_close_them(monkeypatch):
    # Owner rule 2: a 100% reading with no refusal is a hint. It loses a tie, never the slot.
    one, two = _factory_base.SLOTS[:2]
    monkeypatch.setattr(_factory_meters, "slot_meters", lambda slots: {one: [], two: []})
    token = _token_account()
    cached = {token: _meter.Meter(token, "oauth-usage", NOW, 100.0, NOW + 3600)}
    monkeypatch.setattr(_factory_meters, "login_meters", lambda slots, **kw: cached)
    headroom = _factory_meters.slot_headroom([one, two], now=NOW)
    assert headroom == {one: 0.0, two: 0.0}
    assert _factory_meters.free_slot([], headroom=headroom) == one
    assert _factory_meters.free_slot([_slotted("busy", one)], headroom=headroom) == two
