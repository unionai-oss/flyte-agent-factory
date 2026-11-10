"""Deployment profiles: parsing, selection, and that nothing personal is left in the code."""

from __future__ import annotations

import dataclasses
import importlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from flyte_agent_factory import _gate_base, _gates, _instructions, _names, _profile, _standards, _workspace
from ops import personal_literals
from tests.conftest import TEST_PROFILE, reload_agent

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "tests" / "profiles" / "work.toml"
OTHER = ROOT / "tests" / "profiles" / "other.toml"
EXAMPLE = ROOT / "profiles" / "example.toml"
#: The owner's profiles are git-ignored and move to a private repo: tests that read them skip without.
UNION_WORK = ROOT / "profiles" / "union-work.toml"
GKE_PERSONAL = ROOT / "profiles" / "gke-personal.toml"
needs_private = {
    path.stem: pytest.mark.skipif(not path.exists(), reason=f"{path.name} is private and not present")
    for path in (UNION_WORK, GKE_PERSONAL)
}


def _current() -> _profile.Profile:
    profile = _profile.current()
    assert profile is not None
    return profile


def _mcp(profile: _profile.Profile) -> _profile.Mcp:
    assert profile.mcp is not None
    return profile.mcp


def _ops(name: str):
    spec = importlib.util.spec_from_file_location(f"ops_{name}", ROOT / "ops" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def other_profile(monkeypatch):
    """The suite runs on work.toml; this selects `other` and forgets the modules that read it.

    The modules are put back as they were afterwards: left behind, `_factory` imported under
    `other` would be what every later test finds (or imports) in `sys.modules`.
    """
    monkeypatch.setenv("AGENT_FACTORY_PROFILE", str(OTHER))
    # A pod (or a deploy shell) can carry these; they outrank the profile by design.
    for var in (
        "FLYTE_MCP_PROJECT",
        "FLYTE_MCP_DOMAIN",
        "FLYTE_INTERNAL_PROJECT",
        "FLYTE_INTERNAL_DOMAIN",
        "OPERATE_REPO",
        "OPERATE_CAP",
        "OPERATE_TASK",
    ):
        monkeypatch.delenv(var, raising=False)
    import flyte_agent_factory

    # `_factory_base` reads the profile at import; the other `_factory_*` modules are built on it.
    names = (
        "_factory",
        "_factory_mcp",
        "_factory_base",
        "_factory_meters",
        "_factory_policy",
        "_factory_recreate",
        "_factory_status",
    )
    before = {n: sys.modules.pop(f"flyte_agent_factory.{n}", None) for n in names}
    for name in names:
        monkeypatch.delattr(flyte_agent_factory, name, raising=False)
    yield
    for name, module in before.items():
        sys.modules.pop(f"flyte_agent_factory.{name}", None)
        if hasattr(flyte_agent_factory, name):
            delattr(flyte_agent_factory, name)
        if module is not None:
            sys.modules[f"flyte_agent_factory.{name}"] = module
            setattr(flyte_agent_factory, name, module)


# --- loading ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "example",
        pytest.param("gke-personal", marks=needs_private["gke-personal"]),
        pytest.param("union-work", marks=needs_private["union-work"]),
    ],
)
def test_shipped_profiles_parse(name):
    profile = _profile.load(str(ROOT / "profiles" / f"{name}.toml"))
    assert profile.name == name
    assert profile.slots and 1 <= profile.cap <= len(profile.slots)


def test_example_is_safe_to_share():
    text = EXAMPLE.read_text()
    assert "example" in _profile.parse(text).name
    assert not PERSONAL.search(text) and "unionai.cloud" not in text


def test_the_retired_browser_token_key_is_refused_by_name():
    text = EXAMPLE.read_text().replace("[secrets]\n", '[secrets]\nbrowser_token = "t"\n', 1)
    with pytest.raises(_profile.ProfileError, match=r"unknown key\(s\) in \[secrets\]: browser_token"):
        _profile.parse(text)


def test_the_example_profile_documents_every_optional_secret():
    text = EXAMPLE.read_text()
    for key in _profile.OPTIONAL_SECRETS:
        assert re.search(rf"^(# )?{key} = ", text, re.MULTILINE), f"example.toml lacks [secrets] {key}"


@pytest.mark.parametrize("bad", ["-5", "nan", "inf", "1h"])
def test_a_malformed_park_release_minutes_fails_profile_validation(bad):
    text = EXAMPLE.read_text().replace("[policy]\n", f'[policy]\nPARK_RELEASE_MIN = "{bad}"\n', 1)
    with pytest.raises(_profile.ProfileError, match=r"PARK_RELEASE_MIN"):
        _profile.parse(text, "example.toml")
    slot_text = EXAMPLE.read_text().replace(
        '# [slots.policy]\n# CRAFT_ENGINE = "codex"',
        f'[slots.policy]\nPARK_RELEASE_MIN = "{bad}"\n# CRAFT_ENGINE = "codex"',
        1,
    )
    with pytest.raises(_profile.ProfileError, match=r"PARK_RELEASE_MIN"):
        _profile.parse(slot_text, "example.toml")


def test_an_unknown_author_engine_fails_profile_validation():
    text = EXAMPLE.read_text().replace("[policy]\n", '[policy]\nAUTHOR_ENGINE = "gpt"\n', 1)
    with pytest.raises(_profile.ProfileError, match=r"AUTHOR_ENGINE"):
        _profile.parse(text, "example.toml")
    slot_text = EXAMPLE.read_text().replace(
        '# [slots.policy]\n# CRAFT_ENGINE = "codex"',
        '[slots.policy]\nAUTHOR_ENGINE = "gpt"\n# CRAFT_ENGINE = "codex"',
        1,
    )
    with pytest.raises(_profile.ProfileError, match=r"AUTHOR_ENGINE"):
        _profile.parse(slot_text, "example.toml")


@pytest.mark.parametrize("good", ["30", "0"])
def test_a_valid_park_release_minutes_loads(good):
    text = EXAMPLE.read_text().replace("[policy]\n", f'[policy]\nPARK_RELEASE_MIN = "{good}"\n', 1)
    assert _profile.parse(text, "example.toml").policy["PARK_RELEASE_MIN"] == good


def test_only_example_and_readme_are_tracked_in_profiles():
    got = subprocess.run(
        ["git", "ls-files", "profiles/"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    assert got.stdout.splitlines() == [
        "profiles/README.md",
        "profiles/example-claude-personal.toml",
        "profiles/example-codex-personal.toml",
        "profiles/example.toml",
    ]


def test_local_profiles_are_ignored_but_example_and_fixtures_are_not():
    private = ["profiles/union-work.toml", "profiles/gke-personal.toml", "profiles/local.toml"]
    shareable = [
        "profiles/example.toml",
        "profiles/example-claude-personal.toml",
        "profiles/example-codex-personal.toml",
        "tests/profiles/work.toml",
        "tests/profiles/other.toml",
    ]
    got = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin"],
        cwd=ROOT,
        input="\n".join(private + shareable) + "\n",
        capture_output=True,
        text=True,
        check=True,
    )
    assert got.stdout.splitlines() == private


def test_the_suite_profile_has_the_structure_the_tests_rely_on():
    """The values the suite pins are the fixture's, neutral placeholders with a real deployment's shape."""
    p = _profile.load(str(WORK))
    assert (p.backend.kind, p.backend.project, p.backend.domain) == ("union", "proj-work", "development")
    assert p.backend.endpoint == p.backend.console_host == "union.example.org"
    assert p.backend.signin_host == "signin.example.org"
    assert p.backend.config == "~/.union/example-config.yaml"
    assert p.repo == "git@example.org:team/work-sandbox.git"
    assert p.tasks == ("agent-one.project", "agent-two.project")
    assert [(s.env, s.subagent_env) for s in p.slots] == [("agent-one", "sub-one"), ("agent-two", "sub-two")]
    assert p.cap == 2
    assert p.agent_secrets == (
        "deploy_key_x=GIT_DEPLOY_KEY,gateway_key_x=LLM_GATEWAY_KEY,api_key_x=PROJECT_ANTHROPIC_API_KEY"
    )
    assert p.secrets.claude_token == "claude_token_x"
    assert p.browser == _profile.Browser()
    assert _mcp(p).public_url == ""
    assert _mcp(p).repos == ("project",)
    assert p.console_base == "https://union.example.org/v2/domain/development/project/proj-work"
    assert dict(p.slots[0].policy) == {} and set(p.slots[1].policy.values()) == {"codex"}


@needs_private["union-work"]
def test_the_union_work_profile_still_describes_what_is_deployed():
    """The one test of the owner's profile, while it is in this checkout, against what is deployed.

    The values come from the profile itself, so that this file names none of them (the literal guard): what is
    checked is the deployment's shape.
    """
    p = _profile.load(str(UNION_WORK))
    assert p.backend.project and p.backend.domain == "development"
    assert p.repo.startswith("git@github.com:") and p.repo.endswith(".git")
    assert p.cap == 2
    # Two slots: the second's env and sub-agent env are the first's with `-2`.
    (first, second) = [(s.env, s.subagent_env) for s in p.slots]
    assert first[1] == "claude-subagent" and second == (f"{first[0]}-2", f"{first[1]}-2")
    assert p.policy_env == {}
    assert p.deploy_env(p.slots[0]).keys().isdisjoint(_ROLE_KEYS)
    two = p.deploy_env(p.slots[1])
    assert {k: v for k, v in two.items() if k.startswith("AGENT_FACTORY_")} == dict.fromkeys(
        _ROLE_KEYS, "codex"
    )


def test_profile_is_frozen():
    p = _profile.load(str(WORK))
    with pytest.raises(AttributeError):
        p.cap = 5  # type: ignore[misc]  # the point: a read-only property
    with pytest.raises(TypeError):
        p.policy["X"] = "y"  # type: ignore[index]  # the point: a read-only mapping


#: The roles live on Codex on the second slot, and only there.
_ROLE_KEYS = tuple(
    f"AGENT_FACTORY_{role}_ENGINE"
    for role in ("ARBITER", "COMPLETENESS", "CONTRACT", "CORRECTNESS", "CRAFT", "PLANNER")
)


def _without(text: str, line: str) -> str:
    assert line in text, line
    return text.replace(line, "", 1)


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ('project = "my-agents"\n', "missing key 'project' in [backend]"),
        ('kind = "union"\n', "missing key 'kind' in [backend]"),
        ('domain = "development"\n', "missing key 'domain' in [backend]"),
        ('project = "git@github.com:you/my-sandbox.git"\n', "missing key 'project' in [repo]"),
        (
            'deploy_key = "my_deploy_key"               # required: the single-repo deploy key\n',
            "missing key 'deploy_key' in [secrets]",
        ),
        ('subagent_env = "agent-a-sub"\n', "missing key 'subagent_env' in [slots]"),
        ('name = "example"\n', "missing key 'name'"),
    ],
)
def test_missing_keys_are_named(line, message):
    with pytest.raises(_profile.ProfileError, match=re.escape(message)):
        _profile.parse(_without(EXAMPLE.read_text(), line), "example.toml")


