"""The weekly reserve: k percent held back per hour left until a window resets, per account."""

import json
import secrets
from datetime import datetime, timezone

import pytest

from flyte_agent_factory import _factory_base, _factory_meters, _meter, _planner, _queue

H = 3600.0
NOW = 1_000_000.0


def meter(five=None, week=None, *, five_in_h=3.0, week_in_h=61.0, account="claude login"):
    return _meter.Meter(
        account,
        "oauth-usage",
        NOW,
        five_hour_pct=five,
        five_hour_resets_at=NOW + five_in_h * H if five is not None else None,
        weekly_pct=week,
        weekly_resets_at=NOW + week_in_h * H if week is not None else None,
    )


def test_no_reserve_is_todays_behaviour():
    assert _meter.reserve_ok(meter(five=100, week=100), 0, NOW) == (True, "", None)


def test_weekly_under_the_reserve_is_held_back_with_the_reason():
    resets = datetime.fromtimestamp(NOW + 61 * H, timezone.utc)
    ok, reason, usable_at = _meter.reserve_ok(meter(week=78), 0.5, NOW)
    assert not ok
    assert reason == (
        f"reserve: weekly 22% left < 0.5%/h x 61h = 30.5% held back (resets {resets:%a %H:%M}Z)"
    )
    # 22% left covers 44 more hours at 0.5%/h, so it opens when 44h remain.
    assert usable_at == pytest.approx(NOW + (61 - 44) * H)


def test_weekly_over_the_reserve_is_admitted():
    assert _meter.reserve_ok(meter(week=60), 0.5, NOW)[0]


def test_the_held_back_amount_shrinks_as_the_reset_nears():
    assert not _meter.reserve_ok(meter(week=78), 0.5, NOW)[0]
    assert _meter.reserve_ok(meter(week=78), 0.5, NOW + 20 * H)[0]  # 41h left x 0.5 = 20.5 <= 22


def test_every_reported_window_must_clear_its_own_reserve():
    ok, reason, _ = _meter.reserve_ok(meter(five=95, week=10, five_in_h=4, week_in_h=10), 2, NOW)
    assert not ok
    assert reason.startswith("reserve: 5h 5% left < 2%/h x 4h = 8% held back")
    assert "weekly" not in reason


def test_a_weekly_only_account_gates_on_the_weekly_reserve_alone():
    weekly_only = meter(week=78)
    assert weekly_only.five_hour_pct is None
    assert not _meter.reserve_ok(weekly_only, 0.5, NOW)[0]
    assert _meter.reserve_ok(meter(week=50), 0.5, NOW)[0]


def test_a_window_that_has_reset_or_is_unknown_holds_nothing_back():
    assert _meter.reserve_ok(meter(week=99, week_in_h=-1), 5, NOW)[0]
    assert _meter.reserve_ok(_meter.Meter("claude x", "oauth-usage", NOW), 5, NOW)[0]


# --- the stored constant -----------------------------------------------------------------------------


@pytest.fixture
def uri(state_uri):
    """A prefix of its own: the fake object store outlives a test."""
    return f"{state_uri}/{secrets.token_hex(4)}"


def test_k_defaults_to_zero_and_is_changed_without_a_redeploy(uri):
    assert _queue.read_reserves(uri).get("claude", 0) == 0
    _queue.put_reserve(uri, "claude", 0.5)
    assert _queue.read_reserves(uri).get("claude", 0) == 0.5
    assert _queue.read_reserves(uri).get("codex", 0) == 0  # each account has its own
    _queue.put_reserve(uri, "claude", 0.25)
    assert _queue.read_reserves(uri) == {"claude": 0.25}


@pytest.mark.parametrize("account,k", [("gemini", 1), ("claude", -1), ("claude", float("nan"))])
def test_a_bad_account_or_constant_is_refused(uri, account, k):
    with pytest.raises(ValueError):
        _queue.put_reserve(uri, account, k)
    assert _queue.read_reserves(uri) == {}


