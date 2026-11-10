"""The grant server's life inside `project`: who gets its address, who does not, and how it ends."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
import urllib.error
import urllib.request
from types import SimpleNamespace
from typing import Any

import pytest

from flyte_agent_factory import (
    _author,
    _bridge,
    _grants_supervisor,
    _inbox,
    _secret_env,
    _session_config,
    _session_setup,
    _timeline,
    agent,
)
from flyte_agent_factory._grants import TOKEN_ENV, URL_ENV, GrantStore
from tests._wait import wait_for
from tests.test_grants import SECRET, TOKEN
from tests.test_grants_supervisor import everything_written, seal
from tests.test_stop_before_session import harness, until  # noqa: F401 -- `harness` is a fixture requested by name


def fetch(url: str, token: str, path: str) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(url + path, headers={"Authorization": token})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


async def start_project(request, monkeypatch) -> tuple[asyncio.Task[Any], dict[str, str], SimpleNamespace]:
    """Run `project` until the session has started; return the task, the session's env and the proj."""
    proj = request.getfixturevalue("harness")
    seen: dict[str, str] = {}
    original = _author._start_session

    async def spy(*args, **kwargs):
        seen.update(kwargs["env"])
        return await original(*args, **kwargs)

    monkeypatch.setattr(_author, "_start_session", spy)
    run = asyncio.create_task(agent.project.func("brief", browser=False))
    await until(lambda: seen)
    proj.session_may_start.set()
    await until(lambda: proj.session_started)
    return run, seen, proj


async def test_only_the_session_gets_the_endpoint_and_nothing_else_inherits_it(request, monkeypatch):
    run, env, proj = await start_project(request, monkeypatch)
    store: GrantStore = proj.timeline.credentials
    assert env[URL_ENV].startswith("http://127.0.0.1:") and env[TOKEN_ENV]
    assert URL_ENV not in os.environ and TOKEN_ENV not in os.environ
    child = _secret_env.child_env()
    assert URL_ENV not in child and TOKEN_ENV not in child
    assert URL_ENV not in _session_setup._session_env(dict(os.environ))
    assert fetch(env[URL_ENV], env[TOKEN_ENV], "/v1/grants")[0] == 200
    assert store.project == _session_config._project_name or store.project
    await proj.messages.put("/stop")
    await asyncio.wait_for(run, 30)


async def test_the_project_ending_closes_the_store_the_endpoint_and_redaction(request, monkeypatch):
    run, env, proj = await start_project(request, monkeypatch)
    store: GrantStore = proj.timeline.credentials
    await proj.messages.put(seal(store))
    await wait_for(lambda: store.grants())
    assert any(value == SECRET for value in store.live_values())
    assert SECRET not in _bridge.redact_tokens(f"x {SECRET} y")
    await proj.messages.put("/stop")
    await asyncio.wait_for(run, 30)

    [grant] = store.grants()
    assert (grant.state, grant.reason) == ("ended", "the project ended")
    with pytest.raises(urllib.error.URLError):
        fetch(env[URL_ENV], env[TOKEN_ENV], "/v1/grants")
    assert store.live_values() == ()
    assert not any(getattr(source, "__self__", None) is store for source in _bridge._redaction_sources)
    # Told while the session was up; never the value, never the sealed text.
    told = [text for text in proj.submitted if text.startswith("Credential notice:")]
    assert told and all(SECRET not in text and TOKEN not in text and "fgc1." not in text for text in told)
    assert not [text for text in proj.submitted if "fgc1." in text]


async def test_a_restart_tells_the_session_what_ended_with_the_old_process(request, monkeypatch):
    state = _session_config.STATE_ROOT / "bridge"
    state.mkdir(parents=True, exist_ok=True)
    now = time.time()
    (state / "grants.json").write_text(
        json.dumps(
            {
                "grants": [
                    {
                        "name": "aws-marketplace",
                        "kind": "aws",
                        "granted_at": now - 60,
                        "expires_at": now + 3600,
                        "state": "active",
                        "reason": "",
                        "region": "",
                    }
                ]
            }
        )
    )
    run, _, proj = await start_project(request, monkeypatch)
    await until(lambda: any("did not survive the project restart" in text for text in proj.submitted))
    [grant] = proj.timeline.credentials.grants()
    assert (grant.state, grant.reason) == ("ended", "the project restarted")
    await proj.messages.put("/stop")
    await asyncio.wait_for(run, 30)


