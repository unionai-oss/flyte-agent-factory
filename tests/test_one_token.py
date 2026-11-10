"""One factory, one work token: no bootstrap logins, no Remote Control, bench on the production token."""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import replace

import pytest

from flyte_agent_factory import _author, _bench, _bench_flow, _profile
from tests.conftest import reload_agent

SHA = "0123456789abcdef0123456789abcdef01234567"
TOKEN = "work_claude_token"

BASE = """\
name = "one-token"

[backend]
kind = "union"
endpoint = "union.example.org"
project = "proj"
domain = "development"

[repo]
project = "git@github.com:example-org/sandbox.git"
factory = "git@github.com:example-org/factory.git"

[[slots]]
env = "agent-one"
subagent_env = "sub-one"

[[slots]]
env = "agent-two"
subagent_env = "sub-two"

[secrets]
deploy_key = "sandbox_key"
claude_token = "work_claude_token"
codex_token = "work_codex_token"

[github]
app_id = 123
private_key_secret = "app_key"
"""

BENCH = """
[bench]
max_usd = 100
repo = "git@github.com:example-org/factory-bench.git"
repo_key = "bench_repo_key"
"""

# The shape of the old live profile: slot Claude logins, a Codex login under [secrets], and bench logins.
LEGACY = (
    BASE.replace('env = "agent-one"', 'env = "agent-one"\nclaude_login = "old_login_one"').replace(
        'codex_token = "work_codex_token"', 'codex_login = "old_codex_login"'
    )
    + BENCH
    + 'logins = ["old_bench_login"]\n'
)


def test_a_token_profile_with_no_logins_anywhere_parses_and_the_bench_runs_every_case_on_the_token(
    monkeypatch,
):
    profile = _profile.parse(BASE + BENCH)
    assert profile.bench is not None
    assert profile.bench.claude_token == TOKEN

    monkeypatch.setattr(_bench_flow, "PROFILE", profile)
    seen = []

    async def spot_first(task, encoded):
        case = json.loads(encoded)
        seen.append(case["login"])
        return json.dumps({"case": case["case"], "env": case["env"]})

    monkeypatch.setattr(_bench_flow, "_spot_first", spot_first)
    monkeypatch.setattr(_bench_flow, "_case_live", lambda name: False)
    records = asyncio.run(_bench_flow._run_all([{"case": f"c{i}"} for i in range(3)], ["bench-1"]))
    assert len(records) == 3
    assert seen == [TOKEN] * 3


def test_the_legacy_login_profile_parses_with_a_warning_per_key_and_mounts_no_login(monkeypatch, capsys):
    monkeypatch.setattr(_profile, "_warned", set())
    profile = _profile.parse(LEGACY)
    warned = capsys.readouterr().err
    for key, replacement in (
        ("[[slots]] claude_login", "[secrets] claude_token"),
        ("[bench] logins", "[secrets] claude_token"),
    ):
        lines = [line for line in warned.splitlines() if key in line]
        assert len(lines) == 1 and replacement in lines[0], warned

    logins = {"old_login_one", "old_codex_login", "old_bench_login"}
    assert profile.secrets.claude_token == TOKEN
    assert logins.isdisjoint(profile.meter_logins.values())
    for slot in profile.slots:
        env = profile.deploy_env(slot)
        assert logins.isdisjoint(env.values())
        assert env["AGENT_FACTORY_CLAUDE_TOKEN_SECRET"] == TOKEN
    bench = _bench.bench_profile(profile, SHA)
    assert logins.isdisjoint(bench.deploy_env(bench.slots[0]).values())


def test_a_secrets_codex_login_is_ignored_with_a_warning_on_a_profile_without_slots(monkeypatch, capsys):
    monkeypatch.setattr(_profile, "_warned", set())
    text = BASE.split("[[slots]]")[0] + BASE[BASE.index("[secrets]") :]
    profile = _profile.parse(text.replace('codex_token = "work_codex_token"', 'codex_login = "old_codex"'))
    assert profile.shared and profile.secrets.codex_token == ""
    assert "[secrets] codex_login" in capsys.readouterr().err


def test_a_profile_without_a_claude_token_says_how_to_make_one():
    text = BASE.replace('claude_token = "work_claude_token"\n', "")
    with pytest.raises(_profile.ProfileError, match=r"claude setup-token.*\[secrets\] claude_token"):
        _profile.parse(text)
    with pytest.raises(_profile.ProfileError, match="claude setup-token"):
        _profile.parse(LEGACY.replace('claude_token = "work_claude_token"\n', ""))


