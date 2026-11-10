"""`COORDINATOR_ENGINE`: the policy key that selects the engine of the project's coordinator session.

The coordinator is the author session, so the key is the old `AUTHOR_ENGINE` under its canonical name.
The session itself is covered in tests/test_codex_author.py.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from flyte_agent_factory import _factory_policy, _instructions, _names, _profile

EXAMPLE = Path(__file__).resolve().parents[1] / "profiles" / "example.toml"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(_names, "_warned", set())
    monkeypatch.setattr(_profile, "_warned", set())
    for name in (
        "AGENT_FACTORY_COORDINATOR_ENGINE",
        "AGENT_FACTORY_AUTHOR_ENGINE",
    ):
        monkeypatch.delenv(name, raising=False)


def _with_policy(lines: str, slot_lines: str = "") -> str:
    # A codex pin needs the profile to hold a codex token.
    text = EXAMPLE.read_text().replace("[secrets]\n", '[secrets]\ncodex_token = "cx_tok"\n', 1)
    text = text.replace("[policy]\n", f"[policy]\n{lines}", 1)
    if slot_lines:
        text = text.replace(
            'subagent_env = "agent-a-sub"\n', f'subagent_env = "agent-a-sub"\n[slots.policy]\n{slot_lines}', 1
        )
    return text


# --- the profile -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("pin", ["claude", "codex", " Codex "])
def test_the_profile_accepts_a_coordinator_engine(pin):
    profile = _profile.parse(_with_policy(f'COORDINATOR_ENGINE = "{pin}"\n'))
    assert profile.policy["COORDINATOR_ENGINE"] == pin.strip().lower()


def test_an_unknown_coordinator_engine_is_a_profile_error():
    with pytest.raises(_profile.ProfileError, match="COORDINATOR_ENGINE"):
        _profile.parse(_with_policy('COORDINATOR_ENGINE = "gpt"\n'))
    with pytest.raises(_profile.ProfileError, match="COORDINATOR_ENGINE"):
        _profile.parse(_with_policy("", 'COORDINATOR_ENGINE = "gpt"\n'))


def test_author_engine_in_a_profile_is_an_alias_that_warns_naming_the_canonical_key(capsys):
    profile = _profile.parse(_with_policy('AUTHOR_ENGINE = "Codex"\n'))
    assert dict(profile.policy)["COORDINATOR_ENGINE"] == "codex"
    assert "AUTHOR_ENGINE" not in profile.policy
    assert "AUTHOR_ENGINE is deprecated; use COORDINATOR_ENGINE" in capsys.readouterr().err


def test_a_profile_with_both_keys_set_to_different_engines_is_refused():
    with pytest.raises(_profile.ProfileError, match="disagree"):
        _profile.parse(_with_policy('AUTHOR_ENGINE = "claude"\nCOORDINATOR_ENGINE = "codex"\n'))
    both_alike = _profile.parse(_with_policy('AUTHOR_ENGINE = "codex"\nCOORDINATOR_ENGINE = "codex"\n'))
    assert both_alike.policy["COORDINATOR_ENGINE"] == "codex"


def test_a_slots_coordinator_engine_overrides_the_profiles_author_engine():
    profile = _profile.parse(_with_policy('AUTHOR_ENGINE = "claude"\n', 'COORDINATOR_ENGINE = "codex"\n'))
    assert profile.policy["COORDINATOR_ENGINE"] == "claude"
    assert profile.slots[0].policy["COORDINATOR_ENGINE"] == "codex"


def test_a_launch_policy_is_normalised_to_the_canonical_key():
    assert _factory_policy.normalize_policy({"COORDINATOR_ENGINE": " Codex "}) == {
        "AGENT_FACTORY_COORDINATOR_ENGINE": "codex"
    }
    assert _factory_policy.normalize_policy({"AUTHOR_ENGINE": "codex"}) == {
        "AGENT_FACTORY_COORDINATOR_ENGINE": "codex"
    }
    with pytest.raises(ValueError, match="COORDINATOR_ENGINE"):
        _factory_policy.normalize_policy({"COORDINATOR_ENGINE": "gpt"})
    with pytest.raises(ValueError, match="disagree"):
        _factory_policy.normalize_policy({"AUTHOR_ENGINE": "claude", "COORDINATOR_ENGINE": "codex"})


# --- the environment -------------------------------------------------------------------------------------


def test_the_environment_reads_the_canonical_name(monkeypatch, caplog):
    monkeypatch.setenv("AGENT_FACTORY_COORDINATOR_ENGINE", " Codex")
    with caplog.at_level(logging.WARNING):
        assert _names.coordinator_engine() == "codex"
    assert not caplog.records


def test_the_environment_alias_works_and_warns_once_naming_the_canonical_name(monkeypatch, caplog):
    monkeypatch.setenv("AGENT_FACTORY_AUTHOR_ENGINE", "codex")
    with caplog.at_level(logging.WARNING):
        assert _names.coordinator_engine() == "codex"
        assert _names.coordinator_engine() == "codex"
    assert [r.getMessage() for r in caplog.records].count(
        "environment variable AGENT_FACTORY_AUTHOR_ENGINE is deprecated and will be removed; "
        "use AGENT_FACTORY_COORDINATOR_ENGINE"
    ) == 1


def test_the_environment_with_both_names_set_to_different_engines_is_an_error(monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_AUTHOR_ENGINE", "claude")
    monkeypatch.setenv("AGENT_FACTORY_COORDINATOR_ENGINE", "codex")
    with pytest.raises(ValueError, match="disagree"):
        _names.coordinator_engine()
    monkeypatch.setenv("AGENT_FACTORY_AUTHOR_ENGINE", "codex")
    assert _names.coordinator_engine() == "codex"


# --- what a Codex coordinator is told ---------------------------------------------------------------------


@pytest.mark.parametrize("delegation", [True, False])
def test_a_codex_coordinator_is_not_pointed_at_claude_code_subagent_roles(delegation):
    codex = _instructions._project_memory(False, delegation_available=delegation, engine="codex")
    claude = _instructions._project_memory(False, delegation_available=delegation)
    assert "Codex has no subagent roles" in codex
    assert "threads of this process" not in codex and "the roles in `.claude/agents/`" not in codex
    assert ("`delegate --answer" in codex.split("Codex has no subagent roles")[1][:600]) is delegation
    assert "Codex has no subagent roles" not in claude and ".claude/agents/" in claude
