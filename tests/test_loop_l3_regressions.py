"""L3 regressions at the benchmark and production-slot boundaries."""

import asyncio
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import flyte
import flyte.report
import pytest
from flyte import remote

from flyte_agent_factory import (
    _bench_flow,
    _dashboard,
    _factory,
    _factory_base,
    _factory_meters,
    _issues,
    _loop,
    _loop_flow,
    _meter,
    _profile,
    _queue,
)
from tests.test_loop_budget import LIMITS, NOW, meter
from tests.test_loop_flow import ADMISSION, run, world  # noqa: F401 -- fixtures and helpers used by name


@pytest.fixture
def admission(monkeypatch):
    profile = _loop_flow.PROFILE
    monkeypatch.setattr(
        _loop_flow,
        "PROFILE",
        replace(
            profile,
            loop=replace(profile.loop, daily_usd=100, weekly_usd=500),
            bench=_profile.Bench("repo", "key", 300, claude_token=profile.secrets.claude_token),
        ),
    )
    return ADMISSION


def test_unset_caps_have_their_own_reason():
    assert _loop.budget(NOW, [], {}, False, False) == _loop.Budget("wait", "caps-unset")


def test_unknown_production_login_waits_with_owner_demand():
    assert _loop.budget(NOW, [], {"claude": None}, True, False, **LIMITS).reason == "owner-demand"


@pytest.mark.parametrize("used", [None, 75, 99])
def test_bench_uses_its_entire_window_even_with_owner_demand(monkeypatch, used):
    reading = _meter.Meter("claude bench", "fixture", NOW, used, NOW + 3600)

    async def read(login):
        return reading

    async def sleep(seconds):
        pytest.fail("a bench login has no owner reserve")

    monkeypatch.setattr(_bench_flow, "_read_meter", read)
    monkeypatch.setattr(_factory, "owner_demand", lambda: True)
    assert asyncio.run(_bench_flow._quota_guard("bench", clock=lambda: NOW, sleep=sleep)) is reading


@pytest.mark.parametrize("origin,stops", [(False, False), (True, True)])
def test_kill_switch_only_stops_loop_benchmarks(monkeypatch, origin, stops):
    async def read(login):
        return meter()

    monkeypatch.setattr(_bench_flow, "_read_meter", read)
    monkeypatch.setattr(_bench_flow, "_loop_origin", lambda: origin)
    monkeypatch.setattr(_loop, "killed", lambda *args: True)
    monkeypatch.setattr(_bench_flow, "_app", lambda: None)
    monkeypatch.setattr(
        _bench_flow,
        "PROFILE",
        replace(_bench_flow.PROFILE, loop=replace(_bench_flow.PROFILE.loop, enabled=True)),
    )
    if stops:
        with pytest.raises(_bench_flow.HarnessError, match="loop killed"):
            asyncio.run(_bench_flow._quota_guard("bench", clock=lambda: NOW))
    else:
        assert asyncio.run(_bench_flow._quota_guard("bench", clock=lambda: NOW)) is not None


def test_two_meter_polls_have_distinct_child_inputs(monkeypatch):
    reads = []

    class Reader:
        def override(self, **kwargs):
            return self

        async def aio(self, account, poll):
            reads.append((account, poll))
            return json.dumps(meter().to_dict())

    monkeypatch.setattr(_bench_flow, "read_login_meter", Reader())
    asyncio.run(_bench_flow._read_meter("bench"))
    asyncio.run(_bench_flow._read_meter("bench"))
    assert len(reads) == 2 and reads[0] != reads[1]


def test_bench_admission_reads_the_work_token_and_no_other_meter(admission, monkeypatch):
    token = _loop_flow.PROFILE.secrets.claude_token

    def readings(token_used, other_used):
        return {
            f"claude {token}": meter(f"claude {token}", used=token_used),
            "claude another-account": meter("claude another-account", used=other_used),
            "codex": meter("codex", used=other_used),
        }

    monkeypatch.setattr(_factory, "owner_demand", lambda: True)
    monkeypatch.setattr(time, "time", lambda: NOW)
    monkeypatch.setattr(_loop_flow, "_spend", lambda *args: [])
    # Owner rule 2: the router no longer defers on a full window, so the wait for a full token is the
    # admission's own. Every case runs on that one token, so a full one parks the benchmark.
    monkeypatch.setattr(_factory_meters, "login_meters", lambda **kwargs: readings(100, 0))
    assert admission(None).reason == "window-full"
    # Another account's or Codex's full window does not speak for the work token.
    monkeypatch.setattr(_factory_meters, "login_meters", lambda **kwargs: readings(0, 100))
    assert admission(None).action == "run"


