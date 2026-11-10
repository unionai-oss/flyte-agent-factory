"""Usage meters: parsing, secrecy, the supervisor's record and its report header."""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import textwrap
import time
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import flyte
import httpx
import pytest
from flyte.io import Dir

from flyte_agent_factory import (
    _meter,
    _review,
    _reviewer,
    _session_config,
    _session_setup,
    _subagent_run,
    _timeline,
    _usage,
    agent,
)

TOKEN = "sk-ant-oat01-SECRET-ACCESS-TOKEN"
REFRESH = "sk-ant-ort01-SECRET-REFRESH-TOKEN"
CREDENTIALS = json.dumps({"claudeAiOauth": {"accessToken": TOKEN, "refreshToken": REFRESH, "expiresAt": 1}})

# The real response, trimmed (see the brief).
CLAUDE_BODY = {
    "five_hour": {"utilization": 96.0, "resets_at": "2026-10-20T01:50:00.110421+00:00"},
    "seven_day": {"utilization": 15.0, "resets_at": "2026-10-24T18:00:00+00:00"},
    "limits": [
        {"kind": "session", "percent": 96, "resets_at": "2026-10-20T01:50:00.110421+00:00"},
        {"kind": "weekly_all", "percent": 15, "resets_at": "2026-10-24T18:00:00+00:00"},
        {
            "kind": "weekly_scoped",
            "percent": 3,
            "resets_at": "2026-10-24T18:00:00+00:00",
            "scope": {"model": {"display_name": "Fable"}},
        },
    ],
    "extra_usage": {"is_enabled": False, "monthly_limit": None},
    "some_future_key": [1, 2, 3],
}
NOW = 1_792_460_000.0  # 2026-10-20T01:33Z, inside that 5-hour window


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- #
# Claude
# --------------------------------------------------------------------------- #


def test_the_real_response_shape_parses():
    meter = _meter.parse_claude(CLAUDE_BODY, "claude work", NOW)
    assert (meter.five_hour_pct, meter.weekly_pct) == (96.0, 15.0)
    assert meter.five_hour_resets_at == pytest.approx(1_792_461_000.11, abs=1)
    assert meter.weekly_resets_at == 1_792_864_800.0
    assert [(s.scope, s.pct) for s in meter.scoped] == [("Fable", 3.0)]
    assert meter.engine == "claude" and meter.source == "oauth-usage"


@pytest.mark.parametrize("body", [{}, [], None, "x", {"five_hour": None, "limits": "nope"}])
def test_missing_or_odd_bodies_give_empty_meters(body):
    meter = _meter.parse_claude(body, "claude work", NOW)
    assert meter.five_hour_pct is None and meter.weekly_pct is None and meter.scoped == []


def test_limits_fill_in_for_missing_top_level_windows_and_odd_fields_are_ignored():
    body = {
        "limits": [
            {"kind": "session", "percent": 40, "resets_at": "2026-10-20T03:00:00Z"},
            {"kind": "weekly_all", "percent": "lots"},
            {"kind": "weekly_scoped", "percent": 7, "scope": "not-a-dict"},
            "junk",
        ],
        "five_hour": {"utilization": True, "resets_at": 12},
    }
    meter = _meter.parse_claude(body, "claude work", NOW)
    assert meter.weekly_pct is None
    assert meter.five_hour_pct is None  # a bool is not a percentage, and the top level wins
    assert meter.scoped[0].scope == "?" and meter.scoped[0].pct == 7.0


async def test_read_claude_sends_the_documented_request():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=CLAUDE_BODY)

    meter = await _meter.read_claude(CREDENTIALS, "claude work", client=_client(handler))
    (request,) = seen
    assert str(request.url) == "https://api.anthropic.com/api/oauth/usage"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["anthropic-beta"] == "oauth-2025-04-20"
    assert meter.five_hour_pct == 96.0


