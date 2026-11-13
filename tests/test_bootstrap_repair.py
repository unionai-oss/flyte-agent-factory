"""Repairs after the first QA round of the guided-setup bootstrap: one test per finding, run on fakes."""

from __future__ import annotations

import inspect
import json
import os
import re
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path
from typing import Any

import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError

from flyte_agent_factory import _bootstrap, _bootstrap_github, _bootstrap_profile, _profile
from flyte_agent_factory._bootstrap_base import BootstrapError, NotSignedIn, Settings
from flyte_agent_factory._bootstrap_flyte import DeployScripts, FlyteSdk
from tests._bootstrap_fakes import DEPLOY_SENTINEL, World, make_context
from tests.test_bootstrap import ROOT, run, statuses


@pytest.fixture(autouse=True)
def person(monkeypatch, tmp_path_factory):
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "Pat Example")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "pat@example.com")
    # A home of its own per test: the deploy-key step makes its key directory in the home directory and
    # sweeps up the ones a killed run left there, so a shared home lets parallel tests delete each other's.
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


# --- 1, 2: the Flyte adapter against the SDK's real signatures ------------------------------------------


def test_sign_in_calls_project_listall_the_way_the_sdk_allows(monkeypatch):
    from flyte import remote

    real = inspect.signature(remote.Project.listall)
    assert "limit" not in real.parameters  # the SDK's own signature: the old call raised TypeError
    called = []

    def listall(*args, **kwargs):
        real.bind(remote.Project, *args, **kwargs)  # TypeError when the call does not fit
        called.append(kwargs)
        return iter([])

    monkeypatch.setattr(remote.Project, "listall", classmethod(lambda cls, *a, **k: listall(*a, **k)))
    sdk = FlyteSdk(Settings("h", "p", "o/r", "s3://b/p"))
    sdk._ready = True
    sdk.sign_in()
    assert called == [{}]


def test_api_key_available_without_flyteplugins_is_false_not_a_crash(monkeypatch):
    import importlib.util

    def find_spec(name, *a):
        raise ModuleNotFoundError("No module named 'flyteplugins'", name="flyteplugins")

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    assert DeployScripts(env={}).api_key_available() is False


# --- 3: one deployment API key per run ------------------------------------------------------------------


def test_the_deploy_api_key_is_created_once_per_run(monkeypatch, tmp_path):
    import types

    made: list[str] = []

    class ApiKey:
        encoded_credentials = DEPLOY_SENTINEL
        client_id = "agent-factory-deploy"

        @classmethod
        def create(cls, name):
            made.append(name)
            return cls()

    module = types.ModuleType("flyteplugins.union.remote")
    module.ApiKey = ApiKey  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "flyteplugins", types.ModuleType("flyteplugins"))
    monkeypatch.setitem(sys.modules, "flyteplugins.union", types.ModuleType("flyteplugins.union"))
    monkeypatch.setitem(sys.modules, "flyteplugins.union.remote", module)
    (tmp_path / "ops").mkdir()
    for script in ("deploy_work.py", "deploy_factory.py"):
        (tmp_path / "ops" / script).write_text("print('ok')\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)  # the key's client id is recorded there
    deployer = DeployScripts(env={}, engine_root=tmp_path, say=lambda line: None)
    deployer.deploy(tmp_path / "profile.toml")
    deployer.deploy(tmp_path / "profile.toml", work=False)
    assert made == ["agent-factory-deploy"]


# --- 5: the ruleset must not lock the bootstrap's own pushes out ---------------------------------------


def _ruleset_body(no_app: bool) -> dict[str, Any]:
    sent: list[str] = []

    def fake_run(cmd, input=None, **kwargs):
        sent.append(input)
        return subprocess.CompletedProcess(cmd, 0, "{}", "")

    _bootstrap_github.GhCli(run=fake_run).create_ruleset("pat/ws", "n", deploy_key_bypass=no_app)
    body: dict[str, Any] = json.loads(sent[0])
    return body


def test_admins_bypass_the_ruleset_always_so_the_bootstraps_later_pushes_land():
    actors = _ruleset_body(False)["bypass_actors"]
    assert [(a["actor_type"], a["bypass_mode"]) for a in actors] == [("RepositoryRole", "always")]


def test_without_the_app_the_deploy_key_may_push_to_the_default_branch():
    actors = _ruleset_body(True)["bypass_actors"]
    assert ("DeployKey", "always") in {(a["actor_type"], a["bypass_mode"]) for a in actors}


def test_the_ruleset_step_passes_no_app_on(world, monkeypatch):
    seen = []
    out: list[str] = []
    ctx = make_context(world, Settings("h", "p", "pat/agent-workspace", "s3://b/p", no_app=True), out)
    original = ctx.github.create_ruleset

    def create_ruleset(repo, name, deploy_key_bypass=False):
        seen.append(deploy_key_bypass)
        original(repo, name, deploy_key_bypass=deploy_key_bypass)

    monkeypatch.setattr(ctx.github, "create_ruleset", create_ruleset)
    from flyte_agent_factory._bootstrap_steps import RulesetStep

    RulesetStep().do(ctx)
    assert seen == [True]


# --- 6: --dir must be a clone of --workspace ------------------------------------------------------------


def test_a_dir_that_is_another_repository_is_refused_before_anything_is_written(world):
    other = world.root / "clone"
    subprocess.run(["git", "init", "-q", "-b", "main", str(other)], check=True)
    subprocess.run(
        ["git", "-C", str(other), "remote", "add", "origin", "https://x.test/me/else.git"], check=True
    )
    world.repos.add("pat/agent-workspace")  # the workspace exists; the directory is just not it
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(world.bare("pat/agent-workspace"))], check=True
    )
    code, out = run(world)
    assert code == 3 and statuses(out)["template"] == "blocked", out
    assert "not a clone of pat/agent-workspace" in "\n".join(out)
    assert not (other / "profile.example.toml").exists() and not (other / "profile.toml").exists()
    assert world.count("push") == 0


# --- 7: the Codex secret name -------------------------------------------------------------------------


def test_the_codex_login_secret_name_starts_with_codex_login():
    assert _bootstrap_profile.SECRET_CODEX_LOGIN.startswith("codex_login")  # agent.py refuses any other


# --- 8: an explicit auto = false stays ----------------------------------------------------------------


def test_a_rerun_does_not_add_direction_when_the_person_turned_auto_off(world):
    assert run(world, "--no-app")[0] == 0
    path = world.root / "clone/profile.toml"
    path.write_text(path.read_text() + "\n[deploy]\nauto = false\n")
    subprocess.run(["git", "commit", "-qam", "auto off"], cwd=world.root / "clone", check=True)
    s = Settings("h", "p", "pat/agent-workspace", "s3://b/p")
    added = _bootstrap_profile.fill_missing(path, s, "424242")
    assert "direction.repo" not in added and "deploy.auto" not in added
    assert "[direction]" not in path.read_text() and "auto = false" in path.read_text()


# --- 11: a fresh clone has no installed package -------------------------------------------------------


def test_ops_bootstrap_runs_from_a_fresh_clone_without_pythonpath():
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [sys.executable, str(ROOT / "ops" / "bootstrap.py"), "--help"],
        capture_output=True, text=True, env=env, cwd="/",
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr[-500:]
    assert "--endpoint" in proc.stdout


# --- 12: CODEOWNERS names the owner -------------------------------------------------------------------


def test_the_template_step_replaces_the_owner_placeholder_in_codeowners(world):
    assert run(world, "--no-app")[0] == 0
    text = (world.root / "clone/.github/CODEOWNERS").read_text()
    assert "@OWNER" not in text and "/standards/ @pat" in text


def test_an_organization_workspace_names_the_signed_in_user_in_codeowners(world):
    # GitHub accepts only users and org/team handles as code owners; a bare @org is ignored.
    world.org = True
    world.login = "sam"
    assert run(world, "--no-app")[0] == 0
    text = (world.root / "clone/.github/CODEOWNERS").read_text()
    assert "@OWNER" not in text and "/standards/ @sam" in text and "@pat" not in text


# --- 14, 15: deploy evidence is the profile that was deployed -----------------------------------------


def test_a_failed_final_deploy_is_retried_not_skipped(world):
    original = world.__class__.change
    failing = {"on": True}

    def change(self, what):
        if what == "deploy work=False" and failing["on"]:
            raise RuntimeError("deploy failed")
        return original(self, what)

    world.__class__.change = change
    try:
        code, out = run(world)
        assert code == 3 and statuses(out)["mcp-url"] == "blocked", out
        failing["on"] = False
        code, out = run(world)
        assert code == 0 and statuses(out)["mcp-url"] == "done", out
        assert world.count("deploy work=False") == 1
        before = list(world.events)
        assert run(world)[0] == 0 and world.events == before
    finally:
        world.__class__.change = original


def test_adding_the_app_after_a_no_app_run_redeploys_everything(world):
    assert run(world, "--no-app")[0] == 0
    deploys = world.count("deploy work=True")
    code, out = run(world)
    assert code == 0, out
    assert world.count("deploy work=True") == deploys + 1
    assert statuses(out)["deploy"] == "done"
    before = list(world.events)
    assert run(world)[0] == 0 and world.events == before


