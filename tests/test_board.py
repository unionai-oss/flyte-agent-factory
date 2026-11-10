"""Offline board contract; no GitHub credentials or network."""

import json
from datetime import date
from typing import Any, cast

import httpx
import pytest

from flyte_agent_factory import (
    _board,
    _profile,
    _session_config,
    _session_setup,
    _supervise_support,
    _timeline,
)
from tests._wait import wait_for


def client(handler):
    return _board.BoardClient("test", transport=httpx.MockTransport(handler))


def test_bootstrap_and_status():
    calls = []
    fields: list[dict[str, Any]] = [
        {
            "id": "status",
            "name": "Status",
            "options": [{"id": name, "name": name} for name in _board.SELECTS["Status"]],
        }
    ]

    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        query = body["query"]
        if "ResolveProject" in query:
            data: dict[str, Any] = {
                "user": {
                    "projectV2": {
                        "id": "project",
                        "fields": {"nodes": fields, "pageInfo": {"hasNextPage": False}},
                    }
                }
            }
        elif "CreateField" in query:
            data = {
                "createProjectV2Field": {
                    "projectV2Field": {
                        "id": body["variables"]["input"]["name"],
                        "name": body["variables"]["input"]["name"],
                        "options": [],
                    }
                }
            }
        else:
            data = {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "item"}}}
        return httpx.Response(
            200,
            json={"data": data},
            headers={"github-authentication-token-expiration": "2026-12-01 00:00:00 UTC"},
        )

    board = client(handle)
    board.bootstrap("owner", "user", 1)
    board.set_status("item", "In flight")
    assert board.expires == date(2026, 12, 1)
    assert calls[-1]["variables"]["input"]["value"] == {"singleSelectOptionId": "In flight"}
    assert len([c for c in calls if "CreateField" in c["query"]]) == 5


def test_unauthorized():
    board = client(lambda request: httpx.Response(401))
    with pytest.raises(_board.BoardError) as error:
        board.bootstrap("owner", "user", 1)
    assert error.value.status == 401


def test_secret_removed(monkeypatch):
    monkeypatch.setenv("GITHUB_BOARD_PAT", "primary")
    monkeypatch.setenv("_UNION_GITHUB_BOARD_PAT", "secondary")
    assert _board.take_token("github_board_pat") == "primary"
    import os

    assert "GITHUB_BOARD_PAT" not in os.environ
    assert "_UNION_GITHUB_BOARD_PAT" not in os.environ


# Rewritten in state step 1: the backlog-age half pinned the removed outbox; the expiry half is kept.
def test_expiry_and_backlog_alerts():
    rows = []
    _board.warn_expiry("2026-11-01", None, lambda *row: rows.append(row), today=date(2026, 10, 22))
    assert rows[-1][0] == "🚨"


def test_no_board():
    from tests.conftest import TEST_PROFILE

    assert _profile.load(str(TEST_PROFILE)).board is None
    assert _board.supervisor(None, "", "", lambda *row: None) is None


def board_profile(tmp_path):
    from tests.conftest import TEST_PROFILE

    return _profile.parse(
        TEST_PROFILE.read_text()
        + """
[board]
owner = "example"
owner_type = "user"
number = 1
pat_secret = "github_board_pat"
pat_expires = "2026-11-01"
""".replace("[board]", f'[state]\nuri = "{tmp_path.as_uri()}"\n[board]')
    )


def test_board_no_longer_requires_state():
    from tests.conftest import TEST_PROFILE

    text = TEST_PROFILE.read_text() + (
        '\n[board]\nowner = "example"\nowner_type = "user"\nnumber = 1\npat_secret = "github_board_pat"\n'
    )
    profile = _profile.parse(text)
    assert profile.board and not profile.state.uri


@pytest.mark.parametrize(
    "value",
    ["number = true", "number = 0", 'owner_type = "team"', 'pat_expires = "tomorrow"', 'unknown = "x"'],
)
def test_profile_rejects_invalid_board(tmp_path, value):
    from tests.conftest import TEST_PROFILE

    table = {"owner": '"example"', "owner_type": '"org"', "number": "1", "pat_secret": '"pat"'}
    key, raw = value.split(" = ")
    table[key] = raw
    text = TEST_PROFILE.read_text() + f'\n[state]\nuri = "{tmp_path.as_uri()}"\n[board]\n'
    text += "\n".join(f"{k} = {v}" for k, v in table.items())
    with pytest.raises(_profile.ProfileError):
        _profile.parse(text)