def test_error_names_the_file():
    with pytest.raises(_profile.ProfileError, match="example.toml: not valid TOML"):
        _profile.parse("name = ", "example.toml")


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('kind = "union"', 'kind = "aws"', "kind must be one of"),
        ("cap = 2", "cap = 3", "cap must be an integer from 1 to the number of slots (2)"),
        ("cap = 2", "cap = 0", "cap must be an integer"),
        ('env = "agent-b"', 'env = "agent-a"', "env must be unique"),
        ('project = "my-agents"', 'project = "my-agents"\ntypo = "x"', "unknown key(s) in [backend]: typo"),
        ("[policy]", "[polcy]", "unknown top-level key(s): polcy"),
        ("# AUTHOR_MODEL", "AUTHOR_MODEL = 3\n# AUTHOR_MODEL", "[policy] AUTHOR_MODEL must be a string"),
    ],
)
def test_invalid_values_are_refused(old, new, message):
    text = EXAMPLE.read_text()
    assert old in text, old
    with pytest.raises(_profile.ProfileError, match=re.escape(message)):
        _profile.parse(text.replace(old, new, 1))


def test_slots_are_optional_on_the_one_token_but_not_beside_a_cap():
    """The one token needs no slot per login: slots are left out and `cap` goes with them."""
    text = EXAMPLE.read_text()
    text = text[: text.index("\n[[slots]]")] + text[text.index("\n[secrets]") :]  # the tables, not comments
    with pytest.raises(_profile.ProfileError, match=r"cap needs \[\[slots\]\]"):
        _profile.parse(text)
    assert [s.env for s in _profile.parse(_without(text, "cap = 2\n")).slots] == [
        "agent-shared"
    ]  # one shared env


# --- selection -------------------------------------------------------------------------


def test_selected_by_env_name_or_path(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_FACTORY_PROFILE", "example")  # a name in profiles/
    assert _current().name == "example"
    private = tmp_path / "private.toml"
    private.write_text(OTHER.read_text())
    monkeypatch.setenv("AGENT_FACTORY_PROFILE", str(private))  # a path
    assert _current().name == "other"


def test_selected_by_flag(monkeypatch):
    monkeypatch.delenv("AGENT_FACTORY_PROFILE")
    _profile.select_from_argv(["launch", "brief.md", "--profile", "example"])
    assert _current().name == "example"
    _profile.select_from_argv(["--profile=" + str(OTHER)])
    assert _current().name == "other"
    monkeypatch.delenv("AGENT_FACTORY_PROFILE")


def test_the_flag_is_accepted_after_operates_subcommand(other_profile, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "ops"))
    operate = _ops("operate")
    seen: list[Any] = []
    monkeypatch.setattr(operate, "_init", lambda: None)
    monkeypatch.setattr(operate, "running", seen.append)
    operate.main(["running", "--profile", str(OTHER)])
    operate.main(["--profile", str(OTHER), "running"])
    assert len(seen) == 2


def test_the_flag_reaches_an_ops_tool(monkeypatch):
    # Registered before `--profile` writes os.environ directly, so monkeypatch's teardown puts
    # the suite's profile back (registering after would record and restore `other`).
    monkeypatch.setenv("AGENT_FACTORY_PROFILE", str(TEST_PROFILE))
    monkeypatch.setattr(sys, "argv", ["deploy_work.py", "--profile", str(OTHER), "--dry-run"])
    loaded = set(sys.modules)
    try:
        deploy_work = _ops("deploy_work")
        assert list(deploy_work.SLOTS) == ["agent-a", "agent-b", "agent-c"]
    finally:
        # Modules first imported under `other` would be what later tests find (qa: order-dependent).
        for name in set(sys.modules) - loaded:
            if name.startswith("flyte_agent_factory"):
                del sys.modules[name]


@pytest.fixture
def profiles_dir(monkeypatch, tmp_path):
    """An empty profiles/ with nothing selected: neither variable, and no `.default` of the developer's."""
    monkeypatch.delenv("AGENT_FACTORY_PROFILE")
    monkeypatch.delenv("AGENT_FACTORY_PROFILE_TOML", raising=False)
    monkeypatch.setattr(_profile, "PROFILES_DIR", tmp_path)
    return tmp_path


def test_no_profile_is_a_clear_error(profiles_dir):
    assert _profile.current() is None
    with pytest.raises(_profile.ProfileError, match="set AGENT_FACTORY_PROFILE=<name> or pass --profile"):
        _profile.require()


def test_the_no_profile_error_says_how_to_set_a_local_default(profiles_dir):
    (profiles_dir / "mine.toml").write_text(OTHER.read_text())
    with pytest.raises(_profile.ProfileError) as exc:
        _profile.require()
    assert f"echo <name> > {profiles_dir / '.default'}" in str(exc.value)
    assert "mine" in str(exc.value)  # what could be named


def test_a_default_file_names_the_profile_when_nothing_else_does(profiles_dir):
    """A supervisor deployed before profiles has neither variable: it keeps working from profiles/.default."""
    (profiles_dir / "mine.toml").write_text(OTHER.read_text())
    (profiles_dir / ".default").write_text("mine\n")
    assert _profile.require().name == "other"


def test_the_default_file_ranks_below_the_variable_and_the_pods_text(profiles_dir, monkeypatch):
    (profiles_dir / "mine.toml").write_text(OTHER.read_text())
    (profiles_dir / ".default").write_text("mine")
    monkeypatch.setenv("AGENT_FACTORY_PROFILE_TOML", WORK.read_text())
    assert _current().name == "work"
    monkeypatch.setenv("AGENT_FACTORY_PROFILE", str(EXAMPLE))
    assert _current().name == "example"


def test_a_default_file_naming_nothing_real_says_so(profiles_dir):
    (profiles_dir / ".default").write_text("gone\n")
    with pytest.raises(_profile.ProfileError, match=r"cannot read .*gone.toml.*named by .*\.default"):
        _profile.require()


def test_an_ops_tool_falls_back_to_the_default_file(profiles_dir):
    (profiles_dir / "mine.toml").write_text(OTHER.read_text())
    (profiles_dir / ".default").write_text("mine")
    assert list(_ops("deploy_work").SLOTS) == ["agent-a", "agent-b", "agent-c"]


def test_a_bare_toml_name_is_a_profile_not_a_file_in_the_working_directory(monkeypatch, tmp_path):
    (tmp_path / "example.toml").write_text(OTHER.read_text())
    monkeypatch.chdir(tmp_path)
    assert _profile.load("example.toml").name == "example"  # profiles/example.toml
    assert _profile.load("example").name == "example"
    assert _profile.load("./example.toml").name == "other"  # a separator makes it a path


def test_an_unknown_name_names_the_file(monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_PROFILE", "nope")
    with pytest.raises(_profile.ProfileError, match="profile 'nope': cannot read .*nope.toml"):
        _profile.current()


def test_a_pod_reads_the_profile_from_its_environment(monkeypatch):
    """The code bundle has no profiles/: the text rides in the environment definition."""
    monkeypatch.setenv("AGENT_FACTORY_PROFILE", str(OTHER))
    pod = _profile.pod_env()
    monkeypatch.delenv("AGENT_FACTORY_PROFILE")
    monkeypatch.setenv("AGENT_FACTORY_PROFILE_TOML", pod["AGENT_FACTORY_PROFILE_TOML"])
    assert _profile.current() == _profile.load(str(OTHER))


def test_the_make_helper_prints_one_value(monkeypatch):
    def get(key):
        return subprocess.run(
            [sys.executable, "-m", "flyte_agent_factory._profile", key],
            env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "AGENT_FACTORY_PROFILE": str(OTHER)},
            capture_output=True,
            text=True,
            check=False,
        )

    assert get("project").stdout.strip() == "proj-x"
    assert get("agent_secrets").stdout.strip() == "dk=GIT_DEPLOY_KEY"
    assert get("nope").returncode != 0


def _example_with(old: str, new: str) -> str:
    text = EXAMPLE.read_text()
    assert old in text, old
    return text.replace(old, new, 1)


def test_the_make_helper_prints_nothing_for_an_absent_optional_key():
    text = EXAMPLE.read_text().replace('config = "~/.flyte/example-config.yaml"\n', "")
    profile = _profile.parse(text)
    assert _profile._get(profile, "config") == ""
    assert _profile._get(profile, "name") == "example"


# Direct profile loading is also used by documented deployment commands.


def test_direct_profile_command_has_no_self_login(tmp_path):
    profile = _profile.parse(EXAMPLE.read_text())
    assert _profile._get(profile, "project") == "my-agents"
    with pytest.raises(_profile.ProfileError, match="unknown key 'self_login'"):
        _profile._get(profile, "self_login")