# --- 16: an installation is only ever labelled for what was checked -----------------------------------


def test_a_removed_installation_is_not_reported_installed_for_an_organization(world):
    assert run(world)[0] == 0
    world.org = True
    world.installations = []  # the App was uninstalled
    code, out = run(world, "--status")
    assert code == 3 and statuses(out)["github-app"] == "todo"


def test_a_remembered_installation_is_labelled_not_checked(world):
    assert run(world)[0] == 0
    code, out = run(world, "--status")
    line = next(line for line in out if line.startswith("[done] github-app"))
    assert "not re-checked" in line and "verified" not in line.replace("not re-checked", "")


# --- 17: the copy-paste command fails closed ----------------------------------------------------------


def _command() -> str:
    text = (ROOT / "docs" / "quickstart.md").read_text()
    blocks = re.findall(r"```bash\n(.*?)```", text, re.DOTALL)
    block: str = next(b for b in blocks if "ops/bootstrap.py --endpoint" in b and "--redeploy" not in b)
    return block


@pytest.fixture
def shell(tmp_path):
    """A HOME, a fake `uv` that logs its arguments, and a local repository standing in for the engine."""
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    upstream = tmp_path / "upstream"
    for d in (home, bin_dir, upstream):
        d.mkdir()
    (bin_dir / "uv").write_text(f'#!/bin/sh\necho "$PWD: $@" >> {tmp_path}/uv.log\n')
    (bin_dir / "uv").chmod(0o755)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(upstream)]
    subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
    (upstream / "f").write_text("1\n")
    subprocess.run([*git, "add", "f"], check=True)
    subprocess.run([*git, "commit", "-qm", "one"], check=True)
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"url.{upstream}.insteadOf",
        "GIT_CONFIG_VALUE_0": "https://github.com/unionai-oss/flyte-agent-factory",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    }  # fmt: skip

    def go() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-c", _command()], capture_output=True, text=True, env=env, cwd=tmp_path
        )

    go.home, go.log, go.upstream, go.env = home, tmp_path / "uv.log", upstream, env  # type: ignore[attr-defined]
    return go


def _uv_calls(shell) -> list[str]:
    return shell.log.read_text().splitlines() if shell.log.exists() else []


def test_the_command_clones_then_syncs_then_runs_the_bootstrap(shell):
    proc = shell()
    assert proc.returncode == 0 or "ops/bootstrap.py" in "".join(_uv_calls(shell)), proc.stderr
    calls = _uv_calls(shell)
    assert calls[0].endswith("sync -q --python 3.12") and "run python ops/bootstrap.py --endpoint" in calls[1]
    assert calls[1].startswith(str(shell.home / "flyte-agent-factory"))


def test_a_failed_update_stops_before_uv(shell):
    assert shell().returncode == 0
    shell.log.unlink()
    checkout = shell.home / "flyte-agent-factory"
    # A local commit on main: the fast-forward pull then has nothing it can do against a new upstream commit.
    (checkout / "g").write_text("local\n")
    subprocess.run(["git", "-C", str(checkout), "add", "g"], check=True, env=shell.env)
    subprocess.run(["git", "-C", str(checkout), "commit", "-qm", "local"], check=True, env=shell.env)
    (shell.upstream / "h").write_text("up\n")
    subprocess.run(["git", "-C", str(shell.upstream), "add", "h"], check=True, env=shell.env)
    subprocess.run(["git", "-C", str(shell.upstream), "commit", "-qm", "up"], check=True, env=shell.env)
    proc = shell()
    assert proc.returncode != 0 and _uv_calls(shell) == []


def test_an_unreachable_remote_stops_before_uv(shell, tmp_path):
    assert shell().returncode == 0
    shell.log.unlink()
    subprocess.run(["mv", str(shell.upstream), str(tmp_path / "gone")], check=True)
    assert shell().returncode != 0 and _uv_calls(shell) == []


def test_a_checkout_of_another_repository_is_refused(shell):
    other = shell.home / "flyte-agent-factory"
    subprocess.run(["git", "init", "-q", "-b", "main", str(other)], check=True)
    subprocess.run(
        ["git", "-C", str(other), "remote", "add", "origin", "https://example.test/x/y.git"], check=True
    )
    proc = shell()
    assert proc.returncode != 0 and _uv_calls(shell) == []
    assert "not a clone of" in proc.stderr


def test_a_checkout_on_another_branch_is_moved_to_main_first(shell):
    assert shell().returncode == 0
    shell.log.unlink()
    checkout = shell.home / "flyte-agent-factory"
    subprocess.run(["git", "-C", str(checkout), "checkout", "-q", "-b", "side"], check=True)
    assert shell().returncode == 0
    branch = subprocess.run(
        ["git", "-C", str(checkout), "branch", "--show-current"], capture_output=True, text=True
    ).stdout.strip()
    assert branch == "main" and len(_uv_calls(shell)) == 2


def test_both_documents_carry_the_same_command():
    skill = (ROOT / "skills/agent-factory/references/setup.md").read_text()
    assert _command().strip() in skill


# --- 18: a public workspace is refused ----------------------------------------------------------------


def test_a_public_workspace_is_refused_before_anything_is_written(world):
    world.repos.add("pat/agent-workspace")
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(world.bare("pat/agent-workspace"))], check=True
    )
    world.visibility["pat/agent-workspace"] = "public"
    code, out = run(world)
    assert code == 3 and statuses(out)["workspace"] == "blocked", out
    assert "public" in "\n".join(out) and "Settings" in "\n".join(out)
    assert not (world.root / "clone").exists() and world.count("push") == 0
    assert world.count("create_secret") == 0 and "agent-workspace-main" not in world.rulesets


def test_a_failing_lookup_is_not_taken_for_a_missing_repository():
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "error connecting to api.github.com")

    with pytest.raises(BootstrapError):
        _bootstrap_github.GhCli(run=fake_run).repo_visibility("pat/ws")

    def missing(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 1, "", "GraphQL: Could not resolve to a Repository with the name 'pat/ws'."
        )

    assert _bootstrap_github.GhCli(run=missing).repo_visibility("pat/ws") is None


def test_the_app_name_is_cut_to_githubs_limit_and_the_slug_follows():
    name = _bootstrap_github.build_manifest("a" * 39, "ws", "http://127.0.0.1:1/c")["name"]
    assert isinstance(name, str) and len(name) <= 34 and not name.endswith("-")


# --- 10: the checks script gates on both zips ---------------------------------------------------------


def test_checks_sh_runs_the_skill_drift_check_on_both_zips():
    text = (ROOT / "factory-improvement/checks.sh").read_text()
    for zip_name in ("agent-factory.zip", "since.zip"):
        assert f"ops/package_skill.py --check skills/dist/{zip_name}" in text


def test_package_check_exits_non_zero_when_stale_or_unknown(tmp_path):
    for body, code in (("x\n\nSkill version: 000000000000\n", 1), ("no version\n", 2)):
        skill = tmp_path / "SKILL.md"
        skill.write_text(body)
        proc = subprocess.run(
            [sys.executable, str(ROOT / "ops/package_skill.py"), "--check", str(skill)], capture_output=True
        )
        assert proc.returncode == code
    current = subprocess.run(
        [sys.executable, str(ROOT / "ops/package_skill.py"), "--check", str(ROOT / "skills/dist/since.zip")],
        capture_output=True,
        text=True,
    )
    assert current.returncode == 0 and current.stdout.strip() == "current"


_ = (textwrap, _profile, _bootstrap)


# =====================================================================================================
# Repair batch after the second verification (docs/quickstart.md "Manual reference" is unchanged).
# =====================================================================================================

# --- B-A: the deployment API key is ephemeral -----------------------------------------------------------


class _UnionServer:
    """What Union does with API keys that matters here: a second key of the same name is refused, and on a
    serverless tenant the client id is `<tenant>-<handle>-<name>` (`prefix`)."""

    def __init__(self, prefix: str = "") -> None:
        self.keys: dict[str, str] = {}
        self.deleted: list[str] = []
        self.prefix = prefix


def _install_fake_union(monkeypatch, server: _UnionServer) -> None:
    import types

    class Key:
        def __init__(self, client_id: str) -> None:
            self.client_id = client_id
            self.encoded_credentials = DEPLOY_SENTINEL

        @classmethod
        def create(cls, name):
            client_id = server.prefix + name
            if client_id in server.keys:
                raise RuntimeError("AlreadyExists: client id is taken")
            server.keys[client_id] = name
            return cls(client_id)

        @classmethod
        def delete(cls, client_id):
            if client_id not in server.keys:
                raise ConnectError(Code.NOT_FOUND, f"app {client_id} not found")
            server.deleted.append(client_id)
            del server.keys[client_id]

    module = types.ModuleType("flyteplugins.union.remote")
    module.ApiKey = Key  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "flyteplugins", types.ModuleType("flyteplugins"))
    monkeypatch.setitem(sys.modules, "flyteplugins.union", types.ModuleType("flyteplugins.union"))
    monkeypatch.setitem(sys.modules, "flyteplugins.union.remote", module)


