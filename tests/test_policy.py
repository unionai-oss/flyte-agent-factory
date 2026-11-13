"""Model and effort policy: one table, honoured by every call site."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import pytest

from flyte_agent_factory import (
    _policy,
    _review,
    _reviewer,
    _router,
    _session_config,
    _timeline,
    _usage,
    agent,
)

SRC = Path(agent.__file__).parent


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for role in _policy.DEFAULTS:
        monkeypatch.delenv(f"AGENT_FACTORY_{role.upper()}_MODEL", raising=False)
        monkeypatch.delenv(f"AGENT_FACTORY_{role.upper()}_EFFORT", raising=False)


def test_judges_are_stronger_than_the_author_by_default():
    author, judge = _policy.resolve("author"), _policy.resolve("judge")
    assert judge.model == "opus" and judge.effort == "high"
    assert author.model == "sonnet"


def test_judges_grade_at_constant_effort_and_the_arbiter_thinks_hardest():
    """Policy change (research + taskgraph-07): rising reviewer effort widened the search every
    round, so findings never converged. Judges stay at one effort against a frozen contract."""
    assert {_policy.resolve("judge", r).effort for r in range(1, 9)} == {"high"}
    assert _policy.ARBITER_EFFORT == "xhigh"


def test_env_overrides_win_including_on_the_final_round(monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_JUDGE_MODEL", "claude-opus-5-5")
    monkeypatch.setenv("AGENT_FACTORY_JUDGE_EFFORT", "max")
    assert _policy.resolve("judge", 3) == _policy.Setting("claude-opus-5-5", "max")


def test_bad_effort_and_unknown_role_fail_loudly(monkeypatch):
    with pytest.raises(KeyError):
        _policy.resolve("jduge")
    monkeypatch.setenv("AGENT_FACTORY_JUDGE_EFFORT", "extreme")
    with pytest.raises(ValueError, match="extreme"):
        _policy.resolve("judge")


def test_argv_omits_effort_when_unset():
    assert _policy.argv(_policy.Setting("sonnet", "")) == ["--model", "sonnet"]
    assert _policy.argv(_policy.Setting("opus", "high")) == ["--model", "opus", "--effort", "high"]


def test_efforts_match_the_cli():
    """`claude --help` lists exactly these; a drifted list means flags the CLI rejects."""
    assert _policy.EFFORTS == ("low", "medium", "high", "xhigh", "max")


def test_no_model_literal_outside_the_policy():
    """Drift guard: a hardcoded model in a call site is how judges ended up on the author's model."""
    offenders = []
    for path in SRC.glob("*.py"):
        if path.name == "_policy.py":
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"""model\s*[=:][^=\n]*["'](sonnet|opus|haiku|fable|claude-[a-z]+-\d)""", line):
                offenders.append(f"{path.name}:{n}: {line.strip()}")
    assert not offenders, offenders


@pytest.mark.asyncio
async def test_judges_are_launched_with_the_policy_setting(monkeypatch):
    seen = []

    async def reviewer(**kw):
        seen.append((kw["model"], kw["effort"]))
        return "ok\nVERDICT: PASS", None

    monkeypatch.setattr(_reviewer, "_call_reviewer", reviewer)
    for round_ in (1, 3):
        await _review._one_judge(
            "craft", "x", cast(Any, object()), "brief", "", round_, _timeline._Timeline()
        )
    assert seen == [("opus", "high"), ("opus", "high")], "constant across rounds"


def test_resources_rise_with_the_round_where_they_fix_things():
    """User: increasing resources each round. Now on the planner, not the judges, whose rising
    effort kept moving the target. The user approved this research recommendation on 2026-10-19
    ("yes do your recommendation for the big tasks stuff"), which proposed constant judge effort."""
    planner = [_policy.resolve("planner", r) for r in range(1, 6)]
    assert {p.model for p in planner} == {"fable"}
    assert [p.effort for p in planner] == ["medium", "high", "high", "xhigh", "xhigh"]


def test_fable_falls_back_to_opus_at_the_same_effort():
    assert _policy.fallback(_policy.Setting("fable", "xhigh")) == _policy.Setting("opus", "xhigh")
    assert _policy.fallback(_policy.Setting("opus", "high")) is None


@pytest.mark.parametrize(
    "role,effort", [("delegate", "medium"), ("planner", "high"), ("correctness", "high")]
)
def test_codex_defaults(role, effort):
    assert _policy.resolve_codex(role) == _policy.Setting("", effort)


def test_codex_overrides_and_fallback(monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_API_REVIEW_CODEX_MODEL", "gpt-example")
    monkeypatch.setenv("AGENT_FACTORY_API_REVIEW_CODEX_EFFORT", "minimal")
    assert _policy.resolve_codex("api-review", claude_effort="max") == _policy.Setting(  # gitleaks:allow
        "gpt-example", "minimal"
    )
    monkeypatch.delenv("AGENT_FACTORY_API_REVIEW_CODEX_EFFORT")
    assert _policy.resolve_codex("api-review", claude_effort="max").effort == "xhigh"  # gitleaks:allow
    messages = []
    monkeypatch.setenv("AGENT_FACTORY_API_REVIEW_CODEX_EFFORT", "bad")
    monkeypatch.setenv("AGENT_FACTORY_API_REVIEW_CODEX_MODEL", "-bad")
    assert _policy.resolve_codex("api-review", warn=lambda *args: messages.append(args)) == _policy.Setting(
        "", "high"
    )
    assert len(messages) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True])