def test_direct_profile_command_names_broken_profile(tmp_path):
    path = tmp_path / "p.toml"
    path.write_text(_example_with('project = "my-agents"', 'project = "my-agents"\ntypo = "x"'))
    got = subprocess.run(
        [sys.executable, "-m", "flyte_agent_factory._profile", "project"],
        cwd=ROOT,
        env={**os.environ, "AGENT_FACTORY_PROFILE": str(path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert got.returncode != 0
    assert "unknown key(s) in [backend]: typo" in got.stderr and "p.toml" in got.stderr


def test_direct_profile_command_names_missing_profile(tmp_path):
    path = tmp_path / "missing.toml"
    got = subprocess.run(
        [sys.executable, "-m", "flyte_agent_factory._profile", "project"],
        cwd=ROOT,
        env={**os.environ, "AGENT_FACTORY_PROFILE": str(path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert got.returncode != 0
    assert f"cannot read {path}: No such file or directory" in got.stderr
    assert not got.stdout
    assert "Traceback" not in got.stderr


# --- the code reads the profile ----------------------------------------------------------


def test_factory_reads_slots_project_repo_and_cap_from_the_profile(other_profile, monkeypatch):
    from flyte_agent_factory import _factory, _factory_base

    assert (_factory.PROJECT, _factory.DOMAIN) == ("proj-x", "staging")
    assert _factory_base.SLOTS == ("agent-a.project", "agent-b.project", "agent-c.project")
    assert _factory.REPO == "git@example.org:team/sandbox.git"
    assert _factory_base.cap() == 2  # the profile's cap, below the slot count
    assert _factory.env.env_vars is not None
    assert _factory.env.env_vars["OPERATE_CAP"] == "2"
    assert _profile.parse(_factory.env.env_vars["AGENT_FACTORY_PROFILE_TOML"]).name == "other"


def test_the_environment_variable_still_overrides_the_profile(other_profile, monkeypatch):
    monkeypatch.setenv("FLYTE_MCP_PROJECT", "elsewhere")
    monkeypatch.setenv("OPERATE_CAP", "1")
    from flyte_agent_factory import _factory, _factory_base

    assert _factory.PROJECT == "elsewhere" and _factory_base.cap() == 1


def test_operate_reads_slots_and_project_from_the_profile(other_profile, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "ops"))
    operate = _ops("operate")
    assert operate.PROJECT == "proj-x" and operate.DOMAIN == "staging"
    assert operate.SLOTS == ("agent-a.project", "agent-b.project", "agent-c.project")
    assert operate.TASK == "agent-a.project"
    assert operate.CAP == 2
    assert operate.REPO == "git@example.org:team/sandbox.git"


def test_factory_mcp_takes_url_and_repos_from_the_profile(other_profile, monkeypatch):
    monkeypatch.delenv("FACTORY_MCP_PUBLIC_URL", raising=False)
    from flyte_agent_factory import _factory_base, _factory_mcp

    assert not _factory_mcp.app.secrets
    assert _factory_mcp.app.allowed_hosts == ["mcp.example.org"]
    assert _factory_mcp.app.allowed_origins == ["https://mcp.example.org"]
    assert _mcp(_factory_base.PROFILE).repos == ("project", "factory")
    assert _factory_mcp.app.env_vars is not None
    assert _factory_mcp.app.env_vars["FLYTE_MCP_PROJECT"] == "proj-x"


def test_deploy_work_builds_each_slots_recipe_from_the_profile(monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_PROFILE", str(OTHER))
    deploy_work = _ops("deploy_work")
    assert deploy_work.ALLOWED == {
        "agent-a",
        "agent-b",
        "agent-c",
        "sub-a",
        "sub-b",
        "sub-c",
        "factory-setup",
    }
    env = deploy_work.recipe_env({"AGENT_FACTORY_SECRETS": "hostile=X"}, slot="agent-b")
    deploy_work.check_recipe(env)
    assert env["AGENT_FACTORY_ENV_NAME"] == "agent-b"
    assert env["AGENT_FACTORY_CLAUDE_TOKEN_SECRET"] == "claude_tok"
    assert env["AGENT_FACTORY_SUBAGENT_ENV_NAME"] == "sub-b"
    assert env["AGENT_FACTORY_SECRETS"] == "dk=GIT_DEPLOY_KEY"
    assert "AGENT_FACTORY_CODEX_TOKEN_SECRET" not in env  # other.toml has no codex_token


def test_the_deploy_child_gets_the_profile_text_not_a_path(monkeypatch):
    """The throwaway worktree has no profiles/ directory (real ones are untracked)."""
    monkeypatch.setenv("AGENT_FACTORY_PROFILE", str(OTHER))
    profile = _profile.require()
    env = _profile.child_env(profile, {"A": "1", "AGENT_FACTORY_PROFILE": str(OTHER)})
    assert "AGENT_FACTORY_PROFILE" not in env and env["A"] == "1"
    assert _profile.parse(env["AGENT_FACTORY_PROFILE_TOML"]).name == "other"


def test_role_policy_table_is_applied_at_deploy_like_the_env_passthrough(monkeypatch):
    """`[policy]` becomes AGENT_FACTORY_<KEY> on the agent environment, over the shell's value."""
    monkeypatch.setenv("AGENT_FACTORY_PROFILE", str(OTHER))
    monkeypatch.setenv("AGENT_FACTORY_AUTHOR_MODEL", "from-the-shell")
    monkeypatch.setenv("AGENT_FACTORY_AUTHOR_EFFORT", "low")  # shell-only keys still pass through
    from flyte_agent_factory import agent

    try:
        agent = reload_agent(agent)
        got = agent.env.env_vars
        assert got is not None
        assert got["AGENT_FACTORY_AUTHOR_MODEL"] == "sonnet"
        assert got["AGENT_FACTORY_JUDGE_ENGINE"] == "codex"
        assert got["AGENT_FACTORY_AUTHOR_EFFORT"] == "low"
        assert "AGENT_FACTORY_PROFILE" not in got
        assert _profile.parse(got["AGENT_FACTORY_PROFILE_TOML"]).name == "other"
    finally:
        monkeypatch.undo()
        reload_agent(agent)


def test_agent_names_the_console_from_the_profile(monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_PROFILE", str(OTHER))
    from flyte_agent_factory import agent

    try:
        agent = reload_agent(agent)
        note = _instructions._console_auth_note(Path("/tmp/token"))
        assert "route('**://console.example.org/**'" in note
        assert "unionai" not in note
        assert "the console's sign-in page" in _instructions._signin_target()
    finally:
        monkeypatch.undo()
        reload_agent(agent)


# --- per-slot role policy ----------------------------------------------------------------


def _is_policy_var(name):
    """A role-policy variable, as against the slot recipe's, which shares the `AGENT_FACTORY_` prefix."""
    return name.startswith(_names.PREFIX) and name not in _names.POD_FIXED


def test_deploying_slot_two_sets_its_role_policy_and_slot_one_none():
    p = _profile.load(str(WORK))
    one, two = (p.deploy_env(s) for s in p.slots)
    assert {k: v for k, v in two.items() if _is_policy_var(k)} == dict.fromkeys(_ROLE_KEYS, "codex")
    assert not any(_is_policy_var(k) for k in one)


def test_a_slots_policy_wins_over_the_global_one_and_leaves_the_rest():
    text = _example_with("# AUTHOR_MODEL", 'AUTHOR_MODEL = "opus"\nJUDGE_ENGINE = "claude"\n# AUTHOR_MODEL')
    text = text.replace(
        "# [slots.policy]\n# CRAFT_ENGINE", '[slots.policy]\nJUDGE_ENGINE = "codex"\nCRAFT_ENGINE', 1
    )
    p = _profile.parse(text)
    assert p.deploy_env(p.slots[0])["AGENT_FACTORY_JUDGE_ENGINE"] == "claude"
    assert p.deploy_env(p.slots[1])["AGENT_FACTORY_JUDGE_ENGINE"] == "codex"
    assert p.deploy_env(p.slots[1])["AGENT_FACTORY_AUTHOR_MODEL"] == "opus"
    assert p.policy_env == {"AGENT_FACTORY_AUTHOR_MODEL": "opus", "AGENT_FACTORY_JUDGE_ENGINE": "claude"}


def test_deploy_work_gives_slot_two_its_role_policy_over_the_callers_shell():
    deploy_work = _ops("deploy_work")  # the suite's profile
    shell = {"AGENT_FACTORY_CRAFT_ENGINE": "claude", "AGENT_FACTORY_AUTHOR_EFFORT": "low"}
    one = deploy_work.recipe_env(shell, slot="agent-one")
    two = deploy_work.recipe_env(shell, slot="agent-two")
    assert all(two[key] == "codex" for key in _ROLE_KEYS)
    assert [key for key in _ROLE_KEYS if key in one] == ["AGENT_FACTORY_CRAFT_ENGINE"]  # the shell's own
    assert one["AGENT_FACTORY_CRAFT_ENGINE"] == "claude"  # slot one has no policy to override it
    assert one["AGENT_FACTORY_AUTHOR_EFFORT"] == two["AGENT_FACTORY_AUTHOR_EFFORT"] == "low"


def test_the_agent_environment_of_slot_two_carries_its_policy(monkeypatch):
    from flyte_agent_factory import agent

    monkeypatch.setenv("AGENT_FACTORY_ENV_NAME", "agent-two")
    monkeypatch.setenv("AGENT_FACTORY_AUTHOR_EFFORT", "low")
    try:
        agent = reload_agent(agent)
        got = agent.env.env_vars
        assert got is not None
        assert all(got[key] == "codex" for key in _ROLE_KEYS)
        assert got["AGENT_FACTORY_AUTHOR_EFFORT"] == "low"
        monkeypatch.setenv("AGENT_FACTORY_ENV_NAME", "agent-one")
        agent = reload_agent(agent)
        assert agent.env.env_vars is not None
        assert not any(key in agent.env.env_vars for key in _ROLE_KEYS)
    finally:
        monkeypatch.undo()
        reload_agent(agent)


@pytest.mark.parametrize(
    "key",
    [
        "craft_engine",
        "AGENT_FACTORY_CRAFT_ENGINE",
        "PROFILE",
        "PROFILE_TOML",
        "A-B",
        "A B",
        # Any CLAUDE_ prefix, which covers the pre-rename spelling, is refused, not read as some other key.
        "CLAUDE_CRAFT_ENGINE",
    ],
)
@pytest.mark.parametrize("where", ["global", "slot"])
def test_policy_keys_are_validated(key, where):
    if where == "global":
        text = _example_with("# AUTHOR_MODEL", f'"{key}" = "x"\n# AUTHOR_MODEL')
    else:
        text = _example_with('# [slots.policy]\n# CRAFT_ENGINE = "codex"', f'[slots.policy]\n"{key}" = "x"')
    with pytest.raises(_profile.ProfileError, match=r"\[(slots\.)?policy\] .* must be A-Z, 0-9 and _"):
        _profile.parse(text)


def test_a_slots_policy_values_must_be_strings():
    text = _example_with('# [slots.policy]\n# CRAFT_ENGINE = "codex"', "[slots.policy]\nCRAFT_ENGINE = 3")
    with pytest.raises(_profile.ProfileError, match=r"\[slots.policy\] CRAFT_ENGINE must be a string"):
        _profile.parse(text)


# --- the rest of the review ------------------------------------------------------------------


SELF_AGENT_TABLE = '[self_agent]\nenv = "retired-self-agent"\nclaude_login = "l"\ndeploy_key = "k"\n\n'


def test_a_self_agent_table_is_ignored_with_the_retirement_message(capsys):
    # Live pods deployed before the retirement carry [self_agent] in AGENT_FACTORY_PROFILE_TOML and may run
    # newer code: rejecting it made every import of the profile fail there.
    with_table = _profile.parse(_example_with("[mcp]\n", SELF_AGENT_TABLE + "[mcp]\n"))
    assert (
        "[self_agent] was retired and is ignored: self-improve runs on an ordinary slot"
        in capsys.readouterr().err
    )
    without = _profile.parse(_example_with("[mcp]\n", "[mcp]\n"))
    assert dataclasses.replace(with_table, text=without.text) == without  # text is the source, verbatim
    assert not hasattr(with_table, "self_agent")


def _example_with_browser(table: str) -> str:
    return _example_with("[mcp]\n", f"[browser]\n{table}\n[mcp]\n")


def test_the_personal_browser_names_one_of_the_slots_envs():
    p = _profile.parse(
        _example_with_browser('personal = true\nprofile_uri = "memory://p"\npersonal_env = "agent-b"')
    )
    assert p.browser.personal and p.browser.personal_env == "agent-b"
    assert _profile.parse(EXAMPLE.read_text()).browser.personal_env == ""


@pytest.mark.parametrize(
    "table",
    [
        'personal = true\nprofile_uri = "memory://p"',
        'personal = true\nprofile_uri = "memory://p"\npersonal_env = "agent-b-sub"',
        'personal = true\nprofile_uri = "memory://p"\npersonal_env = "elsewhere"',
        'personal_env = "nope"',
    ],
)
def test_personal_env_is_required_for_a_personal_browser_and_must_be_a_slot(table):
    """Name kept from before #90, which reversed the rule: `personal_env` is accepted and grants nothing.

    These tables were refused when a slot held the grant; now they parse, and the legacy browser is identity
    `personal` whatever `personal_env` says. A legacy browser still needs its `profile_uri` (below).
    """
    browser = _profile.parse(_example_with_browser(table)).browser
    assert [i.name for i in browser.identities] == (["personal"] if "personal = true" in table else [])


def test_a_legacy_personal_browser_still_requires_its_profile_uri():
    with pytest.raises(_profile.ProfileError, match="personal browser requires profile_uri"):
        _profile.parse(_example_with_browser('personal = true\npersonal_env = "agent-b"'))


def test_legacy_personal_browser_is_one_identity_named_personal():
    p = _profile.parse(
        _example_with_browser('personal = true\nprofile_uri = "memory://p"\npersonal_env = "agent-b"')
    )
    (identity,) = p.browser.identities
    assert p.browser.personal and p.browser.personal_env == "agent-b"
    assert (identity.name, identity.profile_uri) == ("personal", "memory://p")
    assert identity.app == "claude-browser-personal"
    assert identity.token_secret == "browser_personal_token"
    assert identity.token_env == "BROWSER_PERSONAL_TOKEN"
    assert _profile.parse(EXAMPLE.read_text()).browser.personal_env == ""
    assert not _profile.parse(EXAMPLE.read_text()).browser.personal


def test_personal_env_is_deprecated_and_no_longer_required_or_checked_against_the_slots():
    # Requirement change from the owner: the grant is no longer tied to a slot, so personal_env is ignored.
    for table in (
        'personal = true\nprofile_uri = "memory://p"',
        'personal = true\nprofile_uri = "memory://p"\npersonal_env = "elsewhere"',
        'personal_env = "nope"',
    ):
        _profile.parse(_example_with_browser(table))


def test_a_legacy_personal_browser_requires_profile_uri():
    with pytest.raises(_profile.ProfileError, match="personal browser requires profile_uri"):
        _profile.parse(_example_with_browser('personal = true\npersonal_env = "agent-b"'))


IDENTITIES = """exit_node = "home-router"
tailscale_tag = "tag:factory-browser"
[browser.identities.linkedin]
profile_uri = "s3://b/linkedin.tar.gz"
exit_node = ""
[browser.identities.github]
profile_uri = "s3://b/github.tar.gz"
"""


def test_browser_identities_are_ordered_by_name_and_inherit_the_exit_node():
    browser = _profile.parse(_example_with_browser(IDENTITIES)).browser
    assert [i.name for i in browser.identities] == ["github", "linkedin"]
    github = browser.identity("github")
    assert github == _profile.BrowserIdentity("github", "s3://b/github.tar.gz")
    assert browser.exit_node == "home-router" and github.exit_node is None  # inherited, resolved at deploy
    assert github.app == "claude-browser-github" and github.token_secret == "browser_github_token"
    assert github.token_env == "BROWSER_GITHUB_TOKEN"
    linkedin = browser.identity("linkedin")
    assert linkedin is not None and linkedin.exit_node == ""  # "" is direct egress, not "inherit"
    assert browser.identity("nobody") is None
    assert browser.tailscale_tag == "tag:factory-browser" and browser.personal


def test_the_tailscale_tag_defaults_and_is_validated():
    assert _profile.parse(_example_with_browser(IDENTITIES.replace("tag:factory-browser", "tag:k8s")))
    assert _profile.parse(EXAMPLE.read_text()).browser.tailscale_tag == "tag:k8s"
    with pytest.raises(_profile.ProfileError, match="tailscale_tag"):
        _profile.parse(_example_with_browser(IDENTITIES.replace("tag:factory-browser", "Bad Tag")))


def test_an_identity_name_with_a_dash_maps_to_an_underscored_secret():
    table = '[browser.identities.my-bank]\nprofile_uri = "memory://b"'
    identity = _profile.parse(_example_with_browser(table)).browser.identity("my-bank")
    assert identity is not None
    assert identity.token_secret == "browser_my_bank_token" and identity.token_env == "BROWSER_MY_BANK_TOKEN"


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ('[browser.identities.Bad]\nprofile_uri = "memory://b"', "name must match"),
        ('[browser.identities.1st]\nprofile_uri = "memory://b"', "name must match"),
        (f'[browser.identities.{"a" * 21}]\nprofile_uri = "memory://b"', "name must match"),
        ("[browser.identities.github]\n", "missing key 'profile_uri'"),
        ('[browser.identities.github]\nprofile_uri = ""', "profile_uri must be a non-empty string"),
        ('[browser.identities.github]\nprofile_uri = "memory://b"\nextra = "x"', "unknown key.*extra"),
        (
            '[browser.identities.a]\nprofile_uri = "memory://b"\n[browser.identities.b]\nprofile_uri = "memory://b"',
            "share one profile_uri",
        ),
        (
            'personal = true\nprofile_uri = "memory://p"\n[browser.identities.personal]\n'
            'profile_uri = "memory://q"',
            "both define",
        ),
    ],
)
def test_browser_identities_are_validated(table, message):
    with pytest.raises(_profile.ProfileError, match=message):
        _profile.parse(_example_with_browser(table))


def test_a_placeholder_is_refused_unless_the_profile_allows_it():
    text = _example_with('project = "my-agents"', 'project = "TODO-project"')
    with pytest.raises(_profile.ProfileError, match=r"TODO-.*placeholder left in backend.project"):
        _profile.parse(text)
    assert _profile.parse(
        _example_with('name = "example"\n', 'name = "example"\nallow_placeholders = true\n')
    )
    allowed = text.replace('name = "example"\n', 'name = "example"\nallow_placeholders = true\n', 1)
    assert _profile.parse(allowed).backend.project == "TODO-project"
    with pytest.raises(_profile.ProfileError, match="allow_placeholders must be true or false"):
        _profile.parse(_example_with('name = "example"\n', 'name = "example"\nallow_placeholders = "yes"\n'))


def test_the_isolation_fixture_leaves_no_other_profile_module_behind(monkeypatch):
    """Whether or not `_factory` was imported before, the next test must not find `other`'s."""
    import flyte_agent_factory

    monkeypatch.delitem(sys.modules, "flyte_agent_factory._factory", raising=False)
    sys.modules.pop("flyte_agent_factory._factory", None)  # as if no earlier test had imported it
    monkeypatch.delattr(flyte_agent_factory, "_factory", raising=False)
    gen = other_profile.__wrapped__(monkeypatch)  # type: ignore[attr-defined]  # undecorated fixture function  # the fixture's generator, driven by hand
    next(gen)
    from flyte_agent_factory import _factory

    assert _factory.PROJECT == "proj-x"
    with pytest.raises(StopIteration):
        next(gen)
    assert "flyte_agent_factory._factory" not in sys.modules


# --- nothing personal in the code ---------------------------------------------------------

#: What belongs to one deployment (ops/personal_literals.py): generic shapes, and the owner's own identifiers
#: from the private list `FACTORY_OWNER_LITERALS` names, when it does. The list is private so that the guard
#: does not publish what it guards.
OWNER_LITERALS = personal_literals.private_list()
PERSONAL = personal_literals.pattern(OWNER_LITERALS)
#: A Union run id (`u` + 19 base-36 characters) in a comment names one owner's run. Only code is scanned for
#: it: test names and test comments may cite runs, and the history docs are full of them.
RUN_ID = re.compile(r"\bu[a-z0-9]{19}\b")
#: In prose and config a run id is any 20-character lowercase alphanumeric token with both letters and digits.
#: A 40-hex git sha is longer than the token, and the word boundaries keep it from matching a slice of one.
DOC_RUN_ID = re.compile(r"\b(?=[a-z0-9]*\d)(?=[a-z0-9]*[a-z])[a-z0-9]{20}\b")
#: Operational history and the owner's own profiles and briefs. All of it moves to the private direction repo
#: at the split, so none of it is scanned; the guard then covers everything that stays public.
#: (OPERATIONS.md, ACCEPTANCE.md and PR_DESCRIPTION.md are the three history docs; the docs/ spellings are
#: where the split design puts them.)
EXEMPT = (
    "OPERATIONS.md",
    "ACCEPTANCE.md",
    "PR_DESCRIPTION.md",
    "docs/operations-log.md",
    "profiles/union-work.toml",
    "profiles/gke-personal.toml",
)
#: Tests exempt from the guard. None: the owner's identifiers are in a private list and the checks of the
#: owner's private profiles read their values from those profiles, so no test may name an owner.
TEST_ALLOWLIST: tuple[str, ...] = ()
#: Lines in other tests that match a pattern without naming an owner (shared with ops/scan_public.py): a test
#: name built on the generic ANTHROPIC_API_KEY, and an assertion that a retired task name is gone.
TEST_LINE_ALLOWLIST = personal_literals.LINE_ALLOWLIST
SCANNED_PREFIXES = (
    "src/",
    "ops/",
    "briefs/",
    "skills/",
    "docs/",
    "scripts/",
    "profiles/",
    "standards/",
    "tests/",
)
SCANNED_FILES = ("README.md", "AGENTS.md", "LICENSE", "pyproject.toml", "uv.lock", ".gitignore")


def _tracked_and_new() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [p for p in out if (ROOT / p).is_file()]


def _shareable(paths: list[str]) -> list[str]:
    """The files the literal guard reads: what stays public, minus what is exempt or allowlisted."""
    return [
        p
        for p in paths
        if (p.startswith(SCANNED_PREFIXES) or p in SCANNED_FILES)
        and not p.startswith(EXEMPT)
        and p not in TEST_ALLOWLIST
    ]


def _is_code(path: str) -> bool:
    return path.endswith(".py") and path.startswith(("src/", "ops/"))


def _is_prose(path: str) -> bool:
    """Say whether a file is neither code, a test nor a lock file (whose hashes look like ids)."""
    return not path.endswith((".py", "uv.lock")) and not path.startswith("tests/")


def _literal_hits(root: Path, paths: list[str]) -> list[str]:
    hits = []
    for path in paths:
        try:
            lines = (root / path).read_text().splitlines()
        except UnicodeDecodeError:
            continue
        patterns = [PERSONAL]
        if _is_code(path):
            patterns.append(RUN_ID)
        if _is_prose(path):
            patterns.append(DOC_RUN_ID)
        hits += [
            f"{path}:{n}: {line.strip()[:100]}"
            for n, line in enumerate(lines, 1)
            if any(pattern.search(line) for pattern in patterns)
            and not any(path == p and allowed in line for p, allowed in TEST_LINE_ALLOWLIST)
        ]
    return hits


def test_no_personal_literal_is_left_in_shareable_files():
    scanned = _shareable(_tracked_and_new())
    assert "src/flyte_agent_factory/_profile.py" in scanned and "README.md" in scanned
    # The areas the guard was widened to; one that silently dropped out would stop being checked.
    for path in (
        "src/flyte_agent_factory/_assets/workspace_template/standards/security.md",
        "pyproject.toml",
        "skills/agent-factory/SKILL.md",
        "AGENTS.md",
        "LICENSE",
    ):
        assert path in scanned
    assert "tests/test_deployer.py" in scanned and "profiles/example.toml" in scanned
    hits = _literal_hits(ROOT, scanned)
    assert not hits, "personal literals belong in a profile:\n" + "\n".join(hits)


#: A deployment-specific value of a generic shape (an AWS account id in an ARN), built here so that this file
#: holds none.
PLANTED = "arn:aws:iam::" + "123456789012" + ":role/factory"


@pytest.mark.parametrize(
    "path",
    [
        "src/flyte_agent_factory/x.py",
        "ops/x.py",
        "skills/x/SKILL.md",
        "docs/x.md",
        "scripts/x.sh",
        "standards/x.md",
        "pyproject.toml",
        "README.md",
        "AGENTS.md",
        "LICENSE",
        "uv.lock",
        ".gitignore",
        "profiles/README.md",
        "tests/test_x.py",
    ],
)
def test_the_guard_catches_a_literal_planted_in_each_scanned_area(tmp_path, path):
    file = tmp_path / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(f"clean line\n# kept in {PLANTED}\n")
    assert _shareable([path]) == [path]
    assert _literal_hits(tmp_path, [path]) == [f"{path}:2: # kept in {PLANTED}"]


def test_the_guard_skips_what_moves_to_the_private_repo():
    for path in ("OPERATIONS.md", "profiles/union-work.toml"):
        assert _shareable([path]) == []
    # Briefs are scanned: the owner's own live in the workspace, not here.
    for path in ("ops/briefs/x.md", "briefs/x.md"):
        assert _shareable([path]) == [path]
    # The guard's own file is scanned too: it names no owner now.
    assert _shareable(["tests/test_profile.py"]) == ["tests/test_profile.py"]


PRODUCT_BRIEFS = {"briefs/factory-gardening.md", "briefs/smoke-wordstats.md"}


def test_the_engine_ships_only_product_briefs():
    tracked = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "briefs", "ops/briefs"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    shipped = {p for p in tracked if (ROOT / p).is_file()}
    assert shipped
    assert {p for p in shipped if not p.startswith("briefs/demo-")} <= PRODUCT_BRIEFS
    assert not (ROOT / "ops" / "briefs").exists()


def test_a_run_id_is_caught_in_code_but_not_in_a_test(tmp_path):
    (tmp_path / "ops").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "ops" / "x.py").write_text("# run u52kkt4nnb6fm5n8wd7k died\n")
    (tmp_path / "tests" / "test_x.py").write_text("# run u52kkt4nnb6fm5n8wd7k died\n")
    assert len(_literal_hits(tmp_path, ["ops/x.py"])) == 1
    assert _literal_hits(tmp_path, ["tests/test_x.py"]) == []


def test_a_run_id_is_caught_in_a_doc_but_a_sha_or_a_lock_hash_is_not(tmp_path):
    sha = "e02084d021d4e73b494ca43d604ce49423c57eb1"
    lines = [
        "run k3j9x2m4n8q1w5e7r6t0 died",  # 20 characters, letters and digits: a run id
        f"commit {sha}",
        "an identifier of exactlytwentyletters",  # no digits
        "a number of 12345678901234567890 digits",  # no letters
    ]
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "x.md").write_text("\n".join(lines) + "\n")
    (tmp_path / "uv.lock").write_text(f'hash = "sha256:{sha}{sha[:24]}"\nid = "k3j9x2m4n8q1w5e7r6t0"\n')
    assert _literal_hits(tmp_path, ["docs/x.md"]) == ["docs/x.md:1: run k3j9x2m4n8q1w5e7r6t0 died"]
    assert _literal_hits(tmp_path, ["uv.lock"]) == []


def test_the_guard_would_catch_a_literal():
    """The guard finds a deployment's values of the generic shapes (the owner's own: the owner-list test)."""
    tenant = "acme" + ".hosted.unionai" + ".cloud"  # split, so that this line is not itself a hit
    for line in (
        f"endpoint = dns:///{tenant}",
        "bucket = s3://" + "union-acme-prod-raw/x",
        "gs://" + "union-acme-prod/x",
        PLANTED,
        "image = 123456789012" + ".dkr.ecr.us-east-2.amazonaws.com/factory:1",
        "account " + "123456789012",
        "AWS account id: " + "123456789012",
    ):
        assert PERSONAL.search(line), line
    for line in (
        "endpoint = dns:///union.example.org",
        "bucket = s3://my-bucket/factory-state",
        "sha 1234567890123456",  # 16 digits, not an account id
        "claude-subagent",  # the generic default sub-agent name
        "my_github_app_private_key=GITHUB_APP_PRIVATE_KEY",  # the example's placeholder
    ):
        assert not personal_literals.pattern([]).search(line), line


def test_the_owner_list_is_read_from_the_file_the_variable_names(tmp_path):
    listed = tmp_path / "owner-literals.txt"
    listed.write_text("# a comment\n\nacme-widgets\n\\bwidget-\\d+\\b\n")
    env = {personal_literals.ENV: str(listed)}
    assert personal_literals.private_list(env) == ["acme-widgets", r"\bwidget-\d+\b"]
    assert personal_literals.private_list({}) == []
    found = personal_literals.pattern(personal_literals.private_list(env))
    assert found.search("project = acme-widgets-2") and found.search("env widget-7")
    assert not found.search("acme widgets")
    # Set but absent fails rather than checking nothing; a bad line is named.
    with pytest.raises(FileNotFoundError, match="is not a file"):
        personal_literals.private_list({personal_literals.ENV: str(tmp_path / "missing.txt")})
    listed.write_text("ok\n(unclosed\n")
    with pytest.raises(ValueError, match=r"owner-literals.txt:2: not a regex"):
        personal_literals.private_list(env)


def test_the_owner_list_is_part_of_the_guard():
    # The guard is the generic shapes plus whatever the owner's private list holds (none when it is unset).
    assert OWNER_LITERALS == personal_literals.private_list()
    for literal in (*personal_literals.GENERIC, *OWNER_LITERALS):
        assert f"(?:{literal})" in PERSONAL.pattern
    # The example profile's placeholder stays clear of it.
    assert not PERSONAL.search("my_github_app_private_key=GITHUB_APP_PRIVATE_KEY")


def test_a_subagent_env_may_not_reuse_another_slots_env():
    with pytest.raises(_profile.ProfileError, match="env and subagent_env names must all differ"):
        _profile.parse(_example_with('subagent_env = "agent-b-sub"', 'subagent_env = "agent-a"'))


@pytest.mark.parametrize(
    "key",
    [
        "api_key_secret",
        "google_client_id_secret",
        "google_client_secret_secret",
        "allowed_emails",
        # Union access tokens carry no email, so an allowlist cannot be checked.
        "allowed_users",
    ],
)
def test_reject_removed_mcp_settings(key):
    text = EXAMPLE.read_text().replace("[mcp]", f'[mcp]\n{key} = "old"')
    with pytest.raises(_profile.ProfileError, match=key):
        _profile.parse(text, "example.toml")


def test_mcp_repos_default_to_the_project_repo():
    assert _mcp(_profile.parse(EXAMPLE.read_text(), "example.toml")).repos == ("project",)


def test_mcp_repos_name_keys_of_the_repo_table():
    text = EXAMPLE.read_text().replace(
        'project = "git@github.com:you/my-sandbox.git"\n',
        'project = "git@github.com:you/my-sandbox.git"\nfactory = "git@github.com:you/factory.git"\n',
    )
    profile = _profile.parse(text.replace("[mcp]", '[mcp]\nrepos = ["project", "factory"]'), "example.toml")
    assert _mcp(profile).repos == ("project", "factory")
    assert profile.repos == {
        "project": "git@github.com:you/my-sandbox.git",
        "factory": "git@github.com:you/factory.git",
    }


@pytest.mark.parametrize(
    "repos, message",
    [
        ('["elsewhere"]', "elsewhere"),  # not a key of [repo]
        ('"project"', "must be a list of strings"),
        ("[1]", "must be a list of strings"),
        ("[]", "at least"),
    ],
)
def test_mcp_repos_are_validated(repos, message):
    text = EXAMPLE.read_text().replace("[mcp]", f"[mcp]\nrepos = {repos}")
    with pytest.raises(_profile.ProfileError, match=message):
        _profile.parse(text, "example.toml")


def test_policy_examples_in_the_profiles_readme_are_valid_profile_values():
    """`CRAFT_BLOCKING = false` was documented but `_policy` only accepts strings."""
    readme = (ROOT / "profiles" / "README.md").read_text()
    documented = re.findall(r"`([A-Z][A-Z0-9_]* = [^`]+)`", readme.split("Policy keys", 1)[1])
    assert documented
    text = EXAMPLE.read_text().replace("[policy]\n", "[policy]\n" + "\n".join(documented) + "\n", 1)
    policy = _profile.parse(text).policy
    assert policy["CRAFT_BLOCKING"] == "false"


# --- per-repository deploy keys ------------------------------------------------------------

_FACTORY_URL = "git@github.com:you/factory.git"


def _example_with_factory_repo(repo_keys: str) -> str:
    text = _example_with(
        'project = "git@github.com:you/my-sandbox.git"\n',
        f'project = "git@github.com:you/my-sandbox.git"\nfactory = "{_FACTORY_URL}"\n',
    )
    return text.replace("[policy]\n", f"[repo_keys]\n{repo_keys}\n\n[policy]\n", 1)


def test_repo_keys_mount_each_repositorys_key_beside_the_default_one():
    profile = _profile.parse(_example_with_factory_repo('factory = "my_factory_key"'), "example.toml")
    assert profile.repo_keys == {"factory": "my_factory_key"}
    assert profile.agent_secrets.split(",") == [
        "my_deploy_key=GIT_DEPLOY_KEY",
        "my_factory_key=GIT_DEPLOY_KEY_FACTORY",
        "my_gateway_key=LLM_GATEWAY_KEY",
        "my_anthropic_key=PROJECT_ANTHROPIC_API_KEY",
        "my_github_app_private_key=GITHUB_APP_PRIVATE_KEY",  # example.toml has a [github] table
    ]
    # The pod learns which mounted key belongs to which URL from the slot's deploy env.
    env = profile.deploy_env(profile.slots[0])
    assert json.loads(env["AGENT_FACTORY_REPO_KEYS"]) == {_FACTORY_URL: "GIT_DEPLOY_KEY_FACTORY"}


def test_without_repo_keys_a_slot_holds_only_the_default_key():
    profile = _profile.parse(EXAMPLE.read_text(), "example.toml")
    assert profile.repo_keys == {}
    assert "GIT_DEPLOY_KEY_" not in profile.agent_secrets
    assert "AGENT_FACTORY_REPO_KEYS" not in profile.deploy_env(profile.slots[0])


@pytest.mark.parametrize(
    ("repo_keys", "message"),
    [
        ('elsewhere = "k"', "[repo_keys] elsewhere is not a key of [repo]"),
        ('project = "k"', "[repo_keys] project: the project repository uses [secrets] deploy_key"),
        ('factory = ""', "[repo_keys] factory must be a non-empty string"),
        ("factory = 3", "[repo_keys] factory must be a non-empty string"),
        ('factory = "my_deploy_key"', "[repo_keys] factory must differ from [secrets] deploy_key"),
    ],
)
def test_repo_keys_are_validated(repo_keys, message):
    with pytest.raises(_profile.ProfileError, match=re.escape(message)):
        _profile.parse(_example_with_factory_repo(repo_keys), "example.toml")


def test_repo_keys_needs_the_repository_to_exist():
    text = EXAMPLE.read_text().replace("[policy]\n", '[repo_keys]\nfactory = "k"\n\n[policy]\n', 1)
    with pytest.raises(_profile.ProfileError, match=re.escape("[repo_keys] factory is not a key of [repo]")):
        _profile.parse(text, "example.toml")


def test_a_slot_mounts_the_factory_repo_key_under_its_repos_variable(tmp_path, monkeypatch):
    path = tmp_path / "p.toml"
    path.write_text(_example_with_factory_repo('factory = "my_factory_deploy_key"'))
    monkeypatch.setenv("AGENT_FACTORY_PROFILE", str(path))
    deploy_work = _ops("deploy_work")
    env = deploy_work.recipe_env({}, slot="agent-a")
    assert "my_factory_deploy_key=GIT_DEPLOY_KEY_FACTORY" in env["AGENT_FACTORY_SECRETS"].split(",")
    deploy_work.check_recipe(env)  # does not raise


_OLD_FACTORY_URL = "git@github.com:you/old-factory.git"


def _example_with_aliases(aliases: str) -> str:
    text = _example_with_factory_repo('factory = "my_factory_key"')
    return text.replace("[policy]\n", f"[repo_aliases]\n{aliases}\n\n[policy]\n", 1)


def test_a_repo_alias_maps_to_its_current_url_and_its_deploy_key():
    profile = _profile.parse(_example_with_aliases(f'factory = ["{_OLD_FACTORY_URL}"]'), "example.toml")
    assert profile.repo_aliases == {"factory": (_OLD_FACTORY_URL,)}
    assert profile.canonical_repo(_OLD_FACTORY_URL) == _FACTORY_URL
    # The https spelling of the old repo is the same repository.
    assert profile.canonical_repo("https://github.com/you/old-factory") == _FACTORY_URL
    for url in (_FACTORY_URL, "git@github.com:you/my-sandbox.git", "git@example.org:x/y.git", ""):
        assert profile.canonical_repo(url) == url
    env = profile.deploy_env(profile.slots[0])
    assert json.loads(env["AGENT_FACTORY_REPO_KEYS"]) == {
        _FACTORY_URL: "GIT_DEPLOY_KEY_FACTORY",
        _OLD_FACTORY_URL: "GIT_DEPLOY_KEY_FACTORY",
    }


def test_without_repo_aliases_every_url_is_its_own():
    profile = _profile.parse(_example_with_factory_repo('factory = "my_factory_key"'), "example.toml")
    assert profile.repo_aliases == {}
    assert profile.canonical_repo(_OLD_FACTORY_URL) == _OLD_FACTORY_URL


@pytest.mark.parametrize(
    ("aliases", "message"),
    [
        (f'elsewhere = ["{_OLD_FACTORY_URL}"]', "[repo_aliases] elsewhere is not a key of [repo]"),
        (f'factory = "{_OLD_FACTORY_URL}"', "[repo_aliases] factory must be a list of repository URLs"),
        ('factory = [""]', "[repo_aliases] factory must be a list of repository URLs"),
        ("factory = [3]", "[repo_aliases] factory must be a list of repository URLs"),
        (f'factory = ["{_FACTORY_URL}"]', "is the current [repo] factory URL"),
        ('factory = ["https://github.com/you/my-sandbox"]', "is the current [repo] project URL"),
        (
            f'factory = ["{_OLD_FACTORY_URL}"]\nproject = ["https://github.com/you/old-factory.git"]',
            "is listed under both factory and project",
        ),
        ("factory = 3", "[repo_aliases] factory must be a list"),
    ],
)
def test_repo_aliases_are_validated(aliases, message):
    with pytest.raises(_profile.ProfileError, match=re.escape(message)):
        _profile.parse(_example_with_aliases(aliases), "example.toml")


def test_repo_aliases_must_be_a_table():
    # Before any table, so it is a top-level key.
    text = "repo_aliases = 3\n" + _example_with_factory_repo('factory = "k"')
    with pytest.raises(_profile.ProfileError, match=re.escape("[repo_aliases] must be a table")):
        _profile.parse(text, "example.toml")


@needs_private["union-work"]
def test_union_work_slots_reach_the_factory_repo_with_its_own_key():
    p = _profile.load(str(UNION_WORK))
    factory = p.repos["factory"]
    assert factory.startswith("git@github.com:") and factory.endswith("/flyte-agent-factory.git")
    assert list(p.repo_keys) == ["factory"] and p.repo_keys["factory"] != p.secrets.deploy_key
    assert _mcp(p).repos == ("project", "factory")
    env = p.deploy_env(p.slots[0])
    # The repo's name before the GitHub rename reaches it with the same key.
    assert list(p.repo_aliases) == ["factory"] and isinstance(p.repo_aliases["factory"], tuple)
    (old,) = p.repo_aliases["factory"]
    # The same owner, the repository's earlier name.
    assert old != factory and old.rsplit("/", 1)[0] == factory.rsplit("/", 1)[0]
    assert p.canonical_repo(old) == factory
    assert json.loads(env["AGENT_FACTORY_REPO_KEYS"]) == {
        factory: "GIT_DEPLOY_KEY_FACTORY",
        old: "GIT_DEPLOY_KEY_FACTORY",
    }


# --- [deploy] / [state] / [github]: auto-deploy settings -------------------------------------


def test_auto_deploy_defaults_to_off_and_other_profiles_have_no_state():
    p = _profile.load(str(WORK))
    assert p.deploy.auto is False
    assert p.state.uri == "" and p.github.app_id == ""


def test_example_documents_the_auto_deploy_tables():
    p = _profile.load(str(EXAMPLE))
    assert (p.deploy.auto, p.state.uri.startswith("s3://"), p.github.app_id.isdigit()) == (False, True, True)
    assert p.github.private_key_secret


def _auto_text() -> str:
    """The example with auto-deploy on and the factory repo it needs."""
    text = _example_with("auto = false", "auto = true")
    return text.replace(
        '# factory = "git@github.com:you/factory.git"', 'factory = "git@github.com:you/factory.git"', 1
    )


def test_auto_deploy_with_everything_it_needs_parses():
    p = _profile.parse(_auto_text())
    assert p.deploy.auto is True


@pytest.mark.parametrize("uri", ["s3://b/p", "gs://b/p", "abfs://c@a/p", "file:///tmp/state"])
def test_state_uri_schemes(uri):
    assert (
        _profile.parse(_example_with("s3://my-bucket/factory-state/my-agents/development", uri)).state.uri
        == uri
    )


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("auto = false", 'auto = "yes"', "[deploy] auto must be true or false"),
        ("auto = false", "auto = 1", "[deploy] auto must be true or false"),
        ("auto = false", "auto = false\nsoon = true", "unknown key(s) in [deploy]: soon"),
        (
            "s3://my-bucket/factory-state/my-agents/development",
            "my-bucket/state",
            "[state] uri must start with a scheme",
        ),
        (
            "s3://my-bucket/factory-state/my-agents/development",
            "s3://",
            "[state] uri must start with a scheme",
        ),
        (
            'uri = "s3://my-bucket/factory-state/my-agents/development"',
            "uri = 3",
            "[state] uri must be a non-empty string",
        ),
        ('app_id = "123456"', 'app_id = "12ab"', "[github] app_id must be digits"),
        ('app_id = "123456"', "app_id = 12.5", "[github] app_id must be"),
        (
            'private_key_secret = "my_github_app_private_key"',
            "private_key_secret = 7",
            "[github] private_key_secret must be",
        ),
        ('app_id = "123456"', 'app_id = "123456"\ntypo = "x"', "unknown key(s) in [github]: typo"),
    ],
)
def test_invalid_auto_deploy_values_are_refused(old, new, message):
    with pytest.raises(_profile.ProfileError, match=re.escape(message)):
        _profile.parse(_example_with(old, new))


@pytest.mark.parametrize(
    ("line", "missing"),
    [
        ('app_id = "123456"\n', "[github] app_id"),
        ('private_key_secret = "my_github_app_private_key"\n', "[github] private_key_secret"),
    ],
)
def test_auto_deploy_requires_github(line, missing):
    text = _auto_text()
    with pytest.raises(_profile.ProfileError, match=re.escape(f"[deploy] auto = true requires {missing}")):
        _profile.parse(_without(text, line))


@needs_private["union-work"]
def test_union_work_deploys_itself():
    p = _profile.load(str(UNION_WORK))
    assert p.deploy.auto is True
    assert p.state.uri.startswith("s3://")
    assert p.state.uri.endswith(f"/factory-state/{p.backend.project}/{p.backend.domain}")
    assert p.github.app_id.isdigit()
    # The owner's own secret, not the example's `my_`-prefixed placeholder.
    assert p.github.private_key_secret and not p.github.private_key_secret.startswith("my_")


@pytest.mark.parametrize("uri", ["gcs://b/p", "abfss://c@a/p"])
def test_more_state_uri_schemes(uri):
    old = "s3://my-bucket/factory-state/my-agents/development"
    assert _profile.parse(_example_with(old, uri)).state.uri == uri


@pytest.mark.parametrize("uri", ["http://host/p", "https://host/p", "ftp://host/p", "S3://b/p", "r2://b/p"])
def test_state_uri_scheme_must_be_one_the_state_store_reaches(uri):
    old = "s3://my-bucket/factory-state/my-agents/development"
    with pytest.raises(_profile.ProfileError, match=r"\[state\] uri (scheme must be one of|must start with)"):
        _profile.parse(_example_with(old, uri))


def test_app_id_may_be_a_string_of_digits():
    assert _profile.parse(_example_with('app_id = "123456"', 'app_id = "007"')).github.app_id == "007"


def test_app_id_may_be_a_toml_integer_and_is_stored_as_a_string():
    p = _profile.parse(_example_with('app_id = "123456"', "app_id = 123456"))
    assert p.github.app_id == "123456"


@pytest.mark.parametrize("value", ['"١٢٣"', '"12 3"', '"-5"', "-5", "true", "[1]"])
def test_app_id_must_be_ascii_digits(value):
    with pytest.raises(_profile.ProfileError, match=r"\[github\] app_id must be"):
        _profile.parse(_example_with('app_id = "123456"', f"app_id = {value}"))


def test_auto_deploy_requires_a_factory_repo():
    text = _auto_text().replace('factory = "git@github.com:you/factory.git"', "", 1)
    with pytest.raises(
        _profile.ProfileError, match=re.escape("[deploy] auto = true requires [repo] factory")
    ):
        _profile.parse(text)


@pytest.mark.parametrize(
    "url", ["git@gitlab.com:you/factory.git", "https://example.org/you/factory", "factory"]
)
def test_auto_deploy_factory_repo_must_be_on_github(url):
    text = _auto_text().replace("git@github.com:you/factory.git", url, 1)
    with pytest.raises(_profile.ProfileError, match=r"\[repo\] factory.*github.com"):
        _profile.parse(text)


def test_a_factory_repo_off_github_is_fine_without_auto_deploy():
    text = _example_with(
        '# factory = "git@github.com:you/factory.git"', 'factory = "git@gitlab.com:you/factory.git"'
    )
    assert _profile.parse(text).deploy.auto is False


def test_a_profile_in_a_separate_checkout_ships_this_checkouts_standards(tmp_path, capsys):
    """The name is kept (the review contract forbids removing a test); the old behaviour is gone.

    A direction repository with no standards of its own ships none, even though the engine tree has some:
    the engine's standards are never a fallback.
    """
    text = (Path(__file__).parent / "profiles" / "work.toml").read_text()
    direction = tmp_path / "direction"
    direction.mkdir()
    (direction / "mine.toml").write_text(text)
    engine = tmp_path / "engine"
    shutil.copytree(_workspace.TEMPLATE / "standards", engine / "standards")
    profile = _profile.load_from_direction(direction, "mine.toml")
    assert _standards.decode(profile.deployment_text().splitlines()[0].split('"')[1]) == {}
    assert "no standards configured" in capsys.readouterr().err


def test_a_direction_repos_standards_are_the_ones_deployed(tmp_path):
    text = (Path(__file__).parent / "profiles" / "work.toml").read_text()
    direction = tmp_path / "direction"
    (direction / "standards").mkdir(parents=True)
    (direction / "mine.toml").write_text(text)
    (direction / "standards/engineering.md").write_text(
        "## WORKSPACE-1 MUST\nNo badword.\n\nWhy: w\n\nCheck: c\n\nEnforced: forbid badword\n"
    )
    (direction / "standards/security.md").write_text("# s\n")
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.txt").write_text("badword\n")
    profile = _profile.load_from_direction(direction, "mine.toml")
    files = _standards.decode(profile.deployment_text().splitlines()[0].split('"')[1])
    results = {r.rule: r for r in _gates.standards_results(project, files, configs=tmp_path)}
    assert results["WORKSPACE-1"].status is _gate_base.Status.FAIL
    assert "app.txt:1" in results["WORKSPACE-1"].output


def test_personal_literal_guard_skips_binary_skill_archive(tmp_path):
    path = "skills/dist/agent-factory.zip"
    archive = tmp_path / path
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"PK\x03\x04\xff " + PLANTED.encode())
    assert _literal_hits(tmp_path, [path]) == []
    # Not for want of a match: the same bytes as text would be one.
    assert PERSONAL.search(PLANTED)