@pytest.mark.parametrize(
    "handler",
    [
        lambda r: httpx.Response(401, text=f"bad token {TOKEN}"),
        lambda r: httpx.Response(500, text=f"echo {TOKEN}"),
        lambda r: httpx.Response(200, text=f"not json {TOKEN}"),
        lambda r: (_ for _ in ()).throw(httpx.ConnectError(f"boom {TOKEN}", request=r)),
    ],
)
async def test_no_failure_mentions_the_token(handler):
    with pytest.raises(_meter.MeterError) as caught:
        await _meter.read_claude(CREDENTIALS, "claude work", client=_client(handler))
    text = "".join(traceback.format_exception(caught.value)) + repr(caught.value)
    assert TOKEN not in text and REFRESH not in text


@pytest.mark.parametrize(
    "credentials", ["not json", "{}", json.dumps({"claudeAiOauth": {"accessToken": ""}})]
)
async def test_unusable_credentials_are_a_meter_error_that_quotes_nothing(credentials):
    with pytest.raises(_meter.MeterError) as caught:
        await _meter.read_claude(credentials, "claude work")
    assert credentials not in str(caught.value) or credentials == "{}"


# --------------------------------------------------------------------------- #
# Codex
# --------------------------------------------------------------------------- #

# `account/rateLimits/read` (camelCase; the schema from `codex app-server generate-json-schema`).
APP_SERVER_RESULT = {
    "rateLimits": {
        "limitId": "codex",
        "primary": {"usedPercent": 41, "windowDurationMins": 300, "resetsAt": 1_792_470_000},
        "secondary": {"usedPercent": 12, "windowDurationMins": 10080, "resetsAt": 1_792_900_000},
        "planType": "pro",
    },
    "rateLimitsByLimitId": None,
}
# A `codex exec --json` event stream (the CLI's snake_case).
EXEC_EVENTS = [
    {"type": "thread.started", "thread_id": "t"},
    {"type": "token_count", "rate_limits": {"primary": {"used_percent": 10.0, "window_minutes": 300}}},
    {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": None,
            "rate_limits": {
                "primary": {"used_percent": 88.0, "window_minutes": 300, "resets_in_seconds": 1800},
                "secondary": {"used_percent": 35.0, "window_minutes": 10080, "resets_in_seconds": 400000},
            },
        },
    },
    {"type": "turn.completed", "usage": {"input_tokens": 5}},
]


def test_codex_app_server_result_parses():
    meter = _meter.from_app_server(APP_SERVER_RESULT, "codex", NOW)
    assert meter is not None
    assert (meter.five_hour_pct, meter.weekly_pct) == (41.0, 12.0)
    assert (meter.five_hour_resets_at, meter.weekly_resets_at) == (1_792_470_000.0, 1_792_900_000.0)
    assert meter.source == "app-server"


def test_codex_prefers_the_codex_bucket_when_several_are_listed():
    result = {
        "rateLimits": {"primary": {"usedPercent": 1}},
        "rateLimitsByLimitId": {"codex": {"primary": {"usedPercent": 77, "windowDurationMins": 300}}},
    }
    meter = _meter.from_app_server(result, "codex", NOW)
    assert meter is not None and meter.five_hour_pct == 77.0


def test_codex_windows_are_told_apart_by_length_not_position():
    snapshot = {"primary": {"usedPercent": 30, "windowDurationMins": 10080}}
    meter = _meter.parse_codex(snapshot, "codex", NOW, "x")
    assert meter is not None
    assert meter.five_hour_pct is None and meter.weekly_pct == 30.0


def test_the_last_exec_event_with_rate_limits_wins():
    meter = _meter.from_events(EXEC_EVENTS, now=NOW)
    assert meter is not None
    assert (meter.five_hour_pct, meter.weekly_pct) == (88.0, 35.0)
    assert meter.five_hour_resets_at == NOW + 1800
    assert meter.weekly_resets_at == NOW + 400000
    assert meter.source == "exec-event"
    assert _meter.from_events([{"type": "turn.completed"}], now=NOW) is None


def codex_auth(expires: float | None = None) -> str:
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": expires or time.time() + 3600}).encode())
        .decode()
        .rstrip("=")
    )
    return json.dumps({"tokens": {"access_token": f"{TOKEN}.{payload}.signature"}})