# Rewritten in state step 1: backlog.json is gone (it had no reader). Kept: the board's order as items()
# pages it, and a 401 that fails the reconcile tick with a red row.
def test_backlog_order_cached_on_failure_and_401_alert(tmp_path, monkeypatch):
    rows, requests = [], []
    profile = board_profile(tmp_path)
    original_client = _board.BoardClient
    unauthorized = False

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if unauthorized:
            return httpx.Response(401)
        query = body["query"]
        if "ResolveProject" in query:
            fields: list[dict[str, Any]] = [
                {"id": name, "name": name, "options": [{"id": opt, "name": opt} for opt in options]}
                for name, options in _board.SELECTS.items()
            ]
            fields += [{"id": name, "name": name} for name in _board.TEXT_FIELDS]
            data: dict[str, Any] = {
                "user": {
                    "projectV2": {"id": "p", "fields": {"nodes": fields, "pageInfo": {"hasNextPage": False}}}
                }
            }
        elif "BoardItems" in query:
            after = body["variables"]["after"]
            number = 8 if after is None else 3
            data = {
                "node": {
                    "items": {
                        "nodes": [
                            {
                                "id": f"item-{number}",
                                "fieldValueByName": {"name": "Ready"},
                                "content": {
                                    "id": f"issue-{number}",
                                    "number": number,
                                    "title": f"Issue {number}",
                                    "repository": {"nameWithOwner": "example/scratch"},
                                    "labels": {"nodes": [{"name": "factory"}]},
                                },
                            }
                        ],
                        "pageInfo": {"hasNextPage": after is None, "endCursor": "page2"},
                    }
                }
            }
        else:
            pytest.fail(f"unexpected write: {query}")
        return httpx.Response(200, json={"data": data})

    monkeypatch.setattr(
        _board, "BoardClient", lambda token: original_client(token, transport=httpx.MockTransport(handle))
    )
    looked_up: list[str] = []
    reconciler = _board.Reconciler(profile, "test", looked_up.append, lambda *row: rows.append(row))
    reconciler.tick()
    assert looked_up == ["example/scratch#8", "example/scratch#3"]
    board = _board.BoardClient("test")
    board.project_id = "p"
    items = board.items()
    assert [i["number"] for i in items] == [8, 3]
    assert all(i["labels"] == ["factory"] for i in items)
    unauthorized = True
    with pytest.raises(_board.BoardError):
        reconciler.tick()
    assert any(icon == "🚨" and "failed" in text for icon, text in rows)


def test_missing_options_preserved_and_text_updated():
    existing = {
        "id": "status",
        "name": "Status",
        "options": [{"id": "custom", "name": "Custom", "color": "BLUE", "description": "keep"}],
    }
    updates = []

    def handle(request):
        body = json.loads(request.content)
        query, variables = body["query"], body["variables"]
        if "ResolveProject" in query:
            data: dict[str, Any] = {
                "organization": {
                    "projectV2": {
                        "id": "p",
                        "fields": {"nodes": [existing], "pageInfo": {"hasNextPage": False}},
                    }
                }
            }
        elif "UpdateField" in query:
            updates.append(variables["input"])
            options = [
                dict(opt, id=opt.get("id", opt["name"])) for opt in variables["input"]["singleSelectOptions"]
            ]
            data = {"updateProjectV2Field": {"projectV2Field": dict(existing, options=options)}}
        elif "CreateField" in query:
            value = variables["input"]
            data = {"createProjectV2Field": {"projectV2Field": {"id": value["name"], "name": value["name"]}}}
        elif "AddItem" in query:
            data = {"addProjectV2ItemById": {"item": {"id": "item"}}}
        else:
            updates.append(variables["input"])
            data = {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "item"}}}
        return httpx.Response(200, json={"data": data})

    board = client(handle)
    board.bootstrap("example", "org", 1)
    assert updates[0]["singleSelectOptions"][0] == existing["options"][0]
    assert {o["name"] for o in updates[0]["singleSelectOptions"]} == set(_board.SELECTS["Status"]) | {
        "Custom"
    }
    assert board.add_item("issue") == "item"
    board.set_text("item", "Flyte", "https://console.example/run")
    assert updates[-1]["value"] == {"text": "https://console.example/run"}


def test_board_deploy_and_session_env(tmp_path, monkeypatch):
    from flyte_agent_factory import _deploy

    profile = board_profile(tmp_path)
    assert "factory-reconcile" in _deploy.factory_envs(profile)
    assert "factory-reconcile" in _deploy.planned_envs(profile)
    assert "github_board_pat=GITHUB_BOARD_PAT" in profile.agent_secrets
    monkeypatch.setattr(_session_config, "_PROFILE", profile)
    assert _session_setup._session_env(
        {"GITHUB_BOARD_PAT": "a", "_UNION_GITHUB_BOARD_PAT": "b", "SAFE": "c"}
    ) == {"SAFE": "c"}


