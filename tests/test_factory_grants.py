"""`factory.credential_link` and `factory.credentials`, and the credentials section of the report."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from flyte_agent_factory import (
    _dashboard,
    _factory,
    _factory_base,
    _factory_grants,
    _factory_live,
    _grant_seal,
    _timeline,
    agent,
)
from flyte_agent_factory._grants import GrantStore
from tests.test_grants import SECRET, TOKEN, exports

BASE = "https://mcp.example"
SRC = Path(__file__).resolve().parents[1] / "src" / "flyte_agent_factory"


def sealed_text(store: GrantStore) -> str:
    now = time.time()
    payload = {
        "name": "aws-marketplace",
        "kind": "aws",
        "expires_at": int(now + 3600),
        "sealed_at": int(now),
        "value": exports(),
        "region": "",
    }
    return _grant_seal.seal(store.key.public_b64, store.project, payload)


@pytest.fixture
def held() -> GrantStore:
    """A store holding one grant, as a project's would."""
    store = GrantStore("proj-a")
    store.accept(sealed_text(store))
    return store


def report_of(store: GrantStore, **extra_grant_fields: Any) -> str:
    """The project's report page, with `extra_grant_fields` added to every grant the way a bug might."""
    timeline = _timeline._Timeline()
    timeline.header("project", "proj-a")
    timeline.credentials = store
    data = timeline.snapshot()
    data["credentials"]["grants"] = [{**g, **extra_grant_fields} for g in data["credentials"]["grants"]]
    return _dashboard.status_script(data)


@pytest.fixture
def world(monkeypatch, held):
    """One live project, `proj-a`, with a report and an open `direct-` condition."""
    run = SimpleNamespace(name="r1", phase="ActionPhase.RUNNING", get_report=lambda: report_of(held))
    state = SimpleNamespace(run=run, live=[run], inbox="direct-a0-001", report=lambda: report_of(held))
    run.get_report = lambda: state.report()
    monkeypatch.setattr(
        _factory_base, "_labelled_runs", lambda name, **kw: state.live if name == "proj-a" else []
    )
    monkeypatch.setattr(_factory_base, "_phase", lambda r: "RUNNING")
    monkeypatch.setattr(_factory_live, "project_name", lambda r: "proj-a")
    monkeypatch.setattr(_factory_live, "direct_condition", lambda r: state.inbox)
    monkeypatch.setattr(_factory_grants, "read_page_base_url", lambda *args: BASE)
    monkeypatch.setattr(_factory, "operated_live", lambda: state.live)
    return state


def link_for(name="proj-a", credential="aws-marketplace", hours=4.0, live=lambda: []):
    return _factory_grants.credential_link(name, credential, hours, live)


# --- the report ----------------------------------------------------------------------------------------


def test_the_timeline_snapshot_has_a_credentials_section(held):
    timeline = _timeline._Timeline()
    timeline.credentials = held
    section = timeline.snapshot()["credentials"]
    assert (section["key"], section["kid"]) == (held.key.public_b64, held.key.kid)
    [grant] = section["grants"]
    assert (grant["name"], grant["kind"], grant["state"]) == ("aws-marketplace", "aws", "active")
    assert SECRET not in json.dumps(section) and TOKEN not in json.dumps(section)
    assert _timeline._Timeline().snapshot()["credentials"] == {}


def test_the_credentials_survive_report_truncation(held):
    timeline = _timeline._Timeline()
    timeline.header("project", "proj-a")
    timeline.credentials = held
    for i in range(400):
        timeline.row("🔧", f"row {i} " + "x" * 3000)
    page = timeline.render()
    assert len(page.encode()) <= _dashboard.MAX_REPORT_BYTES
    data = _dashboard.from_report(page)
    assert data is not None
    assert len(data.get("timeline") or []) < 400, "the report did not overflow, so this proved nothing"
    assert data["credentials"]["kid"] == held.key.kid
    assert data["credentials"]["key"] == held.key.public_b64
    assert [g["name"] for g in data["credentials"]["grants"]] == ["aws-marketplace"]


def test_read_status_returns_the_credentials(flyte_world, monkeypatch, held):
    monkeypatch.setattr(_factory_base, "report_html", lambda run: report_of(held))
    _factory.launch_project("brief", "statused")
    section = _factory.read_status("statused")["credentials"]
    assert section["key"] == held.key.public_b64 and section["kid"] == held.key.kid
    assert [g["name"] for g in section["grants"]] == ["aws-marketplace"]


def test_read_status_gives_only_metadata_even_if_the_report_holds_more(flyte_world, monkeypatch, held):
    monkeypatch.setattr(_factory_base, "report_html", lambda run: report_of(held, value=SECRET, token=TOKEN))
    _factory.launch_project("brief", "statused")
    text = json.dumps(_factory.read_status("statused"))
    assert SECRET not in text and TOKEN not in text


