"""The tasks the package registers, per TaskEnvironment, as one table.

The split of the agent into several environments kept every task except the Claude bootstrap-login task,
which was removed with the per-slot logins. The Codex one came back, because a personal ChatGPT plan has no
access token and signs in only with a device code. A task added or dropped changes this table on purpose.
"""

import importlib
import pkgutil

import flyte

import flyte_agent_factory

EXPECTED = {
    "agent-factory": ["agent-factory.project"],
    "agent-factory-subagent": ["agent-factory-subagent.subagent"],
    "factory": [
        "factory.browser_link",
        "factory.credential_link",
        "factory.credentials",
        "factory.garden_weekly",
        "factory.get_reserve",
        "factory.launch",
        "factory.plan_pass",
        "factory.queue_cancel",
        "factory.queue_list",
        "factory.read_login_meter",
        "factory.rotate_token",
        "factory.set_reserve",
        "factory.status",
        "factory.sweep",
    ],
    "factory-bench": [
        "factory-bench.benchmark",
        "factory-bench.collect_labels",
        "factory-bench.deploy_engine",
        "factory-bench.read_login_meter",
        "factory-bench.run_case",
    ],
    "factory-bench-check": ["factory-bench-check.hidden_check"],
    "factory-deployer": ["factory-deployer.deploy_if_changed", "factory-deployer.deploy_pair"],
    "factory-issues": ["factory-issues.process", "factory-issues.wake_loop"],
    "factory-loop": ["factory-loop.generate_backlog", "factory-loop.loop_run"],
    "factory-reconcile": ["factory-reconcile.reconcile"],
    "factory-setup": ["factory-setup.codex_bootstrap_login", "factory-setup.gcp_bootstrap"],
    "gardening-scan": ["gardening-scan.list_targets", "gardening-scan.scan_target"],
    "test-lanes": ["test-lane"],
    "test-lanes-on-demand": ["test-lane-on-demand"],
    "voice-factory": ["voice-factory.train"],
}


def _registered() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for info in pkgutil.iter_modules(flyte_agent_factory.__path__):
        module = importlib.import_module(f"flyte_agent_factory.{info.name}")
        for value in vars(module).values():
            if isinstance(value, flyte.TaskEnvironment):
                found[value.name] = sorted(value.tasks)
    return dict(sorted(found.items()))


def test_the_package_registers_exactly_these_tasks():
    assert _registered() == EXPECTED


def test_only_the_codex_bootstrap_login_task_is_back():
    names = {task for tasks in _registered().values() for task in tasks}
    assert {name for name in names if "bootstrap_login" in name} == {"factory-setup.codex_bootstrap_login"}
