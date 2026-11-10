"""Headless setup-token auth and conservative quota observations."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from flyte.remote import Run

from flyte_agent_factory import (
    _author,
    _bench,
    _meter,
    _profile,
    _session_config,
    _subagent_run,
    _supervise_support,
    _timeline,
    _usage,
)

TEXT = (Path(__file__).parent / "profiles" / "work.toml").read_text()


def token_profile():
    return _profile.parse(TEXT.replace("claude_token_x", "setup_token"))


def test_token_profile_binds_all_slots_to_one_account():
    profile = token_profile()
    assert profile.secrets.claude_token == "setup_token"
    assert profile.meter_logins["claude setup_token"] == "setup_token"
    for slot in profile.slots:
        assert profile.deploy_env(slot)["AGENT_FACTORY_CLAUDE_TOKEN_SECRET"] == "setup_token"
        assert not any("LOGIN" in key or "CREDENTIALS" in key for key in profile.deploy_env(slot))


async def test_usage_endpoint_acceptance_is_explicit(monkeypatch):
    monkeypatch.setattr(_meter, "SETUP_TOKEN_USAGE_SUPPORTED", True)

    def handler(request):
        assert request.headers["authorization"] == "Bearer test-token"
        return httpx.Response(200, json={"five_hour": {"utilization": 25}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        meter = await _meter.read_claude("test-token", setup_token=True, client=client)
    assert meter.five_hour_pct == 25
    assert meter.weekly_pct is None
    monkeypatch.setattr(_meter, "SETUP_TOKEN_USAGE_SUPPORTED", False)
    with pytest.raises(_meter.MeterError, match="setup-token usage"):
        await _meter.read_claude("test-token", setup_token=True)


@pytest.mark.parametrize(
    "headers, expected",
    [
        (
            {
                "retry-after": "1000",
                "anthropic-ratelimit-unified-5h-utilization": "0.8",
                "anthropic-ratelimit-unified-5h-reset": "2000",
            },
            80,
        ),
        (
            {
                "anthropic-ratelimit-tokens-limit": "100",
                "anthropic-ratelimit-tokens-remaining": "0",
                "anthropic-ratelimit-tokens-reset": "1970-01-01T00:33:20Z",
            },
            None,
        ),
    ],
)
def test_cli_headers_supply_only_known_capacity(headers, expected):
    meter = _meter.from_claude_events(
        [{"type": "result", "response_headers": headers, "api_error_status": 429}],
        "claude setup_token",
        now=1000,
    )
    assert meter is not None
    assert meter.five_hour_pct == expected
    assert meter.weekly_pct is None
    assert meter.limited_until == 2000


def test_cli_limit_without_headers_is_exhausted_with_unknown_utilization():
    meter = _meter.from_claude_events(
        [{"type": "result", "is_error": True, "result": "You've hit your usage limit"}], now=1000
    )
    assert meter is not None
    assert meter.limited_until is not None and meter.limited_until > 1000
    assert meter.five_hour_pct is None
    assert _meter.from_claude_events([{"type": "result", "result": "done"}], now=1000) is None


def test_bench_runs_on_the_production_token_unless_it_names_another():
    from tests.test_bench import profile as bench_profile

    profile = bench_profile()
    assert _bench.bench_profile(profile, "a" * 40).secrets.claude_token == "production_token"
    text = profile.text.replace("[bench]", '[bench]\nclaude_token = "bench_token"', 1)
    bench = _bench.bench_profile(_profile.parse(text), "a" * 40)
    assert bench.secrets.claude_token == "bench_token"


async def test_claude_usage_trailer_includes_rate_limit_observation():

    payload = {"type": "result", "response_headers": {"anthropic-ratelimit-unified-5h-utilization": "0.9"}}
    _, rec = _usage._split_usage(_usage._usage_trailer(json.dumps(payload)))
    assert rec["rate_limits"]["five_hour_pct"] == 90


def test_headless_mounts_only_the_setup_token(monkeypatch):
    from flyte_agent_factory import agent

    monkeypatch.setenv("AGENT_FACTORY_CLAUDE_TOKEN_SECRET", "setup_token")
    monkeypatch.setenv("AGENT_FACTORY_CREDENTIALS_SECRET", "1")
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "")
    mounts = agent._credentials_secret()
    assert [(secret.key, secret.as_env_var) for secret in mounts] == [
        ("setup_token", "CLAUDE_CODE_OAUTH_TOKEN")
    ]


def test_standard_headers_with_no_remaining_capacity_are_exhausted():
    meter = _meter.from_claude_events(
        [
            {
                "type": "result",
                "headers": {
                    "anthropic-ratelimit-tokens-remaining": "0",
                    "anthropic-ratelimit-tokens-reset": "2000",
                },
            }
        ],
        now=1000,
    )
    assert meter is not None
    assert meter.limited_until == 2000
    assert meter.five_hour_pct is None
    assert _meter.exhausted(meter, now=1500)
    assert not _meter.exhausted(meter, now=2001)


async def test_headless_start_does_not_wait_for_remote_control(tmp_path, monkeypatch):

    class Process:
        def __init__(self, *args, **kwargs):
            self._on_line: Any = kwargs["on_line"]

        async def start(self):
            pass

    monkeypatch.setattr(_author, "ClaudeProcess", Process)
    monkeypatch.setattr(_session_config, "RAW_TERMINAL_LOG", tmp_path / "terminal.log")
    proc = await _author._start_session(
        [["claude", "--name", "headless"]], env={}, timeline=_timeline._Timeline()
    )
    assert isinstance(proc, Process)
    cast(Any, proc)._on_line.close()  # the fake Process class has no ClaudeProcess-compatible attribute types


def test_usage_refusal_is_reported_as_a_limit():

    with pytest.raises(RuntimeError, match=_usage.USAGE_LIMIT_MARK):
        _subagent_run._raise_on_cli_error(
            json.dumps({"is_error": True, "result": "Claude AI usage limit reached|1792980000"})
        )


@pytest.mark.real_reset
def test_header_reset_survives_the_child_failure_and_blocks_the_router():

    reset = 4_000_000_000
    with pytest.raises(RuntimeError) as caught:
        _subagent_run._raise_on_cli_error(
            json.dumps(
                {
                    "is_error": True,
                    "api_error_status": 429,
                    "result": "Too many requests",
                    "headers": {
                        "anthropic-ratelimit-unified-status": "rejected",
                        "anthropic-ratelimit-unified-reset": str(reset),
                    },
                }
            )
        )
    assert _usage._reset_at(str(caught.value)) == reset


def test_a_full_five_hour_window_does_not_wait_for_a_healthy_weekly_reset():
    meter = _meter.from_claude_events(
        [
            {
                "api_error_status": 429,
                "headers": {
                    "anthropic-ratelimit-unified-5h-utilization": "1",
                    "anthropic-ratelimit-unified-5h-reset": "2000",
                    "anthropic-ratelimit-unified-7d-utilization": "0.1",
                    "anthropic-ratelimit-unified-7d-reset": "9000",
                },
            }
        ],
        now=1000,
    )
    assert meter is not None
    assert meter.limited_until == 2000


def test_login_deployment_env_has_no_token_key(monkeypatch):
    from flyte_agent_factory import agent

    monkeypatch.delenv("AGENT_FACTORY_CLAUDE_TOKEN_SECRET", raising=False)
    assert agent._token_secret_env() == {}
    assert agent.env.env_vars is not None and agent.subagent_env.env_vars is not None
    assert "AGENT_FACTORY_CLAUDE_TOKEN_SECRET" not in agent.env.env_vars
    assert "AGENT_FACTORY_CLAUDE_TOKEN_SECRET" not in agent.subagent_env.env_vars
    monkeypatch.setenv("AGENT_FACTORY_CLAUDE_TOKEN_SECRET", "setup_token")
    assert agent._token_secret_env() == {"AGENT_FACTORY_CLAUDE_TOKEN_SECRET": "setup_token"}


@pytest.mark.parametrize("supported", [False, True])
async def test_direct_meter_with_token_follows_the_probe_constant(monkeypatch, supported):
    import time

    calls = []

    async def read_claude(login, account, *, setup_token=False):
        calls.append(setup_token)
        return _meter.Meter(account, "oauth-usage", time.time(), 10, time.time() + 3600)

    monkeypatch.setattr(_subagent_run, "_direct_meters", None)
    monkeypatch.setattr(_meter, "read_claude", read_claude)
    monkeypatch.setattr(_meter, "SETUP_TOKEN_USAGE_SUPPORTED", supported)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
    monkeypatch.delenv(_session_config.CODEX_TOKEN_ENV_VAR, raising=False)
    found = await _subagent_run._read_direct_meters()
    if supported:
        assert calls == [True] and found["claude"] is not None and found["claude"].five_hour_pct == 10
    else:
        assert calls == [] and "claude" not in found, "an unconfirmed endpoint is unknown, never read"


def test_an_ordinary_error_mentioning_quota_is_not_a_usage_limit():

    with pytest.raises(RuntimeError, match="sub-agent reported an error") as caught:
        _subagent_run._raise_on_cli_error(json.dumps({"is_error": True, "result": "disk quota exceeded"}))
    assert _usage.USAGE_LIMIT_MARK not in str(caught.value)


def test_bench_meter_without_a_bench_table_reads_the_token(monkeypatch):
    from flyte_agent_factory import _bench_flow

    overrides = []

    class Reader:
        def override(self, **kwargs):
            overrides.append(kwargs)
            return self

        async def aio(self, account, poll):
            return json.dumps(_meter.Meter(account, "fake", 1).to_dict())

    monkeypatch.setattr(_bench_flow, "PROFILE", token_profile())
    assert _bench_flow.PROFILE.bench is None
    monkeypatch.setattr(_bench_flow, "read_login_meter", Reader())
    import asyncio

    meter = asyncio.run(_bench_flow._read_meter("bench_token"))
    assert meter.account == "claude bench_token"
    (secret,) = overrides[0]["secrets"]
    assert (secret.key, secret.as_env_var) == ("bench_token", "CLAUDE_CODE_OAUTH_TOKEN")


@pytest.mark.parametrize("retry_after, expected", [("60", 1060), (None, 1300)])
def test_healthy_window_reset_does_not_extend_a_refusal(retry_after, expected):
    headers = {
        "anthropic-ratelimit-unified-7d-utilization": "0.1",
        "anthropic-ratelimit-unified-7d-reset": "9000",
    }
    if retry_after is not None:
        headers["retry-after"] = retry_after
    meter = _meter.from_claude_events([{"api_error_status": 429, "headers": headers}], now=1000)
    assert meter is not None
    assert meter.limited_until == expected


@pytest.mark.parametrize("utilization", [None, "1", "1.1"])
def test_rejected_or_exhausted_window_without_error_is_a_refusal(utilization):
    headers = {"anthropic-ratelimit-unified-5h-reset": "2000"}
    if utilization is None:
        headers["anthropic-ratelimit-unified-5h-status"] = "rejected"
    else:
        headers["anthropic-ratelimit-unified-5h-utilization"] = utilization
    meter = _meter.from_claude_events([{"headers": headers}], now=1000)
    assert meter is not None
    assert meter.limited_until == 2000


@pytest.mark.parametrize("busy_first", [False, True])
def test_shared_token_refusal_blocks_every_slot(monkeypatch, busy_first):
    from types import SimpleNamespace

    from flyte_agent_factory import _factory_base, _planner, _planner_facts

    profile = token_profile()
    first = profile.slots[0]
    meter = _meter.Meter("claude setup_token", "cli-headers", 1000, limited_until=2000)
    monkeypatch.setattr(_factory_base, "cap", lambda: len(profile.slots))
    live = [SimpleNamespace(task_name=first.task)] if busy_first else []
    monkeypatch.setattr(_factory_base, "_task_name", lambda run: run.task_name)
    capacity = _planner_facts.free_capacity(profile, cast(list[Run], live), {first.task: [meter]})
    claude = [slot for slot in capacity if slot.engine == "claude"]
    assert claude
    assert all(slot.meter is not None and slot.meter.limited_until == 2000 for slot in claude)
    assert _planner._choose_capacity(claude, now=1000)[0] is None


def test_rate_limit_configuration_error_is_not_a_refusal():

    payload = {"is_error": True, "result": "Failed to read rate limit configuration: permission denied"}
    assert _meter.from_claude_events([payload], now=1000) is None
    with pytest.raises(RuntimeError, match="sub-agent reported an error") as caught:
        _subagent_run._raise_on_cli_error(json.dumps(payload))
    assert _usage.USAGE_LIMIT_MARK not in str(caught.value)


def test_status_code_429_is_a_usage_refusal():

    with pytest.raises(RuntimeError, match=_usage.USAGE_LIMIT_MARK):
        _subagent_run._raise_on_cli_error(
            json.dumps({"is_error": True, "status_code": 429, "result": "Too many requests"})
        )


def test_legacy_admission_shares_busy_slot_token_refusal(monkeypatch):
    from flyte_agent_factory import _factory_base, _factory_meters

    profile = token_profile()
    # A Codex access token is a second account the slots share.
    profile = replace(profile, secrets=replace(profile.secrets, codex_token="codex_tok_x"))
    first, second = profile.slots[:2]
    reports = {
        # The slots can also run on the shared Codex account, which a free slot may use while the token is
        # limited (QA finding 4); it refused too here, so the token's refusal is what decides.
        first.task: [
            _meter.Meter("claude setup_token", "cli-headers", 1000, limited_until=2000),
            _meter.Meter(f"codex {profile.secrets.codex_token}", "app-server", 1000, limited_until=2000),
        ],
        second.task: [_meter.Meter("claude setup_token", "cli-headers", 1100)],
    }
    monkeypatch.setattr(_factory_base, "PROFILE", profile)
    monkeypatch.setattr(_factory_base, "SLOTS", profile.tasks)
    monkeypatch.setattr(_factory_meters, "slot_meters", lambda slots: {s: reports.get(s, []) for s in slots})
    monkeypatch.setattr(Run, "listall", lambda **kwargs: [])
    headroom = _factory_meters.slot_headroom([second.task], now=1100)
    assert (
        _factory_meters.free_slot(
            [s for s in profile.tasks if s != second.task], task_name=lambda s: s, headroom=headroom
        )
        is None
    )


def test_admission_ignores_meters_for_a_different_credential(monkeypatch):
    from flyte_agent_factory import _factory_base, _factory_meters, _planner_facts

    profile = token_profile()
    first = profile.slots[0]
    reports = {first.task: [_meter.Meter("claude other_login", "cli-headers", 1000, limited_until=2000)]}
    monkeypatch.setattr(_factory_base, "PROFILE", profile)
    monkeypatch.setattr(_factory_base, "SLOTS", profile.tasks)
    monkeypatch.setattr(_factory_base, "cap", lambda: len(profile.slots))
    monkeypatch.setattr(_factory_meters, "slot_meters", lambda slots: {s: reports.get(s, []) for s in slots})
    monkeypatch.setattr(Run, "listall", lambda **kwargs: [])
    capacity = _planner_facts.free_capacity(profile, [], reports)
    assert all(c.meter is None for c in capacity if c.engine == "claude")
    headroom = _factory_meters.slot_headroom([first.task], now=1000)
    assert headroom[first.task] is None
    assert _factory_meters.free_slot([], preferred=first.task, headroom=headroom) == first.task


@pytest.mark.real_reset
@pytest.mark.parametrize("only_limit", [False, True])
def test_rejected_header_error_carries_usage_limit_and_reset(only_limit):

    reset = 4_000_000_000
    payload = {
        "is_error": True,
        "result": "Request refused",
        "headers": {
            "anthropic-ratelimit-unified-5h-status": "rejected",
            "anthropic-ratelimit-unified-5h-reset": str(reset),
        },
    }
    with pytest.raises(RuntimeError, match=_usage.USAGE_LIMIT_MARK) as caught:
        _subagent_run._raise_on_cli_error(json.dumps(payload), only_limit=only_limit)
    assert _usage._reset_at(str(caught.value)) == reset


@pytest.mark.real_reset
async def test_headless_session_refusal_is_recorded_for_the_token_account(tmp_path, monkeypatch):
    from tests._wait import wait_for
    from tests.test_limit_resume import WALL0, _clock, start
    from tests.test_nudge import _Proc

    token = "sk-ant-oat01-secret"
    account = _meter.token_account("setup_token", token)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", token)
    monkeypatch.setenv("AGENT_FACTORY_CLAUDE_TOKEN_SECRET", "setup_token")
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "")
    monkeypatch.setattr(_session_config, "_routes", [])
    monkeypatch.setattr(_session_config, "POLL_EVERY_S", 0.0)
    monkeypatch.setattr(_supervise_support, "_in_flight_children", lambda: (0, ""))
    monkeypatch.setattr(_session_config, "NUDGE_AFTER_S", 1e12)
    store = _meter.MeterStore(tmp_path / "meters")
    monkeypatch.setattr(_usage, "_meters", store)
    now = _clock(monkeypatch, WALL0)
    proc = _Proc()
    proc.output_count = 0
    _bridge, tl, stop, pump = await start(proc)
    await proc.submit("work")
    now["t"] += 33
    proc.limit_hit_at = now["t"]
    proc.limit_text = "resets 8:40pm (UTC)"
    proc.last_output = now["t"]
    await wait_for(lambda: store.readings)
    stop.set()
    await pump

    meter = store.readings[account]
    assert meter.limited_until and meter.limited_until > WALL0
    assert account in tl._meter_data
    assert tl._meter_data[account]["limited_until"] == meter.limited_until
    assert _meter.exhausted(meter, WALL0 + 60)
    assert _usage._route("delegate", None).deferred


def _token_router(monkeypatch, tmp_path, *, codex=False):
    from flyte_agent_factory import agent

    monkeypatch.setenv("AGENT_FACTORY_CLAUDE_TOKEN_SECRET", "setup_token")
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "codex_token" if codex else "")
    monkeypatch.setattr(_session_config, "_routes", [])
    store = _meter.MeterStore(tmp_path / "meters")
    monkeypatch.setattr(_usage, "_meters", store)
    return agent, store


def test_route_ignores_an_exhausted_meter_of_another_credential(monkeypatch, tmp_path):
    import time

    agent, store = _token_router(monkeypatch, tmp_path)
    now = time.time()
    store.update(_meter.Meter("claude other_login", "oauth-usage", now, limited_until=now + 3600))
    decision = _usage._route("delegate", None)
    assert decision.engine == "claude"
    assert not decision.deferred
    assert not _usage._engine_exhausted("claude")


def test_route_blocks_on_the_token_meter_even_after_a_reload(monkeypatch, tmp_path):
    import time

    agent, store = _token_router(monkeypatch, tmp_path)
    now = time.time()
    store.update(_meter.Meter("claude setup_token", "limit-text", now, limited_until=now + 3600))
    store.update(_meter.Meter("claude other_login", "oauth-usage", now + 1))  # read last, healthy
    monkeypatch.setattr(_usage, "_meters", _meter.MeterStore(tmp_path / "meters"))
    assert _usage._engine_exhausted("claude")
    assert _usage._route("delegate", None).deferred


def test_child_refusal_marks_the_token_account_not_another_credentials_meter(monkeypatch, tmp_path):
    import time

    agent, store = _token_router(monkeypatch, tmp_path)
    now = time.time()
    store.update(_meter.Meter("claude other_login", "oauth-usage", now))
    _usage._note_engine_limit("claude", "usage limit, resets 8:40pm (UTC)")
    assert store.readings["claude other_login"].limited_until is None
    assert store.readings["claude setup_token"].limited_until
    assert _usage._engine_exhausted("claude")


async def test_headless_resume_falls_back_when_there_is_no_conversation(tmp_path, monkeypatch):
    import stat
    import sys

    fake = tmp_path / "claude"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import sys, time\n"
        "if '--continue' in sys.argv:\n"
        "    print('No conversation found to continue', flush=True)\n"
        "    sys.exit(1)\n"
        "print('fresh session', flush=True)\n"
        "time.sleep(60)\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(_session_config, "RAW_TERMINAL_LOG", tmp_path / "terminal.log")
    monkeypatch.setattr(_session_config, "WORKSPACE", tmp_path)
    timeline = _timeline._Timeline()
    candidates = _author.session_argv_candidates([str(fake), "--name", "headless"], resume=True)
    proc = await _author._start_session(candidates, env=dict(__import__("os").environ), timeline=timeline)
    try:
        assert "--continue" not in proc._argv
        assert any("starting a fresh session" in text for _, _, text in timeline._rows)
    finally:
        await proc.stop(grace=1.0)


@pytest.mark.parametrize(
    "text",
    [
        "Error: GitHub API rate limit exceeded for user",
        "Could not finish: the npm registry usage limit was reached",
    ],
)
def test_a_third_party_limit_in_the_result_text_is_an_ordinary_error(text):

    payload = json.dumps({"is_error": True, "result": text})
    with pytest.raises(RuntimeError) as raised:
        _subagent_run._raise_on_cli_error(payload)
    assert not str(raised.value).startswith(_usage.USAGE_LIMIT_MARK)
    assert _meter.from_claude_events([json.loads(payload)], now=1000) is None
