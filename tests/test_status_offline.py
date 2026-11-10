"""`factory.status` and the overview read Flyte only: no engine credential, no call to a model provider."""

import json
import socket

import httpx
import pytest

from flyte_agent_factory import _dashboard, _factory, _factory_meters, _secret_env
from tests.test_operate import operate
from tests.test_queue import status_run

# The credentials a pod may carry, by name only. FACTORY_CLAUDE_TOKEN is the factory's mount of
# `[secrets] claude_token`, read to fingerprint a meter account and by nothing a status read does.
CREDENTIALS = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "FACTORY_CLAUDE_TOKEN",
    "CODEX_ACCESS_TOKEN",
    "OPENAI_API_KEY",
    *_secret_env.CREDENTIAL_VARS,
)


@pytest.fixture
def offline(monkeypatch):
    """No engine credential in the environment, and any network use or token read is an error."""
    for name in CREDENTIALS:
        monkeypatch.delenv(name, raising=False)
    calls = []

    def refuse(*args, **kwargs):
        calls.append(args)
        raise AssertionError("status reached for the network or a credential")

    monkeypatch.setattr(httpx.Client, "send", refuse)
    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(_factory_meters, "mounted_token_fingerprint", refuse)
    return calls


def test_status_needs_no_engine_credential(flyte_world, offline):
    status_run(flyte_world, "run")
    result = _factory.read_status("alpha")
    assert result["run"] == "run" and result["activity"] == "reviewing"
    assert json.loads(_run(_factory.status.func("alpha")))["run"] == "run"
    assert offline == []


def test_the_overview_needs_no_engine_credential(flyte_world, offline):
    run = status_run(flyte_world, "run")
    reading = {"account": "claude x", "source": "oauth-usage", "observed_at": 1000.0, "weekly_pct": 40.0}
    snapshot = _dashboard.from_report(run.report)
    assert snapshot is not None
    run.report = _dashboard.status_script({**snapshot, "cost": {"meters": [reading]}})
    view = operate.build_overview([run], now=1000.0)
    assert view["rows"][0]["project"] == "alpha"
    assert "alpha" in operate.overview_text(view)
    assert offline == []


def _run(coroutine):
    import asyncio

    return asyncio.run(coroutine)


def test_status_standards_diagnostics_need_no_engine_credential(flyte_world, offline):
    run = status_run(flyte_world, "run")
    snapshot = _dashboard.from_report(run.report)
    assert snapshot is not None
    standards = {"configured": False, "warning": "no standards configured"}
    run.report = _dashboard.status_script({**snapshot, "standards": standards})
    result = _factory.read_status("alpha", rows=0)
    assert result["standards"] == standards
    assert result["timeline"] == []
    assert offline == []


def test_status_survives_incomplete_workspace_standards(flyte_world, offline, tmp_path, monkeypatch):
    from flyte_agent_factory import _session_config, _timeline
    from tests.test_workspace_standards import workspace_with

    workspace_with(tmp_path, monkeypatch, None)
    (tmp_path / "standards").mkdir()
    (tmp_path / "standards/security.md").write_text("# workspace security\n")
    run = status_run(flyte_world, "run")
    run.report = _timeline._Timeline().render()
    result = _factory.read_status("alpha", rows=0)
    assert "error" not in result
    assert result["run"] == "run" and "review" in result
    assert result["standards"]["configured"] is None
    assert "missing base file: engineering.md" in result["standards"]["warning"]
    with pytest.raises(ValueError, match="missing base file: engineering.md"):
        _session_config._applicable_standards()
    assert offline == []