def test_meter_logins_follow_existing_bindings():
    """One Claude account for every slot (the token's), plus the Codex token's when there is one."""
    profile = _current()
    assert profile.meter_logins == {"claude claude_token_x": "claude_token_x"}
    with_codex = _profile.parse(
        WORK.read_text().replace("[secrets]\n", '[secrets]\ncodex_token = "cx_tok"\n')
    )
    assert with_codex.meter_logins == {
        "claude claude_token_x": "claude_token_x",
        "codex cx_tok": "cx_tok",
    }


def test_auto_deploy_does_not_require_object_storage():
    text = _without(_auto_text(), 'uri = "s3://my-bucket/factory-state/my-agents/development"\n')
    profile = _profile.parse(text)
    assert profile.deploy.auto and profile.state.uri == ""


def test_planner_admission_defaults_to_legacy():
    assert _profile.load(str(WORK)).planner.admission == "legacy"


@pytest.mark.parametrize("mode", ["legacy", "planner"])
def test_planner_admission_mode(mode):
    text = WORK.read_text() + f'\n[planner]\nadmission = "{mode}"\n'
    assert _profile.parse(text).planner.admission == mode


@pytest.mark.parametrize("value", ['"typo"', "true", "1"])
def test_planner_admission_rejects_invalid_mode(value):
    with pytest.raises(_profile.ProfileError, match="admission"):
        _profile.parse(WORK.read_text() + f"\n[planner]\nadmission = {value}\n")