@pytest.fixture
def stored(queue_state):
    """The profile's `[state] uri` on a local directory (`queue_state` sets it)."""
    return _factory_base.PROFILE.state.uri


def test_admission_reads_the_constant_set_from_chat(stored, monkeypatch):
    import asyncio

    from flyte_agent_factory import _factory

    held = meter(week=78)
    monkeypatch.setattr(_factory_meters, "slot_meters", lambda slots: {s: [held] for s in slots})
    monkeypatch.setattr(_factory_meters, "login_meters", lambda *a, **kw: {held.account: held})
    monkeypatch.setattr(_factory_meters, "match_claude_meter", lambda *a, **kw: held)
    monkeypatch.setattr(f"{_factory_meters.__name__}.time.time", lambda: NOW)
    slot = _factory_base.SLOTS[0]

    assert _factory_meters.slot_headroom([slot], now=NOW)[slot] is not None
    reply = json.loads(asyncio.run(_factory.set_reserve.func("claude", 0.5)))
    assert reply == {"account": "claude", "k": 0.5}
    assert json.loads(asyncio.run(_factory.get_reserve.func())) == {"claude": 0.5}
    assert _factory_meters.slot_headroom([slot], now=NOW)[slot] == _factory_meters.LIMITED
    asyncio.run(_factory.set_reserve.func("claude", 0))
    assert _factory_meters.slot_headroom([slot], now=NOW)[slot] != _factory_meters.LIMITED


def test_the_chat_may_run_the_reserve_tasks():
    from flyte_agent_factory import _factory_mcp

    assert {"set_reserve", "get_reserve"} <= set(_factory_mcp.CHAT_TASKS)
    assert "factory.set_reserve" in _factory_mcp.INSTRUCTIONS


# --- the planner ------------------------------------------------------------------------------------


def facts(capacity):
    item = _planner.BoardItem("I1", 1, "example/repo", status="Ready", position=1, body="A task")
    return _planner.Facts(items=(item,), capacity=tuple(capacity), now=NOW)


def test_the_planner_holds_a_launch_whose_account_is_under_its_reserve():
    held = _planner.Capacity("claude", "login", meter(week=78), slot="s", reserve=0.5)
    [waiting] = _planner.plan(facts([held])).waiting
    assert waiting.reason.code == "no_capacity"
    assert waiting.reason.detail.startswith("claude reserve: weekly 22% left < 0.5%/h x 61h = 30.5%")


def test_the_planner_launches_once_the_account_clears_its_reserve():
    clear = _planner.Capacity("claude", "login", meter(week=60), slot="s", reserve=0.5)
    assert len(_planner.plan(facts([clear])).launches) == 1


def test_the_planner_falls_back_to_the_other_account_when_one_is_held():
    held = _planner.Capacity("claude", "c", meter(week=78), slot="s", reserve=0.5)
    open_ = _planner.Capacity("codex", "x", meter(week=10, account="codex"), slot="s", reserve=0.5)
    [launch] = _planner.plan(facts([held, open_])).launches
    assert launch.engine == "codex"


def test_the_queue_listing_and_overview_say_why_an_account_is_held(flyte_world, monkeypatch):
    from flyte_agent_factory import _factory
    from tests.test_operate import operate

    held = meter(week=78)
    monkeypatch.setattr(_factory_meters, "slot_meters", lambda slots: {s: [held] for s in slots})
    monkeypatch.setattr(_factory_meters, "login_meters", lambda *a, **kw: {held.account: held})
    monkeypatch.setattr(_factory_meters, "match_claude_meter", lambda *a, **kw: held)
    assert "reserve" not in _factory.queue_view()
    _queue.put_reserve(_factory_base.PROFILE.state.uri, "claude", 0.5)
    [hold] = _factory_meters.reserve_holds(now=NOW)
    assert _factory.queue_view(now=NOW)["reserve"] == [hold]
    assert hold.startswith("claude reserve: weekly 22% left < 0.5%/h x 61h = 30.5% held back")
    view = operate.build_overview([], now=NOW, reserve=[hold])
    assert hold in operate.overview_text(view)
