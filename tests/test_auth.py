"""Settings, trust, deploy keys and the session environment."""

import json
import os
import pathlib
import shutil
import stat
import subprocess
from typing import Any, cast

import flyte
import pytest

from flyte_agent_factory import _auth, _instructions, _session_config, _session_setup, _subagent_run
from tests.conftest import reload_agent


@pytest.mark.parametrize(
    ("project", "domain", "missing"),
    [
        (None, None, "PROJECT"),
        ("", None, "PROJECT"),
        ("project", None, "DOMAIN"),
        ("project", "", "DOMAIN"),
        ("project with spaces", "domain with spaces", None),
    ],
)
def test_export_credentials_requires_explicit_scope(project, domain, missing):
    script = pathlib.Path(__file__).resolve().parents[1] / "scripts/export_local_credentials.sh"
    setup = script.read_text().split("\nread_token()", 1)[0]
    env = {k: v for k, v in os.environ.items() if k not in {"FLYTE_PROJECT", "FLYTE_DOMAIN"}}
    for name, value in (("FLYTE_PROJECT", project), ("FLYTE_DOMAIN", domain)):
        if value is not None:
            env[name] = value
    result = subprocess.run(
        ["bash"],
        input=setup + '\nprintf "%s\\0" "${SCOPE[@]}"\n',
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if missing:
        assert result.returncode != 0
        assert result.stderr.strip().endswith(
            f"FLYTE_{missing}: set FLYTE_{missing} (the profile's [backend] {missing.lower()})"
        )
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout.split("\0") == ["-p", project, "-d", domain, ""]


def test_settings_let_the_agent_run_unattended(config_dir):
    settings = json.loads(_auth.write_settings(config_dir).read_text())

    # Held messages would silently break chat-agent direction, and an expiring
    # dialog turns an unanswered question into a wrong answer.
    assert settings["crossSessionInbound"] == "accept"
    assert settings["dialogExpiry"] == "never"
    # AGENTS.md is read natively; there is no @-import fallback to maintain.
    assert "projectInstructions" not in settings
    # The swarm stays in one run.
    assert "Bash(flyte run:*)" in settings["permissions"]["deny"]


def test_trust_is_pre_accepted_and_merges_existing_config(config_dir, tmp_path):
    workspace = tmp_path / "workspace"
    (config_dir).mkdir(parents=True, exist_ok=True)
    (config_dir / ".claude.json").write_text(
        json.dumps({"projects": {"/other": {"keep": 1}}, "mine": "keep"})
    )

    _auth.mark_trusted(config_dir, workspace)
    config = json.loads((config_dir / ".claude.json").read_text())

    assert config["projects"][str(workspace)]["hasTrustDialogAccepted"] is True
    assert config["hasCompletedOnboarding"] is True
    assert config["mine"] == "keep"
    assert config["projects"]["/other"]["keep"] == 1


def test_subagent_environment_carries_only_the_claude_token(monkeypatch):
    """Sub-agents must not inherit the parent's other secrets.

    Secrets are declared per environment, so a sub-agent sharing the parent's
    environment would mount the deploy key and every API token the parent has
    -- while being the least supervised thing in the system.
    """
    monkeypatch.setenv("AGENT_FACTORY_BROWSER", "1")
    monkeypatch.setenv("AGENT_FACTORY_SECRETS", "github_token,npm_token=NPM_TOKEN")
    monkeypatch.setenv("AGENT_FACTORY_CLAUDE_TOKEN_SECRET", "claude_setup_token")

    from flyte_agent_factory import agent as agent_module

    agent_module = reload_agent(agent_module)

    parent = {s.key for s in cast(list[flyte.Secret], agent_module.env.secrets)}
    child = {s.key for s in cast(list[flyte.Secret], agent_module.subagent_env.secrets)}

    assert child == {"claude_setup_token"}
    assert "github_token" in parent, "the parent should still get its declared extras"
    assert not (child - {"claude_setup_token"}), f"sub-agent leaked: {child}"


def test_deploy_key_is_written_privately_and_used_exclusively(tmp_path, monkeypatch):
    """The deploy key is what confines the agent to one repository.

    Other repos are unreachable because it holds no credential for them. That
    only holds if ssh is told to offer this key and nothing else.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_FACTORY_BASE", str(tmp_path))  # the agent's base follows this home
    monkeypatch.setenv("GIT_DEPLOY_KEY", "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END...")

    import os
    import stat

    from flyte_agent_factory import agent as agent_module

    agent_module = reload_agent(agent_module)
    path = _session_setup._install_deploy_key()

    assert path and os.path.isfile(path)
    # ssh refuses a key other users can read.
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    # ssh silently ignores a key with no trailing newline.
    assert pathlib.Path(path).read_text().endswith("\n")

    command = os.environ["GIT_SSH_COMMAND"]
    assert f"-i {path}" in command
    assert "IdentitiesOnly=yes" in command, "ssh could otherwise offer another key"
    assert "StrictHostKeyChecking=accept-new" in command, "a host-key prompt would hang"


def test_no_deploy_key_mounted_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_FACTORY_BASE", str(tmp_path))  # the agent's base follows this home
    monkeypatch.delenv("GIT_DEPLOY_KEY", raising=False)

    from flyte_agent_factory import agent as agent_module

    reload_agent(agent_module)
    assert _session_setup._install_deploy_key() is None


_FACTORY_REPO = "git@example.org:team/factory.git"
_DEFAULT_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\ndefault\n-----END OPENSSH PRIVATE KEY-----\n"
_FACTORY_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nfactory\n-----END OPENSSH PRIVATE KEY-----\n"


@pytest.fixture
def two_deploy_keys(tmp_path, monkeypatch):
    """A slot pod that mounts the default key and the factory repo's, as `[repo_keys]` deploys it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_FACTORY_BASE", str(tmp_path))  # the agent's base follows this home
    monkeypatch.delenv("AGENT_FACTORY_BASE", raising=False)
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.setenv("GIT_DEPLOY_KEY", _DEFAULT_KEY)
    monkeypatch.setenv("GIT_DEPLOY_KEY_FACTORY", _FACTORY_KEY)
    monkeypatch.setenv("AGENT_FACTORY_REPO_KEYS", json.dumps({_FACTORY_REPO: "GIT_DEPLOY_KEY_FACTORY"}))


def _assert_no_key_material_in_the_environment():
    left = [name for name in os.environ if name.startswith("GIT_DEPLOY_KEY")]
    assert not left, f"the session would inherit {left}"


@pytest.mark.usefixtures("two_deploy_keys")
def test_the_factory_repo_gets_its_own_deploy_key_and_keeps_it():
    from flyte_agent_factory import _git

    path = _git.install_deploy_key(_FACTORY_REPO)

    assert path is not None
    assert pathlib.Path(path).read_text() == _FACTORY_KEY
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert f"-i {path}" in os.environ["GIT_SSH_COMMAND"]
    _assert_no_key_material_in_the_environment()
    # `_export_runtime_env` calls again without a repo: the first choice stands.
    command = os.environ["GIT_SSH_COMMAND"]
    assert _git.install_deploy_key() == path
    assert _git.install_deploy_key("git@example.org:team/other.git") == path
    assert pathlib.Path(path).read_text() == _FACTORY_KEY
    assert os.environ["GIT_SSH_COMMAND"] == command


@pytest.mark.usefixtures("two_deploy_keys")
@pytest.mark.parametrize("repo", ["", "git@example.org:team/work-sandbox.git"])
def test_any_other_repo_gets_the_default_deploy_key(repo):
    from flyte_agent_factory import _git

    path = _git.install_deploy_key(repo)

    assert path is not None
    assert pathlib.Path(path).read_text() == _DEFAULT_KEY
    _assert_no_key_material_in_the_environment()
    assert _git.install_deploy_key(_FACTORY_REPO) == path
    assert pathlib.Path(path).read_text() == _DEFAULT_KEY


@pytest.mark.usefixtures("two_deploy_keys")
def test_a_renamed_repos_old_url_gets_the_repos_own_deploy_key(monkeypatch):
    """A project launched on the URL from before a GitHub rename, on a slot deployed after it."""
    from flyte_agent_factory import _git, _profile

    old = "git@example.org:team/old-factory.git"
    sandbox = 'project = "git@github.com:you/my-sandbox.git"\n'
    text = (pathlib.Path(__file__).parents[1] / "profiles" / "example.toml").read_text()
    assert sandbox in text
    text = text.replace(sandbox, f'{sandbox}factory = "{_FACTORY_REPO}"\n', 1).replace(
        "[policy]\n", f'[repo_keys]\nfactory = "k"\n\n[repo_aliases]\nfactory = ["{old}"]\n\n[policy]\n', 1
    )
    profile = _profile.parse(text, "example.toml")
    monkeypatch.setenv(
        "AGENT_FACTORY_REPO_KEYS", profile.deploy_env(profile.slots[0])["AGENT_FACTORY_REPO_KEYS"]
    )

    path = _git.install_deploy_key(old)

    assert path is not None
    assert pathlib.Path(path).read_text() == _FACTORY_KEY
    _assert_no_key_material_in_the_environment()


@pytest.mark.usefixtures("two_deploy_keys")
@pytest.mark.parametrize("repo, emptied", [("", "GIT_DEPLOY_KEY"), (_FACTORY_REPO, "GIT_DEPLOY_KEY_FACTORY")])
def test_an_empty_chosen_key_still_clears_every_key_from_the_environment(monkeypatch, repo, emptied):
    from flyte_agent_factory import _git

    monkeypatch.setenv(emptied, "  ")
    assert _git.install_deploy_key(repo) is None
    _assert_no_key_material_in_the_environment()


@pytest.mark.usefixtures("two_deploy_keys")
def test_a_malformed_repo_key_map_still_clears_every_key_from_the_environment(monkeypatch):
    from flyte_agent_factory import _git

    monkeypatch.setenv("AGENT_FACTORY_REPO_KEYS", "not json")
    with pytest.raises(ValueError):
        _git.install_deploy_key(_FACTORY_REPO)
    _assert_no_key_material_in_the_environment()


def test_the_slot_deploy_carries_the_repo_key_map_into_the_pod(monkeypatch):
    """The map is read at deploy, like AGENT_FACTORY_SECRETS, but the pod needs it at runtime."""

    from flyte_agent_factory import agent as agent_module

    value = json.dumps({_FACTORY_REPO: "GIT_DEPLOY_KEY_FACTORY"})
    monkeypatch.setenv("AGENT_FACTORY_REPO_KEYS", value)
    assert reload_agent(agent_module).env.env_vars["AGENT_FACTORY_REPO_KEYS"] == value
    monkeypatch.delenv("AGENT_FACTORY_REPO_KEYS")
    assert "AGENT_FACTORY_REPO_KEYS" not in reload_agent(agent_module).env.env_vars


@pytest.mark.usefixtures("two_deploy_keys")
def test_a_factory_project_runtime_env_installs_the_factory_key(monkeypatch):
    """`project` exports its runtime env before it clones, so that first install must pick right."""

    monkeypatch.setattr(_session_config, "_project_repo", _FACTORY_REPO)
    _session_setup._export_runtime_env()

    key_path = os.environ["GIT_SSH_COMMAND"].split("-i ", 1)[1].split()[0]
    assert pathlib.Path(key_path).read_text() == _FACTORY_KEY
    _assert_no_key_material_in_the_environment()


@pytest.mark.usefixtures("two_deploy_keys")
def test_clone_installs_the_key_for_the_repo_it_clones(tmp_path, monkeypatch):
    import asyncio

    from flyte_agent_factory import _git

    seen: dict[str, Any] = {}

    class _Done:
        returncode = 0

        async def communicate(self):
            return b"", None

    async def fake_exec(*cmd, **_):
        seen["cmd"] = cmd
        seen["ssh"] = os.environ["GIT_SSH_COMMAND"]
        return _Done()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(_git.clone(_FACTORY_REPO, "main", tmp_path / "work"))

    assert seen["cmd"] == ("git", "clone", "--branch", "main", _FACTORY_REPO, str(tmp_path / "work"))
    key_path = seen["ssh"].split("-i ", 1)[1].split()[0]
    assert pathlib.Path(key_path).read_text() == _FACTORY_KEY


def test_the_session_never_sees_an_anthropic_api_key():
    """An API key makes Claude Code bill the API instead of the subscription.

    The session then comes up as "API Usage Billing" and reports "Not logged in"
    -- while spending real credit on work the subscription already covers. The agent is meant to run on the
    account, not the API.
    """
    from flyte_agent_factory._session_setup import _session_env

    env = _session_env(
        {
            "ANTHROPIC_API_KEY": "sk-ant-secret",
            "ANTHROPIC_AUTH_TOKEN": "also-secret",
            "PATH": "/usr/bin",
            "_UNION_EAGER_API_KEY": "needed-for-child-actions",
        }
    )
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert env["PATH"] == "/usr/bin"
    # Not stripped: the session submits child actions with it.
    assert env["_UNION_EAGER_API_KEY"] == "needed-for-child-actions"


def test_both_sessions_are_started_through_the_filter():
    """The project session and sub-agents both run Claude Code, so both bill."""
    import inspect

    from flyte_agent_factory import agent

    expected = "_session_setup._session_env(_session_setup._export_runtime_env())"
    assert expected in inspect.getsource(agent.project.func)
    assert expected in inspect.getsource(agent.subagent.func)


def test_the_key_is_offered_under_a_name_claude_code_does_not_read(monkeypatch):
    """Stripping it must not make it unreachable for projects that need it.

    The gateway benchmark genuinely calls Anthropic directly. Mounted under a
    different name it is inert until a command is told to use it, with no file
    anywhere for it to leak into.
    """

    monkeypatch.setenv(_session_config._PROJECT_API_KEY_VAR, "sk-ant-whatever")
    memory = _instructions._project_memory(False)
    assert "deliberately not called `ANTHROPIC_API_KEY`" in memory
    assert f"ANTHROPIC_API_KEY=${_session_config._PROJECT_API_KEY_VAR} python bench.py" in memory
    assert "never write it to a file, never commit it" in memory

    monkeypatch.delenv(_session_config._PROJECT_API_KEY_VAR)
    assert _session_config._PROJECT_API_KEY_VAR not in _instructions._project_memory(False)


def test_the_renamed_variable_is_not_one_claude_code_bills_with():
    from flyte_agent_factory._session_setup import _MODEL_BILLING_VARS

    assert _session_config._PROJECT_API_KEY_VAR not in _MODEL_BILLING_VARS


def test_an_is_error_payload_is_a_failure_to_run_not_an_answer():
    import pytest as _pytest

    with _pytest.raises(RuntimeError, match="OAuth session expired"):
        _subagent_run._raise_on_cli_error(
            '{"is_error": true, "result": "Failed to authenticate: OAuth session expired"}'
        )
    _subagent_run._raise_on_cli_error('{"is_error": false, "result": "VERDICT: PASS"}')
    _subagent_run._raise_on_cli_error("not json")


AGENTS_MD_PLUGIN = {"agents-md@builtin": {"options": {"instructionFiles": "claude-md-and-agents-md"}}}


def test_agents_md_is_enabled_as_a_builtin_plugin_option(config_dir):
    """Claude Code 2.1.303 ignores a top-level `instructionFiles`; only this shape is read."""
    settings = json.loads(_auth.write_settings(config_dir).read_text())

    assert settings["pluginConfigs"] == AGENTS_MD_PLUGIN
    assert "instructionFiles" not in settings


def test_plugin_configs_from_extra_are_merged_not_replaced(config_dir):
    extra = {
        "pluginConfigs": {
            "other@builtin": {"options": {"a": 1}},
            "agents-md@builtin": {"options": {"keep": True}},
        }
    }
    settings = json.loads(_auth.write_settings(config_dir, extra=extra).read_text())

    assert settings["pluginConfigs"]["other@builtin"] == {"options": {"a": 1}}
    assert settings["pluginConfigs"]["agents-md@builtin"]["options"] == {
        "instructionFiles": "claude-md-and-agents-md",
        "keep": True,
    }


@pytest.mark.skipif(
    shutil.which("claude") is None or not os.environ.get("AGENT_FACTORY_LIVE_TESTS"),
    reason="live, billed model call: set AGENT_FACTORY_LIVE_TESTS=1 with the image's `claude` binary",
)
def test_claude_loads_both_claude_md_and_agents_md(config_dir, tmp_path):
    """Opt-in: runs the real binary, which needs working credentials, so it skips without them.

    Manual check when this skips: in a scratch directory holding a CLAUDE.md that says "the
    codeword is PELICAN" and an AGENTS.md that says "the codeword is HERON", run
    `CLAUDE_CONFIG_DIR=<dir written by write_settings> claude -p "quote both codewords"` and confirm
    the answer contains PELICAN and HERON. Project settings are not read for plugin options, which
    is why the setting lives in the user-level settings.
    """
    (tmp_path / "CLAUDE.md").write_text("The first codeword is PELICAN.\n")
    (tmp_path / "AGENTS.md").write_text("The second codeword is HERON.\n")
    settings = _auth.write_settings(config_dir)
    result = subprocess.run(
        [
            "claude",
            "-p",
            "Quote every codeword given in your project instructions.",
            "--settings",
            str(settings),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode:
        pytest.skip(f"claude cannot run here (no credentials?): {result.stderr[-200:]}")
    assert "PELICAN" in result.stdout and "HERON" in result.stdout