# Rewritten in state step 1 (was an outbox-ordering test): a move whose board write hangs past
# BOARD_MOVE_TIMEOUT_S, and lands only after the next move, must not leave the board on the older status.
async def test_a_move_that_timed_out_and_lands_late_still_sorts_before_the_next(mocked_board, monkeypatch):
    import threading

    state, profile, _ = mocked_board
    worker = _board.supervisor(profile, "test", "example/scratch#8", lambda *row: None)
    assert worker is not None
    monkeypatch.setattr(_supervise_support, "_board_worker", worker)
    monkeypatch.setattr(_supervise_support, "BOARD_MOVE_TIMEOUT_S", 0.1)
    gate = threading.Event()
    hung: list[bool] = []
    real_tick = worker.tick

    def tick():
        if worker._status == "In review" and not hung:
            hung.append(True)
            gate.wait(60)  # the board write hangs for this one move
        return real_tick()

    monkeypatch.setattr(worker, "tick", tick)
    timeline = _timeline._Timeline()
    try:
        await _supervise_support._move_board("In flight", timeline)
        await _supervise_support._move_board("In review", timeline)
        await _supervise_support._move_board("Done", timeline)
    finally:
        gate.set()
    await wait_for(lambda: not worker.pending)
    statuses = [w["value"]["singleSelectOptionId"] for w in state["writes"] if w["fieldId"] == "Status"]
    assert statuses[-1] == "Done", statuses


# Rewritten in state step 1: the moves are written to the board, not queued; the same values in order.
async def test_supervisor_milestones_only_queue(mocked_board, monkeypatch):

    state, profile, _ = mocked_board
    worker = _board.supervisor(profile, "test", "example/scratch#8", lambda *row: None)
    assert worker is not None
    monkeypatch.setattr(_supervise_support, "_board_worker", worker)
    timeline = _timeline._Timeline()
    await _supervise_support._move_board(
        "In flight", timeline, Flyte="https://console.example/run", Branch="factory/x", Engine="claude"
    )
    await _supervise_support._move_board("In review", timeline)
    await _supervise_support._move_board("Done", timeline)
    writes = state["writes"]
    assert [w["value"]["singleSelectOptionId"] for w in writes if w["fieldId"] == "Status"] == [
        "In flight",
        "In review",
        "Done",
    ]
    assert {w["fieldId"] for w in writes if w["fieldId"] != "Status"} == set(_board.TEXT_FIELDS)


# Rewritten in state step 1: a move made before the board has its fields waits in memory until the
# reconciler (the only bootstrapper) creates them, then lands.
def test_bootstrap_precedes_queued_moves(tmp_path, monkeypatch):
    profile = board_profile(tmp_path)
    writes = []
    created: list[dict[str, Any]] = []

    def handle(request):
        body = json.loads(request.content)
        query = body["query"]
        if "ResolveProject" in query:
            data: dict[str, Any] = {
                "user": {
                    "projectV2": {"id": "p", "fields": {"nodes": created, "pageInfo": {"hasNextPage": False}}}
                }
            }
        elif "BoardItems" in query:
            data = {
                "node": {
                    "items": {
                        "nodes": [
                            {
                                "id": "item",
                                "fieldValueByName": None,
                                "content": {
                                    "id": "issue",
                                    "number": 8,
                                    "title": "x",
                                    "repository": {"nameWithOwner": "example/scratch"},
                                    "labels": {"nodes": []},
                                },
                            }
                        ],
                        "pageInfo": {"hasNextPage": False},
                    }
                }
            }
        elif "CreateField" in query:
            value = body["variables"]["input"]
            writes.append(value["name"])
            field = {"id": value["name"], "name": value["name"]}
            if "singleSelectOptions" in value:
                field["options"] = [dict(opt, id=opt["name"]) for opt in value["singleSelectOptions"]]
            created.append(field)
            data = {"createProjectV2Field": {"projectV2Field": field}}
        else:
            writes.append(body["variables"]["input"]["value"])
            data = {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "item"}}}
        return httpx.Response(200, json={"data": data})

    real = _board.BoardClient
    monkeypatch.setattr(
        _board, "BoardClient", lambda token: real(token, transport=httpx.MockTransport(handle))
    )
    worker = _board.supervisor(profile, "test", "example/scratch#8", lambda *row: None)
    assert worker is not None
    worker.move("In flight")
    assert worker.pending and not writes
    _board.Reconciler(profile, "test", lambda ref: None, lambda *row: None).tick()
    worker.tick()
    assert writes[-1] == {"singleSelectOptionId": "In flight"}
    assert not worker.pending


# Re-pointed in state step 1 from the retired factory-board-sync to factory-reconcile; same assertions.
def test_sync_environment_only_mounts_pat(tmp_path):
    import os
    import subprocess
    import sys

    profile = board_profile(tmp_path)
    env = {key: value for key, value in os.environ.items() if key != _profile.PROFILE_ENV}
    env[_profile.TOML_ENV] = profile.text
    code = """
from flyte_agent_factory import _reconcile
assert _reconcile.env.name == "factory-reconcile"
assert [(s.key, s.as_env_var) for s in _reconcile.env.secrets] == [("github_board_pat", "GITHUB_BOARD_PAT")]
assert _reconcile.env.interruptible is True
(trigger,) = _reconcile.reconcile.triggers
assert trigger.automation.interval_minutes == 60
assert trigger.auto_activate is True
"""
    subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)


