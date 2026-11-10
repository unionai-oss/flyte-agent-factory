"""The standing spend policy and the account headroom the session reads in its CLAUDE.md block."""

import json
from types import SimpleNamespace

from flyte_agent_factory import (
    _dashboard,
    _factory,
    _factory_base,
    _gates,
    _instructions,
    _meter,
    _session_config,
    _timeline,
)

RESET = 1_700_000_000.0  # 2023-11-14 22:13 UTC


def test_the_spend_policy_says_what_needs_no_ask_and_what_does():
    text = _instructions.spend_policy_block()
    assert "Subscription use is not spend" in text
    assert "Prefer the cheapest model that does the job" in text
    assert "Haiku first" in text and "replay" in text and "labelling" in text and "triage" in text
    assert "account reserve" in text and "the factory enforces" in text
    # What the session must ask before doing.
    ask = text.split("Ask first")[1]
    assert "API-key" in ask and "factory-bench" in ask and "profile threshold" in ask
    # A bulk cheap-model job is on the no-ask side of the line.
    assert "API-key" not in text.split("Ask first")[0]


def test_headroom_lists_every_reported_window_and_assumes_none():
    meters = [
        _meter.Meter("claude work", "oauth-usage", 1.0, five_hour_pct=97, five_hour_resets_at=RESET),
        _meter.Meter("codex", "exec-event", 1.0, weekly_pct=40.5, weekly_resets_at=RESET + 3600),
    ]
    text = _instructions.headroom_block(meters)
    assert "claude work: 5-hour window 97% used, resets 2023-11-14 22:13 UTC" in text
    assert "codex: weekly window 40.5% used, resets 2023-11-14 23:13 UTC" in text
    assert "5-hour" not in text.split("codex:")[1]  # codex reported no 5-hour window; none is invented


def test_headroom_without_readings_says_so():
    assert "no usage reading yet" in _instructions.headroom_block([])


def test_the_project_memory_carries_both_blocks():
    memory = _instructions._project_memory(False)
    assert _instructions.spend_policy_block() in memory
    assert _instructions.HEADROOM_BEGIN in memory and _instructions.HEADROOM_END in memory


def test_refresh_rewrites_the_block_only_when_headroom_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(_session_config, "WORKSPACE", tmp_path / "work")
    monkeypatch.setattr(_session_config, "CODEX_HOME_DIR", tmp_path / "codex")
    path = _session_config.WORKSPACE / "CLAUDE.md"
    path.parent.mkdir()
    path.write_text(_gates.apply_managed_block("# Project notes\n", _instructions._project_memory(False)))
    meter = _meter.Meter("claude work", "oauth-usage", 1.0, five_hour_pct=10, five_hour_resets_at=RESET)

    assert _instructions.refresh_headroom([meter]) == [path]
    assert "5-hour window 10% used" in path.read_text()
    assert path.read_text().startswith("# Project notes")
    # A later reading with the same numbers is not a change, whatever its timestamp.
    again = _meter.Meter("claude work", "oauth-usage", 99.0, five_hour_pct=10, five_hour_resets_at=RESET)
    assert _instructions.refresh_headroom([again]) == []
    changed = _meter.Meter("claude work", "oauth-usage", 99.0, five_hour_pct=55, five_hour_resets_at=RESET)
    assert _instructions.refresh_headroom([changed]) == [path]
    text = path.read_text()
    assert "5-hour window 55% used" in text and "10% used" not in text
    assert text.count(_instructions.HEADROOM_BEGIN) == 1
    # The rest of the managed block is untouched, so the harness-only gate still recognises it.
    assert _instructions.spend_policy_block() in text
    assert _gates._harness_only("CLAUDE.md", text.encode(), b"# Project notes\n")


def test_refresh_ignores_a_file_that_is_not_there(tmp_path, monkeypatch):
    monkeypatch.setattr(_session_config, "WORKSPACE", tmp_path / "none")
    monkeypatch.setattr(_session_config, "CODEX_HOME_DIR", tmp_path / "none2")
    assert _instructions.refresh_headroom([]) == []


def test_a_meter_reading_refreshes_the_session_block(tmp_path, monkeypatch):
    monkeypatch.setattr(_session_config, "WORKSPACE", tmp_path)
    monkeypatch.setattr(_session_config, "CODEX_HOME_DIR", tmp_path / "codex")
    path = tmp_path / "CLAUDE.md"
    path.write_text(_gates.apply_managed_block("", _instructions._project_memory(False)))
    timeline = _timeline._Timeline()
    timeline.meter(_meter.Meter("claude w", "oauth-usage", 1.0, weekly_pct=70, weekly_resets_at=RESET))
    assert "claude w: weekly window 70% used" in path.read_text()
    timeline.meter(_meter.Meter("claude w", "oauth-usage", 2.0, weekly_pct=80, weekly_resets_at=RESET))
    assert "weekly window 80% used" in path.read_text() and "70% used" not in path.read_text()


# --- acceptance 3, at the instruction level: the session decides from these lines -------------------


def test_cheap_subscription_calls_need_no_ask_and_api_key_calls_do():
    memory = _instructions._project_memory(False)
    free, ask = memory.split("Ask first")[0], memory.split("Ask first")[1]
    assert "use it freely" in free and "Haiku first" in free and "no ask" in free
    assert "API-key" in ask and "ask-human" in ask


# --- factory.status carries the same goal and verdict -----------------------------------------------


def test_factory_status_reports_goal_and_verdict(monkeypatch):
    data = {
        "schema": 1,
        "goal": "Find out if caching helps",
        "phase": {"kind": "review", "since": 1.0, "round": 1},
        "progress": {"criteria": [{"id": "C1", "text": "x", "status": "FAIL"}]},
        "review": {"round": 1, "verdicts": [{"round": 1, "verdict": "FAIL", "blocking": 1}]},
        "timeline": [],
    }
    run = SimpleNamespace(name="r1", url="https://console.example/r1", pb2=SimpleNamespace(labels={}))
    monkeypatch.setattr(_factory_base, "_labelled_runs", lambda *a, **k: [run])
    monkeypatch.setattr(_factory_base, "_phase", lambda r: next(iter(_factory_base.LIVE)))
    monkeypatch.setattr(_factory_base, "report_html", lambda r: _dashboard.status_script(data))
    result = _factory.read_status("proj")
    assert result["goal"] == "Find out if caching helps"
    assert result["verdict"] == {"word": "bad", "reason": "criterion C1 still fails after review round 1"}
    json.dumps(result)