def test_admission_counts_current_run_paid_attempt(admission, monkeypatch):
    monkeypatch.setattr(_factory_meters, "login_meters", lambda **kwargs: {})
    monkeypatch.setattr(time, "time", lambda: NOW)

    def spend(now, exclude_run=""):
        assert exclude_run == ""
        return [{"at": NOW, "engine": "claude", "usd_total": 100}]

    monkeypatch.setattr(_loop_flow, "_spend", spend)
    assert admission(None).reason == "cap-daily"


def test_pending_ledger_becomes_a_visible_wait(admission, monkeypatch):
    monkeypatch.setattr(_factory_meters, "login_meters", lambda **kwargs: {})

    def pending(now):
        raise ValueError("loop benchmark spend is not yet available")

    monkeypatch.setattr(_loop_flow, "_spend", pending)
    assert admission(None).reason == "spend-pending"


def test_revert_breaker_ignores_events_before_owner_restart(monkeypatch):
    profile = SimpleNamespace(
        loop=SimpleNamespace(repo="acme/factory"), github=SimpleNamespace(maintainers=("owner",))
    )
    events = {
        1: [{"event": "closed", "actor": {"login": "owner"}, "created_at": "2026-10-25T11:00:00Z"}],
        2: [
            {
                "event": "labeled",
                "label": {"name": "loop:reverted"},
                "actor": {"login": "factory[bot]"},
                "created_at": "2026-10-25T10:00:00Z",
            }
        ],
        3: [
            {
                "event": "labeled",
                "label": {"name": "loop:reverted"},
                "actor": {"login": "factory[bot]"},
                "created_at": "2026-10-25T09:00:00Z",
            }
        ],
    }
    app = SimpleNamespace(
        app_login=lambda: "factory[bot]",
        labelled_issues=lambda repo, label: (
            [{"number": 1}] if label == "loop-kill" else [{"number": 2}, {"number": 3}]
        ),
        issue_events=lambda repo, number: events[number],
        create_issue=lambda *args: pytest.fail("owner restart must not re-trip on old evidence"),
    )
    monkeypatch.setattr(_loop_flow, "PROFILE", profile)
    reason = _loop_flow._revert_breaker(cast(Any, app), NOW)
    assert reason == ""
    _loop.open_breaker(cast(Any, profile), cast(Any, app), reason)


@pytest.mark.usefixtures("world")
def test_caps_wait_is_written_to_report_and_timeline(monkeypatch, capsys):
    published: list[dict[str, Any]] = []

    async def publish(html, **kwargs):
        state = _dashboard.from_report(html)
        assert state is not None
        published.append(state)

    monkeypatch.setattr(flyte.report.replace, "aio", publish)
    monkeypatch.setattr(_loop_flow, "_admission", lambda app: _loop.Budget("wait", "caps-unset"))
    assert run() == "caps-unset"
    assert published[-1]["loop_wait"]["reason"] == "caps-unset"
    assert published[-1]["timeline"][-1]["reason"] == "caps-unset"
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["reason"] == "caps-unset"


def test_benchmark_submission_is_labelled_and_retry_reattaches(monkeypatch):
    from connectrpc.code import Code
    from connectrpc.errors import ConnectError

    contexts: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    runs: dict[str, Any] = {}

    async def overridden(**kwargs):
        return task

    task = SimpleNamespace(override=SimpleNamespace(aio=overridden))

    async def submit(actual, **kwargs):
        assert actual is task
        assert kwargs == {"candidate_sha": "head", "baseline_sha": "main", "max_usd": 300}
        run = SimpleNamespace(wait=SimpleNamespace(aio=wait), outputs=SimpleNamespace(aio=outputs))
        runs[contexts[-1]["name"]] = run
        return run

    async def wait():
        pass

    async def outputs():
        return '{"verdict": {}}', ""

    async def report(html, **kwargs):
        state = _dashboard.from_report(html)
        assert state is not None
        reports.append(state)

    async def get(name):
        if name not in runs:
            raise ConnectError(Code.NOT_FOUND, "missing")
        return runs[name]

    def context(**kwargs):
        contexts.append(kwargs)
        return SimpleNamespace(run=SimpleNamespace(aio=submit))

    monkeypatch.setattr(remote.Run, "get", SimpleNamespace(aio=get))
    monkeypatch.setattr(remote.Task, "get", lambda *args, **kwargs: task)
    monkeypatch.setattr(flyte, "with_runcontext", context)
    monkeypatch.setattr(flyte, "ctx", lambda: SimpleNamespace(action=SimpleNamespace(run_name="loop")))
    monkeypatch.setattr(flyte.report.replace, "aio", report)
    monkeypatch.setattr(
        _loop_flow,
        "PROFILE",
        replace(_loop_flow.PROFILE, bench=_profile.Bench("repo", "key", 300)),
    )
    for _ in range(2):
        assert asyncio.run(_loop_flow._benchmark("head", "main")) == '{"verdict": {}}'
    assert contexts[0]["labels"] == {"factory-loop": "loop"}
    assert len(contexts) == 1
    assert len(runs) == 1
    assert reports[-1]["loop_benchmark"] == contexts[-1]["name"]
    assert reports[-1]["loop_spend"] == []