def board_handler(state):
    """A board holding example/scratch#8, with every field already bootstrapped."""
    fields: list[dict[str, Any]] = [
        {"id": name, "name": name, "options": [{"id": opt, "name": opt} for opt in options]}
        for name, options in _board.SELECTS.items()
    ]
    fields += [{"id": name, "name": name, "dataType": "TEXT"} for name in _board.TEXT_FIELDS]

    def handle(request):
        body = json.loads(request.content)
        query = body["query"]
        if state.get("status"):
            return httpx.Response(state["status"])
        if "ResolveProject" in query:
            nodes = [f for f in fields if f["name"] not in state.get("deleted", ())]
            if state.get("size_is_text"):  # bootstrap refuses a Size that is not single-select
                nodes = [f for f in nodes if f["name"] != "Size"] + [
                    {"id": "Size", "name": "Size", "dataType": "TEXT"}
                ]
            data: dict[str, Any] = {
                "user": {
                    "projectV2": {"id": "p", "fields": {"nodes": nodes, "pageInfo": {"hasNextPage": False}}}
                }
            }
        elif "BoardItems" in query:
            if state.get("node_null"):
                return httpx.Response(200, json={"data": {"node": None}})
            item = {
                "id": "item-8",
                "fieldValueByName": None,
                "content": {
                    "id": "issue-8",
                    "number": 8,
                    "title": "x",
                    "repository": {"nameWithOwner": "example/scratch"},
                    "labels": {"nodes": []},
                },
            }
            data = {"node": {"items": {"nodes": [item], "pageInfo": {"hasNextPage": False}}}}
        elif "IssueNode" in query:
            data = {"repository": {"issue": None}}  # no issue exists beyond the one on the board
        elif "SetField" in query:
            state.setdefault("writes", []).append(body["variables"]["input"])
            data = {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "item-8"}}}
        elif "CreateField" in query:
            name = body["variables"]["input"]["name"]
            state.setdefault("created", []).append(name)
            state["deleted"] = tuple(n for n in state.get("deleted", ()) if n != name)
            data = {"createProjectV2Field": {"projectV2Field": next(f for f in fields if f["name"] == name)}}
        else:
            pytest.fail(f"unexpected request: {query}")
        return httpx.Response(200, json={"data": data})

    return handle


@pytest.fixture
def mocked_board(tmp_path, monkeypatch):
    state: dict[str, Any] = {}
    original_client = _board.BoardClient
    monkeypatch.setattr(
        _board,
        "BoardClient",
        lambda token: original_client(token, transport=httpx.MockTransport(board_handler(state))),
    )
    return state, board_profile(tmp_path), tmp_path


# Rewritten in state step 1: the missing issue's move waits in its own supervisor's memory, not the outbox.
def test_issue_missing_from_board_does_not_block_other_issues(mocked_board):
    state, profile, tmp_path = mocked_board
    rows = []
    missing = _board.supervisor(profile, "test", "example/scratch#999", lambda *row: rows.append(row))
    assert missing is not None
    present = _board.supervisor(profile, "test", "example/scratch#8", lambda *row: rows.append(row))
    assert present is not None
    missing.move("In flight")
    present.move("In flight")
    present.tick()
    missing.tick()
    assert state["writes"][-1] == {
        "projectId": "p",
        "itemId": "item-8",
        "fieldId": "Status",
        "value": {"singleSelectOptionId": "In flight"},
    }
    # The missing issue's move is retried without holding up #8, and the owner is told once.
    assert missing.pending and not present.pending
    assert len([row for row in rows if row[0] == "🚨" and "example/scratch#999" in row[1]]) == 1


# Round 3 changed this from dead-lettering the write to healing the board (coordinator-approved).
# Re-pointed in state step 1: the reconciler, the only bootstrapper, recreates the field; same assertions.
@pytest.mark.parametrize("field", ["Engine", "Status"])
def test_field_deleted_after_bootstrap_is_recreated_and_the_write_lands(mocked_board, field):
    state, profile, tmp_path = mocked_board
    rows = []
    worker = _board.supervisor(profile, "test", "example/scratch#8", lambda *row: rows.append(row))
    assert worker is not None
    # Bootstrapped earlier; the owner has since deleted the field.
    state["deleted"] = (field,)
    worker.move("In flight", Engine="claude")
    assert bool(worker.pending)  # bool(): a plain `assert` would narrow it and hide the later `not pending`
    _board.Reconciler(profile, "test", lambda ref: None, lambda *row: rows.append(row)).tick()
    worker.tick()
    assert state["created"] == [field]
    assert sorted(w["fieldId"] for w in state["writes"]) == ["Engine", "Status"]
    assert not worker.pending
    assert any(field in text for _, text in rows)


# Rewritten in state step 1: there is no drain and no permanent-failure list; a missing field is a 409,
# which the supervisor retries in memory until the reconciler bootstraps the field back.
def test_write_for_a_field_missing_mid_drain_is_retried(tmp_path):
    board = client(lambda request: pytest.fail("no request for a missing field"))
    board.fields = {}
    with pytest.raises(_board.BoardError) as error:
        board.set_text("item", "Engine", "claude")
    assert error.value.status == 409
    with pytest.raises(_board.BoardError) as error:
        board.set_status("item", "Done")
    assert error.value.status == 409


