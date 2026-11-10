"""Factory deploys must never replace the agent's own environment."""

import dataclasses
import importlib.util
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

SPEC = importlib.util.spec_from_file_location(
    "deploy_factory", Path(__file__).resolve().parents[1] / "ops/deploy_factory.py"
)
assert SPEC is not None and SPEC.loader is not None
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


def _collect_deployed_tasks():
    from flyte_agent_factory import (
        _bench_flow,
        _deployer,
        _factory,
        _factory_mcp,
        _loop_flow,
        _reconcile,
        _webhook,
        agent,
    )

    # Every root a factory or slot deploy sends (_deploy.FACTORY_CHILD, the slot recipe), with dependencies.
    # factory-issues is a dependency only under some profiles, so it is a root here.
    pending = [
        _factory.env,
        _factory_mcp.app,
        _deployer.env,
        _reconcile.env,
        _webhook.env,
        _webhook.issues_env,
    ]
    pending += [_bench_flow.env, _loop_flow.env, agent.env, agent.subagent_env, agent.setup_env]
    envs = {}
    while pending:
        env = pending.pop()
        if env.name not in envs:
            envs[env.name] = env
            pending.extend(env.depends_on)
    return {name: task for env in envs.values() for name, task in getattr(env, "tasks", {}).items()}


DEPLOYED_TASKS = _collect_deployed_tasks()


def test_deployed_tasks_cover_every_task_environment():
    envs = {name.split(".")[0] for name in DEPLOYED_TASKS}
    assert envs == {
        "agent-factory",
        "agent-factory-subagent",
        "factory",
        "factory-bench",
        "factory-bench-check",
        "factory-deployer",
        "factory-issues",
        "factory-loop",
        "factory-reconcile",
        "factory-setup",
        "gardening-scan",
    }
    assert "factory.credential_link" in DEPLOYED_TASKS


@pytest.mark.parametrize("task_name", sorted(DEPLOYED_TASKS))
async def test_deployed_task_defaults_serialize(task_name):
    from flyte._internal.runtime.convert import convert_upload_default_inputs

    task = DEPLOYED_TASKS[task_name]
    # A dry-run deploy does not run this conversion, so importing environments alone misses bad defaults.
    defaults = await convert_upload_default_inputs(task.interface)
    assert {param.name for param in defaults} == {
        name for name, (_, default) in task.interface.inputs.items() if default is not inspect.Parameter.empty
    }


async def test_credential_link_hours_default_is_a_float_literal():
    from flyte._internal.runtime.convert import convert_upload_default_inputs

    defaults = await convert_upload_default_inputs(DEPLOYED_TASKS["factory.credential_link"].interface)
    (hours,) = [param for param in defaults if param.name == "hours"]
    assert hours.parameter.default.scalar.primitive.WhichOneof("value") == "float_value"
    assert hours.parameter.default.scalar.primitive.float_value == 4.0


def test_only_factory_environments():
    assert deploy.ALLOWED == {"factory", "factory-mcp", "factory-deployer", "gardening-scan"}
    deploy.check_environments(
        SimpleNamespace(name="factory", depends_on=[]), SimpleNamespace(name="factory-mcp", depends_on=[])
    )
    with pytest.raises(SystemExit, match="refusing"):
        deploy.check_environments(SimpleNamespace(name="agent-own", depends_on=[]))
    with pytest.raises(SystemExit, match="refusing"):
        deploy.check_environments(
            SimpleNamespace(name="factory", depends_on=[SimpleNamespace(name="agent-own", depends_on=[])])
        )


def test_deploy_uses_a_detached_ref_and_cleans_up(monkeypatch, tmp_path):
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        return SimpleNamespace(stdout="abc123\n", returncode=0)

    monkeypatch.setattr(deploy.subprocess, "run", run)
    monkeypatch.setenv("FACTORY_MCP_PUBLIC_URL", "https://factory.example")
    assert deploy.main(["--ref", "reviewed-ref", "--dry-run"]) == 0
    assert any("rev-parse" in c and "reviewed-ref^{commit}" in c for c, _ in calls)
    assert any("worktree" in c and "--detach" in c for c, _ in calls)
    assert any("worktree" in c and "remove" in c for c, _ in calls)
    child, kwargs = next((c, k) for c, k in calls if "-c" in c)
    assert child[child.index(deploy._CHILD) + 2] == "1"
    assert "FACTORY_MCP_GOOGLE_SECRETS" not in kwargs["env"]
    assert kwargs["env"]["PYTHONPATH"].endswith("/tree/src")