def _engine_root(tmp_path, fail: bool = False):
    # Also the workspace clone in these tests (`tmp_path / "profile.toml"`), whose git config records the key.
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "ops").mkdir(exist_ok=True)
    body = "import sys\nsys.exit(1)\n" if fail else "print('ok')\n"
    for script in ("deploy_work.py", "deploy_factory.py"):
        (tmp_path / "ops" / script).write_text(body)
    return tmp_path


def _deployer(tmp_path, env=None) -> DeployScripts:
    return DeployScripts(env=env or {}, engine_root=tmp_path, say=lambda line: None)


def _recorded(clone: Path) -> str:
    return subprocess.run(
        ["git", "config", "--local", "--get-all", "agent-factory.deploy-api-key"],
        cwd=clone,
        capture_output=True,
        text=True,
    ).stdout


OTHER = "tenant-other-agent-factory-deploy"


def test_another_members_deploy_key_on_the_tenant_is_never_deleted(monkeypatch, tmp_path):
    server = _UnionServer(prefix="tenant-pat-")
    server.keys[OTHER] = "another member's bootstrap, in flight"
    _install_fake_union(monkeypatch, server)
    deployer = _deployer(_engine_root(tmp_path))
    deployer.deploy(tmp_path / "profile.toml")
    assert set(server.keys) == {OTHER, "tenant-pat-agent-factory-deploy"}
    deployer.close()
    assert list(server.keys) == [OTHER] and server.deleted == ["tenant-pat-agent-factory-deploy"]
    assert _recorded(tmp_path) == ""  # no key and no record left after the run


def test_the_key_is_recorded_before_it_is_used(monkeypatch, tmp_path):
    server = _UnionServer(prefix="tenant-pat-")
    _install_fake_union(monkeypatch, server)
    root = _engine_root(tmp_path)
    (root / "ops" / "deploy_work.py").write_text(
        "import subprocess\nprint(subprocess.run(['git', 'config', '--get-all', "
        "'agent-factory.deploy-api-key'], capture_output=True, text=True).stdout.strip())\n"
    )
    seen: list[str] = []
    DeployScripts(env={}, engine_root=root, say=seen.append).deploy(tmp_path / "profile.toml")
    assert seen[0] == "tenant-pat-agent-factory-deploy"


def test_a_killed_runs_prefixed_key_is_deleted_on_rerun_through_its_record(monkeypatch, tmp_path):
    server = _UnionServer(prefix="tenant-pat-")
    server.keys[OTHER] = "another member's"
    _install_fake_union(monkeypatch, server)
    _deployer(_engine_root(tmp_path)).deploy(tmp_path / "profile.toml")  # killed: close() never ran
    assert _recorded(tmp_path).split() == ["tenant-pat-agent-factory-deploy"]
    rerun = _deployer(tmp_path)
    rerun.deploy(tmp_path / "profile.toml")  # Union would refuse the create if the leftover were still there
    rerun.close()
    assert list(server.keys) == [OTHER] and _recorded(tmp_path) == ""


def test_a_recorded_key_deleted_by_hand_is_just_forgotten(monkeypatch, tmp_path):
    server = _UnionServer(prefix="tenant-pat-")
    _install_fake_union(monkeypatch, server)
    _deployer(_engine_root(tmp_path)).deploy(tmp_path / "profile.toml")
    server.keys.clear()  # the person deleted it in Union
    rerun = _deployer(tmp_path)
    rerun.deploy(tmp_path / "profile.toml")
    rerun.close()
    assert server.keys == {} and _recorded(tmp_path) == ""


def test_a_key_that_cannot_be_recorded_is_deleted_unused(monkeypatch, tmp_path):
    server = _UnionServer(prefix="tenant-pat-")
    _install_fake_union(monkeypatch, server)
    root = _engine_root(tmp_path / "engine")
    clone = tmp_path / "not-a-checkout"
    clone.mkdir()
    deployer = DeployScripts(env={}, engine_root=root, say=lambda line: None)
    with pytest.raises(BootstrapError, match="deleted unused"):
        deployer.deploy(clone / "profile.toml")
    assert server.keys == {}


def test_close_deletes_the_key_this_run_made_and_only_that(monkeypatch, tmp_path):
    server = _UnionServer()
    _install_fake_union(monkeypatch, server)
    deployer = _deployer(_engine_root(tmp_path))
    deployer.deploy(tmp_path / "profile.toml")
    deployer.deploy(tmp_path / "profile.toml", work=False)
    assert list(server.keys) == ["agent-factory-deploy"]  # one key for both deploys of the run
    deployer.close()
    assert server.keys == {}
    deployer.close()  # closing twice is harmless


def test_close_without_a_key_does_not_touch_union(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "flyteplugins", None)  # any import of the plugin would raise
    _deployer(tmp_path).close()


def test_a_key_from_the_environment_is_never_deleted(monkeypatch, tmp_path):
    server = _UnionServer()
    server.keys["agent-factory-deploy"] = "the person's own"
    _install_fake_union(monkeypatch, server)
    deployer = _deployer(_engine_root(tmp_path), env={"_UNION_EAGER_API_KEY": DEPLOY_SENTINEL})
    deployer.deploy(tmp_path / "profile.toml")
    deployer.close()
    assert server.deleted == [] and "agent-factory-deploy" in server.keys


def test_a_failed_deploy_then_a_fixed_rerun_completes_and_leaves_no_key(monkeypatch, tmp_path):
    server = _UnionServer()
    _install_fake_union(monkeypatch, server)
    first = _deployer(_engine_root(tmp_path, fail=True))
    with pytest.raises(BootstrapError):
        first.deploy(tmp_path / "profile.toml")
    first.close()  # what `run()` does in its `finally`
    assert server.keys == {}
    second = _deployer(_engine_root(tmp_path))
    second.deploy(tmp_path / "profile.toml")
    second.close()
    assert server.keys == {}


def test_a_crash_after_the_deploy_then_a_rerun_completes(monkeypatch, tmp_path):
    server = _UnionServer()
    _install_fake_union(monkeypatch, server)
    _deployer(_engine_root(tmp_path)).deploy(tmp_path / "profile.toml")  # killed: close() never ran
    assert list(server.keys) == ["agent-factory-deploy"]
    rerun = _deployer(tmp_path)
    rerun.deploy(tmp_path / "profile.toml")
    rerun.close()
    assert server.keys == {}


def test_the_run_releases_the_deployer_whether_or_not_it_finished(world, monkeypatch):
    assert run(world)[0] == 0 and world.closed == 1
    from tests import _bootstrap_fakes

    def boom(self, profile, *, work=True):
        raise ZeroDivisionError("a bug, not an expected failure")

    monkeypatch.setattr(_bootstrap_fakes.FakeDeployer, "deploy", boom)
    w2 = World(world.root / "again")
    w2.root.mkdir()
    with pytest.raises(ZeroDivisionError):
        run(w2)
    assert w2.closed == 1


def test_the_api_key_is_never_printed(monkeypatch, tmp_path, capsys):
    server = _UnionServer()
    _install_fake_union(monkeypatch, server)
    deployer = DeployScripts(env={}, engine_root=_engine_root(tmp_path))
    deployer.deploy(tmp_path / "profile.toml")
    deployer.close()
    seen = capsys.readouterr()
    assert DEPLOY_SENTINEL not in seen.out + seen.err


# --- B1: the redeploy flag -------------------------------------------------------------------------------


def test_redeploy_runs_both_deploys_again_even_though_the_receipts_say_done(world):
    assert run(world)[0] == 0
    work_deploys = world.count("deploy work=True")
    code, out = run(world, "--redeploy")
    assert code == 0, out
    assert world.count("deploy work=True") == work_deploys + 1
    assert statuses(out)["deploy"] == "done"
    assert run(world)[0] == 0 and world.count("deploy work=True") == work_deploys + 1  # not sticky


def test_redeploy_status_reports_the_deploy_as_todo_and_changes_nothing(world):
    assert run(world)[0] == 0
    before = list(world.events)
    code, out = run(world, "--redeploy", "--status")
    assert code == 3 and statuses(out)["deploy"] == "todo" and world.events == before


def test_redeploy_is_in_the_help():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "ops" / "bootstrap.py"), "--help"], capture_output=True, text=True
    )
    assert "--redeploy" in proc.stdout


# --- B2/B-B: the App is written to the profile only once it is installed ------------------------------


def _profile_text(world: World) -> str:
    return (world.root / "clone/profile.toml").read_text()


def _has_app(world: World) -> bool:
    data = tomllib.loads(_profile_text(world))
    return "app_id" in data.get("github", {}) or "direction" in data or "auto" in data.get("deploy", {})


def _deploy_key_bypasses(world: World) -> bool:
    # A call, not `world.rulesets[...]` inline: mypy would carry the first assertion's narrowing past `run`.
    return world.rulesets["agent-workspace-main"]


def test_an_app_that_is_not_installed_is_not_written_to_the_profile(world):
    world.app_installed = False
    code, out = run(world)
    assert code == 3 and statuses(out)["github-app"] == "todo", out
    assert not _has_app(world)
    assert any("--no-app" in line for line in out if "install the App" in line)


def test_a_failed_install_then_no_app_leaves_a_consistent_app_free_setup(world):
    world.app_installed = False
    assert run(world)[0] == 3
    code, out = run(world, "--no-app")
    assert code == 0, out
    assert not _has_app(world) and world.rulesets["agent-workspace-main"] is True
    _profile.load(str(world.root / "clone/profile.toml"))


