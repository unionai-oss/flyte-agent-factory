"""`JUDGE_ENGINE` is the documented way to put every judge on one engine; a judge's own key still wins."""

import pytest

from flyte_agent_factory import _session_config, _usage


@pytest.fixture(autouse=True)
def codex_deployed(monkeypatch):
    monkeypatch.setattr(_session_config, "_codex_deployed", lambda: True)
    for key in ("JUDGE", "CORRECTNESS", "COMPLETENESS", "CRAFT", "PLANNER"):
        monkeypatch.delenv(f"AGENT_FACTORY_{key}_ENGINE", raising=False)


@pytest.mark.parametrize("judge", ["correctness", "completeness", "craft"])
def test_judge_engine_pins_every_judge(monkeypatch, judge):
    monkeypatch.setenv("AGENT_FACTORY_JUDGE_ENGINE", "claude")
    assert _usage._static_engine(judge) == ("claude", True)


def test_a_judges_own_key_wins_over_judge_engine(monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_JUDGE_ENGINE", "claude")
    monkeypatch.setenv("AGENT_FACTORY_CRAFT_ENGINE", "codex")
    assert _usage._static_engine("craft") == ("codex", True)
    assert _usage._static_engine("correctness") == ("claude", True)


def test_judge_engine_does_not_pin_the_builder_side(monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_JUDGE_ENGINE", "codex")
    assert _usage._static_engine("planner") == ("claude", False)
