"""Loop admission and circuit breakers with fixed clocks and meter payloads."""

from dataclasses import replace
from datetime import datetime, timezone
from typing import cast

import flyte
import pytest
from flyte import remote

from flyte_agent_factory import _loop, _meter, _profile
from flyte_agent_factory._github import GithubApp
from tests.conftest import TEST_PROFILE

NOW = datetime(2026, 10, 25, 12, tzinfo=timezone.utc).timestamp()
LIMITS = dict(daily_usd=100, weekly_usd=500)


def meter(engine="claude", used=0, reset=NOW + 3600):
    return _meter.Meter(engine, "fixture", NOW, used, reset, 0, NOW + 86400)


def budget(*, meters=None, spend=(), **kwargs):
    return _loop.budget(
        NOW, spend, meters or {"claude": meter()}, False, kwargs.pop("killed", False), **(LIMITS | kwargs)
    )


def test_router_exhaustion_waits_for_earliest_engine():
    # Owner rule 2: exhaustion is a recorded refusal; this used two 100% readings alone.
    claude = replace(meter(used=100), limited_until=NOW + 3600)
    codex = replace(meter("codex", 100, NOW + 7200), limited_until=NOW + 7200)
    result = budget(meters={"claude": claude, "codex": codex})
    assert (result.action, result.reason, result.until) == ("wait", "router-deferred", NOW + 3600)


@pytest.mark.parametrize("used,expected", [(75, "wait"), (70, "wait"), (65, "run")])
def test_owner_floor(used, expected):
    result = _loop.budget(NOW, [], {"claude": meter(used=used)}, True, False, **LIMITS)
    assert result.action == expected


def test_no_owner_reserve():
    assert budget(meters={"claude": meter(used=99)}).action == "run"


def test_missing_caps_and_kill_block():
    assert _loop.budget(NOW, [], {}, False, False).action == "wait"
    assert budget(killed=True).reason == "killed"


def test_daily_cap_resumes_at_utc_midnight():
    result = budget(spend=[{"at": NOW - 1, "usd_total": 100, "engine": "claude"}])
    assert (result.reason, result.until) == ("cap-daily", NOW + 43200)
    assert budget(spend=[{"at": NOW - 86400, "usd_total": 1000, "engine": "claude"}]).reason == "cap-weekly"


def test_weekly_cap_follows_meter_reset():
    result = budget(spend=[{"at": NOW - 86400, "usd_total": 500, "engine": "claude"}])
    assert (result.reason, result.until) == ("cap-weekly", NOW + 86400)
    assert budget(spend=[{"at": NOW - 8 * 86400, "usd_total": 1000, "engine": "claude"}]).action == "run"


@pytest.mark.parametrize("value", [-1, float("inf"), True, '"100"'])
def test_invalid_caps(value):
    with pytest.raises(_profile.ProfileError):
        _profile.parse(
            TEST_PROFILE.read_text() + f'\n[loop]\nrepo = "acme/factory"\ndaily_usd = {str(value).lower()}\n'
        )


def test_profile_loop_budget_defaults_and_values():
    profile = _profile.parse(TEST_PROFILE.read_text() + '\n[loop]\nrepo = "acme/factory"\n')
    assert profile.loop.daily_usd == profile.loop.weekly_usd == 0
    assert profile.loop.owner_floor_pct == 30
    configured = _profile.parse(
        TEST_PROFILE.read_text()
        + '\n[loop]\nrepo = "acme/factory"\ndaily_usd = 100\nweekly_usd = 500\nowner_floor_pct = 25\n'
    )
    assert (configured.loop.daily_usd, configured.loop.weekly_usd, configured.loop.owner_floor_pct) == (
        100,
        500,
        25,
    )


@pytest.mark.parametrize(
    "reverts,aa,harness,expected",
    [
        ([NOW - 10, NOW - 20], [], [], "reverts"),
        ([NOW - 8 * 86400, NOW], [], [], ""),
        ([], ["better"], [], "aa"),
        ([], ["worse"], [], "aa"),
        ([], ["no_worse"], [], ""),
        ([], [], ["harness_error"] * 3, "harness-errors"),
        ([], [], ["harness_error", "accepted", "harness_error"], ""),
    ],
)
def test_breaker_conditions(reverts, aa, harness, expected):
    assert _loop.breaker(NOW, reverts, aa, harness) == expected


@pytest.mark.parametrize("value", [-1, 101, float("inf"), True])
def test_invalid_owner_floor(value):
    with pytest.raises(_profile.ProfileError, match="owner_floor_pct"):
        _profile.parse(
            TEST_PROFILE.read_text()
            + f'\n[loop]\nrepo = "acme/factory"\nowner_floor_pct = {str(value).lower()}\n'
        )


@pytest.mark.parametrize("reason", ["reverts", "aa", "harness-errors"])
def test_breaker_opens_its_own_switch(reason):
    from types import SimpleNamespace

    events = []
    profile = SimpleNamespace(
        loop=SimpleNamespace(repo="acme/factory"), github=SimpleNamespace(maintainers=("owner",))
    )
    app = SimpleNamespace(
        app_login=lambda: "factory[bot]",
        labelled_issues=lambda *args: [],
        create_issue=lambda *args: {"number": 7},
        add_labels=lambda *args: events.append(args),
    )
    # Deliberately minimal fakes: open_breaker reads only the attributes set above.
    _loop.open_breaker(cast(_profile.Profile, profile), cast(GithubApp, app), reason)
    assert events == [("acme/factory", 7, ["loop-kill"])]