async def test_codex_call_policy_and_timeline(monkeypatch, fallback):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "configured")
    monkeypatch.setenv("AGENT_FACTORY_PLANNER_ENGINE", "claude" if fallback else "codex")
    monkeypatch.setenv("AGENT_FACTORY_PLANNER_CODEX_MODEL", "gpt-example")
    calls, rows = [], []

    async def raw(**kwargs):
        calls.append(kwargs)
        if fallback and len(calls) == 1:
            raise RuntimeError(_usage.USAGE_LIMIT_MARK)
        return "answer", None

    class Timeline:
        def row(self, *args):
            rows.append(args)

    monkeypatch.setattr(_reviewer, "_call_reviewer_raw", raw)
    await _reviewer._call_reviewer(
        purpose="planner", model="opus", effort="max", timeline=cast(Any, Timeline())
    )
    assert calls[-1]["model"] == "gpt-example"
    assert calls[-1]["effort"] == ("xhigh" if fallback else "high")
    assert "codex gpt-example/" in rows[-1][1]


def test_codex_usage_metadata_survives_reload(tmp_path, monkeypatch):
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(_session_config, "_usage", {})
    monkeypatch.setattr(_session_config, "_usage_loaded", False)
    _usage._record_usage("delegate", 1, {"models": ["gpt-example"], "effort": "medium", "engine": "codex"})
    monkeypatch.setattr(_session_config, "_usage", {})
    monkeypatch.setattr(_session_config, "_usage_loaded", False)
    _session_config._load_usage()
    record = _session_config._usage["delegate@1"]
    assert record["models"] == ["gpt-example"]
    assert record["efforts"] == ["medium"]
    assert record["engines"] == ["codex"]


@pytest.mark.parametrize("model", ["sonnet", "opusplan", "sonnet[1m]", "claude-opus-5-5", "Haiku"])
def test_claude_models_are_recognised(model):
    assert _policy.is_claude_model(model)


@pytest.mark.parametrize("model", ["gpt-5.1-codex", "o4-mini", ""])
def test_codex_models_are_not_claude(model):
    assert not _policy.is_claude_model(model)


@pytest.mark.asyncio
async def test_codex_limit_falls_back_to_claude_with_the_claude_setting(monkeypatch):
    """Codex has its own meter: a Codex limit must move the call to Claude, not fail it.

    `DELEGATE_ENGINE` is an explicit pin, hard unless `ENGINE_FALLBACK = "allow"` opts into the move (#125,
    decision-001); `test_a_hard_delegate_pin_defers_instead_of_falling_back` covers the default.
    """
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "configured")
    monkeypatch.setenv("AGENT_FACTORY_DELEGATE_ENGINE", "codex")
    monkeypatch.setenv("AGENT_FACTORY_ENGINE_FALLBACK", "allow")
    calls = []

    async def raw(**kwargs):
        calls.append(kwargs)
        if kwargs["engine"] == "codex":
            raise RuntimeError(f"{_usage.USAGE_LIMIT_MARK} codex: You've hit your usage limit.")
        return "answer", None

    monkeypatch.setattr(_reviewer, "_call_reviewer_raw", raw)
    monkeypatch.setattr(_usage, "_record_usage", lambda *a, **k: None)
    # The router reads module state another test in the same worker may have left set (a meter store holding a
    # Claude refusal, a hard pin); this test is about the fallback, so it starts from none.
    monkeypatch.setattr(_usage, "_meters", None)
    monkeypatch.setattr(_usage, "_hard_pins", False)
    text, _ = await _reviewer._call_reviewer(purpose="delegate", model="opus", effort="high")
    assert text == "answer"
    assert [c["engine"] for c in calls] == ["codex", "claude"]
    assert calls[-1]["model"] == "opus" and calls[-1]["effort"] == "high"


async def test_a_hard_delegate_pin_defers_instead_of_falling_back(monkeypatch):
    """The same Codex limit with `DELEGATE_ENGINE = "codex"` and no `ENGINE_FALLBACK`: wait, never Claude."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "configured")
    monkeypatch.setenv("AGENT_FACTORY_DELEGATE_ENGINE", "codex")
    monkeypatch.delenv("AGENT_FACTORY_ENGINE_FALLBACK", raising=False)
    calls = []

    async def raw(**kwargs):
        calls.append(kwargs)
        # Every engine would refuse; `calls` below is what proves only Codex was ever asked.
        raise RuntimeError(f"{_usage.USAGE_LIMIT_MARK} {kwargs['engine']}: You've hit your usage limit.")

    monkeypatch.setattr(_reviewer, "_call_reviewer_raw", raw)
    monkeypatch.setattr(_usage, "_record_usage", lambda *a, **k: None)
    monkeypatch.setattr(_usage, "_meters", None)
    monkeypatch.setattr(_usage, "_hard_pins", False)
    with pytest.raises(_router.RouterDeferred) as deferred:
        await _reviewer._call_reviewer(purpose="delegate", model="opus", effort="high")
    assert [c["engine"] for c in calls] == ["codex"]
    assert deferred.value.retry_at is not None