def test_cleanup_on_failed_deploy(monkeypatch):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(stdout="abc123\n", returncode=7 if "-c" in argv else 0)

    monkeypatch.setattr(deploy.subprocess, "run", run)
    monkeypatch.setenv("FACTORY_MCP_PUBLIC_URL", "https://factory.example")
    assert deploy.main([]) == 7
    assert "remove" in calls[-1]


@pytest.mark.parametrize("dry", [False, True])
def test_child_deploys_only_factory_and_prints_public_endpoint(monkeypatch, capsys, dry):
    import sys

    import flyte

    root = Path(__file__).resolve().parents[1] / "src"
    got: dict[str, Any] = {}
    monkeypatch.setenv("_UNION_EAGER_API_KEY", "operator-key")
    monkeypatch.setattr(flyte, "init_from_api_key", lambda **kw: got.update(init=kw))
    handle = SimpleNamespace(watch=lambda **kw: SimpleNamespace(endpoint="https://public.example"))
    deployed = SimpleNamespace(get_name=lambda: "factory-mcp", deployed_app=handle)

    def deploy_envs(*envs, **kw):
        got.update(names={env.name for env in envs}, options=kw)
        return [SimpleNamespace(summary_repr=lambda: "deployed", envs={"factory-mcp": deployed})]

    monkeypatch.setattr(flyte, "deploy", deploy_envs)
    monkeypatch.setattr(sys, "argv", ["child", str(root), "1" if dry else "0", "project", "domain"])
    monkeypatch.syspath_prepend(str(root.parent / "ops"))
    exec(deploy._CHILD, {})
    assert got["names"] == {"factory", "factory-mcp", "factory-deployer"}
    assert got["options"] == {"dry_run": dry}
    assert got["init"] == {
        "api_key": "operator-key",
        "project": "project",
        "domain": "domain",
        "root_dir": root,
        "image_builder": "remote",
    }
    output = capsys.readouterr().out
    assert "operator-key" not in output
    if dry:
        assert "public URL" not in output
    else:
        assert "factory-mcp public URL: https://public.example" in output
        assert "Connector URL: https://public.example/flyte-mcp/mcp" in output


def test_localhost_requires_explicit_deploy_flag(monkeypatch):
    monkeypatch.setenv("FACTORY_MCP_ALLOW_LOCALHOST", "1")
    monkeypatch.setattr(deploy.subprocess, "run", lambda *a, **kw: pytest.fail("must refuse before git"))
    with pytest.raises(SystemExit):
        deploy.main([])


def test_only_known_factory_settings_forwarded(monkeypatch):
    calls = []
    monkeypatch.setenv("FACTORY_MCP_UNREVIEWED_SECRET", "must-not-copy")
    monkeypatch.setenv("FACTORY_MCP_ALLOW_LOCALHOST", "1")

    def run(argv, **kw):
        calls.append((argv, kw))
        return SimpleNamespace(stdout="abc123\n", returncode=0)

    monkeypatch.setattr(deploy.subprocess, "run", run)
    assert deploy.main(["--allow-localhost", "--dry-run"]) == 0
    env = next(kw["env"] for argv, kw in calls if "-c" in argv)
    assert "FACTORY_MCP_UNREVIEWED_SECRET" not in env
    assert env["FACTORY_MCP_ALLOW_LOCALHOST"] == "1"


def test_setup_connects_with_a_project_scoped_union_api_key():
    setup = (Path(__file__).resolve().parents[1] / "docs/factory-mcp-setup.md").read_text()
    assert "--value" not in setup
    assert "Authorization: Bearer" in setup
    assert "flyte create api-key --name" in setup
    assert "flyte create assignment --creds-subject" in setup
    assert "flyte delete assignment --creds-subject <name> --policy contributor" in setup
    assert "Google" not in setup