def test_planner_runtime_refuses_cutover_without_ownership_gate(monkeypatch):
    profile = _profile.parse(WORK.read_text() + '\n[planner]\nadmission = "planner"\n')
    monkeypatch.setattr(_profile, "current", lambda: profile)
    with pytest.raises(_profile.ProfileError, match="stop-before-start"):
        _profile.require()


def _pinned_to(engine: str) -> str:
    """The example with agent-a's policy pinning its author engine, as the owner may write it."""
    return EXAMPLE.read_text().replace(
        'subagent_env = "agent-a-sub"\n',
        f'subagent_env = "agent-a-sub"\n[slots.policy]\nAUTHOR_ENGINE = "{engine}"\n',
        1,
    )


@pytest.mark.parametrize("pin", ["codex", "Codex", " codex"])
def test_a_slot_pinned_to_codex_needs_the_codex_token(pin):
    text = _pinned_to(pin)
    with pytest.raises(
        _profile.ProfileError, match="agent-a pins COORDINATOR_ENGINE=codex but has no codex_login"
    ):
        _profile.parse(text)
    with_token = text.replace("[secrets]\n", '[secrets]\ncodex_token = "cx_tok"\n', 1)
    assert _profile.parse(with_token).secrets.codex_token == "cx_tok"


def test_a_slot_pinned_to_claude_needs_nothing_beyond_the_claude_token():
    assert _profile.parse(_pinned_to("claude")).secrets.claude_token == "claude_setup_token"