FAKE_APP_SERVER = textwrap.dedent(
    """
    import json, os, sys
    mode = sys.argv[1]
    if mode == "silent":
        sys.exit(0)
    assert json.load(open(os.path.join(os.environ["CODEX_HOME"], "auth.json")))["tokens"]
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("method") == "initialize":
            print(json.dumps({"method": "configWarning", "params": {}}), flush=True)
            print(json.dumps({"id": msg["id"], "result": {"userAgent": "x"}}), flush=True)
        elif msg.get("method") == "account/rateLimits/read":
            if mode == "error":
                body = {"id": msg["id"], "error": {"code": -32600, "message": "authentication required"}}
            else:
                body = {"id": msg["id"], "result": json.loads(os.environ["FAKE_RESULT"])}
            print(json.dumps(body), flush=True)
    """
)


def _fake_codex(tmp_path: Path, mode: str) -> tuple[str, str, str]:
    script = tmp_path / "app_server.py"
    script.write_text(FAKE_APP_SERVER)
    return (sys.executable, str(script), mode)


async def test_read_codex_talks_json_rpc_to_the_app_server(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("FAKE_RESULT", json.dumps(APP_SERVER_RESULT))
    auth = codex_auth()
    meter = await _meter.read_codex(auth, command=_fake_codex(tmp_path, "ok"))
    assert (meter.five_hour_pct, meter.weekly_pct) == (41.0, 12.0)
    # The throwaway CODEX_HOME holding the login is gone again.
    assert not any((tmp_path / ".codex-meter").iterdir())


async def test_read_codex_hands_the_app_server_no_refresh_token(tmp_path, monkeypatch):
    """A probe from any pod must not be able to rotate the login's refresh chain (D1)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("FAKE_RESULT", json.dumps(APP_SERVER_RESULT))
    command = _fake_codex(tmp_path, "ok")
    script = tmp_path / "app_server.py"
    seen = tmp_path / "seen.json"
    copy = f'open({str(seen)!r}, "w").write(open(os.path.join(os.environ["CODEX_HOME"], "auth.json")).read())'
    script.write_text(script.read_text().replace("assert json.load(", f"{copy}\nassert json.load("))
    login = json.loads(codex_auth())
    login["tokens"]["refresh_token"] = REFRESH
    await _meter.read_codex(json.dumps(login), command=command)
    probed = json.loads(seen.read_text())
    assert probed["tokens"]["refresh_token"] == ""
    assert probed["tokens"]["access_token"] == login["tokens"]["access_token"]


@pytest.mark.parametrize(
    "mode,message", [("error", "authentication required"), ("silent", "exited before|connection failed")]
)
async def test_read_codex_failures_are_meter_errors(tmp_path, monkeypatch, mode, message):
    monkeypatch.setenv("HOME", str(tmp_path))
    auth = codex_auth()
    with pytest.raises(_meter.MeterError, match=message) as caught:
        await _meter.read_codex(auth, command=_fake_codex(tmp_path, mode), timeout=10)
    assert TOKEN not in str(caught.value)


async def test_read_codex_gives_up_on_a_server_that_never_answers(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    hang = (sys.executable, "-c", "import time; time.sleep(60)")
    with pytest.raises(_meter.MeterError, match="in time"):
        await _meter.read_codex(codex_auth(), command=hang, timeout=0.5)


async def test_read_codex_without_the_binary_is_a_meter_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(_meter.MeterError, match="did not start"):
        await _meter.read_codex(codex_auth(), command=("no-such-codex-binary",))


# --------------------------------------------------------------------------- #
# Headroom
# --------------------------------------------------------------------------- #


def _meter_at(five=None, resets=NOW + 600, week=None, week_resets=NOW + 86400, account="claude work"):
    return _meter.Meter(account, "t", NOW, five, resets, week, week_resets)


def test_headroom_is_what_is_left_of_the_current_window():
    assert _meter.headroom(_meter_at(five=34), NOW) == 66.0
    assert _meter.headroom(_meter_at(five=100), NOW) == 0.0
    assert _meter.headroom(_meter_at(), NOW) is None
    assert _meter.headroom(None, NOW) is None


def test_a_reading_from_a_window_that_has_reset_says_nothing():
    old = _meter_at(five=99, resets=NOW - 1)
    assert _meter.headroom(old, NOW) is None
    assert not _meter.exhausted(old, NOW)


def test_exhausted_only_on_a_known_limit():
    # Changed on purpose (owner: no reserve, inbox 024): this used to be 95%. 95-99.9% is usable.
    # Changed again (owner rule 2, inbox 038): a full 5h or weekly window is a hint, not a limit; this was
    # `test_exhausted_only_at_100_percent_a_known_limit_or_a_full_week` and asserted both were exhausted.
    assert not _meter.exhausted(_meter_at(five=94.9), NOW)
    assert not _meter.exhausted(_meter_at(five=95), NOW)
    assert not _meter.exhausted(_meter_at(five=99.9), NOW)
    assert not _meter.exhausted(_meter_at(five=100), NOW)
    assert not _meter.exhausted(_meter_at(five=10, week=100), NOW)
    assert not _meter.exhausted(_meter_at(five=10, week=100, week_resets=NOW - 5), NOW)
    limited = _meter_at(five=10)
    limited.limited_until = NOW + 60
    assert _meter.exhausted(limited, NOW) and _meter.headroom(limited, NOW) == 0.0
    limited.limited_until = NOW - 60
    assert not _meter.exhausted(limited, NOW)
    assert not _meter.exhausted(None, NOW)


# Owner rule 2: only a refusal (`limited`) is exhausted; this expected `five >= 100` with no `limited` case.
@pytest.mark.parametrize("limited", [False, True])
@pytest.mark.parametrize("five", [94.9, 95, 99.9, 100])
def test_the_meter_and_the_router_share_one_definition_of_exhausted(five, limited):
    from flyte_agent_factory import _router

    meter = _meter_at(five=five)
    meter.limited_until = NOW + 60 if limited else None
    assert _meter.exhausted(meter, NOW) == _router.exhausted(meter, NOW) == limited


# --------------------------------------------------------------------------- #
# The record: history, warnings, rate limit, report header
# --------------------------------------------------------------------------- #


def test_the_report_line_reads_like_the_brief():
    line = _meter.format_line(_meter.parse_claude(CLAUDE_BODY, "claude work", NOW))
    assert line == "claude work: 5h 96% (resets 01:50Z) · week 15% (resets Oct 24)"
    assert _meter.format_line(_meter.Meter("codex", "t", NOW)).startswith("codex: 5h ? (resets ?)")


def test_the_store_keeps_the_latest_and_a_history_that_answers_the_weekly_question(tmp_path):
    store = _meter.MeterStore(tmp_path)
    store.update(_meter.parse_claude(CLAUDE_BODY, "claude work", NOW))
    line = json.loads(store.history.read_text())
    assert line["account"] == "claude work" and line["t"] == NOW
    assert (line["five_hour"], line["weekly"]) == (96.0, 15.0)
    assert line["five_hour_resets_at"] and line["weekly_resets_at"]
    reloaded = _meter.MeterStore(tmp_path)
    assert reloaded.latest["claude"].five_hour_pct == 96.0


def test_the_history_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(_meter, "HISTORY_MAX", 20)
    monkeypatch.setattr(_meter, "HISTORY_KEEP", 10)
    store = _meter.MeterStore(tmp_path)
    for n in range(45):
        store.update(_meter_at(five=n % 100))
    lines = store.history.read_text().splitlines()
    assert 10 <= len(lines) <= 20
    assert json.loads(lines[-1])["five_hour"] == 44


def test_warnings_fire_once_per_window_and_survive_a_restart(tmp_path):
    store = _meter.MeterStore(tmp_path)
    assert store.warnings(_meter_at(five=79.9, week=89.9)) == []
    first = store.warnings(_meter_at(five=80, week=90))
    assert len(first) == 2 and "5h window at 80%" in first[0] and "weekly window at 90%" in first[1]
    assert store.warnings(_meter_at(five=97, week=95)) == []
    # The reset time wobbles by fractions of a second between readings: same window.
    assert store.warnings(_meter_at(five=97, resets=NOW + 600.2)) == []
    assert _meter.MeterStore(tmp_path).warnings(_meter_at(five=97, week=95)) == []
    # The next 5-hour window warns again; the weekly one has not changed.
    again = store.warnings(_meter_at(five=85, resets=NOW + 600 + 5 * 3600, week=95))
    assert len(again) == 1 and "5h" in again[0]


def test_a_limit_noted_from_a_refusal_survives_a_reading_that_does_not_know_it(tmp_path):
    store = _meter.MeterStore(tmp_path, clock=lambda: NOW)
    store.note_limit("codex", NOW + 3600)
    store.update(_meter.Meter("codex", "app-server", NOW, 10, NOW + 100))
    assert store.latest["codex"].limited_until == NOW + 3600
    assert _meter.MeterStore(tmp_path).latest["codex"].limited_until == NOW + 3600


def test_the_report_header_shows_one_line_per_account_and_carries_the_meter(tmp_path):
    timeline = _timeline._Timeline()
    timeline.header("project", "p")
    old = _meter.parse_claude(CLAUDE_BODY, "claude work", NOW)
    timeline.meter(old)
    timeline.meter(_meter.Meter("codex", "app-server", NOW, 5.0, NOW + 60, 6.0, NOW + 90))
    newer = _meter.parse_claude({**CLAUDE_BODY, "five_hour": {"utilization": 97.0}}, "claude work", NOW + 1)
    timeline.meter(newer)
    html = timeline.render()
    assert html.count("claude work: 5h") == 1 and "5h 97%" in html and "5h 96%" not in html
    assert "codex: 5h 5%" in html
    back = {m.engine: m for m in _meter.from_report(html)}
    assert back["claude"].five_hour_pct == 97.0 and back["codex"].weekly_pct == 6.0
    assert _meter.from_report("<p>no meters</p>") == []


def test_a_hostile_account_name_cannot_break_out_of_the_comment():
    timeline = _timeline._Timeline()
    timeline.meter(_meter.Meter("claude a-->b<script>", "t", NOW, 1.0, NOW + 5))
    html = timeline.render()
    assert "<script>" not in html
    assert _meter.from_report(html)[0].account == "claude a-->b<script>"


@pytest.fixture
def supervisor(tmp_path, monkeypatch):
    """A supervisor with the work token and a Codex access token in the environment."""
    config = tmp_path / "claude"
    config.mkdir()
    clock = {"now": NOW}
    store = _meter.MeterStore(tmp_path / "meters", clock=lambda: clock["now"])
    monkeypatch.setattr(_session_config, "CONFIG_DIR", config)
    monkeypatch.setattr(_usage, "_meters", store)
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "codex_token")
    monkeypatch.setenv(_session_config.CODEX_TOKEN_ENV_VAR, "codex-secret-token")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    monkeypatch.setenv("AGENT_FACTORY_CLAUDE_TOKEN_SECRET", "work_token")
    monkeypatch.setattr(
        _session_config, "_token_meter_account", ""
    )  # taken from the token above, then put back
    # The usage endpoint is unconfirmed for setup-tokens; these tests are about the supervisor's reads.
    monkeypatch.setattr(_meter, "SETUP_TOKEN_USAGE_SUPPORTED", True)
    reads = []

    async def claude(credentials, account="claude", **kw):
        reads.append(("claude", credentials, account))
        return _meter.parse_claude(CLAUDE_BODY, account, clock["now"])

    async def codex(auth, account="codex", **kw):
        reads.append(("codex", auth, account))
        return _meter.from_app_server(APP_SERVER_RESULT, account, clock["now"])

    monkeypatch.setattr(_meter, "read_claude", claude)
    monkeypatch.setattr(_meter, "read_codex", codex)
    return SimpleNamespace(store=store, clock=clock, reads=reads, root=tmp_path)


async def test_the_work_tokens_meter_is_read_recorded_and_shown(supervisor):
    timeline = _timeline._Timeline()
    await _usage._read_meters(timeline)
    account = _session_config._claude_account()
    assert supervisor.reads == [("claude", TOKEN, account)]
    html = timeline.render()
    assert f"{account}: 5h 96%" in html
    # Codex's meter comes from its exec events, not from a read of the access token.
    assert set(supervisor.store.latest) == {"claude"}
    rows = [text for _, icon, text in timeline._rows if icon == "⚠️"]
    assert len(rows) == 1 and "5h window at 96%" in rows[0]


async def test_meters_are_never_read_more_often_than_every_five_minutes(supervisor):
    timeline = _timeline._Timeline()
    for _ in range(4):  # the loop, a review round and two delegations in a row
        await _usage._read_meters(timeline)
        supervisor.clock["now"] += 60
    assert len(supervisor.reads) == 1
    supervisor.clock["now"] = NOW + _meter.MIN_INTERVAL_S - 1
    await _usage._read_meters(timeline)
    assert len(supervisor.reads) == 1
    supervisor.clock["now"] = NOW + _meter.MIN_INTERVAL_S
    await _usage._read_meters(timeline)
    assert len(supervisor.reads) == 2


async def test_a_failing_read_is_not_retried_inside_the_interval_and_never_raises(supervisor, monkeypatch):

    async def broken(*a, **k):
        supervisor.reads.append("x")
        raise _meter.MeterError("usage request failed: HTTP 500")

    monkeypatch.setattr(_meter, "read_claude", broken)
    await _usage._read_meters(_timeline._Timeline())
    await _usage._read_meters(_timeline._Timeline())
    assert supervisor.reads.count("x") == 1


async def test_no_token_reaches_a_file_the_log_or_the_report(supervisor, capsys):
    timeline = _timeline._Timeline()
    await _usage._read_meters(timeline)
    await timeline.flush(force=True)
    seen = capsys.readouterr()
    blob = seen.out + seen.err + timeline.render()
    for path in supervisor.root.rglob("*"):
        if path.is_file():
            blob += path.read_text()
    for secret in (TOKEN, "codex-secret-token"):
        assert secret not in blob
    assert "[meter]" in seen.out  # the history line is logged: its other copy


async def test_a_second_reading_in_the_same_window_does_not_warn_again(supervisor):
    timeline = _timeline._Timeline()
    await _usage._read_meters(timeline)
    supervisor.clock["now"] += _meter.MIN_INTERVAL_S
    await _usage._read_meters(timeline)
    assert [i for _, i, _ in timeline._rows].count("⚠️") == 1


def test_a_codex_call_that_reports_rate_limits_updates_the_meter_through_the_usage_trailer(
    tmp_path, monkeypatch
):
    """The event-stream fallback: the child's last observed limits ride home in the usage trailer."""
    import os
    import stat

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "codex"
    event = json.dumps(EXEC_EVENTS[2])
    fake.write_text(
        '#!/bin/sh\n[ "$1" = login ] && exit 0\n'
        'out=""; while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done\n'
        f'echo "VERDICT: PASS" > "$out"\necho \'{event}\'\n'
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv(_session_config.CODEX_TOKEN_ENV_VAR, "codex-access-token")
    work = tmp_path / "w"
    (work / ".claude-flyte").mkdir(parents=True)
    import asyncio

    out = asyncio.run(
        _subagent_run._run_codex(work, tmp_path, "b", answer_only=True, role="", model="", effort="")
    )
    _, rec = _usage._split_usage(out)
    assert rec["rate_limits"]["five_hour_pct"] == 88.0


async def test_a_review_round_reads_the_meters_first(monkeypatch):
    from pathlib import Path

    order = []

    async def read(timeline):
        order.append("meters")

    async def snapshot(_path):
        order.append("snapshot")
        raise OSError("stop here")

    monkeypatch.setattr(_usage, "_read_meters", read)
    monkeypatch.setattr(Dir, "from_local", snapshot)
    with pytest.raises(_reviewer.ReviewUnavailable):
        await _review._evaluate(Path("."), "brief", "", 1, _timeline._Timeline())
    assert order[0] == "meters"


def test_a_delegation_reads_the_meters_before_it_starts():
    import inspect

    # The worker `project` hands to `Delegations` (`_delegate_worker(timeline)`).
    assert "_delegate_worker(timeline)" in inspect.getsource(agent.project.func)
    source = inspect.getsource(_session_setup._delegate_worker)
    worker = source[source.index("async def delegate_worker") :]
    assert worker.index("await _usage._read_meters(timeline)") < worker.index(
        "await _reviewer._call_reviewer("
    )


@pytest.mark.parametrize("expires_in", [-1, 299])
async def test_codex_probe_never_starts_with_expiring_access_token(monkeypatch, expires_in):
    async def forbidden(*args, **kwargs):
        pytest.fail("expired login must not reach an app-server")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises(_meter.MeterError, match="near expiry"):
        await _meter.read_codex(codex_auth(time.time() + expires_in))


async def test_large_app_server_notification_is_supported(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("FAKE_RESULT", json.dumps(APP_SERVER_RESULT))
    command = _fake_codex(tmp_path, "ok")
    script = tmp_path / "app_server.py"
    script.write_text(script.read_text().replace('"params": {}', '"params": {"text": "x" * 100000}'))
    assert (await _meter.read_codex(codex_auth(), command=command)).five_hour_pct == 41


@pytest.mark.parametrize("failure", [ValueError, OverflowError])
async def test_unexpected_reader_error_is_safe(supervisor, monkeypatch, capsys, failure):
    async def broken(*args, **kwargs):
        raise failure(TOKEN)

    monkeypatch.setattr(_meter, "read_claude", broken)
    await _usage._read_meters(_timeline._Timeline())
    captured = capsys.readouterr()
    assert TOKEN not in captured.err and failure.__name__ in captured.err


async def test_bad_reset_display_does_not_stop_meter_reads(supervisor, monkeypatch, capsys):
    async def broken_display(*args, **kwargs):
        return _meter.Meter("claude bad", "oauth-usage", NOW, 90, 1e100)

    monkeypatch.setattr(_meter, "read_claude", broken_display)
    await _usage._read_meters(_timeline._Timeline())
    assert "claude bad" in supervisor.store.readings
    assert "OverflowError" in capsys.readouterr().err


def test_display_rounds_reset_to_nearest_minute():
    from datetime import datetime, timezone

    at = datetime(2026, 10, 20, 6, 49, 59, 540000, tzinfo=timezone.utc).timestamp()
    assert "06:50Z" in _meter.format_line(_meter.Meter("codex", "exec-event", NOW, 81, at))


def test_meter_supervision_uses_optional_failure_list():
    import inspect

    source = inspect.getsource(agent.project.func)
    # The formatter wraps the call, so compare without whitespace.
    call = (
        "_supervise_support._supervised("
        '_usage._meter_loop(timeline, stop), "meter", timeline, stop, fatal=None)'
    )
    assert "".join(call.split()) in "".join(source.split())


async def test_a_bug_in_the_meter_code_is_loud(supervisor, monkeypatch):
    def broken():
        raise ZeroDivisionError

    monkeypatch.setattr(supervisor.store, "due", broken)
    with pytest.raises(ZeroDivisionError):
        await _usage._read_meters(_timeline._Timeline())


def test_a_reading_that_cannot_be_displayed_is_logged_not_raised(tmp_path, capsys):
    store = _meter.MeterStore(tmp_path)
    # A reset time past what datetime can represent: format_line raises OverflowError or ValueError.
    meter = _meter.Meter("claude", "t", NOW, 1.0, 1e20)
    _usage._show_meter(store, meter, _timeline._Timeline())
    assert "[meter]" in capsys.readouterr().err


def test_a_display_bug_is_loud(tmp_path):
    timeline = SimpleNamespace(meter=lambda meter: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        _usage._show_meter(
            _meter.MeterStore(tmp_path),
            _meter.Meter("claude", "t", NOW),
            cast("_timeline._Timeline", timeline),  # only `.meter` is reached
        )


def test_stale_full_window_without_reset_is_unknown_in_both_entrypoints():
    from flyte_agent_factory import _router

    meter = _meter.Meter("claude work", "oauth-usage", NOW - _router.STALE_AFTER_S - 1, 100)
    assert not _meter.exhausted(meter, NOW)
    assert not _router.exhausted(meter, NOW)


def test_login_records_and_failed_attempts_survive_restart(tmp_path):
    store = _meter.MeterStore(tmp_path, clock=lambda: NOW)
    for login in ("claude first", "claude second", "codex shared"):
        store.update(_meter.Meter(login, "probe", NOW - 300, 20, NOW + 1000, 30, NOW + 2000))
    store.note_limit("claude first", NOW + 900)
    store.mark_attempt("claude first")
    store.note_failure("claude first", "auth-unavailable")
    loaded = _meter.MeterStore(tmp_path, clock=lambda: NOW + 1)
    assert set(loaded.readings) == {"claude first", "claude second", "codex shared"}
    assert loaded.readings["claude first"].limited_until == NOW + 900
    assert loaded.readings["claude second"].limited_until is None
    assert loaded.readings["claude first"].five_hour_pct is None
    assert loaded.failures["claude first"] == "auth-unavailable"
    assert not loaded.due("claude first")
    assert loaded.due("claude second")


def test_engine_record_migration_keeps_history(tmp_path):
    meter = _meter.Meter("claude old", "probe", NOW)
    (tmp_path / "latest.json").write_text(json.dumps({"latest": {"claude": meter.to_dict()}, "warned": []}))
    (tmp_path / "history.jsonl").write_text("historical reading\n")
    store = _meter.MeterStore(tmp_path, clock=lambda: NOW)
    assert store.readings["claude old"].account == "claude old"
    store.mark_attempt("claude old")
    assert (tmp_path / "history.jsonl").read_text() == "historical reading\n"
    assert "claude old" in json.loads((tmp_path / "latest.json").read_text())["latest"]


@pytest.mark.parametrize(
    "age,reset,pct",
    [(601, NOW + 1000, 100), (0, NOW - 1, 100), (0, None, 0), (0, NOW + 100, float("nan"))],
)
def test_intake_capacity_requires_a_fresh_valid_window(age, reset, pct):
    meter = _meter.Meter("claude login", "probe", NOW - age, pct, reset)
    assert _meter.capacity_meter(meter, NOW).five_hour_pct is None
    meter.limited_until = NOW + 600
    assert _meter.capacity_meter(meter, NOW).limited_until == NOW + 600


async def test_failed_refresh_is_persisted_without_forgetting_refusal(supervisor, monkeypatch):
    account = _session_config._claude_account()
    supervisor.store.update(_meter.Meter(account, "probe", NOW, 20, NOW + 1000))
    supervisor.store.note_limit(account, NOW + 900)

    async def failed(*args, **kwargs):
        raise _meter.MeterError("expired")

    monkeypatch.setattr(_meter, "read_claude", failed)
    supervisor.clock["now"] += _meter.MIN_INTERVAL_S
    await _usage._read_meters(_timeline._Timeline())
    loaded = _meter.MeterStore(supervisor.store.root, clock=lambda: NOW + 301)
    assert loaded.readings[account].limited_until == NOW + 900
    assert loaded.readings[account].five_hour_pct is None
    assert loaded.failures[account] == "MeterError"
    assert not loaded.due(account)


def test_unbound_legacy_refusal_moves_to_the_mounted_login(tmp_path):
    store = _meter.MeterStore(tmp_path, clock=lambda: NOW)
    store.note_limit("codex", NOW + 900)
    store = _meter.MeterStore(tmp_path, clock=lambda: NOW)
    store.update(_meter.Meter("codex shared", "app-server", NOW, 10, NOW + 1000))
    assert set(store.readings) == {"codex shared"}
    assert store.readings["codex shared"].limited_until == NOW + 900


async def test_failed_codex_call_publishes_observed_limits(tmp_path, monkeypatch):
    import os
    import stat

    executable = tmp_path / "codex"
    executable.write_text(
        f"#!/bin/sh\n[ \"$1\" = login ] && exit 0\necho '{json.dumps(EXEC_EVENTS[2])}'\nexit 1\n"
    )
    executable.chmod(executable.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv(_session_config.CODEX_TOKEN_ENV_VAR, "codex-access-token")
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "shared-login")
    reports = []

    async def report(text, **kwargs):
        reports.append(text)

    monkeypatch.setattr(flyte.report, "log", SimpleNamespace(aio=report))
    work = tmp_path / "work"
    (work / ".claude-flyte").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="codex sub-agent exited"):
        await _subagent_run._run_codex(
            work, tmp_path, "brief", answer_only=True, role="", model="", effort=""
        )
    (meter,) = _meter.from_report("".join(reports))
    assert meter.account == "codex shared-login"
    assert meter.five_hour_pct == 88


def test_a_non_finite_percentage_is_no_reading():
    # QA board #15: `1e400` parses as inf and was published as a meter.
    snapshot = {"primary": {"used_percent": float("inf"), "window_minutes": 300}}
    assert _meter.parse_codex(snapshot, "codex", NOW, "exec-event") is None