def _agent_for(monkeypatch, profile):
    """The agent module as a slot of `profile` imports it: the deploy's variables, then a fresh import."""
    from flyte_agent_factory import agent

    for key in [k for k in __import__("os").environ if k.startswith("AGENT_FACTORY_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("AGENT_FACTORY_PROFILE_TOML", profile.deployment_text())
    monkeypatch.delenv("AGENT_FACTORY_PROFILE", raising=False)
    for key, value in profile.deploy_env(profile.slots[0]).items():
        monkeypatch.setenv(key, value)
    return reload_agent(agent)


def test_every_environment_mounts_the_one_token_and_no_login(monkeypatch):
    from flyte_agent_factory import agent

    profile = _profile.parse(BASE)
    try:
        module = _agent_for(monkeypatch, profile)
        for environment in (module.env, module.subagent_env):
            mounted = {
                s if isinstance(s, str) else s.key: getattr(s, "as_env_var", None)
                for s in environment.secrets
            }
            assert mounted[TOKEN] == "CLAUDE_CODE_OAUTH_TOKEN"
            assert "CLAUDE_CREDENTIALS_JSON" not in mounted.values()
            assert "work_codex_token" in mounted
            assert not {k for k in mounted if "login" in str(k)}
        # The setup environment signs nobody in.
        assert module.setup_env.name == "factory-setup"
        assert not module.setup_env.secrets
        # Only the Codex device-code login is back: it holds no credential until you sign in.
        registered = {name for name in dir(module) if "bootstrap_login" in name}
        assert registered == {"codex_bootstrap_login"}
        assert hasattr(module, "gcp_bootstrap")
        assert not hasattr(module, "bootstrap_login")
    finally:
        monkeypatch.undo()
        reload_agent(agent)


def test_the_bench_environments_mount_the_production_token(monkeypatch):
    profile = _profile.parse(BASE + BENCH)
    bench = _bench.bench_profile(profile, SHA)
    assert [s.env for s in bench.slots] == _bench.bench_envs(SHA, 1)
    assert bench.secrets.claude_token == TOKEN
    assert bench.deploy_env(bench.slots[0])["AGENT_FACTORY_CLAUDE_TOKEN_SECRET"] == TOKEN


def test_a_bench_sharing_the_production_token_is_accepted():
    profile = _profile.parse(BASE + BENCH.replace("[bench]", f'[bench]\nclaude_token = "{TOKEN}"'))
    assert profile.bench is not None and profile.bench.claude_token == TOKEN
    # Nothing in code stops it: the account reserve bounds the usage.
    assert _bench.bench_profile(profile, SHA).secrets.claude_token == TOKEN
    own = _profile.parse(BASE + BENCH.replace("[bench]", '[bench]\nclaude_token = "bench_only_token"'))
    assert own.bench is not None and own.bench.claude_token == "bench_only_token"


def test_a_default_session_launch_never_passes_remote_control():
    from flyte_agent_factory import agent

    argv = _author._author_argv("a-project", "bypassPermissions", "opus", "")
    assert argv[0] == "claude" and "--remote-control" not in argv
    assert "remote_control" not in inspect.signature(agent.project.func).parameters
    source = inspect.getsource(agent)
    assert "--remote-control" not in source
    assert not hasattr(agent, "_use_remote_control")


def test_the_replacement_for_a_shared_slot_is_a_profile_dataclass_without_logins():
    fields = {f for f in _profile.Slot.__dataclass_fields__}
    # A Codex ChatGPT login is a slot's own; there is no Claude login.
    assert fields == {"env", "subagent_env", "policy", "codex_login"}
    assert _profile.SHARED_SLOT.codex_login == ""
    assert replace(_profile.SHARED_SLOT).env == "agent-shared"


# --- a slot's own Codex ChatGPT login (`[[slots]] codex_login`) ----------------------------------------


def _with_slot_login(login: str = "codex_login_two") -> str:
    return BASE.replace('subagent_env = "sub-two"', f'subagent_env = "sub-two"\ncodex_login = "{login}"')


def test_a_slot_codex_login_is_mounted_on_that_slot_only_and_metered():
    profile = _profile.parse(_with_slot_login())
    one, two = profile.slots
    assert two.codex_login == "codex_login_two" and one.codex_login == ""
    assert profile.deploy_env(two)["AGENT_FACTORY_CODEX_TOKEN_SECRET"] == "codex_login_two"
    assert profile.deploy_env(one)["AGENT_FACTORY_CODEX_TOKEN_SECRET"] == "work_codex_token"
    assert profile.meter_logins["codex codex_login_two"] == "codex_login_two"
    assert profile.has_codex


def test_a_codex_login_alone_gives_the_profile_codex_on_its_slot():
    profile = _profile.parse(_with_slot_login().replace('codex_token = "work_codex_token"\n', ""))
    one, two = profile.slots
    assert (
        profile.has_codex and profile.codex_secret(two) == "codex_login_two" and not profile.codex_secret(one)
    )
    assert "AGENT_FACTORY_CODEX_TOKEN_SECRET" not in profile.deploy_env(one)


def test_two_slots_sharing_a_codex_login_are_refused():
    text = _with_slot_login("shared").replace(
        'env = "agent-one"', 'env = "agent-one"\ncodex_login = "shared"'
    )
    with pytest.raises(_profile.ProfileError, match="codex_login must be a different secret on every slot"):
        _profile.parse(text)


def test_a_codex_login_that_is_the_shared_token_is_refused():
    with pytest.raises(_profile.ProfileError, match="codex_login must be a different secret on every slot"):
        _profile.parse(_with_slot_login("work_codex_token"))