def test_issue_matches_board_case_insensitively(mocked_board):
    state, profile, tmp_path = mocked_board
    worker = _board.supervisor(profile, "test", "Example/Scratch#8", lambda *row: None)
    assert worker is not None
    worker.move("In flight")
    worker.tick()
    assert state["writes"][-1]["itemId"] == "item-8"


@pytest.mark.parametrize(
    "issue, expected",
    [
        ("example/scratch#8", "example/scratch#8"),
        ("Example/Scratch#8", "example/scratch#8"),
        (" scratch#12 ", "example/scratch#12"),
        ("Scratch#12", "example/scratch#12"),
        ("factory-engine#3", "acme/factory-engine#3"),
    ],
)
def test_normalise_issue(issue, expected):
    repos = ["git@github.com:Example/scratch.git", "https://github.com/acme/factory-engine", "not a url"]
    assert _board.normalise_issue(issue, repos) == expected


@pytest.mark.parametrize("issue", ["scratch", "#8", "unknown#8", "example/scratch#0", "a/b/c#1", "o/r#x"])
def test_normalise_issue_rejects(issue):
    with pytest.raises(ValueError, match="owner/repo#N"):
        _board.normalise_issue(issue, ["git@github.com:example/scratch.git"])


async def test_project_start_normalises_or_drops_issue(tmp_path, monkeypatch):

    rows = []
    profile = board_profile(tmp_path)
    monkeypatch.setattr(_session_config, "_PROFILE", profile)
    assert (
        _supervise_support._board_issue("Example/Scratch#8", lambda *row: rows.append(row))
        == "example/scratch#8"
    )
    assert _supervise_support._board_issue("work-sandbox#8", lambda *row: rows.append(row)) == ""
    assert rows and rows[-1][0] == "⚠️" and "work-sandbox#8" in rows[-1][1]
    assert _supervise_support._board_issue("", lambda *row: rows.append(row)) == ""


# Re-pointed in state step 1 from the board-sync tick to the reconcile tick, which now raises as well.
def test_null_board_node_is_a_board_error(mocked_board):
    state, profile, tmp_path = mocked_board
    state["node_null"] = True
    rows = []
    with pytest.raises(_board.BoardError):
        _board.Reconciler(profile, "test", lambda ref: None, lambda *row: rows.append(row)).tick()
    assert any("Board sync failed" in text for _, text in rows)
    board = _board.BoardClient("test")
    board.project_id = "p"
    with pytest.raises(_board.BoardError) as error:
        board.items()
    assert error.value.status == 404


async def test_board_loop_reports_unexpected_errors_and_keeps_ticking(monkeypatch):
    import asyncio

    rows, ticks = [], []

    class Timeline:
        def row(self, *row):
            rows.append(row)

        async def flush(self, force=False):
            pass

    class Worker:
        def tick(self):
            ticks.append(1)
            if len(ticks) >= 3:
                stop.set()
            raise TypeError("'NoneType' object is not subscriptable")

    stop = asyncio.Event()
    monkeypatch.setattr(_supervise_support, "_board_worker", Worker())
    monkeypatch.setattr(_supervise_support, "_BOARD_EVERY_S", 0.01)
    await asyncio.wait_for(
        _supervise_support._board_loop(cast(_timeline._Timeline, Timeline()), stop), timeout=30
    )
    assert len(ticks) == 3
    assert [row[0] for row in rows] == ["💥"]
    assert "TypeError" in rows[0][1]


def test_token_whitespace_is_stripped(monkeypatch):
    monkeypatch.setenv("GITHUB_BOARD_PAT", "  ghp_secret\n")
    assert _board.take_token("github_board_pat") == "ghp_secret"
    monkeypatch.setenv("GITHUB_BOARD_PAT", "\n")
    monkeypatch.setenv("_UNION_GITHUB_BOARD_PAT", "ghp_other\n")
    assert _board.take_token("github_board_pat") == "ghp_other"


def test_engine_label_is_every_engine_that_ran(monkeypatch):

    monkeypatch.setattr(_session_config, "_load_usage", lambda: None)
    monkeypatch.setattr(_session_config, "_usage", {})
    assert _supervise_support._engines_used() == "claude"
    monkeypatch.setattr(
        _session_config, "_usage", {"delegate@0": {"engines": ["codex"]}, "qa@1": {"engines": ["claude"]}}
    )
    assert _supervise_support._engines_used() == "claude+codex"


OPTIONAL_ENVS = [
    (webhook, maintainers, board)
    for webhook in (False, True)
    for maintainers in (False, True)
    for board in (False, True)
]