def test_no_app_then_the_app_takes_the_deploy_key_off_the_ruleset(world):
    assert run(world, "--no-app")[0] == 0
    assert _deploy_key_bypasses(world) is True
    code, out = run(world)
    assert code == 0, out
    assert _deploy_key_bypasses(world) is False and world.count("update_ruleset") == 1
    assert _has_app(world)
    before = list(world.events)
    assert run(world)[0] == 0 and world.events == before


def test_the_app_then_no_app_removes_the_app_keys_says_so_and_adds_the_bypass(world):
    assert run(world)[0] == 0 and _has_app(world)
    code, out = run(world, "--no-app")
    assert code == 0, out
    assert not _has_app(world) and "private_key_secret" not in _profile_text(world)
    line = next(line for line in out if line.startswith("[done] profile"))
    assert "--no-app" in line and "removed" in line
    assert world.rulesets["agent-workspace-main"] is True
    _profile.load(str(world.root / "clone/profile.toml"))
    log = subprocess.run(
        ["git", "status", "--porcelain"], cwd=world.root / "clone", capture_output=True, text=True
    ).stdout
    assert log.strip() == ""  # committed and pushed, not left in the working tree
    assert run(world, "--no-app")[0] == 0


def test_dropping_the_app_keys_keeps_what_the_person_added_to_the_same_tables(world):
    assert run(world)[0] == 0
    path = world.root / "clone/profile.toml"
    path.write_text(path.read_text().replace("[github]\n", '[github]\n# mine\nintake_label = "todo"\n'))
    subprocess.run(["git", "commit", "-qam", "label"], cwd=world.root / "clone", check=True)
    assert run(world, "--no-app")[0] == 0
    data = tomllib.loads(path.read_text())
    assert data["github"] == {"intake_label": "todo"} and "# mine" in path.read_text()


# --- the ruleset's bypass follows the mode, on the real adapter ----------------------------------------


def _gh_with_ruleset(actors: list[dict[str, Any]]):
    calls: list[tuple[list[str], str | None]] = []

    def fake_run(cmd, input=None, **kwargs):
        calls.append((cmd, input))
        joined = " ".join(cmd)
        if joined.endswith("repos/pat/ws/rulesets"):
            return subprocess.CompletedProcess(cmd, 0, json.dumps([{"id": 7, "name": "n"}]), "")
        if joined.endswith("repos/pat/ws/rulesets/7") and "-X" not in cmd:
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"id": 7, "bypass_actors": actors}), "")
        return subprocess.CompletedProcess(cmd, 0, "{}", "")

    return _bootstrap_github.GhCli(run=fake_run), calls


def test_the_adapter_reads_the_deploy_key_bypass_from_the_ruleset_itself():
    with_key, _ = _gh_with_ruleset([{"actor_type": "DeployKey"}, {"actor_type": "RepositoryRole"}])
    without, _ = _gh_with_ruleset([{"actor_type": "RepositoryRole"}])
    assert with_key.ruleset_deploy_key_bypass("pat/ws", "n") is True
    assert without.ruleset_deploy_key_bypass("pat/ws", "n") is False


def test_the_adapter_updates_the_existing_ruleset_with_put():
    gh, calls = _gh_with_ruleset([{"actor_type": "RepositoryRole"}])
    gh.update_ruleset("pat/ws", "n", deploy_key_bypass=True)
    cmd, body = calls[-1]
    assert cmd[:4] == ["gh", "api", "-X", "PUT"] and cmd[4] == "repos/pat/ws/rulesets/7"
    assert "DeployKey" in {a["actor_type"] for a in json.loads(body)["bypass_actors"]}


# --- the order ------------------------------------------------------------------------------------------


def test_the_steps_run_in_the_documented_order():
    assert [s.id for s in _bootstrap.build_steps()] == [
        "tools", "workspace", "template", "profile",
        "signin", "project", "deploy-key", "github-app",
        "ruleset", "board", "engine-token", "deploy", "mcp-url", "connect",
    ]  # fmt: skip


def test_the_ruleset_follows_every_push_to_main_but_the_factory_url_one(world):
    assert run(world)[0] == 0
    events = world.events
    ruleset = events.index("create_ruleset agent-workspace-main")
    pushes_after = [i for i, e in enumerate(events) if e == "push" and i > ruleset]
    assert len(pushes_after) == 1  # the URL commit, which only exists after the deploy
    assert events.index("deploy work=True") < pushes_after[0]


# --- non-blocking findings ----------------------------------------------------------------------------


def test_the_board_message_names_the_projects_tab_and_the_default_repository(world):
    code, out = run(world)
    line = next(line for line in out if line.endswith("workflows that add new issues to the board"))
    assert "Projects tab" in line and "Default repository" in line


def test_an_empty_existing_dir_is_cloned_into(world):
    (world.root / "clone").mkdir()
    code, out = run(world)
    assert code == 0, out
    assert (world.root / "clone/profile.toml").is_file()


def test_a_dir_that_is_neither_empty_nor_a_checkout_says_what_to_do(world):
    (world.root / "clone").mkdir()
    (world.root / "clone/notes.txt").write_text("mine\n")
    code, out = run(world)
    assert code == 3 and statuses(out)["template"] == "blocked"
    text = "\n".join(out)
    assert "not empty" in text and "pick another --dir" in text
    assert (world.root / "clone/notes.txt").read_text() == "mine\n" and world.count("push") == 0


@pytest.mark.parametrize(
    ("origin", "matches"),
    [
        ("https://github.com/pat/agent-workspace.git", True),
        ("https://github.com/pat/agent-workspace", True),
        ("https://tok@github.com/Pat/Agent-Workspace/", True),
        ("git@github.com:pat/agent-workspace.git", True),
        ("ssh://git@github.com/pat/agent-workspace.git", True),
        ("https://evilgithub.com/pat/agent-workspace", False),
        ("https://github.com.evil.test/pat/agent-workspace", False),
        ("git@evilgithub.com:pat/agent-workspace.git", False),
        ("https://gitlab.com/pat/agent-workspace", False),
        ("https://github.com/pat/other", False),
    ],
)
def test_the_origin_check_pins_the_host(tmp_path, origin, matches):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", origin], check=True)
    assert _bootstrap_github.GhCli().clone_origin_is(tmp_path, "pat/agent-workspace") is matches


def test_package_check_on_a_missing_path_is_one_line_and_exit_2(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(ROOT / "ops/package_skill.py"), "--check", str(tmp_path / "nope.zip")],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2 and "Traceback" not in proc.stderr + proc.stdout
    assert len((proc.stdout + proc.stderr).strip().splitlines()) == 1


