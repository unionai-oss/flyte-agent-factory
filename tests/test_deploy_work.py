"""The scripted work-slot deploy may only ever produce a work-slot deploy."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "deploy_work", Path(__file__).resolve().parents[1] / "ops" / "deploy_work.py"
)
assert SPEC is not None and SPEC.loader is not None
deploy_work = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy_work)


def test_recipe_overrides_a_hostile_caller_environment():
    env = deploy_work.recipe_env(
        {
            "AGENT_FACTORY_ENV_NAME": "agent-own",
            "AGENT_FACTORY_CLAUDE_TOKEN_SECRET": "token_own",
            "AGENT_FACTORY_SECRETS": "own_deploy_key=GIT_DEPLOY_KEY",
        }
    )
    deploy_work.check_recipe(env)  # does not raise
    assert env["AGENT_FACTORY_ENV_NAME"] == "agent-one"
    assert env["AGENT_FACTORY_CLAUDE_TOKEN_SECRET"] == "claude_token_x"
    assert "own_deploy_key" not in env["AGENT_FACTORY_SECRETS"]


@pytest.mark.parametrize("slot", ["agent-one", "agent-two"])
@pytest.mark.parametrize(
    ("key", "value", "why"),
    [
        ("AGENT_FACTORY_ENV_NAME", "agent-own", "env name"),
        ("AGENT_FACTORY_ENV_NAME", "claude-agent", "env name"),
        ("AGENT_FACTORY_CLAUDE_TOKEN_SECRET", "token_own", "must run on claude_token_x"),
        ("AGENT_FACTORY_SUBAGENT_ENV_NAME", "sub-x", "sub-agents must be"),
    ],
)
def test_anything_but_a_work_slot_is_refused(slot, key, value, why):
    env = deploy_work.recipe_env({}, slot=slot)
    env[key] = value
    with pytest.raises(SystemExit, match=why):
        deploy_work.check_recipe(env)


def test_every_slot_runs_on_the_one_work_token():
    """One work token: the slots share it, and each sub-agent env is still its own."""
    tokens = {
        deploy_work.recipe_env({}, slot=slot)["AGENT_FACTORY_CLAUDE_TOKEN_SECRET"]
        for slot in deploy_work.SLOTS
    }
    assert tokens == {"claude_token_x"}
    subs = list(deploy_work.SLOTS.values())
    assert len(set(subs)) == len(subs)


def test_only_work_slots_and_their_subagents_may_be_deployed():
    assert {"agent-one", "sub-one", "agent-two", "sub-two", "factory-setup"} == deploy_work.ALLOWED
    assert "agent-own" not in deploy_work.ALLOWED


def test_the_operator_launches_into_exactly_the_deployed_slots():
    spec = importlib.util.spec_from_file_location(
        "operate_slots", Path(__file__).resolve().parents[1] / "ops" / "operate.py"
    )
    assert spec is not None and spec.loader is not None
    operate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(operate)
    assert list(operate.SLOTS) == [f"{name}.project" for name in deploy_work.SLOTS]


def test_recipe_matches_the_profile_default_secrets():
    """Drift guard: the README's `agent_secrets` export is the recipe's, as deployed by hand before."""
    root = Path(__file__).resolve().parents[1]
    got = subprocess.run(
        [sys.executable, "-m", "flyte_agent_factory._profile", "agent_secrets"],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root / "src")},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert deploy_work.RECIPE["AGENT_FACTORY_SECRETS"] == got