def test_child_includes_configured_webhook(monkeypatch, capsys):
    import dataclasses
    import sys

    import flyte
    import flyte.remote

    from flyte_agent_factory import _deployer, _profile

    profile = dataclasses.replace(
        _deployer.PROFILE,
        github=dataclasses.replace(_deployer.PROFILE.github, webhook_secret="webhook-key"),
    )
    monkeypatch.setattr(_profile, "require", lambda: profile)
    monkeypatch.setattr(_deployer, "PROFILE", profile)
    root = Path(__file__).resolve().parents[1] / "src"
    monkeypatch.setenv("_UNION_EAGER_API_KEY", "operator-key")
    monkeypatch.setattr(flyte, "init_from_api_key", lambda **kwargs: None)
    got = {}

    def deploy_envs(*envs, **kwargs):
        got["names"] = {env.name for env in envs}
        return []

    monkeypatch.setattr(flyte, "deploy", deploy_envs)
    monkeypatch.setattr(sys, "argv", ["child", str(root), "1", "project", "domain"])
    exec(deploy._CHILD, {})
    assert got["names"] == {"factory", "factory-mcp", "factory-deployer", "factory-webhook"}


@pytest.mark.parametrize("endpoint", ["https://hook.example", ""])
def test_child_prints_the_webhook_url_without_waiting_for_the_app(monkeypatch, capsys, endpoint):
    import dataclasses
    import sys

    import flyte
    import flyte.remote

    from flyte_agent_factory import _deployer, _profile

    profile = dataclasses.replace(
        _deployer.PROFILE,
        github=dataclasses.replace(_deployer.PROFILE.github, webhook_secret="webhook-key"),
    )
    monkeypatch.setattr(_profile, "require", lambda: profile)
    monkeypatch.setattr(_deployer, "PROFILE", profile)
    root = Path(__file__).resolve().parents[1] / "src"
    monkeypatch.setenv("_UNION_EAGER_API_KEY", "operator-key")
    monkeypatch.setattr(flyte, "init_from_api_key", lambda **kwargs: None)
    # Waiting for activation could hang the deploy on an app whose secret does not exist yet.
    app = SimpleNamespace(endpoint=endpoint, watch=lambda **kw: pytest.fail("must not wait for the webhook"))
    deployed = SimpleNamespace(get_name=lambda: "factory-webhook", deployed_app=app)
    monkeypatch.setattr(
        flyte,
        "deploy",
        lambda *envs, **kw: [SimpleNamespace(summary_repr=lambda: "deployed", envs={"w": deployed})],
    )
    monkeypatch.setattr(sys, "argv", ["child", str(root), "0", "project", "domain"])
    # A real deploy also deletes the retired issue_outbox trigger (state step 1).
    monkeypatch.setattr(flyte.remote.Trigger, "delete", lambda **kw: None)
    exec(deploy._CHILD, {})
    output = capsys.readouterr().out
    assert "retired trigger issue_outbox (factory-issues.drain_outbox) is deleted" in output
    if endpoint:
        assert "GitHub webhook URL: https://hook.example/github" in output
    else:
        assert "factory-webhook has no public URL yet; see the console's Apps page" in output


@pytest.mark.parametrize("enabled", [False, True])
def test_loop_environment_is_registered_only_when_enabled(monkeypatch, enabled):
    import sys
    from dataclasses import replace

    import flyte

    from flyte_agent_factory import _deploy, _deployer, _profile

    profile = replace(_profile.require(), loop=_profile.Loop(enabled=enabled, repo="acme/factory"))
    monkeypatch.setattr(_deployer, "PROFILE", profile)
    monkeypatch.setattr(_profile, "require", lambda: profile)
    names: set[str] = set()
    monkeypatch.setenv("_UNION_EAGER_API_KEY", "operator-key")
    monkeypatch.setattr(flyte, "init_from_api_key", lambda **kwargs: None)

    def deploy_envs(*envs, **kwargs):
        names.update(env.name for env in envs)
        return []

    monkeypatch.setattr(flyte, "deploy", deploy_envs)
    root = Path(__file__).resolve().parents[1] / "src"
    monkeypatch.setattr(sys, "argv", ["child", str(root), "1", "project", "domain"])
    exec(_deploy.FACTORY_CHILD, {})
    assert ("factory-loop" in names) is enabled
    assert ("factory-loop" in _deploy.factory_envs(profile)) is enabled


def test_planner_deploy_is_blocked_before_any_child_runs(monkeypatch):
    from flyte_agent_factory import _deploy, _profile

    profile = dataclasses.replace(_profile.require(), planner=_profile.Planner(admission="planner"))
    monkeypatch.setattr(_deploy, "_run_child", lambda *a, **kw: pytest.fail("cutover must be refused"))
    with pytest.raises(_deploy.Refused, match="stop-before-start"):
        _deploy.deploy_factory(profile, Path("."), project="test", domain="test", env={})