def expected_factory_envs(webhook, maintainers, board, app=False):
    expected = {"factory", "factory-mcp", "factory-deployer", "gardening-scan"}
    expected |= {"factory-webhook"} if webhook else set()
    # Board events need the private task even when maintainer intake is disabled.
    expected |= {"factory-issues"} if webhook and (maintainers or board) else set()
    # State step 1: the reconciler heals the board, and with the App the issue milestones.
    expected |= {"factory-reconcile"} if board or app else set()
    return expected


@pytest.mark.parametrize("webhook, maintainers, board", OPTIONAL_ENVS)
def test_factory_envs_for_every_optional_env(tmp_path, webhook, maintainers, board):
    import dataclasses

    from flyte_agent_factory import _deploy

    profile = board_profile(tmp_path)
    profile = dataclasses.replace(
        profile,
        board=profile.board if board else None,
        github=dataclasses.replace(
            profile.github,
            webhook_secret="hook" if webhook else "",
            maintainers=("owner",) if maintainers else (),
        ),
    )
    assert _deploy.factory_envs(profile) == expected_factory_envs(webhook, maintainers, board)


def graphql_error(error):
    def handle(request):
        return httpx.Response(200, json={"data": None, "errors": [error]})

    return client(handle)


@pytest.mark.parametrize(
    "error, status",
    [
        ({"type": "FORBIDDEN", "message": "x"}, 403),
        ({"type": "INSUFFICIENT_SCOPES", "message": "x"}, 403),
        ({"type": "RATE_LIMITED", "message": "x"}, 429),
        ({"message": "Something went wrong while executing your query."}, 502),
        ({"type": "INTERNAL", "message": "x"}, 502),
        ({"type": "NOT_FOUND", "message": "x"}, 400),
        ({"type": "UNPROCESSABLE", "message": "x"}, 400),
        ({"message": "x", "extensions": {"code": "argumentLiteralsIncompatible"}}, 400),
    ],
)
def test_graphql_errors_are_classified(error, status):
    with pytest.raises(_board.BoardError) as raised:
        graphql_error(error)._query("mutation {x}")
    assert raised.value.status == status


@pytest.mark.parametrize("kind", ["FORBIDDEN", "INSUFFICIENT_SCOPES", None])
def test_write_denied_or_failing_is_retried_not_dropped(mocked_board, monkeypatch, kind):
    state, profile, tmp_path = mocked_board
    handle = board_handler(state)

    def deny_writes(request):
        if "SetField" in json.loads(request.content)["query"]:
            error = {"message": "x"} | ({"type": kind} if kind else {})
            return httpx.Response(200, json={"data": None, "errors": [error]})
        return handle(request)

    rows = []
    real = type(_board.BoardClient("test"))  # the fixture's factory wraps the real class
    monkeypatch.setattr(
        _board, "BoardClient", lambda token: real(token, transport=httpx.MockTransport(deny_writes))
    )
    worker = _board.supervisor(profile, "test", "example/scratch#8", lambda *row: rows.append(row))
    assert worker is not None
    worker.move("In flight")
    worker.tick()
    # Rewritten in state step 1: kept in the supervisor's memory rather than the outbox, then applied.
    assert bool(worker.pending)  # bool(): a plain `assert` would narrow it and hide the later `not pending`
    if kind:
        assert any(row[0] == "🚨" for row in rows)
    monkeypatch.setattr(
        _board, "BoardClient", lambda token: real(token, transport=httpx.MockTransport(handle))
    )
    worker.tick()
    assert not worker.pending
    assert state["writes"][-1]["value"] == {"singleSelectOptionId": "In flight"}


FACTORY_CHILD_STUB = """
import atexit, json, flyte
flyte.init_from_api_key = lambda **kwargs: None
names = []


class Deployment:
    envs = {}

    def __init__(self, envs):
        # flyte.deploy deploys each env's `depends_on` too: factory-issues rides on the webhook app.
        pending = list(envs)
        while pending:
            env = pending.pop()
            names.append(env.name)
            pending.extend(env.depends_on)

    def summary_repr(self):
        return ""


flyte.deploy = lambda *envs, dry_run: [Deployment(envs)]
atexit.register(lambda: print("ENVS=" + json.dumps(sorted(names))))
"""