def test_daily_windows_are_per_engine():
    rows = [{"at": NOW, "usd_total": 75, "engine": engine} for engine in ("claude", "codex")]
    assert budget(spend=rows).action == "run"


def test_weekly_owner_floor_and_zero_floor():
    reading = meter()
    reading.weekly_pct = 75
    assert _loop.budget(NOW, [], {"claude": reading}, True, False, **LIMITS).reason == "owner-demand"
    assert (
        _loop.budget(NOW, [], {"claude": reading}, True, False, owner_floor_pct=0, **LIMITS).action == "run"
    )


def test_spend_reads_outputs_and_never_bills_reuse(monkeypatch):
    import json
    from types import SimpleNamespace

    from flyte_agent_factory import _dashboard, _factory_base, _loop_flow

    cases = [
        {"ended_at": NOW, "usd_total": 12},
        {"ended_at": NOW, "usd_total": 100, "reused_from": {"bench_run": "old"}},
    ]
    run = SimpleNamespace(
        name="bench",
        task="factory-bench.benchmark",
        pb2=SimpleNamespace(labels={"factory-loop": "42"}),
        outputs=lambda: ("{}", "\n".join(json.dumps(row) for row in cases)),
    )
    project = SimpleNamespace(
        name="project",
        task="slot.project",
        pb2=SimpleNamespace(labels={"factory-loop": "42"}),
        get_report=lambda: _dashboard.status_script(
            {
                "generated_at": NOW,
                "cost": {
                    "coordinator": {"usd_estimate": 3},
                    "subagents": {"usd": 5},
                    "by_engine": {"codex": 5},
                },
            }
        ),
    )
    monkeypatch.setattr(remote.Run, "listall", lambda **kw: [run, project])
    monkeypatch.setattr(_factory_base, "_task_name", lambda r: r.task)
    monkeypatch.setattr(_factory_base, "_phase", lambda r: "SUCCEEDED")
    rows = _loop_flow._spend(NOW)
    assert sum(row["usd_total"] for row in rows) == 20
    assert sum(row["usd_total"] for row in rows if row["engine"] == "codex") == 5


def test_pending_or_unknown_spend_blocks(monkeypatch):
    from types import SimpleNamespace

    from flyte_agent_factory import _dashboard, _factory_base, _loop_flow

    run = SimpleNamespace(
        name="loop",
        task="factory-loop.loop_run",
        pb2=SimpleNamespace(labels={}),
        get_report=lambda: _dashboard.status_script({"loop_pending": True}),
    )
    monkeypatch.setattr(remote.Action, "listall", lambda **kwargs: [])
    monkeypatch.setattr(remote.Run, "listall", lambda **kw: [run])
    monkeypatch.setattr(_factory_base, "_task_name", lambda r: r.task)
    with pytest.raises(ValueError, match="spend"):
        _loop_flow._spend(NOW)
    assert _loop_flow._spend(NOW, exclude_run="loop") == []


def test_owner_demand_ignores_loop_projects(monkeypatch):
    from types import SimpleNamespace

    from flyte_agent_factory import _factory

    loop = SimpleNamespace(pb2=SimpleNamespace(labels={"factory-loop": "42"}))
    owner = SimpleNamespace(pb2=SimpleNamespace(labels={}))
    monkeypatch.setattr(_factory, "operated_live", lambda: [loop])
    assert not _factory.owner_demand()
    monkeypatch.setattr(_factory, "operated_live", lambda: [loop, owner])
    assert _factory.owner_demand()


def test_expired_meter_uses_router_fallback_and_is_unknown_to_owner_demand():
    # A window past its reset is not exhausted, but its new usage is unknown: on a production slot
    # with owner demand an unknown meter waits.
    reading = _meter.Meter("claude", "fixture", NOW, 100, NOW - 1)
    assert _loop.budget(NOW, [], {"claude": reading}, False, False, **LIMITS).action == "run"
    assert _loop.budget(NOW, [], {"claude": reading}, True, False, **LIMITS).reason == "owner-demand"


def test_retry_preserves_previous_attempt_spend(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from flyte_agent_factory import _dashboard, _loop_flow

    spend = [{"at": NOW, "engine": "claude", "usd_total": 12}]
    published = []

    async def details():
        return SimpleNamespace(attempts=1)

    async def report(attempt):
        if attempt == 1:
            raise RuntimeError("No report is available for action 'loop'")
        return _dashboard.status_script({"loop_spend": spend})

    async def get(name):
        return SimpleNamespace(
            action=SimpleNamespace(details=details), get_report=SimpleNamespace(aio=report)
        )

    async def replace(html, **kwargs):
        published.append(_dashboard.from_report(html))

    monkeypatch.setattr(remote.Run, "get", SimpleNamespace(aio=get))
    monkeypatch.setattr(flyte, "ctx", lambda: SimpleNamespace(action=SimpleNamespace(run_name="loop")))
    monkeypatch.setattr(flyte.report.replace, "aio", replace)
    asyncio.run(_loop_flow._initialize_spend())
    assert published == [{"loop_spend": spend}]