# --- one setup-token: the logins of the previous releases are accepted and ignored ----------------


def test_a_one_token_profile_parses_and_mounts_the_token_on_every_slot():
    profile = _profile.parse(WORK.read_text())
    assert profile.secrets.claude_token == "claude_token_x"
    assert not any(hasattr(slot, "claude_login") or slot.codex_login for slot in profile.slots)
    for slot in profile.slots:
        env = profile.deploy_env(slot)
        assert env["AGENT_FACTORY_CLAUDE_TOKEN_SECRET"] == "claude_token_x"
        assert "CLAUDE_CREDENTIALS_SECRET" not in env and "AGENT_FACTORY_CODEX_SECRET" not in env


def _live_shaped_legacy() -> str:
    """work.toml shaped like the live profile: slot claude_login, [secrets] codex_login, [bench] logins."""
    text = WORK.read_text()
    text = text.replace(
        'subagent_env = "sub-one"\n', 'claude_login = "login_one"\nsubagent_env = "sub-one"\n', 1
    )
    text = text.replace(
        'subagent_env = "sub-two"\n', 'claude_login = "login_two"\nsubagent_env = "sub-two"\n', 1
    )
    text = text.replace("[secrets]\n", '[secrets]\ncodex_login = "codex_login_old"\n', 1)
    text = text.replace(
        'project = "git@example.org:team/work-sandbox.git"\n',
        'project = "git@example.org:team/work-sandbox.git"\nfactory = "git@example.org:team/factory.git"\n',
        1,
    )
    return text + (
        '\n[github]\napp_id = "123456"\nprivate_key_secret = "app_key_x"\n'
        '\n[bench]\nlogins = ["bench_login_one", "bench_login_two"]\n'
        'repo = "git@github.com:you/factory-bench.git"\nrepo_key = "bench_key_x"\n'
    )