def test_pending_legacy_ledger_recovers_child_outputs(monkeypatch):
    async def outputs():
        return "{}", json.dumps({"ended_at": NOW, "usd_total": 12})

    async def details():
        return SimpleNamespace(outputs=outputs)

    def actions(**kwargs):
        assert kwargs == {"for_run_name": "loop"}
        return [SimpleNamespace(task_name="factory-bench.benchmark", details=details)]

    run = SimpleNamespace(
        name="loop",
        task="factory-loop.loop_run",
        pb2=SimpleNamespace(labels={}),
        get_report=lambda: _dashboard.status_script({"loop_pending": True}),
    )
    monkeypatch.setattr(remote.Run, "listall", lambda **kwargs: [run])
    monkeypatch.setattr(remote.Action, "listall", actions)
    monkeypatch.setattr(_factory_base, "_task_name", lambda run: run.task)
    assert _loop_flow._spend(NOW) == [{"at": NOW, "usd_total": 12, "engine": "claude"}]


@pytest.mark.parametrize(
    "used,demand,expected",
    [(75, True, "wait"), (65, True, "run"), (None, True, "wait"), (99, False, "run")],
)
def test_production_floor_reads_the_work_token(monkeypatch, used, demand, expected):
    monkeypatch.setattr(
        _factory_base,
        "PROFILE",
        replace(
            _factory_base.PROFILE, loop=replace(_factory_base.PROFILE.loop, daily_usd=100, weekly_usd=500)
        ),
    )
    monkeypatch.setattr(_loop_flow, "_spend", lambda now: [])
    slot = _factory_base.PROFILE.slots[0]
    reading = _meter.Meter(
        f"claude {_factory_base.PROFILE.secrets.claude_token}", "fixture", NOW, used, NOW + 3600
    )
    monkeypatch.setattr(time, "time", lambda: NOW)
    monkeypatch.setattr(
        _factory_meters,
        "login_meters",
        lambda **kwargs: {
            reading.account: reading,
            "claude another-account": meter(),
            "codex": meter("codex"),
        },
    )
    assert _factory_meters._loop_slot_budget(slot.task, demand=demand).action == expected


def test_generated_issue_project_launch_is_labelled(flyte_world, monkeypatch):
    monkeypatch.setattr(
        _factory_base,
        "PROFILE",
        replace(
            _factory_base.PROFILE,
            loop=replace(_factory_base.PROFILE.loop, repo="acme/factory", daily_usd=100, weekly_usd=500),
        ),
    )
    request = _factory._new_request(
        flyte_world.queue,
        [],
        "<!-- loop-source: qa:example -->",
        "loop-project",
        "opus",
        "",
        "",
        {},
        "acme/factory#42",
    )
    _queue.enqueue(flyte_world.queue, request)
    monkeypatch.setattr(_factory_meters, "login_meters", lambda **kwargs: {})
    [name] = _factory.dispatch()
    assert flyte_world.runs[name].pb2.labels["factory-loop"] == _issues.issue_label(request.issue)
    assert not _factory.owner_demand()


def test_generated_issue_waits_but_owner_launches(flyte_world, monkeypatch):
    monkeypatch.setattr(
        _factory_base,
        "PROFILE",
        replace(
            _factory_base.PROFILE,
            loop=replace(_factory_base.PROFILE.loop, repo="acme/factory", daily_usd=100, weekly_usd=500),
        ),
    )
    _factory.launch_project("owner work", "owner-project")
    request = _factory._new_request(
        flyte_world.queue,
        [],
        "<!-- loop-source: qa:example -->",
        "loop-project",
        "opus",
        "",
        "",
        {},
        "acme/factory#42",
    )
    _queue.enqueue(flyte_world.queue, request)
    monkeypatch.setattr(_factory_meters, "login_meters", lambda **kwargs: {})
    assert _factory.dispatch() == []
    assert len(flyte_world.created) == 1
    assert _queue.pending(flyte_world.queue)[0].name == "loop-project"