async def test_the_report_carries_the_key_and_the_grants_metadata(request, monkeypatch):
    run, _, proj = await start_project(request, monkeypatch)
    timeline = proj.timeline
    store: GrantStore = timeline.credentials
    await proj.messages.put(seal(store))
    await wait_for(lambda: store.grants())
    section = timeline.snapshot()["credentials"]
    assert section["key"] == store.key.public_b64 and section["kid"] == store.key.kid
    assert [g["name"] for g in section["grants"]] == ["aws-marketplace"]
    assert SECRET not in json.dumps(section) and TOKEN not in json.dumps(section)
    await proj.messages.put("/stop")
    await asyncio.wait_for(run, 30)


def test_a_sub_agent_pod_has_no_grant_command_server_or_variable(tmp_path):
    _session_setup._install_bridge_commands(tmp_path)
    assert not (tmp_path / "credential-grant").exists()
    _session_setup._install_bridge_commands(tmp_path, grants=True)
    assert "flyte_agent_factory._grants_cli" in (tmp_path / "credential-grant").read_text()
    _session_setup._install_bridge_commands(tmp_path)
    assert not (tmp_path / "credential-grant").exists(), (
        "a pod that does not hold grants removes a stale command"
    )
    assert "_grants_supervisor" not in inspect.getsource(agent.subagent.func)
    assert "grants=True" not in inspect.getsource(agent.subagent.func)
    for environment in (agent.subagent_env, agent.setup_env):
        assert not [name for name in (environment.env_vars or {}) if "GRANTS" in name]
        assert not [secret for secret in _secret_list(environment.secrets) if "GRANT" in str(secret).upper()]


# --- two projects at once ------------------------------------------------------------------------------


@pytest.fixture
def two(tmp_path, monkeypatch, bridge_root):
    workspace = tmp_path / "ws"
    (workspace / _session_config._INBOX_DIR).mkdir(parents=True)
    monkeypatch.setattr(_session_config, "WORKSPACE", workspace)
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(_inbox, "_directions", [])
    return tmp_path


async def test_one_projects_session_cannot_reach_or_feed_anothers_grant(two):
    supervisors = []
    for project in ("proj-a", "proj-b"):
        timeline = _timeline._Timeline()
        bridge = _bridge.Bridge(two / project, on_event=timeline.row)
        credentials = _grants_supervisor.Credentials(project, timeline, bridge, state_root=two / project)
        credentials.start()
        supervisors.append((credentials, timeline))
    (a, _), (b, b_timeline) = supervisors
    try:
        a.store.accept(seal(a.store))
        a_env, b_env = a.session_env(), b.session_env()
        assert a_env[URL_ENV] != b_env[URL_ENV] and a_env[TOKEN_ENV] != b_env[TOKEN_ENV]

        assert fetch(a_env[URL_ENV], a_env[TOKEN_ENV], "/v1/aws/aws-marketplace")[0] == 200
        # B's session, with B's address and token, finds nothing; with A's address, it is not let in.
        assert fetch(b_env[URL_ENV], b_env[TOKEN_ENV], "/v1/aws/aws-marketplace")[0] == 404
        assert fetch(a_env[URL_ENV], b_env[TOKEN_ENV], "/v1/aws/aws-marketplace")[0] == 403
        assert fetch(b_env[URL_ENV], b_env[TOKEN_ENV], "/v1/grants")[1] == {"grants": []}

        # The sealed text of one pasted to the other is refused, and grants nothing.
        assert b.handle(seal(a.store))
        assert b.store.grants() == []
        assert any(text.startswith("sealed credential refused:") for _, _, text in b_timeline._rows)
        assert SECRET not in everything_written(two / "proj-b")
    finally:
        for credentials, _ in supervisors:
            credentials.close()


def _secret_list(secrets: object) -> list[object]:
    """An environment's `secrets` as a list, whichever of the forms Flyte accepts it was given in."""
    if not secrets:
        return []
    return list(secrets) if isinstance(secrets, (list, tuple)) else [secrets]