def test_the_live_shaped_legacy_profile_parses_with_one_warning_per_legacy_key_and_mounts_no_login(
    monkeypatch, capsys
):
    monkeypatch.setattr(_profile, "_warned", set())
    profile = _profile.parse(_live_shaped_legacy(), "live.toml")
    warned = capsys.readouterr().err.splitlines()
    for key, replacement in (
        ("[[slots]] claude_login", "[secrets] claude_token"),
        ("[secrets] codex_login", "[secrets] codex_token"),
        ("[bench] logins", "[secrets] claude_token"),
    ):
        lines = [line for line in warned if key in line]
        assert len(lines) == 1 and replacement in lines[0], (key, warned)
    assert profile.secrets.claude_token == "claude_token_x"
    assert profile.secrets.codex_token == ""
    assert profile.bench is not None and profile.bench.claude_token == "claude_token_x"
    assert profile.meter_logins == {"claude claude_token_x": "claude_token_x"}
    for slot in profile.slots:
        env = profile.deploy_env(slot)
        assert env["AGENT_FACTORY_CLAUDE_TOKEN_SECRET"] == "claude_token_x"
        assert not {"CLAUDE_CREDENTIALS_SECRET", "AGENT_FACTORY_CODEX_SECRET"} & env.keys()
        assert not any("login" in value for value in env.values())
    assert "login" not in profile.agent_secrets


def test_a_legacy_login_does_not_stand_in_for_the_claude_token():
    """A profile that names only logins still has no token to run on, and is told how to get one."""
    text = WORK.read_text().replace('claude_token = "claude_token_x"\n', 'claude_login = "login_x"\n')
    with pytest.raises(_profile.ProfileError, match=r"claude_token is required: run `claude setup-token`"):
        _profile.parse(text, "w.toml")


def test_a_profile_without_a_claude_token_says_how_to_get_one():
    text = _without(WORK.read_text(), 'claude_token = "claude_token_x"\n')
    with pytest.raises(_profile.ProfileError) as err:
        _profile.parse(text, "w.toml")
    message = str(err.value)
    assert message.startswith("w.toml: [secrets] claude_token is required")
    assert "claude setup-token" in message and "Flyte secret" in message


def test_factory_projects_defaults_to_one():
    assert _profile.parse(_example_with_factory_repo('factory = "k"'), "example.toml").factory_projects == 1


def test_factory_projects_is_read_beside_the_factory_repo():
    text = _example_with_factory_repo('factory = "k"').replace(
        f'factory = "{_FACTORY_URL}"\n', f'factory = "{_FACTORY_URL}"\nfactory_projects = 3\n', 1
    )
    profile = _profile.parse(text, "example.toml")
    assert profile.factory_projects == 3
    assert "factory_projects" not in profile.repos  # [repo] holds URLs only


@pytest.mark.parametrize("value", ["0", "-1", "1.5", '"2"', "true"])
def test_factory_projects_must_be_a_positive_integer(value):
    text = _example_with_factory_repo('factory = "k"').replace(
        f'factory = "{_FACTORY_URL}"\n', f'factory = "{_FACTORY_URL}"\nfactory_projects = {value}\n', 1
    )
    with pytest.raises(_profile.ProfileError, match=re.escape("[repo] factory_projects must be an integer")):
        _profile.parse(text, "example.toml")


def test_factory_projects_requires_the_factory_repo():
    text = _example_with(
        'project = "git@github.com:you/my-sandbox.git"\n',
        'project = "git@github.com:you/my-sandbox.git"\nfactory_projects = 2\n',
    )
    with pytest.raises(
        _profile.ProfileError, match=re.escape("[repo] factory_projects requires [repo] factory")
    ):
        _profile.parse(text, "example.toml")


@pytest.mark.parametrize("directory", [None, "../standards", "standards"])
def test_fetched_direction_path_capture_survives_checkout(tmp_path, directory):
    tree = tmp_path / "deploy-fetch-123" / "tree"
    path = tree / "profiles/name.toml"
    path.parent.mkdir(parents=True)
    path.write_text(WORK.read_text() + (f'\n[standards]\ndir = "{directory}"\n' if directory else ""))
    standards = tree / "standards"
    standards.mkdir()
    (standards / "engineering.md").write_text("direction rules\n")
    (standards / "security.md").write_text("direction security\n")
    profile = _profile.load_from_direction(tree, "profiles/name.toml")
    shutil.rmtree(tree)
    deployed = _profile.parse(profile.deployment_text(), _profile.TOML_ENV)
    assert _standards.decode(deployed.standards_payload)["engineering.md"] == "direction rules\n"
    assert deployed.deployment_text() == profile.deployment_text()


