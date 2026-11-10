"""`_names`: the fixed variables the factory owns, and reading a variable by its one name."""

import pytest

from flyte_agent_factory import _names, agent


def test_the_fixed_variables_are_agent_factory_names_and_the_two_groups_are_disjoint():
    assert all(name.startswith(_names.PREFIX) for name in _names.FIXED)
    assert not _names.TOOLING_FIXED & _names.POD_FIXED
    assert _names.FIXED == _names.TOOLING_FIXED | _names.POD_FIXED


def test_getenv_reads_only_the_name_it_is_given(monkeypatch):
    monkeypatch.delenv("AGENT_FACTORY_BASE", raising=False)
    monkeypatch.setenv("AGENT_FACTORY_X", "set")
    assert _names.getenv("AGENT_FACTORY_X") == "set"
    assert _names.getenv("AGENT_FACTORY_BASE", "d") == "d"
    monkeypatch.setenv("AGENT_FACTORY_BASE", "")
    assert _names.getenv("AGENT_FACTORY_BASE", "d") == ""
    assert _names.getenv("AGENT_FACTORY_SECRETS", env={"AGENT_FACTORY_SECRETS": "a=B"}) == "a=B"
    assert _names.getenv("AGENT_FACTORY_SECRETS", "d", env={}) == "d"


@pytest.mark.parametrize(
    ("family", "suffix", "current_suffix"),
    [
        ("FACTORY", "PROFILE", "PROFILE"),
        ("FACTORY", "PROFILE_TOML", "PROFILE_TOML"),
        ("FACTORY", "PROFILES_DIR", "PROFILES_DIR"),
        ("FACTORY", "DEPLOY_REPORT", "DEPLOY_REPORT"),
        ("FACTORY", "LIVE_TESTS", "LIVE_TESTS"),
        ("FACTORY", "AUTHOR_ENGINE", "AUTHOR_ENGINE"),
        ("FACTORY", "JUDGE_MODEL", "JUDGE_MODEL"),
        ("FACTORY", "FUTURE_ROLE_MODEL", "FUTURE_ROLE_MODEL"),
        ("AGENT", "BROWSER", "BROWSER"),
        ("AGENT", "CLAUDE_TOKEN_SECRET", "CLAUDE_TOKEN_SECRET"),
        ("AGENT", "CODEX_SECRET", "CODEX_SECRET"),
        ("AGENT", "CODEX_TOKEN_SECRET", "CODEX_TOKEN_SECRET"),
        ("AGENT", "CREDENTIALS_SECRET", "CREDENTIALS_SECRET"),
        ("AGENT", "ENV_NAME", "ENV_NAME"),
        ("AGENT", "REPO_KEYS", "REPO_KEYS"),
        ("AGENT", "SECRETS", "SECRETS"),
        ("AGENT", "TOKEN_IN_ENV", "TOKEN_IN_ENV"),
        ("SUBAGENT", "ENV_NAME", "SUBAGENT_ENV_NAME"),
        ("BROWSER", "ISOLATED", "BROWSER_ISOLATED"),
        ("BROWSER", "EXIT_NODE", "BROWSER_EXIT_NODE"),
        ("BROWSER", "TAILSCALE_SECRET", "BROWSER_TAILSCALE_SECRET"),
        ("FLYTE", "AGENT_REPO", "AGENT_REPO"),
        ("FLYTE", "BASE", "BASE"),
        ("FLYTE", "BRIDGE_DIR", "BRIDGE_DIR"),
        ("FLYTE", "AUTHOR_ENGINE", "CHOSEN_AUTHOR_ENGINE"),
        ("FLYTE", "ENGINE_SHA", "ENGINE_SHA"),
        ("FLYTE", "STATE_URI", "STATE_URI"),
        ("FLYTE", "WORKSPACE", "WORKSPACE"),
    ],
)
@pytest.mark.parametrize("use_mapping", [False, True], ids=["process", "mapping"])
def test_a_variable_that_is_not_the_given_name_is_not_a_fallback(
    family, suffix, current_suffix, use_mapping, monkeypatch
):
    name = f"AGENT_FACTORY_{current_suffix}"
    old_name = f"CLAUDE_{family}_{suffix}"
    monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(old_name, "obsolete")
    env = {old_name: "obsolete"} if use_mapping else None
    assert _names.getenv(name, "none", env=env) == "none"
    assert _names.getenv(name, env=env) == ""
    for value in ("current", ""):
        monkeypatch.setenv(name, value)
        if env is not None:
            env[name] = value
        assert _names.getenv(name, "none", env=env) == value


def test_the_pod_copy_of_the_shell_policy_forwards_policy_and_tooling_variables_only(monkeypatch):
    forwarded = {
        "AGENT_FACTORY_JUDGE_ENGINE": "codex",  # a policy key
        "AGENT_FACTORY_FUTURE_ROLE_MODEL": "opus",  # a policy key nothing lists
        "AGENT_FACTORY_DEPLOY_REPORT": "/r",
        "AGENT_FACTORY_LIVE_TESTS": "1",
        "AGENT_FACTORY_PROFILES_DIR": "/p",
        "AGENT_FACTORY_BROWSER_IDENTITIES": "github",
    }
    held_back = {name: "x" for name in _names.POD_FIXED} | {
        "AGENT_FACTORY_PROFILE": "a",
        "AGENT_FACTORY_PROFILE_TOML": "b",
        "HOME": "/h",
    }
    for name, value in (forwarded | held_back).items():
        monkeypatch.setenv(name, value)
    assert {k: v for k, v in agent._shell_policy_env().items() if k in forwarded | held_back} == forwarded