@pytest.mark.parametrize("webhook, maintainers, board", OPTIONAL_ENVS)
def test_factory_child_deploys_the_optional_envs(tmp_path, webhook, maintainers, board):
    import os
    import subprocess
    import sys
    from pathlib import Path

    from flyte_agent_factory import _deploy
    from tests.test_deployer import make_profile

    text = make_profile(tmp_path.as_uri()).text
    text += 'webhook_secret = "hook"\n' if webhook else ""
    text += 'maintainers = ["owner"]\n' if maintainers else ""
    text += (
        '\n[board]\nowner = "example"\nowner_type = "user"\nnumber = 1\npat_secret = "github_board_pat"\n'
        if board
        else ""
    )
    profile = _profile.parse(text, "work+deployer")
    src = Path(_deploy.__file__).resolve().parents[1]
    env = _profile.child_env(profile, dict(os.environ))
    env.update(PYTHONPATH=str(src), _UNION_EAGER_API_KEY="unused")
    result = subprocess.run(
        [sys.executable, "-c", FACTORY_CHILD_STUB + _deploy.FACTORY_CHILD, str(src), "1", "p", "d"],
        env=env,
        cwd=src,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    (line,) = [line for line in result.stdout.splitlines() if line.startswith("ENVS=")]
    assert set(json.loads(line.removeprefix("ENVS="))) == expected_factory_envs(
        webhook, maintainers, board, app=True
    )


# Rewritten in state step 1: the move is written to the board item, not queued; and milestones need the
# App and an issue rather than intake (the gating change), so the parameter is the App.
@pytest.mark.parametrize("app", [False, True])
async def test_factory_launch_with_an_issue_moves_its_board_item(flyte_world, monkeypatch, app):
    import dataclasses

    from flyte_agent_factory import _factory, _factory_base, _issues
    from tests.test_reconcile import App, Board

    board = Board({"you/repo#12": {"status": "Ready"}}).install(monkeypatch)
    profile = dataclasses.replace(
        _factory_base.PROFILE,
        repo="git@github.com:you/repo.git",
        repos={"project": "git@github.com:you/repo.git"},
        github=_profile.Github(app_id="1" if app else "", private_key_secret="key" if app else ""),
        board=_profile.Board(owner="example", owner_type="user", number=1, pat_secret="github_board_pat"),
    )
    monkeypatch.setattr(_factory_base, "PROFILE", profile)
    monkeypatch.setattr(_factory, "REPO", profile.repo)
    monkeypatch.setattr(_session_config, "_PROFILE", profile)
    _factory.launch_project("brief", "issue-widget", issue="You/Repo#12")
    timeline = _timeline._Timeline()
    # `App` fakes the GitHub App's comment calls, not the whole `GithubApp`
    timeline.issues = _issues.for_project(profile, "issue-widget", "run", cast(Any, App() if app else None))
    assert bool(timeline.issues) == app
    # `_start_board` sets the module global; restored after the test, so no later test writes to a
    # board through it.
    monkeypatch.setattr(_supervise_support, "_board_worker", None)
    await _supervise_support._start_board("", "issue-widget", "token", timeline)
    assert ("you/repo#12", "Status", "In flight") in board.writes


@pytest.mark.parametrize("framed, rows_expected", [(False, 0), (True, 1)])
async def test_board_without_an_issue_warns_only_for_an_issue_brief(
    tmp_path, monkeypatch, framed, rows_expected
):
    from flyte_agent_factory import _issues

    profile = board_profile(tmp_path)
    monkeypatch.setattr(_session_config, "_PROFILE", profile)
    brief = _issues.frame_brief("you/repo#12", "Owner", "x", title="Widget") if framed else "a brief"
    monkeypatch.setattr(_session_config, "_project_brief", brief)
    rows = []
    timeline = _timeline._Timeline()
    monkeypatch.setattr(timeline, "row", lambda *row: rows.append(row))
    # `_start_board` sets the module global; restored after the test, so no later test writes to a
    # board through it.
    monkeypatch.setattr(_supervise_support, "_board_worker", None)
    await _supervise_support._start_board("", "no-issue", "token", timeline)
    assert len([row for row in rows if row[0] == "⚠️"]) == rows_expected


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(403, headers={"retry-after": "60"}),
        httpx.Response(403, json={"message": "You have exceeded a secondary rate limit."}),
        httpx.Response(403, headers={"x-ratelimit-remaining": "0"}),
    ],
)
def test_http_403_rate_limit_is_a_rate_limit(response):
    with pytest.raises(_board.BoardError) as error:
        client(lambda request: response)._query("query {x}")
    assert error.value.status == 429


def test_http_403_without_rate_limit_stays_forbidden():
    with pytest.raises(_board.BoardError) as error:
        client(lambda request: httpx.Response(403, json={"message": "Resource not accessible"}))._query("q")
    assert error.value.status == 403


# GitHub refuses these as custom Projects v2 field names ("Name cannot have a reserved value"); a bootstrap
# that creates one can never succeed. "Type" was refused live; the rest are the board's built-in fields.
GITHUB_RESERVED_FIELD_NAMES = {
    "Title",
    "Assignees",
    "Labels",
    "Linked pull requests",
    "Milestone",
    "Repository",
    "Reviewers",
    "Type",
    "Parent issue",
    "Sub-issues progress",
    "Created",
    "Updated",
    "Closed",
    "Tracks",
    "Tracked by",
}


def test_the_board_creates_no_field_github_reserves():
    created = {name for name in (*_board.SELECTS, *_board.TEXT_FIELDS) if name != "Status"}
    assert not {name.lower() for name in created} & {name.lower() for name in GITHUB_RESERVED_FIELD_NAMES}