def test_fetched_direction_path_prefers_profile_sibling_and_brand(tmp_path):
    tree = tmp_path / "deploy-fetch-123/tree"
    path = tree / "profiles/name.toml"
    path.parent.mkdir(parents=True)
    path.write_text(WORK.read_text() + '\n[standards]\ndir = "standards"\nbrand = "brand.md"\n')
    for parent, content in ((tree, "repository"), (path.parent, "profile")):
        (parent / "standards").mkdir()
        (parent / "standards/engineering.md").write_text(content)
        (parent / "standards/security.md").write_text(content)
        (parent / "brand.md").write_text(content)
    profile = _profile.load_from_direction(tree, "profiles/name.toml")
    files = _standards.decode(profile.standards_payload)
    assert files["engineering.md"] == "profile"
    assert files["brand.md"] == "profile"


def test_hand_loaded_direction_path_uses_matching_profile_suffix(tmp_path):
    tree = tmp_path / "direction"
    path = tree / "config/profiles/example.toml"
    path.parent.mkdir(parents=True)
    text = (
        _auto_text() + '\n[direction]\nrepo = "https://github.com/you/direction"\n'
        'profile = "config/profiles/example.toml"\n'
    )
    path.write_text(text)
    (tree / "standards").mkdir()
    (tree / "standards/engineering.md").write_text("hand deploy rules")
    (tree / "standards/security.md").write_text("security")
    profile = _profile.load(str(path))
    deployed = _profile.parse(profile.deployment_text(), _profile.TOML_ENV)
    assert _standards.decode(deployed.standards_payload)["engineering.md"] == "hand deploy rules"
    other = tree / "unrelated.toml"
    other.write_text(text)
    assert (
        _standards.decode(_profile.parse(_profile.load(str(other)).deployment_text()).standards_payload) == {}
    )


def test_engine_path_has_no_default_and_keeps_captured_payload(tmp_path):
    (tmp_path / "profiles").mkdir()
    path = tmp_path / "profiles/name.toml"
    path.write_text(WORK.read_text())
    shutil.copytree(_workspace.TEMPLATE / "standards", tmp_path / "standards")
    profile = _profile.load_from_tree(tmp_path, "name")
    assert profile is not None
    assert _standards.decode(profile.standards_payload) == {}
    payload = _standards.encode({"engineering.md": "already captured"})
    text = f'standards_payload = "{payload}"\n' + WORK.read_text()
    fetched = _profile.shipped(text, "name at abc123", tmp_path)
    assert fetched.standards_payload == payload


@pytest.mark.parametrize("directory", ["standards", "../standards"])
def test_fetched_direction_path_missing_explicit_dir_errors(tmp_path, monkeypatch, directory):
    engine = tmp_path / "engine"
    shutil.copytree(_workspace.TEMPLATE / "standards", engine / "standards")
    (engine / "profiles").mkdir()
    monkeypatch.setattr(_standards, "ROOT", engine / "profiles" if directory.startswith("..") else engine)
    monkeypatch.chdir(engine / "profiles")
    tree = tmp_path / "deploy-fetch-123/tree"
    path = tree / "profiles/name.toml"
    path.parent.mkdir(parents=True)
    path.write_text(WORK.read_text() + f'\n[standards]\ndir = "{directory}"\n')
    with pytest.raises(_profile.ProfileError, match="dir does not exist") as exc:
        _profile.load_from_direction(tree, "profiles/name.toml")
    assert str(path) in str(exc.value)
    assert str(tree) in str(exc.value)


def test_fetched_direction_path_parser_stays_strict_and_does_not_infer_origin(tmp_path):
    tree = tmp_path / "direction"
    path = tree / "profiles/example.toml"
    path.parent.mkdir(parents=True)
    text = _auto_text() + '\n[direction]\nrepo = "https://github.com/you/direction"\n'
    path.write_text(text)
    shutil.copytree(_workspace.TEMPLATE / "standards", tree / "standards")
    parsed = _profile.parse(text, str(path))
    assert _standards.decode(_profile.parse(parsed.deployment_text()).standards_payload) == {}
    with pytest.raises(_profile.ProfileError, match="unknown"):
        _profile.shipped("future_key = true\n" + text, "example at abc123", tree)


def test_fetched_direction_path_diagnostic_label_is_not_an_origin(tmp_path, monkeypatch):
    tree = tmp_path / "engine"
    tree.mkdir()
    cwd = tmp_path / "cwd"
    sibling = cwd / "example at abc123/standards"
    shutil.copytree(_workspace.TEMPLATE / "standards", sibling)
    monkeypatch.chdir(cwd)
    text = WORK.read_text() + '\n[standards]\ndir = "standards"\n'
    with pytest.raises(_profile.ProfileError, match="dir does not exist"):
        _profile.shipped(text, "example at abc123/profile.toml", tree)


@pytest.fixture
def fetched_engine_profile():
    text = WORK.read_text()
    name = _profile.parse(text).name
    with tempfile.TemporaryDirectory(prefix="deploy-fetch-", dir="/tmp") as directory:
        tree = Path(directory) / "tree"
        path = tree / "profiles" / f"{name}.toml"
        path.parent.mkdir(parents=True)
        yield tree, path, text


@pytest.mark.parametrize("location", ["parent", "local", "root", "absolute"])
def test_fetched_engine_path_child_captures_explicit_dir(fetched_engine_profile, location):
    from flyte_agent_factory import _deploy_child

    tree, path, text = fetched_engine_profile
    directory = {
        "parent": "../standards",
        "local": "standards",
        "root": "standards",
        "absolute": str(tree.parent / "absolute-standards"),
    }[location]
    selected = {
        "parent": tree / "standards",
        "local": path.parent / "standards",
        "root": tree / "standards",
        "absolute": Path(directory),
    }[location]
    selected.mkdir()
    (selected / "engineering.md").write_text("selected rules\n")
    (selected / "security.md").write_text("selected security\n")
    if location == "local":
        competing = tree / "standards"
        competing.mkdir()
        (competing / "engineering.md").write_text("wrong root rules\n")
        (competing / "security.md").write_text("wrong root security\n")
    path.write_text(text + f'\n[standards]\ndir = "{directory}"\n')
    label = f"profiles/{path.name} at abc123"
    profile = _deploy_child.load(path.read_text(), label, tree)
    shutil.rmtree(tree)
    deployed = _profile.parse(profile.deployment_text(), _profile.TOML_ENV)
    files = _standards.decode(deployed.standards_payload)
    assert files["engineering.md"] == "selected rules\n"
    assert files["security.md"] == "selected security\n"
    assert profile.source_dir == path.parent
    assert profile.source_root == tree


@pytest.mark.parametrize("directory", ["standards", "../standards"])
def test_fetched_engine_path_child_missing_explicit_dir_errors(
    fetched_engine_profile, tmp_path, monkeypatch, directory
):
    from flyte_agent_factory import _deploy_child

    tree, path, text = fetched_engine_profile
    engine = tmp_path / "installed-engine"
    shutil.copytree(_workspace.TEMPLATE / "standards", engine / "standards")
    (engine / "profiles").mkdir()
    monkeypatch.setattr(_standards, "ROOT", engine / "profiles" if directory.startswith("..") else engine)
    monkeypatch.chdir(engine / "profiles")
    path.write_text(text + f'\n[standards]\ndir = "{directory}"\n')
    label = f"profiles/{path.name} at abc123"
    with pytest.raises(_profile.ProfileError, match="dir does not exist") as exc:
        _deploy_child.load(path.read_text(), label, tree)
    assert label in str(exc.value)
    assert str(path.parent / directory) in str(exc.value)


def test_fetched_engine_path_child_keeps_captured_payload(fetched_engine_profile):
    from flyte_agent_factory import _deploy_child

    tree, path, text = fetched_engine_profile
    payload = _standards.encode({"engineering.md": "already captured"})
    path.write_text(f'standards_payload = "{payload}"\n' + text + '\n[standards]\ndir = "missing"\n')
    profile = _deploy_child.load(path.read_text(), f"profiles/{path.name} at abc123", tree)
    assert profile.standards_payload == payload


@pytest.mark.parametrize("location", ["local", "root", "parent", "unset"])
def test_hand_loaded_engine_path_resolves_standards(tmp_path, monkeypatch, location):
    tree = tmp_path / "engine"
    path = tree / "profiles/name.toml"
    path.parent.mkdir(parents=True)
    directory = {"local": "standards", "root": "standards", "parent": "../standards", "unset": None}[location]
    path.write_text(WORK.read_text() + (f'\n[standards]\ndir = "{directory}"\n' if directory else ""))
    for base, marker in ((tree, "repository rules"), (path.parent, "profile rules")):
        (base / "standards").mkdir()
        (base / "standards/engineering.md").write_text(marker)
        (base / "standards/security.md").write_text("security")
    if location == "root":
        shutil.rmtree(path.parent / "standards")
    other = tmp_path / "unrelated"
    (other / "standards").mkdir(parents=True)
    (other / "standards/engineering.md").write_text("wrong rules")
    (other / "standards/security.md").write_text("wrong security")
    monkeypatch.chdir(other)
    monkeypatch.setattr(_standards, "ROOT", other)
    profile = _profile.load(str(path))
    text = profile.deployment_text()
    assert profile.source_root == tree
    shutil.rmtree(tree)
    deployed = _profile.parse(text, _profile.TOML_ENV)
    files = _standards.decode(deployed.standards_payload)
    if location == "unset":
        assert files == {}
    else:
        assert files["engineering.md"] == ("profile rules" if location == "local" else "repository rules")
    assert deployed.deployment_text() == text


@pytest.mark.parametrize("conventional", [True, False])
def test_hand_loaded_engine_path_never_searches_for_standards(tmp_path, monkeypatch, conventional):
    tree = tmp_path / "engine"
    path = tree / ("profiles/name.toml" if conventional else "config/name.toml")
    path.parent.mkdir(parents=True)
    path.write_text(WORK.read_text() + '\n[standards]\ndir = "standards"\n')
    other = tmp_path / "unrelated"
    (other / "standards").mkdir(parents=True)
    (other / "standards/engineering.md").write_text("wrong rules")
    (other / "standards/security.md").write_text("wrong security")
    monkeypatch.chdir(other)
    monkeypatch.setattr(_standards, "ROOT", other)
    if not conventional:
        shutil.copytree(other / "standards", tree / "standards")
        (tree / ".git").mkdir()
    profile = _profile.load(str(path))
    assert profile.source_root == (tree if conventional else None)
    with pytest.raises(ValueError, match="dir does not exist"):
        profile.deployment_text()


def test_hand_loaded_engine_path_does_not_override_direction_suffix(tmp_path):
    tree = tmp_path / "direction"
    path = tree / "profiles/example.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        _auto_text() + '\n[direction]\nrepo = "https://github.com/you/direction"\n'
        'profile = "config/example.toml"\n'
    )
    shutil.copytree(_workspace.TEMPLATE / "standards", tree / "standards")
    profile = _profile.load(str(path))
    assert profile.source_root is None
    assert _standards.decode(_profile.parse(profile.deployment_text()).standards_payload) == {}
