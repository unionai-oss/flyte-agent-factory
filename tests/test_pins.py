"""`_pins`: which engine a builder role is pinned to, and whether waiting for it is the rule (#125)."""

from __future__ import annotations

from pathlib import Path

import pytest

from flyte_agent_factory import _pins, _profile, _session_config, _usage

_KEYS = (
    "AGENT_FACTORY_COORDINATOR_ENGINE",
    "AGENT_FACTORY_AUTHOR_ENGINE",
    "AGENT_FACTORY_DELEGATE_ENGINE",
    "AGENT_FACTORY_ENGINE_FALLBACK",
    "AGENT_FACTORY_ADMITTED_ENGINE",
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(_pins, "engine_input", "")
    monkeypatch.setattr(_usage, "_hard_pins", False)
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path / "state")


def test_unpinned_is_empty_and_soft():
    assert _pins.author_pin() == _pins.Pin("", False, "")
    assert _pins.delegate_pin().engine == ""


def test_a_pin_is_hard_unless_fallback_is_allowed(monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_COORDINATOR_ENGINE", "claude")
    assert _pins.author_pin() == _pins.Pin("claude", True, "COORDINATOR_ENGINE")
    monkeypatch.setenv("AGENT_FACTORY_ENGINE_FALLBACK", "allow")
    assert _pins.author_pin() == _pins.Pin("claude", False, "COORDINATOR_ENGINE")
    monkeypatch.setenv("AGENT_FACTORY_ENGINE_FALLBACK", " Allow ")
    assert not _pins.author_pin().hard
    monkeypatch.setenv("AGENT_FACTORY_ENGINE_FALLBACK", "wait")
    assert _pins.author_pin().hard


def test_precedence_is_direction_then_engine_input_then_coordinator_engine(monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_COORDINATOR_ENGINE", "claude")
    assert _pins.author_pin().source == "COORDINATOR_ENGINE"
    monkeypatch.setattr(_pins, "engine_input", "codex")
    assert _pins.author_pin() == _pins.Pin("codex", True, "engine input")
    _pins.set_direction_pin("claude")
    assert _pins.author_pin() == _pins.Pin("claude", True, "direction")


def test_the_alias_is_read_when_only_it_is_set(monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_AUTHOR_ENGINE", "codex")
    assert _pins.author_pin() == _pins.Pin("codex", True, "AUTHOR_ENGINE (deprecated)")


def test_a_direction_is_hard_even_when_fallback_is_allowed_and_survives_a_restart(monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_ENGINE_FALLBACK", "allow")
    assert _pins.direction_pin() == ""
    _pins.set_direction_pin("codex")
    assert _pins.direction_pin() == "codex"  # read back from the file, not held in memory
    assert _pins.author_pin() == _pins.Pin("codex", True, "direction")


def test_a_garbled_direction_file_is_no_direction():
    path = Path(_session_config.STATE_ROOT / "engine-direction.json")
    path.parent.mkdir(parents=True)
    for junk in ("not json", '["claude"]', '{"engine": "gemini"}'):
        path.write_text(junk)
        assert _pins.direction_pin() == ""


def test_the_benchmark_flag_forces_a_pin_hard(monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_COORDINATOR_ENGINE", "claude")
    monkeypatch.setenv("AGENT_FACTORY_ENGINE_FALLBACK", "allow")
    monkeypatch.setattr(_usage, "_hard_pins", True)
    assert _pins.author_pin() == _pins.Pin("claude", True, "benchmark hard_pin")


def test_delegate_pin_ignores_the_coordinator_engine(monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_COORDINATOR_ENGINE", "codex")
    monkeypatch.setattr(_pins, "engine_input", "codex")
    assert _pins.delegate_pin().engine == ""
    monkeypatch.setenv("AGENT_FACTORY_DELEGATE_ENGINE", "Claude")
    assert _pins.delegate_pin() == _pins.Pin("claude", True, "DELEGATE_ENGINE")


def test_the_admitted_engine_is_not_a_pin(monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_ADMITTED_ENGINE", "codex")
    assert _pins.author_pin().engine == "" and _pins.delegate_pin().engine == ""


def test_a_nonsense_engine_is_no_pin(monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_COORDINATOR_ENGINE", "gemini")
    assert _pins.author_pin().engine == ""


_PROFILE = (Path(__file__).parent.parent / "profiles" / "example.toml").read_text()


def _with_fallback(value: str, text: str = _PROFILE) -> str:
    """`text` with `ENGINE_FALLBACK` set in the profile's own `[policy]` table."""
    return text.replace("[policy]\n", f'[policy]\nENGINE_FALLBACK = "{value}"\n', 1)


@pytest.mark.parametrize("value", ["maybe", "true", "ALLOW_ALL"])
def test_engine_fallback_other_than_allow_or_wait_raises_at_load(value):
    with pytest.raises(_profile.ProfileError, match="ENGINE_FALLBACK"):
        _profile.parse(_with_fallback(value))


@pytest.mark.parametrize("value", ["allow", "wait", "Allow", ""])
def test_engine_fallback_allow_and_wait_load(value):
    assert _profile.parse(_with_fallback(value)).policy["ENGINE_FALLBACK"] == value


def test_a_slot_policy_overrides_the_profile_policy_for_the_key():
    """Assumption 4 of the design: the more specific table wins, so a slot can opt in alone."""
    text = _with_fallback("wait").replace(
        'subagent_env = "agent-a-sub"\n',
        'subagent_env = "agent-a-sub"\n[slots.policy]\nENGINE_FALLBACK = "allow"\n',
        1,
    )
    profile = _profile.parse(text)
    slot = next(s for s in profile.slots if s.env == "agent-a")
    assert profile.policy_env_for(slot)["AGENT_FACTORY_ENGINE_FALLBACK"] == "allow"
    assert profile.policy_env_for(None)["AGENT_FACTORY_ENGINE_FALLBACK"] == "wait"