def test_the_deploy_key_directory_is_made_under_home_when_there_is_one(world, monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    import tempfile

    made: list[Path] = []
    real = tempfile.mkdtemp

    def spy(*args, **kwargs):
        made.append(Path(real(*args, **kwargs)))
        return made[-1]

    monkeypatch.setattr(tempfile, "mkdtemp", spy)
    assert run(world, "--no-app")[0] == 0
    assert made[0].parent == home and not made[0].exists()


# --- F: final cleanup findings ---------------------------------------------------------------------------


def test_a_no_app_profile_still_names_the_public_engine_and_validates(world):
    assert run(world, "--no-app")[0] == 0
    data = tomllib.loads(_profile_text(world))
    assert data["repo"]["factory"] == _bootstrap_profile.ENGINE_REPO_URL
    assert not _has_app(world)
    profile = _profile.load(str(world.root / "clone/profile.toml"))
    assert profile.repos["factory"] == _bootstrap_profile.ENGINE_REPO_URL


def test_dropping_the_app_keeps_the_engine_repo_in_the_profile(world):
    assert run(world)[0] == 0
    assert run(world, "--no-app")[0] == 0
    assert not _has_app(world)
    assert tomllib.loads(_profile_text(world))["repo"]["factory"] == _bootstrap_profile.ENGINE_REPO_URL


def test_a_key_dir_left_by_a_hard_kill_is_removed_by_the_next_deploy_key_run(world, monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    stale = home / ".agent-factory-key-abc123_x"
    stale.mkdir(mode=0o700)
    (stale / "key").write_text("the private half of a deleted GitHub key")
    lookalike = home / ".agent-factory-keys"  # not the prefix: not ours
    lookalike.mkdir()
    elsewhere = home / "projects" / ".agent-factory-key-abc123_y"  # not in the home directory itself
    elsewhere.mkdir(parents=True)
    assert run(world, "--no-app")[0] == 0
    assert not stale.exists() and lookalike.is_dir() and elsewhere.is_dir()
    assert not list(home.glob(".agent-factory-key-*"))


def test_a_key_dir_lookalike_that_is_a_symlink_is_left_alone(world, monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    target = tmp_path / "precious"
    target.mkdir()
    (target / "keep").write_text("not the bootstrap's")
    (home / ".agent-factory-key-link0000").symlink_to(target)
    assert run(world, "--no-app")[0] == 0
    assert (target / "keep").is_file()


def _app_line(out: list[str]) -> str:
    return next(line for line in out if line.startswith("[") and "] github-app:" in line)


def test_a_crash_after_the_key_is_stored_resumes_with_the_recorded_app(world, monkeypatch):
    from tests import _bootstrap_fakes

    calls = []

    def crash_once(self, registration, repo):
        calls.append(registration.app_id)
        raise _bootstrap_fakes.Interrupted("killed while waiting for the install")

    # Scoped, not `monkeypatch.undo()`: that would also undo the autouse fixtures' patches, the commit
    # identity among them, and the second run would depend on the machine's own git config.
    with monkeypatch.context() as patch:
        patch.setattr(_bootstrap_fakes.FakeGitHub, "app_installed", crash_once)
        code, out = run(world)
    assert code == 3 and calls == ["424242"]
    assert "agent_factory_github_app_key" in world.secrets
    code, out = run(world)
    assert code == 0, out
    assert world.count("register_app") == 1 and len(world.apps) == 1
    assert tomllib.loads(_profile_text(world))["github"]["app_id"] == "424242"


def test_an_app_registered_by_a_run_that_died_before_the_conversion_is_not_registered_twice(world):
    world.apps["agent-factory-pat"] = "111"  # made on GitHub; the run died before it got the key
    code, out = run(world)
    assert code == 3 and world.count("register_app") == 0
    assert statuses(out)["github-app"] == "blocked"
    assert "https://github.com/settings/apps/agent-factory-pat/advanced" in _app_line(out)
    world.apps.clear()  # the person deleted it there
    assert run(world)[0] == 0 and world.count("register_app") == 1


def test_a_recorded_app_whose_key_was_never_stored_names_the_page_to_delete_it_on(world):
    world.interrupt_at = 1 + _events_until(world, "create_secret agent_factory_github_app_key")
    assert run(world)[0] == 3
    world.interrupt_at = None
    code, out = run(world)
    assert code == 3 and statuses(out)["github-app"] == "blocked"
    line = _app_line(out)
    assert "424242" in line and "https://github.com/settings/apps/agent-factory-pat/advanced" in line
    assert run(world)[0] == 3 and world.count("register_app") == 1  # still blocked, never a second App
    world.apps.clear()
    assert run(world)[0] == 0 and world.count("register_app") == 2


def test_an_organizations_app_is_deleted_on_the_organizations_settings_page(world):
    world.org = True
    world.apps["agent-factory-pat"] = "111"
    code, out = run(world)
    assert code == 3
    assert "https://github.com/organizations/pat/settings/apps/agent-factory-pat/advanced" in _app_line(out)


def test_the_install_link_uses_the_slug_github_returned(world):
    world.app_slug = "agent-factory-pat-2"  # GitHub's slug, not the one derived from the name
    world.app_installed = False
    code, out = run(world)
    assert code == 3
    text = "\n".join(out)
    assert "https://github.com/apps/agent-factory-pat-2/installations/new" in text
    assert "https://github.com/apps/agent-factory-pat/installations" not in text
    world.app_installed = True
    code, out = run(world, "--status")
    assert "https://github.com/apps/agent-factory-pat-2/installations/new" in _app_line(out)


def _events_until(world, event: str) -> int:
    """How many changes a full run makes before `event`, measured on a scratch world."""
    probe = World(world.root / "probe")
    probe.root.mkdir()
    run(probe)
    return probe.events.index(event)


def test_the_app_id_and_slug_are_recorded_before_its_key_is_stored(world, monkeypatch):
    from tests import _bootstrap_fakes

    seen = {}
    real = _bootstrap_fakes.FakeFlyte.create_secret

    def spy(self, name, value):
        if name == "agent_factory_github_app_key":
            for key in ("agent-factory.app-pending", "agent-factory.app-slug"):
                seen[key] = subprocess.run(
                    ["git", "config", "--local", "--get", key],
                    cwd=world.root / "clone",
                    capture_output=True,
                    text=True,
                ).stdout.strip()
        return real(self, name, value)

    monkeypatch.setattr(_bootstrap_fakes.FakeFlyte, "create_secret", spy)
    assert run(world)[0] == 0
    assert seen == {"agent-factory.app-pending": "424242", "agent-factory.app-slug": "agent-factory-pat"}


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "expected"),
    [
        (0, "Pat\n", "", True),
        (0, "someone-else\n", "", False),  # App names are global: not the owner's App
        (1, "", "gh: Not Found (HTTP 404)", False),
    ],
)
def test_the_adapter_asks_github_whether_the_owner_has_the_app(returncode, stdout, stderr, expected):
    def fake_run(cmd, **kwargs):
        assert cmd[:3] == ["gh", "api", "apps/agent-factory-pat"]
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

    assert _bootstrap_github.GhCli(run=fake_run).app_exists("pat", "agent-factory-pat") is expected


def test_a_failed_app_lookup_is_not_taken_for_a_missing_app():
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "error connecting to api.github.com")

    with pytest.raises(BootstrapError):
        _bootstrap_github.GhCli(run=fake_run).app_exists("pat", "agent-factory-pat")


def test_status_before_sign_in_shows_the_flyte_steps_as_todo_not_blocked(world):
    code, out = run(world, "--status")
    assert code == 3 and world.events == []
    status = statuses(out)
    assert status["signin"] == "todo" and status["project"] == "todo"
    project = next(line for line in out if "] project:" in line)
    assert "sign in first" in project and "InitializationError" not in "\n".join(out)
    assert "blocked" not in status.values()
    assert status["deploy-key"] == "todo"  # the table goes on past the steps that need Flyte


def test_the_sdk_adapter_says_sign_in_first_when_there_is_no_config(tmp_path):
    settings = Settings(endpoint="flyte.example.com", project="p", workspace="pat/ws", state_uri="s3://b/x")
    flyte_ops = FlyteSdk(settings, config_path=tmp_path / "absent.yaml")
    with pytest.raises(NotSignedIn, match="sign in first"):
        flyte_ops.project_exists()


def test_an_invalid_profile_names_the_file_to_edit(world):
    assert run(world, "--no-app")[0] == 0
    path = world.root / "clone" / "profile.toml"
    path.write_text(path.read_text().replace('uri = "s3://example-bucket/state"', 'uri = "az://c/p"'))
    code, out = run(world, "--no-app")
    assert code == 3 and statuses(out)["profile"] == "blocked", out
    text = next(line for line in out if line.startswith("next:"))
    assert str(path) in text and "edit" in text


def test_an_app_secret_without_a_record_prints_the_delete_command_and_the_apps_page(world):
    assert run(world)[0] == 0
    clone = world.root / "clone"
    for key in ("app-installed", "app-installed-how", "app-slug", "app-pending"):
        subprocess.run(
            ["git", "-C", str(clone), "config", "--local", "--unset-all", f"agent-factory.{key}"], check=False
        )
    path = clone / "profile.toml"
    _bootstrap_profile.drop_app_keys(path)
    world.apps.clear()
    assert "agent_factory_github_app_key" in world.secrets
    code, out = run(world)
    assert code == 3 and statuses(out)["github-app"] == "blocked", out
    line = _app_line(out)
    assert "flyte delete secret agent_factory_github_app_key -p my-agents -d development" in line
    assert "https://github.com/settings/apps/agent-factory-pat/advanced" in line


# =====================================================================================================
# Behaviour of single steps and adapters that a whole run does not reach: each test finishes a setup on
# the fakes (or builds the real adapter over a stubbed SDK) and then drives the one piece it names.
# =====================================================================================================


def _finished(world: World, *extra: str):
    """Run the bootstrap to its end and return the `Context` it ran with, to drive single steps."""
    from tests.test_bootstrap import ARGS

    seen: list[Any] = []
    out: list[str] = []

    def make(settings):
        seen.append(make_context(world, settings, out))
        return seen[-1]

    code = _bootstrap.main([*ARGS, "--dir", str(world.root / "clone"), *extra], make_context=make)
    assert code == 0, out
    return seen[0]


def _commit_all(world: World, message: str = "edit") -> None:
    subprocess.run(["git", "commit", "-qam", message], cwd=world.root / "clone", check=True)


def _git_config(world: World, key: str, value: str) -> None:
    subprocess.run(["git", "config", "--local", key, value], cwd=world.root / "clone", check=True)


def _settings() -> Settings:
    return Settings("flyte.example.test", "p", "pat/ws", "s3://b/p")


# --- the GitHub App, profile, deploy and connect steps -------------------------------------------------


def test_the_profile_step_pushes_without_a_commit_when_the_file_did_not_change(world):
    from flyte_agent_factory._bootstrap_base import DONE
    from flyte_agent_factory._bootstrap_finish import ProfileStep

    ctx = _finished(world)
    commits = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"], cwd=world.root / "clone", capture_output=True, text=True
    ).stdout
    pushes = world.count("push")
    assert ProfileStep().do(ctx).status == DONE
    assert world.count("push") == pushes + 1
    assert (
        subprocess.run(
            ["git", "rev-list", "--count", "HEAD"], cwd=world.root / "clone", capture_output=True, text=True
        ).stdout
        == commits
    )