def test_items_include_planning_facts_across_pages():
    cursors = []

    def handle(request):
        body = json.loads(request.content)
        query = body["query"]
        assert "__typename" in query and "body" in query and "state" in query
        assert "DraftIssue" in query and "closedByPullRequestsReferences" in query
        after = body["variables"]["after"]
        cursors.append(after)
        content: dict[str, Any] = (
            {
                "__typename": "Issue",
                "id": "I1",
                "number": 1,
                "title": "issue",
                "body": "## Files\n- a",
                "state": "CLOSED",
                "repository": {"nameWithOwner": "example/repo"},
                "labels": {"nodes": [{"name": "priority"}]},
                "closedByPullRequestsReferences": {
                    "nodes": [
                        {
                            "id": "PR1",
                            "number": 7,
                            "url": "https://github.com/example/repo/pull/7",
                            "state": "OPEN",
                            "repository": {"nameWithOwner": "example/repo"},
                        }
                    ]
                },
            }
            if after is None
            else {
                "__typename": "PullRequest",
                "id": "PR2",
                "number": 2,
                "title": "pr",
                "body": "pr body",
                "state": "OPEN",
                "repository": {"nameWithOwner": "example/repo"},
                "labels": {"nodes": []},
            }
        )
        nodes = [{"id": "item-" + content["id"], "content": content, "Flyte": {"text": "run-url"}}]
        if after is not None:
            nodes.append(
                {
                    "id": "draft",
                    "content": {
                        "__typename": "DraftIssue",
                        "id": "D1",
                        "title": "draft",
                        "body": "draft body",
                    },
                }
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "node": {
                        "items": {
                            "nodes": nodes,
                            "pageInfo": {"hasNextPage": after is None, "endCursor": "next"},
                        }
                    }
                }
            },
        )

    board = client(handle)
    board.project_id = "project"
    items = board.items()
    assert cursors == [None, "next"]
    assert [i["type"] for i in items] == ["issue", "pr", "draft"]
    assert [i["position"] for i in items] == [0, 1, 2]
    assert items[0]["open"] is False and items[1]["open"] is True
    assert items[0]["body"] == "## Files\n- a"
    assert items[0]["linked_prs"][0]["id"] == "PR1"
    assert items[0]["linked_run"] == "run-url"
    assert items[0]["labels"] == ["priority"]
    assert items[2]["body"] == "draft body"


def _listing(contents):
    def handle(request):
        nodes = [{"id": f"item-{i}", "content": content} for i, content in enumerate(contents)]
        page = {"nodes": nodes, "pageInfo": {"hasNextPage": False}}
        return httpx.Response(200, json={"data": {"node": {"items": page}}})

    board = client(handle)
    board.project_id = "project"
    return board


def _issue(**kwargs):
    return {
        "__typename": "Issue",
        "id": "I1",
        "number": 1,
        "title": "issue",
        "body": "",
        "state": "OPEN",
        "repository": {"nameWithOwner": "example/repo"},
        "labels": {"nodes": []},
        **kwargs,
    }


def test_linked_prs_include_only_open_pull_requests():
    def pull(number, state):
        return {
            "id": f"PR{number}",
            "number": number,
            "url": f"https://github.com/example/repo/pull/{number}",
            "state": state,
            "repository": {"nameWithOwner": "example/repo"},
        }

    nodes = [pull(1, "MERGED"), pull(2, "OPEN"), pull(3, "CLOSED")]
    (found,) = _listing([_issue(closedByPullRequestsReferences={"nodes": nodes})]).items()
    assert [pr["number"] for pr in found["linked_prs"]] == [2]


def test_content_without_an_id_or_a_repository_is_skipped_and_logged(caplog):
    contents = [
        {"__typename": "Issue"},  # content the token cannot read
        _issue(id="I2", number=2, repository=None),
        {"__typename": "PullRequest", "id": "P3", "number": 3, "title": "pr"},
        _issue(id="I4", number=4),
        {"__typename": "DraftIssue", "id": "D5", "title": "draft"},
        _issue(id="I6", number=6, body=None),  # GitHub answers null for an empty body
    ]
    with caplog.at_level("WARNING", logger=_board.__name__):
        found = _listing(contents).items()
    assert [(i["node_id"], i["position"], i["body"]) for i in found] == [
        ("I4", 3, ""),
        ("D5", 4, ""),
        ("I6", 5, ""),
    ]
    assert "no id" in caplog.text and "no repository" in caplog.text


def test_advance_status_never_leaves_done_and_only_moves_forward():
    from flyte_agent_factory import _board

    class Fake:
        def __init__(self, status, open_=True):
            self.now = (status, open_)
            self.writes = []

        def status_of(self, item):
            return self.now

        def set_status(self, item, status):
            self.writes.append(status)

    done = Fake("Done", open_=False)
    assert not _board.advance_status(done, "i", "In flight") and done.writes == []
    review = Fake("In review")
    assert not _board.advance_status(review, "i", "Ready") and review.writes == []
    ready = Fake("Ready")
    assert _board.advance_status(ready, "i", "In flight") and ready.writes == ["In flight"]
    fresh = Fake("")
    assert _board.advance_status(fresh, "i", "Backlog") and fresh.writes == ["Backlog"]