# --- factory.credential_link ---------------------------------------------------------------------------


def test_credential_link_is_one_the_seal_page_and_store_accept(world, held):
    result = link_for(hours=2.5)
    assert result["paste_to"] == "direct-a0-001"
    assert (result["project"], result["credential"], result["hours"]) == ("proj-a", "aws-marketplace", 2.5)
    link = _grant_seal.parse_link(result["url"])
    assert result["url"].startswith(f"{BASE}/flyte-mcp/grant#")
    assert (link.project, link.name, link.hours) == ("proj-a", "aws-marketplace", 2.5)
    assert (link.public_b64, link.kid) == (held.key.public_b64, held.key.kid)
    assert SECRET not in json.dumps(result) and TOKEN not in json.dumps(result)


def test_the_task_returns_the_same_json(world):
    out = json.loads(asyncio.run(_factory.credential_link.func("proj-a", "aws-marketplace", 1)))
    assert set(out) == {"project", "credential", "hours", "url", "paste_to"}


@pytest.mark.parametrize("credential", ["", "AWS", "aws_marketplace", "aws marketplace", "-x" * 40])
def test_a_bad_credential_name_is_refused(world, credential):
    with pytest.raises(ValueError, match="not a valid credential name"):
        link_for(credential=credential)


@pytest.mark.parametrize("hours", [0, -1, 168.01, 1000, float("nan"), float("inf")])
def test_hours_outside_the_limit_are_refused(world, hours):
    with pytest.raises(ValueError, match="hours must be more than 0 and at most 168"):
        link_for(hours=hours)


def test_the_limits_themselves_are_allowed(world):
    assert link_for(hours=168)["hours"] == 168
    assert link_for(hours=0.25)["hours"] == 0.25


def test_a_project_that_is_not_live_is_refused(world):
    with pytest.raises(ValueError, match="'other' is not live"):
        link_for(name="other")


def test_a_report_with_no_key_is_refused(world):
    world.report = lambda: _dashboard.status_script({"schema": _dashboard.SCHEMA})
    with pytest.raises(ValueError, match="has not published a credential key"):
        link_for()


def test_an_unreadable_report_is_refused_like_a_missing_one(world):
    world.report = lambda: ""
    with pytest.raises(ValueError, match="has not published a credential key"):
        link_for()


def test_a_project_with_no_open_direct_condition_is_refused(world):
    world.inbox = ""
    with pytest.raises(ValueError, match="no open direct- inbox"):
        link_for()


# --- factory.credentials -------------------------------------------------------------------------------


def test_credentials_lists_metadata_only(world, held):
    # A report that carries more than metadata (the section is read from outside the project).
    world.report = lambda: report_of(held, value=SECRET, token=TOKEN, sealed=sealed_text(held), secret=SECRET)
    result = _factory_grants.credentials(lambda: world.live)
    text = json.dumps(result)
    for forbidden in (SECRET, TOKEN, "fgc1.", "secret", "token", "sealed"):
        assert forbidden not in text
    [project] = result
    assert project["project"] == "proj-a"
    [grant] = project["credentials"]
    assert (grant["name"], grant["kind"], grant["state"]) == ("aws-marketplace", "aws", "active")
    assert "expires" in grant


def test_credentials_of_a_project_without_a_section_is_empty(world):
    world.report = lambda: _dashboard.status_script({"schema": _dashboard.SCHEMA})
    assert _factory_grants.credentials(lambda: world.live) == [{"project": "proj-a", "credentials": []}]


def test_the_task_lists_every_live_project(world):
    out = json.loads(asyncio.run(_factory.credentials.func()))
    assert [p["project"] for p in out] == ["proj-a"]


# --- what a sub-agent pod and the engine modules hold --------------------------------------------------


def test_sub_agent_pods_name_no_grant_variable_or_secret():
    for environment in (agent.subagent_env, agent.setup_env):
        assert not [name for name in (environment.env_vars or {}) if name.startswith("AGENT_FACTORY_GRANTS")]
        assert not [s for s in _secret_list(environment.secrets) if "GRANTS" in str(s).upper()]


def test_the_modules_stay_inside_their_line_budgets():
    for file, limit in (("agent.py", 1415), ("_factory.py", 1501)):
        assert len((SRC / file).read_text().splitlines()) <= limit, file


def _secret_list(secrets: object) -> list[object]:
    """An environment's `secrets` as a list, whichever of the forms Flyte accepts it was given in."""
    if not secrets:
        return []
    return list(secrets) if isinstance(secrets, (list, tuple)) else [secrets]