def test_an_organization_that_may_not_list_installations_falls_back_to_the_receipt(world, monkeypatch):
    from flyte_agent_factory._bootstrap_base import NotAllowed
    from flyte_agent_factory._bootstrap_finish import GithubAppStep
    from tests._bootstrap_fakes import FakeGitHub

    world.org = True
    ctx = _finished(world)

    def refuse(self, owner):
        raise NotAllowed("HTTP 403")

    monkeypatch.setattr(FakeGitHub, "app_installations", refuse)
    how = GithubAppStep()._describe_installation(ctx, "424242")
    assert how is not None and "registered" in how and "not re-checked" in how
    assert GithubAppStep()._describe_installation(ctx, "999") is None  # a receipt for another App is none


def test_an_installed_app_whose_profile_lacks_keys_or_is_unpushed_is_not_done(world):
    from flyte_agent_factory._bootstrap_finish import GithubAppStep

    ctx = _finished(world)
    path = world.root / "clone/profile.toml"
    good = path.read_text()
    path.write_text(good + "\n# a note of mine\n")
    uncommitted = GithubAppStep().check(ctx)
    assert (
        not uncommitted.ok and "is installed" in uncommitted.detail and "profile.toml" in uncommitted.detail
    )
    path.write_text(good)
    assert GithubAppStep().check(ctx).ok
    path.write_text(re.sub(r"^auto = .*\n", "", good, flags=re.MULTILINE))
    lacking = GithubAppStep().check(ctx)
    assert not lacking.ok and "profile.toml lacks deploy.auto" in lacking.detail


def test_an_app_key_that_cannot_be_stored_names_the_app_to_delete(world):
    world.secret_error = "agent_factory_github_app_key"
    code, out = run(world)
    assert code == 3 and statuses(out)["github-app"] == "blocked"
    line = _app_line(out)
    assert "key was lost" in line and "agent-factory-pat" in line and world.count("register_app") == 1


def test_an_app_in_use_whose_secret_went_missing_is_blocked_not_registered_again(world):
    from flyte_agent_factory._bootstrap_base import BLOCKED
    from flyte_agent_factory._bootstrap_finish import GithubAppStep

    ctx = _finished(world)
    del world.secrets["agent_factory_github_app_key"]
    outcome = GithubAppStep().do(ctx)
    assert outcome.status == BLOCKED and "secret agent_factory_github_app_key is missing" in outcome.detail
    assert world.count("register_app") == 1


def test_an_installation_confirmed_on_an_earlier_run_is_adopted_without_asking(world):
    from flyte_agent_factory._bootstrap_base import DONE
    from flyte_agent_factory._bootstrap_finish import GithubAppStep

    world.app_installed = False
    code, _ = run(world)
    assert code == 3 and "app_id" not in (world.root / "clone/profile.toml").read_text()
    ctx = _finished_with_a_person_who_cannot_be_asked(world)
    _git_config(world, "agent-factory.app-installed", "424242")
    _git_config(world, "agent-factory.app-installed-how", "attested")
    outcome = GithubAppStep().do(ctx)
    assert outcome.status == DONE and "installation already confirmed" in outcome.detail
    assert "424242" in (world.root / "clone/profile.toml").read_text()


def _finished_with_a_person_who_cannot_be_asked(world: World):
    from tests.test_bootstrap import ARGS

    def eof(prompt: str) -> str:
        raise EOFError

    seen: list[Any] = []

    def make(settings):
        ctx = make_context(world, settings, [])
        ctx.ask_line = eof
        ctx.ask_secret = eof
        seen.append(ctx)
        return ctx

    _bootstrap.main([*ARGS, "--dir", str(world.root / "clone"), "--status"], make_context=make)
    return seen[0]


def test_an_install_nobody_can_confirm_without_a_terminal_is_a_todo_with_the_link(world):
    from flyte_agent_factory._bootstrap_base import TODO
    from flyte_agent_factory._bootstrap_finish import GithubAppStep

    world.app_installed = False
    assert run(world)[0] == 3
    ctx = _finished_with_a_person_who_cannot_be_asked(world)
    outcome = GithubAppStep().do(ctx)
    assert (
        outcome.status == TODO
        and "https://github.com/apps/agent-factory-pat/installations/new" in outcome.detail
    )
    assert (
        "app-installed"
        not in subprocess.run(
            ["git", "config", "--local", "--list"], cwd=world.root / "clone", capture_output=True, text=True
        ).stdout
    )


def test_a_profile_that_does_not_parse_has_no_conflict_to_report(world):
    from flyte_agent_factory._bootstrap_finish import ProfileStep

    ctx = _finished(world)
    (world.root / "clone/profile.toml").write_text("this is [not toml")
    assert ProfileStep()._find_conflict(ctx) is None
    (world.root / "clone/profile.toml").unlink()
    assert ProfileStep()._find_conflict(ctx) is None


def test_a_profile_missing_managed_keys_is_not_done_and_names_them(world):
    from flyte_agent_factory._bootstrap_finish import ProfileStep

    ctx = _finished(world, "--no-app")
    path = world.root / "clone/profile.toml"
    path.write_text(re.sub(r"(?ms)^\[mcp\].*?(?=^\[|\Z)", "", path.read_text()))
    _commit_all(world)
    check = ProfileStep().check(ctx)
    assert not check.ok and check.detail.startswith("missing keys:") and "mcp.public_url" in check.detail


def test_the_deploy_step_says_what_it_would_do_for_each_backend(world):
    from flyte_agent_factory._bootstrap_finish import _GAP, DeployStep

    ctx = _finished(world)
    assert DeployStep().plan(ctx) == "deploy the work slots and the factory with your profile"
    ctx.settings.backend = "oss"
    assert DeployStep().plan(ctx) == _GAP


def test_a_deploy_that_prints_no_url_leaves_the_url_to_the_next_step(world, monkeypatch):
    from flyte_agent_factory._bootstrap_base import DONE
    from flyte_agent_factory._bootstrap_finish import DeployStep
    from tests._bootstrap_fakes import FakeDeployer

    ctx = _finished(world)
    ctx.facts.clear()
    monkeypatch.setattr(FakeDeployer, "deploy", lambda self, profile, *, work=True: None)
    assert DeployStep().do(ctx).status == DONE
    assert "mcp_url" not in ctx.facts


def test_the_factory_url_step_on_open_source_flyte_stops_with_the_gap(world):
    from flyte_agent_factory._bootstrap_base import BLOCKED, Check
    from flyte_agent_factory._bootstrap_finish import _GAP, McpUrlStep

    ctx = _finished(world)
    ctx.settings.backend = "oss"
    assert McpUrlStep().check(ctx) == Check(False, _GAP)
    outcome = McpUrlStep().do(ctx)
    assert outcome.status == BLOCKED and outcome.detail == _GAP


def test_the_factory_url_step_waits_when_the_app_has_no_public_url_yet(world):
    from flyte_agent_factory._bootstrap_base import TODO
    from flyte_agent_factory._bootstrap_finish import McpUrlStep

    ctx = _finished(world)
    ctx.facts.clear()
    world.mcp_url = None
    outcome = McpUrlStep().do(ctx)
    assert outcome.status == TODO and "no public URL yet" in outcome.detail


def test_the_connect_step_says_what_it_would_print(world):
    from flyte_agent_factory._bootstrap_finish import ConnectStep

    assert "connector URL" in ConnectStep().plan(_finished(world))


# --- the SDK adapter over a stubbed SDK ----------------------------------------------------------------


def _sdk(monkeypatch=None, config_path=None) -> FlyteSdk:
    sdk = FlyteSdk(_settings(), config_path)
    sdk._ready = True
    return sdk


def test_a_missing_flyte_config_has_no_endpoint_and_one_without_one_has_an_empty_endpoint(tmp_path):
    config = tmp_path / "config.yaml"
    assert _sdk(config_path=config).config_endpoint() is None
    config.write_text("admin:\n  insecure: true\n")
    assert _sdk(config_path=config).config_endpoint() == ""


def test_create_config_runs_the_sdk_cli_and_a_local_cluster_gets_insecure(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    local = FlyteSdk(Settings("localhost:30080", "p", "o/r", "s3://b/p"), tmp_path / "a/config.yaml")
    local.create_config()
    remote = FlyteSdk(_settings(), tmp_path / "b/config.yaml")
    remote.create_config()
    assert "--insecure" in calls[0] and "--insecure" not in calls[1]
    assert calls[0][calls[0].index("--endpoint") + 1] == "localhost:30080"
    assert calls[1][-2:] == ["--output", str(tmp_path / "b/config.yaml")]


def test_a_failing_create_config_reports_the_tail_of_what_the_cli_said(tmp_path, monkeypatch):
    noisy = "x" * 2000 + "the real reason\n"
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 2, "", noisy)
    )
    with pytest.raises(BootstrapError, match="flyte create config failed: x+the real reason$") as raised:
        _sdk(config_path=tmp_path / "config.yaml").create_config()
    assert len(str(raised.value)) < 600


def test_without_a_config_file_the_sdk_adapter_cannot_sign_in_without_a_browser(tmp_path):
    assert _sdk(config_path=tmp_path / "absent.yaml")._can_sign_in_without_a_browser() is False


def test_init_loads_the_config_once_and_remembers_it(tmp_path, monkeypatch):
    import flyte

    seen: list[tuple[Any, ...]] = []
    monkeypatch.setattr(flyte, "init_from_config", lambda *a, **k: seen.append((a, k)))
    sdk = FlyteSdk(_settings(), tmp_path / "config.yaml")
    sdk._init()
    sdk._init()
    assert sdk._ready and seen == [((tmp_path / "config.yaml",), {"project": "p", "domain": "development"})]


def test_the_project_and_secret_lookups_treat_only_not_found_as_missing(monkeypatch):
    from flyte import remote

    sdk = _sdk()
    state: dict[str, Any] = {"error": None}

    def get(*args, **kwargs):
        if state["error"]:
            raise state["error"]

    monkeypatch.setattr(remote.Project, "get", get)
    monkeypatch.setattr(remote.Secret, "get", get)
    assert sdk.project_exists() is True and sdk.secret_exists("s") is True
    state["error"] = ConnectError(Code.NOT_FOUND, "no such thing")
    assert sdk.project_exists() is False and sdk.secret_exists("s") is False
    state["error"] = ConnectError(Code.UNAVAILABLE, "the network is down")
    with pytest.raises(ConnectError):
        sdk.project_exists()
    with pytest.raises(ConnectError):
        sdk.secret_exists("s")


def test_create_project_names_it_by_the_projects_id(monkeypatch):
    from flyte import remote

    made: list[dict[str, str]] = []
    monkeypatch.setattr(remote.Project, "create", lambda **kwargs: made.append(kwargs))
    _sdk().create_project()
    assert made == [{"id": "p", "name": "p"}]


def test_the_factory_is_registered_when_its_launch_task_is_listed(monkeypatch):
    from flyte import remote

    listed: list[dict[str, Any]] = []
    tasks: list[str] = []

    def listall(**kwargs):
        listed.append(kwargs)
        return iter(tasks)

    monkeypatch.setattr(remote.Task, "listall", listall)
    assert _sdk().factory_registered() is False
    tasks.append("factory.launch")
    assert _sdk().factory_registered() is True
    assert listed[0] == {"by_task_name": "factory.launch", "limit": 1}


def test_the_factory_url_is_the_apps_endpoint_when_it_has_one(monkeypatch):
    from flyte import remote

    app = type("App", (), {"endpoint": "https://factory-mcp.example.test"})()
    state: dict[str, Any] = {"found": app}

    def get(name):
        assert name == "factory-mcp"
        if isinstance(state["found"], Exception):
            raise state["found"]
        return state["found"]

    monkeypatch.setattr(remote.App, "get", get)
    assert _sdk().factory_mcp_url() == "https://factory-mcp.example.test"
    app.endpoint = ""  # deployed, but no public URL yet
    assert _sdk().factory_mcp_url() is None
    state["found"] = ConnectError(Code.NOT_FOUND, "not deployed")
    assert _sdk().factory_mcp_url() is None
    state["found"] = ConnectError(Code.UNAVAILABLE, "down")
    with pytest.raises(ConnectError):
        _sdk().factory_mcp_url()


def test_the_adapter_can_manage_keys_exactly_when_the_union_plugin_is_there(monkeypatch):
    from flyte_agent_factory import _bootstrap_flyte

    monkeypatch.setattr(_bootstrap_flyte, "_union_plugin_present", lambda: True)
    assert _sdk().can_manage_keys() is True
    monkeypatch.setattr(_bootstrap_flyte, "_union_plugin_present", lambda: False)
    assert _sdk().can_manage_keys() is False


def _union_stub(monkeypatch, **classes: Any) -> None:
    import types

    from flyte_agent_factory import _bootstrap_flyte

    module = types.SimpleNamespace(**classes)
    monkeypatch.setattr(_bootstrap_flyte, "_load_union_remote", lambda: module)


def test_a_refused_api_key_is_reported_by_its_error_class_only(monkeypatch):
    class ApiKey:
        @staticmethod
        def create(name, skip_default_policy_assignments):
            raise ConnectError(Code.UNAVAILABLE, "request body had sentinel-secret")

    _union_stub(monkeypatch, ApiKey=ApiKey)
    with pytest.raises(BootstrapError, match=r"creating API key 'k' failed: ConnectError$") as raised:
        _sdk().create_api_key("k")
    assert "sentinel-secret" not in str(raised.value)


def test_deleting_an_api_key_deletes_it_by_client_id(monkeypatch):
    deleted: list[str] = []

    class ApiKey:
        @staticmethod
        def delete(client_id):
            deleted.append(client_id)

    _union_stub(monkeypatch, ApiKey=ApiKey)
    _sdk().delete_api_key("client-1")
    assert deleted == ["client-1"]


def test_a_policy_or_assignment_failure_that_is_not_a_refusal_surfaces(monkeypatch):
    class Policy:
        @staticmethod
        def get(name):
            raise ConnectError(Code.UNAVAILABLE, "down")

    class Assignment:
        @staticmethod
        def create(creds_subject, policy):
            raise ConnectError(Code.UNAVAILABLE, "down")

    _union_stub(monkeypatch, Policy=Policy, Assignment=Assignment)
    with pytest.raises(ConnectError):
        _sdk().ensure_policy("factory-chat")
    with pytest.raises(ConnectError):
        _sdk().assign_policy("client-1", "factory-chat")


# --- the deploy scripts --------------------------------------------------------------------------------


def test_an_api_key_in_the_environment_means_the_deploy_can_go_ahead():
    assert DeployScripts(env={"_UNION_EAGER_API_KEY": "from-the-person"}).api_key_available() is True


def test_the_key_record_needs_a_clone_to_live_in():
    with pytest.raises(BootstrapError, match="no workspace clone"):
        DeployScripts(env={})._run_git_config("--get-all", "anything")


def test_a_key_that_cannot_be_deleted_at_the_end_is_said_not_raised(monkeypatch, tmp_path):
    said: list[str] = []
    deployer = DeployScripts(env={}, engine_root=tmp_path, say=said.append)
    deployer._key = "made-by-this-run"

    def fail():
        raise RuntimeError("the connection dropped")

    monkeypatch.setattr(deployer, "_delete_recorded_keys", fail)
    deployer.close()
    assert said == ["could not delete the deployment API key 'agent-factory-deploy' (RuntimeError)"]
    assert deployer._key is None  # said once; the record stays for the next run to delete


def test_the_factory_url_a_deploy_script_prints_is_what_the_deploy_returns(tmp_path):
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops/deploy_factory.py").write_text(
        "print('building')\nprint('factory-mcp public URL: https://factory-mcp.example.test')\n"
    )
    said: list[str] = []
    deployer = DeployScripts(env={}, engine_root=tmp_path, say=said.append)
    url = deployer._run_script("deploy_factory.py", tmp_path / "profile.toml", {})
    assert url == "https://factory-mcp.example.test"
    assert said == ["building", "factory-mcp public URL: https://factory-mcp.example.test"]


def test_the_watchdog_stopping_a_script_that_already_exited_is_not_an_error(monkeypatch, tmp_path):
    from flyte_agent_factory import _bootstrap_flyte

    (tmp_path / "ops").mkdir()
    (tmp_path / "ops/deploy_factory.py").write_text("import time\ntime.sleep(30)\n")
    real_killpg = os.killpg

    def killpg_then_gone(pid, sig):
        real_killpg(pid, sig)
        raise ProcessLookupError  # the group ended between the timer firing and the kill

    monkeypatch.setattr(_bootstrap_flyte, "_DEPLOY_SECONDS", 0.2)
    monkeypatch.setattr(os, "killpg", killpg_then_gone)
    deployer = DeployScripts(env={}, engine_root=tmp_path, say=lambda line: None)
    with pytest.raises(BootstrapError, match="was stopped after"):
        deployer._run_script("deploy_factory.py", tmp_path / "profile.toml", {})


# --- the profile file ----------------------------------------------------------------------------------


def test_keys_are_added_after_the_tables_header_even_when_the_file_ends_on_it(tmp_path):
    path = tmp_path / "profile.toml"
    path.write_text('name = "x"\n[mcp]')  # no newline after the header
    _bootstrap_profile.set_mcp_url(path, "https://m.example.test")
    assert path.read_text() == 'name = "x"\n[mcp]\npublic_url = "https://m.example.test"\n'
    assert tomllib.loads(path.read_text())["mcp"] == {"public_url": "https://m.example.test"}


def test_a_missing_mcp_table_is_added_whole(tmp_path):
    path = tmp_path / "profile.toml"
    path.write_text('name = "x"\n')
    _bootstrap_profile.set_mcp_url(path, "https://m.example.test")
    assert tomllib.loads(path.read_text()) == {"name": "x", "mcp": {"public_url": "https://m.example.test"}}


def test_a_table_written_in_a_form_the_tool_cannot_edit_is_refused_untouched(tmp_path):
    path = tmp_path / "profile.toml"
    text = 'name = "x"\nmcp = { other = 1 }\n'
    path.write_text(text)
    with pytest.raises(BootstrapError, match=r"\[mcp\] in .* is written in a form this tool will not edit"):
        _bootstrap_profile.set_mcp_url(path, "https://m.example.test")
    assert path.read_text() == text


def test_a_profile_that_is_not_toml_is_not_filled_in(tmp_path):
    path = tmp_path / "profile.toml"
    path.write_text("name = [unclosed")
    with pytest.raises(BootstrapError, match="is not valid TOML .*fix it and run this again"):
        _bootstrap_profile.fill_missing(path, _settings(), None)
    assert path.read_text() == "name = [unclosed"


def test_a_setting_the_file_does_not_have_is_no_conflict(tmp_path):
    path = tmp_path / "profile.toml"
    path.write_text('[backend]\nkind = "union"\nproject = "other"\n')
    assert _bootstrap_profile.find_conflicts(path, _settings()) == {"backend.project": ("other", "p")}


def test_a_missing_or_unreadable_profile_has_no_app_keys(tmp_path):
    assert _bootstrap_profile.find_app_keys(tmp_path / "absent.toml") == []
    bad = tmp_path / "bad.toml"
    bad.write_text("= not toml")
    assert _bootstrap_profile.find_app_keys(bad) == []


def test_dropping_app_keys_leaves_a_file_without_the_other_tables_alone(tmp_path):
    path = tmp_path / "profile.toml"
    path.write_text('name = "x"\n[github]\napp_id = "1"\nprivate_key_secret = "s"\n')
    assert _bootstrap_profile.drop_app_keys(path) == ["github.app_id", "github.private_key_secret"]
    assert path.read_text() == 'name = "x"\n'


# --- the sign-in, project, template, ruleset, deploy-key and engine-token steps -------------------------


def test_any_failure_of_the_sign_in_call_is_reported_as_not_signed_in(world, monkeypatch):
    from flyte_agent_factory._bootstrap_steps import SignInStep
    from tests._bootstrap_fakes import FakeFlyte

    ctx = _finished(world)

    def fail(self):
        raise OSError("the keyring is locked")

    monkeypatch.setattr(FakeFlyte, "sign_in", fail)
    check = SignInStep().check(ctx)
    assert not check.ok and check.detail == "not signed in (OSError)"


def test_a_flyte_config_for_another_endpoint_is_never_overwritten(world):
    from flyte_agent_factory._bootstrap_base import BLOCKED
    from flyte_agent_factory._bootstrap_steps import SignInStep

    ctx = _finished(world)
    config = world.root / "flyte-config.yaml"
    config.write_text("admin:\n  endpoint: dns:///other.example.test\n")
    outcome = SignInStep().do(ctx)
    assert outcome.status == BLOCKED and "points at dns:///other.example.test" in outcome.detail
    assert config.read_text() == "admin:\n  endpoint: dns:///other.example.test\n"


def test_the_project_step_plans_the_project_it_would_create(world):
    from flyte_agent_factory._bootstrap_steps import ProjectStep

    assert ProjectStep().plan(_finished(world)) == "create Flyte project my-agents"


def test_a_clone_without_the_template_or_with_the_owner_placeholder_is_not_done(world):
    import shutil

    from flyte_agent_factory._bootstrap_steps import TemplateStep

    ctx = _finished(world)
    clone = world.root / "clone"
    codeowners = clone / ".github/CODEOWNERS"
    good = codeowners.read_text()
    codeowners.write_text(good.replace("@pat", "@OWNER"))
    check = TemplateStep().check(ctx)
    assert not check.ok and check.detail.endswith("CODEOWNERS still names @OWNER")
    codeowners.write_text(good)
    shutil.rmtree(clone / "standards")
    check = TemplateStep().check(ctx)
    assert not check.ok and check.detail.endswith("the template is not in the clone")


def test_template_files_changed_in_the_clone_are_committed_as_the_person_and_pushed(world):
    from flyte_agent_factory._bootstrap_base import DONE
    from flyte_agent_factory._bootstrap_steps import TemplateStep

    ctx = _finished(world)
    (world.root / "clone/standards/added-by-hand.md").write_text("a note\n")
    pushes = world.count("push")
    assert TemplateStep().do(ctx).status == DONE
    from tests.test_bootstrap import remote_log

    assert remote_log(world)[0] == "Pat Example|Workspace from template"
    assert world.count("push") == pushes + 1


@pytest.mark.parametrize(
    ("error", "detail"),
    [
        (FileExistsError("profile.example.toml exists"), "profile.example.toml exists; pick another --dir"),
        (RuntimeError("git said no"), "git said no"),
    ],
)
def test_a_template_that_cannot_be_written_blocks_with_the_reason(world, monkeypatch, error, detail):
    from flyte_agent_factory import _workspace
    from flyte_agent_factory._bootstrap_base import BLOCKED
    from flyte_agent_factory._bootstrap_steps import TemplateStep

    ctx = _finished(world)
    (world.root / "clone/profile.example.toml").unlink()

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(_workspace, "create", fail)
    pushes = world.count("push")
    outcome = TemplateStep().do(ctx)
    assert (outcome.status, outcome.detail) == (BLOCKED, detail) and world.count("push") == pushes


def test_a_ruleset_that_cannot_be_read_is_reported_not_raised(world, monkeypatch):
    from flyte_agent_factory._bootstrap_base import NotAllowed
    from flyte_agent_factory._bootstrap_steps import RulesetStep
    from tests._bootstrap_fakes import FakeGitHub

    ctx = _finished(world)

    def refuse(self, repo, name):
        raise NotAllowed("HTTP 403")

    monkeypatch.setattr(FakeGitHub, "ruleset_exists", refuse)
    check = RulesetStep().check(ctx)
    assert not check.ok and check.detail == "cannot read rulesets on this repository"


def test_a_process_that_is_somebody_elses_still_counts_as_alive(monkeypatch):
    from flyte_agent_factory import _bootstrap_steps

    def kill(pid, sig):
        raise PermissionError

    monkeypatch.setattr(os, "kill", kill)
    assert _bootstrap_steps._process_is_alive(1) is True


def test_a_key_directory_that_vanishes_during_the_sweep_is_skipped(tmp_path, monkeypatch):
    from flyte_agent_factory._bootstrap_steps import _remove_stale_key_dirs

    vanishing = tmp_path / ".agent-factory-key-2147483646-gone"
    stale = tmp_path / ".agent-factory-key-2147483646-stale"
    vanishing.mkdir(mode=0o700)
    stale.mkdir(mode=0o700)
    real = Path.lstat

    def lstat(self, **kwargs):
        if self.name == vanishing.name:
            raise FileNotFoundError(self.name)
        return real(self, **kwargs)

    monkeypatch.setattr(Path, "lstat", lstat)
    _remove_stale_key_dirs(tmp_path)
    assert vanishing.exists() and not stale.exists()


def test_the_deploy_key_is_made_in_the_temp_dir_when_there_is_no_home(world, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "no-such-home"))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    code, out = run(world, "--no-app")
    assert code == 0, out
    assert "agent_factory_deploy_key" in world.secrets
    assert not list(tmp_path.glob(".agent-factory-key-*"))  # removed again
    assert not (tmp_path / "no-such-home").exists()


def test_a_failing_ssh_keygen_stops_the_deploy_key_before_anything_reaches_github(world, monkeypatch):
    real = subprocess.run

    def keygen_fails(cmd, **kwargs):
        if cmd[0] == "ssh-keygen":
            return subprocess.CompletedProcess(cmd, 1, "", "no entropy")
        return real(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", keygen_fails)
    code, out = run(world, "--no-app")
    assert code == 3 and any(
        line.startswith("[blocked] deploy-key") and "ssh-keygen failed (1)" in line for line in out
    )
    assert world.count("add_deploy_key") == 0 and not world.deploy_keys


def test_the_engine_token_step_plans_the_secret_it_would_store(world):
    from flyte_agent_factory._bootstrap_steps import EngineTokenStep

    plan = EngineTokenStep().plan(_finished(world))
    assert "claude setup-token" in plan and "secret" in plan


def test_no_terminal_to_ask_for_the_token_on_is_a_todo_with_what_to_do(world):
    from flyte_agent_factory._bootstrap_base import SECRET_CLAUDE, TODO
    from flyte_agent_factory._bootstrap_steps import EngineTokenStep

    ctx = _finished(world)
    del world.secrets[SECRET_CLAUDE]

    def no_terminal(prompt: str) -> str:
        raise EOFError

    ctx.ask_secret = no_terminal
    outcome = EngineTokenStep().do(ctx)
    assert outcome.status == TODO and "claude setup-token" in outcome.detail
    assert SECRET_CLAUDE not in world.secrets


def test_a_token_already_stored_is_not_asked_for_again(world):
    from flyte_agent_factory._bootstrap_base import DONE
    from flyte_agent_factory._bootstrap_steps import EngineTokenStep

    ctx = _finished(world)
    ctx.ask_secret = lambda prompt: pytest.fail("asked for a token that is already stored")
    before = dict(world.secrets)
    assert EngineTokenStep().do(ctx).status == DONE and world.secrets == before


def test_signing_in_again_with_the_config_for_this_endpoint_keeps_the_config(world):
    from flyte_agent_factory._bootstrap_base import DONE
    from flyte_agent_factory._bootstrap_steps import SignInStep

    ctx = _finished(world)
    config = world.root / "flyte-config.yaml"
    before = config.read_text()
    events = list(world.events)
    outcome = SignInStep().do(ctx)
    assert outcome.status == DONE and config.read_text() == before and world.events == events
